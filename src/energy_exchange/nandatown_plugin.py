"""Energy roles and trace validator for the pinned Nandatown Lab API."""

from dataclasses import asdict

from nandatown.records import StageResult
from nandatown.sim.agents import SimAgent, role
from nandatown.sim.validators import validator

from .core import Buyer, Seller, Trade, clear_market, paise, settle


def participants(proposals, readings=None):
    readings = readings or {}
    sellers, buyers = [], []
    for name, p in sorted(proposals.items()):
        r = readings.get(name, {})
        if p["role"] == "seller":
            sellers.append(
                Seller(
                    name,
                    p["forecast_surplus"],
                    p["reserve"],
                    p["min_price"],
                    r.get("kwh"),
                    r.get("quality", "missing"),
                )
            )
        else:
            buyers.append(
                Buyer(
                    name,
                    p["demand"],
                    p["max_price"],
                    p["budget"],
                    r.get("kwh"),
                    r.get("quality", "missing"),
                )
            )
    return sellers, buyers


@role("energy_household")
class Household(SimAgent):
    def on_start(self):
        self.held, self.done = {}, set()
        self.api.register(["energy." + self.config["policy"]["role"]])
        self.api.observe("energy_observe", self.name, self.config["policy"])
        self.api.later(0.2, self.propose)

    def propose(self):
        self.api.observe("energy_proposal", self.name, self.config["policy"])
        self.api.send("market", "energy_propose", self.config["policy"])

    def handle_energy_reserve(self, msg):
        if msg["sender"] != "market":
            return
        d = msg["body"]
        ref = d["trade_id"]
        t = Trade(**d["trade"])
        p = self.config["policy"]
        if ref in self.held:
            return
        if (
            p["role"] != "buyer"
            or t.buyer != self.name
            or t.price > p["max_price"]
            or sum(x.committed_kwh for x in self.held.values()) + t.committed_kwh
            > p["demand"] + 1e-8
        ):
            raise ValueError("Buyer rejected commitment")
        if sum(paise(x.reserved_payment) for x in self.held.values()) + paise(
            t.reserved_payment
        ) > paise(p["budget"]):
            raise ValueError("Buyer budget exceeded")
        if paise(t.reserved_payment):
            self.api.escrow_hold(paise(t.reserved_payment), ref)
        self.held[ref] = t
        self.api.reply(msg, "energy_reserved", {"trade_id": ref})

    def handle_energy_commit_notice(self, msg):
        if msg["sender"] == "market":
            self.api.remember(msg["body"]["trade_id"], msg["body"]["trade"])

    def handle_energy_reconcile(self, msg):
        if msg["sender"] != "market":
            return
        d = msg["body"]
        ref = d["trade_id"]
        t = Trade(**d["trade"])
        if ref in self.done:
            return
        held = self.held[ref]
        if (
            t.buyer != self.name
            or t.seller != held.seller
            or t.settled_kwh > held.committed_kwh
            or paise(t.settled_payment) > paise(held.reserved_payment)
        ):
            raise ValueError("Invalid reconciliation")
        if t.status != "pending":
            # Native escrow has no partial-release API. Refund and pay within
            # one synchronous callback; no other event can interleave here.
            if paise(held.reserved_payment):
                self.api.escrow_refund(ref)
            if paise(t.settled_payment):
                self.api.pay(t.seller, paise(t.settled_payment), ref)
        self.done.add(ref)
        self.api.observe("energy_settlement", ref, asdict(t))
        self.api.reply(msg, "energy_paid", {"trade_id": ref})


@role("energy_utility")
class Utility(SimAgent):
    def on_start(self):
        self.done = False
        self.api.register(["energy.validate"])

    def handle_energy_validate(self, msg):
        if msg["sender"] != "market" or self.done:
            return
        self.done = True
        s, b = participants(msg["body"]["proposals"])
        trades = [asdict(t) for t in clear_market(s, b, self.config["cap_kwh"])]
        self.api.observe(
            "energy_validation",
            "interval-1",
            {"cap_kwh": self.config["cap_kwh"], "trades": trades},
        )
        self.api.reply(msg, "energy_validated", {"trades": trades})


