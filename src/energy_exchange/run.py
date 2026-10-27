"""Generate four runs, native Nandatown evidence and an offline browser demo."""

import argparse
import csv
import hashlib
import json
import time
from pathlib import Path

from nandatown.bundle import load_bundle, verify_bundle
from nandatown.report import render_report
from nandatown.sim.runner import run_lab
from nandatown.visualizer import render_visualizer

from . import nandatown_plugin  # noqa: F401 - register trusted local validator
from .core import number

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ("normal", "cloud", "utility_limit", "preference_change")
NANDATOWN_REVISION = "b7adc6121868742cf0f00cfebbf2d38bdb349057"


def metrics(result, policies, tariff, events, runtime):
    trades = result["trades"]
    buyers, sellers = [], []
    for name, p in policies.items():
        if p["role"] == "buyer":
            ts = [t for t in trades if t["buyer"] == name]
            delivered = sum(t["settled_kwh"] for t in ts)
            spend = sum(t["settled_payment"] for t in ts)
            residual = max(0, p["demand"] - delivered)
            buyers.append(
                dict(
                    name=name,
                    settled_kwh=delivered,
                    p2p_cost=round(spend, 2),
                    grid_cost=round(residual * tariff, 2),
                    savings=round(delivered * tariff - spend, 2),
                    unfulfilled_p2p_kwh=residual,
                )
            )
        else:
            sellers.append(
                dict(
                    name=name,
                    revenue=round(
                        sum(
                            t["settled_payment"] for t in trades if t["seller"] == name
                        ),
                        2,
                    ),
                )
            )
    return dict(
        committed_kwh=result["committed_kwh"],
        settled_kwh=result["settled_kwh"],
        shortfall_rate=(1 - result["settled_kwh"] / result["committed_kwh"])
        if result["committed_kwh"]
        else 0,
        buyer_savings=round(sum(b["savings"] for b in buyers), 2),
        seller_revenue=round(sum(s["revenue"] for s in sellers), 2),
        unfulfilled_p2p_kwh=sum(b["unfulfilled_p2p_kwh"] for b in buyers),
        released_payment=result["released_payment"],
        pending_payment=result["pending_payment"],
        message_count=sum(e["kind"] == "message_sent" for e in events),
        runtime_seconds=runtime,
        buyers=buyers,
        sellers=sellers,
    )


