#!/usr/bin/env python3
"""MCP-сервер хаба коммуникации агентов.

Тонкая обёртка над `hub.py`: конверты, очередь, роутер, контекст под бюджет.
Тот же приём, что у indra-memory: ядро не меняем, добавляем только способ
общения по MCP — чтобы к хабу могли подключаться и другие агенты.

Запуск:  python3 hub_mcp.py            # stdio (по умолчанию)
         python3 hub_mcp.py --http 8193 # streamable-http
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hub  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402

mcp = MCPServer("agent-hub", version="0.1.0")


def _conn():
    return hub.connect()


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


@mcp.tool(name="hub_send", description="Отправить сообщение агенту через хаб: цель, задача, контекст, бюджет токенов, мандаты и цена. Конверт подписывается.")
async def hub_send(to: str, goal: str, task: str, sender: str = "session:mcp",
                   capability: list[str] | None = None, context_query: str = "",
                   token_budget: int = 2000, max_cost_usd: float | None = None,
                   max_calls: int | None = None, price_usdc: float | None = None,
                   cold_refs: list[str] | None = None, ttl_s: float = 3600) -> str:
    conn = _conn()
    env = hub.create(conn, sender=sender, recipient=to, goal=goal, task=task,
                     capability=capability, context_query=context_query,
                     token_budget=token_budget, max_cost_usd=max_cost_usd,
                     max_calls=max_calls, price_usdc=price_usdc, cold_refs=cold_refs,
                     ttl_s=ttl_s)
    conn.close()
    return _json({"id": env["id"], "status": env["status"],
                  "signature": env["signature"].get("algorithm")})


@mcp.tool(name="hub_inbox", description="Очередь агента: конверты, ждущие его, с подписью, проверкой и контекстом, собранным под бюджет токенов.")
async def hub_inbox(agent: str, limit: int = 10) -> str:
    conn = _conn()
    items = []
    for e in hub.queue_for(conn, agent, limit):
        ok, why = hub.verify_env(e)
        items.append({"id": e["id"], "goal": e["goal"], "task": e["task"],
                      "sender": e["sender"], "capability": e["capability"],
                      "signature_ok": ok, "signature": why,
                      "context": hub.pack_context(conn, e)})
    conn.close()
    return _json({"agent": agent, "count": len(items), "items": items})


@mcp.tool(name="hub_claim", description="Забрать конверт из очереди (атомарно) и получить контекст под бюджет.")
async def hub_claim(id: str, agent: str) -> str:
    conn = _conn()
    env, why = hub.claim(conn, id, agent)
    if env is None:
        conn.close()
        return _json({"ok": False, "reason": why})
    ctx = hub.pack_context(conn, env)
    conn.close()
    return _json({"ok": True, "id": env["id"], "context": ctx})


@mcp.tool(name="hub_complete", description="Закрыть конверт результатом. Результат автоматически попадает в тёплый ярус памяти.")
async def hub_complete(id: str, result: str, agent: str = "") -> str:
    conn = _conn()
    ok, why = hub.complete(conn, id, result, agent or None)
    conn.close()
    return _json({"ok": ok, "reason": why})


@mcp.tool(name="hub_route", description="Подобрать агента-исполнителя по capability, мандатам и цене. Если кандидата нет — вернётся честная причина отказа.")
async def hub_route(id: str) -> str:
    conn = _conn()
    env = hub.get(conn, id)
    if not env:
        conn.close()
        return _json({"ok": False, "reason": "нет такого конверта"})
    decision = hub.route(env, hub.load_registry())
    hub.set_routing(conn, id, decision)
    conn.close()
    return _json(decision)


@mcp.tool(name="hub_put_cold", description="Положить артефакт в холодный ярус памяти. Вернёт sha256, по которому его потом можно вложить в конверт.")
async def hub_put_cold(text: str) -> str:
    return _json({"sha256": hub.put_cold(text)})


@mcp.tool(name="hub_stats", description="Состояние хаба: конверты по статусам, холодные артефакты, реестр агентов, память.")
async def hub_stats() -> str:
    conn = _conn()
    out = hub.stats(conn)
    conn.close()
    return _json(out)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", type=int, default=0, help="порт streamable-http (0 = stdio)")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--http-path", default="/mcp")
    args = ap.parse_args()
    if args.http:
        await mcp.run(transport="streamable-http", port=args.http, host=args.host,
                      streamable_http_path=args.http_path)
    else:
        await mcp.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(main())
