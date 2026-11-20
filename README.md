# RTL Agent · 使用 Vivado 检查与修复生成的 Verilog

输入描述电路行为和端口的文本，Agent 调用本地 Qwen 27B 生成 RTL，用 Vivado 检查编译和设计展开，并将诊断或公开规格检查发现的反例反馈给模型。每题最多调用模型三次，可保留此前通过编译的候选版本。结果以 Verilog 文件和运行日志输出。

## 环境要求

| 项目 | 当前版本使用的环境 |
| --- | --- |
| 操作系统 | Linux x86_64，Bash、`setsid`、Git，且能写入 `/opt/rtl-agent` 和 `/workspace` |
| GPU 与推理引擎 | AMD Radeon PRO W7900（gfx1100），原项目使用单卡、32 GB 显存预算。启动脚本要求 `/opt/llamacpp` 下的指定 llama.cpp HIP 原生包及匹配的 HIP SDK、Tensile 库 |
| 模型 | Qwen3.8-27B-UD-Q5_K_M，GGUF 权重约 19.77 GB，另需任务输出空间 |
| Python 与 EDA | Python 3.14，表格辅助求解使用 SymPy 1.14.0。RTL 编译检查使用 Vivado 2026.1 的 `xvlog`、`xelab` 和有效许可证 |
| 服务 | 命令行任务使用本机推理服务；另开 HTTP 任务接口时需设置 `FPGACHINA_TOKEN` |

**开始前先确认引擎来源。** 原项目使用赛事 Linux 实例预装的推理栈。当前脚本会核对二进制及库文件的 SHA256、固定路径和模型版本。请先从原平台或资产维护方取得匹配的运行包。GitHub 提供源码、配置示例和校验清单；GGUF 权重、Vivado 许可证和平台原生包需分别准备。目前支持本地模型，普通 PC 和第三方模型 API 尚无经验证的启动流程。[原生包文件清单](model/ENGINE_PACKAGE_FINGERPRINTS.json) · [引擎路径与启动参数](serve/runtime_assets.example.json)

## 1. 在新环境准备源码和权重

以下命令均在目标 Linux 主机的同一 Bash 会话执行，假设 `/opt/rtl-agent` 尚未使用。

```bash
set -euo pipefail
git clone https://github.com/shannan0v0/rtl-agent-vivado.git /opt/rtl-agent
cd /opt/rtl-agent
export RTL_MODEL_LED_PYTHON=/opt/python/bin/python3
"$RTL_MODEL_LED_PYTHON" -m pip install -r requirements-table-solver.txt

model_dir=/workspace/v2_migration_20261030/models
model_file=Qwen3.8-27B-UD-Q5_K_M.gguf
mkdir -p "$model_dir"
if [[ ! -f "$model_dir/$model_file" ]]; then
  curl --fail --location --continue-at - --output "$model_dir/$model_file.partial" \
    "https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/4ca720788d1e01f1bff70c033e0d0028fd02e502/$model_file"
  echo "2de73110cb254cbf09b54b717578dadff12ef1194e7271527e68202f39ba4bfd  $model_dir/$model_file.partial" | sha256sum -c -
  mv -- "$model_dir/$model_file.partial" "$model_dir/$model_file"
else
  echo "2de73110cb254cbf09b54b717578dadff12ef1194e7271527e68202f39ba4bfd  $model_dir/$model_file" | sha256sum -c -
fi
```

## 2. 配置并启动本机模型

在已有有效 Vivado 许可证的 shell 中执行。平台若通过 `/workspace/env.sh` 提供许可证和令牌，先执行 `source /workspace/env.sh`。

```bash
cd /opt/rtl-agent
cp -n runtime_config.example.json runtime_config.json
cp -n serve/runtime_assets.example.json serve/runtime_assets.json
mkdir -p /workspace/v4_submission/{evidence/model_calls,runtime/service_state,runtime/run_api}
export RTL_MODEL_LED_CONFIG=/opt/rtl-agent/runtime_config.json
export RTL_MODEL_LED_ASSETS=/opt/rtl-agent/serve/runtime_assets.json
export MODEL_NAME=Qwen3.8-27B-UD-Q5_K_M
export LLM_BASE_URL=http://127.0.0.1:18080/v1
export TRACK=rtl
export VIVADO_BIN=/tools/Xilinx/2026.1/Vivado/bin
export PATH="$VIVADO_BIN:/opt/python/bin:$PATH"
export NO_PROXY=127.0.0.1,localhost,::1
export no_proxy="$NO_PROXY"
command -v xvlog xelab
"$RTL_MODEL_LED_PYTHON" -m serve.runtime_environment \
  --config "$RTL_MODEL_LED_CONFIG" --assets "$RTL_MODEL_LED_ASSETS" >/dev/null
./serve/start.sh
```

如果环境校验报告某个引擎文件、模型哈希或 SDK 目录不匹配，请按上方的文件清单核对实际安装。成功后，Agent 通过本机端口 18080 调用模型。

## 3. 运行一道题

将题面保存为 UTF-8 纯文本，文件名为 `prompt.txt`。假设输入目录是 `/work/my-task`，输出写入一个新的目录：

```bash
cd /opt/rtl-agent
./run.sh /work/my-task /work/my-task-result
```

输出目录中的 `solution.v` 是生成的 RTL，`trace.jsonl` 记录模型调用与检查过程。判断设计功能是否符合要求，还需使用相应的测试台验证。

## 可选：启动本机 HTTP 任务接口

在上述环境中设置平台提供的 `FPGACHINA_TOKEN` 后执行：

```bash
./serve/run_api.sh start
./serve/run_api.sh status
```

服务地址为 `http://127.0.0.1:7860`，鉴权使用 `Authorization: Bearer <token>`。通过 `GET /v1/health` 查看就绪状态，正常应返回 HTTP 200 和 `ready: true`。任务接口为 `POST /v1/solve`，用返回的任务 ID 查询 `GET /v1/jobs/<job_id>`。执行 `./serve/run_api.sh stop` 可停止任务 API，本地模型服务仍驻留。[HTTP 接口实现](official_entry/server.py)
