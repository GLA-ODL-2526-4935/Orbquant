"""Transactional SQLite cash, reservation, position and double-entry ledger."""

from __future__ import annotations

import json
import dataclasses
import math
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from enum import Enum
from pathlib import Path
from typing import Iterator, Mapping
import zlib

from orbquant.calendar import TradingCalendar
from orbquant.contracts import AccountSnapshot, Bar, FeatureWindow, Fill, Order, OrderSide, OrderState, Position
from orbquant.provenance import canonical_json, content_hash

UTC = timezone.utc


def _safe_audit_value(value: object) -> object:
    """Convert even malformed model output to deterministic, bounded JSON data."""
    if isinstance(value, FeatureWindow):
        last_bar = value.last_completed_bar
        width = len(value.feature_names)
        return {
            "symbol": value.symbol,
            "decision_at": _safe_audit_value(value.decision_at),
            "last_completed_event_id": last_bar.event_id,
            "last_completed_bar_start": _safe_audit_value(last_bar.start),
            "last_completed_bar_end": _safe_audit_value(last_bar.end),
            "last_completed_bar_available_at": _safe_audit_value(last_bar.available_at),
            "feature_version": value.feature_version,
            "feature_names": _safe_audit_value(value.feature_names),
            "feature_value_count": len(value.values),
            "source_observation_count": len(value.values) // width if width else None,
            "feature_values_hash": content_hash(tuple(value.values)),
            "valid": value.valid,
            "invalid_reason": value.invalid_reason,
        }
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        result = {}
        for field in dataclasses.fields(value):
            try:
                result[field.name] = _safe_audit_value(getattr(value, field.name))
            except Exception:
                result[field.name] = {"unreadable_field": field.name}
        return result
    if isinstance(value, Enum):
        return _safe_audit_value(value.value)
    if isinstance(value, Decimal):
        return format(value, "f") if value.is_finite() else {"invalid_decimal": str(value)}
    if isinstance(value, datetime):
        return value.isoformat(timespec="microseconds") if value.tzinfo is not None else {"naive_datetime": value.isoformat()}
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return value if math.isfinite(value) else {"nonfinite_float": repr(value)}
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    if isinstance(value, Mapping):
        return {str(key): _safe_audit_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_safe_audit_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_safe_audit_value(item) for item in value]
        return sorted(normalized, key=lambda item: canonical_json(item))
    scalar = getattr(value, "item", None)
    if callable(scalar):
        try:
            return _safe_audit_value(scalar())
        except Exception:
            pass
    type_name = f"{type(value).__module__}.{type(value).__qualname__}"
    return {"unserializable_type": type_name}


class LedgerError(RuntimeError):
    """Raised when a ledger operation would violate an accounting invariant."""


class InsufficientCash(LedgerError):
    pass


