import { createHash } from "crypto";
import { readFileSync, writeFileSync, existsSync, readdirSync, statSync } from "fs";
import { join } from "path";

// --- SDK Clients ---
import { v1beta1 } from "@google-cloud/aiplatform";
import { getMemoryBankClient as getSharedMemoryBankClient, parentName as sharedParentName, MemoryBankService, formatMemory, type MemoryBankConfig as SharedMemoryBankConfig } from "./memorybank-core.js";

// The Memory Bank client cache is shared with the Hermes adapter and keyed by
// endpoint, preventing an independent location from reusing the wrong client.
const reasoningEngineClients = new Map<string, v1beta1.ReasoningEngineServiceClient>();

function getMemoryBankClient(cfg: MemoryBankConfig): v1beta1.MemoryBankServiceClient {
  return getSharedMemoryBankClient(cfg) as unknown as v1beta1.MemoryBankServiceClient;
}

function getReasoningEngineClient(cfg: MemoryBankConfig): v1beta1.ReasoningEngineServiceClient {
  const endpoint = `${cfg.location}-aiplatform.googleapis.com`;
  let client = reasoningEngineClients.get(endpoint);
  if (!client) {
    client = new v1beta1.ReasoningEngineServiceClient({ apiEndpoint: endpoint });
    reasoningEngineClients.set(endpoint, client);
  }
  return client;
}

// --- Config ---

interface MemoryBankConfig extends SharedMemoryBankConfig {
  autoRecall?: boolean;
  autoCapture?: boolean;
  autoSyncFiles?: boolean;
  autoSyncTopics?: boolean;
  memoryTopics?: Array<any>;
  perspective?: "first" | "third";
  backgroundGenerate?: boolean;
  // Plugin setting: duration in seconds, mapped to generated-memory TTL configuration.
  ttlSeconds?: number;
  // Plugin presentation setting: what metadata to include in auto-recalled memories
  // "off" = just facts, "scores" = facts + similarity score (default)
  introspection?: "off" | "scores";
}

// --- Default memory topics ---
const DEFAULT_TOPICS = [
  { managed_memory_topic: { managed_topic_enum: "USER_PREFERENCES" } },
  { managed_memory_topic: { managed_topic_enum: "EXPLICIT_INSTRUCTIONS" } },
  { managed_memory_topic: { managed_topic_enum: "KEY_CONVERSATION_DETAILS" } },
  {
    custom_memory_topic: {
      label: "technical_decisions",
      description:
        "Architecture choices, tool evaluations, technology selections, and their rationale. Do NOT include routine debugging steps, temporary error messages, or operational status checks.",
    },
  },
  {
    custom_memory_topic: {
      label: "project_context",
      description:
        "Project names, repository URLs, team members, roles, system configurations, and relationships. Do NOT include transient operational details like 'gateway restarted' or 'build succeeded'.",
    },
  },
  {
    custom_memory_topic: {
      label: "action_items",
      description:
        "Tasks assigned, deadlines, commitments, follow-ups, and their completion status. Do NOT include routine status checks or acknowledgments like 'done' or 'checking'.",
    },
  },
];

// --- Few-shot examples: guide Agent Platform Memory Bank extraction ---
const DEFAULT_FEW_SHOTS = [
  // Negative: short status check, no memory
  {
    conversationSource: {
      events: [
        { content: { role: "user", parts: [{ text: "done?" }] } },
        { content: { role: "model", parts: [{ text: "Yes, the build succeeded and gateway restarted." }] } },
      ],
    },
    generatedMemories: [],
  },
  // Negative: debugging chatter, no memory
  {
    conversationSource: {
      events: [
        { content: { role: "user", parts: [{ text: "what's in the logs?" }] } },
        { content: { role: "model", parts: [{ text: "I see a 400 error on the config field. Let me fix the REST API field name." }] } },
      ],
    },
    generatedMemories: [],
  },
  // Positive: real decision worth remembering
  {
    conversationSource: {
      events: [
        { content: { role: "user", parts: [{ text: "I don't want my memories to go away after 90 days" }] } },
        { content: { role: "model", parts: [{ text: "You're right. TTL removed from config. Memories persist forever by default." }] } },
      ],
    },
    generatedMemories: [
      { fact: "The user does not want memories to expire. TTL should not be enabled by default.", topics: [{ managed_memory_topic: "USER_PREFERENCES" }] },
    ],
  },
  // Positive: user preference
  {
    conversationSource: {
      events: [
        { content: { role: "user", parts: [{ text: "no version bump is needed" }] } },
        { content: { role: "model", parts: [{ text: "Got it. Skipping version bump." }] } },
      ],
    },
    generatedMemories: [
      { fact: "The user prefers not to bump versions for incremental changes.", topics: [{ managed_memory_topic: "USER_PREFERENCES" }] },
    ],
  },
];

