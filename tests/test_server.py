import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.energy_exchange.interactive import presets
from src.energy_exchange.server import create_app, run_worker


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = TestClient(self.app)
        self.payload = presets()["normal"]

    def test_context_and_only_public_assets_are_served(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("No LLM", page.text)
        self.assertIn("Research question".lower(), page.text.lower())
        self.assertIn("synthetic", page.text)
        self.assertEqual(page.headers["cache-control"], "no-store")
        for url in (
            "/context/ORIGINAL_MIT_PROPOSAL_EXTRACT.md",
            "/.git/config",
            "/assets/server.py",
            "/outputs/demo/data.json",
        ):
            self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get("/assets/interactive.js").status_code, 200)
        self.assertEqual(self.client.get("/healthz").json()["llm"], False)

    def test_all_presets_run_on_backend(self):
        expected = {
            "normal": (9, 9),
            "cloud": (9, 7),
            "utility_limit": (5, 5),
            "preference_change": (7, 7),
        }
        for name, inputs in self.client.get("/api/presets").json()["presets"].items():
            with self.subTest(name=name):
                response = self.client.post("/api/runs", json=inputs)
                self.assertEqual(response.status_code, 200, response.text)
                r = response.json()
                self.assertEqual(
                    (r["result"]["committed_kwh"], r["result"]["settled_kwh"]),
                    expected[name],
                )
                self.assertEqual(r["evidence"]["verdict"], "passed")
                self.assertIn("message_delivered", [e["kind"] for e in r["events"]])
                self.assertNotIn("/Users/", response.text)
                self.assertNotIn("energy-request-", response.text)
                self.assertNotIn("energy-run-", response.text)
                self.assertIn("seller,buyer", r["artifacts"]["ledger_csv"])

    def test_budget_price_forecast_and_cap_changes_affect_actual_runs(self):
        modifications = [
            ("budget", lambda p: p["buyers"][1].update(budget=0)),
            ("price", lambda p: [s.update(min_price=99) for s in p["sellers"]]),
            (
                "forecast",
                lambda p: [s.update(forecast_surplus=0) for s in p["sellers"]],
            ),
            ("cap", lambda p: p.update(utility_cap_kwh=2)),
        ]
        for name, change in modifications:
            with self.subTest(name=name):
                payload = copy.deepcopy(self.payload)
                change(payload)
                response = self.client.post("/api/runs", json=payload)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertLess(response.json()["result"]["committed_kwh"], 9)

    def test_meter_does_not_leak_into_commitments(self):
        a = self.client.post("/api/runs", json=self.payload).json()
        self.payload["sellers"][0]["actual_export"] = 0
        b = self.client.post("/api/runs", json=self.payload).json()
        self.assertNotEqual(a["run_id"], b["run_id"])
        self.assertEqual(a["result"]["committed_kwh"], b["result"]["committed_kwh"])
        self.assertEqual(b["result"]["settled_kwh"], 5)
        policies = [e["detail"] for e in b["events"] if e["kind"] == "energy_proposal"]
        self.assertNotIn("actual_export", str(policies))

    def test_missing_meter_pending_and_cancel(self):
        self.payload["buyers"][0]["actual_import"] = None
        pending = self.client.post("/api/runs", json=self.payload).json()
        self.assertGreater(pending["result"]["pending_payment"], 0)
        buyer = next(b for b in pending["metrics"]["buyers"] if b["name"] == "B1")
        self.assertIsNone(buyer["savings"])
        self.assertIsNone(buyer["grid_cost"])
        self.payload["missing_meter_policy"] = "cancel"
        cancelled = self.client.post("/api/runs", json=self.payload).json()
        self.assertEqual(cancelled["result"]["pending_payment"], 0)
        self.assertGreater(cancelled["result"]["released_payment"], 0)

    def test_pending_seller_reading_makes_buyer_savings_unavailable(self):
        self.payload["sellers"][0]["actual_export"] = None
        r = self.client.post("/api/runs", json=self.payload).json()
        for buyer in r["metrics"]["buyers"]:
            if buyer["name"] in ("B2", "B3"):
                self.assertIsNone(buyer["grid_cost"])
                self.assertIsNone(buyer["savings"])

    def test_cost_uses_actual_import_not_forecast(self):
        self.payload["buyers"][0]["actual_import"] = 8
        r = self.client.post("/api/runs", json=self.payload).json()
        buyer = next(b for b in r["metrics"]["buyers"] if b["name"] == "B1")
        self.assertEqual(buyer["grid_cost"], (8 - buyer["settled_kwh"]) * 8.5)

    def test_invalid_inputs_cannot_execute_code_or_mutate_presets(self):
        changes = [
            lambda p: p.update(plugin_files=["/tmp/evil.py"]),
            lambda p: p["sellers"][0].update(forecast_surplus=-1),
            lambda p: p["buyers"][0].update(budget=0.001),
            lambda p: p["buyers"][0].update(budget=True),
            lambda p: p["buyers"][0].update(budget="50"),
            lambda p: p["sellers"][0].update(name="S2"),
            lambda p: p.update(utility_cap_kwh=1001),
            lambda p: p["sellers"].append(p["sellers"][0]),
        ]
        for change in changes:
            payload = copy.deepcopy(self.payload)
            change(payload)
            self.assertEqual(
                self.client.post("/api/runs", json=payload).status_code, 422
            )
        self.assertEqual(
            self.client.get("/api/presets").json()["presets"]["normal"], self.payload
        )
        nan = json.dumps(self.payload).replace("99.0", "NaN")
        self.assertEqual(
            self.client.post(
                "/api/runs", content=nan, headers={"content-type": "application/json"}
            ).status_code,
            422,
        )

    def test_malformed_oversized_and_cross_origin_requests(self):
        self.assertEqual(
            self.client.post(
                "/api/runs",
                content="oops",
                headers={"content-type": "application/json"},
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                "/api/runs",
                content="x" * 17000,
                headers={"content-type": "application/json"},
            ).status_code,
            413,
        )
        self.assertEqual(self.client.post("/api/runs", content="{}").status_code, 415)
        self.assertEqual(
            self.client.post(
                "/api/runs",
                json=self.payload,
                headers={"Origin": "https://other.example"},
            ).status_code,
            403,
        )

    def test_public_origin_supports_https_proxy(self):
        async def result(_):
            return {"ok": True}

        self.app.state.runner = result
        with patch.dict("os.environ", {"ENERGY_PUBLIC_ORIGIN": "https://lab.example"}):
            response = self.client.post(
                "/api/runs",
                json=self.payload,
                headers={"Origin": "https://lab.example"},
            )
            self.assertEqual(response.status_code, 200)
            response = self.client.post(
                "/api/runs",
                json=self.payload,
                headers={"Origin": "https://other.example"},
            )
            self.assertEqual(response.status_code, 403)

    def test_busy_rate_limits_and_failures_release_capacity(self):
        self.app.state.active = 2
        self.assertEqual(
            self.client.post("/api/runs", json=self.payload).status_code, 429
        )
        self.app.state.active = 0

        async def timeout(_):
            raise TimeoutError()

        self.app.state.runner = timeout
        self.assertEqual(
            self.client.post("/api/runs", json=self.payload).status_code, 504
        )
        self.assertEqual(self.app.state.active, 0)

        async def failed(_):
            raise RuntimeError("/private/path not for visitors")

        self.app.state.runner = failed
        response = self.client.post("/api/runs", json=self.payload)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("/private/path", response.text)
        self.assertEqual(self.app.state.active, 0)
        import time

        self.app.state.recent.extend([time.monotonic()] * 60)
        self.assertEqual(
            self.client.post("/api/runs", json=self.payload).status_code, 429
        )


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_runs_are_isolated(self):
        a, b = presets()["normal"], presets()["normal"]
        b["utility_cap_kwh"] = 1
        first, second = await asyncio.gather(run_worker(a), run_worker(b))
        self.assertEqual(first["result"]["committed_kwh"], 9)
        self.assertEqual(second["result"]["committed_kwh"], 1)
        self.assertNotEqual(first["run_id"], second["run_id"])

    async def test_timeout_removes_request_directory(self):
        original = tempfile.TemporaryDirectory
        with original() as root:

            def temp(**kwargs):
                return original(dir=root, **kwargs)

            with (
                patch("src.energy_exchange.server.tempfile.TemporaryDirectory", temp),
                patch("src.energy_exchange.server.RUN_TIMEOUT", 0.001),
                self.assertRaises(TimeoutError),
            ):
                await run_worker(presets()["normal"])
            self.assertEqual(list(Path(root).iterdir()), [])
