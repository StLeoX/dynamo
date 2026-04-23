# SGLang 风格 Mocker + HTTP 轻量镜像

基于 `python:3.12-slim`，在单容器内启动（**默认从当前仓库源码**安装 `ai-dynamo` / runtime，**不**安装 `[vllm]` 推理栈）：

- `dynamo.frontend`（OpenAI 兼容 HTTP，容器内监听 `127.0.0.1:18000`）
- 兼容代理（**唯一对外端口**，默认 **30000**）：转发到 frontend，并对 `GET /metrics` 返回合成 SGLang 风格 Prometheus 文本
- 多个 `dynamo.mocker`（默认 `--engine-type sglang`，无 GPU）

使用 **file** 发现（无需 etcd）。镜像内自带 **nats-server** 并在入口脚本中启动，以满足 `ai-dynamo` 1.0.x 对 NATS 事件面的默认依赖（仍比完整 `vllm-runtime` 镜像小得多）。

## 构建

`Dockerfile` 为**多阶段**构建：最终镜像只复制安装好的 **`/opt/venv`**、`nats-server` 与入口脚本，**不会**把 monorepo 源码留在运行镜像里（源码仅在 builder 阶段用于 `pip install`）。

在仓库根目录执行（**默认 `AI_DYNAMO_INSTALL_SOURCE=1`**，与 `compose` 一致）：

```bash
docker build -f examples/backends/mocker/docker/Dockerfile -t dynamo-mocker-sglang:local .
```

可选：只从 **PyPI** 安装（镜像更小、构建更快；**旧版 wheel 不含** `dynamo.mocker --engine-type`，与默认 `MOCKER_ENGINE_TYPE=sglang` 不兼容）：

```bash
docker build -f examples/backends/mocker/docker/Dockerfile \
  --build-arg AI_DYNAMO_INSTALL_SOURCE=0 \
  --build-arg AI_DYNAMO_VERSION=1.0.1 \
  -t dynamo-mocker-sglang:local .
```

说明：源码路径在 **builder 阶段**编译并安装 `ai-dynamo-runtime` 与 `ai-dynamo`；
最终运行镜像里只有虚拟环境中的已安装包（不额外携带仓库树）。

## 运行

```bash
docker run --rm -p 30000:30000 \
  -e HF_TOKEN="$HF_TOKEN" \
  dynamo-mocker-sglang:local
```

或使用 Compose（同样在仓库根目录）：

```bash
docker compose -f examples/backends/mocker/docker/compose.yaml up --build
```

build 默认 **`AI_DYNAMO_INSTALL_SOURCE=1`**（当前仓库树）。若要用 PyPI：
`AI_DYNAMO_INSTALL_SOURCE=0 AI_DYNAMO_VERSION=1.0.1 docker compose ... build`（且需 `MOCKER_ENGINE_TYPE=vllm` 或等待 PyPI 支持 `--engine-type`）。

快速验证（注意要覆盖 entrypoint）：

```bash
docker compose -f examples/backends/mocker/docker/compose.yaml run --rm \
  --entrypoint python dynamo-mocker-sglang -m dynamo.mocker -h
```

如需 **vLLM** 行为模拟，可覆盖：

```bash
MOCKER_ENGINE_TYPE=vllm \
docker compose -f examples/backends/mocker/docker/compose.yaml up --build
```

## 指标

在同一对外端口上抓取 SGLang 风格指标：

```bash
curl -s "http://localhost:30000/metrics"
```

`MOCKER_METRICS_MODELS` 由入口脚本设为当前 mock 的模型列表（与 `MOCKER_MODELS` 或默认三模型一致），每个模型输出一组 `sglang:*` gauge/histogram 系列。

## 环境变量

| 变量 | 说明 |
|------|------|
| `HTTP_PORT` | 对外 HTTP（代理）端口，默认 `30000` |
| `MOCKER_MODELS` | 空格分隔的 HuggingFace 模型 ID；不设则默认三个：两个 DeepSeek-R1-Distill-Qwen（1.5B / 7B）与 `Qwen/Qwen3-8B`（约 8B，公开权重，一般无需 HF token） |
| `MODEL_1` / `MODEL_2` / `MODEL_3` | 在未设置 `MOCKER_MODELS` 时覆盖默认三项 |
| `MOCK_SPEEDUP` | Mocker 时序加速因子，默认较大以接近即时响应 |
| `ROUTER_MODE` | Frontend 路由模式，默认 `round-robin` |
| `MOCKER_EXTRA_ARGS` | 附加传给 `dynamo.mocker` 的参数（字符串按空格拆分） |
| `MOCKER_ENGINE_TYPE` | mocker 引擎类型：默认 `sglang`，可设为 `vllm` |
| `DYN_NAMESPACE` | Worker 命名空间，默认 `dynamo`；多模型时会用于生成不同的 `dyn://...` endpoint |
| `MOCKER_ENDPOINT` | 仅当 **单个** mocker 进程时可选，覆盖默认 `dyn://$DYN_NAMESPACE.backend.generate` |
| `HF_TOKEN` | 仅在选用 **私有** 或 **gated**（如 `meta-llama/...`）模型时需要；默认三模型一般为公开，可不设 |

当 `MOCKER_MODELS`（或默认的三个模型）包含 **多个** ID 时，入口脚本会为每个进程分配不同的
`--endpoint`（`generate_<model-hash>`），避免多个进程争抢同一个 `dynamo/backend/generate`。

若 `/v1/models` **少于预期**：某个 worker 下载 tokenizer 失败；查看 `docker compose ... logs`，若为 gated 模型请配置 `HF_TOKEN`。

## 调用示例

**必须**在 JSON 中提供 `max_tokens`（mocker 引擎要求）。

```bash
curl -s "http://localhost:30000/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B",
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 32
  }'
```

将 `model` 换成列表中其它 HF id 即可路由到对应 worker。

## 与 `vllm-runtime` 镜像的区别

`nvcr.io/nvidia/ai-dynamo/vllm-runtime` 包含 CUDA、vLLM 与完整推理栈。本镜像仅安装 **CPU** 侧 `ai-dynamo` 与 tokenizer 依赖，用于联调 / 路由 / 前端验证，**不进行真实模型推理**。
