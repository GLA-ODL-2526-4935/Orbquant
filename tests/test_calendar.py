from datetime import datetime, timezone

from orbquant.calendar import TradingCalendar

UTC = timezone.utc


def test_xnys_holiday_early_close_and_dst_boundaries():
    calendar = TradingCalendar()
    assert calendar.session("2024-07-04") is None
    early = calendar.session("2024-11-29")
    assert early is not None and early.is_early_close
    assert early.close_at == datetime(2024, 11, 29, 18, 0, tzinfo=UTC)
    before_dst = calendar.session("2024-03-08")
    after_dst = calendar.session("2024-03-11")
    assert before_dst is not None and after_dst is not None
    assert before_dst.open_at == datetime(2024, 3, 8, 14, 30, tzinfo=UTC)
    assert after_dst.open_at == datetime(2024, 3, 11, 13, 30, tzinfo=UTC)


def test_entry_boundary_is_strictly_after_decision_and_exit_is_same_session():
    calendar = TradingCalendar()
    decision = datetime(2024, 1, 2, 15, 30, 5, tzinfo=UTC)
    entry = calendar.next_minute_start_after(decision)
    assert entry == datetime(2024, 1, 2, 15, 31, tzinfo=UTC)
    assert calendar.exit_reference(entry, 15) == datetime(2024, 1, 2, 15, 46, tzinfo=UTC)
    assert calendar.next_minute_start_after(datetime(2024, 1, 2, 20, 59, tzinfo=UTC)) is None
    assert calendar.exit_reference(datetime(2024, 1, 2, 20, 55, tzinfo=UTC), 1) is None