@role("energy_meter")
class Meter(SimAgent):
    def on_start(self):
        self.requested = False
        self.api.register(["energy.meter"])

    def handle_energy_measure(self, msg):
        if msg["sender"] != "market" or self.requested:
            return
        self.requested = True

        def deliver():
            self.api.observe(
                "energy_meter", "interval-1", {"readings": self.config["readings"]}
            )
            self.api.reply(
                msg, "energy_measured", {"readings": self.config["readings"]}
            )

        self.api.later(self.config.get("delay", 1.0), deliver)


@role("energy_market")
class Market(SimAgent):
    def on_start(self):
        self.proposals, self.trades = {}, {}
        self.reserved, self.paid = set(), set()
        self.validated = self.measured = self.committed = False
        self.api.register(["energy.market"])

    def handle_energy_propose(self, msg):
        name = msg["sender"]
        if name not in self.config["participants"] or name in self.proposals:
            return
        self.proposals[name] = msg["body"]
        if len(self.proposals) == len(self.config["participants"]):
            s, b = participants(self.proposals)
            self.api.observe(
                "energy_candidates",
                "interval-1",
                {"trades": [asdict(t) for t in clear_market(s, b)]},
            )
            self.api.send("utility", "energy_validate", {"proposals": self.proposals})

    def handle_energy_validated(self, msg):
        if msg["sender"] != "utility" or self.validated:
            return
        self.validated = True
        self.trades = {f"trade-{i + 1}": t for i, t in enumerate(msg["body"]["trades"])}
        for ref, t in self.trades.items():
            self.api.send(t["buyer"], "energy_reserve", {"trade_id": ref, "trade": t})
        if not self.trades:
            self.commit()

    def handle_energy_reserved(self, msg):
        ref = msg["body"]["trade_id"]
        if ref not in self.trades or msg["sender"] != self.trades[ref]["buyer"]:
            return
        self.reserved.add(ref)
        if self.reserved == set(self.trades) and not self.committed:
            self.commit()

    def commit(self):
        self.committed = True
        self.api.observe("energy_commitment", "interval-1", {"trades": self.trades})
        for ref, t in self.trades.items():
            for name in (t["buyer"], t["seller"]):
                self.api.send(
                    name, "energy_commit_notice", {"trade_id": ref, "trade": t}
                )
        self.api.send("meter", "energy_measure", {"interval": "interval-1"})

    def handle_energy_measured(self, msg):
        if msg["sender"] != "meter" or not self.committed or self.measured:
            return
        self.measured = True
        s, b = participants(self.proposals, msg["body"]["readings"])
        result = settle(
            [Trade(**t) for t in self.trades.values()],
            s,
            b,
            self.config["missing_meter_policy"],
        )
        self.result = asdict(result)
        for ref, t in zip(self.trades, result.trades):
            self.api.send(
                t.buyer, "energy_reconcile", {"trade_id": ref, "trade": asdict(t)}
            )
        if not self.trades:
            self.finish()

    def handle_energy_paid(self, msg):
        ref = msg["body"]["trade_id"]
        if (
            ref not in self.trades
            or msg["sender"] != self.trades[ref]["buyer"]
            or ref in self.paid
        ):
            return
        self.paid.add(ref)
        if self.paid == set(self.trades):
            self.finish()

    def finish(self):
        self.api.observe("energy_result", "interval-1", self.result)


def check(name, ok, events, note):
    return StageResult(
        name=name,
        status="passed" if ok else ("failed" if events else "not_enough_evidence"),
        evidence=[e.event_id for e in events[:8]],
        note=note,
    )


