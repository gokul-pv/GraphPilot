#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
exec uv run python main.py
