from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given, settings, strategies as st

from orbquant.broker import SimulatedMarketBroker
from orbquant.contracts import Order, OrderSide, OrderState
from orbquant.ledger import InsufficientCash, LedgerError, SQLiteLedger

UTC = timezone.utc
WHEN = datetime(2024, 1, 2, 14, 31, 5, tzinfo=UTC)


def _init(ledger):
    ledger.initialize_account(Decimal("10000"), original_gbp=Decimal("8000"),
                              usd_per_gbp=Decimal("1.251251"), conversion_fee_usd=Decimal("12.50"),
                              initialized_at=WHEN)


def _order(*, side=OrderSide.BUY, quantity=10, reference=None, state=OrderState.PROPOSED):
    return Order("order-1", "key-1", "SPY", side, quantity,
                 reference or WHEN + timedelta(minutes=1), state=state)


def test_cash_reservation_fill_and_sell_are_balanced_and_idempotent(bar_factory):
    ledger = SQLiteLedger()
    _init(ledger)
    approved = ledger.reserve_order(_order(), Decimal("1001"), occurred_at=WHEN)
    assert approved.state is OrderState.APPROVED
    assert ledger.snapshot(as_of=WHEN).reserved_cash == Decimal("1001.00")
    broker = SimulatedMarketBroker()
    submitted = broker.submit(approved, now=WHEN, previous_bar_volume=10000)
    ledger.set_order_state(submitted.order_id, OrderState.SUBMITTED, occurred_at=WHEN)
    reference_bar = bar_factory(start=WHEN + timedelta(minutes=1), price=Decimal("101"), available_lag_seconds=1)
    result = broker.on_bar(reference_bar, now=reference_bar.available_at)
    fill = result.fills[0]
    snapshot = ledger.apply_fill(fill)
    assert ledger.apply_fill(fill).equity == snapshot.equity
    assert snapshot.positions[0].quantity == 10
    assert ledger.fill_count() == 1
    assert ledger.journal_is_balanced()
    ledger.close()


def test_sell_fill_becomes_unsettled_until_next_business_session(bar_factory):
    ledger = SQLiteLedger()
    _init(ledger)
    buy = ledger.reserve_order(_order(), Decimal("1020"), occurred_at=WHEN)
    ledger.set_order_state(buy.order_id, OrderState.SUBMITTED, occurred_at=WHEN)
    buy_bar = bar_factory(start=WHEN + timedelta(minutes=1), price=Decimal("100"), available_lag_seconds=1)
    broker = SimulatedMarketBroker()
    broker.submit(buy, now=WHEN, previous_bar_volume=10000)
    ledger.set_order_state(buy.order_id, OrderState.SUBMITTED, occurred_at=WHEN)
    buy_fill = broker.on_bar(buy_bar, now=buy_bar.available_at).fills[0]
    ledger.apply_fill(buy_fill)
    sell = Order("sell-1", "sell-key", "SPY", OrderSide.SELL, 10,
                 buy_bar.start + timedelta(minutes=15))
    approved_sell = ledger.reserve_order(sell, Decimal("0"), occurred_at=buy_bar.available_at)
    ledger.set_order_state(sell.order_id, OrderState.SUBMITTED, occurred_at=buy_bar.available_at)
    sell_bar = bar_factory(start=sell.scheduled_reference_at, price=Decimal("102"), available_lag_seconds=1)
    sell_broker = SimulatedMarketBroker()
    sell_broker.submit(approved_sell, now=buy_bar.available_at)
    sell_fill = sell_broker.on_bar(sell_bar, now=sell_bar.available_at).fills[0]
    after = ledger.apply_fill(sell_fill)
    assert after.unsettled_cash > 0
    assert after.positions == ()
    assert ledger.journal_is_balanced()
    ledger.close()


def test_actual_buy_fill_over_available_cash_rolls_back_accounting():
    ledger = SQLiteLedger()
    _init(ledger)
    approved = ledger.reserve_order(_order(quantity=100), Decimal("9990"), occurred_at=WHEN)
    ledger.set_order_state(approved.order_id, OrderState.SUBMITTED, occurred_at=WHEN)
    from orbquant.contracts import Fill

    fill = Fill("fill-big", approved.order_id, "SPY", OrderSide.BUY,
                approved.scheduled_reference_at, approved.scheduled_reference_at + timedelta(minutes=1),
                100, Decimal("110"), Decimal("2.20"), Decimal("3.30"), Decimal("0"), "fill-event-big")
    with pytest.raises(InsufficientCash):
        ledger.apply_fill(fill)
    assert ledger.fill_count() == 0
    assert ledger.snapshot(as_of=WHEN).positions == ()
    assert ledger.journal_is_balanced()
    ledger.close()


def test_missed_entry_expires_and_missed_exit_moves_strictly_future(bar_factory):
    broker = SimulatedMarketBroker()
    entry = _order()
    broker.submit(entry, now=WHEN, previous_bar_volume=10000)
    missing_clock = entry.scheduled_reference_at + timedelta(minutes=1, seconds=5)
    result = broker.on_clock(missing_clock)
    assert result.order_updates[0].state is OrderState.EXPIRED

    exit_order = Order("sell-2", "sell-key-2", "QQQ", OrderSide.SELL, 1,
                       WHEN + timedelta(minutes=2), state=OrderState.APPROVED)
    broker.submit(exit_order, now=WHEN)
    stale = bar_factory(symbol="QQQ", start=exit_order.scheduled_reference_at,
                        available_lag_seconds=100)
    moved = broker.on_bar(stale, now=stale.available_at)
    assert moved.fills == ()
    assert moved.order_updates[0].scheduled_reference_at > stale.available_at
    assert moved.incidents[0].category == "missed_exit_reference"


@settings(max_examples=20, deadline=None)
@given(quantity=st.integers(min_value=1, max_value=50), price_cents=st.integers(min_value=100, max_value=5000))
def test_journal_balances_for_generated_full_buy_and_sell_lifecycle(quantity, price_cents):
    ledger = SQLiteLedger()
    _init(ledger)
    price = Decimal(price_cents) / Decimal("100")
    gross = price * quantity
    buy = Order("property-buy", "property-buy-key", "SPY", OrderSide.BUY, quantity,
                WHEN + timedelta(minutes=1))
    ledger.reserve_order(buy, gross, occurred_at=WHEN)
    from orbquant.contracts import Fill

    buy_fill = Fill("property-buy-fill", buy.order_id, "SPY", OrderSide.BUY,
                    buy.scheduled_reference_at, buy.scheduled_reference_at + timedelta(minutes=1),
                    quantity, price, Decimal("0"), Decimal("0"), Decimal("0"), "property-buy-event")
    ledger.apply_fill(buy_fill)
    sell = Order("property-sell", "property-sell-key", "SPY", OrderSide.SELL, quantity,
                 WHEN + timedelta(minutes=20))
    ledger.reserve_order(sell, Decimal("0"), occurred_at=WHEN + timedelta(minutes=2))
    sell_fill = Fill("property-sell-fill", sell.order_id, "SPY", OrderSide.SELL,
                     sell.scheduled_reference_at, sell.scheduled_reference_at + timedelta(minutes=1),
                     quantity, price, Decimal("0"), Decimal("0"), Decimal("0"), "property-sell-event")
    ledger.apply_fill(sell_fill)
    assert ledger.journal_is_balanced()
    assert ledger.snapshot(as_of=sell_fill.recognized_at).positions == ()
    ledger.close()