class SQLiteLedger:
    """Single-writer SQLite ledger with atomic event, cash and position changes."""

    def __init__(self, path: str | Path = ":memory:", *, calendar: TradingCalendar | None = None, currency_minor_units: int = 2) -> None:
        self.path = str(path)
        self.currency_minor_units = currency_minor_units
        if currency_minor_units < 0:
            raise ValueError("currency_minor_units must be nonnegative")
        self.calendar = calendar or TradingCalendar()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._transaction_state = threading.local()
        self._connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=5000")
        if self.path != ":memory:":
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _create_schema(self) -> None:
        self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS ledger_transactions(transaction_id TEXT PRIMARY KEY,kind TEXT NOT NULL,occurred_at TEXT NOT NULL,event_id TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS journal_postings(posting_id INTEGER PRIMARY KEY AUTOINCREMENT,transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),account TEXT NOT NULL,amount TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS journal_account_idx ON journal_postings(account);
            CREATE TABLE IF NOT EXISTS account_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS positions(symbol TEXT PRIMARY KEY,quantity INTEGER NOT NULL,average_cost TEXT NOT NULL,last_price TEXT NOT NULL,opened_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders(order_id TEXT PRIMARY KEY,idempotency_key TEXT NOT NULL UNIQUE,symbol TEXT NOT NULL,side TEXT NOT NULL,quantity INTEGER NOT NULL,scheduled_reference_at TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,payload TEXT NOT NULL,reserve_amount TEXT NOT NULL DEFAULT '0');
            CREATE INDEX IF NOT EXISTS orders_state_idx ON orders(state);
            CREATE TABLE IF NOT EXISTS fills(fill_id TEXT PRIMARY KEY,order_id TEXT NOT NULL REFERENCES orders(order_id),event_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settlements(settlement_id TEXT PRIMARY KEY,due_date TEXT NOT NULL,amount TEXT NOT NULL,source_fill_id TEXT NOT NULL UNIQUE REFERENCES fills(fill_id));
            CREATE INDEX IF NOT EXISTS settlements_due_idx ON settlements(due_date);
            CREATE TABLE IF NOT EXISTS market_marks(symbol TEXT PRIMARY KEY,price TEXT NOT NULL,as_of TEXT NOT NULL,event_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(trading_date TEXT PRIMARY KEY,opening_equity TEXT NOT NULL,high_water_equity TEXT NOT NULL,halted INTEGER NOT NULL DEFAULT 0,halt_reason TEXT NOT NULL DEFAULT '',reconciled INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS processed_events(event_id TEXT PRIMARY KEY,event_type TEXT NOT NULL,occurred_at TEXT NOT NULL,payload_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS event_receipts(
                event_id TEXT NOT NULL REFERENCES processed_events(event_id),
                received_at TEXT NOT NULL, delivery_hash TEXT NOT NULL,
                PRIMARY KEY(event_id,received_at,delivery_hash)
            );
            CREATE TABLE IF NOT EXISTS decision_audit(event_id TEXT PRIMARY KEY REFERENCES processed_events(event_id),payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS incidents(incident_id TEXT PRIMARY KEY,event_id TEXT,payload TEXT NOT NULL);
        """)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._connection
            depth = getattr(self._transaction_state, "depth", 0)
            savepoint = f"ledger_nested_{depth}"
            if depth == 0:
                connection.execute("BEGIN IMMEDIATE")
            else:
                connection.execute(f"SAVEPOINT {savepoint}")
            self._transaction_state.depth = depth + 1
            try:
                yield connection
                if depth == 0:
                    connection.execute("COMMIT")
                else:
                    connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            except BaseException:
                if connection.in_transaction:
                    if depth == 0:
                        connection.execute("ROLLBACK")
                    else:
                        connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            finally:
                self._transaction_state.depth = depth

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """Group multiple ledger operations into one durable transaction."""
        with self._transaction():
            yield

    @staticmethod
    def _utc_text(value: datetime) -> str:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ledger timestamps must be timezone-aware")
        return value.astimezone(UTC).isoformat(timespec="microseconds")

    def _balance(self, connection: sqlite3.Connection, account: str) -> Decimal:
        rows = connection.execute("SELECT amount FROM journal_postings WHERE account=?", (account,)).fetchall()
        return sum((Decimal(row["amount"]) for row in rows), Decimal("0"))

    @staticmethod
    def _journal(connection: sqlite3.Connection, transaction_id: str, kind: str, occurred_at: datetime, postings: Mapping[str, Decimal], *, event_id: str | None = None) -> None:
        nonzero = {account: amount for account, amount in postings.items() if amount != 0}
        if sum(nonzero.values(), Decimal("0")) != 0:
            raise LedgerError(f"unbalanced journal transaction {transaction_id}")
        connection.execute("INSERT INTO ledger_transactions(transaction_id,kind,occurred_at,event_id) VALUES(?,?,?,?)",
                           (transaction_id, kind, SQLiteLedger._utc_text(occurred_at), event_id))
        connection.executemany("INSERT INTO journal_postings(transaction_id,account,amount) VALUES(?,?,?)",
                               ((transaction_id, account, format(amount, "f")) for account, amount in nonzero.items()))

    def _round_money(self, amount: Decimal) -> Decimal:
        quantum = Decimal("1").scaleb(-self.currency_minor_units)
        return amount.quantize(quantum, rounding=ROUND_HALF_EVEN)

    def initialize_account(self, net_usd: Decimal, *, original_gbp: Decimal, usd_per_gbp: Decimal, conversion_fee_usd: Decimal, initialized_at: datetime) -> None:
        values = (net_usd, original_gbp, usd_per_gbp, conversion_fee_usd)
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in values) or net_usd <= 0 or original_gbp <= 0 or usd_per_gbp <= 0 or conversion_fee_usd < 0:
            raise ValueError("initial capital and FX values must be valid positive Decimals")
        self._utc_text(initialized_at)
        net = self._round_money(net_usd)
        fee = self._round_money(conversion_fee_usd)
        gross = net + fee
        with self._transaction() as connection:
            if connection.execute("SELECT 1 FROM account_meta WHERE key='initialized'").fetchone():
                raise LedgerError("account has already been initialized; initial conversion is immutable")
            self._journal(connection, "account-opening", "account_opening", initialized_at, {
                "cash.available": net,
                "capital.contributed": -gross,
                "expense.fx_conversion": fee,
            })
            metadata = {"initialized": "true", "original_gbp": format(original_gbp, "f"),
                        "usd_per_gbp": format(usd_per_gbp, "f"), "conversion_fee_usd": format(fee, "f"),
                        "initial_usd": format(net, "f")}
            connection.executemany("INSERT INTO account_meta(key,value) VALUES(?,?)", metadata.items())

    def initialized(self) -> bool:
        with self._lock:
            return self._connection.execute("SELECT 1 FROM account_meta WHERE key='initialized'").fetchone() is not None

    def account_metadata(self) -> dict[str, str]:
        with self._lock:
            return {row["key"]: row["value"] for row in self._connection.execute("SELECT key,value FROM account_meta")}

    def reserve_order(self, order: Order, reserve_amount: Decimal, *, occurred_at: datetime) -> Order:
        """Atomically reserve settled cash and insert one full-fill V1 order."""
        self._utc_text(occurred_at)
        if not reserve_amount.is_finite() or reserve_amount < 0:
            raise ValueError("reservation must be a finite nonnegative Decimal")
        reserve = self._round_money(reserve_amount)
        with self._transaction() as connection:
            existing = connection.execute("SELECT * FROM orders WHERE idempotency_key=?", (order.idempotency_key,)).fetchone()
            if existing:
                stored = _order_from_row(existing)
                if existing["order_id"] != order.order_id or _replace_order_state(stored, order.state, order.reason) != order:
                    raise LedgerError("idempotency key reused for a different order")
                return _order_from_row(existing)
            if not self.initialized():
                raise LedgerError("account must be initialized before orders can be reserved")
            if order.side is OrderSide.BUY:
                available = self._balance(connection, "cash.available")
                if reserve > available:
                    raise InsufficientCash(f"requested reserve {reserve} exceeds available settled cash {available}")
                if reserve:
                    self._journal(connection, f"reserve:{order.order_id}", "cash_reservation", occurred_at, {
                        "cash.reserved": reserve, "cash.available": -reserve,
                    })
            else:
                position = connection.execute("SELECT quantity FROM positions WHERE symbol=?", (order.symbol,)).fetchone()
                if not position or order.quantity > position["quantity"]:
                    raise LedgerError("sell reservation exceeds the current long position")
            approved = _replace_order_state(order, OrderState.APPROVED, "")
            connection.execute(
                "INSERT INTO orders(order_id,idempotency_key,symbol,side,quantity,scheduled_reference_at,state,reason,payload,reserve_amount) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (order.order_id, order.idempotency_key, order.symbol, order.side.value, order.quantity,
                 self._utc_text(order.scheduled_reference_at), approved.state.value, approved.reason,
                 canonical_json(approved), format(reserve, "f")),
            )
            self._record_processed_event(connection, f"order:{order.order_id}", "order_approved", occurred_at, approved)
            return approved

    def set_order_state(self, order_id: str, state: OrderState, *, occurred_at: datetime, reason: str = "") -> Order:
        self._utc_text(occurred_at)
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
            if row is None:
                raise LedgerError(f"unknown order {order_id}")
            current = OrderState(row["state"])
            terminal = {OrderState.FILLED, OrderState.REJECTED, OrderState.CANCELLED, OrderState.EXPIRED}
            if current in terminal and current != state:
                raise LedgerError(f"terminal order state {current.value} cannot transition to {state.value}")
            if current == state:
                return _order_from_row(row)
            allowed = {
                OrderState.APPROVED: {OrderState.SUBMITTED, OrderState.REJECTED, OrderState.CANCELLED, OrderState.EXPIRED},
                OrderState.SUBMITTED: {OrderState.FILLED, OrderState.REJECTED, OrderState.CANCELLED, OrderState.EXPIRED},
            }
            if state not in allowed.get(current, set()):
                raise LedgerError(f"invalid order transition {current.value} -> {state.value}")
            reserve = Decimal(row["reserve_amount"])
            if state in {OrderState.REJECTED, OrderState.CANCELLED, OrderState.EXPIRED} and reserve > 0:
                self._journal(connection, f"release:{order_id}:{state.value}", "cash_release", occurred_at, {
                    "cash.reserved": -reserve, "cash.available": reserve,
                })
                reserve = Decimal("0")
            payload = json.loads(row["payload"])
            payload["state"] = state.value
            payload["reason"] = reason
            connection.execute("UPDATE orders SET state=?,reason=?,payload=?,reserve_amount=? WHERE order_id=?",
                               (state.value, reason, canonical_json(payload), format(reserve, "f"), order_id))
            return _order_from_row(connection.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone())

    def apply_fill(self, fill: Fill) -> AccountSnapshot:
        """Post a full fill and its position/cash changes in one transaction."""
        with self._transaction() as connection:
            prior = connection.execute("SELECT payload FROM fills WHERE fill_id=? OR event_id=?", (fill.fill_id, fill.event_id)).fetchone()
            if prior:
                if prior["payload"] != canonical_json(fill):
                    raise LedgerError("fill or event ID was reused with different accounting data")
                return self._snapshot(connection, fill.recognized_at)
            order_row = connection.execute("SELECT * FROM orders WHERE order_id=?", (fill.order_id,)).fetchone()
            if order_row is None:
                raise LedgerError(f"fill references unknown order {fill.order_id}")
            order = _order_from_row(order_row)
            if order.state not in {OrderState.APPROVED, OrderState.SUBMITTED}:
                raise LedgerError(f"order state {order.state.value} cannot be filled")
            if (fill.symbol, fill.side, fill.quantity) != (order.symbol, order.side, order.quantity):
                raise LedgerError("fill does not exactly match the approved V1 full-fill order")
            gross = self._round_money(fill.price * fill.quantity)
            fee = self._round_money(fill.fees)
            reserve = Decimal(order_row["reserve_amount"])
            pending_settlement: tuple[str, Decimal] | None = None
            if fill.side is OrderSide.BUY:
                available = self._balance(connection, "cash.available")
                if gross + fee > available + reserve:
                    raise InsufficientCash("actual simulated fill price exceeds settled unreserved and reserved cash")
                if reserve:
                    self._journal(connection, f"consume-reserve:{order.order_id}", "cash_release", fill.recognized_at, {
                        "cash.reserved": -reserve, "cash.available": reserve,
                    })
                self._journal(connection, f"fill:{fill.fill_id}", "buy_fill", fill.recognized_at, {
                    "inventory": gross + fee, "cash.available": -(gross + fee),
                }, event_id=fill.event_id)
                prior_position = connection.execute("SELECT * FROM positions WHERE symbol=?", (fill.symbol,)).fetchone()
                if prior_position:
                    old_qty = int(prior_position["quantity"])
                    new_qty = old_qty + fill.quantity
                    basis = Decimal(prior_position["average_cost"]) * old_qty + gross + fee
                    average_cost = basis / new_qty
                    opened_at = prior_position["opened_at"]
                else:
                    new_qty = fill.quantity
                    average_cost = (gross + fee) / new_qty
                    opened_at = self._utc_text(fill.recognized_at)
                connection.execute(
                    "INSERT INTO positions(symbol,quantity,average_cost,last_price,opened_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(symbol) DO UPDATE SET quantity=excluded.quantity,average_cost=excluded.average_cost,last_price=excluded.last_price",
                    (fill.symbol, new_qty, format(average_cost, "f"), format(fill.price, "f"), opened_at),
                )
            else:
                position = connection.execute("SELECT * FROM positions WHERE symbol=?", (fill.symbol,)).fetchone()
                if position is None or fill.quantity > int(position["quantity"]):
                    raise LedgerError("sell fill exceeds the current long position")
                old_qty = int(position["quantity"])
                basis = self._round_money(Decimal(position["average_cost"]) * fill.quantity)
                net_proceeds = gross - fee
                self._journal(connection, f"fill:{fill.fill_id}", "sell_fill", fill.recognized_at, {
                    "cash.unsettled": net_proceeds,
                    "inventory": -basis,
                    "realized_pnl": -(gross - basis),
                    "expense.execution_fees": fee,
                }, event_id=fill.event_id)
                if fill.quantity == old_qty:
                    connection.execute("DELETE FROM positions WHERE symbol=?", (fill.symbol,))
                else:
                    connection.execute("UPDATE positions SET quantity=?,last_price=? WHERE symbol=?",
                                       (old_qty - fill.quantity, format(fill.price, "f"), fill.symbol))
                due = self.calendar.next_business_session(fill.recognized_at.date(), offset=1).trading_date.isoformat()
                pending_settlement = (due, net_proceeds)
            connection.execute("INSERT INTO fills(fill_id,order_id,event_id,payload) VALUES(?,?,?,?)",
                               (fill.fill_id, fill.order_id, fill.event_id, canonical_json(fill)))
            if pending_settlement is not None:
                due, amount = pending_settlement
                connection.execute("INSERT INTO settlements(settlement_id,due_date,amount,source_fill_id) VALUES(?,?,?,?)",
                                   (f"settle:{fill.fill_id}", due, format(amount, "f"), fill.fill_id))
            payload = json.loads(order_row["payload"])
            payload["state"] = OrderState.FILLED.value
            payload["reason"] = ""
            connection.execute("UPDATE orders SET state=?,reason='',payload=?,reserve_amount='0' WHERE order_id=?",
                               (OrderState.FILLED.value, canonical_json(payload), fill.order_id))
            self._record_processed_event(connection, fill.event_id, "fill", fill.recognized_at, fill)
            return self._snapshot(connection, fill.recognized_at)

    def settle_due(self, trading_date: date, *, occurred_at: datetime) -> Decimal:
        self._utc_text(occurred_at)
        with self._transaction() as connection:
            rows = connection.execute("SELECT * FROM settlements WHERE due_date<=? ORDER BY due_date,settlement_id",
                                      (trading_date.isoformat(),)).fetchall()
            amount = sum((Decimal(row["amount"]) for row in rows), Decimal("0"))
            if amount:
                settlement_id = f"settlement:{trading_date.isoformat()}:{content_hash(tuple(row['settlement_id'] for row in rows))[:12]}"
                self._journal(connection, settlement_id, "cash_settlement", occurred_at, {
                    "cash.available": amount, "cash.unsettled": -amount,
                })
                connection.executemany("DELETE FROM settlements WHERE settlement_id=?",
                                       ((row["settlement_id"],) for row in rows))
            return amount

    def mark_bar(self, bar: Bar) -> bool:
        """Record the latest mark once; old or duplicate bars cannot rewrite it."""
        with self._transaction() as connection:
            existing = connection.execute("SELECT event_id,as_of FROM market_marks WHERE symbol=?", (bar.symbol,)).fetchone()
            if existing and existing["event_id"] == bar.event_id:
                return False
            if existing and self._utc_text(bar.available_at) <= existing["as_of"]:
                return False
            connection.execute(
                "INSERT INTO market_marks(symbol,price,as_of,event_id) VALUES(?,?,?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET price=excluded.price,as_of=excluded.as_of,event_id=excluded.event_id",
                (bar.symbol, format(bar.close, "f"), self._utc_text(bar.available_at), bar.event_id),
            )
            self._ensure_session(connection, bar.available_at.date())
            return True

    def record_event(
        self,
        event_id: str,
        event_type: str,
        occurred_at: datetime,
        payload: object,
        *,
        decision: object | None = None,
    ) -> bool:
        with self._transaction() as connection:
            inserted = self._record_processed_event(connection, event_id, event_type, occurred_at, payload)
            if decision is not None:
                serialized = zlib.compress(canonical_json(_safe_audit_value(decision)).encode("utf-8"), level=6)
                existing = connection.execute("SELECT payload FROM decision_audit WHERE event_id=?", (event_id,)).fetchone()
                if existing is not None and bytes(existing["payload"]) != serialized:
                    raise LedgerError("event decision audit was rewritten with different content")
                connection.execute(
                    "INSERT OR IGNORE INTO decision_audit(event_id,payload) VALUES(?,?)",
                    (event_id, serialized),
                )
            return inserted

    def decision_audit(self) -> tuple[dict[str, object], ...]:
        """Return the durable forecast/policy/risk result for every bar event."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT p.event_id,p.event_type,p.occurred_at,p.payload_hash,d.payload "
                "FROM decision_audit AS d JOIN processed_events AS p USING(event_id) "
                "ORDER BY p.occurred_at,p.event_id"
            ).fetchall()
        return tuple({
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "occurred_at": row["occurred_at"],
            "payload_hash": row["payload_hash"],
            "decision": json.loads(zlib.decompress(row["payload"])),
        } for row in rows)

    def is_duplicate_event(self, event_id: str, payload: object) -> bool:
        digest = _event_payload_hash(payload)
        with self._transaction() as connection:
            existing = connection.execute("SELECT payload_hash FROM processed_events WHERE event_id=?", (event_id,)).fetchone()
            if existing is None:
                return False
            if existing["payload_hash"] != digest:
                raise LedgerError("event ID reused with different market content")
            if isinstance(payload, Bar):
                connection.execute(
                    "INSERT OR IGNORE INTO event_receipts(event_id,received_at,delivery_hash) VALUES(?,?,?)",
                    (event_id, self._utc_text(payload.available_at), content_hash(payload)),
                )
            return True

    def record_incident(self, incident: object) -> bool:
        incident_id = getattr(incident, "incident_id", None)
        if not incident_id:
            raise ValueError("incident must have an incident_id")
        event_id = getattr(incident, "event_id", None)
        payload = canonical_json(incident)
        with self._transaction() as connection:
            existing = connection.execute("SELECT payload FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
            if existing:
                if existing["payload"] != payload:
                    raise LedgerError("incident ID reused with different content")
                return False
            connection.execute("INSERT INTO incidents(incident_id,event_id,payload) VALUES(?,?,?)", (incident_id, event_id, payload))
            return True

    def update_order(self, order: Order) -> Order:
        """Persist a broker-side reference reschedule while preserving cash reserve."""
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM orders WHERE order_id=?", (order.order_id,)).fetchone()
            if row is None:
                raise LedgerError(f"unknown order {order.order_id}")
            current = _order_from_row(row)
            if current.side != order.side or current.quantity != order.quantity or current.symbol != order.symbol:
                raise LedgerError("order update cannot change its approved side, size, or symbol")
            connection.execute(
                "UPDATE orders SET scheduled_reference_at=?,state=?,reason=?,payload=? WHERE order_id=?",
                (self._utc_text(order.scheduled_reference_at), order.state.value, order.reason, canonical_json(order), order.order_id),
            )
            return _order_from_row(connection.execute("SELECT * FROM orders WHERE order_id=?", (order.order_id,)).fetchone())

    def all_orders(self) -> tuple[Order, ...]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM orders ORDER BY scheduled_reference_at,order_id").fetchall()
            return tuple(_order_from_row(row) for row in rows)

    def active_exit_symbols(self) -> frozenset[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT DISTINCT symbol FROM orders WHERE side='sell' AND state IN ('approved','submitted')"
            ).fetchall()
            return frozenset(row["symbol"] for row in rows)

    def _record_processed_event(self, connection: sqlite3.Connection, event_id: str, event_type: str, occurred_at: datetime, payload: object) -> bool:
        digest = _event_payload_hash(payload) if event_type == "bar" else content_hash(payload)
        existing = connection.execute("SELECT payload_hash FROM processed_events WHERE event_id=?", (event_id,)).fetchone()
        if existing:
            if existing["payload_hash"] != digest:
                raise LedgerError("event ID reused with different market content")
            if isinstance(payload, Bar):
                connection.execute(
                    "INSERT OR IGNORE INTO event_receipts(event_id,received_at,delivery_hash) VALUES(?,?,?)",
                    (event_id, self._utc_text(payload.available_at), content_hash(payload)),
                )
            return False
        connection.execute("INSERT INTO processed_events(event_id,event_type,occurred_at,payload_hash) VALUES(?,?,?,?)",
                           (event_id, event_type, self._utc_text(occurred_at), digest))
        if isinstance(payload, Bar):
            connection.execute(
                "INSERT OR IGNORE INTO event_receipts(event_id,received_at,delivery_hash) VALUES(?,?,?)",
                (event_id, self._utc_text(payload.available_at), content_hash(payload)),
            )
        return True

    def has_event(self, event_id: str) -> bool:
        with self._lock:
            return self._connection.execute("SELECT 1 FROM processed_events WHERE event_id=?", (event_id,)).fetchone() is not None

    def set_halt(self, reason: str, *, occurred_at: datetime, persistent: bool = False) -> None:
        if not reason:
            raise ValueError("halt reason cannot be empty")
        trading_date = occurred_at.astimezone(UTC).date()
        with self._transaction() as connection:
            self._ensure_session(connection, trading_date)
            row = connection.execute("SELECT halt_reason FROM sessions WHERE trading_date=?", (trading_date.isoformat(),)).fetchone()
            reasons = list(dict.fromkeys((*filter(None, row["halt_reason"].split("; ")), reason)))
            combined = "; ".join(reasons)
            connection.execute("UPDATE sessions SET halted=1,halt_reason=? WHERE trading_date=?", (combined, trading_date.isoformat()))
            if persistent:
                connection.execute("INSERT INTO account_meta(key,value) VALUES('manual_halt','true') ON CONFLICT(key) DO UPDATE SET value='true'")

    def clear_session_halt(self, trading_date: date, *, occurred_at: datetime, reconciled: bool) -> None:
        if not reconciled:
            raise LedgerError("session halt reset requires successful reconciliation")
        with self._transaction() as connection:
            if connection.execute("SELECT 1 FROM account_meta WHERE key='manual_halt'").fetchone():
                raise LedgerError("persistent manual halt requires explicit operator reset")
            self._ensure_session(connection, trading_date)
            connection.execute("UPDATE sessions SET halted=0,halt_reason='',reconciled=1 WHERE trading_date=?", (trading_date.isoformat(),))

    def reset_persistent_halt(self, *, occurred_at: datetime, between_sessions: bool, reconciled: bool) -> None:
        self._utc_text(occurred_at)
        if not between_sessions or not reconciled:
            raise LedgerError("persistent halt reset requires between-session reconciliation")
        with self._transaction() as connection:
            connection.execute("DELETE FROM account_meta WHERE key='manual_halt'")
            # Session loss halts belong to their trading date and clear only
            # when the next XNYS session opens. Resetting the persistent
            # operator/drawdown latch must not erase a same-day loss halt.

    def is_halted(self, as_of: datetime) -> tuple[bool, tuple[str, ...]]:
        trading_date = as_of.astimezone(UTC).date().isoformat()
        with self._lock:
            manual = self._connection.execute("SELECT value FROM account_meta WHERE key='manual_halt'").fetchone()
            session = self._connection.execute("SELECT halted,halt_reason FROM sessions WHERE trading_date=?", (trading_date,)).fetchone()
        reasons: list[str] = []
        if manual:
            reasons.append("persistent manual halt")
        if session and session["halted"]:
            reasons.extend(filter(None, session["halt_reason"].split("; ")))
        return bool(manual or (session and session["halted"])), tuple(dict.fromkeys(reasons))

    def _ensure_session(self, connection: sqlite3.Connection, trading_date: date) -> None:
        label = trading_date.isoformat()
        if connection.execute("SELECT 1 FROM sessions WHERE trading_date=?", (label,)).fetchone():
            return
        equity = self._equity(connection)
        previous = connection.execute("SELECT high_water_equity FROM sessions ORDER BY trading_date DESC LIMIT 1").fetchone()
        high_water = max(equity, Decimal(previous["high_water_equity"]) if previous else equity)
        connection.execute("INSERT INTO sessions(trading_date,opening_equity,high_water_equity,reconciled) VALUES(?,?,?,0)",
                           (label, format(equity, "f"), format(high_water, "f")))

    def _equity(self, connection: sqlite3.Connection) -> Decimal:
        cash = (self._balance(connection, "cash.available") +
                self._balance(connection, "cash.reserved") +
                self._balance(connection, "cash.unsettled"))
        market = Decimal("0")
        rows = connection.execute("SELECT symbol,quantity,last_price FROM positions").fetchall()
        for row in rows:
            mark = connection.execute("SELECT price FROM market_marks WHERE symbol=?", (row["symbol"],)).fetchone()
            price = Decimal(mark["price"]) if mark else Decimal(row["last_price"])
            market += price * int(row["quantity"])
        return cash + market

    def _snapshot(self, connection: sqlite3.Connection, as_of: datetime) -> AccountSnapshot:
        available = self._balance(connection, "cash.available")
        reserved = self._balance(connection, "cash.reserved")
        unsettled = self._balance(connection, "cash.unsettled")
        positions: list[Position] = []
        rows = connection.execute("SELECT * FROM positions ORDER BY symbol").fetchall()
        for row in rows:
            mark = connection.execute("SELECT price FROM market_marks WHERE symbol=?", (row["symbol"],)).fetchone()
            price = Decimal(mark["price"]) if mark else Decimal(row["last_price"])
            quantity = int(row["quantity"])
            cost = Decimal(row["average_cost"])
            positions.append(Position(
                symbol=row["symbol"], quantity=quantity, average_cost=cost,
                market_value=price * quantity, unrealized_pnl=(price - cost) * quantity,
                opened_at=datetime.fromisoformat(row["opened_at"]),
            ))
        equity = available + reserved + unsettled + sum((position.market_value for position in positions), Decimal("0"))
        trading_date = as_of.astimezone(UTC).date()
        self._ensure_session(connection, trading_date)
        label = trading_date.isoformat()
        session = connection.execute("SELECT * FROM sessions WHERE trading_date=?", (label,)).fetchone()
        high_water = max(Decimal(session["high_water_equity"]), equity)
        if high_water != Decimal(session["high_water_equity"]):
            connection.execute("UPDATE sessions SET high_water_equity=? WHERE trading_date=?", (format(high_water, "f"), label))
        halted = bool(session["halted"])
        reasons = tuple(filter(None, session["halt_reason"].split("; ")))
        manual = connection.execute("SELECT 1 FROM account_meta WHERE key='manual_halt'").fetchone()
        if manual:
            halted = True
            reasons = (*reasons, "persistent manual halt")
        return AccountSnapshot(
            as_of=as_of.astimezone(UTC),
            settled_cash=available + reserved,
            reserved_cash=reserved,
            unsettled_cash=unsettled,
            positions=tuple(positions),
            equity=equity,
            session_open_equity=Decimal(session["opening_equity"]),
            high_water_equity=high_water,
            halted=halted,
            halt_reasons=tuple(dict.fromkeys(reasons)),
            trading_date=label,
        )

    def snapshot(self, *, as_of: datetime) -> AccountSnapshot:
        self._utc_text(as_of)
        with self._transaction() as connection:
            return self._snapshot(connection, as_of)

    def open_orders(self) -> tuple[Order, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM orders WHERE state IN ('proposed','approved','submitted') ORDER BY scheduled_reference_at,order_id"
            ).fetchall()
            return tuple(_order_from_row(row) for row in rows)

    def order_reservations(self) -> tuple[tuple[str, str, Decimal], ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT order_id,symbol,reserve_amount FROM orders WHERE state IN ('approved','submitted') ORDER BY order_id"
            ).fetchall()
            return tuple((row["order_id"], row["symbol"], Decimal(row["reserve_amount"]))
                         for row in rows if Decimal(row["reserve_amount"]) > 0)

    def fill_count(self) -> int:
        with self._lock:
            return int(self._connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0])

    def journal_is_balanced(self) -> bool:
        with self._lock:
            rows = self._connection.execute("SELECT transaction_id,amount FROM journal_postings ORDER BY transaction_id").fetchall()
        totals: dict[str, Decimal] = {}
        for row in rows:
            totals[row["transaction_id"]] = totals.get(row["transaction_id"], Decimal("0")) + Decimal(row["amount"])
        return all(total == 0 for total in totals.values())

    def event_count(self) -> int:
        with self._lock:
            return int(self._connection.execute("SELECT COUNT(*) FROM processed_events").fetchone()[0])

    def event_receipts(self, event_id: str) -> tuple[datetime, ...]:
        """Return all distinct observed delivery times for one raw event."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT received_at FROM event_receipts WHERE event_id=? ORDER BY received_at",
                (event_id,),
            ).fetchall()
        return tuple(datetime.fromisoformat(row["received_at"]) for row in rows)


def _event_payload_hash(payload: object) -> str:
    if isinstance(payload, Bar):
        return content_hash(("market-bar", payload.market_identity))
    return content_hash(payload)


def _replace_order_state(order: Order, state: OrderState, reason: str) -> Order:
    from dataclasses import replace
    return replace(order, state=state, reason=reason)


def _order_from_row(row: sqlite3.Row) -> Order:
    payload = json.loads(row["payload"])
    from orbquant.contracts import Provenance
    provenance_payload = payload.get("provenance")
    provenance = Provenance(**provenance_payload) if provenance_payload else None
    return Order(
        order_id=payload["order_id"],
        idempotency_key=payload["idempotency_key"],
        symbol=payload["symbol"],
        side=OrderSide(payload["side"]),
        quantity=int(payload["quantity"]),
        scheduled_reference_at=datetime.fromisoformat(payload["scheduled_reference_at"]),
        state=OrderState(row["state"]),
        reason=row["reason"],
        provenance=provenance,
        parent_order_id=payload.get("parent_order_id"),
        metadata=tuple(tuple(item) for item in payload.get("metadata", ())),
    )
