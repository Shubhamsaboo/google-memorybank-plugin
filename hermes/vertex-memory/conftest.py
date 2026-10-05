"""Root conftest: runs before any test/collection.

1. Put the package dir on sys.path.
2. Stub the Hermes-only modules (agent.memory_provider, tools.registry,
   hermes_constants) so the plugin's __init__.py imports cleanly even when
   pytest imports it as the parent package of `client`/`config`.
"""

import os
import sys
import types

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _ensure_hermes_stubs() -> None:
    try:
        import agent.memory_provider  # noqa: F401
    except Exception:
        agent_pkg = sys.modules.setdefault("agent", types.ModuleType("agent"))
        agent_pkg.__path__ = []
        mp = types.ModuleType("agent.memory_provider")

        class MemoryProvider:  # shim base
            pass

        mp.MemoryProvider = MemoryProvider
        sys.modules["agent.memory_provider"] = mp
        agent_pkg.memory_provider = mp

    try:
        import tools.registry  # noqa: F401
    except Exception:
        import json as _json
        tools_pkg = sys.modules.setdefault("tools", types.ModuleType("tools"))
        tools_pkg.__path__ = []
        reg = types.ModuleType("tools.registry")
        reg.tool_error = lambda msg: _json.dumps({"error": msg})
        sys.modules["tools.registry"] = reg
        tools_pkg.registry = reg

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
