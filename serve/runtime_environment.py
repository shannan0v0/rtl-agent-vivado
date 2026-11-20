"""V4 binding check only; reuses original service_manager asset verification."""
from __future__ import annotations
import argparse,json,os
from pathlib import Path
from official_entry.runtime_config import RuntimeConfig
from serve.service_manager import validate_runtime_assets

ALIAS='Qwen3.8-27B-UD-Q5_K_M'
BINARY_SHA='290708d22f8b495d36b8aedd0afb0cdc081c415548f855e6a950a75dd742ad97'
def bindings(config, assets, *, verify_assets=validate_runtime_assets):
    config.validate()
    cpp=assets['llama_cpp'];proxy=assets['proxy'];env=assets['deployment_environment']
    if config.model_name!=ALIAS or cpp['alias']!=ALIAS:
        raise ValueError('V4 model alias mismatch')
    if config.model_base_url.rstrip('/')!='http://127.0.0.1:18080/v1':
        raise ValueError('V4 model recording endpoint mismatch')
    if proxy['listen_host']!='127.0.0.1' or proxy['listen_port']!=18080 or proxy['upstream_base_url']!='http://127.0.0.1:18081/v1':
        raise ValueError('V4 proxy listen/upstream seam mismatch')
    if config.ctx_size!=32768 or cpp['context_tokens']!=32768:
        raise ValueError('V4 context mismatch')
    if (config.max_tokens,config.temperature,config.top_p)!=(8192,0.0,1.0):
        raise ValueError('V4 Agent generation limits changed')
    if cpp['binary_sha256']!=BINARY_SHA or cpp['binary_path']!='/opt/llamacpp/llama-server':
        raise ValueError('V4 pinned engine identity mismatch')
    # Original source-build identity checks remain for that profile. The
    # preinstalled profile verifies the pinned complete native file inventory.
    verify_assets(assets)
    sdk=Path(env['sdk_root']);lib=Path(env['sdk_lib_dir'])
    if not sdk.is_absolute() or not lib.is_absolute() or not lib.is_relative_to(sdk):
        raise ValueError('SDK library directory is outside configured SDK')
    paths=env['library_path'].split(':')
    for path in [str(sdk),str(lib),env['tensile_libpath'],*paths]:
        if not path or any(x in path for x in ('\n','\r','\t')) or not Path(path).is_absolute() or not Path(path).is_dir():
            raise ValueError('runtime library binding is absent or invalid')
    for n in ['libamdhip64.so.7','libhipblas.so.3','librocblas.so.5']:
        if not (lib/n).is_file():raise ValueError('required HIP SDK library is absent: '+n)
    if os.environ.get('HSA_OVERRIDE_GFX_VERSION'):
        raise ValueError('HSA gfx override is not allowed')
    return {'MODEL_NAME':ALIAS,'LLM_BASE_URL':config.model_base_url,'LD_LIBRARY_PATH':env['library_path'],'ROCM_PATH':str(sdk),'HIP_PATH':str(sdk),'ROCBLAS_TENSILE_LIBPATH':env['tensile_libpath'],'RTL_MODEL_LED_DEPLOY_ROOT':'/opt/llamacpp'}
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--assets',required=True);args=parser.parse_args()
    cfg=RuntimeConfig.load(args.config);assets=json.loads(Path(args.assets).read_text(encoding='utf-8'))
    values=bindings(cfg,assets)
    for k,v in values.items():
        if any(x in v for x in ('\n','\r')):raise ValueError('invalid environment line')
        print(k+'='+v)
if __name__=='__main__':main()
