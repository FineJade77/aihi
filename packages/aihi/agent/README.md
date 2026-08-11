# aihi-agent (`aihi.agent`)

Recoverable, provider-neutral Agent Runtime for AIHI. Applications must explicitly provide a
Provider, model, Sandbox, and tool set.

This package does not select providers, models, gateways, prompts, or product defaults.

```python
from aihi.agent import HostBackend, ReadFileTool, RuntimeBuilder
from aihi.models import FakeProvider, FakeStep

runtime = RuntimeBuilder(
    provider=FakeProvider([FakeStep(text="done")]),
    model="fake-model",
    sandbox=HostBackend(".", unsafe=True),
    tools=[ReadFileTool()],
).build()
```

Only names in `aihi.agent.__all__` are public. `aihi-models` is a required dependency; applications
may import both public leaf APIs directly.
