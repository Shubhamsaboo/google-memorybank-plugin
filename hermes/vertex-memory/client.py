"""REST client for Vertex AI Memory Bank (Agent Engine).

Thin, dependency-light wrapper over the v1beta1 `reasoningEngines/*/memories*`
endpoints. Auth via Application Default Credentials (ADC). Uses `requests` for
HTTP and `google.auth` for token management — both already present in a Hermes
environment that talks to Google Cloud.

API reference (captured during build):
  POST   {parent}/memories:retrieve   — similarity search (recall)
  POST   {parent}/memories:generate   — write via consolidation (capture/remember)
  GET    {parent}/memories            — list (paginated, max pageSize=100)
  DELETE {parent}/memories/{id}       — forget
  PATCH  {parent}/memories/{id}       — correct (update fact in place)
  POST   {parent}/memories            — create (restore after a failed correct)
where {parent} = projects/{p}/locations/{l}/reasoningEngines/{e}
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_API_VERSION = "v1beta1"
_TEXT_TRUNCATE = 4000   # max chars per conversation event
_DEFAULT_TIMEOUT = 30


_VALID_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_FULL_NAME = re.compile(
    r"^projects/([^/]+)/locations/([^/]+)/reasoningEngines/([^/]+)/memories/([^/]+)$")
_PATCH_UNSUPPORTED = {400, 405, 501}
_RETRYABLE = {429, 500, 502, 503, 504}


class MemoryBankError(RuntimeError):
    """Raised on a non-2xx Memory Bank API response (``status`` = HTTP code).

    ``guard=True`` marks a local, fail-closed refusal (bad id, wrong scope):
    the backend is healthy, so it must not count toward the circuit breaker —
    otherwise a few rejected cross-scope calls would lock out the owner.
    """

    def __init__(self, message: str, status: Optional[int] = None, *, guard: bool = False):
        super().__init__(message)
        self.status = status
        self.guard = guard


class VertexMemoryBankClient:
    """Minimal Vertex AI Memory Bank REST client (ADC-authenticated)."""

    def __init__(self, project_id: str, location: str, reasoning_engine_id: str):
        if not (project_id and location and reasoning_engine_id):
            raise MemoryBankError(
                "project_id, location, and reasoning_engine_id are all required")
        self.project_id = project_id
        self.location = location
        self.reasoning_engine_id = reasoning_engine_id
        self._base = f"https://{location}-aiplatform.googleapis.com/{_API_VERSION}"
        self._parent = (
            f"projects/{project_id}/locations/{location}"
            f"/reasoningEngines/{reasoning_engine_id}"
        )
        self._creds = None
        self._auth_req = None
        self._lock = threading.Lock()

    # -- auth -----------------------------------------------------------------

    def _token(self) -> str:
        """Return a fresh ADC access token, refreshing as needed."""
        with self._lock:
            if self._creds is None:
                import google.auth
                from google.auth.transport.requests import Request
                self._creds, _ = google.auth.default(
                    scopes=["https://www.googleapis.com/auth/cloud-platform"])
                self._auth_req = Request()
            if not self._creds.valid:
                self._creds.refresh(self._auth_req)
            return self._creds.token

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json",
            "User-Agent": "google-memorybank-plugin-hermes/1.0",
        }

    # -- HTTP -----------------------------------------------------------------

    def _request(self, method: str, path: str, *, body: Optional[dict] = None,
                 params: Optional[dict] = None, timeout: int = _DEFAULT_TIMEOUT) -> dict:
        import requests
        url = f"{self._base}/{path}"
        resp = requests.request(
            method, url, headers=self._headers(),
            json=body if body is not None else None,
            params=params, timeout=timeout,
        )
        if not resp.ok:
            raise MemoryBankError(
                f"Memory Bank API {resp.status_code}: {resp.text[:500]}",
                status=resp.status_code)
        if resp.text:
            try:
                return resp.json()
            except ValueError:
                return {}
        return {}

    # -- operations -----------------------------------------------------------

    def retrieve(self, scope: Dict[str, str], query: str, *, top_k: int = 10,
                 max_distance: Optional[float] = None) -> List[Dict[str, Any]]:
        """Similarity search. Returns [{fact, id, distance}]."""
        result = self._request(
            "POST", f"{self._parent}/memories:retrieve",
            body={"scope": scope,
                  "similaritySearchParams": {"searchQuery": query, "topK": top_k}},
        )
        out: List[Dict[str, Any]] = []
        for item in result.get("retrievedMemories", []):
            mem = item.get("memory", {})
            dist = item.get("distance")
            if max_distance is not None and dist is not None and dist > max_distance:
                continue
            out.append({
                "fact": mem.get("fact", ""),
                "id": _memory_id(mem.get("name", "")),
                "distance": dist,
            })
        return out

    def generate_from_conversation(self, scope: Dict[str, str],
                                   messages: List[Dict[str, str]], *,
                                   source: str = "capture") -> dict:
        """Write the last user+assistant pair via consolidation (events format)."""
        pair = [m for m in messages if m.get("role") in ("user", "assistant")][-2:]
        if not pair:
            return {}
        events = [{
            "content": {
                "role": "model" if m["role"] == "assistant" else "user",
                "parts": [{"text": (m.get("content") or "")[:_TEXT_TRUNCATE]}],
            }
        } for m in pair]
        return self._request(
            "POST", f"{self._parent}/memories:generate",
            body={"scope": scope,
                  "direct_contents_source": {"events": events},
                  "revision_labels": {"source": source}},
        )

    def generate_from_fact(self, scope: Dict[str, str], fact: str, *,
                           source: str = "remember", wait: bool = False) -> dict:
        """Write a raw fact via consolidation (direct_memories_source)."""
        result = self._request(
            "POST", f"{self._parent}/memories:generate",
            body={"scope": scope,
                  "direct_memories_source": {"direct_memories": [{"fact": fact}]},
                  "revision_labels": {"source": source}},
        )
        if not wait:
            return {"queued": True}
        generated = result.get("generatedMemories", [])
        created = sum(1 for m in generated if m.get("action") == "CREATED")
        updated = sum(1 for m in generated if m.get("action") == "UPDATED")
        return {"created": created, "updated": updated, "total": len(generated)}

    def _memory_parts(self, memory_id: str) -> tuple:
        """Validate a caller-supplied id -> (project_spelling, bare_id).

        Accepts a bare id, or a full resource name under the configured location
        + reasoning engine (project spelled as id or number). Anything else is
        rejected before any network call, so a caller can never steer a lookup
        or mutation at a foreign engine/location. Mirrors memorybank-core.ts.
        """
        if _VALID_ID.match(memory_id or ""):
            return self.project_id, memory_id
        m = _FULL_NAME.match(memory_id or "")
        if (not m or m.group(2) != self.location
                or m.group(3) != self.reasoning_engine_id or not _VALID_ID.match(m.group(4))):
            raise MemoryBankError(
                "Invalid memory_id: must be a bare memory ID or a full resource "
                "name under the configured reasoning engine.", guard=True)
        return m.group(1), m.group(4)

    def _memory_name(self, memory_id: str) -> str:
        """Always route through the configured parent, never a caller-supplied project."""
        return f"{self._parent}/memories/{self._memory_parts(memory_id)[1]}"

    def get_memory(self, memory_id: str) -> Dict[str, Any]:
        """GET a single memory (bare id or full name) via the configured parent."""
        return self._request("GET", self._memory_name(memory_id))

    def _assert_owned_by_scope(self, memory_id: str,
                               expected_scope: Dict[str, str]) -> Dict[str, Any]:
        """Fail-closed scope guard for mutating operations (forget/correct).

        A single reasoning engine can hold memories for many scopes (different
        users, different agents), and ADC credentials are typically broad
        enough to read/write any of them. Without this check, a caller could
        forget/correct a same-engine memory belonging to a DIFFERENT scope
        than the one this provider instance is configured for — the API layer
        alone does not enforce per-caller scope isolation. On any mismatch,
        missing scope, or lookup failure this raises and performs no mutation;
        callers must not catch it and continue.
        """
        self._memory_parts(memory_id)  # reject malformed / foreign-engine ids un-wrapped
        try:
            memory = self.get_memory(memory_id)
        except MemoryBankError as e:
            raise MemoryBankError(
                f"Cannot verify memory scope before mutating (lookup failed): {e}",
                status=e.status, guard=e.status == 404) from e
        # A full name spelled with a different project (e.g. the project NUMBER
        # the API returns while config holds the project ID) is accepted only
        # when our configured-project lookup returns exactly that name: the
        # trusted response proves the alias. Mirrors memorybank-core.ts.
        project, _ = self._memory_parts(memory_id)
        if project != self.project_id and memory_id != memory.get("name"):
            raise MemoryBankError(
                "Refusing to mutate: memory project does not match the configured "
                "project or its verified alias.", guard=True)
        actual_scope = memory.get("scope")
        if not actual_scope or actual_scope != expected_scope:
            raise MemoryBankError(
                "Refusing to mutate: memory does not belong to the configured scope.",
                guard=True)
        return memory

    def delete(self, memory_id: str, *, scope: Dict[str, str]) -> None:
        self._assert_owned_by_scope(memory_id, scope)
        self._request("DELETE", self._memory_name(memory_id))

    def correct(self, scope: Dict[str, str], memory_id: str, fact: str, *,
                max_attempts: int = 3) -> dict:
        """Correct a memory's fact; returns a result dict and never silently loses data.

        1. Verify ownership (fail closed) and snapshot the old fact.
        2. PATCH in place; transient errors (429/5xx) retry with backoff.
        3. If the backend rejects PATCH (400/405/501): delete + regenerate via
           consolidation; if regeneration fails, restore the old fact.
        Ported from memorybank-core.ts MemoryBankService.correct().
        """
        old = self._assert_owned_by_scope(memory_id, scope)
        name = self._memory_name(memory_id)
        last_err: Optional[MemoryBankError] = None
        for attempt in range(max_attempts):
            try:
                self._request("PATCH", name, params={"updateMask": "fact"},
                              body={"fact": fact})
                return {"corrected": True, "method": "patch"}
            except MemoryBankError as e:
                last_err = e
                if e.status in _PATCH_UNSUPPORTED:
                    break
                if e.status not in _RETRYABLE or attempt == max_attempts - 1:
                    raise
                time.sleep(0.5 * (2 ** attempt))

        old_fact = old.get("fact") if isinstance(old.get("fact"), str) else None
        if not old_fact:
            raise MemoryBankError(
                "Cannot safely replace memory: original fact is unavailable.") from last_err
        self._request("DELETE", name)
        try:
            out = self.generate_from_fact(scope, fact, source="correct-regenerate", wait=True)
            return {"corrected": True, "method": "delete-regenerate", **out}
        except Exception as e:  # noqa: BLE001
            try:
                self._request("POST", f"{self._parent}/memories",
                              body={"fact": old_fact, "scope": scope})
                return {"corrected": False, "method": "delete-regenerate",
                        "recovered": True, "error": str(e)}
            except Exception as restore_err:  # noqa: BLE001
                return {"corrected": False, "method": "delete-regenerate",
                        "recovered": False,
                        "error": f"{e} Old-memory restore failed: {restore_err}"}

    def list_memories(self, scope: Dict[str, str], *,
                      names_only: bool = False) -> List[Dict[str, Any]]:
        """List all memories in scope (paginated). max pageSize=100 server-side."""
        all_items: List[Dict[str, Any]] = []
        page_token = None
        scope_filter = 'scope="%s"' % json.dumps(scope).replace('"', '\\"')
        while True:
            params = {"pageSize": 100, "filter": scope_filter}
            if names_only:
                params["$fields"] = "memories/name,nextPageToken"
            if page_token:
                params["pageToken"] = page_token
            result = self._request("GET", f"{self._parent}/memories", params=params)
            all_items.extend(result.get("memories", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                break
        return all_items

    def count(self, scope: Dict[str, str]) -> int:
        """Count memories in scope (lightweight field-masked pagination)."""
        return len(self.list_memories(scope, names_only=True))


def _memory_id(resource_name: str) -> str:
    """Extract trailing id from .../memories/{id}."""
    return resource_name.rsplit("/", 1)[-1] if resource_name else ""
