"""Deterministic offline illustration, never evidence of market performance."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from dataclasses import asdict, replace
import json
import math
from orbquant.baseline import LogLinearVolatility
from orbquant.contracts import Bar
from orbquant.options import OptionMark, ReplayRules, black_scholes, replay


def run_demo():
    start = datetime(2024, 7, 1, 13, 30, tzinfo=timezone.utc)
    bars = []
    for i in range(120):
        close = Decimal(str(500*math.exp(.00035*math.sin(i*.7))))
        when = start+timedelta(minutes=i)
        bars.append(Bar(symbol='SPY', feed='synthetic', start=when, end=when+timedelta(minutes=1),
                        available_at=when+timedelta(minutes=1, seconds=5),
                        open=close, high=close, low=close, close=close, volume=100_000))
    forecast = LogLinearVolatility().forecast(bars, bars[-1].available_at)
    def mark(at, minute):
        spot = 500*math.exp(.0006*math.sin(minute*.5))
        years = (10*86400-minute*60)/(365*86400)
        call = black_scholes('call', spot, 500, years, .18)
        put = black_scholes('put', spot, 500, years, .18)
        return OptionMark(at, spot, max(0,call[0]-.02), call[0]+.02,
                          max(0,put[0]-.02), put[0]+.02,
                          call[1]+put[1], call[2]+put[2], call[3]+put[3])
    decision = mark(forecast.decision_at, 0)
    execution = [mark(forecast.entry_at+timedelta(minutes=i), i+1) for i in range(61)]
    policy = replay(forecast, decision, execution)
    # Separate mechanical example: deliberately elevated, invented variance to
    # exercise hedge/cash code. Never describe this as the fitted model's return.
    illustration = replay(replace(forecast, variance=.0002), decision, execution)
    illustration['label'] = 'SYNTHETIC MECHANICS ONLY: deliberately invented high variance, not the model forecast'
    return dict(data='SYNTHETIC; no downloaded data or credentials',
                model_forecast={**asdict(forecast), 'decision_at': forecast.decision_at.isoformat(),
                                'entry_at': forecast.entry_at.isoformat(), 'volatility_bps': forecast.volatility_bps},
                baseline_policy=policy, synthetic_mechanics=illustration,
                market_performance_claim=False, orders_submitted=0)


def main():
    print(json.dumps(run_demo(), indent=2))


if __name__ == '__main__':
    main()
