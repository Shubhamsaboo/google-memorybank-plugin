# FAQ — Agent Platform Memory Bank for OpenClaw and Hermes

Answers about [Agent Platform Memory Bank on Google Cloud](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/memory-bank), its OpenClaw plugin, and its Hermes MCP adapter. The package and plugin ID `openclaw-vertexai-memorybank` remain legacy compatibility identifiers, not current product branding.

## What is Memory Bank?

Gemini Enterprise Agent Platform Memory Bank is a managed service for generating, storing, and retrieving long-term agent memories. It supports natural-language memories and structured Memory Profiles, with scope-based isolation.

The implementation in this repository uses the existing `@google-cloud/aiplatform` v1beta1 SDK and `reasoningEngines` resource segment. Those are required technical compatibility identifiers; the implementation does not migrate SDKs or alter core API behavior.

## Which operations are used?

| Operation | Purpose in the service | OpenClaw plugin use | Hermes MCP use |
| --- | --- | --- | --- |
| `GenerateMemories` | extracts memories from supplied conversation/content and consolidates generated memories in the same exact scope | automatic capture and file sync | Not invoked automatically; no lifecycle capture/file sync |
| `CreateMemory` | directly writes a supplied memory | `memorybank-remember` | `memorybank_remember` |
| `GetMemory` | fetches one named memory | correction fallback recovery | correction fallback recovery |
| `RetrieveMemories` | returns all scoped memories or similarity-search results | automatic recall and search | `memorybank_search` |
| `ListMemories` | enumerates memories with pagination | list/count/stats | stats |
| update/delete operations | modify or remove a named memory | correction and forget | `memorybank_correct` and `memorybank_forget` |

`CreateMemory` is not a generation/consolidation request. Direct remembers can create duplicates until a later `GenerateMemories` run consolidates related facts.

## How does retrieval work?

`RetrieveMemories` can return all memories in a scope or run similarity search. Similarity results are ordered from shortest to greatest Euclidean distance. The OpenClaw plugin calls the returned value a **similarity distance** and applies `maxDistance` as an optional plugin-side filter; lower is stricter.

Use `GetMemory` for one resource, `RetrieveMemories` for scoped/similarity retrieval, and paginated `ListMemories` to enumerate a scope. They have distinct semantics.

## What are the scope constraints?

Scope is an immutable dictionary that controls isolation and generation consolidation. Official constraints are:

- no more than five key/value pairs;
- values must not contain `*`;
- matching is exact and independent of key order;
- a memory's scope cannot be changed after creation.

For cross-runtime sharing, use the same scope in both OpenClaw and Hermes, commonly `{ "user_id": "your-user-id" }`. Adding an agent name deliberately isolates memories between agents.

## What does automatic capture do?

The OpenClaw plugin submits only the final user/assistant pair when it passes local noise filtering. It calls `GenerateMemories`, which extracts facts based on the configured Memory Bank topics and may create, update, or remove generated memories in the same scope.

File sync also uses `GenerateMemories`. The plugin does not automatically create Memory Profiles.

## Are Memory Profiles supported?

Memory Profiles are an official structured-memory feature: a profile schema has one source-of-truth profile per schema and scope, maintained through generation. This plugin supports natural-language memories only and does not configure profile schemas or profile retrieval. See [Memory Profiles](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/memory-bank/profiles).

## What IAM roles and setup are required?

Enable the **Agent Platform API** and billing for the project. The identity enabling APIs needs `serviceusage.services.enable`, typically via `roles/serviceusage.serviceUsageAdmin` or a broader role.

For Memory Bank:

- `roles/aiplatform.user` creates or updates a Memory Bank instance.
- `roles/aiplatform.memoryUser` reads, writes, and generates memories.

Use the narrower Memory Bank viewer/editor roles for least privilege where they fit. The [setup guide](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/memory-bank/setup) is authoritative.

## Are `ttlSeconds` and `introspection` official API fields?

No. They are plugin configuration names:

