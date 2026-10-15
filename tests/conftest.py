"""Shared deterministic builders used by the unit and replay tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

import pytest

from orbquant.config import AppConfig, DEFAULT_CONFIG
from orbquant.contracts import AccountSnapshot, Bar, ForecastBatch, ForecastPoint

UTC = timezone.utc


@pytest.fixture(autouse=True)
def isolate_operator_credentials(tmp_path, monkeypatch):
    """Tests must opt into fake keys; never read the operator's private file."""
    monkeypatch.setattr("orbquant.credentials.default_credentials_path", lambda: tmp_path / "test-credentials.toml")
    for name in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "ORBQUANT_CREDENTIALS_FILE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def utc() -> timezone:
    return UTC


@pytest.fixture
def config() -> AppConfig:
    return DEFAULT_CONFIG


@pytest.fixture
def bar_factory() -> Callable[..., Bar]:
    def make_bar(
        *,
        symbol: str = "SPY",
        start: datetime | None = None,
        feed: str = "iex",
        price: Decimal = Decimal("100.00"),
        volume: int = 10000,
        revision: int = 0,
        available_lag_seconds: int = 5,
    ) -> Bar:
        bar_start = start or datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
        bar_end = bar_start + timedelta(minutes=1)
        return Bar(
            symbol=symbol,
            feed=feed,
            start=bar_start,
            end=bar_end,
            available_at=bar_end + timedelta(seconds=available_lag_seconds),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=volume,
            revision=revision,
        )

    return make_bar


@pytest.fixture
def account_factory() -> Callable[..., AccountSnapshot]:
    def make_account(
        *,
        as_of: datetime | None = None,
        cash: Decimal = Decimal("10000.00"),
        reserved: Decimal = Decimal("0.00"),
        equity: Decimal | None = None,
        halted: bool = False,
        halt_reasons: tuple[str, ...] = (),
    ) -> AccountSnapshot:
        mark = as_of or datetime(2024, 1, 2, 14, 31, 5, tzinfo=UTC)
        current_equity = equity if equity is not None else cash
        return AccountSnapshot(
            as_of=mark,
            settled_cash=cash,
            reserved_cash=reserved,
            unsettled_cash=Decimal("0.00"),
            positions=(),
            equity=current_equity,
            session_open_equity=current_equity,
            high_water_equity=current_equity,
            halted=halted,
            halt_reasons=halt_reasons,
            trading_date="2024-01-02",
        )

    return make_account


@pytest.fixture
def forecast_factory() -> Callable[..., ForecastBatch]:
    def make_forecast(
        *,
        symbol: str = "SPY",
        decision_at: datetime | None = None,
        horizons: tuple[int, ...] = tuple(range(15, 61)),
        mean_return: Decimal = Decimal("0.001"),
        q10: Decimal = Decimal("-0.002"),
        q50: Decimal = Decimal("0.0005"),
        q90: Decimal = Decimal("0.003"),
    ) -> ForecastBatch:
        return ForecastBatch(
            decision_at=decision_at or datetime(2024, 1, 2, 14, 31, 5, tzinfo=UTC),
            symbol=symbol,
            model_id="fixture-model",
            data_id="fixture-data",
            config_id=DEFAULT_CONFIG.config_id,
            points=tuple(
                ForecastPoint(minutes, mean_return, q10, q50, q90)
                for minutes in horizons
            ),
        )

    return make_forecast
