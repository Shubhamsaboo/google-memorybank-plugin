"""Vertex AI Memory Bank — Hermes MemoryProvider plugin.

Cross-session, cross-agent persistent memory backed by Google Cloud
Vertex AI Memory Bank (Agent Engine). Memories are extracted *facts*
(not raw transcripts), consolidated server-side with automatic
deduplication and contradiction resolution.

Native Hermes counterpart of this repo's OpenClaw plugin and Hermes MCP
server; mirrors memorybank-core.ts for scope/ownership guards and safe
correction.

Config (admin dashboard Memory panel, `hermes memory setup`, or
$HERMES_HOME/vertex-memory/config.json; legacy $HERMES_HOME/vertex_memory.json
is still read):
  project_id          — GCP project id (required)
  location            — GCP region, e.g. us-central1 (required)
  reasoning_engine_id — Agent Engine reasoning engine id (required)
  scope_key           — scope dimension, default "user_id"
  top_k               — max memories per recall (default 10)
  max_distance        — relevance cutoff (lower=stricter), optional

Auth: Application Default Credentials (ADC). No secret stored by Hermes.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

try:  # normal: imported as a package (Hermes plugin)
    from .client import VertexMemoryBankClient, MemoryBankError
    from .config import load_config, save_config_file
except ImportError:  # fallback: imported flat (tests / standalone)
    from client import VertexMemoryBankClient, MemoryBankError
    from config import load_config, save_config_file

logger = logging.getLogger(__name__)

# Circuit breaker — pause API calls after repeated failures.
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_SECS = 120

# Noise filter thresholds for auto-capture (mirrors openclaw behaviour).
_MIN_USER_CHARS = 20
_MIN_TOTAL_CHARS = 100


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

SEARCH_SCHEMA = {
    "name": "memorybank_search",
    "description": (
        "Semantic search over long-term memory (Vertex AI Memory Bank). "
        "Returns relevant facts ranked by similarity, with memory IDs and "
        "scores. Use to recall the user's preferences, decisions, project "
        "context, or anything established in past sessions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to recall."},
            "top_k": {"type": "integer", "description": "Max results (default 10, max 50)."},
        },
        "required": ["query"],
    },
}

REMEMBER_SCHEMA = {
    "name": "memorybank_remember",
    "description": (
        "Store a durable fact in long-term memory. Goes through Memory Bank's "
        "consolidation pipeline (dedup + contradiction resolution), so a new "
        "fact that contradicts an old one updates it in place. Use for explicit "
        "user preferences, decisions, and corrections."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "fact": {"type": "string", "description": "The fact to remember."},
        },
        "required": ["fact"],
    },
}

FORGET_SCHEMA = {
    "name": "memorybank_forget",
    "description": (
        "Delete a specific memory by its ID (obtain IDs from memorybank_search). "
        "Use when you discover an outdated or incorrect memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "Memory ID to delete."},
        },
        "required": ["memory_id"],
    },
}

CORRECT_SCHEMA = {
    "name": "memorybank_correct",
    "description": (
        "Update a memory's fact text in place by ID. If the memory no longer "
        "exists, the corrected fact is stored fresh via the consolidation pipeline."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "Memory ID to correct."},
            "fact": {"type": "string", "description": "Corrected fact text."},
        },
        "required": ["memory_id", "fact"],
    },
}

STATS_SCHEMA = {
    "name": "memorybank_stats",
    "description": (
        "Report long-term memory stats: total count in the current scope and "
        "the scope identity. Lightweight (field-masked count)."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class VertexMemoryBankProvider(MemoryProvider):
    """Vertex AI Memory Bank — managed cross-session memory."""

    def __init__(self):
        self._cfg: dict = {}
        self._client: VertexMemoryBankClient | None = None
        self._client_lock = threading.Lock()
        self._scope: Dict[str, str] = {}
        self._top_k = 10
        self._max_distance = None
        self._prefetch_result = ""
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread: threading.Thread | None = None
        self._sync_thread: threading.Thread | None = None
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0

    @property
    def name(self) -> str:
        return "vertex-memory"

    # -- availability / config ------------------------------------------------

    def is_available(self) -> bool:
        """No network — just check required config is present."""
        cfg = load_config()
        return bool(
            cfg.get("project_id")
            and cfg.get("location")
            and cfg.get("reasoning_engine_id")
        )

    def get_config_schema(self):
        # Web-dashboard surface. Keep in sync with config_schema.py (the
        # desktop app's declared surface); both persist to
        # $HERMES_HOME/vertex-memory/config.json.
        return [
            {"key": "project_id", "label": "GCP project ID",
             "description": "Google Cloud project hosting the reasoning engine", "required": True},
            {"key": "location", "label": "Region", "description": "GCP region (e.g. us-central1)",
             "required": True, "default": "us-central1"},
            {"key": "reasoning_engine_id", "label": "Reasoning engine ID",
             "description": "Vertex Agent Engine reasoning engine id "
                            "(create one with create_engine.py)", "required": True},
            {"key": "scope_key", "label": "Scope dimension",
             "description": "What memory isolation is keyed on",
             "default": "user_id", "choices": ["user_id", "agent_name"]},
            {"key": "scope_value", "label": "Static scope value",
             "description": "Fallback scope value for CLI/single-user sessions",
             "default": "hermes-user"},
            {"key": "top_k", "label": "Recall top-K", "description": "Max memories per recall",
             "default": "10"},
            {"key": "max_distance", "label": "Max relevance distance",
             "description": "Recall cutoff (lower = stricter); blank = no cutoff"},
        ]

    def save_config(self, values: dict, hermes_home: str) -> None:
        save_config_file(values, hermes_home)

    # -- lifecycle ------------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        self._cfg = load_config()
        self._top_k = int(self._cfg.get("top_k", 10) or 10)
        md = self._cfg.get("max_distance")
        self._max_distance = float(md) if md not in (None, "", "none") else None

        scope_key = self._cfg.get("scope_key", "user_id")
        # Prefer gateway-provided user_id for per-user scoping; fall back to
        # a stable config default for CLI single-user sessions.
        scope_val = (
            kwargs.get("user_id")
            or self._cfg.get("scope_value")
            or "hermes-user"
        )
        self._scope = {scope_key: str(scope_val)}
        logger.info("Vertex Memory Bank initialized (scope=%s)", self._scope)

    def _get_client(self) -> VertexMemoryBankClient:
        with self._client_lock:
            if self._client is None:
                self._client = VertexMemoryBankClient(
                    project_id=self._cfg["project_id"],
                    location=self._cfg["location"],
                    reasoning_engine_id=self._cfg["reasoning_engine_id"],
                )
            return self._client

    # -- circuit breaker ------------------------------------------------------

    def _is_breaker_open(self) -> bool:
        if self._consecutive_failures < _BREAKER_THRESHOLD:
            return False
        if time.monotonic() >= self._breaker_open_until:
            self._consecutive_failures = 0
            return False
        return True

    def _record_success(self) -> None:
        self._consecutive_failures = 0

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= _BREAKER_THRESHOLD:
            self._breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECS
            logger.warning(
                "Vertex Memory Bank circuit breaker tripped after %d failures; "
                "pausing %ds.", self._consecutive_failures, _BREAKER_COOLDOWN_SECS,
            )

    # -- system prompt --------------------------------------------------------

    def system_prompt_block(self) -> str:
        scope_desc = ", ".join(f"{k}={v}" for k, v in self._scope.items())
        return (
            "# Vertex AI Memory Bank\n"
            f"Active. Scope: {scope_desc}.\n"
            "Long-term memory persists across sessions and agents. Use "
            "memorybank_search to recall, memorybank_remember to store durable "
            "facts, memorybank_correct/forget to fix mistakes."
        )

    # -- prefetch (recall) ----------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=3.0)
        with self._prefetch_lock:
            result = self._prefetch_result
            self._prefetch_result = ""
        return f"## Recalled Memory\n{result}" if result else ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if self._is_breaker_open() or not query:
            return

        def _run():
            try:
                memories = self._get_client().retrieve(
                    self._scope, query, top_k=min(self._top_k, 5),
                    max_distance=self._max_distance,
                )
                if memories:
                    lines = [f"- {m['fact']}" for m in memories if m.get("fact")]
                    with self._prefetch_lock:
                        self._prefetch_result = "\n".join(lines)
                self._record_success()
            except Exception as e:  # noqa: BLE001
                self._record_failure()
                logger.debug("Vertex prefetch failed: %s", e)

        self._prefetch_thread = threading.Thread(
            target=_run, daemon=True, name="vertex-mem-prefetch")
        self._prefetch_thread.start()

    # -- capture (write) ------------------------------------------------------

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages=None) -> None:
        if self._is_breaker_open():
            return
        # Noise filter — skip trivial exchanges.
        if (len(user_content.strip()) < _MIN_USER_CHARS
                and len(user_content) + len(assistant_content) < _MIN_TOTAL_CHARS):
            return

        def _sync():
            try:
                self._get_client().generate_from_conversation(
                    self._scope,
                    [
                        {"role": "user", "content": user_content},
                        {"role": "assistant", "content": assistant_content},
                    ],
                    source="capture",
                )
                self._record_success()
            except Exception as e:  # noqa: BLE001
                self._record_failure()
                logger.warning("Vertex sync failed: %s", e)

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)
        self._sync_thread = threading.Thread(
            target=_sync, daemon=True, name="vertex-mem-sync")
        self._sync_thread.start()

    def on_memory_write(self, action, target, content, metadata=None) -> None:
        """Mirror built-in MEMORY.md/USER.md writes into Memory Bank."""
        if self._is_breaker_open() or action == "remove" or not content:
            return
        try:
            self._get_client().generate_from_fact(
                self._scope, content, source=f"builtin-{target}")
            self._record_success()
        except Exception as e:  # noqa: BLE001
            self._record_failure()
            logger.debug("Vertex on_memory_write failed: %s", e)

    # -- tools ----------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [SEARCH_SCHEMA, REMEMBER_SCHEMA, FORGET_SCHEMA, CORRECT_SCHEMA, STATS_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if self._is_breaker_open():
            return json.dumps({"error": "Vertex Memory Bank temporarily unavailable "
                                        "(consecutive failures). Retrying automatically."})
        try:
            client = self._get_client()
        except Exception as e:  # noqa: BLE001
            return tool_error(str(e))

        try:
            if tool_name == "memorybank_search":
                query = args.get("query", "")
                if not query:
                    return tool_error("Missing required parameter: query")
                top_k = min(int(args.get("top_k", self._top_k)), 50)
                results = client.retrieve(self._scope, query, top_k=top_k,
                                          max_distance=self._max_distance)
                self._record_success()
                if not results:
                    return json.dumps({"result": "No relevant memories found."})
                return json.dumps({"results": results, "count": len(results)})

            if tool_name == "memorybank_remember":
                fact = args.get("fact", "")
                if not fact:
                    return tool_error("Missing required parameter: fact")
                outcome = client.generate_from_fact(self._scope, fact,
                                                    source="tool-remember", wait=True)
                self._record_success()
                return json.dumps({"result": "Stored.", "outcome": outcome})

            if tool_name == "memorybank_forget":
                mid = args.get("memory_id", "")
                if not mid:
                    return tool_error("Missing required parameter: memory_id")
                client.delete(mid, scope=self._scope)
                self._record_success()
                return json.dumps({"result": f"Deleted memory {mid}."})

            if tool_name == "memorybank_correct":
                mid = args.get("memory_id", "")
                fact = args.get("fact", "")
                if not mid or not fact:
                    return tool_error("Need memory_id and fact")
                result = client.correct(self._scope, mid, fact)
                self._record_success()
                return json.dumps({"memory_id": mid, **result})

            if tool_name == "memorybank_stats":
                count = client.count(self._scope)
                self._record_success()
                return json.dumps({"total_memories": count, "scope": self._scope})

            return tool_error(f"Unknown tool: {tool_name}")
        except MemoryBankError as e:
            if getattr(e, "guard", False):
                # Fail-closed refusal: backend is healthy; don't trip the breaker.
                return tool_error(str(e))
            self._record_failure()
            return tool_error(str(e))
        except Exception as e:  # noqa: BLE001
            self._record_failure()
            return tool_error(f"{tool_name} failed: {e}")

    def shutdown(self) -> None:
        for t in (self._prefetch_thread, self._sync_thread):
            if t and t.is_alive():
                t.join(timeout=5.0)
        with self._client_lock:
            self._client = None


def register(ctx) -> None:
    """Plugin entry point — register the memory provider."""
    ctx.register_memory_provider(VertexMemoryBankProvider())
