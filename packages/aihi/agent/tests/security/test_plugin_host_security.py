from pathlib import Path

import pytest
from aihi.agent.plugins import (
    InMemoryTrustStore,
    PluginCapabilityDenied,
    PluginDiscovery,
    PluginHost,
    PluginHostPolicy,
    PluginIntegrityError,
    PluginNotTrusted,
    PluginTrustManager,
)

from packages.aihi.agent.tests.unit.test_plugin_host import write_plugin


def prepared_plugin(tmp_path: Path):
    root = write_plugin(tmp_path / "demo")
    discovery = PluginDiscovery((tmp_path,), harness_version="0.1.0")
    candidate = discovery.discover()[0]
    trust = PluginTrustManager(InMemoryTrustStore(), discovery=discovery)
    return root, discovery, candidate, trust


def permissive_policy() -> PluginHostPolicy:
    return PluginHostPolicy(
        allowed_capabilities=frozenset({"tool", "skill", "hook"}),
        allowed_permissions=frozenset({"tools:read"}),
    )


@pytest.mark.asyncio
async def test_plugin_host_is_untrusted_by_default(tmp_path: Path) -> None:
    _root, discovery, candidate, trust = prepared_plugin(tmp_path)
    host = PluginHost(candidate, trust, discovery=discovery, policy=permissive_policy())
    with pytest.raises(PluginNotTrusted):
        await host.start()
    assert host.running is False


@pytest.mark.asyncio
async def test_plugin_host_requires_capability_and_permission_subset(tmp_path: Path) -> None:
    _root, discovery, candidate, trust = prepared_plugin(tmp_path)
    trust.trust(candidate, trusted_by="security-test", enable=True)
    host = PluginHost(
        candidate,
        trust,
        discovery=discovery,
        policy=PluginHostPolicy(allowed_capabilities=frozenset({"tool"})),
    )
    with pytest.raises(PluginCapabilityDenied) as error:
        await host.start()
    assert error.value.details["missing_capabilities"] == ["hook", "skill"]
    assert error.value.details["missing_permissions"] == ["tools:read"]
    assert host.running is False


@pytest.mark.asyncio
async def test_plugin_host_fresh_verification_rejects_tampering(tmp_path: Path) -> None:
    root, discovery, candidate, trust = prepared_plugin(tmp_path)
    trust.trust(candidate, trusted_by="security-test", enable=True)
    (root / "plugin_impl.py").write_text(
        (root / "plugin_impl.py").read_text(encoding="utf-8") + "\n# tampered\n",
        encoding="utf-8",
    )
    host = PluginHost(candidate, trust, discovery=discovery, policy=permissive_policy())
    with pytest.raises(PluginIntegrityError):
        await host.start()
    assert host.running is False
