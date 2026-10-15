"""Versioned, immutable messages shared by research and execution code.

Returns and rates are decimal fractions (``0.001`` means 10 bp); all prices,
money, and costs use :class:`~decimal.Decimal`. Contract timestamps are aware
UTC datetimes. The contracts deliberately avoid I/O and wall-clock defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Iterable, Mapping, Protocol, Sequence, runtime_checkable

CONTRACT_SCHEMA_VERSION = "1.0"


def require_utc(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{name} must be UTC")


def require_decimal(value: Decimal, name: str, *, nonnegative: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    if nonnegative and value < 0:
        raise ValueError(f"{name} must be nonnegative")


def _nonempty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderState(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    SUBMITTED = "submitted"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class IncidentSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class GateStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True, slots=True)
class Bar:
    """One immutable OHLCV bar, available only at/after its interval ends."""

    symbol: str
    feed: str
    start: datetime
    end: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    revision: int = 0

    def __post_init__(self) -> None:
        _nonempty(self.symbol, "symbol")
        _nonempty(self.feed, "feed")
        for name in ("start", "end", "available_at"):
            require_utc(getattr(self, name), name)
        if not self.start < self.end:
            raise ValueError("bar interval must have positive duration")
        if self.available_at < self.end:
            raise ValueError("a completed bar cannot be available before its end")
        for name in ("open", "high", "low", "close"):
            require_decimal(getattr(self, name), name, nonnegative=True)
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("bar high/low do not contain open and close")
        if self.high < self.low:
            raise ValueError("bar high cannot be below low")
        if isinstance(self.volume, bool) or not isinstance(self.volume, int) or self.volume < 0:
            raise ValueError("volume must be a nonnegative integer")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("revision must be a nonnegative integer")

    @property
    def event_id(self) -> str:
        """Stable identity for deduplicating the same feed revision."""
        from orbquant.provenance import content_hash

        return content_hash((self.symbol, self.feed, self.start, self.end, self.revision))

    @property
    def market_identity(self) -> tuple[object, ...]:
        """Immutable market content, excluding per-delivery receipt time.

        A websocket can redeliver the same completed bar after reconnect with a
        later local receipt timestamp.  That delivery metadata is recorded by
        the inbox, while the first observed availability stays attached to the
        event used for decisions.  Prices, size, feed, interval, and revision
        remain part of the identity and conflicting payloads are rejected.
        """
        return (
            self.symbol, self.feed, self.start, self.end, self.revision,
            self.open, self.high, self.low, self.close, self.volume,
        )


@dataclass(frozen=True, slots=True)
class FeatureWindow:
    symbol: str
    decision_at: datetime
    last_completed_bar: Bar
    values: tuple[float, ...]
    feature_version: str
    valid: bool = True
    invalid_reason: str | None = None
    feature_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _nonempty(self.symbol, "symbol")
        _nonempty(self.feature_version, "feature_version")
        require_utc(self.decision_at, "decision_at")
        if self.last_completed_bar.symbol != self.symbol:
            raise ValueError("window symbol must match its last completed bar")
        if self.last_completed_bar.available_at > self.decision_at:
            raise ValueError("window cannot use a bar unavailable at decision time")
        if not self.values:
            raise ValueError("feature window must contain at least one value")
        if any(not isinstance(value, (int, float)) or not _finite_float(value) for value in self.values):
            raise ValueError("feature values must be finite numbers")
        if self.feature_names and len(self.feature_names) != len(self.values):
            raise ValueError("feature_names and values must have matching lengths")
        if self.valid and self.invalid_reason:
            raise ValueError("a valid feature window cannot have an invalid_reason")


def _finite_float(value: int | float) -> bool:
    import math

    return math.isfinite(float(value))


@dataclass(frozen=True, slots=True)
class ForecastPoint:
    """One horizon's net-unadjusted return distribution forecast."""

    horizon_minutes: int
    mean_return: Decimal
    q10: Decimal
    q50: Decimal
    q90: Decimal

    def __post_init__(self) -> None:
        if isinstance(self.horizon_minutes, bool) or not isinstance(self.horizon_minutes, int) or self.horizon_minutes <= 0:
            raise ValueError("horizon_minutes must be a positive integer")
        for name in ("mean_return", "q10", "q50", "q90"):
            require_decimal(getattr(self, name), name)
        if not self.q10 <= self.q50 <= self.q90:
            raise ValueError("forecast quantiles must satisfy q10 <= q50 <= q90")


