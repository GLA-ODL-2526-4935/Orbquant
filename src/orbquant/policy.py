"""Transparent long-only selection from frozen model forecasts."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Mapping, Sequence

from orbquant.calendar import TradingCalendar
from orbquant.config import AppConfig, DEFAULT_CONFIG
from orbquant.contracts import AccountSnapshot, Bar, Candidate, ForecastBatch, PolicyDecision

BPS = Decimal("10000")


@dataclass(frozen=True, slots=True)
class MeanAfterCostsPolicy:
    config: AppConfig = DEFAULT_CONFIG
    calendar: TradingCalendar | None = None

    @property
    def policy_id(self) -> str:
        return f"mean-after-costs:{self.config.config_id}"

    def _roundtrip_cost_bps(self, price: Decimal) -> Decimal:
        execution = self.config.execution
        total = (execution.spread_bps_per_side + execution.slippage_bps_per_side) * 2
        fee_bps = execution.fee_per_share_usd / price * BPS * 2
        return total + fee_bps

    def _shares(self, equity: Decimal, price: Decimal, cost_bps: Decimal) -> int:
        fraction = self.config.policy.allocation_fraction
        estimated_buy_price = price * (Decimal("1") + cost_bps / BPS / 2)
        if estimated_buy_price <= 0:
            return 0
        budget = equity * fraction
        return int((budget / estimated_buy_price).to_integral_value(rounding=ROUND_FLOOR))

    def decide(
        self,
        forecasts: Sequence[ForecastBatch],
        context: AccountSnapshot,
        *,
        latest_bars: Mapping[str, Bar],
    ) -> PolicyDecision:
        """Return at most one qualifying horizon per symbol, ranked deterministically."""
        rejected: list[tuple[str, str]] = []
        winners: list[Candidate] = []
        seen_symbols: set[str] = set()
        calendar = self.calendar or TradingCalendar(self.config.exchange_calendar, timezone_name=self.config.timezone)
        for batch in sorted(forecasts, key=lambda item: item.symbol):
            if batch.symbol in seen_symbols:
                rejected.append((batch.symbol, "duplicate forecast batch for symbol"))
                continue
            seen_symbols.add(batch.symbol)
            bar = latest_bars.get(batch.symbol)
            if bar is None:
                rejected.append((batch.symbol, "no completed reference bar"))
                continue
            if bar.symbol != batch.symbol or bar.available_at > batch.decision_at:
                rejected.append((batch.symbol, "reference bar is incompatible with decision"))
                continue
            if bar.feed.lower() != self.config.data.feed.lower():
                rejected.append((batch.symbol, "reference bar feed does not match configured feed"))
                continue
            entry_at = calendar.next_minute_start_after(batch.decision_at)
            if entry_at is None:
                rejected.append((batch.symbol, "no eligible entry minute remains this session"))
                continue
            cost_bps = self._roundtrip_cost_bps(bar.close)
            shares = self._shares(context.equity, bar.close, cost_bps)
            if shares < 1:
                rejected.append((batch.symbol, "target allocation is below one whole share"))
                continue
            eligible: list[Candidate] = []
            for point in batch.points:
                label = f"{batch.symbol}:{point.horizon_minutes}m"
                exit_at = calendar.exit_reference(entry_at, point.horizon_minutes)
                if exit_at is None:
                    rejected.append((label, "horizon exits outside the eligible session window"))
                    continue
                mean_net = point.mean_return - cost_bps / BPS
                q10_net = point.q10 - cost_bps / BPS
                if mean_net < self.config.policy.min_mean_net_bps / BPS:
                    rejected.append((label, "expected net return is below policy threshold"))
                    continue
                if q10_net < self.config.policy.min_q10_net_bps / BPS:
                    rejected.append((label, "q10 net return is below policy threshold"))
                    continue
                eligible.append(Candidate(
                    symbol=batch.symbol,
                    horizon_minutes=point.horizon_minutes,
                    forecast_id=batch.forecast_id,
                    estimated_roundtrip_bps=cost_bps,
                    quantity=shares,
                    entry_reference_at=entry_at,
                    exit_reference_at=exit_at,
                    expected_net_return=mean_net,
                    q10_net_return=q10_net,
                    reason="mean and q10 clear the configured cost-aware thresholds",
                ))
            if not eligible:
                rejected.append((batch.symbol, "no horizon met both policy thresholds"))
                continue
            # Score by expected net return per trading minute, then shorter hold.
            best = min(eligible, key=lambda candidate: (
                -candidate.expected_net_return / Decimal(candidate.horizon_minutes),
                candidate.horizon_minutes,
                candidate.symbol,
            ))
            winners.append(best)
        winners.sort(key=lambda candidate: (
            -candidate.expected_net_return / Decimal(candidate.horizon_minutes),
            candidate.horizon_minutes,
            candidate.symbol,
        ))
        return PolicyDecision(candidates=tuple(winners), rejected=tuple(rejected))
