# Mocker + HTTP 轻量镜像

基于 `python:3.12-slim` 与 PyPI 上的 `ai-dynamo`（**不**安装 `[vllm]`），在单容器内启动：

- `dynamo.frontend`（OpenAI 兼容 HTTP，默认 `:8000`）
- 多个 `dynamo.mocker`（`--engine-type vllm`，模拟 vLLM 调度行为，无 GPU）

使用 **file** 发现（无需 etcd）。镜像内自带 **nats-server** 并在入口脚本中启动，以满足 `ai-dynamo` 1.0.x 对 NATS 事件面的默认依赖（仍比完整 `vllm-runtime` 镜像小得多）。

## 构建

在仓库根目录执行：

```bash
docker build -f examples/backends/mocker/docker/Dockerfile -t dynamo-mocker-vllm:local .
```

可选：覆盖 PyPI 版本（仓库开发版可能领先于 PyPI）：

```bash
docker build -f examples/backends/mocker/docker/Dockerfile \
  --build-arg AI_DYNAMO_VERSION=1.0.1 -t dynamo-mocker-vllm:local .
```

## 运行

```bash
docker run --rm -p 8000:8000 \
  -e HF_TOKEN="$HF_TOKEN" \
  dynamo-mocker-vllm:local
```

或使用 Compose（同样在仓库根目录）：

```bash
docker compose -f examples/backends/mocker/docker/compose.yaml up --build
```

## 环境变量

| 变量 | 说明 |
|------|------|
| `MOCKER_MODELS` | 空格分隔的 HuggingFace 模型 ID；不设则默认两个 DeepSeek-R1-Distill-Qwen（1.5B 与 7B） |
| `MOCK_SPEEDUP` | Mocker 时序加速因子，默认较大以接近即时响应 |
| `ROUTER_MODE` | Frontend 路由模式，默认 `round-robin` |
| `MOCKER_EXTRA_ARGS` | 附加传给 `dynamo.mocker` 的参数（字符串按空格拆分） |
| `HF_TOKEN` | 访问私有或限流模型时需要 |

## 调用示例

**必须**在 JSON 中提供 `max_tokens`（mocker 引擎要求）。

```bash
curl -s "http://localhost:8000/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B",
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 32
  }'
```

将 `model` 换成 `deepseek-ai/DeepSeek-R1-Distill-Qwen-7B` 即路由到第二个 worker。

## 与 `vllm-runtime` 镜像的区别

`nvcr.io/nvidia/ai-dynamo/vllm-runtime` 包含 CUDA、vLLM 与完整推理栈。本镜像仅安装 **CPU** 侧 `ai-dynamo` 与 tokenizer 依赖，用于联调 / 路由 / 前端验证，**不进行真实模型推理**。
