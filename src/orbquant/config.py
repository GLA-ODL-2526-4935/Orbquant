"""Frozen application configuration and the reviewed default risk parameters."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

CONFIG_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class DataConfig:
    feed: str = "iex"
    symbols: tuple[str, ...] = ("SPY", "QQQ", "IWM")
    source: str = "alpaca"
    raw_dir: str = "data/raw"
    parquet_dir: str = "data/bars"
    request_timeout_seconds: float = 20.0
    max_retries: int = 5
    retry_base_seconds: float = 0.5
    retry_max_seconds: float = 8.0
    historical_availability_lag_seconds: int = 5
    max_bar_age_seconds: int = 90
    minimum_training_years: int = 3

    def __post_init__(self) -> None:
        if self.feed not in {"iex", "local", "synthetic"}:
            raise ValueError("feed must be one of the explicit iex, local, or synthetic modes")
        if self.source not in {"alpaca", "local", "synthetic"}:
            raise ValueError("source must be alpaca, local, or synthetic")
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols must be non-empty and unique")
        if any(not symbol or symbol != symbol.upper() for symbol in self.symbols):
            raise ValueError("symbols must be uppercase ticker symbols")
        if self.request_timeout_seconds <= 0 or self.retry_base_seconds < 0 or self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("invalid request timeout or retry backoff")
        if self.max_retries < 0 or self.historical_availability_lag_seconds < 0 or self.max_bar_age_seconds <= 0:
            raise ValueError("invalid data retry or freshness setting")
        if self.minimum_training_years < 0:
            raise ValueError("minimum_training_years must be nonnegative")


@dataclass(frozen=True, slots=True)
class FeatureConfig:
    version: str = "intraday-120-v1"
    lookback_minutes: int = 120
    return_windows: tuple[int, ...] = (1, 5, 15, 30)
    volatility_windows: tuple[int, ...] = (5, 15, 30)
    require_same_session: bool = True
    reject_gaps: bool = True

    def __post_init__(self) -> None:
        if not self.version or self.lookback_minutes < 1:
            raise ValueError("feature version and positive lookback are required")
        for windows in (self.return_windows, self.volatility_windows):
            if not windows or any(window < 1 for window in windows):
                raise ValueError("feature windows must contain positive lengths")


@dataclass(frozen=True, slots=True)
class ResearchConfig:
    first_horizon_minutes: int = 15
    last_horizon_minutes: int = 60
    horizon_step_minutes: int = 1
    holdout_complete_months: int = 6
    minimum_training_months: int = 18
    validation_months: int = 3
    evaluation_months: int = 3
    advance_months: int = 3
    maximum_configs_per_family_fold: int = 3
    training_sample_limit: int | None = 5000
    seeds: tuple[int, ...] = (11, 29, 47)
    cpu_inference_budget_seconds: Decimal = Decimal("1")
    bootstrap_replicates: int = 2000

    def __post_init__(self) -> None:
        if self.first_horizon_minutes <= 0 or self.last_horizon_minutes < self.first_horizon_minutes:
            raise ValueError("research horizons must be positive and ordered")
        if self.horizon_step_minutes <= 0:
            raise ValueError("horizon_step_minutes must be positive")
        for value in (self.holdout_complete_months, self.minimum_training_months, self.validation_months, self.evaluation_months, self.advance_months, self.maximum_configs_per_family_fold):
            if value <= 0:
                raise ValueError("research windows and budget values must be positive")
        if self.training_sample_limit is not None and (
            isinstance(self.training_sample_limit, bool)
            or not isinstance(self.training_sample_limit, int)
            or self.training_sample_limit < 1
        ):
            raise ValueError("training_sample_limit must be a positive integer or None")
        if len(set(self.seeds)) != len(self.seeds) or not self.seeds:
            raise ValueError("research seeds must be non-empty and unique")
        if not self.cpu_inference_budget_seconds.is_finite() or self.cpu_inference_budget_seconds <= 0:
            raise ValueError("CPU inference budget must be a positive finite Decimal")
        if self.bootstrap_replicates < 1:
            raise ValueError("bootstrap_replicates must be positive")


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    min_mean_net_bps: Decimal = Decimal("5")
    min_q10_net_bps: Decimal = Decimal("-50")
    allocation_fraction: Decimal = Decimal("0.10")
    entry_boundary_delay_minutes: int = 1
    parameter_grid_mean_net_bps: tuple[Decimal, ...] = (Decimal("5"), Decimal("10"))
    parameter_grid_min_q10_bps: tuple[Decimal, ...] = (Decimal("-25"), Decimal("-50"))

    def __post_init__(self) -> None:
        for name in ("min_mean_net_bps", "min_q10_net_bps", "allocation_fraction"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{name} must be a finite Decimal")
        if not Decimal("0") < self.allocation_fraction <= Decimal("1"):
            raise ValueError("allocation_fraction must be in (0, 1]")
        if self.entry_boundary_delay_minutes < 1:
            raise ValueError("entry reference must be at least one minute after decision")


@dataclass(frozen=True, slots=True)
class RiskConfig:
    max_instrument_equity_fraction: Decimal = Decimal("0.10")
    max_gross_equity_fraction: Decimal = Decimal("0.30")
    max_positions: int = 3
    daily_loss_limit_fraction: Decimal = Decimal("0.01")
    max_drawdown_fraction: Decimal = Decimal("0.05")
    max_estimated_roundtrip_bps: Decimal = Decimal("20")
    max_bar_age_seconds: int = 90

    def __post_init__(self) -> None:
        for name in ("max_instrument_equity_fraction", "max_gross_equity_fraction", "daily_loss_limit_fraction", "max_drawdown_fraction", "max_estimated_roundtrip_bps"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a finite nonnegative Decimal")
        if self.max_positions < 1 or self.max_bar_age_seconds < 1:
            raise ValueError("max_positions and max_bar_age_seconds must be positive")


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    simulated: bool = True
    spread_bps_per_side: Decimal = Decimal("2")
    slippage_bps_per_side: Decimal = Decimal("3")
    fee_per_share_usd: Decimal = Decimal("0")
    max_previous_bar_volume_fraction: Decimal = Decimal("0.01")
    whole_shares_only: bool = True
    settlement_business_days: int = 1

    def __post_init__(self) -> None:
        for name in ("spread_bps_per_side", "slippage_bps_per_side", "fee_per_share_usd", "max_previous_bar_volume_fraction"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a finite nonnegative Decimal")
        if self.max_previous_bar_volume_fraction > 1:
            raise ValueError("participation fraction cannot exceed one")
        if self.settlement_business_days < 0:
            raise ValueError("settlement_business_days must be nonnegative")


@dataclass(frozen=True, slots=True)
class CapitalConfig:
    starting_account_currency: str = "GBP"
    starting_account_amount: Decimal = Decimal("10000")
    strategy_currency: str = "USD"
    fx_conversion_fee_bps: Decimal = Decimal("10")
    currency_minor_units: int = 2

    def __post_init__(self) -> None:
        if not self.starting_account_currency or not self.strategy_currency:
            raise ValueError("capital currencies must be non-empty")
        if not self.starting_account_amount.is_finite() or self.starting_account_amount <= 0:
            raise ValueError("starting_account_amount must be positive and finite")
        if not self.fx_conversion_fee_bps.is_finite() or self.fx_conversion_fee_bps < 0:
            raise ValueError("fx_conversion_fee_bps must be nonnegative and finite")
        if self.currency_minor_units < 0:
            raise ValueError("currency_minor_units must be nonnegative")


@dataclass(frozen=True, slots=True)
class FxConfig:
    provider: str = "ecb"
    base_currency: str = "EUR"
    rate_date_policy: str = "previous_published_business_day"
    reporting_currency: str = "GBP"


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Entire versioned config; immutable so IDs cannot drift mid-run."""

    config_version: str = CONFIG_SCHEMA_VERSION
    strategy_id: str = "us-etf-long-intraday-v1"
    timezone: str = "America/New_York"
    exchange_calendar: str = "XNYS"
    data: DataConfig = field(default_factory=DataConfig)
    features: FeatureConfig = field(default_factory=FeatureConfig)
    research: ResearchConfig = field(default_factory=ResearchConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    capital: CapitalConfig = field(default_factory=CapitalConfig)
    fx: FxConfig = field(default_factory=FxConfig)

    def __post_init__(self) -> None:
        if not self.config_version or not self.strategy_id or not self.timezone or not self.exchange_calendar:
            raise ValueError("config identity, timezone, and calendar are required")
        if not self.execution.simulated:
            raise ValueError("this application supports simulated execution only")

    @property
    def config_id(self) -> str:
        from orbquant.provenance import content_id

        return content_id("config", self)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_CONFIG = AppConfig()


def config_from_dict(record: Mapping[str, Any]) -> AppConfig:
    """Build and validate an immutable configuration from decoded JSON data."""
    if not isinstance(record, Mapping):
        raise ValueError("config document must be an object")
    values = dict(record)
    nested = {
        "data": (DataConfig, {"symbols": tuple}),
        "features": (FeatureConfig, {"return_windows": tuple, "volatility_windows": tuple}),
        "research": (ResearchConfig, {"seeds": tuple, "cpu_inference_budget_seconds": Decimal}),
        "policy": (PolicyConfig, {
            "min_mean_net_bps": Decimal, "min_q10_net_bps": Decimal,
            "allocation_fraction": Decimal, "parameter_grid_mean_net_bps": lambda x: tuple(map(Decimal, x)),
            "parameter_grid_min_q10_bps": lambda x: tuple(map(Decimal, x)),
        }),
        "risk": (RiskConfig, {
            "max_instrument_equity_fraction": Decimal, "max_gross_equity_fraction": Decimal,
            "daily_loss_limit_fraction": Decimal, "max_drawdown_fraction": Decimal,
            "max_estimated_roundtrip_bps": Decimal,
        }),
        "execution": (ExecutionConfig, {
            "spread_bps_per_side": Decimal, "slippage_bps_per_side": Decimal,
            "fee_per_share_usd": Decimal, "max_previous_bar_volume_fraction": Decimal,
        }),
        "capital": (CapitalConfig, {
            "starting_account_amount": Decimal, "fx_conversion_fee_bps": Decimal,
        }),
        "fx": (FxConfig, {}),
    }
    try:
        for key, (constructor, conversions) in nested.items():
            if key not in values:
                raise ValueError(f"config is missing the {key} section")
            section = values[key]
            if not isinstance(section, Mapping):
                raise ValueError(f"config {key} section must be an object")
            converted = dict(section)
            for name, converter in conversions.items():
                if name not in converted:
                    continue
                if converter is Decimal:
                    converted[name] = Decimal(str(converted[name]))
                elif converter is tuple:
                    converted[name] = tuple(converted[name])
                else:
                    converted[name] = converter(converted[name])
            values[key] = constructor(**converted)
        return AppConfig(**values)
    except (ArithmeticError, TypeError, KeyError) as exc:
        raise ValueError(f"invalid config: {exc}") from exc


def save_config(config: AppConfig, path: str | Path) -> Path:
    """Persist the complete resolved config and its content-derived identity."""
    if not isinstance(config, AppConfig):
        raise TypeError("config must be an AppConfig")
    from orbquant.provenance import canonical_json

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json({"config": config.as_dict(), "config_id": config.config_id}) + "\n"
    temporary = target.with_name(f".{target.name}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def load_config(path: str | Path) -> AppConfig:
    """Load a strict resolved config JSON document and verify its saved hash."""
    from orbquant.provenance import content_id

    def reject_constant(value: str):
        raise ValueError(f"non-finite JSON number is forbidden: {value}")

    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=reject_constant)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read config document: {exc}") from exc
    if not isinstance(document, Mapping) or set(document) != {"config", "config_id"}:
        raise ValueError("config document must contain exactly config and config_id")
    config = config_from_dict(document["config"])
    actual = content_id("config", config)
    if document["config_id"] != actual:
        raise ValueError("saved config ID does not match the resolved config contents")
    return config
