#!/usr/bin/env bash
set -euo pipefail

package_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
config="${RTL_MODEL_LED_CONFIG:-$package_dir/runtime_config.json}"
assets="${RTL_MODEL_LED_ASSETS:-$package_dir/serve/runtime_assets.json}"
python_bin="${RTL_MODEL_LED_PYTHON:-python3}"
if [[ ! -r "$config" || ! -r "$assets" ]]; then
  echo "runtime config or verified asset manifest is missing" >&2
  exit 78
fi
cd -- "$package_dir"
"$package_dir/serve/start.sh"
"$python_bin" -m serve.service_manager check --assets "$assets"
exec "$python_bin" -m official_entry.server --config "$config"