@validator("energy_p2p")
def validate_energy(spec, trace):
    """Recompute expected commitments and allocation from exported evidence.

    Verifies recorded behavior of these scripted roles; it is not a proof of
    adversarial authorization, electrical feasibility, or a Tata billing rule.
    """
    policies = {
        a.name: a.config["policy"] for a in spec.agents if a.role == "energy_household"
    }
    utility = next(a for a in spec.agents if a.role == "energy_utility")
    market = next(a for a in spec.agents if a.role == "energy_market")
    proposals = trace.find("energy_proposal")
    v = trace.find("energy_validation", observer=utility.name)
    c = trace.find("energy_commitment", observer=market.name)
    m = trace.find("energy_meter", observer="meter")
    r = trace.find("energy_result", observer=market.name)
    stages = [
        check(
            "structured_proposals",
            len(proposals) == len(policies)
            and {e.subject: e.detail for e in proposals} == policies
            and all(e.observer == e.subject for e in proposals),
            proposals,
            "Every household emits its configured policy without actual meter values.",
        )
    ]
    complete = len(v) == len(c) == len(m) == len(r) == 1
    stages.append(
        check(
            "lifecycle_order",
            complete
            and trace.index(v[0])
            < trace.index(c[0])
            < trace.index(m[0])
            < trace.index(r[0]),
            v + c + m + r,
            "Utility approval precedes commitment; meter data precedes settlement.",
        )
    )
    if not complete:
        return stages
    try:
        meter = next(a for a in spec.agents if a.role == "energy_meter")
        stages.append(
            check(
                "synthetic_meter_inputs",
                m[0].detail == {"readings": meter.config["readings"]},
                m,
                "Recorded synthetic readings match the declared fixture.",
            )
        )
        s, b = participants(policies)
        expected = clear_market(s, b, utility.config["cap_kwh"])
        expected_dict = {f"trade-{i + 1}": asdict(t) for i, t in enumerate(expected)}
        stages.append(
            check(
                "bounded_commitments",
                c[0].detail["trades"] == expected_dict
                and v[0].detail
                == {
                    "cap_kwh": utility.config["cap_kwh"],
                    "trades": list(expected_dict.values()),
                },
                c + v,
                "Recomputed price, reserve, budget, demand and aggregate-cap constraints.",
            )
        )
        s, b = participants(policies, m[0].detail["readings"])
        result = settle(expected, s, b, market.config["missing_meter_policy"])
        settlements = trace.find("energy_settlement")
        expected_settled = {
            ref: asdict(t) for ref, t in zip(expected_dict, result.trades)
        }
        actual_settled = {e.subject: e.detail for e in settlements}
        stages.append(
            check(
                "meter_supported_settlement",
                asdict(result) == r[0].detail
                and actual_settled == expected_settled
                and len(settlements) == len(expected)
                and all(
                    trace.index(m[0]) < trace.index(e) < trace.index(r[0])
                    and e.observer == e.detail["buyer"]
                    for e in settlements
                ),
                m + r + settlements,
                "Recomputed sequential allocation, pending/cancelled states and released reservations.",
            )
        )
        money_ok = True
        all_money = []
        for ref, t in zip(expected_dict, result.trades):
            holds = trace.find("escrow_held", subject=ref)
            refunds = trace.find("escrow_refunded", subject=ref)
            payments = trace.find("payment_settled", subject=ref)
            expected_hold = paise(t.reserved_payment)
            expected_pay = paise(t.settled_payment)
            money_ok &= len(holds) == int(expected_hold > 0)
            money_ok &= all(
                e.detail == {"from": t.buyer, "cents": expected_hold, "ref": ref}
                or e.detail == {"from": t.buyer, "cents": expected_hold}
                for e in holds
            )
            money_ok &= all(
                trace.index(v[0]) < trace.index(e) < trace.index(c[0]) for e in holds
            )
            money_ok &= len(refunds) == int(expected_hold > 0 and t.status != "pending")
            money_ok &= all(
                e.detail == {"to": t.buyer, "cents": expected_hold}
                and trace.index(m[0]) < trace.index(e) < trace.index(r[0])
                for e in refunds
            )
            money_ok &= len(payments) == int(expected_pay > 0)
            money_ok &= all(
                e.detail == {"from": t.buyer, "to": t.seller, "cents": expected_pay}
                and trace.index(m[0]) < trace.index(e) < trace.index(r[0])
                for e in payments
            )
            all_money += holds + refunds + payments
        allowed_refs = set(expected_dict)
        money_ok &= not trace.find("escrow_released")
        money_ok &= all(
            e.subject in allowed_refs
            for e in trace.events
            if e.kind in ("escrow_held", "escrow_refunded", "payment_settled")
        )
        stages.append(
            check(
                "reservation_and_payment",
                bool(money_ok),
                all_money or r,
                "Native ledger holds, refunds and payments match every energy trade exactly.",
            )
        )
    except (KeyError, TypeError, ValueError) as exc:
        stages.append(check("energy_evidence", False, r, f"Malformed evidence: {exc}"))
    return stages
