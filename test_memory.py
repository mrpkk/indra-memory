"""Тесты длинной памяти. Ключевое свойство — бюджет жёсткий."""

import time

import pytest

from memory import (
    connect, remember, recall, supersede, link, forget, pack, stats, neighbours
)


@pytest.fixture()
def db(tmp_path):
    return connect(tmp_path / "m.db")


# ------------------------------------------------------------ базовое

def test_remember_and_find(db):
    fid = remember(db, "attest проверяет артефакт до выплаты", tags="attest", importance=5)
    assert fid > 0
    rows = recall(db, "attest проверяет")
    assert len(rows) == 1
    assert rows[0]["id"] == fid
    assert rows[0]["importance"] == 5


def test_empty_fact_rejected(db):
    with pytest.raises(ValueError):
        remember(db, "   ")


def test_survives_reopen(tmp_path):
    """Сессия кончилась, процесс умер — память обязана выжить."""
    path = tmp_path / "m.db"
    remember(connect(path), "факт пережил перезапуск", importance=4)
    rows = recall(connect(path), "пережил")
    assert len(rows) == 1


def test_importance_clamped(db):
    remember(db, "x", importance=99)
    assert recall(db, "x")[0]["importance"] == 5
    remember(db, "y", importance=-5)
    assert recall(db, "y")[0]["importance"] == 1


# ------------------------------------------------------------ поиск

def test_search_is_by_any_word_not_exact_phrase(db):
    remember(db, "корень технологии лежит в двойной записи 1494 года", importance=4)
    rows = recall(db, "двойная запись")
    assert len(rows) == 1


def test_search_many_words_still_finds(db):
    remember(db, "ИИ-агенты платят токенами за API")
    assert len(recall(db, "агенты токенами API")) == 1


def test_search_misses_unrelated(db):
    remember(db, "DeFi TVL упал до 96 миллиардов")
    assert recall(db, "композитор обретает форму") == []


def test_min_importance_filter(db):
    remember(db, "мелочь unimportant", importance=1)
    remember(db, "важное протокол", importance=5)
    rows = recall(db, "мелочь важное", min_importance=3)
    assert len(rows) == 1
    assert rows[0]["text"] == "важное протокол"


def test_superseded_hidden_by_default(db):
    old = remember(db, "старый факт про цену")
    supersede(db, old, "новый факт про цену")
    texts = [r["text"] for r in recall(db, "факт про цену")]
    assert "старый факт про цену" not in texts
    assert "новый факт про цену" in texts


def test_superseded_visible_when_asked(db):
    old = remember(db, "исторический факт")
    supersede(db, old, "актуальный факт")
    texts = [r["text"] for r in recall(db, "факт", include_superseded=True)]
    assert "исторический факт" in texts


def test_forgotten_hidden(db):
    fid = remember(db, "забываемый факт")
    forget(db, fid)
    assert recall(db, "забываемый") == []
    assert recall(db, "забываемый", include_superseded=True)


def test_recency_wins_on_equal_relevance(db):
    """Свежая запись должна вытеснять протухшую при равной важности."""
    old = remember(db, "текущая версия настроек", importance=3)
    conn_ts = db.execute("SELECT updated_at FROM facts WHERE id=?", (old,)).fetchone()[0]
    db.execute("UPDATE facts SET updated_at=? WHERE id=?", (conn_ts - 86400 * 400, old))
    db.commit()
    remember(db, "текущая версия настроек обновлена", importance=3)
    rows = recall(db, "текущая версия настроек", k=1)
    assert "обновлена" in rows[0]["text"]


def test_empty_query_returns_important_first(db):
    remember(db, " unimportant", importance=1)
    remember(db, "критично важное", importance=5)
    rows = recall(db, "")
    assert rows[0]["importance"] == 5


def test_special_characters_do_not_crash(db):
    remember(db, "факт про C++ и (скобки) с двоеточием: да")
    rows = recall(db, '": () C++')
    assert isinstance(rows, list)


