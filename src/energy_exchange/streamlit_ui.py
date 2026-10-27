"""Visitor interface; only capacity state is shared across Streamlit sessions."""

import asyncio
import json
import threading
import time
from collections import deque

import streamlit as st
from pydantic import ValidationError

from .interactive import RunInput, presets
from .network import network_html
from .server import MAX_CONCURRENT, MAX_PER_MINUTE, run_worker

TITLES = {
    "normal": "Normal trading",
    "cloud": "Cloud shortfall",
    "utility_limit": "Utility limit",
    "preference_change": "Preference change",
}
DESCRIPTIONS = {
    "normal": "Forecasts match available energy. Start here, then change a budget or price.",
    "cloud": "A cloud reduces S1's actual export after its forecast-based offer. Compare commitment with settlement.",
    "utility_limit": "The utility allows at most 5 kWh of trading. Approval happens before commitment.",
    "preference_change": "S1 keeps a larger reserve and raises its minimum selling price. Both preferences constrain its offer.",
}


class BusyError(RuntimeError):
    pass


class RunGate:
    """Thread-safe process-wide admission; never caches visitor inputs/results."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.recent = deque()

    def run(self, raw):
        payload = RunInput.model_validate(raw).model_dump()
        with self.lock:
            now = time.monotonic()
            while self.recent and now - self.recent[0] >= 60:
                self.recent.popleft()
            if self.active >= MAX_CONCURRENT or len(self.recent) >= MAX_PER_MINUTE:
                raise BusyError(
                    "The lab is busy. Please wait a few seconds and run again."
                )
            self.active += 1
            self.recent.append(now)
        try:
            return asyncio.run(run_worker(payload))
        finally:
            with self.lock:
                self.active -= 1


@st.cache_resource
def gate():
    return RunGate()


def load_preset():
    cfg = presets()[st.session_state.get("preset", "normal")]
    for group in ("sellers", "buyers"):
        for person in cfg[group]:
            for field, value in person.items():
                if field != "name":
                    st.session_state[f"{person['name']}_{field}"] = value
    for key in ("utility_cap_kwh", "grid_tariff", "missing_meter_policy"):
        st.session_state[key] = cfg[key]


def number(label, key, maximum=1000.0, optional=False):
    return st.number_input(
        label,
        min_value=0.0,
        max_value=maximum,
        step=0.01,
        value=None if optional else "min",
        key=key,
        format="%.2f",
    )


def controls():
    st.subheader("Experiment")
    st.selectbox(
        "Starting scenario",
        list(TITLES),
        format_func=TITLES.get,
        key="preset",
        on_change=load_preset,
    )
    st.caption(DESCRIPTIONS[st.session_state.preset])
    st.button("Reset selected preset", on_click=load_preset)
    payload = {"sellers": [], "buyers": []}
    for group, heading, fields in (
        (
            "sellers",
            "Solar sellers",
            [
                ("forecast_surplus", "Forecast surplus (kWh)", 1000.0),
                ("reserve", "Keep in reserve (kWh)", 1000.0),
                ("min_price", "Minimum price (INR/kWh)", 100.0),
            ],
        ),
        (
            "buyers",
            "Energy buyers",
            [
                ("demand", "Expected demand (kWh)", 1000.0),
                ("max_price", "Maximum price (INR/kWh)", 100.0),
                ("budget", "Spending limit (INR)", 100000.0),
            ],
        ),
    ):
        with st.expander(heading):
            st.caption(
                "Surplus is after expected household use; reserve further reduces the offer."
                if group == "sellers"
                else "The budget covers P2P purchases. Residual consumption can be supplied by the grid in the cost comparison."
            )
            if group == "buyers":
                st.caption(
                    "A higher maximum price gives earlier access to sellers, but also raises the price for the same seller. It does not guarantee cheaper energy. See ‘How bids affect priority and price’ below the network."
                )
            for i in range(1, 4):
                name = ("S" if group == "sellers" else "B") + str(i)
                with st.container():
                    st.markdown(f"**{name}**")
                    person = {"name": name}
                    for field, label, maximum in fields:
                        person[field] = number(
                            f"{name} · {label}", f"{name}_{field}", maximum
                        )
                    payload[group].append(person)
    with st.expander("Utility & tariff"):
        payload["utility_cap_kwh"] = number(
            "Aggregate trading cap (kWh)", "utility_cap_kwh"
        )
        payload["grid_tariff"] = number(
            "Grid comparison tariff (INR/kWh)", "grid_tariff", 100.0
        )
    with st.expander("Simulated meter outcomes · after commitment"):
        st.caption(
            "These values define the experiment's realized outcome, not the forecast. Agents see them only after commitment. Clear a reading to simulate missing data."
        )
        for group in ("sellers", "buyers"):
            with st.container():
                field = "actual_export" if group == "sellers" else "actual_import"
                for person in payload[group]:
                    name = person["name"]
                    person[field] = number(
                        f"{name} · {field.replace('_', ' ')} (kWh)",
                        f"{name}_{field}",
                        optional=True,
                    )
        payload["missing_meter_policy"] = st.selectbox(
            "When a meter reading is missing",
            ["pending", "cancel"],
            key="missing_meter_policy",
            format_func=lambda p: (
                "Keep affected trades pending"
                if p == "pending"
                else "Cancel affected trades and release payment"
            ),
        )
    return payload


def render_input_changes(payload, latest):
    """Compare the visitor's current settings with the normal scenario."""
    baseline = presets()["normal"]
    changes = []
    meter_changes = []
    fields = {
        "forecast_surplus": ("Forecast surplus", "kWh"),
        "reserve": ("Keep in reserve", "kWh"),
        "min_price": ("Minimum selling price", "INR/kWh"),
        "demand": ("Expected demand", "kWh"),
        "max_price": ("Maximum buying price", "INR/kWh"),
        "budget": ("Spending limit", "INR"),
    }

    def add(name, label, before, after, unit):
        if before != after:
            changes.append(
                {
                    "Agent / setting": f"{name} · {label}",
                    "Normal trading": f"{before:g} {unit}",
                    "Current settings": f"{after:g} {unit}",
                }
            )

    def reading(value):
        return "missing" if value is None else f"{value:g} kWh"

    for group in ("sellers", "buyers"):
        originals = {p["name"]: p for p in baseline[group]}
        for person in payload[group]:
            name = person["name"]
            original = originals[name]
            for field, (label, unit) in fields.items():
                if field in person:
                    add(name, label, original[field], person[field], unit)
            if group == "sellers":
                add(
                    name,
                    "Energy offered",
                    max(0, original["forecast_surplus"] - original["reserve"]),
                    max(0, person["forecast_surplus"] - person["reserve"]),
                    "kWh",
                )
            field = "actual_export" if group == "sellers" else "actual_import"
            if original[field] != person[field]:
                meter_changes.append(
                    f"{name} {field.replace('_', ' ')}: "
                    f"{reading(original[field])} → {reading(person[field])}"
                )
    add(
        "Utility",
        "Trading cap",
        baseline["utility_cap_kwh"],
        payload["utility_cap_kwh"],
        "kWh",
    )
    add(
        "Comparison",
        "Grid tariff",
        baseline["grid_tariff"],
        payload["grid_tariff"],
        "INR/kWh",
    )
    policy_changed = payload["missing_meter_policy"] != baseline["missing_meter_policy"]
    if not changes and not meter_changes and not policy_changed:
        return
    st.markdown("**What changed from normal trading?**")
    st.caption(
        "Current settings · reflected in the replay below."
        if latest and payload == latest["inputs"]
        else "Current settings · run the simulation to see their effect."
    )
    if changes:
        st.dataframe(changes, hide_index=True, width="stretch")
    if meter_changes:
        st.caption(
            "Separate simulated meter settings: "
            + "; ".join(meter_changes)
            + ". Agents receive these readings only after commitment."
        )
    if policy_changed:
        st.caption(
            "Missing-meter rule: pending → " + payload["missing_meter_policy"] + "."
        )


