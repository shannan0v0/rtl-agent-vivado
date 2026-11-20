#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 2 ]]; then
  echo "usage: run.sh <task_dir> <out_dir>" >&2
  exit 2
fi

# Preserve the caller's working directory. The Python CLI resolves relative
# task, output, and config paths before it starts the supervised worker.
package_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
config="${RTL_MODEL_LED_CONFIG:-$package_dir/runtime_config.json}"
python_bin="${RTL_MODEL_LED_PYTHON:-python3}"
resolve_path() {
  "$python_bin" -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$1"
}
task_dir="$(resolve_path "$1")"
out_dir="$(resolve_path "$2")"
config="$(resolve_path "$config")"
cd -- "$package_dir"
exec "$python_bin" -m serve.run_task_dir \
  --mode agent \
  --task-dir "$task_dir" \
  --out-dir "$out_dir" \
  --config "$config"
