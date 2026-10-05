# Hermes native memory provider (`vertex-memory`)

A native [Hermes Agent](https://github.com/NousResearch/hermes-agent) memory provider for Agent Platform Memory Bank. It runs in-process (no MCP child), uses Hermes' memory-provider lifecycle for **auto-recall and auto-capture**, and is configurable from the **Memory panel of the Hermes admin dashboard**.

It shares memories with the OpenClaw plugin and the Hermes MCP server in this repository when the project, location, reasoning engine, and scope match exactly. Mutation guards mirror `memorybank-core.ts`.

> **Disclaimer:** This is not an officially supported Google product.

## Native provider vs. MCP server

| | Native provider (this directory) | Hermes MCP server |
| --- | --- | --- |
| Auto-recall before each turn | Yes (`prefetch`) | No |
| Auto-capture after each turn | Yes (`sync_turn`, background) | No |
| Mirrors built-in `MEMORY.md`/`USER.md` writes | Yes | No |
| Admin-dashboard config panel | Yes | No (edit `config.yaml`) |
| Process model | In-process Python | Node stdio child |
| Explicit tools | `memorybank_search/remember/forget/correct/stats` | Same five tools |

Hermes allows one external memory provider at a time; the MCP server can be used alongside any provider.

## Prerequisites

The [repository prerequisites](../../README.md#prerequisites) apply: billing-enabled project, Memory Bank IAM roles, a reasoning engine, and ADC visible to the Hermes process. `create_engine.py` here creates an engine and prints the config to paste.

## Install

```bash
hermes plugins install Shubhamsaboo/google-memorybank-plugin/hermes/vertex-memory
hermes plugins enable vertex-memory   # consents to google-auth + requests (pinned in plugin.yaml)
hermes config set memory.provider vertex-memory
```

Then configure in **Admin dashboard → Memory → Vertex Memory** (web) or **Vertex AI Memory Bank** (desktop), or write `$HERMES_HOME/vertex-memory/config.json` directly:

```json
{
  "project_id": "your-gcp-project-id",
  "location": "us-central1",
  "reasoning_engine_id": "your-reasoning-engine-id",
  "scope_key": "user_id",
  "top_k": 10
}
```

Restart Hermes (or `hermes gateway restart`) and check `hermes vertex-memory status`.

## Configuration

| Key | Required | Default | Description |
| --- | --- | --- | --- |
| `project_id` | Yes | — | GCP project ID |
| `location` | Yes | `us-central1` | GCP region of the reasoning engine |
| `reasoning_engine_id` | Yes | — | Reasoning engine ID |
| `scope_key` | | `user_id` | Scope dimension (`user_id` shares memory across agents for one user) |
| `scope_value` | | gateway `user_id`, else `hermes-user` | Static scope value for CLI/single-user sessions |
| `top_k` | | `10` | Max memories per recall |
| `max_distance` | | none | Recall relevance cutoff (lower = stricter) |

Precedence: env vars (`VERTEX_MEMORY_PROJECT_ID`, `VERTEX_MEMORY_LOCATION`, `VERTEX_MEMORY_ENGINE_ID`, `VERTEX_MEMORY_SCOPE_KEY`, `VERTEX_MEMORY_SCOPE_VALUE`, `VERTEX_MEMORY_TOP_K`, `VERTEX_MEMORY_MAX_DISTANCE`) < legacy `$HERMES_HOME/vertex_memory.json` < `$HERMES_HOME/vertex-memory/config.json` (what both dashboard surfaces write).

To share with OpenClaw or the MCP server, make the scope identical, e.g. `{"user_id": "your-user-id"}` = `scope_key: user_id` + the same user value.

## Mutation isolation

Same contract as the [shared core](../../README.md#mutation-isolation-and-error-handling):

- `memory_id` must be a bare ID or a full name under the configured location and reasoning engine; anything else is rejected before any network call. Requests always route through the configured parent.
- Forget/correct read the memory first and require an exact scope match; otherwise nothing is mutated.
- A full name spelled with the project **number** is accepted only when the configured-project lookup returns exactly that name.
- `memorybank_correct` patches in place. If the backend rejects PATCH (400/405/501) it deletes and regenerates, and restores the original fact if regeneration fails. The result reports `corrected`, `method`, and `recovered`.
- Guard refusals do not count toward the provider's circuit breaker, so repeated cross-scope attempts cannot lock out the owner.

## CLI

```bash
hermes vertex-memory status
hermes vertex-memory search "deployment region" --top-k 5 --show-ids
hermes vertex-memory list --count-only
hermes vertex-memory remember "Deploys use us-central1"
hermes vertex-memory correct <memory_id> "Deploys use europe-west4"
hermes vertex-memory forget <memory_id>
```

## Privacy

Auto-capture sends the last user/assistant pair to your Memory Bank instance for extraction. Set `memory.provider` back to `builtin` to disable.

## Development

```bash
cd hermes/vertex-memory
pip install -r requirements-dev.txt
pytest -q   # fully mocked, no GCP calls
```

Ported from [zeroasterisk/hermes-memory-vertex](https://github.com/zeroasterisk/hermes-memory-vertex), which is now deprecated in favor of this directory.
