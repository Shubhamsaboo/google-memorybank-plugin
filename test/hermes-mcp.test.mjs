import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { createInterface } from "node:readline";
import plugin from "../dist/index.js";
import { spawn } from "node:child_process";
import { createMcpRequestHandler, TOOL_DEFINITIONS } from "../dist/hermes-mcp.js";
import { getMemoryBankClient, hermesConfigFromEnv, MemoryBankService, parentName, resetMemoryBankClientsForTests, setMemoryBankClientFactoryForTests } from "../dist/memorybank-core.js";

const operation = (value = {}) => ({ promise: async () => [value] });
const config = { projectId: "project", location: "us-central1", reasoningEngineId: "engine", scope: { shared_scope: "team" } };
const SUBPROCESS_TIMEOUT_MS = 15000;
const validEnv = {
  MEMORYBANK_PROJECT_ID: "project",
  MEMORYBANK_LOCATION: "us-central1",
  MEMORYBANK_REASONING_ENGINE_ID: "engine",
};

function mockClient(overrides = {}) {
  return {
    retrieveMemories: async () => [{ retrievedMemories: [{ memory: { name: "memories/one", fact: "Use tests", topics: ["engineering"] }, distance: 0.1 }] }],
    createMemory: async () => [operation()],
    deleteMemory: async () => [operation()],
    updateMemory: async () => [operation({ name: "memories/one", fact: "Correct" })],
    getMemory: async () => [{ fact: "Old fact", scope: { shared_scope: "team" } }],
    generateMemories: async () => [operation()],
    listMemories: async () => [[{ name: "memories/one", topics: ["engineering"] }], undefined, {}],
    ...overrides,
  };
}

async function request(handler, id, method, params) {
  return handler({ jsonrpc: "2.0", ...(id === undefined ? {} : { id }), method, params });
}

function callText(response) {
  return JSON.parse(response.result.content[0].text);
}

test("lists annotated MCP tools", async () => {
  const handler = createMcpRequestHandler(new MemoryBankService(config, mockClient(), { agent_name: "hermes" }));
  const response = await request(handler, 1, "tools/list", {});
  assert.deepEqual(response.result.tools.map((tool) => tool.name), TOOL_DEFINITIONS.map((tool) => tool.name));
  const byName = Object.fromEntries(response.result.tools.map((tool) => [tool.name, tool]));
  assert.equal(byName.memorybank_search.annotations.readOnlyHint, true);
  assert.equal(byName.memorybank_stats.annotations.readOnlyHint, true);
  assert.equal(byName.memorybank_forget.annotations.destructiveHint, true);
  assert.equal(byName.memorybank_forget.annotations.idempotentHint, true);
  assert.equal(byName.memorybank_remember.annotations.idempotentHint, false);
});

test("echoes all Hermes mcp_types handshake versions and counter-offers the latest", async () => {
  const handler = createMcpRequestHandler(new MemoryBankService(config, mockClient()));
  for (const protocolVersion of ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"]) {
    const response = await request(handler, 1, "initialize", { protocolVersion });
    assert.equal(response.result.protocolVersion, protocolVersion);
    assert.equal(response.result.serverInfo.name, "agent-platform-memorybank");
  }
  const counterOffer = await request(handler, 2, "initialize", { protocolVersion: "2099-01-01" });
  assert.equal(counterOffer.result.protocolVersion, "2025-11-25");
});

test("calls all memory tools through the mocked Agent Platform client", async () => {
  const calls = [];
  const client = mockClient({
    retrieveMemories: async (input) => { calls.push(["search", input]); return [{ retrievedMemories: [] }]; },
    createMemory: async (input) => { calls.push(["remember", input]); return [operation()]; },
    deleteMemory: async (input) => { calls.push(["forget", input]); return [operation()]; },
    updateMemory: async (input) => { calls.push(["correct", input]); return [operation()]; },
    listMemories: async (input) => { calls.push(["stats", input]); return [[], undefined, {}]; },
  });
  const handler = createMcpRequestHandler(new MemoryBankService(config, client, { agent_name: "hermes" }));
  await request(handler, 1, "tools/call", { name: "memorybank_search", arguments: { query: "test", top_k: 2 } });
  await request(handler, 2, "tools/call", { name: "memorybank_remember", arguments: { fact: "Remember this" } });
  await request(handler, 3, "tools/call", { name: "memorybank_forget", arguments: { memory_id: "one" } });
  await request(handler, 4, "tools/call", { name: "memorybank_correct", arguments: { memory_id: "one", new_fact: "Corrected" } });
  await request(handler, 5, "tools/call", { name: "memorybank_stats", arguments: {} });
  assert.deepEqual(calls.map(([name]) => name), ["search", "remember", "forget", "correct", "stats"]);
  assert.deepEqual(calls[0][1].scope, { shared_scope: "team" });
});

