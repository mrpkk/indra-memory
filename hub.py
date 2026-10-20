#!/usr/bin/env python3
"""Хаб коммуникации агентов — ступень 1.

Что это: единая шина между сессиями, моделями и агентами. Не новый продукт,
а оркестратор поверх уже существующих слоёв (indra-memory, attest, drift,
agentpay, skilllint, mitracore).

Четыре вещи, которых не хватало агентам:
  1. Конверт — один формат сообщения: цель, задача, контекст, бюджет,
     мандаты, цена, подпись, TTL.
  2. Память по ярусам: горячий (очередь), тёплый (факты в SQLite+FTS),
     холодный (файлы по sha256). Контекст собирается ЖЁСТКО под бюджет
     токенов — длинная память растёт, а контекст не раздувается.
  3. Роутер: выбор агента по capability, мандатам и цене. Нет кандидата —
     честный отказ с причиной, а не молчаливая потеря задачи.
  4. Подпись Ed25519 (attest) — конверт проверяется без сети и без секрета.

Запуск:  python3 hub.py send --to research --goal "..." --task "..."
         python3 hub.py inbox --agent research
         python3 hub.py claim --id <id> --agent research
         python3 hub.py complete --id <id> --result "..."
         python3 hub.py route --id <id>
         python3 hub.py stats
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import memory as mem

DB = Path(os.environ.get("AGENT_HUB_DB", Path.home() / ".indra" / "hub.db"))
REGISTRY = Path(os.environ.get("AGENT_HUB_REGISTRY", Path.home() / ".indra" / "agents.json"))
COLD = Path(os.environ.get("AGENT_HUB_COLD", Path.home() / ".indra" / "cold"))
MEM_DB = Path(os.environ.get("INDRA_MEMORY_DB", str(mem.DEFAULT_DB)))
CHARS_PER_TOKEN = 220.0  # та же оценка, что в indra-memory.pack
V = 1

# поля, которые НЕ подписываются: они меняются по ходу жизни конверта
MUTABLE = ("status", "claimed_by", "claimed_at", "result", "routing", "dropped", "signature")


# ------------------------------------------------------------------ утилиты

def canon(env: dict[str, Any]) -> bytes:
    """Каноническая сериализация подписываемой части конверта."""
    payload = {k: v for k, v in env.items() if k not in MUTABLE}
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      default=str).encode("utf-8")


def sign_env(env: dict[str, Any]) -> dict[str, Any]:
    """Подписать конверт. Ed25519 (attest) — при наличии ключа, иначе HMAC."""
    payload = canon(env)
    try:
        from attest.signing import sign as ed_sign
        s = ed_sign(payload)
        env["signature"] = {"algorithm": s.algorithm, "signature": s.signature,
                            "public_key": s.public_key}
        return env
    except Exception:
        secret = os.environ.get("HUB_SECRET") or os.environ.get("ATTEST_SECRET")
        if not secret:
            env["signature"] = {"algorithm": "none"}
            return env
        import hmac
        mac = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        env["signature"] = {"algorithm": "hmac-sha256", "signature": mac}
        return env


def verify_env(env: dict[str, Any]) -> tuple[bool, str]:
    """Проверить подпись конверта. Вернёт (ok, причина)."""
    sig = env.get("signature") or {}
    algo = sig.get("algorithm")
    if not algo or algo == "none":
        return False, "конверт не подписан"
    payload = canon(env)
    if algo == "hmac-sha256":
        secret = os.environ.get("HUB_SECRET") or os.environ.get("ATTEST_SECRET")
        if not secret:
            return False, "нет секрета для проверки HMAC"
        import hmac
        expect = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        ok = hmac.compare_digest(expect, sig.get("signature", ""))
        return ok, "ok" if ok else "подпись не сошлась"
    if algo == "ed25519":
        try:
            from attest.signing import verify as ed_verify
            ok = ed_verify(payload, sig.get("signature", ""), sig.get("public_key", ""))
            return ok, "ok" if ok else "подпись не сошлась"
        except Exception as exc:
            return False, f"проверка не выполнена: {exc}"
    return False, f"неизвестный алгоритм: {algo}"


def tokens_of(text: str) -> float:
    return len(text) / CHARS_PER_TOKEN


# ------------------------------------------------------------------ холодный ярус

def put_cold(text: str) -> str:
    """Положить артефакт в холодный ярус. Возвращает sha256."""
    COLD.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    (COLD / f"{h}.txt").write_text(text, encoding="utf-8")
    return h


def get_cold(h: str) -> str | None:
    p = COLD / f"{h}.txt"
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


# ------------------------------------------------------------------ база

SCHEMA = """
CREATE TABLE IF NOT EXISTS envelopes (
    id            TEXT PRIMARY KEY,
    created_at    REAL NOT NULL,
    ttl_s         REAL NOT NULL,
    sender        TEXT NOT NULL,
    recipient     TEXT NOT NULL,
    goal          TEXT NOT NULL,
    task          TEXT NOT NULL,
    capability    TEXT NOT NULL DEFAULT '[]',
    context_query TEXT NOT NULL DEFAULT '',
    token_budget  INTEGER NOT NULL DEFAULT 2000,
    max_cost_usd  REAL,
    max_calls     INTEGER,
    price_usdc    REAL,
    cold_refs     TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'queued',
    claimed_by    TEXT,
    claimed_at    REAL,
    result        TEXT,
    signature     TEXT NOT NULL DEFAULT '{}',
    routing       TEXT NOT NULL DEFAULT '{}',
    dropped       TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_env_status ON envelopes(status, created_at);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    envelope_id TEXT,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}'
);
"""


def connect(path: str | Path = DB) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _row_to_env(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "v": V, "created_at": row["created_at"], "ttl_s": row["ttl_s"],
        "sender": row["sender"], "recipient": row["recipient"], "goal": row["goal"],
        "task": row["task"], "capability": json.loads(row["capability"]),
        "context": {"query": row["context_query"], "budget": row["token_budget"],
                    "cold": json.loads(row["cold_refs"])},
        "max_cost_usd": row["max_cost_usd"], "max_calls": row["max_calls"],
        "price_usdc": row["price_usdc"], "status": row["status"],
        "claimed_by": row["claimed_by"], "claimed_at": row["claimed_at"],
        "result": row["result"], "signature": json.loads(row["signature"]),
        "routing": json.loads(row["routing"]), "dropped": json.loads(row["dropped"]),
    }


def _event(conn: sqlite3.Connection, env_id: str | None, kind: str, payload: dict) -> None:
    conn.execute("INSERT INTO events(ts, envelope_id, kind, payload) VALUES(?,?,?,?)",
                 (time.time(), env_id, kind, json.dumps(payload, ensure_ascii=False, default=str)))


# ------------------------------------------------------------------ создание и доставка

def create(conn: sqlite3.Connection, *, sender: str, recipient: str, goal: str, task: str,
           capability: list[str] | None = None, context_query: str = "",
           token_budget: int = 2000, max_cost_usd: float | None = None,
           max_calls: int | None = None, price_usdc: float | None = None,
           cold_refs: list[str] | None = None, ttl_s: float = 3600,
           sign: bool = True) -> dict[str, Any]:
    """Создать конверт, подписать и положить в очередь."""
    if not goal.strip() or not task.strip():
        raise ValueError("пустая цель или задача — конверт не создаётся")
    env = {
        "id": uuid.uuid4().hex[:16], "v": V, "created_at": time.time(), "ttl_s": ttl_s,
        "sender": sender, "recipient": recipient, "goal": goal, "task": task,
        "capability": list(capability or []),
        "context": {"query": context_query, "budget": int(token_budget),
                    "cold": list(cold_refs or [])},
        "max_cost_usd": max_cost_usd, "max_calls": max_calls, "price_usdc": price_usdc,
        "status": "queued", "claimed_by": None, "claimed_at": None, "result": None,
        "signature": {}, "routing": {}, "dropped": [],
    }
    if sign:
        sign_env(env)
    conn.execute(
        "INSERT INTO envelopes(id, created_at, ttl_s, sender, recipient, goal, task, capability,"
        " context_query, token_budget, max_cost_usd, max_calls, price_usdc, cold_refs,"
        " status, signature) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (env["id"], env["created_at"], env["ttl_s"], env["sender"], env["recipient"], env["goal"],
         env["task"], json.dumps(env["capability"], ensure_ascii=False), context_query,
         int(token_budget), max_cost_usd, max_calls, price_usdc,
         json.dumps(env["context"]["cold"], ensure_ascii=False), env["status"],
         json.dumps(env["signature"], ensure_ascii=False)))
    _event(conn, env["id"], "created", {"to": recipient, "goal": goal})
    conn.commit()
    return env


def expire_due(conn: sqlite3.Connection) -> int:
    """Пометить просроченные. Возвращает количество."""
    now = time.time()
    rows = conn.execute("SELECT id FROM envelopes WHERE status='queued' AND created_at + ttl_s < ?",
                        (now,)).fetchall()
    for r in rows:
        conn.execute("UPDATE envelopes SET status='expired' WHERE id=?", (r["id"],))
        _event(conn, r["id"], "expired", {})
    conn.commit()
    return len(rows)


def queue_for(conn: sqlite3.Connection, agent: str, limit: int = 10) -> list[dict[str, Any]]:
    """Очередь агента: адресные ему + broadcast («*»), без просроченных."""
    expire_due(conn)
    rows = conn.execute(
        "SELECT * FROM envelopes WHERE status='queued' AND (recipient=? OR recipient='*')"
        " ORDER BY created_at LIMIT ?", (agent, limit)).fetchall()
    return [_row_to_env(r) for r in rows]


def claim(conn: sqlite3.Connection, env_id: str, agent: str) -> tuple[dict | None, str]:
    """Забрать конверт. Атомарно: только из 'queued'."""
    expire_due(conn)
    cur = conn.execute(
        "UPDATE envelopes SET status='claimed', claimed_by=?, claimed_at=?"
        " WHERE id=? AND status='queued'", (agent, time.time(), env_id))
    conn.commit()
    if cur.rowcount == 0:
        row = conn.execute("SELECT status FROM envelopes WHERE id=?", (env_id,)).fetchone()
        return None, f"конверт недоступен (status={row['status'] if row else 'нет такого'})"
    env = get(conn, env_id)
    _event(conn, env_id, "claimed", {"agent": agent})
    conn.commit()
    return env, "ok"


def complete(conn: sqlite3.Connection, env_id: str, result: str, agent: str | None = None,
             remember: bool = True) -> tuple[bool, str]:
    """Закрыть конверт результатом. Результат уходит в тёплый ярус памяти."""
    row = conn.execute("SELECT status FROM envelopes WHERE id=?", (env_id,)).fetchone()
    if not row:
        return False, "нет такого конверта"
    conn.execute("UPDATE envelopes SET status='done', result=? WHERE id=?", (result, env_id))
    _event(conn, env_id, "completed", {"agent": agent})
    conn.commit()
    if remember:
        try:
            mconn = mem.connect(MEM_DB)
            mem.remember(mconn, text=f"[{env_id}] {result}", tags="hub,result",
                         importance=3, kind="result", source=f"hub:{env_id}", session=agent or "")
            mconn.close()
        except Exception:
            pass  # память не обязана ломать доставку
    return True, "ok"


def get(conn: sqlite3.Connection, env_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM envelopes WHERE id=?", (env_id,)).fetchone()
    return _row_to_env(row) if row else None


# ------------------------------------------------------------------ контекст (ярусы памяти)

def pack_context(conn: sqlite3.Connection, env: dict[str, Any]) -> dict[str, Any]:
    """Собрать контекст под жёсткий бюджет. Возвращает текст + честный отчёт."""
    ctx = env.get("context") or {}
    budget = int(ctx.get("budget") or 2000)
    approx = max(1.0, float(budget))   # бюджет в токенах, как и в indra-memory
    used = 0.0
    parts: list[str] = []
    sources: list[str] = []
    dropped: list[str] = []

    hot = f"ЦЕЛЬ: {env.get('goal', '')}\nЗАДАЧА: {env.get('task', '')}"
    cost = tokens_of(hot)
    if cost <= approx:
        parts.append(hot)
        used += cost
        sources.append("hot")
    else:
        dropped.append("hot: цель и задача не влезли в бюджет")

    if used < approx and ctx.get("query"):
        try:
            mconn = mem.connect(MEM_DB)
            warm = mem.pack(mconn, ctx["query"], token_budget=int(max(0, approx - used)))
            mconn.close()
        except Exception as exc:
            warm = ""
            dropped.append(f"warm: чтение памяти не удалось ({exc})")
        if warm:
            c = tokens_of(warm)
            parts.append(warm)
            used += c
            sources.append("warm")
        else:
            dropped.append("warm: по запросу не найдено")

    for h in ctx.get("cold") or []:
        txt = get_cold(h)
        if txt is None:
            dropped.append(f"cold:{h}: артефакт не найден")
            continue
        c = tokens_of(txt) + 1
        if used + c > approx:
            dropped.append(f"cold:{h}: не влез в бюджет")
            continue
        parts.append(f"[cold:{h}] {txt}")
        used += c
        sources.append("cold")

    return {"text": "\n\n".join(parts), "tokens": round(used), "budget": budget,
            "sources": sources, "dropped": dropped}


# ------------------------------------------------------------------ роутер

def load_registry(path: str | Path = REGISTRY) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def route(env: dict[str, Any], registry: dict[str, Any]) -> dict[str, Any]:
    """Выбрать агента: capability ∩ мандаты ∩ цена. Нет кандидата — честно."""
    caps = set(env.get("capability") or [])
    max_cost = env.get("max_cost_usd")
    price = env.get("price_usdc")
    cands: list[tuple[float, str, dict]] = []
    rejected: list[str] = []
    for name, spec in (registry or {}).items():
        agent_caps = set(spec.get("capabilities") or [])
        if caps and not (caps & agent_caps):
            rejected.append(f"{name}: нет capability {sorted(caps & agent_caps)}")
            continue
        cost = float(spec.get("cost_per_call", 0) or 0)
        if max_cost is not None and cost > float(max_cost):
            rejected.append(f"{name}: цена {cost} выше мандата {max_cost}")
            continue
        if price is not None and cost > float(price):
            rejected.append(f"{name}: цена {cost} выше price_usdc {price}")
            continue
        cands.append((cost, name, spec))
    if not cands:
        return {"agent": None, "reason": "нет агента, проходящего capability+мандаты+цену",
                "rejected": rejected}
    cands.sort(key=lambda x: (x[0], x[1]))
    cost, name, spec = cands[0]
    return {"agent": name, "reason": f"подходит по capability и цене (cost_per_call={cost})",
            "cost_per_call": cost, "rejected": rejected,
            "alternatives": [c[1] for c in cands[1:]]}


def set_routing(conn: sqlite3.Connection, env_id: str, decision: dict[str, Any]) -> None:
    conn.execute("UPDATE envelopes SET routing=?, recipient=? WHERE id=?",
                 (json.dumps(decision, ensure_ascii=False), decision.get("agent") or "", env_id))
    _event(conn, env_id, "routed", decision)
    conn.commit()


# ------------------------------------------------------------------ статистика

def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT status, COUNT(*) c FROM envelopes GROUP BY status").fetchall()
    by_status = {r["status"]: r["c"] for r in rows}
    total = sum(by_status.values())
    cold_n = len(list(COLD.glob("*.txt"))) if COLD.exists() else 0
    try:
        mconn = mem.connect(MEM_DB)
        mem_stats = mem.stats(mconn)
        mconn.close()
    except Exception:
        mem_stats = {}
    return {"конвертов": total, "по статусам": by_status,
            "холодных артефактов": cold_n, "реестр агентов": load_registry(), "память": mem_stats}


# ------------------------------------------------------------------ CLI

def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main() -> int:
    ap = argparse.ArgumentParser(description="Хаб коммуникации агентов")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send")
    s.add_argument("--to", required=True)
    s.add_argument("--from", dest="sender", default="session:cli")
    s.add_argument("--goal", required=True)
    s.add_argument("--task", required=True)
    s.add_argument("--capability", default="")
    s.add_argument("--query", default="")
    s.add_argument("--budget", type=int, default=2000)
    s.add_argument("--max-cost", type=float, default=None)
    s.add_argument("--max-calls", type=int, default=None)
    s.add_argument("--price-usdc", type=float, default=None)
    s.add_argument("--ttl", type=float, default=3600)

    q = sub.add_parser("inbox")
    q.add_argument("--agent", required=True)
    q.add_argument("--limit", type=int, default=10)

    cl = sub.add_parser("claim")
    cl.add_argument("--id", required=True)
    cl.add_argument("--agent", required=True)

    cp = sub.add_parser("complete")
    cp.add_argument("--id", required=True)
    cp.add_argument("--result", required=True)
    cp.add_argument("--agent", default="")

    rt = sub.add_parser("route")
    rt.add_argument("--id", required=True)

    sub.add_parser("stats")

    args = ap.parse_args()
    conn = connect()

    if args.cmd == "send":
        env = create(conn, sender=args.sender, recipient=args.to, goal=args.goal, task=args.task,
                     capability=[c for c in args.capability.split(",") if c],
                     context_query=args.query, token_budget=args.budget,
                     max_cost_usd=args.max_cost, max_calls=args.max_calls,
                     price_usdc=args.price_usdc, ttl_s=args.ttl)
        _print({"id": env["id"], "signature": env["signature"].get("algorithm"), "status": env["status"]})
    elif args.cmd == "inbox":
        items = queue_for(conn, args.agent, args.limit)
        out = []
        for e in items:
            ctx = pack_context(conn, e)
            ok, why = verify_env(e)
            out.append({"id": e["id"], "goal": e["goal"], "task": e["task"], "sender": e["sender"],
                        "capability": e["capability"], "signature_ok": ok, "signature": why,
                        "context": ctx})
        _print({"agent": args.agent, "count": len(out), "items": out})
    elif args.cmd == "claim":
        env, why = claim(conn, args.id, args.agent)
        if env is None:
            _print({"ok": False, "reason": why})
            return 1
        _print({"ok": True, "id": env["id"], "context": pack_context(conn, env)})
    elif args.cmd == "complete":
        ok, why = complete(conn, args.id, args.result, args.agent or None)
        _print({"ok": ok, "reason": why})
    elif args.cmd == "route":
        env = get(conn, args.id)
        if not env:
            _print({"ok": False, "reason": "нет такого конверта"})
            return 1
        decision = route(env, load_registry())
        set_routing(conn, args.id, decision)
        _print(decision)
    elif args.cmd == "stats":
        _print(stats(conn))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
