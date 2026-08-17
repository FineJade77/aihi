"""Run AIHI evaluation gates locally or in CI.

The default modes are deliberately deterministic.  Live Coding Agent modes
require an explicit configuration file so a CI job cannot accidentally invoke
an external Provider with an implicit local configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Keep the script usable from a clean checkout without requiring an editable
# install.  CI still sets PYTHONPATH explicitly to make the package boundary
# visible in logs.
for source_root in (
    REPO_ROOT / "packages" / "aihi" / "models" / "src",
    REPO_ROOT / "packages" / "aihi" / "agent" / "src",
    REPO_ROOT / "packages" / "aihi" / "code-agent" / "src",
):
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

from aihi.agent.evals import (  # noqa: E402
    EvalDataset,
    HarnessConformanceReport,
    HarnessConformanceRunner,
)
from aihi.agent.evals.errors import EvalGateFailed  # noqa: E402
from aihi.code_agent import CodeAgentEvalRunner, CodeTaskDataset, load_config  # noqa: E402
from aihi.code_agent.evals import CodeEvalGateFailed, CodeEvalReport  # noqa: E402

from scripts.evals.reference_baseline import reference_executor  # noqa: E402

MODES = ("offline", "pr", "nightly", "release")
EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_SETUP_ERROR = 2
_LIVE_PROVIDERS = frozenset({"openai", "anthropic", "deepseek", "openai_compatible"})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "eval-results",
        help="directory for machine-readable reports",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="explicit Code Agent TOML config (required for nightly/release)",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        help="benchmark baseline JSON (defaults to the v1 committed baseline)",
    )
    return parser


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
    )


def _harness_report() -> HarnessConformanceReport:
    manifest = REPO_ROOT / "evals" / "aihi_agent" / "v1" / "manifest.jsonl"
    dataset = EvalDataset.from_jsonl(
        "aihi-agent-conformance-v1", manifest.read_text(encoding="utf-8")
    )
    return HarnessConformanceRunner().run_dataset(dataset)


def validate_live_config(
    config: object, *, environment: Mapping[str, str] | None = None
) -> None:
    """Fail closed before a nightly/release run can invoke a Provider."""

    provider = getattr(config, "provider", None)
    sandbox = getattr(config, "sandbox", None)
    if provider is None or sandbox is None:
        raise ValueError("live evaluation requires a resolved Code Agent config")
    provider_name = str(getattr(provider, "name", "")).replace("-", "_").lower()
    if provider_name not in _LIVE_PROVIDERS:
        raise ValueError(
            "nightly/release evaluation requires a real Provider; "
            "fake is only valid for offline/PR smoke runs"
        )
    model = str(getattr(provider, "model", "")).strip()
    if not model or model.startswith("REPLACE_WITH_"):
        raise ValueError("live evaluation requires an explicit provider model")
    key_env = getattr(provider, "api_key_env", None)
    if not isinstance(key_env, str) or not key_env.strip():
        raise ValueError("live evaluation requires provider.api_key_env")
    env = os.environ if environment is None else environment
    if not env.get(key_env):
        raise ValueError(f"Provider credential environment variable is missing: {key_env}")
    if getattr(sandbox, "backend", None) != "docker":
        raise ValueError("live evaluation requires the Docker sandbox")
    if not getattr(sandbox, "image", None):
        raise ValueError("live evaluation requires sandbox.image")
    if getattr(sandbox, "allow_network", True) is not False:
        raise ValueError("live evaluation requires sandbox.allow_network = false")
    if getattr(sandbox, "network", None) != "none":
        raise ValueError("live evaluation requires sandbox.network = \"none\"")
    if getattr(config, "mcp_servers", ()):
        raise ValueError("live evaluation does not allow configured MCP servers")


def compare_baseline(report: CodeEvalReport, baseline: Mapping[str, object]) -> dict[str, object]:
    """Compare dataset shape and outcome counts without hiding live results."""

    baseline_dataset = baseline.get("dataset_id")
    if baseline_dataset != report.dataset_id:
        raise ValueError("baseline dataset_id does not match the evaluation dataset")
    raw_case_ids = baseline.get("case_ids")
    if not isinstance(raw_case_ids, list) or any(
        not isinstance(item, str) for item in raw_case_ids
    ):
        raise ValueError("baseline.case_ids must be a list of strings")
    actual_case_ids = [result.case_id for result in report.results]
    if actual_case_ids != raw_case_ids:
        raise ValueError("baseline case_ids do not match the evaluation dataset")
    raw_summary = baseline.get("summary")
    if not isinstance(raw_summary, Mapping):
        raise ValueError("baseline.summary must be an object")
    baseline_pass_rate = raw_summary.get("pass_rate")
    if not isinstance(baseline_pass_rate, (int, float)) or isinstance(baseline_pass_rate, bool):
        raise ValueError("baseline.summary.pass_rate must be numeric")
    return {
        "baseline_version": baseline.get("baseline_version"),
        "dataset_id": report.dataset_id,
        "baseline": {
            "total": raw_summary.get("total"),
            "passed": raw_summary.get("passed"),
            "failed": raw_summary.get("failed"),
            "pass_rate": float(baseline_pass_rate),
        },
        "actual": {
            "total": report.total,
            "passed": report.passed,
            "failed": report.failed,
            "pass_rate": report.pass_rate,
        },
        "delta": {
            "passed": report.passed - int(raw_summary.get("passed", 0)),
            "pass_rate": report.pass_rate - float(baseline_pass_rate),
        },
        "case_ids_match": True,
    }


async def _code_report(mode: str, config_path: Path | None) -> CodeEvalReport:
    benchmark_root = REPO_ROOT / "evals" / "aihi_code_agent" / "v1"
    dataset = CodeTaskDataset.from_jsonl(
        "aihi-code-agent-benchmark-v1",
        (benchmark_root / "manifest.jsonl").read_text(encoding="utf-8"),
        base_dir=benchmark_root,
    )
    if mode == "pr":
        runner = CodeAgentEvalRunner(executor=reference_executor)
    else:
        if config_path is None:
            raise ValueError(f"--config is required for {mode} mode")
        config = load_config(config_path, cwd=REPO_ROOT)
        validate_live_config(config)
        runner = CodeAgentEvalRunner(config=config)
    return await runner.run_dataset(dataset, mode=mode)


async def _run(args: argparse.Namespace) -> int:
    output = args.output / args.mode if args.output.name != args.mode else args.output
    try:
        harness = _harness_report()
        _write_json(output / "harness.json", harness.to_dict())
        harness.assert_gate()
        if args.mode == "offline":
            print(f"offline: harness {harness.passed}/{harness.total} passed")
            return EXIT_OK

        code = await _code_report(args.mode, args.config)
        _write_json(output / "code.json", code.to_dict())
        benchmark_root = REPO_ROOT / "evals" / "aihi_code_agent" / "v1"
        baseline_path = args.baseline or benchmark_root / "baseline.json"
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        comparison = compare_baseline(code, baseline)
        _write_json(output / "baseline-comparison.json", comparison)
        code.assert_gate()
        delta = comparison["delta"]
        if not isinstance(delta, Mapping):  # pragma: no cover - compare_baseline owns this shape
            raise ValueError("baseline comparison delta must be an object")
        print(
            f"{args.mode}: harness {harness.passed}/{harness.total} passed; "
            f"code {code.passed}/{code.total} passed; "
            f"baseline delta {int(delta['passed']):+d}"
        )
        return EXIT_OK
    except (EvalGateFailed, CodeEvalGateFailed) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_GATE_FAILED
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"evaluation setup failed: {exc}", file=sys.stderr)
        return EXIT_SETUP_ERROR
    except Exception as exc:
        print(f"evaluation setup failed: {exc}", file=sys.stderr)
        return EXIT_SETUP_ERROR


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXIT_GATE_FAILED",
    "EXIT_OK",
    "EXIT_SETUP_ERROR",
    "compare_baseline",
    "main",
    "validate_live_config",
]
