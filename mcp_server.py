#!/usr/bin/env python3
"""MCP-сервер долговременной памяти агента (indra-memory).

Тонкая обёртка над `memory.py`: та же БД (SQLite + FTS5), те же функции.
Ничего в `memory.py` не меняется — добавлен только способ общения по MCP.

Запуск:  python3 mcp_server.py            # stdio (по умолчанию)
         python3 mcp_server.py --http 8190 # streamable-http
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import memory as mem  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402

mcp = MCPServer("indra-memory", version="1.0.0")

DB = os.environ.get("INDRA_MEMORY_DB", str(mem.DEFAULT_DB))


def _conn():
    return mem.connect(DB)


def _rows(rows) -> list[dict]:
    return [dict(r) for r in rows]


@mcp.tool(name="remember", description="Запомнить факт. Версии и замещение: старый факт помечается superseded, история сохраняется.")
async def remember(text: str, tags: str = "", importance: int = 3, kind: str = "fact",
                   source: str = "", session: str = "") -> str:
    conn = _conn()
    fid = mem.remember(conn, text=text, tags=tags, importance=importance, kind=kind,
                       source=source, session=session)
    conn.close()
    return json.dumps({"id": fid, "stored": True}, ensure_ascii=False)


@mcp.tool(name="recall", description="Найти факты по запросу: полнотекстовый поиск + ранжирование по важности и свежести.")
async def recall(query: str, k: int = 10, min_importance: int = 1, tags: str = "") -> str:
    conn = _conn()
    rows = mem.recall(conn, query, k=k, min_importance=min_importance, tags=tags)
    conn.close()
    return json.dumps({"query": query, "count": len(rows), "facts": _rows(rows)}, ensure_ascii=False)


@mcp.tool(name="pack", description="Собрать контекст под жёсткий бюджет токенов — защита от деградации длинного контекста.")
async def pack(query: str, token_budget: int = 800) -> str:
    conn = _conn()
    text = mem.pack(conn, query, token_budget=token_budget)
    conn.close()
    return text


@mcp.tool(name="link", description="Связать два факта отношением (например 'опровергает', 'подтверждает').")
async def link(src: int, relation: str, dst: int) -> str:
    conn = _conn()
    mem.link(conn, src, relation, dst)
    conn.close()
    return json.dumps({"linked": [src, relation, dst]}, ensure_ascii=False)


@mcp.tool(name="supersede", description="Заменить факт новой версией: старый помечается superseded, но остаётся в истории.")
async def supersede(old_id: int, new_text: str, tags: str = "", importance: int = 3) -> str:
    conn = _conn()
    new_id = mem.supersede(conn, old_id, new_text, tags=tags, importance=importance)
    conn.close()
    return json.dumps({"old_id": old_id, "new_id": new_id}, ensure_ascii=False)


@mcp.tool(name="neighbours", description="Соседние факты вокруг факта — связи графа знаний.")
async def neighbours(fact_id: int) -> str:
    conn = _conn()
    rows = mem.neighbours(conn, fact_id)
    conn.close()
    return json.dumps({"fact_id": fact_id, "neighbours": _rows(rows)}, ensure_ascii=False)


@mcp.tool(name="forget", description="Забыть факт (помечает неактивным; история остаётся в базе).")
async def forget(fact_id: int) -> str:
    conn = _conn()
    mem.forget(conn, fact_id)
    conn.close()
    return json.dumps({"forgotten": fact_id}, ensure_ascii=False)


@mcp.tool(name="stats", description="Статистика памяти: сколько фактов, версий, связей.")
async def stats() -> str:
    conn = _conn()
    st = mem.stats(conn)
    conn.close()
    return json.dumps(st, ensure_ascii=False, default=str)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", type=int, default=0, help="порт streamable-http (0 = stdio)")
    args = ap.parse_args()
    if args.http:
        await mcp.run(transport="streamable-http", port=args.http)
    else:
        await mcp.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(main())
