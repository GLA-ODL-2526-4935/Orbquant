"""Frozen CPU log-linear forecast of the next 60-minute realised variance."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib.resources import files
import json
import math
import numpy as np
from orbquant.calendar import TradingCalendar


@dataclass(frozen=True)
class VolatilityForecast:
    symbol: str
    decision_at: datetime
    entry_at: datetime
    variance: float
    horizon_minutes: int = 60
    model_id: str = 'loglinear-volatility-public-v1'

    @property
    def volatility_bps(self):
        return math.sqrt(self.variance) * 10_000


class LogLinearVolatility:
    """Inference only. All coefficients and calibration factors are frozen."""
    def __init__(self):
        self.spec = json.loads(files('orbquant').joinpath('baseline.json').read_text())

    def predict_features(self, symbol, features):
        record = self.spec['models'][symbol]
        x = np.asarray(features, dtype=float)
        if x.shape != (4,) or not np.isfinite(x).all():
            raise ValueError('Four finite features are required')
        log_variance = float(np.r_[x, 1.] @ np.asarray(record['coefficients']))
        return math.exp(float(np.clip(log_variance, -40, 0))) * record['factor']

    def forecast(self, bars, decision_at):
        if len(bars) < 120:
            raise ValueError('Require 120 completed contiguous same-session minutes')
        if decision_at.tzinfo is None or decision_at.utcoffset() != timedelta(0):
            raise ValueError('Decision must be aware UTC')
        bars = tuple(bars[-120:])
        symbol = bars[0].symbol
        session = TradingCalendar().session(bars[-1].start.date())
        if session is None:
            raise ValueError('Not a regular trading session')
        if any(b.symbol != symbol or b.available_at > decision_at or b.end > decision_at
               or b.end - b.start != timedelta(minutes=1)
               or not session.open_at <= b.start < b.end <= session.close_at for b in bars):
            raise ValueError('Invalid, unavailable or cross-session input')
        if any(a.end != b.start for a, b in zip(bars, bars[1:])):
            raise ValueError('Missing, duplicate or out-of-order minute')
        if decision_at - bars[-1].available_at > timedelta(seconds=60):
            raise ValueError('Stale input')
        end = bars[-1].end
        if (end - session.open_at).total_seconds() % 300:
            raise ValueError('Predict only at completed five-minute boundaries')
        entry = decision_at.replace(second=0, microsecond=0) + timedelta(minutes=1)
        if entry + timedelta(minutes=60) >= session.close_at:
            raise ValueError('Insufficient same-session exit time')
        closes = np.asarray([float(b.close) for b in bars], dtype=float)
        if not np.isfinite(closes).all() or (closes <= 0).any():
            raise ValueError('Invalid close prices')
        variance60 = max(float(np.square(np.diff(np.log(closes[-61:]))).sum()), 1e-16)
        variance15 = max(float(np.square(np.diff(np.log(closes[-16:]))).sum()) * 4, 1e-16)
        # Match the fitted baseline's five-minute aggregate start and float32
        # cyclical features, rather than using wall-clock time after receipt.
        phase = (end - timedelta(minutes=5) - session.open_at).total_seconds() / (session.close_at-session.open_at).total_seconds()
        sin, cos = np.asarray([math.sin(2*math.pi*phase), math.cos(2*math.pi*phase)], dtype=np.float32)
        features = [math.log(variance60), math.log(variance15), float(sin), float(cos)]
        return VolatilityForecast(symbol, decision_at, entry, self.predict_features(symbol, features))
