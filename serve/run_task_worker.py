"""Internal supervised child. It deliberately calls process_task directly."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from official_entry.runtime_config import RuntimeConfig
from official_entry.task_runner import process_task


def main() -> int:
    parser = argparse.ArgumentParser(description="Internal task worker")
    parser.add_argument("--mode", required=True, choices=("agent", "baseline"))
    parser.add_argument("--task-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--timeout-s", required=True, type=float)
    args = parser.parse_args()
    config = RuntimeConfig.load(args.config)
    result = process_task(
        args.task_dir,
        args.out_dir,
        args.mode,
        config,
        timeout_s=args.timeout_s,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
