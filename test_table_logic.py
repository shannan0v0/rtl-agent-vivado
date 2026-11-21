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


if __name__ == "__main__":
    unittest.main()