test("correct fallback returns a replacement memory name when generation supplies one", async () => {
  const service = new MemoryBankService(config, mockClient({
    updateMemory: async () => { throw Object.assign(new Error("unsupported"), { code: 12 }); },
    generateMemories: async () => [operation({ generatedMemories: [{ memory: { name: "memories/replacement" } }] })],
  }));
  assert.deepEqual(await service.correct("one", "New fact"), {
    corrected: true, method: "delete-regenerate", replacementMemoryName: "memories/replacement",
  });
});

test("correct fallback restores the old fact when regeneration fails", async () => {
  const calls = [];
  const service = new MemoryBankService(config, mockClient({
    updateMemory: async () => { throw Object.assign(new Error("unsupported"), { code: 12 }); },
    getMemory: async () => [{ fact: "Old fact", scope: { shared_scope: "team" } }],
    deleteMemory: async () => { calls.push("delete"); return [operation()]; },
    generateMemories: async () => { calls.push("generate"); throw new Error("generation failed"); },
    createMemory: async (input) => { calls.push(input.memory.fact); return [operation({ name: "memories/restored" })]; },
  }));
  assert.deepEqual(await service.correct("one", "New fact"), {
    corrected: false, method: "delete-regenerate", recovered: true, error: "generation failed", restoredMemoryName: "memories/restored",
  });
  assert.deepEqual(calls, ["delete", "generate", "Old fact"]);
});

test("correct fallback reports unrecovered failures without claiming success", async () => {
  const service = new MemoryBankService(config, mockClient({
    updateMemory: async () => { throw Object.assign(new Error("unsupported"), { code: 12 }); },
    getMemory: async () => [{ fact: "Old fact", scope: { shared_scope: "team" } }],
    generateMemories: async () => { throw new Error("generation failed"); },
    createMemory: async () => { throw new Error("restore failed"); },
  }));
  const handler = createMcpRequestHandler(service);
  const response = await request(handler, 1, "tools/call", { name: "memorybank_correct", arguments: { memory_id: "one", new_fact: "New" } });
  assert.equal(response.result.isError, true);
  assert.match(callText(response).error, /restore failed/);
  assert.equal(callText(response).recovered, false);
});

test("stats paginates and search applies configured distance filtering", async () => {
  const pages = [];
  const service = new MemoryBankService({ ...config, maxDistance: 0.2 }, mockClient({
    retrieveMemories: async () => [{ retrievedMemories: [
      { memory: { name: "memories/keep", fact: "keep" }, distance: 0.2 },
      { memory: { name: "memories/drop", fact: "drop" }, distance: 0.3 },
    ] }],
    listMemories: async (input, options) => {
      assert.equal(options.autoPaginate, false);
      pages.push(input.pageToken);
      return input.pageToken ? [[{ topics: [{ customMemoryTopicLabel: "two" }] }], undefined, {}] : [[{ topics: [{ customMemoryTopicLabel: "one" }] }], undefined, { nextPageToken: "next" }];
    },
  }));
  assert.deepEqual((await service.search("query")).map((memory) => memory.fact), ["keep"]);
  assert.deepEqual(await service.stats(), { totalMemories: 2, byTopic: { one: 1, two: 1 }, scope: { shared_scope: "team" } });
  assert.deepEqual(pages, [undefined, "next"]);
});

