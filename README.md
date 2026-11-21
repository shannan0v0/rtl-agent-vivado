# 逻辑回声（RTL Echo）

**RTL Echo 是面向 FPGA 开发者的开源 RTL 生成与调试工具。** 写明电路的端口和预期行为，RTL Echo 会从完整的真值表等规格直接推导 Verilog，或调用模型生成代码。默认模型为本地 Qwen3.8-27B，也支持自选模型 API。

## 从规格生成和调试电路

在一个四输入组合电路的波形中，16 种输入组合对应的输出都等于 `b | c`。RTL Echo 从波形还原真值表，将电路化简为 `q = b | c`，再逐项核对这 16 种组合。求解器直接处理以下规格。

- 完整真值表和卡诺图最多支持四个输入位，可以是标量，也可以用下标指定总线中的每一位。对于明确列出取 1、取 0 和无关输入的标量问题，也能求最简 SOP/POS。求解器先最小化项数，再最小化总文字数，并逐项核对受约束的输入组合。
- 组合电路的波形按列标出各个标量端口，覆盖每一种输入组合，且同一输入始终对应相同的二进制输出。求解器按列名读取波形并还原逻辑。
- 串行移位与加减计数共用寄存器时，求解器按规格给出的位宽、传入顺序、计数方向、时钟边沿和使能优先级或互斥条件生成电路。

其他规格交给模型生成 RTL。配置 Vivado 后，RTL Echo 会编译代码并展开顶层设计。对能从规格提取行为约束的计数器、状态机等电路，工具还会比对输出。发现错误时，工具把编译诊断或出错时的输入、期望值和实际值反馈给模型。默认每题最多调用模型三次。

`solution.v` 保存生成的电路，`trace.jsonl` 记录生成、检查和修复过程。`compile_status` 给出编译与设计展开的状态。用功能测试台比对电路输出，用目标器件的综合报告查看资源用量。

## 用自己的模型 API

准备 Python 3.12 和服务商提供的 **API 基础地址、模型 ID 和 API Key**。推理在服务商端运行。接口使用 `POST /chat/completions` 和 Bearer 鉴权，在 `choices[0].message.content` 返回文本。

1. 下载仓库并进入项目目录。在该目录下创建 `my-task/prompt.txt`，用 UTF-8 保存题面，写清模块名、端口和电路行为。
2. 将下面的地址和模型 ID 换成服务商给你的值，再运行命令。地址保留服务商要求的路径前缀（常见为 `/v1`）。程序会自动追加 `/chat/completions`。

   ```shell
   python run_api_model.py my-task my-result --base-url https://api.example.com/v1 --model your-model-id
   ```

3. 在终端的隐藏输入提示下键入 API Key。脚本调用也可通过环境变量 `RTL_API_KEY` 提供密钥。程序将题面和修复所需的 RTL、诊断发送给选定的服务商。
4. 打开 `my-result/solution.v` 查看代码，打开 `my-result/trace.jsonl` 查看调用与检查记录。每次运行使用新的输出目录。要启用编译反馈，将已授权 Vivado 的 `bin` 目录加入 `PATH` 或设置 `VIVADO_BIN`。`compile_status` 中的 `unavailable` 表示编译工具不可用，`not_run` 表示跳过了编译。

## 本地 Qwen 模型的环境要求

本地示例配置为每题分配 890 秒，单次模型请求最多 500 秒。程序按剩余时间计算每轮上限，并为检查留出时间。`runtime_config.json` 中的 `model_call_timeout_s` 控制单次请求上限，缺省值为 300 秒。外部模型 API 的单次上限为 300 秒。

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
git clone https://github.com/shannan0v0/rtl-echo.git /opt/rtl-agent
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

在 `/work/my-task-result/solution.v` 查看生成的 RTL，在同目录的 `trace.jsonl` 查看本题的调用与检查记录。

## 可选：启动本机 HTTP 任务接口

在上述环境中设置平台提供的 `FPGACHINA_TOKEN` 后执行：

```bash
./serve/run_api.sh start
./serve/run_api.sh status
```

服务地址为 `http://127.0.0.1:7860`，鉴权使用 `Authorization: Bearer <token>`。通过 `GET /v1/health` 查看就绪状态，正常应返回 HTTP 200 和 `ready: true`。任务接口为 `POST /v1/solve`，用返回的任务 ID 查询 `GET /v1/jobs/<job_id>`。执行 `./serve/run_api.sh stop` 可停止任务 API，本地模型服务仍驻留。[HTTP 接口实现](official_entry/server.py)

## 许可证

RTL Echo 团队编写的代码采用 GNU 通用公共许可证第 3 版（SPDX 标识符 `GPL-3.0-only`）。[GPLv3 许可全文](LICENSE)

AMD 提供了赛事基线运行器 `baseline.py` 和启动脚本 `run_baseline.sh`。仓库根目录及 `official_entry/` 下的四个文件均保留了上游原始内容，版权归属为 Copyright (c) 2026 AMD，按 MIT 许可证分发。再分发这些文件时请保留其版权和许可声明。[完整许可文本](LICENSES/AMD-MIT.txt) · [上游仓库](https://gitee.com/Vickyiii/rtlagent2026) · [来源版本与文件校验值](official_entry/baseline_identity.py)
