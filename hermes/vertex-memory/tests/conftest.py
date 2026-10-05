"""Test fixtures + standalone stubs.

These tests run *outside* a Hermes checkout, so we stub the two Hermes modules
the plugin imports (`agent.memory_provider`, `tools.registry`) with minimal
shims. Inside a real Hermes environment the genuine modules take precedence
(this conftest only injects stubs if they're not already importable).
"""

from __future__ import annotations

import sys
import types


def _ensure_hermes_stubs() -> None:
    # agent.memory_provider.MemoryProvider — minimal ABC stand-in.
    try:
        import agent.memory_provider  # noqa: F401
    except Exception:
        agent_pkg = sys.modules.setdefault("agent", types.ModuleType("agent"))
        agent_pkg.__path__ = []  # mark as package
        mp = types.ModuleType("agent.memory_provider")

        class MemoryProvider:  # noqa: D401 - shim
            pass

        mp.MemoryProvider = MemoryProvider
        sys.modules["agent.memory_provider"] = mp
        setattr(agent_pkg, "memory_provider", mp)

    # tools.registry.tool_error — returns a JSON error string.
    try:
        import tools.registry  # noqa: F401
    except Exception:
        import json as _json
        tools_pkg = sys.modules.setdefault("tools", types.ModuleType("tools"))
        tools_pkg.__path__ = []
        reg = types.ModuleType("tools.registry")

        def tool_error(msg):
            return _json.dumps({"error": msg})

        reg.tool_error = tool_error
        sys.modules["tools.registry"] = reg
        setattr(tools_pkg, "registry", reg)

    # hermes_constants.get_hermes_home — temp dir.
    try:
        import hermes_constants  # noqa: F401
    except Exception:
        import tempfile
        from pathlib import Path
        hc = types.ModuleType("hermes_constants")
        _tmp = Path(tempfile.mkdtemp(prefix="hermes-home-"))
        hc.get_hermes_home = lambda: _tmp
        sys.modules["hermes_constants"] = hc


_ensure_hermes_stubs()
