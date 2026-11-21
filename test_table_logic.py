import unittest

from agent.model_agent import compose_public_input
from agent.table_logic import solve_table_logic


def solve(prompt, interface):
    return solve_table_logic(prompt, compose_public_input(prompt, interface), interface)


class ScalarTableLogic(unittest.TestCase):
    HEADER = "module gate(input left, input right, output flag);"
    TRUTH = """Combinational truth table:
flag | right | left
  0  | 0 | 0
  1  | 1 | 0
  1  | 0 | 1
  0  | 1 | 1
"""
    KMAP = """Karnaugh map.
  right
left    1  0
  1  | 0 | 1 |
  0  | 1 | 0 |
"""

    def test_reordered_truth_columns_and_reversed_map_labels_agree(self):
        truth, proof = solve(self.TRUTH, self.HEADER)
        mapped, mapping = solve(self.KMAP, self.HEADER)
        self.assertEqual(truth, mapped)
        self.assertIn("assign flag = (~left & right) | (left & ~right);", truth)
        self.assertEqual(proof["checked_output_cells"], mapping["checked_output_cells"])
        self.assertEqual(mapping["input_rows"], 4)

    def test_constant_columns_multiple_outputs(self):
        ports = "module both(input pin, output untouched, output active);"
        rtl, proof = solve("Truth table:\npin|active|untouched\n0|1|0\n1|1|0", ports)
        self.assertIn("assign active = 1'b1;", rtl)
        self.assertIn("assign untouched = 1'b0;", rtl)
        self.assertEqual(proof["checked_output_cells"], 4)

    def test_explicit_dont_care_cells(self):
        example = self.KMAP.replace(" | 0 | 1 |", " | d | 1 |")
        self.assertIsNone(solve(example, self.HEADER))
        explicit = example.replace("Karnaugh map.", "Karnaugh map. d is don't-care, "
                                   "which means you may choose to output whatever value is convenient.")
        self.assertEqual(solve(explicit, self.HEADER)[1]["checked_output_cells"], 3)
        self.assertIsNone(solve(explicit.replace("| d |", "| x |"), self.HEADER))

    def test_ambiguous_structural_or_extra_content_defers_to_agent(self):
        for source in (
            self.TRUTH + "Use only NAND gates.",
            self.TRUTH.replace("truth table:", "registered truth table:"),
            self.TRUTH + "0|1",
            self.TRUTH.replace("1  | 1 | 0", "1  | 0 | 0"),
            self.KMAP + "Latch the result at the rising edge.",
            self.KMAP.replace("1  0", "1  1"),
            self.KMAP.replace("  0  | 1 | 0 |", ""),
        ):
            with self.subTest(source=source):
                self.assertIsNone(solve(source, self.HEADER))
        for header in (
            self.HEADER.replace("input left", "input [1:0] left"),
            self.HEADER.replace("input left", "input [0:0] left"),
            self.HEADER + " always @(posedge left) flag <= right; endmodule",
        ):
            with self.subTest(header=header):
                self.assertIsNone(solve(self.TRUTH, header))


class WaveformLogic(unittest.TestCase):
    HEADER = "module pair(input a, input b, output parity, output carry);"
    WAVE = """Combinational waveform:
time carry b parity a
0ns  0 0 0 0
2ns  0 0 1 1
5ns  1 1 0 1
9ns  0 1 1 0
10ns 0 0 0 0
"""

    def test_permuted_waveform_columns_multiple_outputs_and_repeated_sample(self):
        rtl, proof = solve(self.WAVE, self.HEADER)
        self.assertIn("assign parity = (~a & b) | (a & ~b);", rtl)
        self.assertIn("assign carry = (a & b);", rtl)
        self.assertEqual(proof["table_format"], "waveform")
        self.assertEqual(proof["observed_samples"], 5)
        self.assertEqual(proof["input_rows"], 4)
        self.assertEqual(proof["checked_output_cells"], 8)

    def test_incomplete_uncertain_and_sequential_observations_defer(self):
        for source in (
            self.WAVE.replace("2ns  0 0 1 1\n", ""),
            self.WAVE.replace("10ns 0 0 0 0", "10ns 0 0 1 0"),
            self.WAVE.replace("5ns  1", "5ns  x"),
            self.WAVE.replace("carry b parity a", "carry a parity a"),
            self.WAVE.replace("5ns", "1ns"),
            self.WAVE.replace("5ns", "5ps"),
            self.WAVE.replace("Combinational", "Simulation"),
            self.WAVE.replace("Combinational", "Sequential"),
            self.WAVE + "Implement with NAND gates only.",
        ):
            with self.subTest(source=source):
                self.assertIsNone(solve(source, self.HEADER))


class PackedTableLogic(unittest.TestCase):
    HEADER = "module indexed(input wire [1:0] bus, output y);"
    KMAP = """The module should implement the function y shown in the Karnaugh map below.
     bus[0]
bus[1] 1 0
1 | 1 | 0
0 | 1 | 0"""

    def test_packed_indices_determine_the_output_independently_of_column_order(self):
        rtl, proof = solve(self.KMAP, self.HEADER)
        self.assertIn("input wire [1:0] bus", rtl)
        self.assertIn("assign y = (bus[0]);", rtl)
        self.assertEqual(proof["input_order"], ["bus[1]", "bus[0]"])
        self.assertEqual(proof["checked_output_cells"], 4)

    def test_wrong_output_out_of_range_or_overlapping_indices_defer(self):
        for source in (
            self.KMAP.replace("function y", "function z"),
            self.KMAP.replace("bus[0]", "bus[2]"),
            self.KMAP.replace("bus[0]", "bus[1]"),
        ):
            with self.subTest(source=source):
                self.assertIsNone(solve(source, self.HEADER))
        self.assertIsNone(solve(self.KMAP, self.HEADER.replace("[1:0]", "[0:1]")))

    def test_individual_bus_bits_in_truth_table_with_legal_dollar_in_name(self):
        header = self.HEADER.replace("bus", "data$in")
        table = "Truth table:\ndata$in[0]|y|data$in[1]\n0|0|0\n1|0|0\n0|1|1\n1|1|1"
        rtl, proof = solve(table, header)
        self.assertIn("assign y = (data$in[1]);", rtl)
        self.assertEqual(proof["checked_output_cells"], 4)


if __name__ == "__main__":
    unittest.main()
