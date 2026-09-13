"""Tests for the MCP server. Run: pytest -v test_mcp_server.py"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parents[1]
SERVER = HERE / "mcp_server.py"
SANDBOX = HERE / "sandbox"

def _result(res) -> object:
    """Extract a structured payload from a CallToolResult."""
    if getattr(res, "structuredContent", None) is not None:
        sc = res.structuredContent
        if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
            return sc["result"]
        return sc
    block = res.content[0]
    text = getattr(block, "text", None)
    if text is None:
        return block
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text

def _clean_sandbox() -> None:
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    SANDBOX.mkdir()

@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def session():
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            yield s

@pytest.mark.network
@pytest.mark.asyncio
async def test_web_search(session):
    res = await session.call_tool("web_search", {"query": "python asyncio", "max_results": 3})
    data = _result(res)
    print("web_search:", data)
    assert isinstance(data, list)
    assert len(data) >= 1
    for hit in data:
        assert {"title", "url", "snippet"} <= set(hit)

@pytest.mark.network
@pytest.mark.asyncio
async def test_fetch_url(session):
    res = await session.call_tool("fetch_url", {"url": "https://example.com"})
    data = _result(res)
    print("fetch_url status/len:", data["status"], data["length_bytes"])
    assert data["status"] == 200
    assert "Example Domain" in data["text"]
    assert data["length_bytes"] > 0
    assert "text" in data["content_type"].lower() or "html" in data["content_type"].lower()

@pytest.mark.asyncio
async def test_get_time(session):
    res = await session.call_tool("get_time", {"timezone": "Asia/Kolkata"})
    data = _result(res)
    print("get_time:", data)
    assert data["timezone"] == "Asia/Kolkata"
    assert data["offset_hours"] == 5.5
    assert "T" in data["iso"]
    assert data["human"]

@pytest.mark.network
@pytest.mark.asyncio
async def test_currency_convert(session):
    res = await session.call_tool(
        "currency_convert", {"amount": 100, "from_currency": "usd", "to_currency": "eur"}
    )
    data = _result(res)
    print("currency_convert:", data)
    assert data["from"] == "USD"
    assert data["to"] == "EUR"
    assert data["amount"] == 100
    assert data["source"] == "frankfurter.dev"
    assert data["converted"] > 0
    assert data["rate"] > 0

@pytest.mark.asyncio
async def test_read_file(session):
    _clean_sandbox()
    (SANDBOX / "hello.txt").write_text("hello world", encoding="utf-8")
    res = await session.call_tool("read_file", {"path": "hello.txt"})
    data = _result(res)
    print("read_file:", data)
    assert data["content"] == "hello world"
    assert data["encoding"] == "utf-8"
    assert data["size_bytes"] == 11
    assert data["path"] == "hello.txt"

@pytest.mark.asyncio
async def test_list_dir(session):
    _clean_sandbox()
    (SANDBOX / "a.txt").write_text("a", encoding="utf-8")
    (SANDBOX / "sub").mkdir()
    res = await session.call_tool("list_dir", {"path": "."})
    data = _result(res)
    print("list_dir:", data)
    assert isinstance(data, list)
    names = {e["name"]: e for e in data}
    assert names["a.txt"]["type"] == "file"
    assert names["a.txt"]["size_bytes"] == 1
    assert names["sub"]["type"] == "dir"
    assert names["sub"]["size_bytes"] == 0

@pytest.mark.asyncio
async def test_create_file(session):
    _clean_sandbox()
    res = await session.call_tool("create_file", {"path": "new.txt", "content": "fresh"})
    data = _result(res)
    print("create_file:", data)
    assert data["ok"] is True
    assert data["size_bytes"] == 5
    assert (SANDBOX / "new.txt").read_text(encoding="utf-8") == "fresh"

    dup = await session.call_tool("create_file", {"path": "new.txt", "content": "x"})
    assert dup.isError, "second create on same path must error"
    print("create_file dup error:", dup.content[0].text if dup.content else "")

@pytest.mark.asyncio
async def test_update_file(session):
    _clean_sandbox()
    (SANDBOX / "u.txt").write_text("old", encoding="utf-8")
    res = await session.call_tool("update_file", {"path": "u.txt", "content": "brand new body"})
    data = _result(res)
    print("update_file:", data)
    assert data["ok"] is True
    assert (SANDBOX / "u.txt").read_text(encoding="utf-8") == "brand new body"
    assert data["size_bytes"] == len("brand new body")

    missing = await session.call_tool("update_file", {"path": "nope.txt", "content": "x"})
    assert missing.isError
    print("update_file missing error:", missing.content[0].text if missing.content else "")

@pytest.mark.asyncio
async def test_edit_file(session):
    _clean_sandbox()
    (SANDBOX / "e.txt").write_text("foo bar foo", encoding="utf-8")

    multi = await session.call_tool(
        "edit_file", {"path": "e.txt", "find": "foo", "replace": "FOO"}
    )
    assert multi.isError, "ambiguous find without replace_all must error"
    print("edit_file ambiguous error:", multi.content[0].text if multi.content else "")

    res_all = await session.call_tool(
        "edit_file",
        {"path": "e.txt", "find": "foo", "replace": "FOO", "replace_all": True},
    )
    data = _result(res_all)
    print("edit_file replace_all:", data)
    assert data["replacements"] == 2
    assert (SANDBOX / "e.txt").read_text(encoding="utf-8") == "FOO bar FOO"

    res_single = await session.call_tool(
        "edit_file", {"path": "e.txt", "find": "bar", "replace": "BAZ"}
    )
    data = _result(res_single)
    print("edit_file single:", data)
    assert data["replacements"] == 1
    assert (SANDBOX / "e.txt").read_text(encoding="utf-8") == "FOO BAZ FOO"

    missing = await session.call_tool(
        "edit_file", {"path": "e.txt", "find": "zzz", "replace": "x"}
    )
    assert missing.isError
    print("edit_file not-found error:", missing.content[0].text if missing.content else "")

@pytest.mark.asyncio
async def test_sandbox_escape(session):
    res = await session.call_tool("read_file", {"path": "../foo"})
    assert res.isError, "sandbox escape must be rejected"
    msg = res.content[0].text if res.content else ""
    print("sandbox_escape error:", msg)
    assert "escape" in msg.lower() or "sandbox" in msg.lower()
