"""Regressions for the Boolean solver and delivery through the public task pipeline."""

import itertools
import json
from dataclasses import replace
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from agent.indexed_logic import solve_indexed_logic
from agent.model_agent import compose_public_input
from official_entry.adapter import OfficialEntry
from official_entry.runtime_config import RuntimeConfig
from official_entry.task_runner import process_task


HEADER = "module select_fn(input wire high, input wire low, output wire p, output wire n);"


def problem(values):
    def indices(target):
        return ", ".join(str(i) for i, value in enumerate(values) if value == target) or "none"

    return (f"Inputs (MSB first): high, low. Ones: {indices(1)}. Zeros: {indices(0)}. "
            f"Don't-cares: {indices(None)}. Minimum SOP: p. Minimum POS: n.")


def evaluate(expression, bits):
    # Interpret the emitted Verilog operators independently of the cube representation.
    if expression in ("1'b1", "1'b0"):
        return expression == "1'b1"
    terms = re.findall(r"\(([^()]*)\)", expression)
    def evaluate_term(term):
        literals = [bits[token.lstrip("~")] != token.startswith("~") for token in re.split(r" [|&] ", term)]
        return any(literals) if " | " in term else all(literals)
    values = [evaluate_term(term) for term in terms]
    return all(values) if ") & (" in expression else any(values)


class IndexedLogic(unittest.TestCase):
    def test_every_two_input_function_with_partial_rows(self):
        for cells in itertools.product((0, 1, None), repeat=4):
            p = problem(cells)
            result = solve_indexed_logic(p, compose_public_input(p, HEADER), HEADER)
            if all(value is None for value in cells):
                self.assertIsNone(result)
                continue
            expressions = re.findall(r"assign [pn] = ([^;]+);", result[0])
            self.assertEqual(len(expressions), 2)
            for i, value in enumerate(cells):
                if value is not None:
                    inputs = {"high": bool(i & 2), "low": bool(i & 1)}
                    self.assertEqual([evaluate(e, inputs) for e in expressions], [bool(value)] * 2, cells)

    def test_ambiguous_and_constrained_specs_fall_back(self):
        p = problem((1, 0, 0, None))
        for invalid in (p + " Use NAND gates only.", p.replace("Zeros: 1, 2", "Zeros: 0, 2"),
                        p.replace("high, low", "low, low"), p.replace("Don't-cares: 3", "Don't-cares: none"),
                        p.replace("Minimum POS: n", "Minimum POS: p")):
            self.assertIsNone(solve_indexed_logic(invalid, compose_public_input(invalid, HEADER), HEADER))
        extra = HEADER + " // the outputs must be registered"
        self.assertIsNone(solve_indexed_logic(p, compose_public_input(p, extra), extra))

    def test_public_adapter_delivers_the_checked_file_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = root / "input"
            task.mkdir()
            (task / "prompt.txt").write_text(problem((0, 1, None, 1)), encoding="utf-8")
            (task / "interface.txt").write_text(HEADER, encoding="utf-8")
            config = RuntimeConfig.load(Path(__file__).parent / "runtime_config.example.json")
            config = replace(config, model_lock_path=str(root / "model.lock"))
            output = root / "out"
            # Simulate only absence of Vivado. All file and adapter handling runs normally.
            with patch("agent.model_agent.compile_rtl", return_value={"status": "unavailable"}):
                result = process_task(task, output, "agent", config, transport=self.fail)
            self.assertEqual(result["request_count"], 0)
            self.assertEqual(OfficialEntry._read_outputs(output)[2:], ("success", True))
            (output / "solution.v").write_text("module tampered; endmodule", encoding="utf-8")
            self.assertEqual(OfficialEntry._read_outputs(output)[2], "invalid_output_status")

    def test_rejected_direct_candidate_falls_back_to_model(self):
        model_rtl = HEADER + "\nassign p = high ^ low;\nassign n = high ^ low;\nendmodule\n"
        response = {"choices": [{"message": {"content": model_rtl}, "finish_reason": "stop"}]}
        checks = [{"status": "fail", "reason": "diagnostic for direct candidate"},
                  {"status": "unavailable", "reason": "no further EDA checks"}]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = root / "input"
            task.mkdir()
            (task / "prompt.txt").write_text(problem((0, 1, 1, 0)), encoding="utf-8")
            (task / "interface.txt").write_text(HEADER, encoding="utf-8")
            cfg = RuntimeConfig.load(Path(__file__).parent / "runtime_config.example.json")
            cfg = replace(cfg, model_lock_path=str(root / "model.lock"))
            with patch("agent.model_agent.compile_rtl", side_effect=checks) as compiler:
                result = process_task(task, root / "out", "agent", cfg,
                                      transport=lambda *args: (200, {}, json.dumps(response).encode()))
            rtl, _, status, valid = OfficialEntry._read_outputs(root / "out")
            self.assertEqual(compiler.call_count, 2)
            self.assertEqual(result["request_count"], 1)
            self.assertEqual((status, valid, rtl.strip()), ("success", True, model_rtl.strip()))


if __name__ == "__main__":
    unittest.main()
