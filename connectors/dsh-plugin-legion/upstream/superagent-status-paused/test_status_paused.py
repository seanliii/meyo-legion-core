"""Offline regression for the upstream CLI; set SUPERAGENT_SOURCE_ROOT to a controlled checkout."""
import io
import json
import os
from pathlib import Path
import runpy
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch


class StatusPausedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = os.environ.get("SUPERAGENT_SOURCE_ROOT")
        if not root:
            raise RuntimeError("Set SUPERAGENT_SOURCE_ROOT to a controlled upstream checkout, not a running installation")
        script = Path(root) / "scripts" / "dsh-superagent"
        cls.status_function = staticmethod(runpy.run_path(str(script), run_name="offline_regression")["status"])

    def observe(self, payload, *, pids=(123,), manually_stopped=False, unreachable=False):
        status = self.status_function
        output = io.StringIO()
        response = io.BytesIO(json.dumps(payload).encode())
        with patch.dict(status.__globals__,
                        processes=lambda profile: list(pids),
                        stopped=lambda profile: manually_stopped):
            with patch("subprocess.run", side_effect=AssertionError("No subprocess is permitted")):
                with patch("urllib.request.urlopen",
                           side_effect=OSError("offline fixture") if unreachable else None,
                           return_value=response) as transport:
                    with redirect_stdout(output):
                        code = status(SimpleNamespace(profile="offline-fixture", url="https://offline.invalid"))
                    self.assertEqual(transport.call_count, 1 if pids else 0)
        return json.loads(output.getvalue()), code

    @staticmethod
    def healthy(**extra):
        return {
            "health": {"runtime": {"state": "ready", "detail": "Offline health fixture"}},
            "setup": {"problem": None, "feishuProblem": None},
            **extra,
        }

    def test_boolean_pause_is_preserved_without_changing_healthy_exit(self):
        results = []
        for paused in (False, True):
            with self.subTest(paused=paused):
                value, code = self.observe(self.healthy(paused=paused))
                self.assertIn("paused", value)
                self.assertIs(value["paused"], paused)
                self.assertEqual(code, 0)
                results.append(value)
        if len(results) == 2:
            self.assertNotEqual(results[0], results[1])
            self.assertEqual({k: v for k, v in results[0].items() if k != "paused"},
                             {k: v for k, v in results[1].items() if k != "paused"})

    def test_missing_or_non_boolean_pause_is_unknown_not_false(self):
        cases = [{}, *({"paused": value} for value in (None, 0, 1, "", "false", [], {}))]
        for fields in cases:
            with self.subTest(fields=fields):
                value, code = self.observe(self.healthy(**fields))
                self.assertIn("paused", value)
                self.assertIsNone(value["paused"])
                self.assertEqual(code, 0)

    def test_setup_reason_and_exit_three_are_preserved(self):
        problem = {"code": "repositoryUnset", "place": "github",
                   "detail": "The GitHub Source names no repository."}
        source = self.healthy(paused=False)
        source["health"]["runtime"]["state"] = "paused"
        source["setup"]["problem"] = problem
        value, code = self.observe(source)
        self.assertEqual(value["setup"], problem)
        self.assertEqual(value["health"]["runtime"], "paused")
        self.assertEqual(code, 3)

    def test_component_and_runtime_error_exits_are_unchanged(self):
        for component, expected in (("runtime", 1), ("github", 2)):
            with self.subTest(component=component):
                source = self.healthy(paused=True)
                source["health"][component] = {"state": "error", "detail": "Offline component error"}
                value, code = self.observe(source)
                self.assertEqual(value["errors"][component], "Offline component error")
                self.assertEqual(code, expected)

    def test_unreachable_or_stopped_has_unknown_pause_and_exit_one(self):
        for options in ({"unreachable": True}, {"pids": (), "manually_stopped": True}):
            with self.subTest(options=options):
                value, code = self.observe(self.healthy(paused=True), **options)
                self.assertIn("paused", value)
                self.assertIsNone(value["paused"])
                self.assertEqual(code, 1)
                self.assertIs(value["manually_stopped"], options.get("manually_stopped", False))

    def test_processing_pause_does_not_replace_manual_stop_marker(self):
        value, code = self.observe(self.healthy(paused=True), manually_stopped=False)
        self.assertIn("paused", value)
        self.assertIs(value["paused"], True)
        self.assertIs(value["manually_stopped"], False)
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