// --- File sync state ---
interface SyncIndex {
  version: number;
  entries: Record<string, { hash: string; syncedAt: string }>;
}

let syncIndex: SyncIndex = { version: 1, entries: {} };
let syncIndexPath = "";

function loadSyncIndex(workspaceDir: string): void {
  syncIndexPath = join(workspaceDir, ".memorybank-sync.json");
  if (existsSync(syncIndexPath)) {
    try {
      syncIndex = JSON.parse(readFileSync(syncIndexPath, "utf8"));
    } catch {
      syncIndex = { version: 1, entries: {} };
    }
  }
}

function saveSyncIndex(): void {
  if (!syncIndexPath) return;
  try {
    writeFileSync(syncIndexPath, JSON.stringify(syncIndex, null, 2));
  } catch (e: any) {
    console.error(`[memorybank] failed to save sync index: ${e.message}`);
  }
}

function hashContent(content: string): string {
  return createHash("sha256").update(content).digest("hex").slice(0, 16);
}

// --- Collect memory files ---
interface MemoryFile {
  path: string;
  relativePath: string;
  content: string;
  hash: string;
}

function collectMemoryFiles(workspaceDir: string): MemoryFile[] {
  const files: MemoryFile[] = [];
  const topLevel = ["MEMORY.md", "USER.md", "SOUL.md", "TOOLS.md"];

  for (const name of topLevel) {
    const fullPath = join(workspaceDir, name);
    if (existsSync(fullPath)) {
      const content = readFileSync(fullPath, "utf8");
      files.push({ path: fullPath, relativePath: name, content, hash: hashContent(content) });
    }
  }

  const memoryDir = join(workspaceDir, "memory");
  if (existsSync(memoryDir) && statSync(memoryDir).isDirectory()) {
    for (const name of readdirSync(memoryDir)) {
      if (!name.endsWith(".md")) continue;
      const fullPath = join(memoryDir, name);
      if (!statSync(fullPath).isFile()) continue;
      const content = readFileSync(fullPath, "utf8");
      files.push({ path: fullPath, relativePath: `memory/${name}`, content, hash: hashContent(content) });
    }
  }

  return files;
}

function getChangedFiles(files: MemoryFile[]): MemoryFile[] {
  return files.filter((f) => {
    const existing = syncIndex.entries[f.relativePath];
    return !existing || existing.hash !== f.hash;
  });
}

// --- Helpers ---
function parentName(cfg: MemoryBankConfig): string {
  return sharedParentName(cfg);
}

// Convert scope Record to the SDK's map format
function scopeToSdk(scope: Record<string, string>): { [key: string]: string } {
  return { ...scope };}

// --- Core operations ---

async function retrieveMemories(cfg: MemoryBankConfig, query: string): Promise<any[]> {
  try {
    return await new MemoryBankService(cfg).retrieve(query);
  } catch (e: any) {
    console.error(`[memorybank] retrieve error: ${e.message}`);
    return [];
  }
}

