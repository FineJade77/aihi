"""Build and install both namespace wheels as release artifacts."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
PACKAGES = REPOSITORY / "packages" / "aihi"
SMOKE = REPOSITORY / "tests" / "integration" / "installed_wheel_smoke.py"


@pytest.fixture(scope="session")
def wheels(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    output = tmp_path_factory.mktemp("wheels")
    for package in (PACKAGES / "models", PACKAGES / "agent"):
        subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(output),
                str(package),
            ],
            cwd=REPOSITORY,
            check=True,
            capture_output=True,
            text=True,
        )
    return {
        "models": next(output.glob("aihi_models-*.whl")),
        "agent": next(output.glob("aihi_agent-*.whl")),
    }


def wheel_names(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as archive:
        return set(archive.namelist())


def clean_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    return environment


def isolated_environment(site_packages: Path) -> dict[str, str]:
    """Expose only the target venv and host tooling, never editable source paths."""

    environment = clean_environment()
    host_site_packages = tuple(
        dict.fromkeys(
            entry
            for entry in sys.path
            if entry
            and Path(entry).name in {"site-packages", "dist-packages"}
            and Path(entry).is_dir()
        )
    )
    environment["PYTHONPATH"] = os.pathsep.join((str(site_packages), *host_site_packages))
    return environment


def install_pure_wheel(wheel: Path, site_packages: Path) -> None:
    """Install a pure-Python wheel by applying its archive to site-packages."""

    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site_packages)


def uninstall_leaf(site_packages: Path, *, distribution: str, leaf: str) -> None:
    """Apply the wheel's leaf-scoped uninstall in the disposable environment."""

    shutil.rmtree(site_packages / "aihi" / leaf)
    for metadata in site_packages.glob(f"{distribution}-*.dist-info"):
        shutil.rmtree(metadata)


def test_wheels_contain_only_their_namespace_leaf(wheels: dict[str, Path]) -> None:
    for leaf, wheel in wheels.items():
        names = wheel_names(wheel)
        code = {name for name in names if ".dist-info/" not in name}
        assert code
        assert all(name.startswith(f"aihi/{leaf}/") for name in code)
        assert f"aihi/{leaf}/py.typed" in names
        assert "aihi/__init__.py" not in names
        assert not any(name.startswith(f"{leaf}/") for name in names)


def test_agent_wheel_declares_the_models_dependency(wheels: dict[str, Path]) -> None:
    with zipfile.ZipFile(wheels["agent"]) as archive:
        metadata_name = next(name for name in archive.namelist() if name.endswith("METADATA"))
        metadata = archive.read(metadata_name).decode("utf-8")
    requirements = [
        line.removeprefix("Requires-Dist:").strip().replace(" ", "")
        for line in metadata.splitlines()
        if line.startswith("Requires-Dist:")
    ]
    assert "aihi-models<0.2,>=0.1" in requirements


def test_installed_wheels_coexist_run_and_remain_typed(
    wheels: dict[str, Path], tmp_path: Path
) -> None:
    environment = clean_environment()
    virtualenv = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True, system_site_packages=False).create(virtualenv)
    python = virtualenv / "bin" / "python"
    site_packages = Path(
        subprocess.run(
            [str(python), "-c", "import site; print(site.getsitepackages()[0])"],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        ).stdout.strip()
    )
    isolated = isolated_environment(site_packages)
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "--disable-pip-version-check",
            "install",
            "--no-deps",
            "--no-compile",
            str(wheels["models"]),
            str(wheels["agent"]),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    subprocess.run(
        [str(python), "-S", str(SMOKE), str(tmp_path / "workspace")],
        check=True,
        capture_output=True,
        text=True,
        env=isolated,
    )

    probe = tmp_path / "typing_probe.py"
    probe.write_text(
        "from aihi.agent import RuntimeBuilder\n"
        "from aihi.models import Message\n"
        "message: Message = Message.text('user', 'typed')\n"
        "builder_type: type[RuntimeBuilder] = RuntimeBuilder\n",
        encoding="utf-8",
    )
    subprocess.run(
        [str(python), "-S", "-m", "mypy", "--strict", "--config-file", os.devnull, str(probe)],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=isolated,
    )

    uninstall_leaf(site_packages, distribution="aihi_agent", leaf="agent")
    subprocess.run(
        [str(python), "-S", "-c", "from aihi.models import Message; print(Message.__name__)"],
        check=True,
        capture_output=True,
        text=True,
        env=isolated,
    )

    install_pure_wheel(wheels["agent"], site_packages)
    uninstall_leaf(site_packages, distribution="aihi_models", leaf="models")
    subprocess.run(
        [
            str(python),
            "-S",
            "-c",
            "import importlib.util as u; assert u.find_spec('aihi.agent') is not None; "
            "assert u.find_spec('aihi.models') is None",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=isolated,
    )
