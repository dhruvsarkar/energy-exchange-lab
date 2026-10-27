"""Guard replay causality: never show future measurements or settled trades."""

import asyncio
import unittest

from src.energy_exchange.interactive import presets
from src.energy_exchange.network import network_html, replay_data
from src.energy_exchange.server import run_worker


class NetworkTests(unittest.TestCase):
    def run_case(self, payload):
        return asyncio.run(run_worker(payload))

    def test_replay_preserves_events_and_final_values(self):
        for name, payload in presets().items():
            with self.subTest(scenario=name):
                run = self.run_case(payload)
                frames = replay_data(run)["frames"]
                self.assertEqual(frames[-1]["settled"], run["result"]["settled_kwh"])
                self.assertEqual(
                    frames[-1]["committed"], run["result"]["committed_kwh"]
                )
                self.assertAlmostEqual(
                    frames[-1]["paid"], run["metrics"]["seller_revenue"]
                )
                events = {e["event_id"]: e for e in run["events"]}
                for frame in frames[1:]:
                    self.assertIn(frame["event_id"], events)
                    self.assertEqual(frame["at"], events[frame["event_id"]]["at"])
                self.assertEqual(
                    len(frames[-1]["trades"]), len(run["result"]["trades"])
                )

    def test_no_future_meter_or_commitment_leakage(self):
        run = self.run_case(presets()["cloud"])
        frames = replay_data(run)["frames"]
        commitment = next(
            i for i, f in enumerate(frames) if f["title"] == "Trades committed"
        )
        meter = next(
            i for i, f in enumerate(frames) if f["title"] == "Meter outcomes revealed"
        )
        for f in frames[:commitment]:
            self.assertEqual(f["committed"], 0)
            self.assertEqual(f["settled"], 0)
        for f in frames[:meter]:
            self.assertEqual(f["readings"], {})
            self.assertEqual(f["settled"], 0)
        self.assertEqual(frames[0]["trades"], [])
        self.assertIn("4.00 kWh", frames[1]["note"])
        self.assertNotIn("actual_export", frames[1]["note"])
        self.assertEqual(frames[-1]["readings"]["S1"]["kwh"], 2)
        self.assertEqual(frames[-1]["settled"], 7)

    def test_pending_cancel_and_empty_market(self):
        for policy in ["pending", "cancel"]:
            payload = presets()["normal"]
            payload["sellers"][0]["actual_export"] = None
            payload["missing_meter_policy"] = policy
            run = self.run_case(payload)
            last = replay_data(run)["frames"][-1]
            status = "pending" if policy == "pending" else "cancelled"
            self.assertTrue(any(t["status"] == status for t in last["trades"]))
            self.assertEqual(last["settled"], 5)
        payload = presets()["normal"]
        payload["utility_cap_kwh"] = 0
        last = replay_data(self.run_case(payload))["frames"][-1]
        self.assertEqual(last["trades"], [])
        self.assertEqual(last["settled"], 0)

    def test_preview_and_html_data_escaping(self):
        preview = replay_data()
        self.assertIsNone(preview["run_id"])
        self.assertEqual(len(preview["frames"]), 1)
        run = self.run_case(presets()["normal"])
        run["run_id"] = '</script><script>alert("test")</script>'
        html = network_html(run)
        self.assertNotIn(run["run_id"], html)
        self.assertNotIn("__REPLAY_DATA__", html)
        self.assertIn("\\u003c/script>", html)
