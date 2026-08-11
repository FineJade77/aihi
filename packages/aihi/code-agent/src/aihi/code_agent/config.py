"""Typed, file-backed configuration for the Coding Agent application.

Configuration is application-owned.  The Harness still owns runtime safety
defaults: a Host sandbox is never enabled unless the file explicitly contains
``unsafe = true`` and provider credentials are referenced by environment name,
never copied into the TOML document.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from aihi.agent.policy import PermissionMode
from aihi.agent.skills import SkillScope

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONFIG_FILENAME = "aihi-code.toml"
_DEFAULT_TOOLS = ("read_file", "glob", "grep", "edit_file", "write_file", "bash")


class CodeAgentConfigError(ValueError):
    """A Coding Agent configuration is malformed or unsafe."""


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    name: str = "fake"
    model: str = "demo"
    api_key_env: str | None = None
    base_url: str | None = None
    timeout_seconds: float = 90.0


@dataclass(frozen=True, slots=True)
class SandboxSettings:
    backend: str = "host"
    root: Path = Path(".")
    unsafe: bool = False
    image: str | None = None
    network: str = "none"
    allow_network: bool = False
    workspace_read_only: bool = False


@dataclass(frozen=True, slots=True)
class SkillRootSettings:
    path: Path
    scope: SkillScope


@dataclass(frozen=True, slots=True)
class McpServerSettings:
    name: str
    command: tuple[str, ...]
    cwd: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    allowed_tools: frozenset[str] | None = None
    request_timeout_seconds: float = 30.0
    reconnect_attempts: int = 1


@dataclass(frozen=True, slots=True)
class CodeAgentConfig:
    """Resolved Coding Agent settings.

    ``base_dir`` is the directory containing the loaded file (or the supplied
    workspace when no file exists), so all relative paths are deterministic.
    """

    base_dir: Path
    provider: ProviderSettings = ProviderSettings()
    provider_profiles: Mapping[str, ProviderSettings] = field(default_factory=dict)
    system_prompt: str = ""
    max_output_tokens: int = 4_096
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    require_capability_lease: bool = False
    tools: tuple[str, ...] = _DEFAULT_TOOLS
    sandbox: SandboxSettings = SandboxSettings()
    skill_roots: tuple[SkillRootSettings, ...] = ()
    skill_trust_path: Path | None = None
    skill_load_tool: bool = False
    mcp_servers: tuple[McpServerSettings, ...] = ()
    source_path: Path | None = None

    @classmethod
    def defaults(cls, cwd: str | Path) -> CodeAgentConfig:
        base_dir = Path(cwd).expanduser().resolve(strict=True)
        provider = ProviderSettings()
        return cls(
            base_dir=base_dir,
            provider=provider,
            provider_profiles={provider.name: provider},
            sandbox=SandboxSettings(root=base_dir),
        )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        base_dir: str | Path,
        source_path: str | Path | None = None,
    ) -> CodeAgentConfig:
        root = Path(base_dir).expanduser().resolve(strict=True)
        provider_map = _section(value, "provider")
        agent_map = _section(value, "agent")
        sandbox_map = _section(value, "sandbox")
        skills_map = _section(value, "skills")
        mcp_map = _section(value, "mcp")
        if "api_key" in provider_map:
            raise CodeAgentConfigError(
                "provider.api_key is not supported; reference credentials with api_key_env"
            )

        provider = _parse_provider_settings(
            provider_map,
            key="provider",
            default_name="fake",
            default_model=agent_map.get("model", "demo"),
        )
        provider_profiles = _parse_provider_profiles(value.get("providers", {}), provider)

        permission_mode = _enum(
            agent_map.get("permission_mode", PermissionMode.DEFAULT.value),
            PermissionMode,
            "agent.permission_mode",
        )
        max_output_tokens = _positive_int(
            agent_map.get("max_output_tokens", 4_096), "agent.max_output_tokens"
        )
        raw_system_prompt = agent_map.get("system_prompt", "")
        if not isinstance(raw_system_prompt, str):
            raise CodeAgentConfigError("agent.system_prompt must be a string")
        system_prompt = raw_system_prompt
        require_capability_lease = _boolean(
            agent_map.get("require_capability_lease", False), "agent.require_capability_lease"
        )
        tools = _string_tuple(agent_map.get("tools", _DEFAULT_TOOLS), "agent.tools")
        if not tools:
            raise CodeAgentConfigError("agent.tools must contain at least one tool")

        sandbox_backend = _text(sandbox_map.get("backend", "host"), "sandbox.backend").lower()
        sandbox_root = _resolve_path(
            sandbox_map.get("root", "."), root, "sandbox.root"
        )
        sandbox = SandboxSettings(
            backend=sandbox_backend,
            root=sandbox_root,
            unsafe=_boolean(sandbox_map.get("unsafe", False), "sandbox.unsafe"),
            image=(
                _text(sandbox_map["image"], "sandbox.image")
                if "image" in sandbox_map and sandbox_map["image"] is not None
                else None
            ),
            network=_text(sandbox_map.get("network", "none"), "sandbox.network"),
            allow_network=_boolean(
                sandbox_map.get("allow_network", False), "sandbox.allow_network"
            ),
            workspace_read_only=_boolean(
                sandbox_map.get("workspace_read_only", False), "sandbox.workspace_read_only"
            ),
        )
        if sandbox_backend not in {"host", "docker"}:
            raise CodeAgentConfigError(
                "sandbox.backend must be one of: host, docker"
            )

        skill_roots = _parse_skill_roots(skills_map, root)
        skill_load_tool = _boolean(
            skills_map.get("load_tool", bool(skill_roots)), "skills.load_tool"
        )
        skill_trust_path = (
            _resolve_path(
                skills_map.get("trust_lockfile", ".aiharness/skills.lock.json"),
                root,
                "skills.trust_lockfile",
            )
            if skill_roots
            else None
        )
        mcp_servers = _parse_mcp_servers(mcp_map, root)
        return cls(
            base_dir=root,
            provider=provider,
            provider_profiles=provider_profiles,
            system_prompt=system_prompt,
            max_output_tokens=max_output_tokens,
            permission_mode=permission_mode,
            require_capability_lease=require_capability_lease,
            tools=tools,
            sandbox=sandbox,
            skill_roots=skill_roots,
            skill_trust_path=skill_trust_path,
            skill_load_tool=skill_load_tool,
            mcp_servers=mcp_servers,
            source_path=(Path(source_path).expanduser().resolve() if source_path else None),
        )

    def select_provider(
        self, provider: str | None = None, *, model: str | None = None
    ) -> CodeAgentConfig:
        """Return a run config with an explicitly selected configured provider/model."""

        selected_name = self.provider.name if provider is None else _provider_name(provider)
        selected = self.provider_profiles.get(selected_name)
        if selected is None:
            raise CodeAgentConfigError(
                f"Provider {selected_name!r} is not configured; add [providers.{selected_name}]"
            )
        selected_model = selected.model if model is None else _text(model, "model")
        return replace(self, provider=replace(selected, model=selected_model))

    def public_descriptor(self) -> dict[str, object]:
        """Return non-secret config metadata for the CLI and diagnostics."""

        providers = [
            {
                "name": profile.name,
                "model": profile.model,
                "api_key_env": profile.api_key_env,
                "base_url": profile.base_url,
            }
            for profile in self.provider_profiles.values()
        ]
        providers.sort(key=lambda item: str(item["name"]))
        return {
            "source_path": str(self.source_path) if self.source_path else None,
            "base_dir": str(self.base_dir),
            "provider": {
                "name": self.provider.name,
                "model": self.provider.model,
                "api_key_env": self.provider.api_key_env,
                "base_url": self.provider.base_url,
            },
            "providers": providers,
            "tools": list(self.tools),
            "sandbox": {
                "backend": self.sandbox.backend,
                "root": str(self.sandbox.root),
                "unsafe": self.sandbox.unsafe,
            },
            "skills": {
                "roots": [str(root.path) for root in self.skill_roots],
                "load_tool": self.skill_load_tool,
                "trust_lockfile": (
                    str(self.skill_trust_path) if self.skill_trust_path else None
                ),
            },
            "mcp_servers": [server.name for server in self.mcp_servers],
        }


def load_config(
    path: str | Path | None = None,
    *,
    cwd: str | Path,
) -> CodeAgentConfig:
    """Load ``aihi-code.toml`` or return safe defaults when it is absent."""

    workspace = Path(cwd).expanduser().resolve(strict=True)
    requested = Path(path).expanduser() if path is not None else workspace / _CONFIG_FILENAME
    if not requested.is_absolute():
        requested = workspace / requested
    requested = requested.resolve()
    if not requested.exists():
        if path is not None:
            raise CodeAgentConfigError(f"Configuration file does not exist: {requested}")
        return CodeAgentConfig.defaults(workspace)
    if not requested.is_file():
        raise CodeAgentConfigError(f"Configuration path is not a file: {requested}")
    try:
        with requested.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise CodeAgentConfigError(f"Cannot load configuration: {requested}") from error
    if not isinstance(raw, dict):
        raise CodeAgentConfigError("Configuration root must be a TOML table")
    return CodeAgentConfig.from_mapping(raw, base_dir=requested.parent, source_path=requested)


def resolve_env_mapping(value: Mapping[str, str]) -> dict[str, str]:
    """Resolve ``ENV:NAME`` references without putting secrets in config files."""

    resolved: dict[str, str] = {}
    for key, raw in value.items():
        if not _ENV_NAME.fullmatch(key):
            raise CodeAgentConfigError(f"MCP environment key is invalid: {key!r}")
        if raw.startswith("ENV:"):
            name = _env_name(raw[4:], f"MCP environment reference for {key}")
            secret = os.environ.get(name)
            if secret is None:
                raise CodeAgentConfigError(f"Required environment variable is missing: {name}")
            resolved[key] = secret
        else:
            resolved[key] = raw
    return resolved


def _parse_skill_roots(value: Mapping[str, Any], base_dir: Path) -> tuple[SkillRootSettings, ...]:
    raw_roots = value.get("roots", [])
    if not isinstance(raw_roots, list):
        raise CodeAgentConfigError("skills.roots must be an array of tables")
    roots: list[SkillRootSettings] = []
    for index, raw in enumerate(raw_roots):
        if not isinstance(raw, dict):
            raise CodeAgentConfigError(f"skills.roots[{index}] must be a table")
        path = _resolve_path(raw.get("path"), base_dir, f"skills.roots[{index}].path")
        scope = _enum(raw.get("scope"), SkillScope, f"skills.roots[{index}].scope")
        roots.append(SkillRootSettings(path=path, scope=scope))
    return tuple(roots)


def _parse_provider_settings(
    value: Mapping[str, Any],
    *,
    key: str,
    default_name: str,
    default_model: object,
) -> ProviderSettings:
    provider_name = _provider_name(value.get("name", default_name), f"{key}.name")
    model = _text(value.get("model", default_model), f"{key}.model")
    api_key_env = value.get("api_key_env")
    if api_key_env is not None:
        api_key_env = _env_name(api_key_env, f"{key}.api_key_env")
    base_url = value.get("base_url")
    if base_url is not None:
        base_url = _text(base_url, f"{key}.base_url")
    timeout_seconds = _positive_float(
        value.get("timeout_seconds", 90.0), f"{key}.timeout_seconds"
    )
    return ProviderSettings(
        name=provider_name,
        model=model,
        api_key_env=api_key_env,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def _parse_provider_profiles(
    value: object, active: ProviderSettings
) -> dict[str, ProviderSettings]:
    if not isinstance(value, dict):
        raise CodeAgentConfigError("providers must be a TOML table")
    profiles: dict[str, ProviderSettings] = {active.name: active}
    for raw_name, raw_settings in value.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise CodeAgentConfigError("Provider profile names must be non-empty strings")
        if not isinstance(raw_settings, dict):
            raise CodeAgentConfigError(f"providers.{raw_name} must be a TOML table")
        profile_name = _provider_name(raw_name, f"providers.{raw_name}")
        profile = _parse_provider_settings(
            raw_settings,
            key=f"providers.{raw_name}",
            default_name=profile_name,
            default_model=active.model,
        )
        if profile.name != profile_name:
            raise CodeAgentConfigError(
                f"providers.{raw_name}.name must match the profile name"
            )
        profiles[profile_name] = profile
    return profiles


def _parse_mcp_servers(value: Mapping[str, Any], base_dir: Path) -> tuple[McpServerSettings, ...]:
    raw_servers = value.get("servers", {})
    if not isinstance(raw_servers, dict):
        raise CodeAgentConfigError("mcp.servers must be a table")
    servers: list[McpServerSettings] = []
    for name, raw in sorted(raw_servers.items()):
        if not isinstance(name, str) or not name.strip():
            raise CodeAgentConfigError("MCP server names must be non-empty strings")
        if not isinstance(raw, dict):
            raise CodeAgentConfigError(f"mcp.servers.{name} must be a table")
        command = _string_tuple(raw.get("command"), f"mcp.servers.{name}.command")
        if not command:
            raise CodeAgentConfigError(f"mcp.servers.{name}.command must not be empty")
        cwd = (
            _resolve_path(raw["cwd"], base_dir, f"mcp.servers.{name}.cwd")
            if "cwd" in raw
            else None
        )
        raw_env = raw.get("env", {})
        if not isinstance(raw_env, dict) or any(
            not isinstance(key, str) or not isinstance(item, str)
            for key, item in raw_env.items()
        ):
            raise CodeAgentConfigError(f"mcp.servers.{name}.env must be a string table")
        raw_allowed = raw.get("allowed_tools")
        allowed = None if raw_allowed is None else frozenset(
            _string_tuple(raw_allowed, f"mcp.servers.{name}.allowed_tools")
        )
        servers.append(
            McpServerSettings(
                name=name,
                command=command,
                cwd=cwd,
                env=dict(raw_env),
                allowed_tools=allowed,
                request_timeout_seconds=_positive_float(
                    raw.get("request_timeout_seconds", 30.0),
                    f"mcp.servers.{name}.request_timeout_seconds",
                ),
                reconnect_attempts=_non_negative_int(
                    raw.get("reconnect_attempts", 1),
                    f"mcp.servers.{name}.reconnect_attempts",
                ),
            )
        )
    return tuple(servers)


def _section(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    section = value.get(key, {})
    if not isinstance(section, dict):
        raise CodeAgentConfigError(f"{key} must be a TOML table")
    return section


def _text(value: object, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CodeAgentConfigError(f"{key} must be a non-empty string")
    return value.strip()


def _provider_name(value: object, key: str = "provider") -> str:
    return _text(value, key).replace("-", "_").lower()


def _env_name(value: object, key: str) -> str:
    text = _text(value, key)
    if _ENV_NAME.fullmatch(text) is None:
        raise CodeAgentConfigError(f"{key} must be a valid environment variable name")
    return text


def _boolean(value: object, key: str) -> bool:
    if not isinstance(value, bool):
        raise CodeAgentConfigError(f"{key} must be a boolean")
    return value


def _positive_int(value: object, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CodeAgentConfigError(f"{key} must be a positive integer")
    return value


def _non_negative_int(value: object, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CodeAgentConfigError(f"{key} must be a non-negative integer")
    return value


def _positive_float(value: object, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise CodeAgentConfigError(f"{key} must be a positive number")
    return float(value)


def _string_tuple(value: object, key: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise CodeAgentConfigError(f"{key} must be an array of strings")
    parsed = tuple(_text(item, f"{key}[]") for item in value)
    if len(set(parsed)) != len(parsed):
        raise CodeAgentConfigError(f"{key} must not contain duplicates")
    return parsed


def _enum(value: object, enum_type: type[Any], key: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as error:
        choices = ", ".join(item.value for item in enum_type)
        raise CodeAgentConfigError(f"{key} must be one of: {choices}") from error


def _resolve_path(value: object, base_dir: Path, key: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise CodeAgentConfigError(f"{key} must be a non-empty path")
    candidate = Path(value).expanduser()
    return (candidate if candidate.is_absolute() else base_dir / candidate).resolve()


__all__ = [
    "CodeAgentConfig",
    "CodeAgentConfigError",
    "McpServerSettings",
    "ProviderSettings",
    "SandboxSettings",
    "SkillRootSettings",
    "load_config",
    "resolve_env_mapping",
]
