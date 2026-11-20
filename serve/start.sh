#!/usr/bin/env bash
set -euo pipefail

package_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
# The official container fallback invokes this entry without the API wrapper.
source "$package_dir/serve/hip_runtime_env.sh"
rtl_prepare_hip_runtime_environment "$package_dir"
assets="${RTL_MODEL_LED_ASSETS:-$package_dir/serve/runtime_assets.json}"
config="${RTL_MODEL_LED_CONFIG:-$package_dir/runtime_config.json}"
python_bin="${RTL_MODEL_LED_PYTHON:-python3}"

if [[ ! -r "$assets" || ! -r "$config" ]]; then
  echo "runtime asset manifest or runtime config is missing" >&2
  exit 78
fi
cd -- "$package_dir"
"$python_bin" - "$config" "$assets" <<'PY'
import json
import os
import sys

config = json.load(open(sys.argv[1], encoding="utf-8"))
assets = json.load(open(sys.argv[2], encoding="utf-8"))
model = assets.get("llama_cpp", {})
if config.get("model_base_url", "").rstrip("/") != "http://127.0.0.1:18080/v1":
    raise SystemExit("runtime config must use the shared local recording endpoint")
if config.get("model_name") != model.get("alias") or os.environ.get("MODEL_NAME") != model.get("alias"):
    raise SystemExit("agent, baseline environment, and asset alias must match")
if os.environ.get("LLM_BASE_URL", "").rstrip("/") != config.get("model_base_url", "").rstrip("/"):
    raise SystemExit("baseline environment and agent config must use the same model URL")
if config.get("ctx_size") != model.get("context_tokens"):
    raise SystemExit("agent and model context declarations must match")
if (config.get("max_tokens"), config.get("temperature"), config.get("top_p")) != (8192, 0.0, 1.0):
    raise SystemExit("agent must retain the team limits: max_tokens=8192, temperature=0, top_p=1")
PY
exec "$python_bin" -m serve.service_manager start --assets "$assets"
