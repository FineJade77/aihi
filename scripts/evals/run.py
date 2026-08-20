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
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
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
from aihi.agent.policy import PermissionMode  # noqa: E402
from aihi.code_agent import CodeAgentEvalRunner, CodeTaskDataset, load_config  # noqa: E402
from aihi.code_agent.evals import (  # noqa: E402
    CodeEvalGateFailed,
    CodeEvalReport,
    CodeTaskResult,
)

from scripts.evals.context_baseline import context_reference_executor  # noqa: E402
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
        action="append",
        help=(
            "explicit Code Agent TOML config; repeat for a multi-model "
            "nightly/release comparison"
        ),
    )
    parser.add_argument(
        "--repeat",
        type=_positive_int,
        help="attempts per task (default: 1 for PR, 3 for nightly/release)",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        help=(
            "explicit benchmark baseline JSON; by default PR uses the scripted "
            "baseline and live modes select a reviewed provider/model baseline"
        ),
    )
    return parser


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


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


def repeat_dataset(dataset: CodeTaskDataset, repetitions: int) -> CodeTaskDataset:
    """Override manifest repetition for a reproducible live sampling profile."""

    if isinstance(repetitions, bool) or not isinstance(repetitions, int) or repetitions <= 0:
        raise ValueError("repetitions must be a positive integer")
    return CodeTaskDataset(
        dataset.dataset_id,
        tuple(replace(task, repeat=repetitions) for task in dataset.tasks),
    )


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
    if getattr(config, "permission_mode", None) is not PermissionMode.BYPASS:
        raise ValueError(
            "live evaluation requires agent.permission_mode = \"bypass\" for "
            "non-interactive process execution"
        )
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


def validate_docker_daemon() -> None:
    """Fail before a billable model call when Docker execution is unavailable."""

    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            check=False,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("live evaluation requires a reachable Docker daemon") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError("live evaluation requires a reachable Docker daemon")


def compare_baseline(report: CodeEvalReport, baseline: Mapping[str, object]) -> dict[str, object]:
    """Compare dataset shape and outcome counts without hiding live results."""

    comparison_kind = baseline.get("artifact_kind", "scripted_reference")
    if not isinstance(comparison_kind, str) or not comparison_kind.strip():
        raise ValueError("baseline.artifact_kind must be a non-empty string")
    baseline_dataset = baseline.get("dataset_id")
    if baseline_dataset != report.dataset_id:
        raise ValueError("baseline dataset_id does not match the evaluation dataset")
    if comparison_kind == "reviewed_live_baseline":
        baseline_profile = (baseline.get("provider"), baseline.get("model"))
        report_profile = (report.config.get("provider"), report.config.get("model"))
        if baseline_profile != report_profile:
            raise ValueError("live baseline provider/model does not match the report")
    raw_case_ids = baseline.get("case_ids")
    if not isinstance(raw_case_ids, list) or any(
        not isinstance(item, str) for item in raw_case_ids
    ):
        raise ValueError("baseline.case_ids must be a list of strings")
    actual_case_ids: list[str] = []
    for result in report.results:
        raw_base = result.metrics.get("base_case_id")
        case_id = (
            raw_base.strip()
            if isinstance(raw_base, str) and raw_base.strip()
            else result.case_id
        )
        if case_id not in actual_case_ids:
            actual_case_ids.append(case_id)
    if actual_case_ids != raw_case_ids:
        raise ValueError("baseline case_ids do not match the evaluation dataset")
    raw_summary = baseline.get("summary")
    if not isinstance(raw_summary, Mapping):
        raise ValueError("baseline.summary must be an object")
    baseline_pass_at_1 = raw_summary.get("pass_at_1", raw_summary.get("pass_rate"))
    if not isinstance(baseline_pass_at_1, (int, float)) or isinstance(
        baseline_pass_at_1, bool
    ):
        raise ValueError("baseline.summary.pass_at_1 or pass_rate must be numeric")
    actual_summary = report.summary()
    actual_pass_at_1 = actual_summary["pass_at_1"]
    if not isinstance(actual_pass_at_1, (int, float)) or isinstance(
        actual_pass_at_1, bool
    ):
        raise ValueError("report summary pass_at_1 must be numeric")
    pass_at_1_delta = float(actual_pass_at_1) - float(baseline_pass_at_1)
    if abs(pass_at_1_delta) < 1e-12:
        pass_at_1_delta = 0.0
    return {
        "comparison_kind": comparison_kind,
        "baseline_version": baseline.get("baseline_version"),
        "dataset_id": report.dataset_id,
        "baseline": {
            "total": raw_summary.get("total"),
            "passed": raw_summary.get("passed"),
            "failed": raw_summary.get("failed"),
            "pass_rate": raw_summary.get("pass_rate"),
            "pass_at_1": float(baseline_pass_at_1),
        },
        "actual": {
            "total": report.total,
            "base_cases": actual_summary["base_cases"],
            "passed": report.passed,
            "failed": report.failed,
            "pass_rate": report.pass_rate,
            "pass_at_1": float(actual_pass_at_1),
            "pass_at_least_once": actual_summary["pass_at_least_once"],
            "stable_pass_rate": actual_summary["stable_pass_rate"],
        },
        "delta": {
            "pass_at_1": pass_at_1_delta,
        },
        "case_ids_match": True,
    }


