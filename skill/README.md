# V4 skills

Only rtl_initial.md is loaded before the first attempt. It now includes a short
radix/width/encoding reminder and is NOT byte-identical to the baseline prompt.
rtl_edit.md is loaded after diagnostics for exact, model-authored edits;
rtl_generation.md and rtl_reread.md retain full repair/re-read paths. At most
three calls and the existing deadline remain. Incomplete/truncated finals and
invalid edits cannot replace the existing compiled fallback. The four templates
and all executable Agent code are unchanged from the exercised V4 source.
Public checks are bounded and may report unverified; no whole-class coverage
or hidden-test score is claimed. No external reference RTL/testbench is input.
