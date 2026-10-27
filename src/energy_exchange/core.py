"""Deterministic single-interval acceptance model; all inputs are synthetic.

Currency is INR, rounded half-up to paise per trade. Energy is rounded down
at commitment to six decimals. Meter allocation is sequential trade order.
"""

import math
from dataclasses import dataclass, field, replace
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

EPS = 1e-8


def number(value, label, allow_infinity=False):
    if (
        type(value) not in (int, float)
        or value < 0
        or (not math.isfinite(value) and not (allow_infinity and value == math.inf))
    ):
        raise ValueError(f"{label} must be a nonnegative finite number")
    return value


def paise(value):
    number(value, "payment")
    return int(
        (Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


@dataclass(frozen=True)
class Seller:
    name: str
    forecast_surplus: float
    reserve: float
    min_price: float
    actual_export: float | None = None
    meter_quality: str = "valid"

    @property
    def offer_qty(self):
        return max(0.0, self.forecast_surplus - self.reserve)


@dataclass(frozen=True)
class Buyer:
    name: str
    demand: float
    max_price: float
    budget: float
    actual_import: float | None = None
    meter_quality: str = "valid"


@dataclass
class Trade:
    seller: str
    buyer: str
    price: float
    committed_kwh: float
    settled_kwh: float = 0.0
    reserved_payment: float = 0.0
    settled_payment: float = 0.0
    status: str = "committed"
    reason: str = ""


@dataclass
class MarketResult:
    trades: list[Trade] = field(default_factory=list)
    committed_kwh: float = 0.0
    settled_kwh: float = 0.0
    released_payment: float = 0.0
    pending_payment: float = 0.0


def validate_participants(sellers, buyers):
    names = [x.name for x in [*sellers, *buyers]]
    if any(not isinstance(n, str) or not n for n in names) or len(set(names)) != len(
        names
    ):
        raise ValueError("Participant names must be nonempty and unique across roles")
    for s in sellers:
        for key in ("forecast_surplus", "reserve", "min_price"):
            number(getattr(s, key), key)
    for b in buyers:
        for key in ("demand", "max_price", "budget"):
            number(getattr(b, key), key)
        if abs(b.budget * 100 - round(b.budget * 100)) > EPS:
            raise ValueError("Budget must be expressed in whole paise")


def clear_market(sellers, buyers, utility_cap=float("inf")):
    validate_participants(sellers, buyers)
    number(utility_cap, "utility_cap", allow_infinity=True)
    sellers = sorted(sellers, key=lambda x: (x.min_price, x.name))
    buyers = sorted(buyers, key=lambda x: (-x.max_price, x.name))
    sr = {s.name: s.offer_qty for s in sellers}
    br = {b.name: b.demand for b in buyers}
    budgets = {b.name: paise(b.budget) for b in buyers}
    cap = utility_cap
    trades = []
    for b in buyers:
        for s in sellers:
            if cap <= EPS or br[b.name] <= EPS:
                break
            if s.min_price > b.max_price or sr[s.name] <= EPS:
                continue
            price = (s.min_price + b.max_price) / 2
            affordable = budgets[b.name] / 100 / price if price else math.inf
            qty = min(sr[s.name], br[b.name], affordable, cap)
            qty = float(
                Decimal(str(qty)).quantize(Decimal(".000001"), rounding=ROUND_DOWN)
            )
            if qty <= EPS:
                continue
            pay = paise(qty * price)
            trades.append(Trade(s.name, b.name, price, qty, reserved_payment=pay / 100))
            sr[s.name] -= qty
            br[b.name] -= qty
            budgets[b.name] -= pay
            cap -= qty
    return trades


def valid_meter(value, quality):
    return (
        quality == "valid"
        and type(value) in (int, float)
        and math.isfinite(value)
        and value >= 0
    )


def settle(trades, sellers, buyers, missing_meter_policy="pending"):
    """Pure reconciliation of one complete interval; never accumulates payments.

    Pending retains reservations. Cancel releases them. Corrected/estimated
    readings require explicit review and are not accepted as valid readings.
    """
    if missing_meter_policy not in ("pending", "cancel"):
        raise ValueError("missing_meter_policy must be pending or cancel")
    validate_participants(sellers, buyers)
    sm, bm = {s.name: s for s in sellers}, {b.name: b for b in buyers}
    sr = {s.name: s.actual_export for s in sellers}
    br = {b.name: b.actual_import for b in buyers}
    committed_s, committed_b, reserved_b = {}, {}, {}
    for t in trades:
        if t.seller not in sm or t.buyer not in bm:
            raise ValueError("Unknown trade participant")
        for key in ("price", "committed_kwh", "reserved_payment"):
            number(getattr(t, key), key)
        if not sm[t.seller].min_price <= t.price <= bm[t.buyer].max_price:
            raise ValueError("Trade violates price limits")
        if paise(t.reserved_payment) != paise(t.price * t.committed_kwh):
            raise ValueError("Reservation does not match commitment")
        committed_s[t.seller] = committed_s.get(t.seller, 0) + t.committed_kwh
        committed_b[t.buyer] = committed_b.get(t.buyer, 0) + t.committed_kwh
        reserved_b[t.buyer] = reserved_b.get(t.buyer, 0) + paise(t.reserved_payment)
    if (
        any(q > sm[n].offer_qty + EPS for n, q in committed_s.items())
        or any(q > bm[n].demand + EPS for n, q in committed_b.items())
        or any(q > paise(bm[n].budget) for n, q in reserved_b.items())
    ):
        raise ValueError("Commitments violate participant quantity or budget limits")
    result = MarketResult()
    for original in trades:
        t = replace(
            original, settled_kwh=0, settled_payment=0, status="committed", reason=""
        )
        result.committed_kwh += t.committed_kwh
        if not (
            valid_meter(sr[t.seller], sm[t.seller].meter_quality)
            and valid_meter(br[t.buyer], bm[t.buyer].meter_quality)
        ):
            t.status = "pending" if missing_meter_policy == "pending" else "cancelled"
            t.reason = "missing_or_invalid_meter"
            if t.status == "pending":
                result.pending_payment += t.reserved_payment
            else:
                result.released_payment += t.reserved_payment
        else:
            t.settled_kwh = max(0.0, min(t.committed_kwh, sr[t.seller], br[t.buyer]))
            t.settled_payment = (
                min(paise(t.reserved_payment), paise(t.settled_kwh * t.price)) / 100
            )
            t.status = "settled"
            sr[t.seller] -= t.settled_kwh
            br[t.buyer] -= t.settled_kwh
            result.settled_kwh += t.settled_kwh
            result.released_payment += t.reserved_payment - t.settled_payment
        result.trades.append(t)
    result.released_payment = round(result.released_payment, 2)
    result.pending_payment = round(result.pending_payment, 2)
    return result
