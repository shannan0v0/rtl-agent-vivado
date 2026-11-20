"""Run one RTL task with a user-selected Chat Completions provider."""

import argparse
from functools import partial
from getpass import getpass
import ipaddress
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from agent.model_agent import http_post_once, run_agent
from official_entry.runtime_config import RuntimeConfig


def base_url(value: str) -> str:
    parts = urlsplit(value)
    host = parts.hostname
    if not host or parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise argparse.ArgumentTypeError("Use a base URL without credentials, query or fragment.")
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        raise argparse.ArgumentTypeError("Use HTTPS for external endpoints or HTTP on loopback.")
    if parts.path.rstrip("/").endswith("/chat/completions"):
        raise argparse.ArgumentTypeError("Enter the API base URL without /chat/completions.")
    return value.rstrip("/")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_dir", type=Path, help="Directory containing UTF-8 prompt.txt")
    parser.add_argument("out_dir", type=Path, help="New directory for RTL and logs")
    parser.add_argument("--base-url", type=base_url, required=True)
    parser.add_argument("--model", required=True, help="Model ID from your provider")
    args = parser.parse_args()
    model = args.model.strip()
    if not model or not (args.task_dir / "prompt.txt").is_file():
        parser.error("Provide a model ID and a task directory containing prompt.txt.")
    key = os.environ.pop("RTL_API_KEY", "").strip()
    if not key and sys.stdin.isatty():
        key = getpass("Provider API key: ").strip()
    if not key or not key.isascii() or any(not (33 <= ord(c) <= 126) for c in key):
        parser.error("Provide a valid API key at the prompt or via RTL_API_KEY.")
    try:
        args.out_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error("Choose a new output directory to preserve earlier results.")

    config = RuntimeConfig(model_base_url=args.base_url, model_name=model)
    result = run_agent(
        args.task_dir.resolve(), args.out_dir.resolve(), config,
        timeout_s=config.request_budget_s, transport=partial(http_post_once, api_key=key),
        provider_defaults=True,
    )
    print(json.dumps({
        "solution": str(args.out_dir / "solution.v"),
        "model_calls": result["request_count"],
        "compile_status": result.get("compile_status", "not_run"),
        "stop_reason": result["stop_reason"],
        "http_status": result.get("http_status"),
        "detail": result.get("response_error") or result.get("reason"),
    }, ensure_ascii=False))
    return 0 if (args.out_dir / "solution.v").stat().st_size else 1


if __name__ == "__main__":
    raise SystemExit(main())
