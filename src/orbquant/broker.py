"""Deterministic simulated market-on-future-open broker."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Iterable

from orbquant.calendar import TradingCalendar
from orbquant.config import AppConfig, DEFAULT_CONFIG
from orbquant.contracts import Bar, Fill, Incident, IncidentSeverity, Order, OrderSide, OrderState
from orbquant.provenance import content_id

UTC = timezone.utc
BPS = Decimal("10000")


class BrokerReject(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BrokerResult:
    fills: tuple[Fill, ...] = ()
    order_updates: tuple[Order, ...] = ()
    incidents: tuple[Incident, ...] = ()


@dataclass(slots=True)
class SimulatedMarketBroker:
    config: AppConfig = DEFAULT_CONFIG
    calendar: TradingCalendar | None = None
    _orders: dict[str, Order] = field(default_factory=dict)
    _decision_volume: dict[str, int] = field(default_factory=dict)

    @property
    def execution_config(self):
        return self.config.execution

    def restore(self, orders: Iterable[Order]) -> None:
        for order in orders:
            if order.state in {OrderState.APPROVED, OrderState.SUBMITTED}:
                self._orders[order.order_id] = order

    def checkpoint_state(self) -> tuple[dict[str, Order], dict[str, int]]:
        """Snapshot mutable simulator state around an atomic engine event."""
        return dict(self._orders), dict(self._decision_volume)

    def restore_checkpoint(self, state: tuple[dict[str, Order], dict[str, int]]) -> None:
        self._orders, self._decision_volume = dict(state[0]), dict(state[1])

    def submit(self, order: Order, *, now: datetime, previous_bar_volume: int | None = None) -> Order:
        now_utc = _utc(now)
        if order.state not in {OrderState.PROPOSED, OrderState.APPROVED}:
            raise BrokerReject(f"cannot submit order in state {order.state.value}")
        if order.scheduled_reference_at <= now_utc:
            raise BrokerReject("order reference time must be in the future")
        if order.side is OrderSide.BUY:
            if previous_bar_volume is None or previous_bar_volume < 0:
                raise BrokerReject("entry order requires the previous completed bar volume")
            # Participation is a hard floor, never rounded up.
            cap = int(Decimal(previous_bar_volume) * self.config.execution.max_previous_bar_volume_fraction)
            if order.quantity > cap:
                raise BrokerReject(f"entry quantity {order.quantity} exceeds previous-bar participation cap {cap}")
            self._decision_volume[order.order_id] = previous_bar_volume
        if order.order_id in self._orders:
            existing = self._orders[order.order_id]
            if replace(existing, state=order.state, reason=order.reason) != order:
                raise BrokerReject("order ID reused for different order data")
            return existing
        submitted = replace(order, state=OrderState.SUBMITTED, reason="")
        self._orders[order.order_id] = submitted
        return submitted

    def cancel(self, order_id: str, *, now: datetime, reason: str = "cancelled by operator") -> Order:
        _utc(now)
        order = self._orders.get(order_id)
        if order is None:
            raise BrokerReject(f"unknown order {order_id}")
        if order.state not in {OrderState.SUBMITTED, OrderState.APPROVED}:
            raise BrokerReject(f"cannot cancel terminal order in state {order.state.value}")
        cancelled = replace(order, state=OrderState.CANCELLED, reason=reason)
        self._orders[order_id] = cancelled
        return cancelled

    def on_bar(self, bar: Bar, *, now: datetime) -> BrokerResult:
        now_utc = _utc(now)
        if now_utc < bar.available_at:
            raise ValueError("broker cannot recognize a completed bar before it is available")
        fills: list[Fill] = []
        updates: list[Order] = []
        incidents: list[Incident] = []
        current_orders = sorted(
            (order for order in self._orders.values() if order.symbol == bar.symbol and order.state is OrderState.SUBMITTED),
            key=lambda order: (order.scheduled_reference_at, order.order_id),
        )
        for order in current_orders:
            if bar.start < order.scheduled_reference_at:
                continue
            max_age = self.config.risk.max_bar_age_seconds
            if (now_utc - bar.end).total_seconds() > max_age:
                if order.side is OrderSide.BUY:
                    expired = replace(order, state=OrderState.EXPIRED, reason="reference bar arrived stale; no retrospective entry")
                    self._orders[order.order_id] = expired
                    updates.append(expired)
                else:
                    moved, incident = self._move_missed_exit(order, now_utc, "exit reference arrived stale")
                    self._orders[order.order_id] = moved
                    updates.append(moved)
                    incidents.append(incident)
                continue
            if bar.start > order.scheduled_reference_at:
                if order.side is OrderSide.BUY:
                    expired = replace(order, state=OrderState.EXPIRED, reason="entry reference bar was missed; no retrospective fill")
                    self._orders[order.order_id] = expired
                    updates.append(expired)
                else:
                    moved, incident = self._move_missed_exit(order, now_utc, "scheduled exit bar was missed")
                    self._orders[order.order_id] = moved
                    updates.append(moved)
                    incidents.append(incident)
                continue
            cost_bps = self.config.execution.spread_bps_per_side + self.config.execution.slippage_bps_per_side
            side_sign = Decimal("1") if order.side is OrderSide.BUY else Decimal("-1")
            price = (bar.open * (Decimal("1") + side_sign * cost_bps / BPS)).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_EVEN)
            spread_cost = (bar.open * order.quantity * self.config.execution.spread_bps_per_side / BPS).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
            slippage_cost = (bar.open * order.quantity * self.config.execution.slippage_bps_per_side / BPS).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
            fees = (self.config.execution.fee_per_share_usd * order.quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
            fill_id = content_id("fill", (order.order_id, bar.event_id))
            event_id = content_id("fill-event", (order.order_id, bar.event_id))
            fill = Fill(
                fill_id=fill_id, order_id=order.order_id, symbol=order.symbol, side=order.side,
                reference_at=bar.start, recognized_at=now_utc, quantity=order.quantity, price=price,
                spread_cost=spread_cost, slippage_cost=slippage_cost, fees=fees, event_id=event_id,
            )
            fills.append(fill)
            filled = replace(order, state=OrderState.FILLED, reason="")
            self._orders[order.order_id] = filled
            updates.append(filled)
        return BrokerResult(tuple(fills), tuple(updates), tuple(incidents))

    def on_clock(self, now: datetime) -> BrokerResult:
        """Expire missed entries and roll missed exits to a strictly future bar."""
        now_utc = _utc(now)
        updates: list[Order] = []
        incidents: list[Incident] = []
        for order in sorted(self._orders.values(), key=lambda item: (item.scheduled_reference_at, item.order_id)):
            if order.state is not OrderState.SUBMITTED or now_utc < order.scheduled_reference_at + timedelta(minutes=1, seconds=self.config.data.historical_availability_lag_seconds):
                continue
            if order.side is OrderSide.BUY:
                expired = replace(order, state=OrderState.EXPIRED, reason="entry reference was not received on time; no retrospective fill")
                self._orders[order.order_id] = expired
                updates.append(expired)
            else:
                moved, incident = self._move_missed_exit(order, now_utc, "exit reference was not received on time")
                self._orders[order.order_id] = moved
                updates.append(moved)
                incidents.append(incident)
        return BrokerResult(order_updates=tuple(updates), incidents=tuple(incidents))

    def _move_missed_exit(self, order: Order, recognized_at: datetime, reason: str) -> tuple[Order, Incident]:
        calendar = self.calendar or TradingCalendar(self.config.exchange_calendar, timezone_name=self.config.timezone)
        next_at = calendar.next_minute_start_after(recognized_at)
        if next_at is None:
            next_at = calendar.next_business_session(recognized_at.date()).open_at
        moved = replace(order, scheduled_reference_at=next_at, reason=f"{reason}; requeued for next future reference")
        incident = Incident(
            incident_id=content_id("incident", (order.order_id, recognized_at, reason)),
            occurred_at=recognized_at,
            category="missed_exit_reference",
            severity=IncidentSeverity.ERROR,
            message=f"{reason}; position remains open until a future valid market reference",
            symbol=order.symbol,
            event_id=content_id("missed-exit-event", (order.order_id, recognized_at)),
            details=(("next_reference_at", next_at.isoformat()),),
        )
        return moved, incident

    def update_order(self, order: Order) -> None:
        if order.order_id not in self._orders:
            self._orders[order.order_id] = order
        else:
            self._orders[order.order_id] = order

    def pending_orders(self) -> tuple[Order, ...]:
        return tuple(sorted((order for order in self._orders.values()
                             if order.state in {OrderState.APPROVED, OrderState.SUBMITTED}),
                            key=lambda order: (order.scheduled_reference_at, order.order_id)))

    def decision_volume(self, order_id: str) -> int | None:
        return self._decision_volume.get(order_id)

    def order(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def orders(self) -> tuple[Order, ...]:
        return tuple(sorted(self._orders.values(), key=lambda order: (order.scheduled_reference_at, order.order_id)))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("broker times must be timezone-aware")
    return value.astimezone(UTC)
