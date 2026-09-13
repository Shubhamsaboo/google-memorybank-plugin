#!/usr/bin/env node
import { readFileSync } from "node:fs";
import { createInterface } from "node:readline";
import { hermesConfigFromEnv, MemoryBankService } from "./memorybank-core.js";

const JSON_RPC_VERSION = "2.0";
// Keep this in sync with the legacy stdio protocol versions accepted by
// Hermes' mcp_types 2.0.0 client.
const SUPPORTED_PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"] as const;
const LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0];
const SERVER_VERSION = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).version as string;

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
type Request = { jsonrpc?: unknown; id?: Json; method?: unknown; params?: unknown };
type ToolHandler = (arguments_: Record<string, unknown>) => Promise<unknown>;

export const TOOL_DEFINITIONS = [
  {
    name: "memorybank_search",
    description: "Search Agent Platform Memory Bank by semantic similarity distance.",
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true },
    inputSchema: {
      type: "object", additionalProperties: false,
      properties: { query: { type: "string", minLength: 1 }, top_k: { type: "integer", minimum: 1, maximum: 100 } },
      required: ["query"],
    },
  },
  {
    name: "memorybank_remember",
    description: "Directly store a fact in Agent Platform Memory Bank; duplicates can persist until later generation consolidation.",
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false },
    inputSchema: {
      type: "object", additionalProperties: false,
      properties: { fact: { type: "string", minLength: 1, maxLength: 10000 } }, required: ["fact"],
    },
  },
  {
    name: "memorybank_forget",
    description: "Permanently delete a memory by its ID or resource name.",
    annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: true },
    inputSchema: {
      type: "object", additionalProperties: false,
      properties: { memory_id: { type: "string", minLength: 1 } }, required: ["memory_id"],
    },
  },
  {
    name: "memorybank_correct",
    description: "Correct a memory's fact text, restoring the old fact if fallback regeneration fails when possible.",
    annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: false },
    inputSchema: {
      type: "object", additionalProperties: false,
      properties: { memory_id: { type: "string", minLength: 1 }, new_fact: { type: "string", minLength: 1, maxLength: 10000 } },
      required: ["memory_id", "new_fact"],
    },
  },
  {
    name: "memorybank_stats",
    description: "Return the count and topic breakdown for the configured scope.",
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true },
    inputSchema: { type: "object", additionalProperties: false, properties: {} },
  },
] as const;

function isObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function rpcError(id: Json | undefined, code: number, message: string): Record<string, Json> {
  return { jsonrpc: JSON_RPC_VERSION, id: id ?? null, error: { code, message } };
}

function result(id: Json, value: Json): Record<string, Json> {
  return { jsonrpc: JSON_RPC_VERSION, id, result: value };
}

function requireString(args: Record<string, unknown>, name: string, maxLength = 10000): string {
  const value = args[name];
  if (typeof value !== "string" || !value.trim()) throw new Error(`${name} must be a non-empty string.`);
  if (value.length > maxLength) throw new Error(`${name} must be at most ${maxLength} characters.`);
  return value;
}

function assertOnly(args: Record<string, unknown>, names: string[]): void {
  for (const name of Object.keys(args)) if (!names.includes(name)) throw new Error(`Unexpected argument: ${name}.`);
}

function negotiateProtocolVersion(value: unknown): string {
  // A legacy initialize response counter-offers the newest version we support.
  // Hermes treats an initialize error as a signal to try its stateless protocol,
  // which this stdio server intentionally does not implement.
  return typeof value === "string" && (SUPPORTED_PROTOCOL_VERSIONS as readonly string[]).includes(value)
    ? value
    : LATEST_PROTOCOL_VERSION;
}