@dataclass(frozen=True, slots=True)
class ForecastBatch:
    decision_at: datetime
    symbol: str
    model_id: str
    data_id: str
    config_id: str
    points: tuple[ForecastPoint, ...]
    model_version: str = "1"
    schema_version: str = CONTRACT_SCHEMA_VERSION
    feature_version: str = "intraday-120-v1"

    def __post_init__(self) -> None:
        require_utc(self.decision_at, "decision_at")
        for name in ("symbol", "model_id", "data_id", "config_id", "schema_version", "model_version", "feature_version"):
            _nonempty(getattr(self, name), name)
        if not self.points:
            raise ValueError("forecast batch must contain at least one horizon")
        horizons = tuple(point.horizon_minutes for point in self.points)
        if len(set(horizons)) != len(horizons):
            raise ValueError("forecast horizons must be unique")

    def point_for(self, horizon_minutes: int) -> ForecastPoint:
        for point in self.points:
            if point.horizon_minutes == horizon_minutes:
                return point
        raise KeyError(horizon_minutes)

    @property
    def forecast_id(self) -> str:
        from orbquant.provenance import content_hash

        return content_hash(self)


@dataclass(frozen=True, slots=True)
class Candidate:
    symbol: str
    horizon_minutes: int
    forecast_id: str
    estimated_roundtrip_bps: Decimal
    quantity: int
    entry_reference_at: datetime
    exit_reference_at: datetime
    expected_net_return: Decimal
    q10_net_return: Decimal
    reason: str = ""

    def __post_init__(self) -> None:
        _nonempty(self.symbol, "symbol")
        _nonempty(self.forecast_id, "forecast_id")
        if self.horizon_minutes <= 0 or self.quantity <= 0:
            raise ValueError("candidate horizon and quantity must be positive")
        require_decimal(self.estimated_roundtrip_bps, "estimated_roundtrip_bps", nonnegative=True)
        require_decimal(self.expected_net_return, "expected_net_return")
        require_decimal(self.q10_net_return, "q10_net_return")
        require_utc(self.entry_reference_at, "entry_reference_at")
        require_utc(self.exit_reference_at, "exit_reference_at")
        if self.exit_reference_at <= self.entry_reference_at:
            raise ValueError("candidate exit reference must follow entry reference")


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    quantity: int
    average_cost: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal = Decimal("0")
    opened_at: datetime | None = None

    def __post_init__(self) -> None:
        _nonempty(self.symbol, "symbol")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity <= 0:
            raise ValueError("position quantity must be a positive integer")
        for name in ("average_cost", "market_value", "unrealized_pnl"):
            require_decimal(getattr(self, name), name)
        if self.opened_at is not None:
            require_utc(self.opened_at, "opened_at")


