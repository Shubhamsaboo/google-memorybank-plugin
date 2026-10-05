"""Tests for config.load_config()/save_config_file() — dashboard/legacy path
precedence and round-trip."""

from __future__ import annotations

import json

import config as config_mod


def test_load_config_reads_standard_dashboard_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "_hermes_home", lambda: tmp_path)
    standard = tmp_path / "vertex-memory" / "config.json"
    standard.parent.mkdir(parents=True)
    standard.write_text(json.dumps({"project_id": "from-dashboard"}))

    cfg = config_mod.load_config()
    assert cfg["project_id"] == "from-dashboard"


def test_load_config_falls_back_to_legacy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "_hermes_home", lambda: tmp_path)
    legacy = tmp_path / "vertex_memory.json"
    legacy.write_text(json.dumps({"project_id": "from-legacy"}))

    cfg = config_mod.load_config()
    assert cfg["project_id"] == "from-legacy"


def test_standard_path_wins_over_legacy(tmp_path, monkeypatch):
    """A dashboard save must win over a stale legacy file, not be shadowed by it."""
    monkeypatch.setattr(config_mod, "_hermes_home", lambda: tmp_path)
    (tmp_path / "vertex_memory.json").write_text(json.dumps({"project_id": "stale-legacy"}))
    standard = tmp_path / "vertex-memory" / "config.json"
    standard.parent.mkdir(parents=True)
    standard.write_text(json.dumps({"project_id": "fresh-dashboard"}))

    cfg = config_mod.load_config()
    assert cfg["project_id"] == "fresh-dashboard"


def test_save_config_file_writes_standard_path(tmp_path):
    config_mod.save_config_file({"project_id": "p", "location": "us-central1"}, str(tmp_path))
    standard = tmp_path / "vertex-memory" / "config.json"
    assert standard.exists()
    assert json.loads(standard.read_text())["project_id"] == "p"


def test_save_then_load_round_trip(tmp_path, monkeypatch):
    """What the admin dashboard writes, the running provider must read back."""
    monkeypatch.setattr(config_mod, "_hermes_home", lambda: tmp_path)
    config_mod.save_config_file(
        {"project_id": "roundtrip-proj", "reasoning_engine_id": "999"}, str(tmp_path))
    cfg = config_mod.load_config()
    assert cfg["project_id"] == "roundtrip-proj"
    assert cfg["reasoning_engine_id"] == "999"
