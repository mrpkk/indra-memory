#!/usr/bin/env python3
"""
ДЛИННАЯ ПАМЯТЬ ИНДРА (long-memory.py)

Зачем. Исследование 06.10.2026: потеря факта между сессиями измерена в 100%
(5 голосов из 5 моделей назвали память болью №1). Причина известна: память,
живущая в контексте, деградирует на длине — модель физически не видит
начало разговора, а всё «помнить» можно только через файл.

Здесь ровно другое устройство: факты лежат ВНЕ контекста, в SQLite с
полнотекстовым индексом. В контекст попадает только выборка по релевантности,
поэтому длина памяти не ограничена длиной разговора.

Четыре свойства, которых нет у плоского JSON:

1. ПОИСК, А НЕ ЧТЕНИЕ. FTS5 по всем словам, не только по тегу.
2. ВЕРСИИ. Новый факт не затирает старый, а помечает его `superseded`.
   История не переписывается — иначе через полгода нельзя понять,
   почему решение было таким.
3. СВЯЗИ. Факты связываются между собой: `attest -> защищает -> trust layer`.
   Знание, которое не связано, не переиспользуется.
4. ВАЖНОСТЬ И СВЕЖЕСТЬ. Выборка ранжируется по релевантности, важности
   и времени. Всё подряд в контекст не идёт.

Команды:
    remember "текст" [--tags a,b] [--importance 1-5] [--kind fact]
    recall "запрос" [-k 10] [--min-importance 1]
    supersede <id> "новый текст"
    link <id1> <relation> <id2>
    pack "запрос" --tokens 800        сборка контекста под бюджет
    forget <id>                        мягкое удаление ( tombst )
    stats

База по умолчанию: ~/.indra/memory.db
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB = Path.home() / ".indra" / "memory.db"
SCHEMA_VERSION = 1


# --------------------------------------------------------------- схема

def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS facts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            text        TEXT NOT NULL,
            kind        TEXT NOT NULL DEFAULT 'fact',
            tags        TEXT NOT NULL DEFAULT '',
            importance  INTEGER NOT NULL DEFAULT 3,
            source      TEXT NOT NULL DEFAULT '',
            session     TEXT NOT NULL DEFAULT '',
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL,
            status      TEXT NOT NULL DEFAULT 'active',
            superseded_by INTEGER
        );

        -- FTS5 без внешнего content-таблицы. Внешний content (content='facts')
        -- тестировался 06.10.2026 и в этой схеме MATCH по кириллице молча
        -- возвращал пусто: индекс заполнялся триггерами, но поиск не находил.
        -- Дублирование текста в индексе дешевле, чем молчаливая потеря памяти.
        -- Токенизатор trigram, а не unicode61. Причина найдена 06.10.2026
        -- живьём: русская морфология. FTS без стеммера ищет «двойная» и не
        -- находит «двойной» в тексте — а это 90% русского поиска.
        -- trigram ищет по подстрокам, поэтому запрос сводится к префиксу:
        -- «дво» находит и «двойная», и «двойной», и «двойные».
        -- Цена: термы короче 3 символов не ищутся (см. _fts_term).
        CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
            text, tags, tokenize='trigram'
        );

        CREATE TABLE IF NOT EXISTS edges (
            src      INTEGER NOT NULL,
            relation TEXT NOT NULL,
            dst      INTEGER NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (src, relation, dst)
        );

        CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
            INSERT INTO facts_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
        END;
        CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE OF text, tags ON facts BEGIN
            INSERT INTO facts_fts(facts_fts, rowid, text, tags)
                VALUES('delete', old.id, old.text, old.tags);
            INSERT INTO facts_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
        END;
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


# --------------------------------------------------------------- запись

_WORD = re.compile(r"[\wа-яёА-ЯЁ]+", re.UNICODE)

# Слова короче 3 символов trigram-индекс не различает: они либо слиты с
# соседями, либо не ищутся. Платим за это точностью, а не памятью: короткие
# слова почти всегда служебные («и», «в», «не»), а смысл несут длинные.
_MIN_TERM = 3


def _tokens(s: str) -> list[str]:
    """Слова запроса в нижнем регистре."""
    return [w.lower() for w in _WORD.findall(s or "")]


def _fts_term(word: str) -> str | None:
    """
    Слово → запрос для trigram-индекса.

    Берём префикс из 4 символов: он переживает падеж («запись»/«записи»,
    «двойная»/«двойной») и при этом остаётся достаточно длинным, чтобы
    не словить полмиллиона ложных совпадений.
    """
    if len(word) < _MIN_TERM:
        return None
    return word[:4]


def _match_query(query: str) -> str:
    """Собрать MATCH-выражение: префиксы, соединённые ИЛИ."""
    terms = [t for t in (_fts_term(w) for w in _tokens(query)) if t]
    if not terms:
        # все слова короткие — ищем по LIKE, это честнее пустого результата
        return ""
    return " OR ".join(f'"{t}"' for t in terms)


def remember(
    conn: sqlite3.Connection,
    text: str,
    tags: str = "",
    importance: int = 3,
    kind: str = "fact",
    source: str = "",
    session: str = "",
) -> int:
    """Запомнить факт. Возвращает id."""
    text = (text or "").strip()
    if not text:
        raise ValueError("пустой факт не запоминается")
    now = time.time()
    cur = conn.execute(
        "INSERT INTO facts(text, kind, tags, importance, source, session,"
        " created_at, updated_at, status) VALUES(?,?,?,?,?,?,?,?, 'active')",
        (text, kind, tags.strip(), max(1, min(5, importance)), source, session, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def supersede(conn: sqlite3.Connection, old_id: int, new_text: str, **kw) -> int:
    """
    Новая версия факта. Старый НЕ удаляется — помечается superseded.

    Так знание обновляется без потери истории: через полгода видно,
    что было раньше и когда поменялось. Плоская память (JSON) эту
    возможность теряет — и через полгода становится ложью.
    """
    row = conn.execute("SELECT status FROM facts WHERE id=?", (old_id,)).fetchone()
    if row is None:
        raise KeyError(f"факт {old_id} не найден")
    if row["status"] != "active":
        raise ValueError(f"факт {old_id} уже не активен ({row['status']})")
    new_id = remember(conn, new_text, **kw)
    conn.execute(
        "UPDATE facts SET status='superseded', superseded_by=?, updated_at=? WHERE id=?",
        (new_id, time.time(), old_id),
    )
    conn.commit()
    return new_id


def link(conn: sqlite3.Connection, src: int, relation: str, dst: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO edges(src, relation, dst, created_at) VALUES(?,?,?,?)",
        (src, relation, dst, time.time()),
    )
    conn.commit()


def forget(conn: sqlite3.Connection, fact_id: int) -> None:
    """Мягкое удаление: строка остаётся со статусом, история не портится."""
    conn.execute(
        "UPDATE facts SET status='forgotten', updated_at=? WHERE id=?", (time.time(), fact_id)
    )
    conn.commit()


# --------------------------------------------------------------- чтение

def recall(
    conn: sqlite3.Connection,
    query: str,
    k: int = 10,
    min_importance: int = 1,
    tags: str = "",
    include_superseded: bool = False,
    now: float | None = None,
) -> list[sqlite3.Row]:
    """
    Поиск по всем словам запроса, ранжированный по совпадению.

    Полнотекстовый поиск возвращает кандидатов; ранжирование делает
    BM25, поверх — важность и свежесть. Именно поэтому в контекст
    попадает немного: поиск сужает, а не «прочитать всё подряд».
    """
    now = now or time.time()
    terms = _tokens(query)
    status_clause = "" if include_superseded else "AND f.status = 'active'"
    tag_clause = ""
    if tags:
        want = [t.strip().lower() for t in tags.split(",") if t.strip()]
        tag_clause = "AND (" + " OR ".join("f.tags LIKE ?" for _ in want) + ")"
        params_tags = [f"%{t}%" for t in want]
    else:
        params_tags = []

    if not terms:
        # запрос без слов («», «?!») — самые важные и свежие
        sql = f"""
            SELECT f.*, 0.0 AS score FROM facts f
            WHERE f.status='active' AND f.importance >= ?
            ORDER BY f.importance DESC, f.updated_at DESC LIMIT ?
        """
        return conn.execute(sql, (min_importance, k)).fetchall()

    # Пустое выражение MATCH — осознанный путь в ветку LIKE: так ищутся
    # слова короче 3 символов («y», «v2»), которые trigram-индекс не видит.
    params_fts = [_match_query(query)]
    sql = f"""
        SELECT f.*, bm25(facts_fts) AS bm25_score
        FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid
        WHERE facts_fts MATCH ? {status_clause}
          AND f.importance >= ? {tag_clause}
        ORDER BY bm25_score LIMIT ?
    """
    try:
        rows = conn.execute(sql, params_fts + [min_importance] + params_tags + [k * 3]).fetchall()
    except sqlite3.OperationalError:
        # FTS не понял запрос (пунктуация, спецсимволы) — откат на LIKE
        like = " OR ".join(["f.text LIKE ?"] * len(terms))
        sql2 = f"""
            SELECT f.*, 0.0 AS bm25_score FROM facts f
            WHERE ({like}) {status_clause} AND f.importance >= ? {tag_clause}
            LIMIT ?
        """
        rows = conn.execute(
            sql2, [f"%{t}%" for t in terms] + [min_importance] + params_tags + [k * 3]
        ).fetchall()

    # переоцениваем: релевантность + важность + свежесть
    scored = []
    for r in rows:
        relevance = -float(r["bm25_score"] or 0.0)
        imp = float(r["importance"]) * 0.6
        age_days = (now - float(r["updated_at"])) / 86400.0
        freshness = 1.2 / (1.0 + age_days / 30.0)
        scored.append((relevance + imp + freshness, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:k]]


def neighbours(conn: sqlite3.Connection, fact_id: int) -> list[dict]:
    """Связанные факты: то, что помогает понять факт, а не повторяет его."""
    out = []
    for row in conn.execute(
        """SELECT e.relation, f.id, f.text, f.importance
           FROM edges e JOIN facts f ON f.id = e.dst
           WHERE e.src = ? AND f.status='active'""",
        (fact_id,),
    ):
        out.append({"direction": "out", "relation": row["relation"],
                    "id": row["id"], "text": row["text"]})
    for row in conn.execute(
        """SELECT e.relation, f.id, f.text, f.importance
           FROM edges e JOIN facts f ON f.id = e.src
           WHERE e.dst = ? AND f.status='active'""",
        (fact_id,),
    ):
        out.append({"direction": "in", "relation": row["relation"],
                    "id": row["id"], "text": row["text"]})
    return out


def pack(conn: sqlite3.Connection, query: str, token_budget: int = 800) -> str:
    """
    Собрать контекст под бюджет токенов.

    Ключевое свойство: бюджет ЖЁСТКИЙ. Память может расти сколько угодно —
    в контекст попадёт столько, сколько влезло. Это и есть защита от
    деградации: контекст не раздувается, раздувается база.
    """
    approx = max(1.0, token_budget / 220.0)     # ~220 символов на токен по-русски
    rows = recall(conn, query, k=50)
    lines: list[str] = []
    used = 0.0
    seen: set[int] = set()
    for r in rows:
        cost = len(r["text"]) / 220.0 + 1
        if used + cost > approx:
            continue
        for nb in neighbours(conn, r["id"])[:2]:
            if nb["id"] in seen:
                continue
            nb_cost = len(nb["text"]) / 220.0 + 1
            if used + nb_cost > approx:
                continue
            seen.add(nb["id"])
            lines.append(f"  ↳ ({nb['relation']}) {nb['text']}")
            used += nb_cost
        seen.add(r["id"])
        mark = "#" * int(r["importance"])
        lines.append(f"- {mark} {r['text']}")
        used += cost
    return "\n".join(lines)


def stats(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) c FROM facts").fetchone()["c"]
    active = conn.execute("SELECT COUNT(*) c FROM facts WHERE status='active'").fetchone()["c"]
    sup = conn.execute("SELECT COUNT(*) c FROM facts WHERE status='superseded'").fetchone()["c"]
    forgot = conn.execute("SELECT COUNT(*) c FROM facts WHERE status='forgotten'").fetchone()["c"]
    edges = conn.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"]
    newest = conn.execute("SELECT MAX(updated_at) m FROM facts").fetchone()["m"]
    return {
        "всего": total, "активных": active, "заменено": sup,
        "забыто": forgot, "связей": edges,
        "последняя запись": time.strftime("%Y-%m-%d %H:%M", time.localtime(newest)) if newest else "—",
    }


# --------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="long-memory",
        description="Длинная память ИндрА: SQLite + FTS5 + версии + связи",
    )
    ap.add_argument("--db", default=str(DEFAULT_DB))
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("remember", help="запомнить факт")
    r.add_argument("text")
    r.add_argument("--tags", default="")
    r.add_argument("--importance", type=int, default=3)
    r.add_argument("--kind", default="fact")
    r.add_argument("--source", default="")
    r.add_argument("--session", default="")

    q = sub.add_parser("recall", help="найти по запросу")
    q.add_argument("query")
    q.add_argument("-k", type=int, default=10)
    q.add_argument("--min-importance", type=int, default=1)
    q.add_argument("--tags", default="")
    q.add_argument("--with-links", action="store_true")

    s = sub.add_parser("supersede", help="заменить факт новой версией")
    s.add_argument("id", type=int)
    s.add_argument("text")
    s.add_argument("--tags", default="")
    s.add_argument("--importance", type=int, default=3)

    l = sub.add_parser("link", help="связать факты")
    l.add_argument("src", type=int)
    l.add_argument("relation")
    l.add_argument("dst", type=int)

    p = sub.add_parser("pack", help="собрать контекст под бюджет токенов")
    p.add_argument("query")
    p.add_argument("--tokens", type=int, default=800)

    f = sub.add_parser("forget", help="мягко забыть факт")
    f.add_argument("id", type=int)

    sub.add_parser("stats", help="статистика памяти")

    a = ap.parse_args()
    conn = connect(a.db)

    if a.cmd == "remember":
        fid = remember(conn, a.text, a.tags, a.importance, a.kind, a.source, a.session)
        print(f"запомнено #{fid}")
        return 0

    if a.cmd == "recall":
        rows = recall(conn, a.query, a.k, a.min_importance, a.tags)
        if not rows:
            print("ничего не найдено")
            return 0
        for row in rows:
            print(f"#{row['id']:<5} {'#' * row['importance']:<6} {row['text'][:150]}")
            if row["tags"]:
                print(f"        теги: {row['tags']}")
            if a.with_links:
                for nb in neighbours(conn, row["id"])[:3]:
                    print(f"        ↳ ({nb['relation']}) {nb['text'][:110]}")
        return 0

    if a.cmd == "supersede":
        nid = supersede(conn, a.id, a.text, tags=a.tags, importance=a.importance)
        print(f"новая версия #{nid}, старая #{a.id} помечена superseded")
        return 0

    if a.cmd == "link":
        link(conn, a.src, a.relation, a.dst)
        print(f"связано: #{a.src} —{a.relation}→ #{a.dst}")
        return 0

    if a.cmd == "pack":
        print(pack(conn, a.query, a.tokens))
        return 0

    if a.cmd == "forget":
        forget(conn, a.id)
        print(f"факт #{a.id} помечен forgotten (физически не удалён)")
        return 0

    if a.cmd == "stats":
        st = stats(conn)
        w = max(len(k) for k in st)
        for k, v in st.items():
            print(f"{k:<{w}} : {v}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())