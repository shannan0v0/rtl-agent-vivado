# RTL Agent · 使用 Vivado 检查与修复生成的 Verilog

输入描述电路行为和端口的文本，Agent 可使用本地 Qwen 27B 或你指定的模型 API 生成 Verilog。配好 Vivado 后，Agent 会检查编译和设计展开，并将诊断或公开规格检查发现的反例反馈给模型。每题最多调用模型三次。

## 用自己的模型 API

准备 Python 3.12、服务商提供的 **API 基础地址、模型 ID 和 API Key**。接口需兼容 `POST /chat/completions`，使用 Bearer 鉴权，并在 `choices[0].message.content` 返回文本。这种方式无需本地 GPU 或 Qwen 权重。

1. 下载仓库并进入项目目录。在该目录下创建 `my-task/prompt.txt`，用 UTF-8 保存题面，写清模块名、端口和电路行为。
2. 将下面的地址和模型 ID 换成服务商给你的值，再运行命令。地址保留服务商要求的路径前缀（常见为 `/v1`）。程序会自动追加 `/chat/completions`。

   ```shell
   python run_api_model.py my-task my-result --base-url https://api.example.com/v1 --model your-model-id
   ```

3. 按提示输入 API Key，终端不会回显。脚本调用也可通过环境变量 `RTL_API_KEY` 提供密钥。题面、修复所需的候选 RTL 和诊断会发送给你选定的服务商。
4. 打开 `my-result/solution.v` 查看代码，打开 `my-result/trace.jsonl` 查看调用与检查记录。每次运行使用新的输出目录。没有 Vivado 也可以生成候选代码；`compile_status` 为 `unavailable` 或 `not_run` 时，编译尚未得到验证。需要编译与修复反馈时，配置已授权的 Vivado，将其 `bin` 目录加入 `PATH` 或设置 `VIVADO_BIN`。

## 本地 Qwen 模型的环境要求

| 项目 | 当前版本使用的环境 |
| --- | --- |
| 操作系统 | Linux x86_64，Bash、`setsid`、Git，且能写入 `/opt/rtl-agent` 和 `/workspace` |
| GPU 与推理引擎 | AMD Radeon PRO W7900（gfx1100），原项目使用单卡、32 GB 显存预算。启动脚本要求 `/opt/llamacpp` 下的指定 llama.cpp HIP 原生包及匹配的 HIP SDK、Tensile 库 |
| 模型 | Qwen3.8-27B-UD-Q5_K_M，GGUF 权重约 19.77 GB，另需任务输出空间 |
| Python 与 EDA | Python 3.14，表格辅助求解使用 SymPy 1.14.0。RTL 编译检查使用 Vivado 2026.1 的 `xvlog`、`xelab` 和有效许可证 |
| 服务 | 命令行任务使用本机推理服务；另开 HTTP 任务接口时需设置 `FPGACHINA_TOKEN` |

原项目使用赛事 Linux 实例预装的推理栈。本地模型启动脚本会核对二进制及库文件的 SHA256、固定路径和模型版本。请先从原平台或资产维护方取得匹配的运行包，并单独准备 GGUF 权重和 Vivado 许可证。[原生包文件清单](model/ENGINE_PACKAGE_FINGERPRINTS.json) · [引擎路径与启动参数](serve/runtime_assets.example.json)

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
