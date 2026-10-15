"""Independent hard risk validation, separate from learned forecasts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping, Sequence

from orbquant.config import AppConfig, DEFAULT_CONFIG
from orbquant.contracts import (
    AccountSnapshot,
    Bar,
    Candidate,
    Fill,
    ForecastBatch,
    ForecastPoint,
    OrderSide,
    PolicyDecision,
    RiskDecision,
)

BPS = Decimal("10000")


@dataclass(slots=True)
class Guard:
    config: AppConfig = DEFAULT_CONFIG
    emergency_halt: bool = False
    _drawdown_latched: bool = False
    _manual_reset_required: bool = False

    @staticmethod
    def expected_horizons(config: AppConfig = DEFAULT_CONFIG) -> tuple[int, ...]:
        research = config.research
        return tuple(range(research.first_horizon_minutes, research.last_horizon_minutes + 1, research.horizon_step_minutes))

    def validate_forecasts(
        self,
        forecasts: Sequence[object],
        *,
        expected_data_id: str | None = None,
        expected_model_id: str | None = None,
        expected_config_id: str | None = None,
        expected_model_version: str | None = None,
        expected_feature_version: str | None = None,
        expected_decision_at: datetime | None = None,
    ) -> tuple[str, ...]:
        """Recheck untrusted model outputs at the hard-risk boundary."""
        issues: list[str] = []
        expected = self.expected_horizons(self.config)
        seen: set[str] = set()
        for index, batch in enumerate(forecasts):
            if not isinstance(batch, ForecastBatch):
                issues.append(f"forecast[{index}]: model emitted {type(batch).__name__}, expected ForecastBatch")
                continue
            symbol = batch.symbol
            if not isinstance(symbol, str) or not symbol.strip():
                issues.append(f"forecast[{index}]: batch symbol is malformed")
                continue
            if symbol in seen:
                issues.append(f"{symbol}: duplicate forecast batch")
                continue
            seen.add(symbol)
            if symbol not in self.config.data.symbols:
                issues.append(f"{symbol}: symbol is not enabled")
            if batch.schema_version != "1.0":
                issues.append(f"{symbol}: incompatible forecast schema")
            if expected_data_id is not None and batch.data_id != expected_data_id:
                issues.append(f"{symbol}: forecast data ID mismatch")
            if expected_model_id is not None and batch.model_id != expected_model_id:
                issues.append(f"{symbol}: forecast model ID mismatch")
            if expected_config_id is not None and batch.config_id != expected_config_id:
                issues.append(f"{symbol}: forecast config ID mismatch")
            if expected_model_version is not None and batch.model_version != expected_model_version:
                issues.append(f"{symbol}: incompatible model version")
            if expected_feature_version is not None and batch.feature_version != expected_feature_version:
                issues.append(f"{symbol}: incompatible feature version")
            decision_at = batch.decision_at
            if (
                not isinstance(decision_at, datetime)
                or decision_at.tzinfo is None
                or decision_at.utcoffset() != timezone.utc.utcoffset(decision_at)
                or (expected_decision_at is not None and decision_at != expected_decision_at)
            ):
                issues.append(f"{symbol}: forecast decision time is invalid or mismatched")
            if not isinstance(batch.points, (tuple, list)):
                issues.append(f"{symbol}: forecast points are not a sequence")
                continue
            points = batch.points
            valid_points: list[ForecastPoint] = []
            for point_index, point in enumerate(points):
                if not isinstance(point, ForecastPoint):
                    issues.append(f"{symbol}: malformed forecast point at index {point_index}")
                    continue
                horizon = point.horizon_minutes
                if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon <= 0:
                    issues.append(f"{symbol}: malformed forecast horizon at index {point_index}")
                    continue
                valid_points.append(point)
            horizons = tuple(point.horizon_minutes for point in valid_points)
            if horizons != expected:
                issues.append(f"{symbol}: required forecast horizons are missing, reordered, or unexpected")
            for point in valid_points:
                values = (point.mean_return, point.q10, point.q50, point.q90)
                if any(not isinstance(value, Decimal) or not value.is_finite() for value in values):
                    issues.append(f"{symbol}:{point.horizon_minutes}: non-finite forecast output")
                    continue
                if not point.q10 <= point.q50 <= point.q90:
                    issues.append(f"{symbol}:{point.horizon_minutes}: crossing forecast quantiles")
        return tuple(issues)

    def latch_drawdown(self) -> None:
        self._drawdown_latched = True
        self._manual_reset_required = True

    def check_actual_fill(
        self,
        fill: Fill,
        context: AccountSnapshot,
        *,
        order_reservation: Decimal = Decimal("0"),
    ) -> str | None:
        """Recheck hard cash/exposure limits at the simulated fill price.

        A next-minute open can gap far past the decision close. This guard is
        independent of the forecast and runs before any BUY is journaled.
        Existing sell fills are allowed to reduce risk; the ledger still
        validates that they close no more than the actual long position.
        """
        if fill.side is OrderSide.SELL:
            return None
        if fill.symbol in context.position_symbols:
            return "actual entry would add to an existing position"
        if len(context.positions) >= self.config.risk.max_positions:
            return "actual entry would exceed the maximum position count"
        if not order_reservation.is_finite() or order_reservation < 0:
            return "actual entry has an invalid persisted cash reservation"
        notional = fill.price * fill.quantity
        fee = fill.fees
        per_name = sum((position.market_value for position in context.positions if position.symbol == fill.symbol), Decimal("0"))
        gross = sum((position.market_value for position in context.positions), Decimal("0"))
        if per_name + notional > context.equity * self.config.risk.max_instrument_equity_fraction:
            return "actual fill price would exceed the per-instrument equity limit"
        if gross + notional > context.equity * self.config.risk.max_gross_equity_fraction:
            return "actual fill price would exceed the gross exposure limit"
        spendable = context.available_cash + order_reservation
        if notional + fee > spendable:
            return "actual fill price and fees exceed settled cash available to this order"
        return None

    def reset_emergency_halt(self) -> None:
        self.emergency_halt = False

    def manual_reset_drawdown(self, *, between_sessions: bool, reconciled: bool) -> None:
        if not between_sessions or not reconciled:
            raise ValueError("drawdown reset requires an off-session reconciled account")
        self._drawdown_latched = False
        self._manual_reset_required = False

    def evaluate(
        self,
        decision: PolicyDecision,
        context: AccountSnapshot,
        *,
        now: datetime,
        latest_bars: Mapping[str, Bar],
        forecast_issues: Sequence[str] = (),
        reserved_symbols: Sequence[str] = (),
        pending_entry_notional: Decimal = Decimal("0"),
    ) -> RiskDecision:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("guard time must be timezone-aware")
        now_utc = now.astimezone(timezone.utc)
        reasons: list[str] = list(forecast_issues)
        liquidation = False
        if context.equity <= context.session_open_equity * (Decimal("1") - self.config.risk.daily_loss_limit_fraction):
            reasons.append("daily loss limit breached")
            liquidation = True
        if context.equity <= context.high_water_equity * (Decimal("1") - self.config.risk.max_drawdown_fraction):
            self.latch_drawdown()
            reasons.append("persistent high-water drawdown limit breached; manual reset required")
            liquidation = True
        if self._drawdown_latched or context.halted:
            reasons.extend(context.halt_reasons or ("account is halted",))
            liquidation = True
        if self.emergency_halt:
            reasons.append("emergency halt is active")
            liquidation = True
        if reasons:
            return RiskDecision(approved=(), rejected=tuple((candidate.symbol, "; ".join(reasons)) for candidate in decision.candidates), halt_reasons=tuple(dict.fromkeys(reasons)), liquidation_requested=liquidation)

        approved: list[Candidate] = []
        rejected = list(decision.rejected)
        occupied = set(context.position_symbols) | set(reserved_symbols)
        max_positions = self.config.risk.max_positions
        gross_exposure = sum((position.market_value for position in context.positions), Decimal("0")) + pending_entry_notional
        remaining_cash = context.available_cash
        if not pending_entry_notional.is_finite() or pending_entry_notional < 0:
            return RiskDecision(approved=(), rejected=tuple((candidate.symbol, "invalid pending entry reservation") for candidate in decision.candidates), halt_reasons=("invalid pending entry reservation",))
        for candidate in decision.candidates:
            label = f"{candidate.symbol}:{candidate.horizon_minutes}m"
            bar = latest_bars.get(candidate.symbol)
            if bar is None:
                rejected.append((label, "no current completed bar for GUARD"))
                continue
            age_seconds = (now_utc - bar.end).total_seconds()
            if bar.available_at > now_utc or age_seconds < 0:
                rejected.append((label, "latest completed bar is unavailable at guard time"))
                continue
            if age_seconds > self.config.risk.max_bar_age_seconds:
                rejected.append((label, f"latest bar is stale ({age_seconds:.1f}s)"))
                continue
            if candidate.estimated_roundtrip_bps > self.config.risk.max_estimated_roundtrip_bps:
                rejected.append((label, "estimated roundtrip cost exceeds hard limit"))
                continue
            if candidate.symbol in occupied:
                rejected.append((label, "symbol already has a position or reserved entry"))
                continue
            if len(occupied) >= max_positions:
                rejected.append((label, "maximum position count reached"))
                continue
            mark = bar.close
            estimated_notional = mark * candidate.quantity
            max_symbol_value = context.equity * self.config.risk.max_instrument_equity_fraction
            max_gross_value = context.equity * self.config.risk.max_gross_equity_fraction
            if estimated_notional > max_symbol_value:
                rejected.append((label, "candidate exceeds per-instrument equity limit"))
                continue
            if gross_exposure + estimated_notional > max_gross_value:
                rejected.append((label, "candidate exceeds total gross exposure limit"))
                continue
            estimated_cash = estimated_notional * (Decimal("1") + candidate.estimated_roundtrip_bps / BPS / 2)
            if estimated_cash > remaining_cash:
                rejected.append((label, "candidate exceeds settled unreserved cash"))
                continue
            approved.append(candidate)
            occupied.add(candidate.symbol)
            gross_exposure += estimated_notional
            remaining_cash -= estimated_cash
        return RiskDecision(approved=tuple(approved), rejected=tuple(rejected))
