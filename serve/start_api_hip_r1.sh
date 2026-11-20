#!/usr/bin/env bash
set -euo pipefail

package_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
source "$package_dir/serve/hip_runtime_env.sh"
rtl_prepare_hip_runtime_environment "$package_dir"
cd -- "$package_dir"
exec "$package_dir/serve/start_api.sh"
