"""Typed, file-backed configuration for the Coding Agent application.

Configuration is application-owned. The Harness still owns runtime safety
defaults: a Host sandbox is never enabled unless configuration explicitly sets
``unsafe = true`` or the user acknowledges that exact workspace. Provider
credentials are referenced by environment name, never copied into TOML.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aihi.agent.policy import PermissionMode
from aihi.agent.skills import SkillScope

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONFIG_FILENAME = "aihi-code.toml"
_PROJECT_CONFIG_DIRNAME = ".aihi"
_HOST_ACK_FILENAME = "host-workspaces.json"
DEFAULT_USER_CONFIG_TOML = '''\
# AIHI Coding Agent user configuration.
# Project config (<cwd>/.aihi/aihi-code.toml) is merged over these defaults.

[provider]
name = "fake"
model = "demo"

[sandbox]
backend = "host"
# The Host backend is not an isolation boundary.  With unsafe = true the
# bash, write_file, and edit_file tools act directly on this machine under
# your own account. The interactive CLI asks before trusting this workspace;
# setting this to true is the non-interactive, configuration-owned opt-in.
unsafe = false
# sandbox.root is deliberately unset.  Relative paths resolve against the
# directory holding this file, so root = "." would confine the agent to
# ~/.aihi; leaving it unset roots the sandbox at the workspace you launch in.

[artifacts]
# Also relative to this file, so this is ~/.aihi/artifacts.  Override it in a
# project config to keep a workspace's artifacts inside that workspace.
path = "artifacts"

[agent]
# Permission modes: default, accept_edits, plan, or bypass.
permission_mode = "default"
'''
_DEFAULT_TOOLS = (
    "read_file",
    "glob",
    "grep",
    "git_status",
    "git_diff",
    "edit_file",
    "write_file",
    "bash",
)


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
class SubagentTypeSettings:
    """Per-type overrides for one named Subagent."""

    enabled: bool = True
    model: str | None = None


@dataclass(frozen=True, slots=True)
class SubagentSettings:
    # On by default, but the ceiling below is read-only: delegation that works
    # out of the box must not also grant write or process capabilities.
    enabled: bool = True
    model: str | None = None
    max_tokens: int = 8_192
    timeout_seconds: float = 600.0
    max_tool_calls: int = 100
    max_depth: int = 1
    max_children: int = 3
    capabilities: frozenset[str] = frozenset({"filesystem.read"})
    types: Mapping[str, SubagentTypeSettings] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CodeAgentConfig:
    """Resolved Coding Agent settings.

    ``base_dir`` is the explicit file directory or the workspace for a merged
    configuration. ``source_paths`` records merged layers from low to high
    precedence; ``source_path`` is the highest-precedence compatibility view.
    """

    base_dir: Path
    provider: ProviderSettings = ProviderSettings()
    provider_profiles: Mapping[str, ProviderSettings] = field(default_factory=dict)
    max_output_tokens: int = 4_096
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    require_capability_lease: bool = False
    tools: tuple[str, ...] = _DEFAULT_TOOLS
    sandbox: SandboxSettings = SandboxSettings()
    skill_roots: tuple[SkillRootSettings, ...] = ()
    skill_trust_path: Path | None = None
    # BUILTIN Skills are always present, so their explicit loading tool is an
    # out-of-box capability unless configuration deliberately disables it.
    skill_load_tool: bool = True
    mcp_servers: tuple[McpServerSettings, ...] = ()
    artifact_path: Path | None = None
    compact_model: str | None = None
    context_window: int | None = None
    subagents: SubagentSettings = SubagentSettings()
    source_path: Path | None = None
    source_paths: tuple[Path, ...] = ()

    @classmethod
    def defaults(cls, cwd: str | Path) -> CodeAgentConfig:
        base_dir = Path(cwd).expanduser().resolve(strict=True)
        provider = ProviderSettings()
        return cls(
            base_dir=base_dir,
            provider=provider,
            provider_profiles={provider.name: provider},
            sandbox=SandboxSettings(root=base_dir),
            artifact_path=base_dir / ".aihi" / "artifacts",
        )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        base_dir: str | Path,
        workspace_root: str | Path | None = None,
        source_path: str | Path | None = None,
        source_paths: tuple[str | Path, ...] = (),
    ) -> CodeAgentConfig:
        root = Path(base_dir).expanduser().resolve(strict=True)
        workspace = Path(workspace_root or root).expanduser().resolve(strict=True)
        provider_map = _section(value, "provider")
        agent_map = _section(value, "agent")
        sandbox_map = _section(value, "sandbox")
        skills_map = _section(value, "skills")
        mcp_map = _section(value, "mcp")
        artifacts_map = _section(value, "artifacts")
        subagents_map = _section(value, "subagents")
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
        for retired in ("system_prompt", "system_prompt_mode"):
            if retired in agent_map:
                raise CodeAgentConfigError(
                    f"agent.{retired} is no longer supported; the Coding Agent owns its "
                    "prompt. Edit aihi/code_agent/prompts/coding.md, or put project rules "
                    "in the workspace's AGENTS.md."
                )
        require_capability_lease = _boolean(
            agent_map.get("require_capability_lease", False), "agent.require_capability_lease"
        )
        tools = _string_tuple(agent_map.get("tools", _DEFAULT_TOOLS), "agent.tools")
        if not tools:
            raise CodeAgentConfigError("agent.tools must contain at least one tool")

        sandbox_backend = _text(sandbox_map.get("backend", "host"), "sandbox.backend").lower()
        sandbox_root = (
            _resolve_path(sandbox_map["root"], root, "sandbox.root")
            if "root" in sandbox_map
            else workspace
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

        raw_compact_model = agent_map.get("compact_model")
        compact_model = (
            _text(raw_compact_model, "agent.compact_model")
            if raw_compact_model is not None
            else None
        )
        raw_context_window = agent_map.get("context_window")
        context_window = (
            _positive_int(raw_context_window, "agent.context_window")
            if raw_context_window is not None
            else None
        )

        skill_roots = _parse_skill_roots(skills_map, root)
        # On by default, because the BUILTIN root is always discovered: keying
        # this off configured roots left the shipped Skills advertised in the
        # context with no tool able to fetch them, and the model improvising.
        skill_load_tool = _boolean(skills_map.get("load_tool", True), "skills.load_tool")
        skill_trust_path = (
            _resolve_path(
                skills_map.get("trust_lockfile", ".aihi/skills.lock.json"),
                root,
                "skills.trust_lockfile",
            )
            if skill_roots
            else None
        )
        mcp_servers = _parse_mcp_servers(mcp_map, root)
        artifact_path = (
            _resolve_path(
                artifacts_map.get("path", ".aihi/artifacts"),
                root,
                "artifacts.path",
            )
            if _boolean(artifacts_map.get("enabled", True), "artifacts.enabled")
            else None
        )
        subagents = _parse_subagents(subagents_map)
        return cls(
            base_dir=root,
            provider=provider,
            provider_profiles=provider_profiles,
            max_output_tokens=max_output_tokens,
            permission_mode=permission_mode,
            require_capability_lease=require_capability_lease,
            tools=tools,
            sandbox=sandbox,
            skill_roots=skill_roots,
            skill_trust_path=skill_trust_path,
            skill_load_tool=skill_load_tool,
            mcp_servers=mcp_servers,
            artifact_path=artifact_path,
            compact_model=compact_model,
            context_window=context_window,
            subagents=subagents,
            source_path=(Path(source_path).expanduser().resolve() if source_path else None),
            source_paths=tuple(Path(path).expanduser().resolve() for path in source_paths),
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
            "source_paths": [str(path) for path in self.source_paths],
            "base_dir": str(self.base_dir),
            "provider": {
                "name": self.provider.name,
                "model": self.provider.model,
                "api_key_env": self.provider.api_key_env,
                "base_url": self.provider.base_url,
            },
            "providers": providers,
            "tools": list(self.tools),
            "permission_mode": self.permission_mode.value,
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
            "artifacts": {
                "enabled": self.artifact_path is not None,
                "path": str(self.artifact_path) if self.artifact_path else None,
            },
            "compact_model": self.compact_model,
            "context_window": self.context_window,
            "subagents": {
                "enabled": self.subagents.enabled,
                "model": self.subagents.model,
                "max_tokens": self.subagents.max_tokens,
                "timeout_seconds": self.subagents.timeout_seconds,
                "max_tool_calls": self.subagents.max_tool_calls,
                "capabilities": sorted(self.subagents.capabilities),
            },
        }


def load_config(
    path: str | Path | None = None,
    *,
    cwd: str | Path,
) -> CodeAgentConfig:
    """Load an explicit file or merge the fixed user/project config layers.

    Implicit configuration is merged from low to high precedence: user,
    legacy project-root, then ``<cwd>/.aihi``. Tables are merged recursively
    and arrays are replaced by the higher layer. Paths declared in a layer are
    resolved relative to that layer before merging, so inherited user Skill or
    MCP paths keep their original meaning.

    ``path`` remains an embedded-Python API for tests and custom hosts. The
    Worker wire protocol intentionally exposes no configuration-path override.
    """

    workspace = Path(cwd).expanduser().resolve(strict=True)
    if path is None:
        sources = _discover_default_configs(workspace)
        if not sources:
            return CodeAgentConfig.defaults(workspace)
        merged: dict[str, Any] = {}
        for source in sources:
            raw = _load_toml(source)
            _deep_merge(merged, _anchor_layer_paths(raw, source.parent))
        return CodeAgentConfig.from_mapping(
            merged,
            base_dir=workspace,
            workspace_root=workspace,
            source_path=sources[-1],
            source_paths=tuple(sources),
        )

    requested = Path(path).expanduser()
    if not requested.is_absolute():
        requested = workspace / requested
    requested = requested.resolve()
    if not requested.exists():
        if path is not None:
            raise CodeAgentConfigError(f"Configuration file does not exist: {requested}")
        return CodeAgentConfig.defaults(workspace)
    if not requested.is_file():
        raise CodeAgentConfigError(f"Configuration path is not a file: {requested}")
    raw = _load_toml(requested)
    return CodeAgentConfig.from_mapping(
        raw,
        base_dir=requested.parent,
        workspace_root=workspace,
        source_path=requested,
        source_paths=(requested,),
    )


def user_config_path() -> Path:
    """Return the user-scope config path, the lowest-precedence candidate."""

    return Path.home() / _PROJECT_CONFIG_DIRNAME / _CONFIG_FILENAME


def host_acknowledgement_path() -> Path:
    """Return the fixed user-scope store for trusted Host workspaces."""

    return Path.home() / _PROJECT_CONFIG_DIRNAME / _HOST_ACK_FILENAME


def host_execution_acknowledged(cwd: str | Path, *, root: str | Path | None = None) -> bool:
    """Fail closed unless the exact workspace and Host root were acknowledged."""

    workspace = str(Path(cwd).expanduser().resolve(strict=True))
    execution_root = str(Path(root or cwd).expanduser().resolve(strict=True))
    path = host_acknowledgement_path()
    if not path.is_file():
        return False
    try:
        import json

        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    if not isinstance(raw, dict) or raw.get("version") != 1:
        return False
    workspaces = raw.get("workspaces")
    if not isinstance(workspaces, dict):
        return False
    acknowledgement = workspaces.get(workspace)
    return (
        isinstance(acknowledgement, dict)
        and acknowledgement.get("root") == execution_root
    )


def acknowledge_host_execution(
    cwd: str | Path,
    *,
    root: str | Path | None = None,
) -> Path:
    """Persist an explicit acknowledgement scoped to a workspace and Host root."""

    import json

    workspace = str(Path(cwd).expanduser().resolve(strict=True))
    execution_root = str(Path(root or cwd).expanduser().resolve(strict=True))
    path = host_acknowledgement_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    workspaces: dict[str, object] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise CodeAgentConfigError(f"Cannot load Host acknowledgement: {path}") from error
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise CodeAgentConfigError(f"Host acknowledgement has an unsupported format: {path}")
        existing = raw.get("workspaces")
        if not isinstance(existing, dict):
            raise CodeAgentConfigError(f"Host acknowledgement workspaces must be an object: {path}")
        workspaces.update(existing)
    workspaces[workspace] = {
        "acknowledged_at": datetime.now(UTC).isoformat(),
        "root": execution_root,
    }
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps({"version": 1, "workspaces": workspaces}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(path)
    return path


def load_worker_config(cwd: str | Path) -> CodeAgentConfig:
    """Load fixed config layers and apply a persisted workspace Host acknowledgement."""

    config = load_config(cwd=cwd)
    if (
        config.sandbox.backend == "host"
        and not config.sandbox.unsafe
        and host_execution_acknowledged(cwd, root=config.sandbox.root)
    ):
        config = replace(config, sandbox=replace(config.sandbox, unsafe=True))
    return config


def ensure_user_config() -> tuple[Path, bool]:
    """Seed ``~/.aihi/aihi-code.toml`` when absent; never overwrite it.

    Returns the path and whether this call created the file.  Callers own the
    decision to write to the user's home directory; loading config never does.
    """

    path = user_config_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        return path, False
    # ``x`` keeps a concurrent Worker from clobbering a file we just lost a race for.
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(DEFAULT_USER_CONFIG_TOML)
    except FileExistsError:
        return path, False
    return path, True


def _discover_default_configs(workspace: Path) -> tuple[Path, ...]:
    """Return existing implicit config layers from lowest to highest precedence."""

    candidates = (
        Path.home() / _PROJECT_CONFIG_DIRNAME / _CONFIG_FILENAME,
        # Keep the old project-root location readable as a migration layer.
        workspace / _CONFIG_FILENAME,
        workspace / _PROJECT_CONFIG_DIRNAME / _CONFIG_FILENAME,
    )
    sources: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if resolved not in seen:
            sources.append(resolved)
            seen.add(resolved)
    return tuple(sources)


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise CodeAgentConfigError(f"Cannot load configuration: {path}") from error
    if not isinstance(raw, dict):
        raise CodeAgentConfigError("Configuration root must be a TOML table")
    return raw


def _deep_merge(target: dict[str, Any], overlay: Mapping[str, Any]) -> None:
    """Merge a config table in place; higher-layer arrays and scalars replace."""

    for key, value in overlay.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            _deep_merge(current, value)
        else:
            target[key] = deepcopy(value)


def _anchor_layer_paths(value: Mapping[str, Any], base_dir: Path) -> dict[str, Any]:
    """Make path values absolute before differently-rooted layers are merged."""

    anchored = deepcopy(dict(value))

    def anchor(table: object, key: str) -> None:
        if not isinstance(table, dict) or key not in table:
            return
        candidate = table[key]
        if not isinstance(candidate, str) or not candidate.strip():
            return
        path = Path(candidate).expanduser()
        table[key] = str((path if path.is_absolute() else base_dir / path).resolve())

    anchor(anchored.get("sandbox"), "root")
    anchor(anchored.get("artifacts"), "path")
    skills = anchored.get("skills")
    anchor(skills, "trust_lockfile")
    if isinstance(skills, dict):
        roots = skills.get("roots")
        if isinstance(roots, list):
            for root in roots:
                anchor(root, "path")
    mcp = anchored.get("mcp")
    if isinstance(mcp, dict):
        servers = mcp.get("servers")
        if isinstance(servers, dict):
            for server in servers.values():
                anchor(server, "cwd")
    return anchored


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
        if scope is SkillScope.BUILTIN:
            raise CodeAgentConfigError(
                f"skills.roots[{index}].scope cannot be builtin; "
                "builtin roots are supplied by the application"
            )
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


def _parse_subagents(value: Mapping[str, Any]) -> SubagentSettings:
    raw_capabilities = value.get("capabilities", ["filesystem.read"])
    capabilities = frozenset(_string_tuple(raw_capabilities, "subagents.capabilities"))
    raw_model = value.get("model")
    return SubagentSettings(
        enabled=_boolean(value.get("enabled", True), "subagents.enabled"),
        model=_text(raw_model, "subagents.model") if raw_model is not None else None,
        max_tokens=_positive_int(value.get("max_tokens", 8_192), "subagents.max_tokens"),
        timeout_seconds=_positive_float(
            value.get("timeout_seconds", 600.0), "subagents.timeout_seconds"
        ),
        max_tool_calls=_positive_int(
            value.get("max_tool_calls", 100), "subagents.max_tool_calls"
        ),
        capabilities=capabilities,
        max_depth=_non_negative_int(value.get("max_depth", 1), "subagents.max_depth"),
        max_children=_non_negative_int(value.get("max_children", 3), "subagents.max_children"),
        types=_parse_subagent_types(value.get("types", {})),
    )


def _parse_subagent_types(value: object) -> dict[str, SubagentTypeSettings]:
    """Parse `[subagents.types.<name>]`, rejecting names with no definition."""

    from .subagents import SUBAGENT_TYPE_NAMES

    if not isinstance(value, dict):
        raise CodeAgentConfigError("subagents.types must be a TOML table")
    parsed: dict[str, SubagentTypeSettings] = {}
    for raw_name, raw_settings in value.items():
        name = _text(raw_name, "subagents.types")
        if name not in SUBAGENT_TYPE_NAMES:
            raise CodeAgentConfigError(f"Unknown subagent type: {name}")
        if not isinstance(raw_settings, dict):
            raise CodeAgentConfigError(f"subagents.types.{name} must be a TOML table")
        raw_model = raw_settings.get("model")
        parsed[name] = SubagentTypeSettings(
            enabled=_boolean(
                raw_settings.get("enabled", True), f"subagents.types.{name}.enabled"
            ),
            model=(
                _text(raw_model, f"subagents.types.{name}.model")
                if raw_model is not None
                else None
            ),
        )
    return parsed


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
    "SubagentSettings",
    "load_config",
    "resolve_env_mapping",
]
