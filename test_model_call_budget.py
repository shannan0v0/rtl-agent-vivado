"""Exercise the task deadline through the model transport, without waiting for inference."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent.model_agent import run_agent
from official_entry.runtime_config import RuntimeConfig


ROOT = Path(__file__).resolve().parent
PROMPT = (
    "A priority encoder returns the index of the most significant 1. For zero input, output zero.\n"
    "module encoder(input [3:0] data, output [1:0] index);\nendmodule"
)
RTL = "module encoder(input [3:0] data, output [1:0] index); assign index = data[3] ? 3 : 0; endmodule"


class ModelCallBudget(unittest.TestCase):
    def run_task(self, config, task_budget, replies, *, provider=False):
        now = 0.0
        observed = []
        replies = iter(replies)

        def transport(url, request_body, timeout):
            nonlocal now
            observed.append(timeout)
            code, seconds = next(replies)
            if seconds > timeout:
                now += timeout
                raise TimeoutError("fixture did not finish before client deadline")
            now += seconds
            response = {"choices": [{"finish_reason": "stop", "message": {"content": code}}]}
            return 200, {}, json.dumps(response).encode()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "prompt.txt").write_text(PROMPT, encoding="utf-8")
            # Exercise generation and interface parsing. EDA is outside this
            # scheduling test. Mark it unavailable rather than reporting a pass.
            with patch("agent.model_agent.time.monotonic", side_effect=lambda: now), patch(
                "agent.model_agent.compile_rtl", return_value={"status": "unavailable", "stages": []}
            ) as compile_rtl:
                result = run_agent(root, root / "out", config, timeout_s=task_budget,
                                   transport=transport, provider_defaults=provider)
            return observed, result, compile_rtl.call_args

    def test_slow_response_reaches_compiler_with_new_profile(self):
        config = RuntimeConfig.load(ROOT / "runtime_config.example.json")
        self.assertEqual(config.public_dict()["model_call_timeout_s"], 500)
        observed, current, compiled = self.run_task(config, 890, [(RTL, 450)])
        self.assertEqual(observed, [500])
        self.assertEqual(current["compile_status"], "unavailable")
        self.assertEqual(compiled.kwargs["timeout_s"], 120)
        legacy = replace(config, model_call_timeout_s=300)
        observed, original, compiled = self.run_task(legacy, 890, [(RTL, 450)])
        self.assertEqual(observed, [300])
        self.assertEqual(original["output_status"], "empty_transport_error")
        self.assertIsNone(compiled)

    def test_repair_uses_actual_remaining_time_with_both_check_stages(self):
        config = RuntimeConfig.load(ROOT / "runtime_config.example.json")
        wrong_interface = RTL.replace("module encoder", "module other")
        observed, result, _ = self.run_task(config, 890, [(wrong_interface, 280), (RTL, 350)])
        self.assertEqual(observed, [500, 390])  # 890 - 10 - 280 - 120 - 90
        self.assertEqual(result["request_count"], 2)

    def test_short_deadlines_and_provider_policy(self):
        config = RuntimeConfig.load(ROOT / "runtime_config.example.json")
        for deadline, provider, expected in ((600, False, 380), (60, False, 50), (890, True, 300)):
            with self.subTest(deadline=deadline, provider=provider):
                observed, _, _ = self.run_task(config, deadline, [(RTL, 1)], provider=provider)
                self.assertEqual(observed, [expected])

    def test_old_configs_and_invalid_limits(self):
        config = RuntimeConfig.load(ROOT / "runtime_config.example.json")
        raw = json.loads((ROOT / "runtime_config.example.json").read_text(encoding="utf-8"))
        raw.pop("model_call_timeout_s")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime_config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            self.assertEqual(RuntimeConfig.load(path).model_call_timeout_s, 300)
        for invalid in (0, -1, True, 12.5):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                replace(config, model_call_timeout_s=invalid).validate()


if __name__ == "__main__":
    unittest.main()
