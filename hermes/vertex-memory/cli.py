"""CLI subcommands for the Vertex Memory Bank plugin.

Discovered by Hermes' `discover_plugin_cli_commands()` when this provider is the
active `memory.provider`. Exposes:

  hermes vertex-memory status
  hermes vertex-memory search <query> [--top-k N] [--show-ids]
  hermes vertex-memory list [--show-ids] [--count-only]
  hermes vertex-memory remember <fact>
  hermes vertex-memory forget <memory_id>
"""

from __future__ import annotations

import sys

try:
    from .client import VertexMemoryBankClient, MemoryBankError
    from .config import load_config
except ImportError:
    from client import VertexMemoryBankClient, MemoryBankError
    from config import load_config


def _client_and_scope():
    cfg = load_config()
    missing = [k for k in ("project_id", "location", "reasoning_engine_id")
               if not cfg.get(k)]
    if missing:
        print(f"vertex-memory not configured (missing: {', '.join(missing)}). "
              f"Run: hermes memory setup", file=sys.stderr)
        sys.exit(1)
    client = VertexMemoryBankClient(
        cfg["project_id"], cfg["location"], cfg["reasoning_engine_id"])
    scope_key = cfg.get("scope_key", "user_id")
    scope_val = cfg.get("scope_value", "hermes-user")
    return client, {scope_key: scope_val}, cfg


def _cmd_status(args):
    client, scope, cfg = _client_and_scope()
    print(f"Project:  {cfg['project_id']}")
    print(f"Location: {cfg['location']}")
    print(f"Engine:   {cfg['reasoning_engine_id']}")
    print(f"Scope:    {scope}")
    try:
        count = client.count(scope)
        print(f"Status:   connected ✓  ({count} memories in scope)")
    except MemoryBankError as e:
        print(f"Status:   ERROR — {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_search(args):
    client, scope, _ = _client_and_scope()
    results = client.retrieve(scope, args.query, top_k=args.top_k)
    if not results:
        print("No relevant memories found.")
        return
    for i, m in enumerate(results, 1):
        dist = m.get("distance")
        dist_s = f"  [dist={dist:.3f}]" if isinstance(dist, (int, float)) else ""
        ids = f"  (id: {m['id']})" if args.show_ids else ""
        print(f"{i}. {m['fact']}{dist_s}{ids}")


def _cmd_list(args):
    client, scope, _ = _client_and_scope()
    if args.count_only:
        print(client.count(scope))
        return
    items = client.list_memories(scope)
    if not items:
        print("No memories in scope.")
        return
    for i, m in enumerate(items, 1):
        fact = m.get("fact", "")
        ids = f"  (id: {m.get('name', '').rsplit('/', 1)[-1]})" if args.show_ids else ""
        print(f"{i}. {fact}{ids}")


def _cmd_remember(args):
    client, scope, _ = _client_and_scope()
    outcome = client.generate_from_fact(scope, args.fact, source="cli-remember", wait=True)
    print(f"Stored. created={outcome.get('created', 0)} "
          f"updated={outcome.get('updated', 0)}")


def _cmd_forget(args):
    client, scope, _ = _client_and_scope()
    try:
        client.delete(args.memory_id, scope=scope)
    except MemoryBankError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Deleted memory {args.memory_id}.")


def _cmd_correct(args):
    client, scope, _ = _client_and_scope()
    try:
        result = client.correct(scope, args.memory_id, args.fact)
    except MemoryBankError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if not result.get("corrected"):
        print(f"Correction failed: {result}", file=sys.stderr)
        sys.exit(1)
    print(f"Corrected memory {args.memory_id} ({result.get('method')}).")


def _cmd_stats(args):
    client, scope, _ = _client_and_scope()
    print(f"{client.count(scope)} memories in scope {scope}")


def _dispatch(args):
    sub = getattr(args, "vertex_memory_command", None)
    handlers = {
        "status": _cmd_status, "search": _cmd_search, "list": _cmd_list,
        "remember": _cmd_remember, "forget": _cmd_forget,
        "correct": _cmd_correct, "stats": _cmd_stats,
    }
    fn = handlers.get(sub)
    if not fn:
        print("Usage: hermes vertex-memory <status|search|list|remember|forget|correct|stats>",
              file=sys.stderr)
        sys.exit(2)
    fn(args)


def register_cli(subparser) -> None:
    """Build the `hermes vertex-memory` argparse tree."""
    subs = subparser.add_subparsers(dest="vertex_memory_command")

    subs.add_parser("status", help="Show config and connection status")

    p_search = subs.add_parser("search", help="Semantic search of memories")
    p_search.add_argument("query")
    p_search.add_argument("--top-k", type=int, default=10)
    p_search.add_argument("--show-ids", action="store_true")

    p_list = subs.add_parser("list", help="List all memories in scope")
    p_list.add_argument("--show-ids", action="store_true")
    p_list.add_argument("--count-only", action="store_true")

    p_remember = subs.add_parser("remember", help="Store a fact via consolidation")
    p_remember.add_argument("fact")

    p_forget = subs.add_parser("forget", help="Delete a memory by id")
    p_forget.add_argument("memory_id")

    p_correct = subs.add_parser("correct", help="Update a memory's fact in place")
    p_correct.add_argument("memory_id")
    p_correct.add_argument("fact")

    subs.add_parser("stats", help="Total memory count in scope")

    subparser.set_defaults(func=_dispatch)
