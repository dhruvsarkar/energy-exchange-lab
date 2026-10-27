"""Build a visual replay strictly from recorded energy events."""

import copy
import json
from pathlib import Path

PHASES = ["Observe", "Propose", "Validate", "Commit", "Meter", "Settle"]


def replay_data(run=None):
    state = {
        "phase": "Observe",
        "title": "Ready to trade",
        "note": "Run a scenario to watch offers, checks and trades.",
        "source": None,
        "target": None,
        "trades": [],
        "readings": {},
        "committed": 0,
        "settled": 0,
        "paid": 0,
        "at": 0,
    }
    frames = [copy.deepcopy(state)]
    if run is None:
        return {"run_id": None, "frames": frames, "policies": {}, "utility_cap": None}
    state["note"] = (
        "Households observe forecasts and preferences. Meter outcomes are still hidden."
    )
    frames[0] = copy.deepcopy(state)
    trade_map = {}
    for event in run["events"]:
        kind, d = event["kind"], event["detail"]
        source, target, title, note = event["observer"], None, "", ""
        phase = state["phase"]
        if kind == "message_sent":
            msg, body, target = d.get("kind"), d.get("body", {}), d.get("to")
            if msg == "energy_propose":
                phase = "Propose"
                title = f"{source} → Market · {'offer' if body['role'] == 'seller' else 'bid'}"
                if body["role"] == "seller":
                    note = f"Offers {max(0, body['forecast_surplus'] - body['reserve']):.2f} kWh at a minimum INR {body['min_price']:.2f}/kWh."
                else:
                    note = f"Requests {body['demand']:.2f} kWh, up to INR {body['max_price']:.2f}/kWh; budget INR {body['budget']:.2f}."
            elif msg == "energy_validate":
                phase, title, note = (
                    "Validate",
                    "Market → Utility · request approval",
                    "Check candidate trades against the aggregate trading cap.",
                )
            elif msg == "energy_validated":
                phase, title, note = (
                    "Validate",
                    "Utility → Market · approval",
                    f"{len(body['trades'])} candidate trades pass the utility check. Payment is not yet reserved.",
                )
            elif msg == "energy_reserve":
                phase, title = "Commit", f"Market → {target} · reserve payment"
                note = f"Reserve INR {body['trade']['reserved_payment']:.2f} for {body['trade_id']}."
            elif msg == "energy_reserved":
                phase, title, note = (
                    "Commit",
                    f"{source} → Market · reservation accepted",
                    f"{body['trade_id']}: the buyer has reserved payment within its budget.",
                )
            elif msg == "energy_commit_notice":
                phase, title, note = (
                    "Commit",
                    f"Market → {target} · trade confirmed",
                    f"{body['trade_id']}: {body['trade']['seller']} → {body['trade']['buyer']}, {body['trade']['committed_kwh']:.2f} kWh at INR {body['trade']['price']:.2f}/kWh.",
                )
            elif msg == "energy_measure":
                phase, title, note = (
                    "Meter",
                    "Market → Meter · request readings",
                    "Commitments are fixed. Wait for the simulated interval's actual import and export.",
                )
            elif msg == "energy_measured":
                phase, title, note = (
                    "Meter",
                    "Meter → Market · readings delivered",
                    "The market can now calculate meter-supported settlement.",
                )
            elif msg == "energy_reconcile":
                phase, title, note = (
                    "Settle",
                    f"Market → {target} · reconcile trade",
                    f"{body['trade_id']}: {body['trade']['settled_kwh']:.2f} kWh supported; buyer processes the settlement next.",
                )
            elif msg == "energy_paid":
                phase, title, note = (
                    "Settle",
                    f"{source} → Market · outcome acknowledged",
                    f"{body['trade_id']}: settlement or missing-reading outcome recorded.",
                )
            else:
                continue
        elif kind in ("energy_candidates", "energy_validation"):
            validated = kind == "energy_validation"
            phase = "Validate" if validated else "Propose"
            title = (
                "Utility checks trading cap"
                if validated
                else "Market finds compatible offers"
            )
            note = (
                f"{len(d['trades'])} {'approved candidates' if validated else 'candidate matches'}"
                + (
                    f" within a {d['cap_kwh']:.2f} kWh cap."
                    if validated
                    else ". Prices use each pair's midpoint; no trade is committed yet."
                )
            )
            trade_map = {
                f"trade-{i + 1}": dict(
                    t, status="approved" if validated else "candidate"
                )
                for i, t in enumerate(d["trades"])
            }
        elif kind == "energy_commitment":
            phase, title, note = (
                "Commit",
                "Trades committed",
                "All required buyer reservations are in place. These quantities are promises, not measured delivery.",
            )
            trade_map = {
                key: dict(t, status="committed") for key, t in d["trades"].items()
            }
            state["committed"] = sum(t["committed_kwh"] for t in trade_map.values())
        elif kind == "energy_meter":
            phase, title, note = (
                "Meter",
                "Meter outcomes revealed",
                "Actual readings are now available. Missing readings cannot support settlement.",
            )
            state["readings"] = d["readings"]
        elif kind == "energy_settlement":
            phase, target = "Settle", d["seller"]
            title = f"{d['buyer']} → {d['seller']} · {d['status']}"
            note = f"{d['settled_kwh']:.2f} / {d['committed_kwh']:.2f} kWh settled; INR {d['settled_payment']:.2f} paid."
            if d["status"] == "pending":
                note += " Payment remains reserved while readings are missing."
            elif d["status"] == "cancelled":
                note += " Reservation released under the missing-reading policy."
            trade_map[event["subject"]] = dict(d)
            state["settled"] = sum(t["settled_kwh"] for t in trade_map.values())
            state["paid"] = sum(t["settled_payment"] for t in trade_map.values())
        elif kind == "energy_result":
            phase, title, note = (
                "Settle",
                "Run complete",
                f"{d['settled_kwh']:.2f} of {d['committed_kwh']:.2f} kWh settled. INR {d['released_payment']:.2f} released; INR {d['pending_payment']:.2f} still reserved.",
            )
            state.update(committed=d["committed_kwh"], settled=d["settled_kwh"])
        else:
            continue
        state.update(
            phase=phase,
            title=title,
            note=note,
            source=source,
            target=target,
            at=event["at"],
            event_id=event.get("event_id"),
            trades=[dict(t, id=ref) for ref, t in trade_map.items()],
        )
        frames.append(copy.deepcopy(state))
    return {
        "run_id": run["run_id"],
        "frames": frames,
        "policies": {
            e["observer"]: e["detail"]
            for e in run["events"]
            if e["kind"] == "energy_observe"
        },
        "utility_cap": run["inputs"]["utility_cap_kwh"],
    }


def network_html(run=None):
    template = (Path(__file__).parent / "web/network.html").read_text()
    data = json.dumps(replay_data(run), allow_nan=False).replace("<", "\\u003c")
    return template.replace("__REPLAY_DATA__", data)
