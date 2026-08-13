# aihi-models

[English](README.md) | **简体中文**

AIHI 的 Provider-neutral 模型契约和 Provider 适配器。它是仓库中最低层的 Python 包，负责统一
消息、流式输出、工具定义、用量、Provider 错误、序列化和 token 估算。

## 职责边界

- 不可变的请求、响应和 Content Block 契约。
- 文本、工具输入和推理载荷的标准化 Stream Chunk。
- Provider Protocol、HTTP transport 和 Provider adapters。
- 类型化 Provider 错误、Context Length 分类和 Message Schema 版本化 codec。

本包不负责 ModelRouter、ModelGateway、应用配置、Prompt、Session、ToolSpec 或 Agent loop。
这些能力由应用层或 `aihi-agent` 负责。

## 支持的 Provider

| Adapter | 用途 |
| --- | --- |
| `OpenAIProvider` | OpenAI Chat Completions |
| `AnthropicProvider` | Anthropic Messages API |
| `DeepSeekProvider` | DeepSeek；复用 OpenAI-compatible 协议 |
| `OpenAICompatibleProvider` | 其他兼容 OpenAI API 的服务；必须传入完整 endpoint |
| `FakeProvider` | 测试和离线 fixture |

Provider 是扁平模块，凭据、模型选择和多个 Provider 的组合由应用层提供，不会静默读取环境变量。

## 安装

```bash
uv sync
uv pip install -e packages/aihi/models
```

要求 Python 3.11+，默认 HTTP transport 依赖 `httpx`。

## 最小示例

```python
from aihi.models import Message, ModelRequest
from aihi.models.providers.fake import FakeProvider

provider = FakeProvider()
request = ModelRequest(
    model="fake-model",
    messages=(Message.user("Say hello"),),
)
async for chunk in provider.stream(request):
    print(chunk)
```

## 公共 API 与兼容性

跨包只使用 `aihi.models` 顶层导出。Message codec 使用独立版本号；旧事件缺少版本时按 v1 读取。
Provider stream 在首个 chunk 之后不得自动切换 Provider，错误必须包含稳定 code 和 `retryable`。

## 开发

```bash
pytest packages/aihi/models/tests
ruff check packages/aihi/models
mypy packages/aihi/models/src
```

详见仓库 [架构文档](../../../docs/ARCHITECTURE.zh-CN.md)。
