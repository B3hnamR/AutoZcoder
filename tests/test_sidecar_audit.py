"""Offline sidecar protocol fixtures; never imports the claim/network stack."""
import ast
from contextlib import nullcontext
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


def load_captcha_token(run_sidecar):
    source = Path(__file__).resolve().parents[1] / "generator" / "claim_start.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "_captcha_token")
    namespace = {
        "os": os, "re": re, "APP_VERSION": "fixture-version",
        "_captcha_config": lambda log: {"sceneId": "fixture", "region": "sgp", "prefix": "fixture"},
        "_find_mint_exe": lambda: "",
        "direct_requests": lambda force_direct: nullcontext(),
        "_proxies": lambda: {},
        "_run_sidecar": run_sidecar,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    return namespace["_captcha_token"]


class SidecarProtocolTests(unittest.TestCase):
    def setUp(self):
        self.run_sidecar = Mock()
        self.token = load_captcha_token(self.run_sidecar)
        self.param = "dummy-offline-token-not-a-real-captcha"
        self.logs = []

    def invoke(self, stdout, returncode=0, stderr=""):
        self.run_sidecar.reset_mock()
        self.run_sidecar.return_value = SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr)
        with patch.dict(os.environ, {"ZAI_CAPTCHA_CMD": "fixture-sidecar"}, clear=True):
            result = self.token(self.logs.append)
        self.run_sidecar.assert_called_once()
        argv, env = self.run_sidecar.call_args.args
        self.assertEqual(argv, ["fixture-sidecar"])
        self.assertEqual(env["ZAI_CAPTCHA_SCENE"], "fixture")
        self.assertEqual(env["ZAI_APP_VERSION"], "fixture-version")
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
        for stdout, returncode in (
            (f"{self.param} sgp\n", 1),
            (f"debug log\n{self.param} sgp\n", 0),
        ):
            with self.subTest(returncode=returncode):
                self.assertEqual(self.invoke(stdout, returncode, f"debug {self.param}"), ("", ""))
                self.assertNotIn(self.param, " ".join(self.logs))

    def test_timeout_and_missing_executable_fail_closed(self):
        for error in (subprocess.TimeoutExpired("fixture", 120),
                      TimeoutError("sidecar deadline exceeded"),
                      FileNotFoundError("fixture"),
                      ValueError("sidecar output limit exceeded")):
            with self.subTest(error=type(error).__name__):
                self.run_sidecar.reset_mock()
                self.run_sidecar.side_effect = error
                with patch.dict(os.environ, {"ZAI_CAPTCHA_CMD": "fixture-sidecar"}, clear=True):
                    self.assertEqual(self.token(self.logs.append), ("", ""))
                self.run_sidecar.assert_called_once()

    def test_explicit_param_override_does_not_launch_child(self):
        with patch.dict(os.environ, {"ZAI_CAPTCHA_PARAM": self.param}, clear=True):
            self.assertEqual(self.token(self.logs.append), (self.param, "sgp"))
            self.run_sidecar.assert_not_called()


if __name__ == "__main__":
    unittest.main()
