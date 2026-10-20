"""Тесты хаба коммуникации агентов.

Изоляция: все пути (БД хаба, холодный ярус, память) — в临时-каталог,
заданный ДО импорта hub, потому что пути читаются при импорте.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="hub-test-"))
os.environ["AGENT_HUB_DB"] = str(_TMP / "hub.db")
os.environ["AGENT_HUB_COLD"] = str(_TMP / "cold")
os.environ["INDRA_MEMORY_DB"] = str(_TMP / "memory.db")
os.environ["HUB_SECRET"] = "test-secret"

import hub  # noqa: E402
import memory as mem  # noqa: E402


def test_signature_roundtrip_and_tamper():
    conn = hub.connect()
    env = hub.create(conn, sender="s1", recipient="a1", goal="проверить", task="подписать",
                     context_query="проверка")
    ok, why = hub.verify_env(env)
    assert ok, why
    assert env["signature"]["algorithm"] == "ed25519"

    tampered = dict(env)
    tampered["task"] = "подменить"
    ok2, _ = hub.verify_env(tampered)
    assert not ok2, "подпись должна ломаться при подмене задачи"
    conn.close()


def test_context_respects_token_budget():
    conn = hub.connect()
    mconn = mem.connect(hub.MEM_DB)
    for i in range(30):
        mem.remember(mconn, text=f"факт номер {i}: очень длинный текст про проверку "
                                 f"бюджета токенов " * 6, importance=3)
    mconn.close()
    env = hub.create(conn, sender="s1", recipient="a1", goal="собрать контекст",
                     task="уложиться в бюджет", context_query="бюджет токенов",
                     token_budget=200)
    ctx = hub.pack_context(conn, env)
    assert ctx["tokens"] <= ctx["budget"], ctx
    assert "warm" in ctx["sources"], ctx
    assert ctx["dropped"] or ctx["tokens"] > 0
    conn.close()


def test_router_respects_capability_and_mandates():
    registry = {
        "researcher": {"capabilities": ["research"], "cost_per_call": 0.01},
        "coder": {"capabilities": ["code"], "cost_per_call": 0.02},
    }
    env = {"capability": ["code"], "max_cost_usd": 0.5, "price_usdc": None}
    d = hub.route(env, registry)
    assert d["agent"] == "coder", d

    env2 = {"capability": ["code"], "max_cost_usd": 0.001, "price_usdc": None}
    d2 = hub.route(env2, registry)
    assert d2["agent"] is None, d2
    assert d2["reason"], "отказ должен объясняться"

    env3 = {"capability": ["исследование"], "max_cost_usd": None, "price_usdc": None}
    d3 = hub.route(env3, registry)
    assert d3["agent"] is None and d3["rejected"], d3


def test_ttl_expiry_and_single_claim():
    conn = hub.connect()
    env = hub.create(conn, sender="s1", recipient="a2", goal="г", task="т", ttl_s=0)
    assert hub.queue_for(conn, "a2") == [], "просроченный конверт не должен попадать в очередь"
    assert hub.get(conn, env["id"])["status"] == "expired"

    env2 = hub.create(conn, sender="s1", recipient="a2", goal="г", task="т")
    claimed, why = hub.claim(conn, env2["id"], "a2")
    assert claimed is not None, why
    again, why2 = hub.claim(conn, env2["id"], "a3")
    assert again is None and "недоступен" in why2, why2
    conn.close()


def test_result_lands_in_warm_memory():
    conn = hub.connect()
    env = hub.create(conn, sender="s1", recipient="a3", goal="г", task="т")
    ok, why = hub.complete(conn, env["id"], "ответ агента про хаб", agent="a3")
    assert ok, why
    mconn = mem.connect(hub.MEM_DB)
    rows = mem.recall(mconn, "ответ агента про хаб", k=5)
    mconn.close()
    assert any(env["id"] in r["text"] for r in rows), rows


def test_broadcast_reaches_any_agent():
    conn = hub.connect()
    hub.create(conn, sender="s1", recipient="*", goal="всем", task="общее")
    assert len(hub.queue_for(conn, "любой-агент")) == 1
    conn.close()


def test_put_and_read_cold():
    h = hub.put_cold("холодный артефакт про контракт")
    assert hub.get_cold(h) == "холодный артефакт про контракт"
    conn = hub.connect()
    env = hub.create(conn, sender="s1", recipient="a4", goal="г", task="т", cold_refs=[h],
                     token_budget=500)
    ctx = hub.pack_context(conn, env)
    assert "cold" in ctx["sources"], ctx
    assert h[:8] in ctx["text"]
    conn.close()
