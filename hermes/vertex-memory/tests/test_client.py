"""Tests for VertexMemoryBankClient — HTTP shapes mocked, no GCP calls."""

from __future__ import annotations

import json
from unittest import mock

import pytest

from client import VertexMemoryBankClient, MemoryBankError, _memory_id


class FakeResp:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload if payload is not None else {}
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture
def client():
    c = VertexMemoryBankClient("proj", "us-central1", "eng123")
    # Bypass real ADC.
    c._token = lambda: "fake-token"  # type: ignore
    return c


def _last_call(m):
    """Return (method, url, kwargs) of the last requests.request call."""
    args, kwargs = m.call_args
    return args[0], args[1], kwargs


def test_init_requires_all_fields():
    with pytest.raises(MemoryBankError):
        VertexMemoryBankClient("", "us-central1", "eng")


def test_parent_and_base(client):
    assert client._parent == "projects/proj/locations/us-central1/reasoningEngines/eng123"
    assert client._base == "https://us-central1-aiplatform.googleapis.com/v1beta1"


def test_memory_id_helper():
    assert _memory_id("projects/p/locations/l/reasoningEngines/e/memories/999") == "999"
    assert _memory_id("") == ""


def test_retrieve_shape_and_parsing(client):
    payload = {"retrievedMemories": [
        {"memory": {"fact": "likes elixir", "name": ".../memories/1"}, "distance": 0.1},
        {"memory": {"fact": "uses uv", "name": ".../memories/2"}, "distance": 0.9},
    ]}
    with mock.patch("requests.request", return_value=FakeResp(payload=payload)) as m:
        out = client.retrieve({"user_id": "alan"}, "tooling", top_k=7)
    method, url, kw = _last_call(m)
    assert method == "POST"
    assert url.endswith("/memories:retrieve")
    assert kw["json"]["scope"] == {"user_id": "alan"}
    assert kw["json"]["similaritySearchParams"] == {"searchQuery": "tooling", "topK": 7}
    assert [o["fact"] for o in out] == ["likes elixir", "uses uv"]
    assert out[0]["id"] == "1"


def test_retrieve_max_distance_filters(client):
    payload = {"retrievedMemories": [
        {"memory": {"fact": "close", "name": "x/memories/1"}, "distance": 0.1},
        {"memory": {"fact": "far", "name": "x/memories/2"}, "distance": 0.8},
    ]}
    with mock.patch("requests.request", return_value=FakeResp(payload=payload)):
        out = client.retrieve({"user_id": "a"}, "q", max_distance=0.5)
    assert [o["fact"] for o in out] == ["close"]


def test_generate_from_conversation_role_mapping(client):
    with mock.patch("requests.request", return_value=FakeResp(payload={})) as m:
        client.generate_from_conversation(
            {"user_id": "a"},
            [{"role": "user", "content": "hi"},
             {"role": "assistant", "content": "hello"}],
        )
    _, url, kw = _last_call(m)
    assert url.endswith("/memories:generate")
    events = kw["json"]["direct_contents_source"]["events"]
    assert events[0]["content"]["role"] == "user"
    assert events[1]["content"]["role"] == "model"  # assistant → model
    assert kw["json"]["revision_labels"]["source"] == "capture"


def test_generate_from_conversation_truncates(client):
    long_text = "x" * 5000
    with mock.patch("requests.request", return_value=FakeResp(payload={})) as m:
        client.generate_from_conversation(
            {"u": "a"}, [{"role": "user", "content": long_text}])
    _, _, kw = _last_call(m)
    text = kw["json"]["direct_contents_source"]["events"][0]["content"]["parts"][0]["text"]
    assert len(text) == 4000


def test_generate_from_conversation_empty_noop(client):
    with mock.patch("requests.request") as m:
        out = client.generate_from_conversation({"u": "a"}, [])
    assert out == {}
    m.assert_not_called()


def test_generate_from_fact_wait_counts(client):
    payload = {"generatedMemories": [
        {"action": "CREATED"}, {"action": "UPDATED"}, {"action": "CREATED"}]}
    with mock.patch("requests.request", return_value=FakeResp(payload=payload)) as m:
        out = client.generate_from_fact({"u": "a"}, "a fact", wait=True)
    _, url, kw = _last_call(m)
    assert url.endswith("/memories:generate")
    assert kw["json"]["direct_memories_source"]["direct_memories"] == [{"fact": "a fact"}]
    assert out == {"created": 2, "updated": 1, "total": 3}


def test_generate_from_fact_fire_and_forget(client):
    with mock.patch("requests.request", return_value=FakeResp(payload={})):
        out = client.generate_from_fact({"u": "a"}, "fact", wait=False)
    assert out == {"queued": True}


def test_delete(client):
    with mock.patch.object(client, "get_memory", return_value={"scope": {"u": "a"}}):
        with mock.patch("requests.request", return_value=FakeResp(status=200, text="")) as m:
            client.delete("42", scope={"u": "a"})
    method, url, _ = _last_call(m)
    assert method == "DELETE"
    assert url.endswith("/memories/42")