// Send the last message pair to Agent Platform Memory Bank for extraction and consolidation.
async function captureFromConversation(
  cfg: MemoryBankConfig,
  messages: Array<{ role: string; content: string }>
): Promise<void> {
  const parent = parentName(cfg);
  const scope = cfg.scope || { agent_name: "openclaw" };
  const client = getMemoryBankClient(cfg);

  // Only send the last user+assistant pair (not the whole conversation)
  const lastPair = messages
    .filter((m) => m.role === "user" || m.role === "assistant")
    .slice(-2);

  if (lastPair.length === 0) return;

  const events = lastPair.map((m) => ({
    content: {
      role: m.role === "assistant" ? "model" : "user",
      parts: [{ text: m.content.slice(0, 4000) }],
    },
  }));

  // Fire-and-forget: don't block agent waiting for consolidation results
  client.generateMemories({
    parent,
    scope: scopeToSdk(scope),
    directContentsSource: { events },
  }).then(async ([operation]) => {
    console.log("[memorybank] capture fired (bg)");
    const [result] = await (operation as any).promise();
    const generated = (result as any)?.generatedMemories || [];
    if (generated.length > 0) {
      const created = generated.filter((m: any) => m.action === "CREATED").length;
      const updated = generated.filter((m: any) => m.action === "UPDATED").length;
      const deleted = generated.filter((m: any) => m.action === "DELETED").length;
      const facts = generated
        .filter((m: any) => m.action === "CREATED" || m.action === "UPDATED")
        .map((m: any) => m.memory?.fact || "")
        .filter((f: string) => f);
      console.log(
        `[memorybank] captured: ${created} new, ${updated} updated, ${deleted} deleted`
      );
      if (facts.length > 0) {
        console.log(`[memorybank] facts: ${facts.join(" | ")}`);
      }
    }
  }).catch((e: any) => {
    console.error(`[memorybank] capture error: ${e.message}`);
  });
}

async function syncFiles(cfg: MemoryBankConfig, files: MemoryFile[]): Promise<void> {
  const parent = parentName(cfg);
  const scope = cfg.scope || { agent_name: "openclaw" };
  const client = getMemoryBankClient(cfg);

  for (const file of files) {
    const chunks: string[] = [];
    for (let i = 0; i < file.content.length; i += 2000) {
      chunks.push(file.content.slice(i, i + 2000));
    }

    const events = chunks.map((chunk) => ({
      content: {
        role: "user" as const,
        parts: [{ text: `[File: ${file.relativePath}]\n${chunk}` }],
      },
    }));

    try {
      const [operation] = await client.generateMemories({
        parent,
        scope: scopeToSdk(scope),
        directContentsSource: { events },
      });
      await (operation as any).promise();
      syncIndex.entries[file.relativePath] = {
        hash: file.hash,
        syncedAt: new Date().toISOString(),
      };
      saveSyncIndex();
      console.log(`[memorybank] synced file: ${file.relativePath}`);
    } catch (e: any) {
      console.error(`[memorybank] file sync error (${file.relativePath}): ${e.message}`);
    }
  }
}

async function syncInstanceConfig(cfg: MemoryBankConfig): Promise<void> {
  const parent = parentName(cfg);
  const topics = cfg.memoryTopics || DEFAULT_TOPICS;
  const perspective = cfg.perspective || "third";
  const reClient = getReasoningEngineClient(cfg);

  try {
    const customizationConfig: any = {
      memory_topics: topics,
      enable_third_person_memories: perspective !== "first",
    };

    // Add few-shot examples if using default topics
    if (!cfg.memoryTopics) {
      customizationConfig.generate_memories_examples = DEFAULT_FEW_SHOTS;
    }

    const memoryBankConfig: any = {
      customization_configs: [customizationConfig],
    };

    // TTL: auto-expire memories after configured duration
    if (cfg.ttlSeconds && cfg.ttlSeconds > 0) {
      memoryBankConfig.ttl_config = {
        generateCreatedTtl: `${cfg.ttlSeconds}s`,
        generateUpdatedTtl: `${cfg.ttlSeconds}s`,
      };
    }

    const response = (await (reClient.updateReasoningEngine as any)({
      reasoningEngine: {
        name: parent,
        spec: {
          contextSpec: { memoryBankConfig },
        },
      },
      updateMask: { paths: ["spec.context_spec.memory_bank_config"] },
    })) as any[];
    const operation = response[0];
    await operation.promise();

    const parts = [`${topics.length} topics`, `${perspective}-person`];
    if (cfg.ttlSeconds) parts.push(`TTL ${Math.round(cfg.ttlSeconds / 86400)}d`);
    console.log(`[memorybank] synced config: ${parts.join(", ")}`);
  } catch (e: any) {
    console.error(`[memorybank] config sync error: ${e.message}`);
  }
}