# ------------------------------------------------------------ версии

def test_supersede_keeps_history(db):
    old = remember(db, "цена x402 была 0.10")
    new = supersede(db, old, "цена x402 стала 0.05")
    row = db.execute("SELECT status, superseded_by FROM facts WHERE id=?", (old,)).fetchone()
    assert row["status"] == "superseded"
    assert row["superseded_by"] == new


def test_supersede_twice_rejected(db):
    old = remember(db, "первая")
    supersede(db, old, "вторая")
    with pytest.raises(ValueError):
        supersede(db, old, "третья")


def test_supersede_unknown_rejected(db):
    with pytest.raises(KeyError):
        supersede(db, 99999, "нет такого")


# ------------------------------------------------------------ связи

def test_link_both_directions(db):
    a = remember(db, "attest — проверка артефакта")
    b = remember(db, "trust layer — пустая ниша рынка")
    link(db, a, "закрывает", b)
    nb = neighbours(db, a)
    assert any(x["id"] == b and x["direction"] == "out" for x in nb)
    back = neighbours(db, b)
    assert any(x["id"] == a and x["direction"] == "in" for x in back)


def test_link_is_idempotent(db):
    a, b = remember(db, "alpha"), remember(db, "beta")
    link(db, a, "rel", b)
    link(db, a, "rel", b)
    assert db.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"] == 1


def test_forgotten_neighbour_hidden(db):
    a, b = remember(db, "alpha fact"), remember(db, "beta fact")
    link(db, a, "rel", b)
    forget(db, b)
    assert all(x["id"] != b for x in neighbours(db, a))


# ------------------------------------------------------------ бюджет

def test_pack_respects_token_budget(db):
    """Главное свойство: сколько бы фактов ни накопилось, в контекст
    влезает ровно заданный бюджет. Деградация на длине невозможна."""
    for i in range(400):
        remember(db, f"факт номер {i} про устройство моста между агентами", importance=3)
    for budget in (100, 300, 800, 2000):
        out = pack(db, "мост", budget)
        est = len(out) / 220.0 + out.count("\n")
        assert est <= budget * 1.15 + 10, f"бюджет {budget} превышен: {est:.0f}"


def test_pack_grows_with_budget(db):
    for i in range(100):
        remember(db, f"факт {i} про агента", importance=3)
    small = pack(db, "агента", 100)
    big = pack(db, "агента", 2000)
    assert len(big) > len(small)


def test_pack_empty_when_nothing_matches(db):
    remember(db, "совсем другое")
    assert pack(db, "несуществующий термин xyzzy", 800).strip() == ""


def test_pack_includes_linked_context(db):
    a = remember(db, "attest проверяет артефакт", importance=5)
    b = remember(db, "мост держит эскроу", importance=5)
    link(db, a, "входит_в", b)
    out = pack(db, "attest", 800)
    assert "attest" in out
    assert "эскроу" in out


# ------------------------------------------------------------ статистика

def test_stats_counts(db):
    a = remember(db, "первый факт")
    b = remember(db, "второй факт")
    remember(db, "третий факт")
    link(db, a, "rel", b)
    supersede(db, b, "четвёртый факт")
    forget(db, a)
    st = stats(db)
    assert st["всего"] == 4
    assert st["активных"] == 2
    assert st["заменено"] == 1
    assert st["забыто"] == 1
    assert st["связей"] == 1


def test_stats_empty_db(db):
    st = stats(db)
    assert st["всего"] == 0
    assert st["последняя запись"] == "—"


# ------------------------------------------------------------ устойчивость

def test_db_survives_corrupt_query_characters(db):
    remember(db, "обычный факт")
    for bad in ('"', "*", "NEAR(", "a AND", "^", "-", ":", "(unbalanced"):
        assert isinstance(recall(db, bad), list), bad


def test_concurrent_safe_sequential_writes(db):
    for i in range(200):
        remember(db, f"факт {i}")
    assert stats(db)["всего"] == 200