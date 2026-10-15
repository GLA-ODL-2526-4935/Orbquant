"""UTC exchange-session helpers pinned to the XNYS calendar definition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Iterable
from zoneinfo import ZoneInfo

import exchange_calendars as exchange_calendars

UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class Session:
    trading_date: date
    open_at: datetime
    close_at: datetime

    def __post_init__(self) -> None:
        if self.open_at.tzinfo is None or self.close_at.tzinfo is None:
            raise ValueError("session boundaries must be timezone-aware")
        if self.open_at.utcoffset() != timedelta(0) or self.close_at.utcoffset() != timedelta(0):
            raise ValueError("session boundaries must be UTC")
        if self.open_at >= self.close_at:
            raise ValueError("session open must precede close")

    @property
    def is_early_close(self) -> bool:
        local_close = self.close_at.astimezone(ZoneInfo("America/New_York"))
        return (local_close.hour, local_close.minute) < (16, 0)


class TradingCalendar:
    """Regular trading sessions and valid minute-open references.

    Entry references must be strictly later than the decision timestamp. Exit
    references are measured in trading minutes and constrained to the same
    session; callers cannot silently create an overnight position.
    """

    def __init__(self, name: str = "XNYS", *, timezone_name: str = "America/New_York") -> None:
        self.name = name
        self.timezone_name = timezone_name
        self._zone = ZoneInfo(timezone_name)
        self._calendar = exchange_calendars.get_calendar(name)
        self.version = f"exchange_calendars:{exchange_calendars.__version__}:{name}"

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _label(value: date | datetime | str) -> str:
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return date.fromisoformat(value).isoformat()

    @lru_cache(maxsize=4096)
    def session(self, trading_date: date | str) -> Session | None:
        label = self._label(trading_date)
        if not self._calendar.is_session(label):
            return None
        open_at = self._calendar.session_open(label).to_pydatetime().astimezone(UTC)
        close_at = self._calendar.session_close(label).to_pydatetime().astimezone(UTC)
        return Session(date.fromisoformat(label), open_at, close_at)

    def sessions(self, start: date | datetime | str, end: date | datetime | str) -> tuple[Session, ...]:
        start_date = date.fromisoformat(self._label(start))
        end_date = date.fromisoformat(self._label(end))
        if start_date > end_date:
            raise ValueError("start date must not follow end date")
        labels = self._calendar.sessions_in_range(start_date.isoformat(), end_date.isoformat())
        return tuple(session for label in labels if (session := self.session(label.date())) is not None)

    def session_for(self, timestamp: datetime) -> Session | None:
        stamp = self._aware_utc(timestamp)
        session = self.session(stamp.date())
        if session and session.open_at <= stamp < session.close_at:
            return session
        return None

    def contains_bar(self, start: datetime, end: datetime) -> bool:
        start_utc = self._aware_utc(start)
        end_utc = self._aware_utc(end)
        if end_utc <= start_utc:
            return False
        session = self.session(start_utc.date())
        return bool(session and session.open_at <= start_utc and end_utc <= session.close_at)

    def next_minute_start_after(self, decision_at: datetime) -> datetime | None:
        """First same-session minute open strictly after a decision, if any."""
        stamp = self._aware_utc(decision_at)
        session = self.session(stamp.date())
        if session is None or stamp >= session.close_at:
            return None
        if stamp < session.open_at:
            return session.open_at
        minute_epoch = int(stamp.timestamp() // 60) + 1
        next_start = datetime.fromtimestamp(minute_epoch * 60, tz=UTC)
        if next_start >= session.close_at:
            return None
        return next_start

    def exit_reference(self, entry_reference_at: datetime, horizon_minutes: int, *, close_buffer_minutes: int = 5) -> datetime | None:
        if horizon_minutes <= 0 or close_buffer_minutes < 0:
            raise ValueError("horizon must be positive and close buffer nonnegative")
        entry = self._aware_utc(entry_reference_at)
        session = self.session(entry.date())
        if session is None or not session.open_at <= entry < session.close_at:
            return None
        exit_at = entry + timedelta(minutes=horizon_minutes)
        if exit_at > session.close_at - timedelta(minutes=close_buffer_minutes):
            return None
        return exit_at

    def next_business_session(self, trading_date: date | str, *, offset: int = 1) -> Session:
        """Return a later exchange session (offset 1 means next session)."""
        if offset < 1:
            raise ValueError("offset must be at least one")
        label = self._label(trading_date)
        search_end = date.fromisoformat(label) + timedelta(days=max(14, offset * 5))
        sessions = self._calendar.sessions_in_range(label, search_end.isoformat())
        future = [self.session(ts.date()) for ts in sessions if ts.date().isoformat() > label]
        future = [item for item in future if item is not None]
        while len(future) < offset:
            search_end += timedelta(days=max(14, offset * 5))
            sessions = self._calendar.sessions_in_range(label, search_end.isoformat())
            future = [self.session(ts.date()) for ts in sessions if ts.date().isoformat() > label]
            future = [item for item in future if item is not None]
        return future[offset - 1]

    def previous_business_session(self, trading_date: date | str, *, offset: int = 1) -> Session:
        if offset < 1:
            raise ValueError("offset must be at least one")
        label = self._label(trading_date)
        start = date.fromisoformat(label) - timedelta(days=max(14, offset * 5))
        sessions = self._calendar.sessions_in_range(start.isoformat(), label)
        previous = [self.session(ts.date()) for ts in sessions if ts.date().isoformat() < label]
        previous = [item for item in previous if item is not None]
        while len(previous) < offset:
            start -= timedelta(days=max(14, offset * 5))
            sessions = self._calendar.sessions_in_range(start.isoformat(), label)
            previous = [self.session(ts.date()) for ts in sessions if ts.date().isoformat() < label]
            previous = [item for item in previous if item is not None]
        return previous[-offset]

    def trading_minutes(self, start: datetime, end: datetime) -> int:
        """Count complete minute intervals between two UTC instants in a session."""
        start_utc = self._aware_utc(start)
        end_utc = self._aware_utc(end)
        if end_utc <= start_utc:
            return 0
        total = 0
        for session in self.sessions(start_utc.date(), end_utc.date()):
            left = max(start_utc, session.open_at)
            right = min(end_utc, session.close_at)
            if right > left:
                total += int((right - left).total_seconds() // 60)
        return total
