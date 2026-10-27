"""Visitor flows use actual isolated Nandatown workers."""

import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from src.energy_exchange.interactive import presets
from src.energy_exchange.streamlit_ui import BusyError, RunGate

APP = Path(__file__).resolve().parents[1] / "streamlit_app.py"


class StreamlitTests(unittest.TestCase):
    def app(self):
        app = AppTest.from_file(str(APP), default_timeout=30).run()
        self.assertFalse(app.exception)
        return app

    def run_simulation(self, app):
        next(b for b in app.button if b.label == "Run simulation").click().run()
        self.assertFalse(app.exception)
        self.assertFalse(app.error)
        return app.session_state.latest

    def test_drafts_budget_and_session_isolation(self):
        app = self.app()
        self.assertEqual(len(app.metric), 0)
        first = self.run_simulation(app)
        self.assertEqual(first["result"]["settled_kwh"], 9)
        app.number_input(key="B2_budget").set_value(0.0).run()
        self.assertTrue(any("unrun changes" in w.value for w in app.warning))
        self.assertEqual(app.session_state.latest["run_id"], first["run_id"])
        second = self.run_simulation(app)
        self.assertEqual(second["result"]["settled_kwh"], 5)
        self.assertNotEqual(second["run_id"], first["run_id"])
        self.assertEqual(app.session_state.previous["run_id"], first["run_id"])
        other = self.app()
        self.assertEqual(len(other.metric), 0)
        self.assertEqual(other.number_input(key="B2_budget").value, 40)

    def test_presets_reset_and_missing_meters(self):
        app = self.app()
        app.selectbox(key="preset").select("cloud").run()
        self.assertEqual(self.run_simulation(app)["result"]["settled_kwh"], 7)
        app.number_input(key="S1_actual_export").set_value(None).run()
        pending = self.run_simulation(app)
        self.assertGreater(pending["result"]["pending_payment"], 0)
        self.assertIsNone(pending["metrics"]["buyer_savings"])
        app.selectbox(key="missing_meter_policy").select("cancel").run()
        self.assertEqual(self.run_simulation(app)["result"]["pending_payment"], 0)
        app.button[0].click().run()
        self.assertEqual(app.number_input(key="S1_actual_export").value, 2)
        self.assertEqual(app.selectbox(key="missing_meter_policy").value, "pending")

    def test_replay_and_downloads_do_not_execute_again(self):
        app = self.app()
        result = self.run_simulation(app)
        app.run()
        self.assertEqual(app.session_state.latest["run_id"], result["run_id"])
        self.assertEqual(len(app.get("iframe")), 1)
        self.assertEqual(len(app.sidebar.number_input), 26)
        self.assertEqual(len(app.get("download_button")), 6)
        self.assertNotIn("/Users/", str(result))

    def test_failure_keeps_last_result(self):
        app = self.app()
        old = self.run_simulation(app)
        with patch(
            "src.energy_exchange.streamlit_ui.RunGate.run", side_effect=TimeoutError
        ):
            app.button[1].click().run()
        self.assertFalse(app.exception)
        self.assertIn("timed out", app.error[0].value)
        self.assertEqual(app.session_state.latest["run_id"], old["run_id"])

    def test_shared_gate_limits_and_releases_after_failure(self):
        gate = RunGate()
        gate.active = 2
        with self.assertRaises(BusyError):
            gate.run(presets()["normal"])
        gate.active = 0

        async def fail(_):
            raise TimeoutError

        with (
            patch("src.energy_exchange.streamlit_ui.run_worker", fail),
            self.assertRaises(TimeoutError),
        ):
            gate.run(presets()["normal"])
        self.assertEqual(gate.active, 0)
        gate.recent.extend([gate.recent[0]] * 59)
        with self.assertRaises(BusyError):
            gate.run(presets()["normal"])
