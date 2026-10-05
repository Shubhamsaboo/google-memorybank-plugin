"""Tests for VertexMemoryBankProvider — provider lifecycle and tool dispatch."""

from __future__ import annotations

import json
import os
from unittest import mock

import pytest

# Import the package by loading __init__ as a top-level module.
import importlib.util
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent


def _load_provider_module():
    """Import the plugin's __init__.py as a flat module 'vertex_pkg'.

    The plugin's __init__ has a try/except so its `from .client` imports fall
    back to flat `from client` when loaded outside a package — which is exactly
    this path. client/config are already importable (root conftest adds the dir
    to sys.path)."""
    import sys
    if str(_PKG_DIR) not in sys.path:
        sys.path.insert(0, str(_PKG_DIR))
    if "vertex_pkg" in sys.modules:
        return sys.modules["vertex_pkg"]
    spec = importlib.util.spec_from_file_location("vertex_pkg", _PKG_DIR / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["vertex_pkg"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def provider_mod():
    return _load_provider_module()


@pytest.fixture
def provider(provider_mod):
    p = provider_mod.VertexMemoryBankProvider()
    p._cfg = {"project_id": "proj", "location": "us-central1",
              "reasoning_engine_id": "eng"}
    p._scope = {"user_id": "alan"}
    return p


def test_name(provider):
    assert provider.name == "vertex-memory"


def test_is_available_requires_config(provider_mod):
    p = provider_mod.VertexMemoryBankProvider()
    with mock.patch.object(provider_mod, "load_config", return_value={}):
        assert p.is_available() is False
    with mock.patch.object(provider_mod, "load_config", return_value={
            "project_id": "p", "location": "l", "reasoning_engine_id": "e"}):
        assert p.is_available() is True


def test_initialize_scope_from_user_id(provider_mod):
    p = provider_mod.VertexMemoryBankProvider()
    with mock.patch.object(provider_mod, "load_config", return_value={
            "project_id": "p", "location": "l", "reasoning_engine_id": "e",
            "scope_key": "user_id", "top_k": "10"}):
        p.initialize("sess1", user_id="alan")
    assert p._scope == {"user_id": "alan"}
    assert p._top_k == 10


def test_initialize_scope_fallback(provider_mod):
    p = provider_mod.VertexMemoryBankProvider()
    with mock.patch.object(provider_mod, "load_config", return_value={
            "project_id": "p", "location": "l", "reasoning_engine_id": "e"}):
        p.initialize("sess1")  # no user_id
    assert p._scope == {"user_id": "hermes-user"}


def test_get_tool_schemas(provider):
    names = {s["name"] for s in provider.get_tool_schemas()}
    assert names == {"memorybank_search", "memorybank_remember",
                     "memorybank_forget", "memorybank_correct", "memorybank_stats"}


def test_noise_filter_skips_trivial(provider):
    fake_client = mock.Mock()
    with mock.patch.object(provider, "_get_client", return_value=fake_client):
        provider.sync_turn("ok", "done")  # too short
    fake_client.generate_from_conversation.assert_not_called()


def test_sync_turn_captures_substantive(provider):
    fake_client = mock.Mock()
    with mock.patch.object(provider, "_get_client", return_value=fake_client):
        provider.sync_turn(
            "Please remember I prefer Elixir over Python for all new services",
            "Understood — I'll default to Elixir for new service work.")
        # join the daemon thread
        if provider._sync_thread:
            provider._sync_thread.join(timeout=5)
    fake_client.generate_from_conversation.assert_called_once()


def test_search_tool_dispatch(provider):
    fake_client = mock.Mock()
    fake_client.retrieve.return_value = [{"fact": "uses uv", "id": "1", "distance": 0.1}]
    with mock.patch.object(provider, "_get_client", return_value=fake_client):
        out = json.loads(provider.handle_tool_call("memorybank_search", {"query": "tools"}))
    assert out["count"] == 1
    assert out["results"][0]["fact"] == "uses uv"


def test_search_missing_query(provider):
    fake_client = mock.Mock()
    with mock.patch.object(provider, "_get_client", return_value=fake_client):
        out = json.loads(provider.handle_tool_call("memorybank_search", {}))
    assert "error" in out


def test_remember_tool_dispatch(provider):
    fake_client = mock.Mock()
    fake_client.generate_from_fact.return_value = {"created": 1, "updated": 0, "total": 1}
    with mock.patch.object(provider, "_get_client", return_value=fake_client):
        out = json.loads(provider.handle_tool_call(
            "memorybank_remember", {"fact": "deploys to us-central1"}))
    assert out["result"] == "Stored."
    fake_client.generate_from_fact.assert_called_once()


def test_forget_and_correct(provider):
    fake_client = mock.Mock()
    fake_client.correct.return_value = {"corrected": True, "method": "patch"}
    with mock.patch.object(provider, "_get_client", return_value=fake_client):
        out_f = json.loads(provider.handle_tool_call("memorybank_forget", {"memory_id": "9"}))
        out_c = json.loads(provider.handle_tool_call(
            "memorybank_correct", {"memory_id": "9", "fact": "new"}))
    fake_client.delete.assert_called_once_with("9", scope={"user_id": "alan"})
    fake_client.correct.assert_called_once()
    assert "Deleted" in out_f["result"]
    assert out_c == {"memory_id": "9", "corrected": True, "method": "patch"}


def test_forget_propagates_scope_guard_error(provider, provider_mod):
    """A cross-scope forget attempt must surface as a tool error, not succeed."""
    fake_client = mock.Mock()
    fake_client.delete.side_effect = provider_mod.MemoryBankError(
        "Refusing to mutate: memory does not belong to the configured scope.")
    with mock.patch.object(provider, "_get_client", return_value=fake_client):
        out = json.loads(provider.handle_tool_call("memorybank_forget", {"memory_id": "9"}))
    assert "error" in out
    assert "does not belong to the configured scope" in out["error"]


def test_stats_tool(provider):
    fake_client = mock.Mock()
    fake_client.count.return_value = 42
    with mock.patch.object(provider, "_get_client", return_value=fake_client):
        out = json.loads(provider.handle_tool_call("memorybank_stats", {}))
    assert out["total_memories"] == 42
    assert out["scope"] == {"user_id": "alan"}


def test_unknown_tool(provider):
    fake_client = mock.Mock()
    with mock.patch.object(provider, "_get_client", return_value=fake_client):
        out = json.loads(provider.handle_tool_call("nope", {}))
    assert "error" in out


def test_circuit_breaker_opens_and_blocks(provider, provider_mod):
    # Force the breaker open.
    provider._consecutive_failures = provider_mod._BREAKER_THRESHOLD
    provider._breaker_open_until = float("inf")
    out = json.loads(provider.handle_tool_call("memorybank_search", {"query": "x"}))
    assert "unavailable" in out["error"].lower()


def test_guard_refusals_do_not_trip_breaker(provider, provider_mod):
    """Cross-scope attempts are refused locally; they must not lock out the owner."""
    fake_client = mock.Mock()
    fake_client.delete.side_effect = provider_mod.MemoryBankError(
        "Refusing to mutate: memory does not belong to the configured scope.", guard=True)
    with mock.patch.object(provider, "_get_client", return_value=fake_client):
        for _ in range(provider_mod._BREAKER_THRESHOLD * 2):
            out = json.loads(provider.handle_tool_call("memorybank_forget", {"memory_id": "9"}))
            assert "configured scope" in out["error"]
    assert provider._consecutive_failures == 0
    assert not provider._is_breaker_open()


def test_backend_errors_still_trip_breaker(provider, provider_mod):
    fake_client = mock.Mock()
    fake_client.count.side_effect = provider_mod.MemoryBankError("Memory Bank API 503", status=503)
    with mock.patch.object(provider, "_get_client", return_value=fake_client):
        for _ in range(provider_mod._BREAKER_THRESHOLD):
            provider.handle_tool_call("memorybank_stats", {})
    assert provider._is_breaker_open()


def test_on_memory_write_mirrors(provider):
    fake_client = mock.Mock()
    with mock.patch.object(provider, "_get_client", return_value=fake_client):
        provider.on_memory_write("add", "user", "Alan prefers Elixir")
    fake_client.generate_from_fact.assert_called_once()


def test_on_memory_write_skips_remove(provider):
    fake_client = mock.Mock()
    with mock.patch.object(provider, "_get_client", return_value=fake_client):
        provider.on_memory_write("remove", "user", "stale")
    fake_client.generate_from_fact.assert_not_called()


def test_system_prompt_block(provider):
    block = provider.system_prompt_block()
    assert "Vertex AI Memory Bank" in block
    assert "user_id=alan" in block