def generate(out, names=SCENARIOS, tariff=8.5):
    number(tariff, "grid tariff")
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    runs = []
    for name in names:
        scenario = ROOT / "scenarios" / f"energy_p2p_{name}.yaml"
        started = time.perf_counter()
        directory, evidence = run_lab(str(scenario), str(out / "bundles"))
        runtime = time.perf_counter() - started
        if evidence.verdict != "passed":
            raise RuntimeError(f"{name} failed: {evidence.model_dump_json()}")
        problems = verify_bundle(directory)
        if problems:
            raise RuntimeError(f"Bundle verification failed: {problems}")
        bundle = load_bundle(directory)
        events = [e.model_dump() for e in bundle["events"]]
        result = next(e["detail"] for e in events if e["kind"] == "energy_result")
        policies = {
            a.name: a.config["policy"]
            for a in bundle["profile"].agents
            if a.role == "energy_household"
        }
        cap = next(
            a.config["cap_kwh"]
            for a in bundle["profile"].agents
            if a.role == "energy_utility"
        )
        stats = metrics(result, policies, tariff, events, runtime)
        stats["utility_cap_violations"] = int(result["committed_kwh"] > cap + 1e-8)
        relative = str(Path(directory).relative_to(out))
        run = dict(
            scenario=name,
            run_id=bundle["run"].run_id,
            utility_cap_kwh=cap,
            result=result,
            metrics=stats,
            policies=policies,
            events=events,
            evidence=evidence.model_dump(),
            bundle=relative,
            native_visualizer=f"{name}-town.html",
            ledger=f"{name}-ledger.csv",
        )
        runs.append(run)
        (out / f"{name}-town.html").write_text(render_visualizer(bundle))
        (out / f"{name}-report.txt").write_text(render_report(bundle))
        (out / f"{name}-result.json").write_text(
            json.dumps(run, indent=2, allow_nan=False) + "\n"
        )
        with (out / f"{name}-ledger.csv").open("w", newline="") as f:
            fields = (
                list(result["trades"][0])
                if result["trades"]
                else ["seller", "buyer", "committed_kwh", "settled_kwh"]
            )
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(result["trades"])
    provenance = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for pattern in (
            "src/**/*.py",
            "src/**/*.html",
            "scenarios/*",
            "CONTEXT_MANIFEST.sha256",
            "pyproject.toml",
            "requirements-lock.txt",
        )
        for p in sorted(ROOT.glob(pattern))
        if p.is_file()
    }
    data = dict(
        schema_version=1,
        synthetic=True,
        currency="INR",
        grid_tariff=tariff,
        nandatown_revision=NANDATOWN_REVISION,
        policy="deterministic_point_forecast",
        source_hashes=provenance,
        runs=runs,
    )
    serialized = json.dumps(data, allow_nan=False)
    (out / "data.json").write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")
    template = (Path(__file__).parent / "web" / "index.html").read_text()
    (out / "index.html").write_text(
        template.replace("__ENERGY_DATA__", serialized.replace("<", "\\u003c"))
    )
    lines = [
        "# Synthetic Nandatown energy P2P run report",
        "",
        f"Nandatown revision: `{NANDATOWN_REVISION}`. Grid comparison tariff: INR {tariff}/kWh (synthetic assumption).",
        "",
        "| Scenario | Committed kWh | Settled kWh | Shortfall | Released INR | Buyer savings INR | Messages | Evidence |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in runs:
        m = r["metrics"]
        lines.append(
            f"| {r['scenario']} | {m['committed_kwh']:.2f} | {m['settled_kwh']:.2f} | {m['shortfall_rate']:.1%} | {m['released_payment']:.2f} | {m['buyer_savings']:.2f} | {m['message_count']} | {r['evidence']['verdict']} |"
        )
    lines += [
        "",
        "Savings compare settled P2P purchases plus grid purchases for residual demand against grid-only purchases. They exclude fees, taxes and export opportunity cost. Unfulfilled demand means unfulfilled by P2P, not a physical outage. Single deterministic interval; no policy-superiority claim.",
        "",
        "Each bundle includes events, intents, profile, result, integrity manifest and native report. The local trusted energy validator must be registered to replay energy checks (`energy-p2p --verify BUNDLE`). Native `nandatown verify` in a fresh process does not automatically execute custom validators.",
        "",
        "Implemented: four scenarios, native message transport and ledger, separate lifecycle stages, missing-meter pending/cancel handling, trace replay checks and offline demo. Proposed: policy comparisons, LLM interface, Tata adapters, network feasibility and population experiments.",
    ]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="outputs/demo")
    parser.add_argument("--scenario", choices=("all", *SCENARIOS), default="all")
    parser.add_argument(
        "--grid-tariff",
        type=float,
        default=8.5,
        help="Synthetic INR/kWh grid-only comparator",
    )
    parser.add_argument(
        "--verify",
        metavar="BUNDLE",
        help="Verify a bundle with the trusted local energy validator",
    )
    args = parser.parse_args()
    if args.verify:
        problems = verify_bundle(args.verify)
        print(
            "\n".join(problems) if problems else "Bundle integrity and replay verified."
        )
        raise SystemExit(bool(problems))
    data = generate(
        args.out,
        SCENARIOS if args.scenario == "all" else (args.scenario,),
        args.grid_tariff,
    )
    for r in data["runs"]:
        print(
            f"{r['scenario']}: {r['result']['committed_kwh']:g} committed / {r['result']['settled_kwh']:g} settled kWh; {r['evidence']['verdict']}"
        )
    print(Path(args.out).resolve() / "index.html")


if __name__ == "__main__":
    main()