export function createMcpRequestHandler(service: Pick<MemoryBankService, "search" | "remember" | "forget" | "correct" | "stats">) {
  const tools: Record<string, ToolHandler> = {
    async memorybank_search(args) {
      assertOnly(args, ["query", "top_k"]);
      const query = requireString(args, "query");
      const topK = args.top_k;
      if (topK !== undefined && (!Number.isInteger(topK) || (topK as number) < 1 || (topK as number) > 100)) {
        throw new Error("top_k must be an integer from 1 to 100.");
      }
      return service.search(query, topK as number | undefined);
    },
    async memorybank_remember(args) {
      assertOnly(args, ["fact"]);
      await service.remember(requireString(args, "fact"));
      return { remembered: true };
    },
    async memorybank_forget(args) {
      assertOnly(args, ["memory_id"]);
      const memoryId = requireString(args, "memory_id");
      await service.forget(memoryId);
      return { forgotten: true, memory_id: memoryId };
    },
    async memorybank_correct(args) {
      assertOnly(args, ["memory_id", "new_fact"]);
      const memoryId = requireString(args, "memory_id");
      return { memory_id: memoryId, ...(await service.correct(memoryId, requireString(args, "new_fact"))) };
    },
    async memorybank_stats(args) {
      assertOnly(args, []);
      return service.stats();
    },
  };

  return async (request: Request): Promise<Record<string, Json> | undefined> => {
    const isNotification = isObject(request) && request.id === undefined;
    if (!isObject(request) || request.jsonrpc !== JSON_RPC_VERSION || typeof request.method !== "string") {
      return isNotification ? undefined : rpcError(isObject(request) ? request.id as Json | undefined : undefined, -32600, "Invalid JSON-RPC request.");
    }
    if (request.method === "notifications/initialized") return undefined;
    if (request.method === "initialize") {
      const protocolVersion = negotiateProtocolVersion(isObject(request.params) ? request.params.protocolVersion : undefined);
      return isNotification ? undefined : result(request.id as Json, {
        protocolVersion,
        capabilities: { tools: {} },
        serverInfo: { name: "agent-platform-memorybank", version: SERVER_VERSION },
      });
    }
    if (request.method === "tools/list") {
      return isNotification ? undefined : result(request.id as Json, { tools: TOOL_DEFINITIONS as unknown as Json });
    }
    if (request.method === "tools/call") {
      if (!isObject(request.params) || typeof request.params.name !== "string") {
        return isNotification ? undefined : rpcError(request.id as Json | undefined, -32602, "tools/call requires params.name.");
      }
      const handler = tools[request.params.name];
      if (!handler) return isNotification ? undefined : rpcError(request.id as Json | undefined, -32602, `Unknown tool: ${request.params.name}.`);
      if (request.params.arguments !== undefined && !isObject(request.params.arguments)) {
        return isNotification ? undefined : rpcError(request.id as Json | undefined, -32602, "tools/call params.arguments must be an object.");
      }
      try {
        const toolResult = await handler((request.params.arguments as Record<string, unknown>) || {});
        const toolFailed = isObject(toolResult) && toolResult.corrected === false;
        return isNotification ? undefined : result(request.id as Json, {
          content: [{ type: "text", text: JSON.stringify(toolResult, null, 2) }],
          ...(toolFailed ? { isError: true } : {}),
        });
      } catch (error: any) {
        return isNotification ? undefined : result(request.id as Json, {
          content: [{ type: "text", text: error?.message || "Memory Bank operation failed." }], isError: true,
        });
      }
    }
    return isNotification ? undefined : rpcError(request.id as Json | undefined, -32601, `Method not found: ${request.method}.`);
  };
}

export function runMcpServer(): void {
  let handler: ReturnType<typeof createMcpRequestHandler>;
  try {
    // Hermes intentionally has a separate default scope. Set MEMORYBANK_SCOPE to
    // the same explicit JSON object in both runtimes to share memories.
    handler = createMcpRequestHandler(new MemoryBankService(hermesConfigFromEnv()));
  } catch (error: any) {
    process.stderr.write(`[memorybank] configuration error: ${error.message}\n`);
    process.exitCode = 1;
    return;
  }
  const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
  input.on("line", async (line) => {
    // Empty delimiter lines are not JSON-RPC messages and must not produce parse errors.
    if (!line.trim()) return;
    let request: Request;
    try {
      request = JSON.parse(line) as Request;
    } catch {
      process.stdout.write(`${JSON.stringify(rpcError(undefined, -32700, "Parse error."))}\n`);
      return;
    }
    try {
      const response = await handler(request);
      if (response) process.stdout.write(`${JSON.stringify(response)}\n`);
    } catch (error: any) {
      process.stderr.write(`[memorybank] internal MCP error: ${error?.message || error}\n`);
      if (request.id !== undefined) process.stdout.write(`${JSON.stringify(rpcError(request.id as Json, -32603, "Internal error."))}\n`);
    }
  });
}
