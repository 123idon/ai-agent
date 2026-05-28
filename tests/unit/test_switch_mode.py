"""Tests for switch_mode.py CLI."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

SCRIPT = Path(__file__).parents[2] / "scripts" / "switch_mode.py"


def _setup_repo(tmp_path: Path, *, current: str = "paper") -> Path:
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "mode.yaml").write_text(
        yaml.safe_dump({"current_mode": current, "history": []}),
        encoding="utf-8",
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    target = scripts / "switch_mode.py"
    target.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def test_switch_paper_to_live(tmp_path: Path) -> None:
    target = _setup_repo(tmp_path, current="paper")
    r = subprocess.run(
        [sys.executable, str(target), "--to", "live"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    doc = yaml.safe_load((tmp_path / "config" / "mode.yaml").read_text(encoding="utf-8"))
    assert doc["current_mode"] == "live"
    assert doc["history"][-1]["to"] == "live"


def test_switch_to_same_mode_is_noop(tmp_path: Path) -> None:
    target = _setup_repo(tmp_path, current="paper")
    r = subprocess.run(
        [sys.executable, str(target), "--to", "paper"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert r.returncode == 0
    assert "already" in r.stdout