def test_delete_refuses_cross_scope(client):
    """delete() must raise and perform no mutation when the memory belongs to a
    different scope than the one the caller expects — this is the fail-closed
    guard against a same-engine, cross-user/cross-agent forget/correct."""
    with mock.patch.object(client, "get_memory", return_value={"scope": {"u": "someone-else"}}):
        with mock.patch("requests.request") as m:
            with pytest.raises(MemoryBankError, match="does not belong to the configured scope"):
                client.delete("42", scope={"u": "a"})
    m.assert_not_called()


def test_delete_refuses_when_scope_lookup_fails(client):
    """A failed ownership lookup must fail closed (raise), never fall through to delete."""
    with mock.patch.object(client, "get_memory", side_effect=MemoryBankError("500: boom")):
        with mock.patch("requests.request") as m:
            with pytest.raises(MemoryBankError, match="Cannot verify memory scope"):
                client.delete("42", scope={"u": "a"})
    m.assert_not_called()


def test_correct_patch_shape(client):
    with mock.patch.object(client, "get_memory", return_value={"scope": {"u": "a"}}):
        with mock.patch("requests.request", return_value=FakeResp(payload={})) as m:
            client.correct({"u": "a"}, "7", "new fact")
    method, url, kw = _last_call(m)
    assert method == "PATCH"
    assert url.endswith("/memories/7")
    assert kw["params"] == {"updateMask": "fact"}
    assert kw["json"] == {"fact": "new fact"}


def test_correct_refuses_cross_scope(client):
    with mock.patch.object(client, "get_memory", return_value={"scope": {"u": "someone-else"}}):
        with mock.patch("requests.request") as m:
            with pytest.raises(MemoryBankError, match="does not belong to the configured scope"):
                client.correct({"u": "a"}, "7", "new fact")
    m.assert_not_called()


def _router(handlers):
    """requests.request side_effect dispatching on method; records calls."""
    calls = []

    def fake(method, url, **kw):
        calls.append((method, url, kw))
        h = handlers.get(method)
        return h(url, kw) if callable(h) else (h or FakeResp(payload={}))
    return fake, calls


OWNED = {"name": "projects/proj/locations/us-central1/reasoningEngines/eng123/memories/7",
         "scope": {"u": "a"}, "fact": "old fact"}


def test_correct_returns_patch_result(client):
    fake, calls = _router({"GET": FakeResp(payload=OWNED)})
    with mock.patch("requests.request", side_effect=fake):
        out = client.correct({"u": "a"}, "7", "new")
    assert out == {"corrected": True, "method": "patch"}
    assert [c[0] for c in calls] == ["GET", "PATCH"]


def test_correct_patch_unsupported_delete_regenerates(client):
    fake, calls = _router({
        "GET": FakeResp(payload=OWNED),
        "PATCH": FakeResp(status=405, text="method not allowed"),
        "DELETE": FakeResp(status=200, text=""),
        "POST": FakeResp(payload={"generatedMemories": [{"action": "CREATED"}]}),
    })
    with mock.patch("requests.request", side_effect=fake):
        out = client.correct({"u": "a"}, "7", "new")
    assert out["corrected"] is True and out["method"] == "delete-regenerate"
    assert [c[0] for c in calls] == ["GET", "PATCH", "DELETE", "POST"]
    assert calls[-1][1].endswith("/memories:generate")


def test_correct_restores_old_fact_when_regenerate_fails(client):
    def post(url, kw):
        if url.endswith(":generate"):
            return FakeResp(status=500, text="boom")
        return FakeResp(payload={})  # restore create
    fake, calls = _router({
        "GET": FakeResp(payload=OWNED),
        "PATCH": FakeResp(status=501, text="unimplemented"),
        "DELETE": FakeResp(status=200, text=""),
        "POST": post,
    })
    with mock.patch("requests.request", side_effect=fake):
        out = client.correct({"u": "a"}, "7", "new")
    assert out["corrected"] is False and out["recovered"] is True
    restore = calls[-1]
    assert restore[1].endswith("/eng123/memories")
    assert restore[2]["json"] == {"fact": "old fact", "scope": {"u": "a"}}


def test_correct_reports_failed_restore(client):
    fake, _ = _router({
        "GET": FakeResp(payload=OWNED),
        "PATCH": FakeResp(status=400, text="bad"),
        "DELETE": FakeResp(status=200, text=""),
        "POST": FakeResp(status=500, text="down"),
    })
    with mock.patch("requests.request", side_effect=fake):
        out = client.correct({"u": "a"}, "7", "new")
    assert out["corrected"] is False and out["recovered"] is False
    assert "restore failed" in out["error"]


def test_correct_no_delete_without_old_fact(client):
    fake, calls = _router({
        "GET": FakeResp(payload={**OWNED, "fact": ""}),
        "PATCH": FakeResp(status=405, text="nope"),
    })
    with mock.patch("requests.request", side_effect=fake):
        with pytest.raises(MemoryBankError, match="original fact is unavailable"):
            client.correct({"u": "a"}, "7", "new")
    assert "DELETE" not in [c[0] for c in calls]