test("forget rejects a resource name belonging to a foreign reasoning engine", async () => {
  const calls = [];
  const service = new MemoryBankService(config, mockClient({
    getMemory: async () => { calls.push("getMemory"); return [{ scope: { shared_scope: "team" } }]; },
    deleteMemory: async () => { calls.push("deleteMemory"); return [operation()]; },
  }));
  const foreignName = "projects/other-project/locations/us-central1/reasoningEngines/other-engine/memories/one";
  await assert.rejects(() => service.forget(foreignName), /must be a bare memory ID or a full resource name/);
  assert.deepEqual(calls, [], "no lookup or mutation should occur for a foreign-engine name");
});

test("forget rejects a same-engine memory belonging to a different scope", async () => {
  const calls = [];
  const service = new MemoryBankService(config, mockClient({
    getMemory: async () => { calls.push("getMemory"); return [{ scope: { shared_scope: "other-team" } }]; },
    deleteMemory: async () => { calls.push("deleteMemory"); return [operation()]; },
  }));
  await assert.rejects(() => service.forget("one"), /Refusing to mutate: memory does not belong to the configured scope/);
  assert.deepEqual(calls, ["getMemory"], "must verify scope before any mutation, and must not delete on mismatch");
});

test("forget rejects when the target memory has no scope at all", async () => {
  const service = new MemoryBankService(config, mockClient({
    getMemory: async () => [{ fact: "no scope field" }],
  }));
  await assert.rejects(() => service.forget("one"), /Refusing to mutate: memory does not belong to the configured scope/);
});

test("forget rejects when scope verification lookup fails, and does not mutate", async () => {
  const calls = [];
  const service = new MemoryBankService(config, mockClient({
    getMemory: async () => { throw new Error("not found"); },
    deleteMemory: async () => { calls.push("deleteMemory"); return [operation()]; },
  }));
  await assert.rejects(() => service.forget("one"), /Cannot verify memory scope before mutating/);
  assert.deepEqual(calls, []);
});

test("forget succeeds for a bare ID or full same-engine, same-scope resource name", async () => {
  const deletedNames = [];
  const service = new MemoryBankService(config, mockClient({
    getMemory: async () => [{ scope: { shared_scope: "team" } }],
    deleteMemory: async (input) => { deletedNames.push(input.name); return [operation()]; },
  }));
  await service.forget("one");
  await service.forget(`${parentName(config)}/memories/two`);
  assert.deepEqual(deletedNames, [`${parentName(config)}/memories/one`, `${parentName(config)}/memories/two`]);
});

test("forget and correct accept only the project-number alias returned by the configured-project lookup", async () => {
  const numberName = `projects/999888777/locations/${config.location}/reasoningEngines/${config.reasoningEngineId}/memories/one`;
  const configuredName = `${parentName(config)}/memories/one`;
  const calls = [];
  const service = new MemoryBankService(config, mockClient({
    getMemory: async (input) => {
      assert.equal(input.name, configuredName);
      return [{ name: numberName, fact: "Old fact", scope: config.scope }];
    },
    deleteMemory: async (input) => { calls.push(input.name); return [operation()]; },
    updateMemory: async (input) => { calls.push(input.memory.name); return [operation()]; },
  }));
  await service.forget(numberName);
  await service.correct(numberName, "New fact");
  assert.deepEqual(calls, [configuredName, configuredName]);
});

for (const tool of ["forget", "correct"]) {
  test(`${tool} rejects another project with identical engine, region, scope and memory ID`, async () => {
    const calls = [];
    const service = new MemoryBankService(config, mockClient({
      getMemory: async (input) => { calls.push(input.name); return [{ name: `projects/999888777/locations/${config.location}/reasoningEngines/engine/memories/one`, scope: config.scope }]; },
      deleteMemory: async () => { assert.fail("must not delete"); },
      updateMemory: async () => { assert.fail("must not update"); },
    }));
    for (const project of ["unrelated-project", "111222333"]) {
      await assert.rejects(() => service[tool](`projects/${project}/locations/${config.location}/reasoningEngines/engine/memories/one`, "new"), /memory project does not match/);
    }
    assert.deepEqual(calls, [`${parentName(config)}/memories/one`, `${parentName(config)}/memories/one`]);
  });

  test(`${tool} rejects an unverified alias when lookup has no canonical name`, async () => {
    const service = new MemoryBankService(config, mockClient({
      deleteMemory: async () => assert.fail("must not delete"),
      updateMemory: async () => assert.fail("must not update"),
    }));
    await assert.rejects(() => service[tool](`projects/999888777/locations/${config.location}/reasoningEngines/engine/memories/one`, "new"), /memory project does not match/);
  });
}