// --- Direct memory creation ---
async function createMemory(cfg: MemoryBankConfig, fact: string): Promise<void> {
  try {
    await new MemoryBankService(cfg).remember(fact);
    console.log(`[memorybank] remembered: ${fact}`);
  } catch (e: any) {
    console.error(`[memorybank] create memory error: ${e.message}`);
    throw e;
  }
}

// --- Delete a memory ---
async function deleteMemory(cfg: MemoryBankConfig, memoryId: string): Promise<void> {
  await new MemoryBankService(cfg).forget(memoryId);
  console.log(`[memorybank] deleted memory: ${memoryId}`);
}

// --- Memory counting ---
//
// TODO(google-api): The memories.list endpoint does NOT return a totalSize field,
// and there is no memories:count RPC. We verified this by:
//   1. Requesting $fields=totalSize — returns "Cannot find matching fields for path 'totalSize'"
//   2. Checking discovery doc — only list/get/create/patch/delete/generate/retrieve/rollback/purge
//   3. Checking v1alpha1 — returns 404 (does not exist)
//   4. Max pageSize is 100 even when requesting 1000
//
// This means counting requires paginating through ALL memories. We mitigate this by:
//   - Caching the count in-memory with a 5-minute TTL
//   - Auto-incrementing/decrementing on create/delete within the session
//
// When Google adds totalSize or a count RPC to the memories.list response,
// this pagination loop should be replaced with a single API call.
// Track: https://google.aip.dev/132 (standard List should include totalSize)

interface CountCache {
  count: number;
  fetchedAt: number;
}

const COUNT_CACHE_TTL_MS = 5 * 60 * 1000; // 5 minutes
let countCache: CountCache | null = null;

/**
 * Count memories using paginated list (lightweight).
 */
async function countMemories(cfg: MemoryBankConfig, opts?: { force?: boolean }): Promise<number> {
  // Return cached count if fresh
  if (!opts?.force && countCache && (Date.now() - countCache.fetchedAt) < COUNT_CACHE_TTL_MS) {
    return countCache.count;
  }

  try {
    const total = (await new MemoryBankService(cfg).list()).length;
    countCache = { count: total, fetchedAt: Date.now() };
    return total;
  } catch (e: any) {
    console.error(`[memorybank] count error: ${e.message}`);
    // Return stale cache if available, otherwise 0
    return countCache?.count ?? 0;
  }
}

/** Adjust cached count without re-fetching (call after create/delete). */
function adjustCachedCount(delta: number): void {
  if (countCache) {
    countCache.count = Math.max(0, countCache.count + delta);
  }
}

/**
 * List all memories in scope with full details (paginated).
 * Use countMemories() instead when you only need the count.
 */
async function listMemories(
  cfg: MemoryBankConfig,
  scope?: Record<string, string>
): Promise<any[]> {
  try {
    const all = (await new MemoryBankService({ ...cfg, scope: scope || cfg.scope }).list())
      .map((memory: any) => ({ memory }));
    countCache = { count: all.length, fetchedAt: Date.now() };
    return all;
  } catch (e: any) {
    console.error(`[memorybank] list error: ${e.message}`);
    throw e;
  }
}

