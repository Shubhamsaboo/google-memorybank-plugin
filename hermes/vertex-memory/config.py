"""Config loading/saving for the Vertex Memory Bank plugin.

Precedence: environment variables provide defaults; the legacy
$HERMES_HOME/vertex_memory.json and the standard
$HERMES_HOME/vertex-memory/config.json (dashboard-writable) override
individual keys, with the standard path taking priority. No secrets are
stored — auth is via ADC.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_ENV_MAP = {
    "project_id": "VERTEX_MEMORY_PROJECT_ID",
    "location": "VERTEX_MEMORY_LOCATION",
    "reasoning_engine_id": "VERTEX_MEMORY_ENGINE_ID",
    "scope_key": "VERTEX_MEMORY_SCOPE_KEY",
    "scope_value": "VERTEX_MEMORY_SCOPE_VALUE",
    "top_k": "VERTEX_MEMORY_TOP_K",
    "max_distance": "VERTEX_MEMORY_MAX_DISTANCE",
}

_DEFAULTS = {
    "location": "us-central1",
    "scope_key": "user_id",
    "top_k": "10",
}

_CONFIG_FILENAME = "vertex_memory.json"


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home()
    except Exception:
        return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


def _standard_config_path() -> Path:
    """$HERMES_HOME/vertex-memory/config.json — the STORAGE_FLAT_JSON convention
    every other declarative-schema memory provider uses (see config_schema.py /
    hermes_cli/web_routers/memory_providers.py:_flat_json_path). The admin
    dashboard writes here; load_config() must read from the same place or a
    value saved via the dashboard silently never reaches the running provider.
    """
    return _hermes_home() / "vertex-memory" / "config.json"


def _legacy_config_path() -> Path:
    """Pre-dashboard config location. Kept as a read-only fallback so an
    existing $HERMES_HOME/vertex_memory.json from before the config_schema.py
    dashboard integration keeps working without a manual migration step."""
    return _hermes_home() / _CONFIG_FILENAME


def load_config() -> dict:
    """Merge defaults < env vars < legacy vertex_memory.json < standard
    vertex-memory/config.json (dashboard-writable; highest precedence so a
    value edited in the admin UI always wins over a stale legacy file)."""
    cfg = dict(_DEFAULTS)
    for key, env in _ENV_MAP.items():
        val = os.environ.get(env)
        if val:
            cfg[key] = val

    for cfg_path in (_legacy_config_path(), _standard_config_path()):
        if cfg_path.exists():
            try:
                file_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                cfg.update({k: v for k, v in file_cfg.items()
                            if v is not None and v != ""})
            except Exception:
                pass
    return cfg


def save_config_file(values: dict, hermes_home: str) -> None:
    """Persist non-secret config to $HERMES_HOME/vertex-memory/config.json
    (the standard path load_config() and the admin dashboard both use)."""
    cfg_path = Path(hermes_home) / "vertex-memory" / "config.json"
    existing = {}
    if cfg_path.exists():
        try:
            existing = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    existing.update({k: v for k, v in values.items() if v not in (None, "")})
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
