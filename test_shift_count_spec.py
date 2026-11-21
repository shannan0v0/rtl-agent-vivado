import itertools
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent.shift_count_spec import solve_shift_count


def description(order="most", direction="down", edge="rising", priority="gate and tick are mutually exclusive"):
    op = "decremented" if direction == "down" else "incremented"
    return f"""I would like you to implement a module named Word with the following interface.
All input and output ports are one bit unless otherwise specified.
 - input pulse
 - input gate
 - input tick
 - input stream
 - output value (3 bits)
The module should implement a three-bit shift register that also acts as a {direction} counter.
stream is shifted in {order}-significant-bit first when gate is 1.
The number currently in the shift register is {op} when tick is 1.
{priority}. Assume all sequential logic is triggered on the {edge} edge of pulse.
Otherwise the register holds its value."""


class ShiftCount(unittest.TestCase):
    def test_explicit_operation_order_and_scalar_width(self):
        prompt = (description("least", "up", "falling", "tick takes priority over gate")
                  .replace("The module should implement a three-bit shift register that also acts as a", "Build a one-bit shift register with a")
                  .replace("(3 bits)", "(1 bit)")
                  .replace("stream is shifted in least-significant-bit first when gate is 1", "When gate is high, shift in stream LSB-first")
                  .replace("The number currently in the shift register is incremented when tick is 1", "When tick is high, increment value by one")
                  .replace("Assume all sequential logic is triggered", "Update value"))
        rtl, proof = solve_shift_count(prompt, prompt)
        self.assertEqual((proof["word_width"], proof["edge"], proof["priority"]), (1, "negedge", "tick"))
        self.assertIn("value <= stream;", rtl)

    def test_refuses_ambiguity_and_unsupported_behavior(self):
        prompt = description()
        for other in [
            prompt.replace("three-bit", "four-bit"), prompt.replace("gate is 1", "gate is 0"),
            prompt.replace("gate and tick are mutually exclusive.", ""),
            prompt + " It asynchronously resets to zero.", prompt + " Swap the two highest bits.",
            prompt.replace("stream is shifted", "pulse is shifted"), prompt.replace("when tick", "when gate"),
            prompt.replace("decremented", "incremented"), prompt.replace("rising edge", "either edge"),
            prompt.replace("gate and tick", "gate and pulse"), prompt.replace("edge of pulse", "edge of missing"),
        ]:
            with self.subTest(other=other):
                self.assertIsNone(solve_shift_count(other, other))

    @unittest.skipUnless(shutil.which("iverilog") and shutil.which("vvp"), "requires Icarus Verilog")
    def test_all_one_step_transitions(self):
        cases = [("most", "up", "falling", "tick"), ("least", "down", "rising", "gate")]
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for order, direction, edge, first in cases:
                other = "gate" if first == "tick" else "tick"
                prompt = description(order, direction, edge, f"{first} takes priority over {other}")
                rtl, proof = solve_shift_count(prompt, prompt)
                self.assertEqual(proof["priority"], first)
                # Directly seed every old state so both wrap boundaries, idle,
                # simultaneous enables and the inactive clock edge are covered.
                inactive = int(edge == "falling")
                steps = []
                for old, enable, count, data in itertools.product(range(8), range(2), range(2), range(2)):
                    expected = old
                    if enable and (not count or first == "gate"):
                        expected = ((old * 2 + data) % 8 if order == "most" else old // 2 + data * 4)
                    elif count:
                        expected = (old + (1 if direction == "up" else -1)) % 8
                    steps.append(f"pulse={inactive}; #1; dut.value={old}; gate={enable}; tick={count}; stream={data}; "
                                 f"#1; if (value !== 3'd{old}) $fatal; pulse={1 - inactive}; #1; "
                                 f"if (value !== 3'd{expected}) $fatal;")
                bench = ("module tb; reg pulse,gate,tick,stream; wire [2:0] value; "
                         "Word dut(pulse,gate,tick,stream,value); initial begin\n" +
                         "\n".join(steps) + '\n$display("PASS"); $finish; end endmodule\n')
                (folder / "test.v").write_text(rtl + bench, encoding="utf-8")
                subprocess.run(["iverilog", "-g2012", "-s", "tb", "-o", "sim", "test.v"], cwd=folder,
                               check=True, capture_output=True, timeout=10)
                result = subprocess.run(["vvp", "sim"], cwd=folder, check=True, capture_output=True, timeout=10)
                self.assertIn(b"PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
