from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from orbquant.guard import Guard
from orbquant.policy import MeanAfterCostsPolicy
from orbquant.contracts import Candidate, PolicyDecision, Position

UTC = timezone.utc


def test_policy_selects_one_horizon_and_strictly_future_references(bar_factory, account_factory, forecast_factory):
    bar = bar_factory()
    forecast = forecast_factory(decision_at=bar.available_at, mean_return=Decimal("0.002"))
    decision = MeanAfterCostsPolicy().decide([forecast], account_factory(as_of=bar.available_at), latest_bars={"SPY": bar})
    assert len(decision.candidates) == 1
    candidate = decision.candidates[0]
    assert candidate.entry_reference_at == datetime(2024, 1, 2, 14, 32, tzinfo=UTC)
    assert candidate.exit_reference_at > candidate.entry_reference_at
    assert 15 <= candidate.horizon_minutes <= 60
    assert len([item for item in decision.candidates if item.symbol == "SPY"]) == 1


def test_guard_blocks_old_late_arriving_bars_and_pending_symbols(bar_factory, account_factory):
    now = datetime(2024, 1, 2, 16, 0, 5, tzinfo=UTC)
    late_old = bar_factory(start=now - timedelta(minutes=20), available_lag_seconds=5)
    candidate = Candidate("SPY", 15, "f", Decimal("10"), 5,
                          now + timedelta(minutes=1), now + timedelta(minutes=16),
                          Decimal("0.01"), Decimal("-0.001"))
    old_result = Guard().evaluate(PolicyDecision((candidate,)), account_factory(as_of=now), now=now,
                                  latest_bars={"SPY": late_old})
    assert not old_result.approved
    assert "stale" in old_result.rejected[0][1]
    fresh = bar_factory(start=now - timedelta(minutes=1, seconds=5), available_lag_seconds=5)
    pending_result = Guard().evaluate(PolicyDecision((candidate,)), account_factory(as_of=now), now=now,
                                      latest_bars={"SPY": fresh}, reserved_symbols=("SPY",))
    assert not pending_result.approved
    assert "reserved entry" in pending_result.rejected[0][1]


def test_guard_tracks_cash_and_gross_cumulatively(bar_factory, account_factory):
    now = datetime(2024, 1, 2, 14, 31, 5, tzinfo=UTC)
    bar_a = bar_factory(symbol="SPY")
    bar_b = bar_factory(symbol="QQQ")
    candidates = tuple(Candidate(symbol, 15, symbol, Decimal("0"), 9,
                                 now + timedelta(minutes=1), now + timedelta(minutes=16),
                                 Decimal("0.01"), Decimal("0")) for symbol in ("SPY", "QQQ"))
    # Each notional is below the 10% per-name limit, but together exceed the
    # $10,000 account's remaining settled cash once one is approved.
    result = Guard().evaluate(PolicyDecision(candidates), account_factory(cash=Decimal("1000"), equity=Decimal("10000")), now=now,
                              latest_bars={"SPY": bar_a, "QQQ": bar_b})
    assert len(result.approved) == 1
    assert len(result.rejected) == 1


def test_guard_halt_is_liquidation_and_persistent_drawdown_stays_latched(bar_factory, account_factory):
    now = datetime(2024, 1, 2, 14, 31, 5, tzinfo=UTC)
    guard = Guard()
    candidate = Candidate("SPY", 15, "f", Decimal("10"), 1,
                          now + timedelta(minutes=1), now + timedelta(minutes=16),
                          Decimal("0.01"), Decimal("0"))
    emergency = replace(guard, emergency_halt=True).evaluate(
        PolicyDecision((candidate,)), account_factory(), now=now, latest_bars={"SPY": bar_factory()})
    assert emergency.liquidation_requested
    drawdown_context = replace(account_factory(equity=Decimal("9499")),
                               session_open_equity=Decimal("10000"), high_water_equity=Decimal("10000"))
    guard.evaluate(PolicyDecision(()), drawdown_context, now=now, latest_bars={})
    rebound = replace(account_factory(equity=Decimal("10000")), high_water_equity=Decimal("10000"))
    latched = guard.evaluate(PolicyDecision((candidate,)), rebound, now=now, latest_bars={"SPY": bar_factory()})
    assert latched.liquidation_requested
    assert not latched.approved


def test_guard_reports_untrusted_model_types_and_malformed_horizon_rows():
    from orbquant.contracts import ForecastBatch

    malformed = object.__new__(ForecastBatch)
    object.__setattr__(malformed, "symbol", "SPY")
    object.__setattr__(malformed, "schema_version", "1.0")
    object.__setattr__(malformed, "model_id", "m")
    object.__setattr__(malformed, "data_id", "d")
    object.__setattr__(malformed, "config_id", "c")
    object.__setattr__(malformed, "model_version", "1")
    object.__setattr__(malformed, "feature_version", "intraday-120-v1")
    object.__setattr__(malformed, "decision_at", datetime(2024, 1, 2, 14, 31, 5, tzinfo=UTC))
    object.__setattr__(malformed, "points", (object(),))
    unhashable_symbol = object.__new__(ForecastBatch)
    for name in ("schema_version", "model_id", "data_id", "config_id", "model_version", "feature_version", "decision_at", "points"):
        object.__setattr__(unhashable_symbol, name, getattr(malformed, name))
    object.__setattr__(unhashable_symbol, "symbol", ["SPY"])
    issues = Guard().validate_forecasts([None, {}, malformed, unhashable_symbol])
    assert any("expected ForecastBatch" in issue for issue in issues)
    assert any("horizons are missing" in issue for issue in issues)
    assert any("symbol is malformed" in issue for issue in issues)