test("malformed memory IDs are rejected before lookup", async () => {
  const service = new MemoryBankService(config, mockClient({ getMemory: async () => assert.fail("must not fetch") }));
  for (const id of ["", ".", "..", "one?query", "%2e%2e", "one/../two", `${parentName(config)}/memories/one/extra`, `${parentName(config)}/memories/..`]) {
    await assert.rejects(() => service.forget(id), /Invalid memory_id/);
  }
});

test("scope equality requires every key and value but ignores key order", async () => {
  const cfg = { ...config, scope: { user_id: "alice", agent_name: "hermes" } };
  let actual = { agent_name: "hermes", user_id: "alice" };
  let deletes = 0;
  const service = new MemoryBankService(cfg, mockClient({
    getMemory: async () => [{ scope: actual }],
    deleteMemory: async () => { deletes++; return [operation()]; },
  }));
  await service.forget("one");
  for (const scope of [{ user_id: "alice" }, { user_id: "alice", agent_name: "hermes", extra: "x" }, { user_id: "bob", agent_name: "hermes" }]) {
    actual = scope;
    await assert.rejects(() => service.forget("one"), /configured scope/);
  }
  assert.equal(deletes, 1);
});

test("correction uses the verified fact for recovery without a second lookup", async () => {
  let reads = 0;
  let restored;
  const service = new MemoryBankService(config, mockClient({
    getMemory: async () => { assert.equal(++reads, 1); return [{ fact: "Original", scope: config.scope }]; },
    updateMemory: async () => { throw Object.assign(new Error("unsupported"), { code: 12 }); },
    generateMemories: async () => { throw new Error("generation failed"); },
    createMemory: async (input) => { restored = input.memory; return [operation()]; },
  }));
  assert.equal((await service.correct("one", "New")).recovered, true);
  assert.deepEqual(restored, { fact: "Original", scope: config.scope });
});

test("correction never deletes when the original fact is unavailable", async () => {
  const service = new MemoryBankService(config, mockClient({
    getMemory: async () => [{ scope: config.scope }],
    updateMemory: async () => { throw Object.assign(new Error("unsupported"), { code: 12 }); },
    deleteMemory: async () => assert.fail("must not delete"),
  }));
  await assert.rejects(() => service.correct("one", "New"), /original fact is unavailable/);
});

test("forget rejects a resource name from the same project but a different reasoningEngineId", async () => {
  const service = new MemoryBankService(config, mockClient({
    getMemory: async () => [{ scope: { shared_scope: "team" } }],
  }));
  const otherEngineName = `projects/${config.projectId}/locations/${config.location}/reasoningEngines/different-engine/memories/one`;
  await assert.rejects(() => service.forget(otherEngineName), /must be a bare memory ID or a full resource name/);
});

test("correct rejects a foreign-engine resource name without calling updateMemory", async () => {
  const calls = [];
  const service = new MemoryBankService(config, mockClient({
    updateMemory: async () => { calls.push("updateMemory"); return [operation()]; },
  }));
  const foreignName = "projects/other-project/locations/us-central1/reasoningEngines/other-engine/memories/one";
  await assert.rejects(() => service.correct(foreignName, "New fact"), /must be a bare memory ID or a full resource name/);
  assert.deepEqual(calls, []);
});

test("correct rejects a same-engine, different-scope memory without calling updateMemory or deleteMemory", async () => {
  const calls = [];
  const service = new MemoryBankService(config, mockClient({
    getMemory: async () => { calls.push("getMemory"); return [{ scope: { shared_scope: "other-team" } }]; },
    updateMemory: async () => { calls.push("updateMemory"); return [operation()]; },
    deleteMemory: async () => { calls.push("deleteMemory"); return [operation()]; },
  }));
  await assert.rejects(() => service.correct("one", "New fact"), /Refusing to mutate: memory does not belong to the configured scope/);
  assert.deepEqual(calls, ["getMemory"]);
});