@dataclass(frozen=True, slots=True)
class Provenance:
    run_id: str
    data_id: str
    model_id: str
    config_id: str
    schema_version: str = CONTRACT_SCHEMA_VERSION
    code_version: str = "unknown"

    def __post_init__(self) -> None:
        for name in ("run_id", "data_id", "model_id", "config_id", "schema_version", "code_version"):
            _nonempty(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    idempotency_key: str
    symbol: str
    side: OrderSide
    quantity: int
    scheduled_reference_at: datetime
    state: OrderState = OrderState.PROPOSED
    reason: str = ""
    provenance: Provenance | None = None
    parent_order_id: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for name in ("order_id", "idempotency_key", "symbol"):
            _nonempty(getattr(self, name), name)
        if not isinstance(self.side, OrderSide):
            raise ValueError("side must be an OrderSide")
        if not isinstance(self.state, OrderState):
            raise ValueError("state must be an OrderState")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity <= 0:
            raise ValueError("order quantity must be a positive integer")
        require_utc(self.scheduled_reference_at, "scheduled_reference_at")
        keys = [key for key, _ in self.metadata]
        if len(keys) != len(set(keys)):
            raise ValueError("order metadata keys must be unique")


@dataclass(frozen=True, slots=True)
class Fill:
    fill_id: str
    order_id: str
    symbol: str
    side: OrderSide
    reference_at: datetime
    recognized_at: datetime
    quantity: int
    price: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    fees: Decimal
    event_id: str

    def __post_init__(self) -> None:
        for name in ("fill_id", "order_id", "symbol", "event_id"):
            _nonempty(getattr(self, name), name)
        if not isinstance(self.side, OrderSide):
            raise ValueError("side must be an OrderSide")
        require_utc(self.reference_at, "reference_at")
        require_utc(self.recognized_at, "recognized_at")
        if self.recognized_at < self.reference_at:
            raise ValueError("fill cannot be recognized before its reference time")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity <= 0:
            raise ValueError("fill quantity must be a positive integer")
        require_decimal(self.price, "price", nonnegative=True)
        if self.price <= 0:
            raise ValueError("fill price must be greater than zero")
        for name in ("spread_cost", "slippage_cost", "fees"):
            require_decimal(getattr(self, name), name, nonnegative=True)


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    as_of: datetime
    settled_cash: Decimal
    reserved_cash: Decimal
    unsettled_cash: Decimal
    positions: tuple[Position, ...]
    equity: Decimal
    session_open_equity: Decimal
    high_water_equity: Decimal
    halted: bool = False
    halt_reasons: tuple[str, ...] = ()
    trading_date: str | None = None

    def __post_init__(self) -> None:
        require_utc(self.as_of, "as_of")
        for name in ("settled_cash", "reserved_cash", "unsettled_cash", "equity", "session_open_equity", "high_water_equity"):
            require_decimal(getattr(self, name), name)
        if self.reserved_cash < 0:
            raise ValueError("reserved_cash must be nonnegative")
        symbols = [position.symbol for position in self.positions]
        if len(symbols) != len(set(symbols)):
            raise ValueError("account snapshot cannot contain duplicate symbol positions")

    @property
    def available_cash(self) -> Decimal:
        return self.settled_cash - self.reserved_cash

    @property
    def position_symbols(self) -> frozenset[str]:
        return frozenset(position.symbol for position in self.positions)


@dataclass(frozen=True, slots=True)
class Incident:
    incident_id: str
    occurred_at: datetime
    category: str
    severity: IncidentSeverity
    message: str
    symbol: str | None = None
    event_id: str | None = None
    resolved_at: datetime | None = None
    details: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for name in ("incident_id", "category", "message"):
            _nonempty(getattr(self, name), name)
        require_utc(self.occurred_at, "occurred_at")
        if self.resolved_at is not None:
            require_utc(self.resolved_at, "resolved_at")
            if self.resolved_at < self.occurred_at:
                raise ValueError("incident cannot resolve before it occurred")


@dataclass(frozen=True, slots=True)
class ResearchSplit:
    name: str
    train_start: datetime
    train_end: datetime
    validation_start: datetime
    validation_end: datetime
    evaluation_start: datetime
    evaluation_end: datetime
    label_maturity_at: datetime
    fold: int = 0
    purged_sample_count: int = 0

    def __post_init__(self) -> None:
        _nonempty(self.name, "name")
        for name in (
            "train_start", "train_end", "validation_start", "validation_end",
            "evaluation_start", "evaluation_end", "label_maturity_at",
        ):
            require_utc(getattr(self, name), name)
        if not self.train_start < self.train_end <= self.validation_start < self.validation_end <= self.evaluation_start < self.evaluation_end:
            raise ValueError("research split intervals must be ordered and non-overlapping")
        if self.label_maturity_at < self.evaluation_end:
            raise ValueError("split labels cannot be used before they mature")
        if self.fold < 0 or self.purged_sample_count < 0:
            raise ValueError("fold and purged_sample_count must be nonnegative")


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    artifact_id: str
    kind: str
    created_at: datetime
    content_hash: str
    data_id: str
    config_id: str
    schema_version: str
    code_version: str
    feature_version: str
    training_period_start: datetime | None = None
    training_period_end: datetime | None = None
    seed: int | None = None
    checksums: tuple[tuple[str, str], ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for name in ("artifact_id", "kind", "content_hash", "data_id", "config_id", "schema_version", "code_version", "feature_version"):
            _nonempty(getattr(self, name), name)
        require_utc(self.created_at, "created_at")
        if self.training_period_start is not None:
            require_utc(self.training_period_start, "training_period_start")
        if self.training_period_end is not None:
            require_utc(self.training_period_end, "training_period_end")
        if (self.training_period_start is None) != (self.training_period_end is None):
            raise ValueError("training period start and end must be provided together")
        if self.training_period_start and self.training_period_end and self.training_period_start >= self.training_period_end:
            raise ValueError("training period must have positive duration")


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    status: GateStatus
    observed: str
    threshold: str
    explanation: str = ""


@dataclass(frozen=True, slots=True)
class PromotionReport:
    report_id: str
    artifact_id: str
    evaluated_at: datetime
    status: GateStatus
    gates: tuple[GateResult, ...]
    holdout_used: bool
    policy_id: str
    research_data_id: str
    operational_readiness: GateStatus = GateStatus.INSUFFICIENT_EVIDENCE
    evidence_session_count: int = 0
    source_evidence_id: str | None = None
    coverage_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("report_id", "artifact_id", "policy_id", "research_data_id"):
            _nonempty(getattr(self, name), name)
        require_utc(self.evaluated_at, "evaluated_at")
        if self.evidence_session_count < 0:
            raise ValueError("evidence_session_count must be nonnegative")
        if self.source_evidence_id is not None:
            _nonempty(self.source_evidence_id, "source_evidence_id")
        if self.coverage_id is not None:
            _nonempty(self.coverage_id, "coverage_id")


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    candidates: tuple[Candidate, ...]
    rejected: tuple[tuple[str, str], ...] = ()
    abstentions: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: tuple[Candidate, ...]
    rejected: tuple[tuple[str, str], ...] = ()
    halt_reasons: tuple[str, ...] = ()
    liquidation_requested: bool = False


@dataclass(frozen=True, slots=True)
class DataBatch:
    bars: tuple[Bar, ...]
    source_id: str
    retrieved_at: datetime
    next_page_token: str | None = None
    coverage_complete: bool = False
    issues: tuple[str, ...] = ()
    excluded_count: int = 0

    def __post_init__(self) -> None:
        _nonempty(self.source_id, "source_id")
        require_utc(self.retrieved_at, "retrieved_at")
        if self.excluded_count < 0:
            raise ValueError("excluded_count must be nonnegative")


@runtime_checkable
class MarketDataProvider(Protocol):
    def fetch_history(self, symbol: str, start: datetime, end: datetime, *, feed: str) -> DataBatch: ...

    def iter_live(self, symbols: Sequence[str], *, feed: str) -> Iterable[Bar]: ...


@runtime_checkable
class FeatureBuilder(Protocol):
    feature_version: str

    def build(self, bars: Sequence[Bar], decision_at: datetime) -> FeatureWindow: ...


@runtime_checkable
class Forecaster(Protocol):
    model_id: str

    def predict(self, window: FeatureWindow, horizons: Sequence[int]) -> ForecastBatch: ...


@runtime_checkable
class TradingPolicy(Protocol):
    policy_id: str

    def decide(self, forecasts: Sequence[ForecastBatch], context: AccountSnapshot, *, latest_bars: Mapping[str, Bar]) -> PolicyDecision: ...


@runtime_checkable
class RiskEngine(Protocol):
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
    ) -> RiskDecision: ...


@runtime_checkable
class SimulatedBroker(Protocol):
    def submit(self, order: Order, *, now: datetime, previous_bar_volume: int | None = None) -> Order: ...

    def on_bar(self, bar: Bar, *, now: datetime) -> object: ...

    def on_clock(self, now: datetime) -> object: ...


@runtime_checkable
class Ledger(Protocol):
    def snapshot(self, *, as_of: datetime) -> AccountSnapshot: ...

    def apply_fill(self, fill: Fill) -> AccountSnapshot: ...

    def reserve_order(self, order: Order, reserve_amount: Decimal, *, occurred_at: datetime) -> Order: ...


@runtime_checkable
class EventClock(Protocol):
    def now(self) -> datetime: ...
