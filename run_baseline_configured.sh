#!/usr/bin/env bash
set -euo pipefail
if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "usage: run_baseline_configured.sh <task_dir> <out_dir> [rtl|hls]" >&2
  exit 2
fi

package_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
config="${RTL_MODEL_LED_CONFIG:-$package_dir/runtime_config.json}"
python_bin="${RTL_MODEL_LED_PYTHON:-python3}"
values="$("$python_bin" -c 'import json,sys; c=json.load(open(sys.argv[1],encoding="utf-8")); print(c["model_base_url"]+"\t"+c["model_name"])' "$config")"
IFS=$'\t' read -r model_base_url model_name <<< "$values"
if [[ -z "$model_base_url" || -z "$model_name" ]]; then
  echo "runtime config must include model_base_url and model_name" >&2
  exit 2
fi

# The fixed official entry reads the same endpoint and model alias as the HTTP
# observer. Its files and sampling constants remain byte-for-byte unchanged.
export LLM_BASE_URL="$model_base_url"
export MODEL_NAME="$model_name"
exec "$package_dir/run_baseline.sh" "$1" "$2" "${3:-rtl}"