test("correct succeeds via patch for a valid same-scope target", async () => {
  const calls = [];
  const service = new MemoryBankService(config, mockClient({
    getMemory: async () => { calls.push("getMemory"); return [{ scope: { shared_scope: "team" } }]; },
    updateMemory: async (input) => { calls.push(["updateMemory", input.memory.name]); return [operation()]; },
  }));
  assert.deepEqual(await service.correct("one", "New fact"), { corrected: true, method: "patch" });
  assert.deepEqual(calls, ["getMemory", ["updateMemory", `${parentName(config)}/memories/one`]]);
});

test("correct fallback still enforces the scope check before delete-regenerate", async () => {
  // Scope check must run before the patch attempt so a foreign/wrong-scope
  // memory can never reach the delete step, even via the fallback path.
  const calls = [];
  const service = new MemoryBankService(config, mockClient({
    getMemory: async () => { calls.push("getMemory"); return [{ scope: { shared_scope: "other-team" } }]; },
    updateMemory: async () => { calls.push("updateMemory"); throw Object.assign(new Error("unsupported"), { code: 12 }); },
    deleteMemory: async () => { calls.push("deleteMemory"); return [operation()]; },
  }));
  await assert.rejects(() => service.correct("one", "New fact"), /Refusing to mutate: memory does not belong to the configured scope/);
  assert.deepEqual(calls, ["getMemory"], "updateMemory/deleteMemory must never be reached");
});

test("validates JSON-RPC requests, arguments, and notification silence", async () => {
  const handler = createMcpRequestHandler(new MemoryBankService(config, mockClient()));
  const unknown = await request(handler, 1, "tools/call", { name: "nope", arguments: {} });
  assert.equal(unknown.error.code, -32602);
  const invalid = await request(handler, 2, "tools/call", { name: "memorybank_search", arguments: { query: "", extra: true } });
  assert.equal(invalid.result.isError, true);
  assert.match(invalid.result.content[0].text, /Unexpected argument/);
  const malformed = await handler({ jsonrpc: "1.0", id: 3, method: "tools/list" });
  assert.equal(malformed.error.code, -32600);
  assert.equal(await handler({ jsonrpc: "2.0", method: "tools/call", params: { name: "missing" } }), undefined);
  assert.equal(await handler({ jsonrpc: "2.0", method: "initialize", params: {} }), undefined);
  assert.equal(await handler({ jsonrpc: "2.0", method: "not/a/method", params: {} }), undefined);
});

test("reuses Agent Platform clients only for the same endpoint", () => {
  const created = [];
  setMemoryBankClientFactoryForTests((input) => {
    const client = mockClient();
    created.push([input.location, client]);
    return client;
  });
  try {
    const first = getMemoryBankClient(config);
    assert.equal(first, getMemoryBankClient({ ...config, projectId: "another-project" }));
    assert.notEqual(first, getMemoryBankClient({ ...config, location: "us-east1" }));
    assert.equal(created.length, 2);
  } finally {
    setMemoryBankClientFactoryForTests();
    resetMemoryBankClientsForTests();
  }
});

test("validates Hermes environment configuration", () => {
  assert.deepEqual(hermesConfigFromEnv(validEnv).scope, { agent_name: "hermes" });
  for (const [env, message] of [
    [{ ...validEnv, MEMORYBANK_PROJECT_ID: "${MEMORYBANK_PROJECT_ID}" }, /unresolved environment placeholder/],
    [{ ...validEnv, MEMORYBANK_LOCATION: "${env:LOCATION}" }, /unresolved environment placeholder/],
    [{ ...validEnv, MEMORYBANK_SCOPE: "" }, /scope must not be empty/],
    [{ ...validEnv, MEMORYBANK_SCOPE: "{}" }, /non-empty JSON object/],
    [{ ...validEnv, MEMORYBANK_SCOPE: "${SCOPE}" }, /unresolved environment placeholder/],
    [{ ...validEnv, MEMORYBANK_TOP_K: "0" }, /integer from 1 to 100/],
    [{ ...validEnv, MEMORYBANK_TOP_K: "1.5" }, /integer from 1 to 100/],
  ]) assert.throws(() => hermesConfigFromEnv(env), message);
});