- `ttlSeconds` is translated by the plugin into generated-memory TTL configuration during reasoning-engine configuration sync. It affects generated memories only; direct `CreateMemory` remembers do not receive this generated-memory TTL.
- `introspection` controls how this plugin formats recalled context (`off` facts only, or `scores` with distance metadata).

They are not official Memory Bank request-field names.

## Why are some old-looking identifiers still present?

The package/OpenClaw-plugin ID `openclaw-vertexai-memorybank`, `@google-cloud/aiplatform`, `google-cloud-aiplatform`, `import vertexai`, `v1beta1`, `aiplatform.googleapis.com`, `reasoningEngines`, `reasoningEngineId`, and `MEMORYBANK_REASONING_ENGINE_ID` are required compatibility identifiers in the present implementation and API examples. They do not describe the current product brand.

## How do I use the Hermes MCP server?

The Hermes MCP executable is `agent-platform-memorybank-hermes` when the package has been linked or installed globally; from a checkout, configure `node` with the absolute `bin/hermes-mcp.js` path as shown in the README. The MCP server name is `agent-platform-memorybank`. Configure the recommended Hermes key `agent_platform_memorybank`:

```yaml
mcp_servers:
  agent_platform_memorybank:
    command: "node"
    args: ["/absolute/path/google-memorybank-plugin/bin/hermes-mcp.js"]
    env:
      MEMORYBANK_PROJECT_ID: "${MEMORYBANK_PROJECT_ID}"
      MEMORYBANK_LOCATION: "${MEMORYBANK_LOCATION}"
      MEMORYBANK_REASONING_ENGINE_ID: "${MEMORYBANK_REASONING_ENGINE_ID}"
      GOOGLE_APPLICATION_CREDENTIALS: "/absolute/path/service-account.json"
      MEMORYBANK_SCOPE: '{"user_id":"your-user-id"}'
    trust: untrusted
    tools:
      include: [memorybank_search, memorybank_remember, memorybank_forget, memorybank_correct, memorybank_stats]
```

Hermes filters stdio-child environments. Pass an absolute service-account credential path, or pass `HOME` for user ADC created with `gcloud auth application-default login`. The server rejects unresolved `${...}` values in required settings. Test with:

```bash
hermes mcp test agent_platform_memorybank
```

The `mcp_servers` key is user-chosen: the recommended `agent_platform_memorybank` key exposes `mcp__agent_platform_memorybank__memorybank_search`; another key changes that `mcp__<key>__...` prefix. The server writes JSON-RPC only to stdout and `[memorybank]` diagnostics only to stderr.

## Can Hermes and OpenClaw share memories?

Yes, if they use the same project, location, reasoning engine, and exact scope. By default, Hermes uses `{ "agent_name": "hermes" }`, so it remains isolated until `MEMORYBANK_SCOPE` explicitly matches OpenClaw's `scope`.

## What is the pricing model?

As of **2026-08-21**, billing for Agent Platform Memory Bank is announced to begin **September 1, 2026**. The official pricing page lists:

- $0.000410959/GiB-hour for Agent Storage, with the first 1 GiB-month free;
- $0.085 Agent Compute/vCPU-hour; Memory Bank meters one vCPU-hour per 3 million reads and per 1 million writes, with the first 50 vCPU-hours free where stated on the official page;
- separate model-generation and embedding-token charges.

Avoid repository cost estimates because usage and model pricing vary. Consult the canonical [Gemini Enterprise Agent Platform pricing page](https://cloud.google.com/products/gemini-enterprise-agent-platform/pricing).

## Where are the official docs?

- [Memory Bank overview](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/memory-bank)
- [Setup](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/memory-bank/setup)
- [API quickstart](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/memory-bank/api-quickstart)
- [Generate memories](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/memory-bank/generate-memories)
- [Fetch memories](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/memory-bank/fetch-memories)
- [Memory Profiles](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/memory-bank/profiles)
- [Agent Platform REST reference](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/rest)
- [Pricing](https://cloud.google.com/products/gemini-enterprise-agent-platform/pricing)
