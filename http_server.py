#!/usr/bin/env python3
"""HTTP-обёртка над долговременной памятью Индры.

Библиотека `memory.py` работает и проверена 28 тестами, но вызывалась
только из кода. Наружу не выставлена — то есть продукт, который можно
попробовать, не существует.

Здесь ровно обвязка: те же функции, тот же SQLite, доступ по HTTP.
Ничего нового по смыслу не добавлено — только способ доступа.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import memory  # noqa: E402

LANDING = """<!doctype html><meta charset=utf-8><title>indra-memory</title>
<style>
 body{background:#0b0d12;color:#e8ecf4;font:16px/1.6 system-ui,sans-serif;
      max-width:760px;margin:60px auto;padding:0 24px}
 code{background:#141821;padding:2px 6px;border-radius:5px;color:#f0a35e}
 h1{font-size:26px;margin-bottom:4px} .s{color:#969eac}
 li{margin:6px 0} .box{background:#141821;border-radius:10px;padding:14px 18px;margin:14px 0}
</style>
<h1>indra-memory</h1>
<p class=s>Long memory for agents. Facts live outside the context window.</p>

<div class=box><b>Why.</b> Context-bound memory degrades with length — the model
physically cannot see the start of its own conversation. Here facts live in
SQLite with full-text search, and only a relevance-ranked sample enters the
prompt.</div>

<h2>Try it</h2>
<div class=box>
<b>Store</b><br>
<pre>curl -X POST http://HOST:PORT/remember \\
  -H 'Content-Type: application/json' \\
  -d '{"text":"user prefers Russian","tags":["pref"]}'</pre>

<b>Recall</b><br>
<pre>curl 'http://HOST:PORT/recall?q=language'</pre>

<b>Forget</b> — <code>POST /forget {"id": 1}</code><br>
<b>Stats</b> — <code>GET /stats</code>
</div>

<p class=s>Free during preview. Public repo: github.com/mrpkk/indra-memory</p>
"""


def _rows(rows) -> list[dict]:
    """recall() отдаёт sqlite3.Row — их нельзя положить в JSON.

    Плоский dict достаточно: в этих полях нет вложенности, а Row
    сериализуется только вручную, по именам колонок.
    """
    return [dict(r) for r in rows]


def _tags(raw: object) -> str:
    """tags у remember() — строка через запятую; HTTP-клиент шлёт список.

    Приводить молча нельзя: иначе тег молча пропадёт, и факт окажется
    без метки, по которой его потом ищут.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, (list, tuple)):
        return ",".join(str(t).strip() for t in raw if str(t).strip())
    raise ValueError("tags должны быть строкой или списком строк")


def _base_url() -> str:
    """Публичный адрес: в sitemap обязаны быть абсолютные URL."""
    import os
    return os.environ.get("INDRA_MEMORY_BASE_URL", "https://rta.lat/memory").rstrip("/")


def _robots() -> str:
    """Краулеров пускаем: индексация витрины — это трафик."""
    return ("User-agent: *\n"
            "Allow: /\n"
            "Disallow: /mcp\n"
            f"Sitemap: {_base_url()}/sitemap.xml\n")


def _sitemap() -> str:
    """Карта сайта: документы, а не endpoints.

    `/subscribe`, `/quote`, `/check` сюда не идут — их индексация
    бессмысленна, это машиночитаемые адреса.
    """
    base = _base_url()
    urls = "".join(
        f"<url><loc>{base}{page}</loc></url>"
        for page in ('/', '/health', '/recall')
    )
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{urls}</urlset>")



def _schema_block() -> str:
    """Разметка товара для поиска: тип, имя, цена, где найти."""
    data = {
    "@type": "SoftwareApplication",
    "name": "indra-memory",
    "applicationCategory": "DeveloperApplication",
    "operatingSystem": "Any",
    "description": "Persistent memory for AI agents over HTTP: write, recall, forget and tag facts across sessions without a client SDK.",
    "url": "https://rta.lat/memory/",
    "offers": {
        "@type": "Offer",
        "price": "0",
        "priceCurrency": "USD"
    }
}
    data["@context"] = "https://schema.org"
    return ('<script type="application/ld+json">'
            + json.dumps(data, ensure_ascii=False) + "</script>")


def _with_schema(html: str) -> str:
    """Вставить разметку в HTML. Идемпотентно: повторный вызов не дублирует."""
    if not isinstance(html, str) or "application/ld+json" in html:
        return html
    block = _schema_block()
    lowered = html.lower()
    for marker in ("</head>", "<body", "</title>"):
        pos = lowered.find(marker)
        if pos != -1:
            return html[:pos] + block + html[pos:]
    import re
    m = re.search(r"<meta[^>]+charset[^>]*>", html, re.IGNORECASE)
    if m:
        return html[:m.end()] + block + html[m.end():]
    return block + html

class Handler(BaseHTTPRequestHandler):
    server_version = "indra-memory"

    def _send(self, code: int, payload: dict | str) -> None:
        raw = payload.encode() if isinstance(payload, str) else json.dumps(
            payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type",
                         "text/html; charset=utf-8" if isinstance(payload, str)
                         else "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path.rstrip("/") or "/"
        for pfx in ("/memory",):
            if path == pfx or path.startswith(pfx + "/"):
                path = path[len(pfx):] or "/"
                break
        conn = memory.connect()
        try:
            if path == "/robots.txt":
                self._send(200, _robots())
            elif path == "/sitemap.xml":
                self._send(200, _sitemap())
            if path == "/":
                self._send(200, _with_schema(LANDING))
            elif path == "/health":
                self._send(200, {"status": "ok", "service": "indra-memory"})
            elif path == "/stats":
                self._send(200, memory.stats(conn))
            elif path == "/recall":
                facts = memory.recall(conn, (q.get("q") or [""])[0],
                                      k=int((q.get("limit") or ["20"])[0]))
                self._send(200, {"facts": _rows(facts)})
            else:
                self._send(404, {"error": "not found", "path": path})
        finally:
            conn.close()

    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        for pfx in ("/memory",):
            if path == pfx or path.startswith(pfx + "/"):
                path = path[len(pfx):] or "/"
                break
        body = self._body()
        conn = memory.connect()
        try:
            if path == "/remember":
                text = (body.get("text") or "").strip()
                if not text:
                    return self._send(400, {"error": "text is required"})
                fact_id = memory.remember(conn, text,
                                          tags=_tags(body.get("tags")),
                                          importance=int(body.get("importance") or 3))
                self._send(200, {"id": fact_id, "stored": True})
            elif path == "/forget":
                fid = body.get("id")
                if fid is None:
                    return self._send(400, {"error": "id is required"})
                memory.forget(conn, int(fid))
                self._send(200, {"id": int(fid), "forgotten": True})
            else:
                self._send(404, {"error": "not found", "path": path})
        finally:
            conn.close()

    def log_message(self, *_a) -> None:
        pass


def serve(host: str = "127.0.0.1", port: int = 8186) -> None:
    print(f"indra-memory: http://{host}:{port}", flush=True)
    srv = ThreadingHTTPServer((host, port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8186)
    a = ap.parse_args()
    serve(a.host, a.port)