async function runStdio(lines, { cwd = process.cwd(), env = validEnv } = {}) {
  const child = spawn(process.execPath, [resolve(process.cwd(), "bin/hermes-mcp.js")], {
    cwd, env: { ...validEnv, ...env }, stdio: ["pipe", "pipe", "pipe"],
  });
  let stdout = "";
  let stderr = "";
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (data) => { stdout += data; });
  child.stderr.on("data", (data) => { stderr += data; });
  child.stdin.end(lines);
  await new Promise((resolvePromise, reject) => {
    const timer = setTimeout(() => { child.kill(); reject(new Error("MCP process timed out")); }, SUBPROCESS_TIMEOUT_MS);
    child.once("exit", (code) => { clearTimeout(timer); code === 0 ? resolvePromise() : reject(Object.assign(new Error(`MCP process exited ${code}`), { stdout, stderr, code })); });
  });
  return { stdout, stderr };
}

test("stdio server ignores blank lines, handles malformed JSON, notifications, and independent cwd", async () => {
  const otherCwd = await mkdtemp(resolve(tmpdir(), "memorybank-mcp-"));
  try {
    const { stdout, stderr } = await runStdio([
      "\n",
      "not json\n",
      `${JSON.stringify({ jsonrpc: "2.0", method: "tools/list", params: {} })}\n`,
      `${JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-03-26" } })}\n`,
      `${JSON.stringify({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} })}\n`,
    ].join(""), { cwd: otherCwd });
    const responses = stdout.trim().split("\n").map(JSON.parse);
    assert.equal(responses.length, 3);
    assert.equal(responses[0].error.code, -32700);
    assert.equal(responses[1].result.protocolVersion, "2025-03-26");
    assert.equal(responses[2].result.tools.length, 5);
    // SDK metadata probing may emit a Node warning on stderr. Protocol data must
    // still be confined to stdout, and server diagnostics must not corrupt it.
    assert.doesNotMatch(stderr, /\"jsonrpc\"/);
  } finally {
    await rm(otherCwd, { recursive: true, force: true });
  }
});

function startStdio(t, { env = {}, evalCode } = {}) {
  const args = evalCode ? ["--input-type=module", "--eval", evalCode] : [resolve("bin/hermes-mcp.js")];
  // Deliberately exclude ambient ADC and user credentials from subprocess tests.
  const child = spawn(process.execPath, args, { env: { ...validEnv, ...env }, stdio: ["pipe", "pipe", "pipe"] });
  const pending = new Map();
  const lines = createInterface({ input: child.stdout });
  let stderr = "";
  child.stderr.on("data", data => { stderr += data; });
  const closed = new Promise((resolveExit, reject) => {
    child.once("error", reject);
    child.once("close", code => {
      for (const waiter of pending.values()) waiter.reject(new Error(`MCP exited ${code}: ${stderr}`));
      resolveExit(code);
    });
  });
  lines.on("line", line => {
    const response = JSON.parse(line);
    pending.get(response.id)?.resolve(response);
  });
  t.after(() => { child.kill(); lines.close(); });
  let nextId = 0;
  return {
    child, closed, stderr: () => stderr,
    async call(method, params = {}) {
      const id = ++nextId;
      let timer;
      try {
        return await new Promise((resolveResponse, reject) => {
          timer = setTimeout(() => { child.kill(); reject(new Error("MCP response timed out")); }, SUBPROCESS_TIMEOUT_MS);
          pending.set(id, { resolve: resolveResponse, reject });
          child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);
        });
      } finally { clearTimeout(timer); pending.delete(id); }
    },
  };
}

