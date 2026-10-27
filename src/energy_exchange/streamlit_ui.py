"""Visitor interface; only capacity state is shared across Streamlit sessions."""

import asyncio
import json
import threading
import time
from collections import deque

import streamlit as st
from pydantic import ValidationError

from .interactive import RunInput, presets
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
STAGES = {
    "Observe": (
        "energy_observe",
        "Households observe forecasts and preferences. Actual meter readings are withheld from their policies.",
    ),
    "Propose": (
        "energy_proposal",
        "Sellers offer surplus minus reserve. Buyers submit demand, price ceilings and budgets. No commitment exists yet.",
    ),
    "Validate": (
        "energy_validation",
        "The utility applies the aggregate trading cap. This is not electrical network feasibility.",
    ),
    "Commit": (
        "energy_commitment",
        "Buyer payment is reserved before commitments are recorded. Promised energy is not measured delivery.",
    ),
    "Meter": (
        "energy_meter",
        "The meter role now reveals import/export readings. Missing readings cannot support settlement.",
    ),
    "Settle": (
        "energy_result",
        "Valid readings limit settlement in commitment order. Unused payment is released; missing readings follow the selected policy.",
    ),
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
    st.subheader("Set the conditions")
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
        with st.expander(heading, expanded=True):
            st.caption(
                "Surplus is after expected household use; reserve further reduces the offer."
                if group == "sellers"
                else "The budget covers P2P purchases. Residual consumption can be supplied by the grid in the cost comparison."
            )
            for i, column in enumerate(st.columns(3), 1):
                name = ("S" if group == "sellers" else "B") + str(i)
                with column:
                    st.markdown(f"**{name}**")
                    person = {"name": name}
                    for field, label, maximum in fields:
                        person[field] = number(
                            f"{name} · {label}", f"{name}_{field}", maximum
                        )
                    payload[group].append(person)
    left, right = st.columns(2)
    with left:
        payload["utility_cap_kwh"] = number(
            "Aggregate trading cap (kWh)", "utility_cap_kwh"
        )
    with right:
        payload["grid_tariff"] = number(
            "Grid comparison tariff (INR/kWh)", "grid_tariff", 100.0
        )
    with st.expander("Simulated meter outcomes · after commitment"):
        st.caption(
            "These values define the experiment's realized outcome, not the forecast. Agents see them only after commitment. Clear a reading to simulate missing data."
        )
        for group, column in zip(("sellers", "buyers"), st.columns(2)):
            with column:
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
    st.divider()
    st.subheader("Simulation results")
    st.success("Run evidence verified")
    st.caption(
        f"{run['run_id']} · Fresh Nandatown simulation · {run['metrics']['message_count']} messages"
    )
    st.write(narrative(run))
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
    replay, ledger, evidence = st.tabs(
        ["Lifecycle & messages", "Trades & household outcomes", "Evidence & downloads"]
    )
    with replay:
        stage = st.radio("Lifecycle stage", list(STAGES), index=5, horizontal=True)
        kind, explanation = STAGES[stage]
        st.write(explanation)
        end = max(
            (i for i, event in enumerate(run["events"]) if event["kind"] == kind),
            default=-1,
        )
        rows = [
            {
                "Time": e["at"],
                "Agent": e["observer"],
                "Event": e["kind"],
                "Details": json.dumps(e["detail"]),
            }
            for e in run["events"][: end + 1]
            if e["kind"].startswith("energy_") or e["kind"] == "message_sent"
        ]
        st.dataframe(rows, hide_index=True)
        st.caption(
            "Events accumulate through the selected stage. Headline values describe the completed run."
        )
    with ledger:
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
    with evidence:
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
    st.caption("MIT × TATA POWER · PROPOSED CONSUMER-AGENT RESEARCH")
    st.title("What should a household promise to trade?")
    st.markdown(
        "**A forecast is a prediction. A commitment is a promise. A meter reading determines what can be settled.**"
    )
    st.write(
        "Explore six synthetic households: three offer spare solar energy and three want to buy it. Change their preferences or forecasts, run the agents, and see what gets agreed and paid."
    )
    st.info(
        "Research question: How can customer agents respect individual preferences, make decisions under uncertainty, and coordinate with utility constraints? This lab demonstrates the workflow with deterministic rules. No LLM is used, and smarter policies have not yet been compared."
    )
    for col, title, body in zip(
        st.columns(3),
        [
            "01 · Households decide",
            "02 · The market checks",
            "03 · Meters determine payment",
        ],
        [
            "Sellers keep a reserve and set a minimum price. Buyers set demand, a price ceiling and a budget.",
            "Compatible offers match. A utility trading cap limits commitments, and buyers reserve payment.",
            "Readings arrive after commitment. Payment follows measured quantities; missing readings can leave trades pending or cancel them.",
        ],
    ):
        col.markdown(f"**{title}**")
        col.write(body)
    st.caption(
        "One synthetic trading interval · Energy in kWh (one unit) · Currency in INR · Aggregate trading cap, not a power-flow model · No live Tata data or production integration"
    )
    if "S1_forecast_surplus" not in st.session_state:
        load_preset()
    payload = controls()
    latest = st.session_state.get("latest")
    if latest and payload != latest["inputs"]:
        st.warning(
            "You have unrun changes. The results below still belong to the previous inputs. Select Run simulation to update them."
        )
    if st.button("Run simulation", type="primary"):
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
        st.info(
            "Choose a scenario or edit the controls, then select Run simulation to generate your first result."
        )
    with st.expander("Research context and limits"):
        st.write(
            "This prototype explores a proposed consumer-agent research direction. It demonstrates Observe → Propose → Validate → Commit → Meter → Settle using Nandatown. It does not claim policy superiority, production settlement, network feasibility, real device control, or dedicated physical electricity flow between households. Policy comparisons are the next scientific milestone."
        )
    st.caption(
        "Each simulation runs in an isolated temporary worker. Temporary files and signing keys are deleted afterward. Results remain in your Streamlit session; download them to keep a copy. No account or LLM API key is needed to run this synthetic demo."
    )
