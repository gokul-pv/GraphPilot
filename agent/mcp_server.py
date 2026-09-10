"""
MCP server

Eleven tools, stdio transport:
    web_search, fetch_url, get_time, currency_convert,
    read_file, list_dir, create_file, update_file, edit_file,
    index_document, search_knowledge

web_search:        Tavily primary, DuckDuckGo fallback. Hard-capped at 5 results.
fetch_url:         crawl4ai only. Clean markdown via headless Chromium.
index_document:    Chunks a sandbox file or artifact and writes the chunks as
                   fact records into Memory, where they become FAISS-searchable.
search_knowledge:  Vector search over indexed facts. Same backend as
                   memory.read but exposed to the model as a tool.

Usage for tavily and duckduckgo is logged to ./usage.json with monthly
rollover and a soft cap of 950/1000 on Tavily.

File tools are sandboxed under ./sandbox/. Run:  python mcp_server.py
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from ddgs import DDGS
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Same-directory imports for the Memory and Artifact services so that the
# new index_document / search_knowledge tools can delegate into them.
import sys
sys.path.insert(0, str(Path(__file__).parent))
import artifacts as _artifacts  # noqa: E402
import memory as _memory  # noqa: E402

MAX_SEARCH_RESULTS = 5  # hard cap — Tavily prices per result

load_dotenv(Path(__file__).parent / ".env")

mcp = FastMCP("MCP-server")

SANDBOX = Path(__file__).parent / "sandbox"
SANDBOX.mkdir(exist_ok=True)

USAGE_PATH = Path(__file__).parent / "usage.json"
MONTHLY_CAP = 950  # leave 50/mo headroom on Tavily
_usage_lock = threading.Lock()


def _safe(path: str) -> Path:
    p = (SANDBOX / path).resolve()
    base = SANDBOX.resolve()
    if p != base and base not in p.parents:
        raise ValueError(f"Path '{path}' escapes the sandbox")
    return p


def _empty_usage(month: str) -> dict:
    return {
        "month": month,
        "tavily": {"count": 0, "errors": 0},
        "duckduckgo": {"count": 0, "errors": 0},
    }


def _load_usage() -> dict:
    month = datetime.now().strftime("%Y-%m")
    if not USAGE_PATH.exists():
        return _empty_usage(month)
    try:
        data = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty_usage(month)
    if data.get("month") != month:
        return _empty_usage(month)
    for k in ("tavily", "duckduckgo"):
        data.setdefault(k, {"count": 0, "errors": 0})
    return data


def _save_usage(data: dict) -> None:
    USAGE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _bump(provider: str, field: str = "count") -> None:
    with _usage_lock:
        data = _load_usage()
        data[provider][field] = data[provider].get(field, 0) + 1
        _save_usage(data)


def _under_cap(provider: str) -> bool:
    return _load_usage()[provider]["count"] < MONTHLY_CAP


def _tavily_search(query: str, max_results: int) -> list[dict]:
    from tavily import TavilyClient

    client = TavilyClient(os.environ["TAVILY_API_KEY"])
    resp = client.search(query=query, max_results=max_results, search_depth="advanced")
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", ""),
        }
        for r in resp.get("results", [])
    ]


def _ddg_search(query: str, max_results: int) -> list[dict]:
    hits: list[dict] = []
    with DDGS() as ddgs:
        for backend in ("auto", "html", "lite"):
            try:
                hits = list(ddgs.text(query, max_results=max_results, backend=backend))
            except Exception:
                hits = []
            if hits:
                break
    return [
        {
            "title": h.get("title", ""),
            "url": h.get("href", ""),
            "snippet": h.get("body", ""),
        }
        for h in hits
    ]


async def _crawl4ai_fetch(url: str) -> dict:
    from crawl4ai import AsyncWebCrawler

    # crawl4ai uses Rich which writes via its own captured stdout reference, so
    # contextlib.redirect_stdout doesn't catch it. Redirect at the file-descriptor
    # level — crawl4ai's banner / [FETCH] / [SCRAPE] markers would otherwise
    # corrupt the MCP stdio JSON-RPC stream.
    saved_fd = os.dup(1)
    os.dup2(2, 1)
    try:
        async with AsyncWebCrawler(verbose=False) as crawler:
            r = await crawler.arun(url=url)
    finally:
        os.dup2(saved_fd, 1)
        os.close(saved_fd)
    # r.markdown is a str subclass (StringCompatibleMarkdown) that Pydantic
    # serializes as {} because its real field is private. Pull the raw string
    # out and force a plain str so FastMCP serializes correctly.
    md = r.markdown
    raw = (
        getattr(md, "raw_markdown", None)
        or getattr(md, "fit_markdown", None)
        or md
        or r.cleaned_html
        or r.html
        or ""
    )
    text = str(raw)
    return {
        "status": int(getattr(r, "status_code", None) or 200),
        "content_type": "text/markdown",
        "length_bytes": len(text.encode("utf-8")),
        "text": text,
    }


@mcp.tool()
def web_search(query: str, max_results: int = 5) -> list[dict]:
    """Search the web (Tavily primary, DDG fallback). Hard-capped at 5 results. Example: web_search("python asyncio tutorial", 3)."""
    max_results = max(1, min(max_results, MAX_SEARCH_RESULTS))
    if os.environ.get("TAVILY_API_KEY") and _under_cap("tavily"):
        try:
            results = _tavily_search(query, max_results)
            if results:
                _bump("tavily")
                return results
        except Exception:
            _bump("tavily", "errors")
    results = _ddg_search(query, max_results)
    _bump("duckduckgo")
    return results


@mcp.tool()
async def fetch_url(url: str, timeout: int = 20) -> dict:
    """Fetch clean markdown from a URL via crawl4ai (headless Chromium). Example: fetch_url("https://example.com")."""
    return await _crawl4ai_fetch(url)


@mcp.tool()
def get_time(timezone: str = "UTC") -> dict:
    """Current time in a named IANA timezone. Example: get_time("Asia/Kolkata")."""
    tz = ZoneInfo(timezone)
    now = datetime.now(tz)
    offset = now.utcoffset()
    offset_hours = offset.total_seconds() / 3600 if offset else 0.0
    return {
        "iso": now.isoformat(),
        "human": now.strftime("%A, %d %B %Y %H:%M:%S %Z"),
        "timezone": timezone,
        "offset_hours": offset_hours,
    }


@mcp.tool()
def currency_convert(amount: float, from_currency: str, to_currency: str) -> dict:
    """Convert money between ISO-3 currencies via frankfurter.dev. Example: currency_convert(100, "USD", "INR")."""
    f = from_currency.upper()
    t = to_currency.upper()
    url = f"https://api.frankfurter.dev/v1/latest?amount={amount}&base={f}&symbols={t}"
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        data = r.json()
    converted = data["rates"][t]
    return {
        "amount": amount,
        "from": f,
        "to": t,
        "rate": converted / amount if amount else 0.0,
        "converted": converted,
        "date": data["date"],
        "source": "frankfurter.dev",
    }


@mcp.tool()
def read_file(path: str) -> dict:
    """Read a UTF-8 text file from the sandbox. Example: read_file("notes.txt")."""
    p = _safe(path)
    text = p.read_text(encoding="utf-8")
    return {
        "path": path,
        "size_bytes": p.stat().st_size,
        "content": text,
        "encoding": "utf-8",
    }


@mcp.tool()
def list_dir(path: str = ".") -> dict:
    """List a directory inside the sandbox. Example: list_dir(".")."""
    # NOTES_RUNS §6 (1): a list[dict] return was being rendered as one MCP
    # TextContent per entry. After agent7.py's 300-char clip and decision.py's
    # downstream slicing, only the first 2-3 file dicts survived into the
    # Decision prompt, and Decision then declared the directory complete at
    # whatever it could see. Returning a single dict with `count` and a flat
    # `names` list keeps the cardinality visible even under truncation.
    p = _safe(path)
    entries = []
    names: list[str] = []
    for child in sorted(p.iterdir()):
        is_dir = child.is_dir()
        entries.append({
            "name": child.name,
            "type": "dir" if is_dir else "file",
            "size_bytes": 0 if is_dir else child.stat().st_size,
        })
        names.append(child.name)
    return {"path": path, "count": len(entries), "names": names, "entries": entries}


@mcp.tool()
def create_file(path: str, content: str) -> dict:
    """Create a new file in the sandbox; errors if it exists. Example: create_file("hello.txt", "hi")."""
    p = _safe(path)
    if p.exists():
        raise ValueError(f"File '{path}' already exists")
    if not p.parent.exists():
        raise ValueError(f"Parent directory of '{path}' does not exist")
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": path, "size_bytes": p.stat().st_size}


@mcp.tool()
def update_file(path: str, content: str) -> dict:
    """Overwrite an existing sandbox file. Example: update_file("hello.txt", "new body")."""
    p = _safe(path)
    if not p.exists():
        raise ValueError(f"File '{path}' does not exist")
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": path, "size_bytes": p.stat().st_size}


@mcp.tool()
def edit_file(path: str, find: str, replace: str, replace_all: bool = False) -> dict:
    """Find-and-replace inside a sandbox file. Example: edit_file("hello.txt", "foo", "bar")."""
    p = _safe(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(find)
    if count == 0:
        raise ValueError(f"'{find}' not found in '{path}'")
    if count > 1 and not replace_all:
        raise ValueError(
            f"'{find}' occurs {count} times in '{path}'; pass replace_all=True"
        )
    new_text = text.replace(find, replace) if replace_all else text.replace(find, replace, 1)
    p.write_text(new_text, encoding="utf-8")
    replacements = count if replace_all else 1
    return {
        "ok": True,
        "path": path,
        "replacements": replacements,
        "size_bytes": p.stat().st_size,
    }


# ── document indexing (Session 7) ───────────────────────────────────────────

def _read_for_index(path: str) -> tuple[str, str]:
    """Return (content, source_label) for an indexable file or artifact."""
    if path.startswith("art:"):
        return _artifacts.get_bytes(path).decode("utf-8", errors="replace"), path
    p = _safe(path)
    return p.read_text(encoding="utf-8"), f"sandbox:{path}"


def _chunk_text(text: str, size: int = 400, overlap: int = 80) -> list[str]:
    """Sliding-window chunking by word count. S7 default; semantic chunking
    arrives in Session 8."""
    words = text.split()
    if not words:
        return []
    chunks: list[str] = []
    stride = max(1, size - overlap)
    i = 0
    while i < len(words):
        chunks.append(" ".join(words[i:i + size]))
        if i + size >= len(words):
            break
        i += stride
    return chunks


@mcp.tool()
def index_document(path: str, chunk_size: int = 400, overlap: int = 80) -> dict:
    """Chunk a sandbox file or artifact and write each chunk into Memory as a searchable `fact`. Use this when the content must remain retrievable across later turns or runs (an indexing step before later vector queries). For one-shot inspection of a known file's contents in this turn, prefer `read_file` instead. Example: index_document("notes/spec.md")."""
    text, source = _read_for_index(path)
    if not text.strip():
        return {"path": path, "source": source, "chunks_indexed": 0, "warning": "empty content"}
    chunks = _chunk_text(text, size=chunk_size, overlap=overlap)
    run_id = f"index-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    indexed = 0
    for i, chunk in enumerate(chunks):
        preview = chunk[:120].replace("\n", " ")
        descriptor = f"[{source} chunk {i+1}/{len(chunks)}] {preview}"
        _memory.add_fact(
            descriptor=descriptor,
            value={
                "chunk": chunk,
                "chunk_index": i,
                "total_chunks": len(chunks),
                "source": source,
            },
            source=source,
            run_id=run_id,
        )
        indexed += 1
    return {
        "path": path,
        "source": source,
        "chunks_indexed": indexed,
        "chunk_size": chunk_size,
        "overlap": overlap,
    }


@mcp.tool()
def search_knowledge(query: str, k: int = 5) -> list[dict]:
    """Vector search over indexed `fact` chunks. Returns up to k ranked chunks with provenance. Call this rather than re-fetching URLs or re-reading source files whenever Memory already contains indexed chunks for the topic — that is the whole point of having indexed the corpus. Example: search_knowledge("authentication flow", 5)."""
    items = _memory.read(query, kinds=["fact"], top_k=k)
    return [
        {
            "id": item.id,
            "descriptor": item.descriptor,
            "source": item.source,
            "chunk": item.value.get("chunk") or "",
            "metadata": {k_: v for k_, v in item.value.items() if k_ != "chunk"},
        }
        for item in items
    ]


if __name__ == "__main__":
    mcp.run(transport="stdio")