def test_correct_non_retryable_error_raises_without_delete(client):
    fake, calls = _router({"GET": FakeResp(payload=OWNED),
                           "PATCH": FakeResp(status=403, text="denied")})
    with mock.patch("requests.request", side_effect=fake):
        with pytest.raises(MemoryBankError, match="403"):
            client.correct({"u": "a"}, "7", "new")
    assert [c[0] for c in calls] == ["GET", "PATCH"]


def test_correct_retries_transient_then_succeeds(client):
    seq = iter([FakeResp(status=503, text="busy"), FakeResp(payload={})])
    fake, calls = _router({"GET": FakeResp(payload=OWNED), "PATCH": lambda u, k: next(seq)})
    with mock.patch("requests.request", side_effect=fake), mock.patch("time.sleep"):
        out = client.correct({"u": "a"}, "7", "new")
    assert out["method"] == "patch"
    assert [c[0] for c in calls] == ["GET", "PATCH", "PATCH"]


@pytest.mark.parametrize("bad", [
    "../7", "a/b", "projects/proj/locations/europe-west4/reasoningEngines/eng123/memories/7",
    "projects/proj/locations/us-central1/reasoningEngines/OTHER/memories/7", "",
])
def test_malformed_or_foreign_ids_rejected_before_network(client, bad):
    with mock.patch("requests.request") as m:
        with pytest.raises(MemoryBankError, match="Invalid memory_id"):
            client.delete(bad, scope={"u": "a"})
    m.assert_not_called()


def test_full_name_routes_through_configured_parent(client):
    """A caller-supplied project spelling must never be used in the request URL."""
    alias = "projects/123456/locations/us-central1/reasoningEngines/eng123/memories/7"
    fake, calls = _router({"GET": FakeResp(payload={**OWNED, "name": alias})})
    with mock.patch("requests.request", side_effect=fake):
        client.delete(alias, scope={"u": "a"})
    assert all("/projects/proj/" in c[1] for c in calls)
    assert [c[0] for c in calls] == ["GET", "DELETE"]


def test_project_alias_accepted_only_when_api_confirms(client):
    """Number-spelled name accepted iff the configured-project lookup returns that exact name."""
    alias = "projects/123456/locations/us-central1/reasoningEngines/eng123/memories/7"
    fake, calls = _router({"GET": FakeResp(payload=OWNED)})  # API says name uses 'proj'
    with mock.patch("requests.request", side_effect=fake):
        with pytest.raises(MemoryBankError, match="verified alias") as ei:
            client.delete(alias, scope={"u": "a"})
    assert ei.value.guard is True
    assert "DELETE" not in [c[0] for c in calls]


def test_guard_flag_semantics(client):
    with pytest.raises(MemoryBankError) as ei:
        client.delete("../x", scope={"u": "a"})
    assert ei.value.guard is True
    with mock.patch("requests.request", return_value=FakeResp(payload={"scope": {"u": "b"}})):
        with pytest.raises(MemoryBankError) as ei:
            client.delete("7", scope={"u": "a"})
    assert ei.value.guard is True
    with mock.patch("requests.request", return_value=FakeResp(status=503, text="down")):
        with pytest.raises(MemoryBankError) as ei:
            client.delete("7", scope={"u": "a"})
    assert ei.value.guard is False and ei.value.status == 503


def test_error_carries_status(client):
    with mock.patch("requests.request", return_value=FakeResp(status=429, text="slow")):
        with pytest.raises(MemoryBankError) as ei:
            client.retrieve({"u": "a"}, "q")
    assert ei.value.status == 429


def test_error_raises(client):
    with mock.patch("requests.request",
                    return_value=FakeResp(status=403, text="denied")):
        with pytest.raises(MemoryBankError) as ei:
            client.retrieve({"u": "a"}, "q")
    assert "403" in str(ei.value)


def test_count_paginates(client):
    pages = [
        FakeResp(payload={"memories": [{"name": "x/1"}, {"name": "x/2"}],
                          "nextPageToken": "tok"}),
        FakeResp(payload={"memories": [{"name": "x/3"}]}),
    ]
    with mock.patch("requests.request", side_effect=pages) as m:
        n = client.count({"user_id": "a"})
    assert n == 3
    # second page must carry the page token
    second_kw = m.call_args_list[1].kwargs
    assert second_kw["params"]["pageToken"] == "tok"
    assert second_kw["params"]["$fields"] == "memories/name,nextPageToken"


def test_list_scope_filter_escaped(client):
    with mock.patch("requests.request",
                    return_value=FakeResp(payload={"memories": []})) as m:
        client.list_memories({"user_id": "alan"})
    _, _, kw = _last_call(m)
    assert 'scope=' in kw["params"]["filter"]
    assert "user_id" in kw["params"]["filter"]
