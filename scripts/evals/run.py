"""Run AIHI evaluation gates locally or in CI.

The default modes are deliberately deterministic.  Live Coding Agent modes
require an explicit configuration file so a CI job cannot accidentally invoke
an external Provider with an implicit local configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
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
        code.assert_gate()
        print(
            f"{args.mode}: harness {harness.passed}/{harness.total} passed; "
            f"code {code.passed}/{code.total} passed"
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


__all__ = ["EXIT_GATE_FAILED", "EXIT_OK", "EXIT_SETUP_ERROR", "main"]
