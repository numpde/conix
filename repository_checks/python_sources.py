"""Authored Python owners covered by runtime diagnostic posture checks."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def production_sources():
    roots = (ROOT / "src", ROOT / "tools")
    assert all(root.is_dir() for root in roots), "A production source owner disappeared."
    return sorted(path for root in roots for path in root.rglob("*.py"))