def narrative(run):
    r = run["result"]
    pending = sum(t["status"] == "pending" for t in r["trades"])
    cancelled = sum(t["status"] == "cancelled" for t in r["trades"])
    if not r["trades"]:
        return "No trades were committed. Check price overlap, available surplus, budgets and the utility cap."
    text = f"The agents committed {r['committed_kwh']:.2f} kWh; meter-supported settlement was {r['settled_kwh']:.2f} kWh. "
    if pending:
        return (
            text
            + f"{pending} trade(s) await readings. INR {r['pending_payment']:.2f} remains reserved; unresolved energy is not yet a confirmed delivery shortfall."
        )
    if cancelled:
        return (
            text
            + f"{cancelled} trade(s) were cancelled under your missing-meter policy."
        )
    return (
        text
        + f"Buyers paid INR {run['metrics']['seller_revenue']:.2f}; INR {r['released_payment']:.2f} of reserved payment was released."
    )


def render_results(run):
    st.iframe(network_html(run), height="content", alt="Energy network replay")
    st.caption(f"Completed run · {run['run_id']} · Evidence verified")
    previous = st.session_state.get("previous")
    for col, (key, label, unit) in zip(
        st.columns(4),
        [
            ("committed_kwh", "Committed", "kWh"),
            ("settled_kwh", "Settled", "kWh"),
            ("released_payment", "Payment released", "INR"),
            ("pending_payment", "Still reserved", "INR"),
        ],
    ):
        col.metric(
            label,
            f"{run['result'][key]:.2f} {unit}",
            delta=f"{run['result'][key] - previous['result'][key]:+.2f} {unit}"
            if previous
            else None,
            delta_color="off",
        )
    if previous:
        st.caption(f"Deltas compare with your previous run: {previous['run_id']}.")
    with st.expander("Trade ledger & household results"):
        st.write(narrative(run))
        st.markdown("**Trade ledger**")
        st.caption(
            "Midpoint pricing, partial fills and commitment-order meter allocation are synthetic research assumptions, not confirmed Tata market rules."
        )
        st.dataframe(run["result"]["trades"], hide_index=True)
        st.markdown("**Buyer outcomes**")
        st.dataframe(run["metrics"]["buyers"], hide_index=True)
        st.caption(
            f"Costs and savings are INR, energy is kWh. Grid costs use valid actual import and an assumed INR {run['inputs']['grid_tariff']:.2f}/kWh tariff. Missing readings or pending trades make final grid cost and savings unavailable. Unfulfilled P2P demand refers to forecast demand, not an outage. Fees and taxes are excluded."
        )
        st.markdown("**Seller revenue**")
        st.dataframe(run["metrics"]["sellers"], hide_index=True)
        st.caption("Revenue is INR; seller export opportunity cost is not modeled.")
    with st.expander("Evidence & downloads"):
        st.dataframe(
            [
                {"Check": s["name"], "Status": s["status"]}
                for s in run["evidence"]["stages"]
            ],
            hide_index=True,
        )
        st.caption(
            "Evidence verifies synthetic execution and replay. Privacy redaction is marked not_tested; these checks do not establish production or regulatory validity."
        )
        with st.expander("Full event trace"):
            st.dataframe(
                [
                    {
                        "Time": e["at"],
                        "Agent": e["observer"],
                        "Event": e["kind"],
                        "Details": json.dumps(e["detail"]),
                    }
                    for e in run["events"]
                ],
                hide_index=True,
            )
        with st.expander("Exact inputs for this result"):
            st.json(run["inputs"])
        public = {k: v for k, v in run.items() if k != "artifacts"}
        downloads = [
            ("Ledger CSV", run["artifacts"]["ledger_csv"], "ledger.csv", "text/csv"),
            (
                "Event trace",
                json.dumps(run["events"], indent=2),
                "trace.json",
                "application/json",
            ),
            (
                "Run inputs",
                json.dumps(run["inputs"], indent=2),
                "inputs.json",
                "application/json",
            ),
            (
                "Run result",
                json.dumps(public, indent=2),
                "result.json",
                "application/json",
            ),
            ("Text report", run["artifacts"]["report_txt"], "report.txt", "text/plain"),
            (
                "Nandatown replay",
                run["artifacts"]["native_html"],
                "replay.html",
                "text/html",
            ),
        ]
        for i, (label, body, filename, mime) in enumerate(downloads):
            st.download_button(
                label,
                body,
                file_name=f"{run['run_id']}-{filename}",
                mime=mime,
                key=f"download_{i}",
                on_click="ignore",
            )
        st.caption(
            "Open the downloaded replay HTML in a browser. Downloads always belong to the displayed run."
        )


