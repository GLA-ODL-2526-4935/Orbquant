from dataclasses import replace
from datetime import timedelta
import json
import math
import pytest
from orbquant.baseline import LogLinearVolatility
from orbquant.demo import run_demo
from orbquant.options import OptionMark, ReplayRules, CashJournal, black_scholes, replay


def test_demo_deterministic_and_explicitly_synthetic():
    first, second = run_demo(), run_demo()
    assert first == second
    assert first['orders_submitted'] == 0
    assert not first['market_performance_claim']
    assert first['synthetic_mechanics']['status'] == 'closed'
    assert len(first['synthetic_mechanics']['journal']) > 12
    assert all(float(row['cash_delta']) == -float(row['counter_delta']) for row in first['synthetic_mechanics']['journal'])


def test_model_is_positive_finite_and_rejects_bad_features():
    model = LogLinearVolatility()
    for symbol in ('SPY','QQQ'):
        assert 0 < model.predict_features(symbol, [-12,-12,0,1]) < 1
    with pytest.raises(ValueError):
        model.predict_features('SPY', [float('nan')]*4)


@pytest.mark.parametrize('problem', ['short','gap','duplicate','future','stale','session','boundary'])
def test_features_reject_unavailable_or_incomplete_history(bar_factory, problem):
    from datetime import datetime, timezone
    start = datetime(2024,7,1,13,30,tzinfo=timezone.utc)
    bars = [bar_factory(start=start+timedelta(minutes=i)) for i in range(120)]
    decision = bars[-1].available_at
    if problem == 'short':
        bars = bars[:-1]
    elif problem == 'gap':
        bars[20] = replace(bars[20],start=bars[20].start+timedelta(seconds=1))
    elif problem == 'duplicate':
        bars[20] = bars[19]
    elif problem == 'future':
        bars[-1] = replace(bars[-1],available_at=decision+timedelta(seconds=1))
    elif problem == 'stale':
        decision += timedelta(seconds=61)
    elif problem == 'session':
        bars[0] = replace(bars[0],symbol='QQQ')
    elif problem == 'boundary':
        bars = [replace(b,start=b.start+timedelta(minutes=1),end=b.end+timedelta(minutes=1),
                        available_at=b.available_at+timedelta(minutes=1)) for b in bars]
        decision += timedelta(minutes=1)
    with pytest.raises(ValueError):
        LogLinearVolatility().forecast(bars,decision)


def test_journal_rejects_duplicate_and_cent_rounds():
    from datetime import datetime, timezone
    book = CashJournal(100)
    when = datetime.now(timezone.utc)
    book.post('one', .015, when, 'fees')
    assert str(book.cash) == '100.02'
    book.verify()
    with pytest.raises(ValueError):
        book.post('one', 1, when, 'fees')


def test_option_values_and_hedge_signs():
    call, put = black_scholes('call',100,100,.1,.2), black_scholes('put',100,100,.1,.2)
    assert 0 < call[1] < 1 and -1 < put[1] < 0
    assert call[2] > 0 and put[2] > 0
    assert abs((call[0]-put[0])-(100-100*math.exp(-.04*.1))) < 1e-8


@pytest.mark.parametrize('problem', ['same_bar', 'stale', 'nan', 'missing_entry', 'missing_exit', 'gap', 'higher_fees'])
def test_replay_rejects_unsafe_or_unscorable_cases(problem):
    from datetime import datetime, timezone
    from orbquant.baseline import VolatilityForecast
    at = datetime(2024,7,1,16,0,5,tzinfo=timezone.utc)
    forecast = VolatilityForecast('SPY',at,at.replace(second=0)+timedelta(minutes=1),.0002)
    mark = OptionMark(at,100,1,1.01,1,1.01,0,.2,-20)
    marks = [replace(mark, at=forecast.entry_at+timedelta(minutes=i)) for i in range(61)]
    rules = ReplayRules()
    if problem == 'same_bar':
        forecast = replace(forecast,entry_at=at)
    elif problem == 'stale':
        mark = replace(mark,at=at-timedelta(seconds=31))
    elif problem == 'nan':
        forecast = replace(forecast,variance=float('nan'))
    elif problem == 'missing_entry':
        marks = marks[1:]
    elif problem == 'missing_exit':
        marks = marks[:-1]
    elif problem == 'gap':
        marks = marks[:3]+marks[4:]
    elif problem == 'higher_fees':
        rules = replace(rules,option_fee_per_contract_side=100_000)
    if problem in ('same_bar','stale','nan'):
        with pytest.raises(ValueError):
            replay(forecast,mark,marks,rules)
    else:
        result = replay(forecast,mark,marks,rules)
        assert result['status'] == {'missing_entry':'missing_entry','missing_exit':'unresolved','gap':'unresolved','higher_fees':'abstained'}[problem]
        assert result['pnl'] is None
