#!/usr/bin/env bash
# V4 source adapter. Actual asset/build binding and Linux startup remain unverified.
rtl_hip_runtime_fail() { printf 'V4 startup: %s\n' "$1" >&2; exit 78; }
rtl_prepare_hip_runtime_environment() {
  local package_dir="$1" python_bin values name value
  [[ "$package_dir" == '/opt/rtl-agent' ]] || rtl_hip_runtime_fail 'package root differs from official integrated path'
  : "${RTL_MODEL_LED_CONFIG:?runtime config must be explicit}"
  : "${RTL_MODEL_LED_ASSETS:?runtime assets must be explicit}"
  [[ "$RTL_MODEL_LED_CONFIG" == "$package_dir/runtime_config.json" && -r "$RTL_MODEL_LED_CONFIG" ]] || rtl_hip_runtime_fail 'runtime config path is absent or mismatched'
  [[ "$RTL_MODEL_LED_ASSETS" == "$package_dir/serve/runtime_assets.json" && -r "$RTL_MODEL_LED_ASSETS" ]] || rtl_hip_runtime_fail 'runtime assets path is absent or mismatched'
  python_bin="${RTL_MODEL_LED_PYTHON:-python3}"
  values="$(cd -- "$package_dir" && "$python_bin" -m serve.runtime_environment --config "$RTL_MODEL_LED_CONFIG" --assets "$RTL_MODEL_LED_ASSETS")" || rtl_hip_runtime_fail 'verified V4 runtime bindings unavailable'
  while IFS='=' read -r name value; do
    case "$name" in MODEL_NAME|LLM_BASE_URL|LD_LIBRARY_PATH|ROCM_PATH|HIP_PATH|ROCBLAS_TENSILE_LIBPATH|RTL_MODEL_LED_DEPLOY_ROOT) export "$name=$value";; *) rtl_hip_runtime_fail 'unexpected runtime environment field';; esac
  done <<< "$values"
}