def select_baseline(
    report: CodeEvalReport,
    benchmark_root: Path,
    explicit_path: Path | None = None,
) -> Mapping[str, object] | None:
    """Select the scripted PR baseline or a reviewed live profile baseline."""

    if explicit_path is not None:
        return _read_json_object(explicit_path)
    provider = report.config.get("provider")
    model = report.config.get("model")
    if not isinstance(provider, str) or not isinstance(model, str):
        return _read_json_object(benchmark_root / "baseline.json")
    matches: list[Mapping[str, object]] = []
    for path in sorted((benchmark_root / "baselines").glob("*.json")):
        payload = _read_json_object(path)
        if (
            payload.get("artifact_kind") == "reviewed_live_baseline"
            and payload.get("provider") == provider
            and payload.get("model") == model
        ):
            matches.append(payload)
    if len(matches) > 1:
        raise ValueError(f"multiple reviewed live baselines match {provider}/{model}")
    return matches[0] if matches else None


def _read_json_object(path: Path) -> Mapping[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"baseline must be a JSON object: {path}")
    return payload


def _unbaselined_comparison(report: CodeEvalReport) -> dict[str, object]:
    summary = report.summary()
    return {
        "comparison_kind": "unbaselined",
        "dataset_id": report.dataset_id,
        "baseline": None,
        "actual": {
            "total": report.total,
            "base_cases": summary["base_cases"],
            "passed": report.passed,
            "failed": report.failed,
            "pass_rate": report.pass_rate,
            "pass_at_1": summary["pass_at_1"],
            "pass_at_least_once": summary["pass_at_least_once"],
            "stable_pass_rate": summary["stable_pass_rate"],
        },
        "delta": None,
        "case_ids_match": None,
    }


def assert_baseline_gate(
    report: CodeEvalReport, comparison: Mapping[str, object]
) -> None:
    """Reject a live result only when empirical pass@1 regresses."""

    baseline = comparison.get("baseline")
    actual = comparison.get("actual")
    if not isinstance(baseline, Mapping) or not isinstance(actual, Mapping):
        raise ValueError("reviewed baseline comparison is incomplete")
    baseline_pass_at_1 = baseline.get("pass_at_1")
    actual_pass_at_1 = actual.get("pass_at_1")
    if (
        not isinstance(baseline_pass_at_1, (int, float))
        or isinstance(baseline_pass_at_1, bool)
        or not isinstance(actual_pass_at_1, (int, float))
        or isinstance(actual_pass_at_1, bool)
    ):
        raise ValueError("baseline gate requires numeric pass_at_1 values")
    if float(actual_pass_at_1) + 1e-12 < float(baseline_pass_at_1):
        failed = [result.case_id for result in report.results if not result.passed]
        raise CodeEvalGateFailed(
            "Coding Agent pass@1 "
            f"{float(actual_pass_at_1):.3f} is below reviewed baseline "
            f"{float(baseline_pass_at_1):.3f}: {', '.join(failed)}"
        )


