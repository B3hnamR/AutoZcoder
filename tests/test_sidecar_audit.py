"""Offline sidecar protocol fixtures; never imports the claim/network stack."""
import ast
import os
from pathlib import Path
import re
import subprocess
import unittest
from unittest.mock import patch


def load_captcha_token():
    source = Path(__file__).resolve().parents[1] / "generator" / "claim_start.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "_captcha_token")
    namespace = {
        "os": os, "re": re, "APP_VERSION": "fixture-version",
        "_captcha_config": lambda log: {"sceneId": "fixture", "region": "sgp", "prefix": "fixture"},
        "_find_mint_exe": lambda: "",
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    return namespace["_captcha_token"]


class SidecarProtocolTests(unittest.TestCase):
    def setUp(self):
        self.token = load_captcha_token()
        self.param = "dummy-offline-token-not-a-real-captcha"
        self.logs = []

    def invoke(self, stdout, returncode=0, stderr=""):
        child = subprocess.CompletedProcess(["fixture-sidecar"], returncode, stdout, stderr)
        with patch.dict(os.environ, {"ZAI_CAPTCHA_CMD": "fixture-sidecar"}, clear=True):
            with patch("subprocess.run", return_value=child) as run:
                result = self.token(self.logs.append)
        self.assertEqual(run.call_args.kwargs["timeout"], 120)
        self.assertEqual(run.call_args.kwargs["env"]["ZAI_CAPTCHA_SCENE"], "fixture")
        return result

    def test_accepts_exact_record_with_optional_line_ending(self):
        for ending in ("", "\n", "\r\n"):
            with self.subTest(ending=repr(ending)):
                self.assertEqual(self.invoke(f"{self.param} sgp{ending}"), (self.param, "sgp"))

    def test_nonzero_child_never_supplies_a_token(self):
        for rc in (1, 2, -9):
            with self.subTest(rc=rc):
                self.assertEqual(self.invoke(f"{self.param} sgp\n", rc), ("", ""))

    def test_rejects_missing_extra_or_noisy_fields(self):
        for stdout in (
            "", self.param, "short sgp\n", f"{self.param} sgp extra\n",
            f"debug log\n{self.param} sgp\n", f"{self.param} sgp\n\n",
            f" {self.param} sgp\n", f"{self.param}\tsgp\n",
            f"{self.param}  sgp\n", f"{self.param} region/invalid\n",
        ):
            with self.subTest(stdout=stdout):
                self.assertEqual(self.invoke(stdout), ("", ""))

    def test_failure_logs_do_not_leak_token_output(self):
        self.invoke(f"{self.param} sgp\n", 1, f"debug {self.param}")
        self.assertNotIn(self.param, " ".join(self.logs))

    def test_timeout_and_missing_executable_fail_closed(self):
        for error in (subprocess.TimeoutExpired("fixture", 120), FileNotFoundError("fixture")):
            with self.subTest(error=type(error).__name__):
                with patch.dict(os.environ, {"ZAI_CAPTCHA_CMD": "fixture-sidecar"}, clear=True):
                    with patch("subprocess.run", side_effect=error):
                        self.assertEqual(self.token(self.logs.append), ("", ""))

    def test_explicit_param_override_does_not_launch_child(self):
        with patch.dict(os.environ, {"ZAI_CAPTCHA_PARAM": self.param}, clear=True):
            with patch("subprocess.run") as run:
                self.assertEqual(self.token(self.logs.append), (self.param, "sgp"))
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
