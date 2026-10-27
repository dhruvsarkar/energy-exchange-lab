"""Bounded visitor inputs and one isolated, evidence-checked simulation."""

import csv
import hashlib
import io
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .core import paise
from .run import NANDATOWN_REVISION, metrics

Energy = Annotated[float, Field(ge=0, le=1000, allow_inf_nan=False)]
Price = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
Budget = Annotated[float, Field(ge=0, le=100000, allow_inf_nan=False)]


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SellerInput(StrictInput):
    name: Literal["S1", "S2", "S3"]
    forecast_surplus: Energy
    reserve: Energy
    min_price: Price
    actual_export: Energy | None


class BuyerInput(StrictInput):
    name: Literal["B1", "B2", "B3"]
    demand: Energy
    max_price: Price
    budget: Budget
    actual_import: Energy | None

    @field_validator("budget")
    @classmethod
    def whole_paise(cls, value):
        if abs(value * 100 - round(value * 100)) > 1e-8:
            raise ValueError("Budget must have at most two decimal places")
        return value


class RunInput(StrictInput):
    sellers: list[SellerInput] = Field(min_length=3, max_length=3)
    buyers: list[BuyerInput] = Field(min_length=3, max_length=3)
    utility_cap_kwh: Energy
    grid_tariff: Price = 8.5
    missing_meter_policy: Literal["pending", "cancel"] = "pending"

    @model_validator(mode="after")
    def fixed_population(self):
        if {s.name for s in self.sellers} != {"S1", "S2", "S3"} or {
            b.name for b in self.buyers
        } != {"B1", "B2", "B3"}:
            raise ValueError("Use each of the six synthetic household IDs exactly once")
        return self


def presets():
    from dataclasses import asdict

    from .demo import SCENARIOS

    result = {}
    for name, cfg in SCENARIOS.items():
        sellers = [
            {k: v for k, v in asdict(s).items() if k != "meter_quality"}
            for s in cfg["sellers"]
        ]
        buyers = [
            {k: v for k, v in asdict(b).items() if k != "meter_quality"}
            for b in cfg["buyers"]
        ]
        result[name] = RunInput(
            sellers=sellers, buyers=buyers, utility_cap_kwh=cfg["cap"]
        ).model_dump()
    return result


def scenario(inputs):
    agents, readings = [], {}
    for s in inputs.sellers:
        policy = s.model_dump(exclude={"name", "actual_export"}) | {"role": "seller"}
        agents.append(
            {"name": s.name, "role": "energy_household", "config": {"policy": policy}}
        )
        readings[s.name] = {
            "kwh": s.actual_export,
            "quality": "valid" if s.actual_export is not None else "missing",
        }
    for b in inputs.buyers:
        policy = b.model_dump(exclude={"name", "actual_import"}) | {"role": "buyer"}
        agents.append(
            {
                "name": b.name,
                "role": "energy_household",
                "config": {"policy": policy, "balance_cents": paise(b.budget)},
            }
        )
        readings[b.name] = {
            "kwh": b.actual_import,
            "quality": "valid" if b.actual_import is not None else "missing",
        }
    names = [a["name"] for a in agents]
    agents.extend(
        [
            {
                "name": "market",
                "role": "energy_market",
                "config": {
                    "participants": names,
                    "missing_meter_policy": inputs.missing_meter_policy,
                },
            },
            {
                "name": "utility",
                "role": "energy_utility",
                "config": {"cap_kwh": inputs.utility_cap_kwh},
            },
            {
                "name": "meter",
                "role": "energy_meter",
                "config": {"readings": readings, "delay": 1},
            },
        ]
    )
    # The client cannot select roles, plugins, faults, files, or Python code.
    return {
        "name": "energy_p2p_interactive",
        "validator": "energy_p2p",
        "plugin_files": [],
        "seed": 42,
        "max_time": 20,
        "agents": agents,
    }


def simulate(raw):
    from nandatown.bundle import load_bundle, verify_bundle
    from nandatown.report import render_report
    from nandatown.sim.runner import run_lab
    from nandatown.visualizer import render_visualizer

    inputs = RunInput.model_validate(raw)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(
        prefix="energy-run-", dir=os.environ.get("ENERGY_RUN_ROOT")
    ) as directory:
        spec_path = Path(directory) / "scenario.yaml"
        spec_path.write_text(json.dumps(scenario(inputs), allow_nan=False))
        bundle_path, evidence = run_lab(str(spec_path), str(Path(directory) / "runs"))
        if evidence.verdict != "passed" or verify_bundle(bundle_path):
            raise RuntimeError("Simulation evidence did not verify")
        bundle = load_bundle(bundle_path)
        events = [e.model_dump() for e in bundle["events"]]
        result = next(e["detail"] for e in events if e["kind"] == "energy_result")
        policies = {
            a.name: a.config["policy"]
            for a in bundle["profile"].agents
            if a.role == "energy_household"
        }
        stats = metrics(
            result, policies, inputs.grid_tariff, events, time.perf_counter() - started
        )
        stats["utility_cap_violations"] = int(
            result["committed_kwh"] > inputs.utility_cap_kwh + 1e-8
        )
        # Cost comparisons use realized buyer import, not its forecast demand.
        actuals = {b.name: b.actual_import for b in inputs.buyers}
        for buyer in stats["buyers"]:
            actual = actuals[buyer["name"]]
            if any(
                t["buyer"] == buyer["name"] and t["status"] == "pending"
                for t in result["trades"]
            ):
                actual = None
            buyer["grid_cost"] = (
                round(max(0, actual - buyer["settled_kwh"]) * inputs.grid_tariff, 2)
                if actual is not None
                else None
            )
            buyer["savings"] = (
                round(buyer["settled_kwh"] * inputs.grid_tariff - buyer["p2p_cost"], 2)
                if actual is not None
                else None
            )
        stats["buyer_savings"] = (
            round(sum(b["savings"] for b in stats["buyers"]), 2)
            if all(b["savings"] is not None for b in stats["buyers"])
            else None
        )

        # Render only a public display copy. The original bundle verified above
        # remains unmodified on disk and is removed with the temporary directory.
        bundle["run"].config["rerun_command"] = (
            "POST downloaded inputs.json to /api/runs"
        )
        ledger = io.StringIO()
        fields = [
            "seller",
            "buyer",
            "price",
            "committed_kwh",
            "settled_kwh",
            "reserved_payment",
            "settled_payment",
            "status",
            "reason",
        ]
        writer = csv.DictWriter(ledger, fieldnames=fields)
        writer.writeheader()
        writer.writerows(result["trades"])
        source_hash = hashlib.sha256(
            b"".join(
                (Path(__file__).parent / n).read_bytes()
                for n in ("core.py", "nandatown_plugin.py", "interactive.py", "run.py")
            )
        ).hexdigest()
        return {
            "schema_version": 1,
            "run_id": bundle["run"].run_id,
            "synthetic": True,
            "inputs": inputs.model_dump(),
            "result": result,
            "metrics": stats,
            "events": events,
            "evidence": evidence.model_dump(),
            "nandatown_revision": NANDATOWN_REVISION,
            "implementation_sha256": source_hash,
            "artifacts": {
                "ledger_csv": ledger.getvalue(),
                "native_html": render_visualizer(bundle),
                "report_txt": render_report(bundle),
            },
        }


if __name__ == "__main__":
    import sys

    # A fresh interpreter isolates Nandatown's global registries per request.
    print(json.dumps(simulate(json.load(sys.stdin)), allow_nan=False))