def compare_context_report(report: CodeEvalReport) -> dict[str, object]:
    """Compare the deterministic long-session baseline with hard compaction."""

    if report.dataset_id != "aihi-code-agent-context-v1":
        raise ValueError("context comparison requires aihi-code-agent-context-v1")
    by_id = {result.case_id: result for result in report.results}
    try:
        baseline = by_id["long-session-uncompacted"]
        compacted = by_id["long-session-compacted"]
    except KeyError as exc:
        raise ValueError("context report requires baseline and compacted cases") from exc

    def integer(result: CodeTaskResult, name: str) -> int:
        raw = result.metrics.get(name, 0)
        return (
            raw
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0
            else 0
        )

    def number(result: CodeTaskResult, name: str) -> float:
        raw = result.metrics.get(name, 0.0)
        return (
            float(raw)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0
            else 0.0
        )

    baseline_tokens = integer(baseline, "input_tokens")
    compacted_tokens = integer(compacted, "input_tokens")
    token_delta = compacted_tokens - baseline_tokens
    baseline_key = baseline.metrics.get("cache_key_hash")
    compacted_key = compacted.metrics.get("cache_key_hash")

    def snapshot(result: CodeTaskResult) -> dict[str, object]:
        return {
            "passed": result.passed,
            "duration_seconds": number(result, "duration_seconds"),
            "input_tokens": integer(result, "input_tokens"),
            "cached_input_tokens": integer(result, "cached_input_tokens"),
            "cache_hit_ratio": number(result, "cache_hit_ratio"),
            "cache_key_change_count": integer(result, "cache_key_change_count"),
            "compaction_count": integer(result, "compaction_count"),
            "hard_compaction_count": integer(result, "hard_compaction_count"),
            "soft_compaction_count": integer(result, "soft_compaction_count"),
            "critical_state_recall": number(result, "critical_state_recall"),
        }

    return {
        "comparison_version": 1,
        "dataset_id": report.dataset_id,
        "task_success_rate": report.pass_rate,
        "baseline": snapshot(baseline),
        "compacted": snapshot(compacted),
        "input_token_delta": token_delta,
        "input_token_reduction_ratio": (
            -token_delta / baseline_tokens if baseline_tokens else 0.0
        ),
        "latency_delta_seconds": (
            number(compacted, "duration_seconds")
            - number(baseline, "duration_seconds")
        ),
        "stable_cache_family": (
            isinstance(baseline_key, str)
            and bool(baseline_key)
            and baseline_key == compacted_key
        ),
    }


def assert_context_gate(
    report: CodeEvalReport, comparison: Mapping[str, object]
) -> None:
    """Enforce semantic success before accepting cache or token improvements."""

    report.assert_gate()
    baseline = comparison.get("baseline")
    compacted = comparison.get("compacted")
    if not isinstance(baseline, Mapping) or not isinstance(compacted, Mapping):
        raise ValueError("context comparison is incomplete")
    if comparison.get("stable_cache_family") is not True:
        raise CodeEvalGateFailed("Context evaluation cache family changed after compaction")
    if int(compacted.get("hard_compaction_count", 0)) < 1:
        raise CodeEvalGateFailed("Context evaluation did not exercise hard compaction")
    if int(compacted.get("input_tokens", 0)) >= int(baseline.get("input_tokens", 0)):
        raise CodeEvalGateFailed("Context evaluation did not reduce input tokens")
    if int(compacted.get("cached_input_tokens", 0)) <= 0:
        raise CodeEvalGateFailed("Context evaluation did not observe a cache hit")
    if any(
        int(profile.get("cache_key_change_count", 0)) != 0
        for profile in (baseline, compacted)
    ):
        raise CodeEvalGateFailed("Context evaluation changed cache key within the task")
    if any(
        float(profile.get("critical_state_recall", 0.0)) != 1.0
        for profile in (baseline, compacted)
    ):
        raise CodeEvalGateFailed("Context evaluation lost critical state")


def build_live_summary(reports: tuple[CodeEvalReport, ...]) -> dict[str, object]:
    """Build one credential-free comparison artifact for multiple live models."""

    if not reports:
        raise ValueError("live summary requires at least one report")
    dataset_id = reports[0].dataset_id
    mode = reports[0].mode
    if any(report.dataset_id != dataset_id or report.mode != mode for report in reports):
        raise ValueError("live summary reports must share dataset_id and mode")
    profiles: list[dict[str, object]] = []
    for report in reports:
        provider = report.config.get("provider")
        model = report.config.get("model")
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("live report is missing its provider")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("live report is missing its model")
        profiles.append(
            {
                "provider": provider,
                "model": model,
                "summary": report.summary(),
            }
        )
    return {
        "summary_version": 1,
        "dataset_id": dataset_id,
        "mode": mode,
        "generated_at": datetime.now(UTC).isoformat(),
        "profile_count": len(profiles),
        "profiles": profiles,
    }


async def _code_report(
    mode: str, config_path: Path | None, repetitions: int
) -> CodeEvalReport:
    benchmark_root = REPO_ROOT / "evals" / "aihi_code_agent" / "v1"
    dataset = CodeTaskDataset.from_jsonl(
        "aihi-code-agent-benchmark-v1",
        (benchmark_root / "manifest.jsonl").read_text(encoding="utf-8"),
        base_dir=benchmark_root,
    )
    dataset = repeat_dataset(dataset, repetitions)
    if mode == "pr":
        runner = CodeAgentEvalRunner(executor=reference_executor)
    else:
        if config_path is None:
            raise ValueError(f"--config is required for {mode} mode")
        config = load_config(config_path, cwd=REPO_ROOT)
        validate_live_config(config)
        runner = CodeAgentEvalRunner(config=config)
    return await runner.run_dataset(dataset, mode=mode)


