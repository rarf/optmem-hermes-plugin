"""Packaging contract: a real wheel must ship the package and the RIGHT entry point.

Baseline defect (proved by this file before the fix): ``py-modules = ["optmem"]``
for a *directory* package produced a wheel whose dist-info claims ``top_level
optmem`` but which contains **no module files at all**, and the entry point was
declared under the obsolete ``hermes.plugins`` group instead of the memory
provider group Hermes actually scans (``hermes_agent.memory_providers``).

These tests build a wheel with setuptools' build backend in an isolated copy of
the project (never touching the worktree) and then import the installed
package from the extracted wheel.
"""

from __future__ import annotations

import email
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

REQUIRED_MODULES = (
    "optmem/__init__.py",
    "optmem/engine.py",
    "optmem/config.py",
    "optmem/config_schema.py",
    "optmem/cli.py",
    "optmem/migrate.py",
)


def _build_wheel(tmp_path: Path) -> Path:
    """Build the project wheel from an isolated copy; return its path."""
    build_meta = pytest.importorskip("setuptools.build_meta")
    src = tmp_path / "src"
    src.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(PROJECT_ROOT / name, src / name)
    shutil.copytree(
        PROJECT_ROOT / "optmem",
        src / "optmem",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    out = tmp_path / "wheelhouse"
    out.mkdir()
    import os

    cwd = os.getcwd()
    os.chdir(src)
    try:
        filename = build_meta.build_wheel(str(out))
    finally:
        os.chdir(cwd)
    return out / filename


@pytest.fixture(scope="module")
def wheel(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("wheelbuild")
    return _build_wheel(tmp_path)


def test_wheel_ships_every_package_module(wheel: Path):
    names = set(zipfile.ZipFile(wheel).namelist())
    missing = [m for m in REQUIRED_MODULES if m not in names]
    assert not missing, f"wheel is missing package files: {missing}"


def test_wheel_ships_plugin_manifest(wheel: Path):
    names = set(zipfile.ZipFile(wheel).namelist())
    assert "optmem/plugin.yaml" in names


def test_wheel_declares_memory_provider_entry_point(wheel: Path):
    z = zipfile.ZipFile(wheel)
    entry_points = next(n for n in z.namelist() if n.endswith("entry_points.txt"))
    text = z.read(entry_points).decode()
    assert "[hermes_agent.memory_providers]" in text
    # Canonical registered provider name, not the bare upstream project key.
    assert "optmem-hermes = optmem" in text
    assert "optmem = optmem" not in text


def test_wheel_has_no_obsolete_plugin_entry_point(wheel: Path):
    z = zipfile.ZipFile(wheel)
    entry_points = next(n for n in z.namelist() if n.endswith("entry_points.txt"))
    assert "[hermes.plugins]" not in z.read(entry_points).decode()


def test_wheel_metadata_name_and_version(wheel: Path):
    z = zipfile.ZipFile(wheel)
    meta = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
    msg = email.message_from_bytes(z.read(meta))
    assert msg["Name"] == "optmem-hermes-plugin"
    from optmem import __version__

    assert msg["Version"] == __version__


def test_installed_package_imports_from_wheel(tmp_path, wheel: Path):
    """The extracted wheel must be importable as a package and construct a provider."""
    extract = tmp_path / "site"
    extract.mkdir()
    with zipfile.ZipFile(wheel) as z:
        z.extractall(extract)
    # Fail for the right reason: the wheel itself must contain the package. Without
    # this, a missing module falls through to the worktree copy on sys.path and the
    # import test passes while the wheel is empty.
    assert (extract / "optmem" / "__init__.py").is_file(), "wheel did not ship the package"
    for mod in [m for m in list(sys.modules) if m == "optmem" or m.startswith("optmem.")]:
        sys.modules.pop(mod, None)
    sys.path.insert(0, str(extract))
    try:
        import optmem

        assert Path(optmem.__file__).resolve().is_relative_to(extract.resolve())
        provider = optmem.OptMemProvider()
        names = {s["name"] for s in provider.get_tool_schemas()}
        assert "optmem_note" in names
    finally:
        sys.path.remove(str(extract))
        for mod in [m for m in list(sys.modules) if m == "optmem" or m.startswith("optmem.")]:
            sys.modules.pop(mod, None)
