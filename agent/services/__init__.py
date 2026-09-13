"""Capabilities a run *calls*.

  gateway.py       bridge to the LLM gateway; also resolves the environment
  memory.py        the typed memory service
  vector_index.py  FAISS index behind memory's vector reads
  artifacts.py     content-addressable blob store
  sandbox.py       subprocess Python runner for the sandbox_executor skill
  mcp_runner.py    the tool-use loop over the MCP server
"""