async def _context_report(mode: str) -> CodeEvalReport:
    benchmark_root = REPO_ROOT / "evals" / "aihi_code_agent" / "context-v1"
    dataset = CodeTaskDataset.from_jsonl(
        "aihi-code-agent-context-v1",
        (benchmark_root / "manifest.jsonl").read_text(encoding="utf-8"),
        base_dir=benchmark_root,
    )
    return await CodeAgentEvalRunner(executor=context_reference_executor).run_dataset(
        dataset, mode=mode
    )


def _profile_slug(report: CodeEvalReport, index: int) -> str:
    provider = str(report.config.get("provider", "provider"))
    model = str(report.config.get("model", "model"))
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{provider}-{model}").strip("-.")
    return f"{index:02d}-{safe[:80] or 'profile'}"


async def _run(args: argparse.Namespace) -> int:
    output = args.output / args.mode if args.output.name != args.mode else args.output
    try:
        harness = _harness_report()
        _write_json(output / "harness.json", harness.to_dict())
        harness.assert_gate()
        if args.mode == "offline":
            print(f"offline: harness {harness.passed}/{harness.total} passed")
            return EXIT_OK

        config_paths = tuple(args.config or ())
        if args.mode == "pr" and config_paths:
            raise ValueError("--config is only valid for nightly/release")
        if args.mode in {"nightly", "release"} and not config_paths:
            raise ValueError(f"--config is required for {args.mode} mode")
        # Validate the whole matrix before the first billable Provider call so
        # one bad profile cannot fail only after earlier profiles have spent.
        for config_path in config_paths:
            validate_live_config(load_config(config_path, cwd=REPO_ROOT))
        if config_paths:
            validate_docker_daemon()
        context = await _context_report(args.mode)
        context_comparison = compare_context_report(context)
        _write_json(output / "context.json", context.to_dict())
        _write_json(output / "context-comparison.json", context_comparison)
        assert_context_gate(context, context_comparison)
        repetitions = args.repeat or (1 if args.mode == "pr" else 3)
        reports: tuple[CodeEvalReport, ...]
        if args.mode == "pr":
            reports = (await _code_report(args.mode, None, repetitions),)
        else:
            reports = tuple(
                [
                    await _code_report(args.mode, config_path, repetitions)
                    for config_path in config_paths
                ]
            )
        benchmark_root = REPO_ROOT / "evals" / "aihi_code_agent" / "v1"
        baselines = tuple(
            select_baseline(report, benchmark_root, args.baseline) for report in reports
        )
        comparisons = tuple(
            compare_baseline(report, baseline)
            if baseline is not None
            else _unbaselined_comparison(report)
            for report, baseline in zip(reports, baselines, strict=True)
        )
        if len(reports) == 1:
            _write_json(output / "code.json", reports[0].to_dict())
            _write_json(output / "baseline-comparison.json", comparisons[0])
        else:
            for index, (report, comparison) in enumerate(
                zip(reports, comparisons, strict=True), start=1
            ):
                profile = output / "profiles" / _profile_slug(report, index)
                _write_json(profile / "code.json", report.to_dict())
                _write_json(profile / "baseline-comparison.json", comparison)
        if args.mode in {"nightly", "release"}:
            _write_json(output / "live-summary.json", build_live_summary(reports))
        for report, comparison in zip(reports, comparisons, strict=True):
            summary = report.summary()
            delta = comparison["delta"]
            prefix = (
                f"{args.mode}: {report.config.get('provider', 'reference')}/"
                f"{report.config.get('model', 'reference')} "
                f"code {report.passed}/{report.total}, "
                f"pass@1 {float(summary['pass_at_1']):.3f}"
            )
            if isinstance(delta, Mapping):
                print(f"{prefix}, baseline delta {float(delta['pass_at_1']):+.3f}")
            else:
                print(f"{prefix}, baseline unavailable")
        for report, comparison in zip(reports, comparisons, strict=True):
            if comparison.get("comparison_kind") == "reviewed_live_baseline":
                assert_baseline_gate(report, comparison)
            else:
                report.assert_gate()
        print(
            f"{args.mode}: context {context.passed}/{context.total} passed, "
            f"input token delta {int(context_comparison['input_token_delta']):+d}"
        )
        print(f"{args.mode}: harness {harness.passed}/{harness.total} passed")
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
    "assert_baseline_gate",
    "assert_context_gate",
    "build_live_summary",
    "compare_baseline",
    "compare_context_report",
    "main",
    "repeat_dataset",
    "select_baseline",
    "validate_docker_daemon",
    "validate_live_config",
]
