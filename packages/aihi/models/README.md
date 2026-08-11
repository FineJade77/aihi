# aihi-models (`aihi.models`)

Provider-neutral model contracts, versioned message serialization, and Provider adapters for AIHI.

This package does not provide Agent Runtime, model routing, gateways, model roles, or application
configuration.

```python
from aihi.models import FakeProvider, FakeStep, Message, ModelRequest

provider = FakeProvider([FakeStep(text="done")])
request = ModelRequest(model="fake-model", messages=(Message.text("user", "hello"),))
```

Only names in `aihi.models.__all__` are public. Provider constructors do not read credentials or
select models on behalf of an application.

Provider adapters live as flat modules under `aihi.models.providers`. `DeepSeekProvider` is a thin
OpenAI-compatible adapter for the official DeepSeek Chat Completions endpoint; callers still pass
the model name explicitly on each `ModelRequest`.

The generic adapter requires an explicit full Chat Completions endpoint so credentials cannot fall
through to an OpenAI default:

```python
from aihi.models import OpenAICompatibleProvider

provider = OpenAICompatibleProvider(
    "provider-api-key",
    base_url="https://provider.example/v1/chat/completions",
)
```