def main():
    st.set_page_config(page_title="Energy exchange lab", page_icon="☀️", layout="wide")
    st.title("Energy exchange lab")
    st.write("Six households. One market. Watch offers become meter-supported trades.")
    st.caption("Synthetic research demo · No LLM · kWh / INR")
    if "S1_forecast_surplus" not in st.session_state:
        load_preset()
    with st.sidebar:
        payload = controls()
    latest = st.session_state.get("latest")
    render_input_changes(payload, latest)
    if latest and payload != latest["inputs"]:
        st.warning("You have unrun changes. Run simulation to update this replay.")
    if st.sidebar.button("Run simulation", type="primary", use_container_width=True):
        try:
            with st.spinner("Running Nandatown and verifying evidence…"):
                result = gate().run(payload)
            st.session_state.previous = (
                {"run_id": latest["run_id"], "result": latest["result"]}
                if latest
                else None
            )
            st.session_state.latest = result
            st.rerun()
        except BusyError as exc:
            st.error(str(exc))
        except ValidationError:
            st.error(
                "Check your numeric inputs. Budgets must use at most two decimal places; energy, prices and budgets must stay within the displayed limits."
            )
        except TimeoutError:
            st.error(
                "The simulation timed out. Your last result is preserved. Please retry."
            )
        except (RuntimeError, ValueError, OSError):
            st.error(
                "The simulation could not be completed and verified. Your last result is preserved. Please retry."
            )
    if latest:
        render_results(latest)
    else:
        st.iframe(network_html(), height="content", alt="Energy network replay")
        st.caption("Choose a scenario in the sidebar and select Run simulation.")
    with st.expander("How bids affect priority and price"):
        st.write(
            "Buyers with higher maximum prices go first and choose from the cheapest eligible sellers. That can give them earlier access to low-cost supply."
        )
        st.write(
            "The trade price is halfway between the seller’s minimum and the buyer’s maximum. If a seller asks INR 5/kWh, a buyer maximum of INR 8 gives a price of INR 6.50; a maximum of INR 7 gives INR 6.00—if supply remains when that buyer’s turn arrives."
        )
        st.markdown(
            "**A higher maximum improves priority, but does not guarantee cheaper electricity.**"
        )
        st.caption(
            "This simplified demo rule can encourage strategic bidding because the declared maximum affects both priority and price. It is a research assumption, not a confirmed Tata market rule."
        )
    with st.expander("Why think about mechanism design?"):
        st.markdown(
            "**Strategic bidding means choosing a bid to benefit from the market’s rules, rather than simply reporting your true willingness to pay.** It can be rational behavior; it is not automatically dishonest."
        )
        st.write(
            "Example: in Normal trading, suppose B3 truly values energy at INR 8/kWh. With everyone else’s bids unchanged, declaring INR 7.60 still puts B3 ahead of B2’s INR 7.50. Against S1’s INR 5 minimum, B3 pays INR 6.30 instead of INR 6.50 per kWh—saving INR 0.40 on the same 2 kWh. This is bid shading."
        )
        st.markdown(
            "**Why it matters:** submitted bids may not reveal real preferences. Strategic behavior can prevent useful trades or favor sophisticated bidders. An agent’s apparent improvement could come from exploiting the pricing rule rather than managing energy better. These are risks to test, not outcomes established by this demo."
        )
        st.write(
            "Mechanism design asks how matching and payment rules can make self-interested choices produce useful market outcomes. One goal is incentive compatibility: making truthful reporting a good strategy under stated assumptions."
        )
        st.caption(
            "Research next step: separate private preferences, the agent’s submitted bid, and market rules. Compare incentives, trade volume, household benefits, fairness and whether payments cover payouts. Truthful incentives, efficiency and budget balance involve trade-offs. This demo does not yet model private values separately or compare alternative mechanisms."
        )
        st.markdown(
            "**Before adding smarter LLM traders, test the rules they would be optimizing against.**"
        )
        st.caption(
            "Background: [mechanism design and incentives](https://www.nobelprize.org/uploads/2018/06/advanced-economicsciences2007.pdf) · [buyer–seller auction design](https://theory.stanford.edu/~italgam/papers/Modular%20Double%20Auctions.pdf)"
        )
    with st.expander("Where could an LLM help?"):
        st.markdown("**No LLM is used in this demo.** The roles below are proposed extensions.")
        st.markdown(
            "**1. Set preferences.** Translate requests such as ‘Keep enough energy for tonight and sell the rest above INR 6/kWh’ into structured settings, asking what ‘enough’ means. Code validates the settings and permissions."
        )
        st.markdown(
            "**2. Plan an offer or bid.** Interpret context, such as an upcoming EV trip, and propose a strategy using forecasting or optimization tools. Code checks energy availability, reserves, prices and budgets."
        )
        st.markdown(
            "**3. Coordinate before commitment.** Interpret other agents’ flexibility and propose revised quantities or prices. An REP-based extension could be explored here; REP and back-and-forth bargaining are not implemented in this demo. Hard constraints still govern every commitment."
        )
        st.markdown(
            "**4. Explain the result.** Answer questions such as ‘Why did B1 get no energy?’ using the recorded trades and events. The explanation must match the simulation’s actual numbers."
        )
        st.caption(
            "Matching, meter readings, payment reservation and settlement do not inherently need an LLM. Forecasting and optimization calculate feasible actions; an LLM must not bypass hard constraints."
        )
        st.caption(
            "Proposed first steps: natural-language preferences and trace-grounded explanations. LLM trading strategies would be evaluated later against rule-based and optimization baselines."
        )
    with st.expander("About this research"):
        st.write(
            "Proposed MIT × Tata Power consumer-agent research: how can households respect preferences and coordinate under uncertainty? This demo uses deterministic offers and midpoint matching, without iterative price bargaining."
        )
        st.write(
            "Forecast ≠ commitment ≠ measured delivery. The utility cap is an aggregate trading limit, not electrical network feasibility. No live Tata data, production integration, or policy-superiority claim."
        )
        st.caption(
            "Meter outcomes are hidden from agents until commitment. Missing readings can leave trades pending or cancel them. Runs use temporary server files; download results to keep a copy."
        )