// --- Plugin ---
const plugin = {
  id: "openclaw-vertexai-memorybank",
  name: "Memory (Agent Platform Memory Bank)",
  kind: "general" as const,

  register(api: any) {
    const config = api.pluginConfig as MemoryBankConfig;
    const autoRecall = config.autoRecall !== false;
    const autoCapture = config.autoCapture !== false;
    const autoSyncFiles = config.autoSyncFiles !== false;
    const autoSyncTopics = config.autoSyncTopics !== false;
    const backgroundGenerate = config.backgroundGenerate !== false;

    const workspaceDir =
      api.workspaceDir ||
      process.env.OPENCLAW_WORKSPACE ||
      join(process.env.HOME || process.env.USERPROFILE || ".", ".openclaw", "workspace");

    // --- Startup service ---
    api.registerService({
      id: "memorybank-sync",
      async start() {
        if (autoSyncTopics) await syncInstanceConfig(config);
        if (autoSyncFiles) {
          loadSyncIndex(workspaceDir);
          const files = collectMemoryFiles(workspaceDir);
          const changed = getChangedFiles(files);
          if (changed.length > 0) {
            console.log(`[memorybank] startup: ${changed.length} changed file(s) to sync`);
            await syncFiles(config, changed);
          } else {
            console.log("[memorybank] startup: all files in sync");
          }
        }
      },
    });

    // --- Auto-recall ---
    if (autoRecall) {
      api.on("before_agent_start", async (event: any) => {
        const query = event.prompt || "";
        if (!query || query.length < 5) return;

        const memories = await retrieveMemories(config, query);
        if (memories.length === 0) return;

        const introspection = config.introspection || "scores";
        const formatted = memories
          .map((m: any, i: number) => {
            const fact = m.memory?.fact || m.fact || JSON.stringify(m);
            if (introspection === "off") {
              return `${i + 1}. ${fact}`;
            }
            // "scores" (default) — include similarity score
            const score = m.score ?? m.similarity ?? m.distance ?? null;
            const scoreStr = score != null ? ` [score: ${(typeof score === "number" ? score.toFixed(3) : score)}]` : "";
            return `${i + 1}. ${fact}${scoreStr}`;
          })
          .join("\n");

        return {
          prependContext: `<agent_platform_memory_bank>\nRelevant memories from prior sessions:\n${formatted}\n</agent_platform_memory_bank>`,
        };
      });
    }

    // --- Auto-capture ---
    if (autoCapture) {
      api.on("agent_end", async (event: any) => {
        if (!event.success) return;

        // 1. Capture last message pair
        const messages = (event.messages || [])
          .filter((m: any) => m.role === "user" || m.role === "assistant")
          .map((m: any) => ({
            role: m.role,
            content: typeof m.content === "string" ? m.content : JSON.stringify(m.content),
          }));

        // Skip capture if messages are too short (noise filter)
        const lastUserMsg = [...messages].reverse().find((m: any) => m.role === "user");
        const lastAssistantMsg = [...messages].reverse().find((m: any) => m.role === "assistant");
        const userLen = lastUserMsg?.content?.length || 0;
        const totalLen = (lastUserMsg?.content?.length || 0) + (lastAssistantMsg?.content?.length || 0);

        if (userLen < 20 || totalLen < 100) {
          console.log(`[memorybank] skipped capture: too short (user=${userLen}, total=${totalLen})`);
        } else if (messages.length > 0) {
          const capture = captureFromConversation(config, messages);
          if (!backgroundGenerate) await capture;
          else capture.catch((e) => console.error(`[memorybank] bg capture error: ${e}`));
        }

        // 2. Sync changed files
        if (autoSyncFiles) {
          try {
            const files = collectMemoryFiles(workspaceDir);
            const changed = getChangedFiles(files);
            if (changed.length > 0) {
              console.log(`[memorybank] agent_end: ${changed.length} changed file(s) to sync`);
              const sync = syncFiles(config, changed);
              if (!backgroundGenerate) await sync;
              else sync.catch((e) => console.error(`[memorybank] bg file sync error: ${e}`));
            }
          } catch (e: any) {
            console.error(`[memorybank] file change detection error: ${e.message}`);
          }
        }
      });
    }

    // --- Agent tools ---

    // memorybank_search — Search memories by semantic similarity
    api.registerTool({
      name: "memorybank_search",
      description: "Search the Memory Bank for memories semantically similar to a query. Returns matching facts with similarity scores, topics, and timestamps.",
      label: "Memory Search",
      parameters: {
        type: "object",
        properties: {
          query: { type: "string", description: "Semantic search query" },
          top_k: { type: "number", description: "Max results to return (default: 10)" },
        },
        required: ["query"],
      },
      async execute(_toolCallId: string, params: { query: string; top_k?: number }) {
        const searchConfig = { ...config, topK: params.top_k || config.topK || 10 };
        const memories = await retrieveMemories(searchConfig, params.query);
        const results = memories.map((memory: any, i: number) => formatMemory(memory, i + 1));
        return {
          content: [{ type: "text" as const, text: JSON.stringify({ count: results.length, memories: results }, null, 2) }],
          details: { count: results.length },
        };
      },
    });

    // memorybank_forget — Delete a memory by ID
    api.registerTool({
      name: "memorybank_forget",
      description: "Delete (forget) a specific memory by its ID. Permanently removes it from the Memory Bank.",
      label: "Memory Forget",
      parameters: {
        type: "object",
        properties: {
          memory_id: { type: "string", description: "The memory ID (resource name) to delete" },
        },
        required: ["memory_id"],
      },
      async execute(_toolCallId: string, params: { memory_id: string }) {
        try {
          await deleteMemory(config, params.memory_id);
          adjustCachedCount(-1);
          return {
            content: [{ type: "text" as const, text: `Memory deleted: ${params.memory_id}` }],
            details: { deleted: true },
          };
        } catch (e: any) {
          return {
            content: [{ type: "text" as const, text: `Error deleting memory: ${e.message}` }],
            details: { deleted: false, error: e.message },
            isError: true,
          };
        }
      },
    });

    // memorybank_correct — Update a memory's fact text
    api.registerTool({
      name: "memorybank_correct",
      description: "Update/correct a memory's fact text. The old memory is replaced with the corrected version.",
      label: "Memory Correct",
      parameters: {
        type: "object",
        properties: {
          memory_id: { type: "string", description: "The memory ID to update" },
          new_fact: { type: "string", description: "The corrected fact text" },
        },
        required: ["memory_id", "new_fact"],
      },
      async execute(_toolCallId: string, params: { memory_id: string; new_fact: string }) {
        try {
          const result = await new MemoryBankService(config).correct(params.memory_id, params.new_fact);
          countCache = null; // Generation may consolidate multiple facts.
          return {
            content: [{ type: "text" as const, text: JSON.stringify(result, null, 2) }],
            details: result,
            ...(result.corrected ? {} : { isError: true }),
          };
        } catch (e: any) {
          return {
            content: [{ type: "text" as const, text: `Failed to update memory: ${e.message}` }],
            details: { corrected: false, error: e.message },
            isError: true,
          };
        }
      },
    });

    // memorybank_stats — Fresh scoped count and topic breakdown
    api.registerTool({
      name: "memorybank_stats",
      description: "Get fresh Memory Bank statistics: total count, breakdown by topic, and scope info.",
      label: "Memory Stats",
      parameters: {
        type: "object",
        properties: {
          force_refresh: { type: "boolean", description: "Accepted for compatibility; statistics always refresh" },
        },
      },
      async execute(_toolCallId: string, params: { force_refresh?: boolean }) {
        try {
          const stats = await new MemoryBankService(config).stats();
          countCache = { count: stats.totalMemories, fetchedAt: Date.now() };
          return {
            content: [{ type: "text" as const, text: JSON.stringify(stats, null, 2) }],
            details: { totalMemories: stats.totalMemories },
          };
        } catch (e: any) {
          return {
            content: [{ type: "text" as const, text: `Error getting stats: ${e.message}` }],
            details: { error: e.message },
            isError: true,
          };
        }
      },
    });

    // --- CLI ---
    api.registerCli(
      (ctx: any) => {
        const prog = ctx.program;

        prog
          .command("memorybank-status")
          .description("Show Memory Bank status")
          .action(async () => {
            console.log("Agent Platform Memory Bank Plugin");
            console.log(`  Project:     ${config.projectId}`);
            console.log(`  Location:    ${config.location}`);
            console.log(`  Engine:      ${config.reasoningEngineId}`);
            console.log(`  Scope:       ${JSON.stringify(config.scope || { agent_name: "openclaw" })}`);
            console.log(`  Recall:      ${autoRecall}`);
            console.log(`  Capture:     ${autoCapture}`);
            console.log(`  File Sync:   ${autoSyncFiles}`);
            console.log(`  Topic Sync:  ${autoSyncTopics}`);
            console.log(`  Perspective: ${config.perspective || "third"}-person`);
            console.log(`  TTL:         ${config.ttlSeconds ? `${Math.round(config.ttlSeconds / 86400)} days` : "none (memories persist forever)"}`);
            console.log(`  Introspect:  ${config.introspection || "scores"}`);
            console.log("  Auth:        SDK (ADC)");
            try {
              const total = await countMemories(config, { force: true });
              console.log(`  Memories:    ${total} in scope`);
            } catch (e: any) {
              console.log(`  Memories:    error (${e.message})`);
            }
            if (autoSyncFiles) {
              loadSyncIndex(workspaceDir);
              const files = collectMemoryFiles(workspaceDir);
              const changed = getChangedFiles(files);
              console.log(`  Files:       ${files.length} tracked, ${changed.length} pending`);
            }
          });

        prog
          .command("memorybank-search")
          .description("Search memories")
          .argument("<query>", "Search query")
          .option("--top-k <n>", "Max results", "10")
          .option("--show-ids", "Show memory IDs")
          .action(async (query: string, opts: any) => {
            const memories = await retrieveMemories({ ...config, topK: parseInt(opts.topK) }, query);
            if (memories.length === 0) return console.log("No memories found.");
            memories.forEach((m: any, i: number) => {
              const fact = m.memory?.fact || m.fact || JSON.stringify(m);
              const id = m.memory?.name?.split("/").pop() || "";
              const dist = m.distance != null ? ` [dist=${m.distance.toFixed(3)}]` : "";
              console.log(`${i + 1}. ${fact}${dist}${opts.showIds && id ? ` (id: ${id})` : ""}`);
            });
          });

        prog
          .command("memorybank-list")
          .description("List all memories in scope")
          .option("--show-ids", "Show memory IDs")
          .option("--count-only", "Only show count (uses lightweight paginated API)")
          .action(async (opts: any) => {
            if (opts.countOnly) {
              const total = await countMemories(config, { force: true });
              console.log(`Total memories: ${total}`);
              return;
            }
            const memories = await listMemories(config);
            if (memories.length === 0) return console.log("No memories in scope.");
            console.log(`Total: ${memories.length} memories\n`);
            memories.forEach((m: any, i: number) => {
              const fact = m.memory?.fact || m.fact || JSON.stringify(m);
              const id = m.memory?.name?.split("/").pop() || "";
              console.log(`${i + 1}. ${fact}${opts.showIds && id ? ` (id: ${id})` : ""}`);
            });
          });

        prog
          .command("memorybank-sync")
          .description("Manually sync files")
          .action(async () => {
            loadSyncIndex(workspaceDir);
            const files = collectMemoryFiles(workspaceDir);
            const changed = getChangedFiles(files);
            if (changed.length === 0) return console.log("All files in sync.");
            console.log(`Syncing ${changed.length} file(s)...`);
            await syncFiles(config, changed);
            console.log("Done.");
          });

        prog
          .command("memorybank-remember")
          .description("Directly store a fact as a memory")
          .argument("<fact>", "The fact to remember")
          .action(async (fact: string) => {
            try {
              await createMemory(config, fact);
              adjustCachedCount(1);
              console.log("Stored.");
            } catch {
              console.log("Failed to store memory.");
            }
          });

        prog
          .command("memorybank-forget")
          .description("Delete a memory by ID")
          .argument("<memoryId>", "Memory ID to delete (use --show-ids with search/list to find IDs)")
          .action(async (memoryId: string) => {
            try {
              await deleteMemory(config, memoryId);
              adjustCachedCount(-1);
              console.log("Deleted.");
            } catch (e: any) {
              console.error(`Failed to delete: ${e.message}`);
            }
          });
      },
      { commands: ["memorybank-status", "memorybank-search", "memorybank-list", "memorybank-sync", "memorybank-remember", "memorybank-forget"] }
    );
  },
};

export default plugin;