test("broken credentials return stats/search errors and leave the same process usable", async t => {
  const dir = await mkdtemp(resolve(tmpdir(), "memorybank-no-creds-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const proc = startStdio(t, { env: { GOOGLE_APPLICATION_CREDENTIALS: resolve(dir, "missing.json") } });
  await proc.call("initialize", { protocolVersion: "2025-11-25" });
  for (const name of ["memorybank_stats", "memorybank_search"]) {
    const response = await proc.call("tools/call", { name, arguments: name === "memorybank_search" ? { query: "test" } : {} });
    assert.equal(response.result.isError, true);
    assert.match(response.result.content[0].text, /ENOENT|does not exist/);
    assert.equal((await proc.call("tools/list")).result.tools.length, 5);
  }
  assert.doesNotMatch(proc.stderr(), /AutopaginateTrueWarning/);
});

test("unexpected unhandled rejections terminate instead of being swallowed", async t => {
  const entry = new URL("../dist/hermes-mcp.js", import.meta.url).href;
  const proc = startStdio(t, { evalCode: `
    import { runMcpServer } from ${JSON.stringify(entry)};
    runMcpServer();
    process.stdin.once("data", () => Promise.reject(new Error("unexpected programmer failure")));
  ` });
  proc.child.stdin.end("\n");
  const timer = setTimeout(() => proc.child.kill(), SUBPROCESS_TIMEOUT_MS);
  t.after(() => clearTimeout(timer));
  assert.equal(await proc.closed, 1);
  assert.match(proc.stderr(), /unexpected programmer failure/);
});

test("stdio entry reports missing configuration only on stderr", async () => {
  const child = spawn(process.execPath, [resolve(process.cwd(), "bin/hermes-mcp.js")], { cwd: process.cwd(), env: {}, stdio: ["ignore", "pipe", "pipe"] });
  let stdout = "";
  let stderr = "";
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (data) => { stdout += data; });
  child.stderr.on("data", (data) => { stderr += data; });
  const code = await new Promise((resolvePromise) => child.once("exit", resolvePromise));
  assert.equal(code, 1);
  assert.equal(stdout, "");
  assert.match(stderr, /Missing required environment variable MEMORYBANK_PROJECT_ID/);
});


test("OpenClaw tools use the shared ownership, correction recovery and pagination behavior", async () => {
  const tools = new Map();
  let mutations = 0;
  let scope = { shared_scope: "wrong" };
  const pages = [];
  setMemoryBankClientFactoryForTests(() => mockClient({
    getMemory: async () => [{ fact: "Original", scope }],
    deleteMemory: async () => { mutations++; return [operation()]; },
    updateMemory: async () => { throw Object.assign(new Error("unsupported"), { code: 12 }); },
    generateMemories: async () => { throw new Error("generation failed"); },
    createMemory: async (input) => { assert.equal(input.memory.fact, "Original"); mutations++; return [operation()]; },
    listMemories: async (input, options) => {
      assert.equal(options.autoPaginate, false);
      pages.push(input.pageToken);
      return input.pageToken ? [[{ fact: "two" }], undefined, {}] : [[{ fact: "one" }], undefined, { nextPageToken: "next" }];
    },
  }));
  try {
    plugin.register({
      pluginConfig: { ...config, autoRecall: false, autoCapture: false, autoSyncFiles: false, autoSyncTopics: false },
      registerTool: tool => tools.set(tool.name, tool), registerCli() {}, registerService() {}, on() {},
    });
    const forgotten = await tools.get("memorybank_forget").execute("test", { memory_id: "one" });
    assert.equal(forgotten.details.deleted, false);
    const rejected = await tools.get("memorybank_correct").execute("test", { memory_id: "one", new_fact: "New" });
    assert.equal(rejected.details.corrected, false);
    assert.equal(mutations, 0);
    scope = config.scope;
    const recovered = await tools.get("memorybank_correct").execute("test", { memory_id: "one", new_fact: "New" });
    assert.equal(recovered.details.recovered, true);
    assert.equal(recovered.isError, true);
    assert.equal(mutations, 2);
    const stats = await tools.get("memorybank_stats").execute("test", {});
    assert.equal(JSON.parse(stats.content[0].text).totalMemories, 2);
    assert.deepEqual(pages, [undefined, "next"]);
  } finally { setMemoryBankClientFactoryForTests(); }
});

test("initialization failure is handled before every shared operation dispatches an RPC", async () => {
  const client = mockClient({ initialize: async () => { throw new Error("credential initialization failed"); } });
  for (const method of ["retrieveMemories", "createMemory", "getMemory", "deleteMemory", "updateMemory", "listMemories"]) {
    client[method] = async () => assert.fail(`must not dispatch ${method}`);
  }
  const service = new MemoryBankService(config, client);
  for (const [method, args] of [["search", ["q"]], ["remember", ["fact"]], ["forget", ["one"]], ["correct", ["one", "fact"]], ["stats", []]]) {
    await assert.rejects(() => service[method](...args), /credential initialization failed/);
  }
});
