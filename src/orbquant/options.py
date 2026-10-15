"""Isolated, deterministic synthetic/quote replay. No broker connection."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN
import math


def money(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('Nonfinite money')
    return result.quantize(Decimal('.01'), rounding=ROUND_HALF_EVEN)


def black_scholes(kind, spot, strike, years, sigma, rate=.04):
    """European, zero-dividend approximation: not American option valuation."""
    if kind not in ('call', 'put') or not all(math.isfinite(v) and v > 0 for v in (spot, strike, years, sigma)):
        raise ValueError('Invalid option inputs')
    if not math.isfinite(rate):
        raise ValueError('Invalid interest rate')
    root = math.sqrt(years)
    d1 = (math.log(spot/strike)+(rate+.5*sigma*sigma)*years)/(sigma*root)
    d2 = d1-sigma*root
    norm = lambda z: .5*(1+math.erf(z/math.sqrt(2)))
    pdf = math.exp(-.5*d1*d1)/math.sqrt(2*math.pi)
    discount = math.exp(-rate*years)
    gamma = pdf/(spot*sigma*root)
    theta = -spot*pdf*sigma/(2*root)
    if kind == 'call':
        return spot*norm(d1)-strike*discount*norm(d2), norm(d1), gamma, theta-rate*strike*discount*norm(d2)
    return strike*discount*norm(-d2)-spot*norm(-d1), norm(d1)-1, gamma, theta+rate*strike*discount*norm(-d2)


@dataclass(frozen=True)
class OptionMark:
    at: datetime
    spot: float
    call_bid: float
    call_ask: float
    put_bid: float
    put_ask: float
    delta: float  # Aggregate call+put delta per share.
    gamma: float
    theta_year: float

    def validate(self):
        values = (self.spot, self.call_bid, self.call_ask, self.put_bid, self.put_ask,
                  self.delta, self.gamma, self.theta_year)
        if self.at.tzinfo is None or self.at.utcoffset() != timedelta(0) or not all(math.isfinite(v) for v in values):
            raise ValueError('Marks require UTC and finite values')
        if not (self.spot > 0 and 0 <= self.call_bid <= self.call_ask and 0 <= self.put_bid <= self.put_ask and self.gamma >= 0):
            raise ValueError('Invalid or crossed prices')


@dataclass(frozen=True)
class ReplayRules:
    initial_cash: float = 10_000
    minimum_edge_usd: float = 5
    max_premium_fraction: float = .2
    max_reserved_fraction: float = .9
    stock_cost_bps_per_side: float = 2
    option_fee_per_contract_side: float = .65
    borrow_annual_rate: float = .03
    funding_annual_rate: float = .06

    def __post_init__(self):
        if not all(math.isfinite(v) and v >= 0 for v in self.__dict__.values()) or self.initial_cash <= 0:
            raise ValueError('Invalid replay rules')
        if not 0 < self.max_premium_fraction <= 1 or not 0 < self.max_reserved_fraction <= 1:
            raise ValueError('Invalid risk fractions')


class CashJournal:
    def __init__(self, initial):
        self.initial = self.cash = money(initial)
        self.entries = []
        self.ids = set()

    def post(self, event_id, amount, when, account):
        if event_id in self.ids:
            raise ValueError('Duplicate accounting event')
        amount = money(amount)
        self.cash += amount
        self.ids.add(event_id)
        self.entries.append(dict(id=event_id, at=when.isoformat(), cash_delta=str(amount),
                                 counter_delta=str(-amount), account=account, cash_after=str(self.cash)))

    def verify(self):
        running = self.initial
        for entry in self.entries:
            assert money(entry['cash_delta']) + money(entry['counter_delta']) == 0
            running += money(entry['cash_delta'])
            assert running == money(entry['cash_after'])
        assert running == self.cash


def assess(mark, variance, rules=ReplayRules()):
    mark.validate()
    if not math.isfinite(variance) or not 0 < variance <= 1:
        raise ValueError('Invalid integrated variance forecast')
    # Local gamma/theta approximation, not an options return forecast.
    gross = .5*100*mark.gamma*mark.spot**2*variance + 100*mark.theta_year/(365*24)
    spread = 100*(mark.call_ask-mark.call_bid+mark.put_ask-mark.put_bid)
    stock_turnover = 200*abs(mark.delta)+100*mark.gamma*mark.spot*math.sqrt(variance)*(math.sqrt(12)+1)*math.sqrt(2/math.pi)
    stock_cost = stock_turnover*mark.spot*rules.stock_cost_bps_per_side/10_000
    premium = 100*(mark.call_ask+mark.put_ask)
    carry = (premium*rules.funding_annual_rate+abs(100*mark.delta)*mark.spot*rules.borrow_annual_rate)/(365*24)
    net = gross-spread-stock_cost-4*rules.option_fee_per_contract_side-carry
    shares = round(-100*mark.delta)
    reserved = premium+abs(shares)*mark.spot*(1.5 if shares < 0 else 1)
    reasons = []
    if net <= rules.minimum_edge_usd:
        reasons.append('estimated_edge_below_buffer')
    if premium > rules.initial_cash*rules.max_premium_fraction:
        reasons.append('premium_limit')
    if reserved > rules.initial_cash*rules.max_reserved_fraction:
        reasons.append('reserve_limit')
    return dict(eligible=not reasons, reasons=reasons, estimated_net_edge_usd=net)


def replay(forecast, decision_mark, execution_marks, rules=ReplayRules()):
    """One long straddle; rebalance stock every five minutes; no orders emitted.

    Entry is fixed before seeing future marks. Missing entry/exit data is not
    silently replaced or dropped. This isolated engine can simulate a short
    stock hedge; it does not change the long-only core GUARD or broker.
    """
    decision_mark.validate()
    if decision_mark.at > forecast.decision_at or forecast.decision_at-decision_mark.at > timedelta(seconds=30):
        raise ValueError('Unavailable or stale decision quote')
    if forecast.entry_at <= forecast.decision_at or forecast.horizon_minutes != 60:
        raise ValueError('Require future entry and fixed 60-minute holding period')
    result = assess(decision_mark, forecast.variance, rules)
    if not result['eligible']:
        return dict(status='abstained', decision=result, pnl=None, journal=[])
    marks = tuple(execution_marks)
    for mark in marks:
        mark.validate()
    if any(a.at >= b.at for a, b in zip(marks, marks[1:])):
        raise ValueError('Execution marks must be unique and chronological')
    if not marks or marks[0].at != forecast.entry_at:
        return dict(status='missing_entry', decision=result, pnl=None, journal=[])
    deadline = forecast.entry_at+timedelta(minutes=60)
    book = CashJournal(rules.initial_cash)
    shares = 0
    prior = marks[0]
    premium = 100*(prior.call_ask+prior.put_ask)
    initial_shares = round(-100*prior.delta)
    if premium > rules.initial_cash*rules.max_premium_fraction or premium+abs(initial_shares)*prior.spot*(1.5 if initial_shares < 0 else 1) > rules.initial_cash*rules.max_reserved_fraction:
        return dict(status='entry_risk_rejected', decision=result, pnl=None, journal=[])
    book.post('entry_options', -premium-2*rules.option_fee_per_contract_side, prior.at, 'options')
    for i, mark in enumerate(marks):
        if mark.at > deadline+timedelta(minutes=5):
            break
        if i and mark.at < deadline and mark.at-prior.at != timedelta(minutes=1):
            book.verify()
            return dict(status='unresolved', decision=result, pnl=None, journal=book.entries,
                        reason='Missing intrahold minute; no interpolation or performance scored')
        elapsed = (mark.at-prior.at).total_seconds()/(365*86400)
        carry = (abs(min(shares, 0))*prior.spot*rules.borrow_annual_rate+premium*rules.funding_annual_rate)*elapsed
        book.post(f'carry_{i}', -carry, mark.at, 'carry')
        if mark.at >= deadline:
            if mark.at-deadline > timedelta(minutes=5):
                break
            book.post('exit_options', 100*(mark.call_bid+mark.put_bid)-2*rules.option_fee_per_contract_side, mark.at, 'options')
            book.post('exit_stock', shares*mark.spot-abs(shares)*mark.spot*rules.stock_cost_bps_per_side/10_000, mark.at, 'stock')
            book.verify()
            return dict(status='closed', decision=result, pnl=str(book.cash-book.initial), journal=book.entries,
                        exit_at=mark.at.isoformat(), assumption='Simultaneous top-of-book full fills; no depth or queue model')
        if i == 0 or (mark.at-forecast.entry_at).total_seconds() % 300 == 0:
            target = round(-100*mark.delta)
            reserve = premium+abs(target)*mark.spot*(1.5 if target < 0 else 1)
            if reserve > rules.initial_cash*rules.max_reserved_fraction:
                # Close at this observed mark; a hard limit cannot be bypassed by the model.
                book.post('risk_exit_options', 100*(mark.call_bid+mark.put_bid)-2*rules.option_fee_per_contract_side, mark.at, 'options')
                book.post('risk_exit_stock', shares*mark.spot-abs(shares)*mark.spot*rules.stock_cost_bps_per_side/10_000, mark.at, 'stock')
                book.verify()
                return dict(status='risk_closed', decision=result, pnl=str(book.cash-book.initial), journal=book.entries)
            change = target-shares
            book.post(f'hedge_{i}', -change*mark.spot-abs(change)*mark.spot*rules.stock_cost_bps_per_side/10_000, mark.at, 'stock')
            shares = target
        prior = mark
    book.verify()
    return dict(status='unresolved', decision=result, pnl=None, journal=book.entries,
                reason='No observed simultaneous exit by five-minute deadline; no performance scored')
