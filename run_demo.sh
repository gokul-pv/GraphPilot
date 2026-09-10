#!/usr/bin/env bash
# Session 9 demo runner.
#
# Run the unit-test suite, then walk through a curated set of queries
# that each exercise one orchestrator feature. Every query's stdout is
# teed into S9/logs/<slug>.log so students can re-read it after the
# live demo. After each run, the script prints the session id and a
# one-liner showing how to inspect a node's exact rendered prompt.
#
# Usage:
#   ./run_demo.sh              run pytest + the 5 canonical queries
#   ./run_demo.sh tests        only pytest
#   ./run_demo.sh hello        smallest DAG (planner → formatter)
#   ./run_demo.sh shannon      single-item query (USER_QUERY flow)
#   ./run_demo.sh populations  parallel fan-out (per-worker scoping)
#   ./run_demo.sh structured   forces distiller → auto-critic chain
#   ./run_demo.sh fail         graceful-fail-by-planning
#   ./run_demo.sh browser      Session 9 Browser skill end-to-end
#   ./run_demo.sh wipe         clear state/sessions + logs
#
# Requires the V9 gateway running on :8109 (start with
#   cd ../llm_gatewayV9 && uv run main.py
# ).

set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CODE_DIR="$SCRIPT_DIR/agent"
LOG_DIR="$SCRIPT_DIR/logs"

mkdir -p "$LOG_DIR"

usage() {
  sed -n '2,22p' "$0"
}

query_for() {
  case "$1" in
    hello)        echo "hello" ;;
    shannon)      echo "When was Claude Shannon born and when did he die? Name three of his contributions to information theory." ;;
    populations)  echo "Find the populations of London, Paris, Berlin and tell me which two are closest in size." ;;
    structured)   echo "Compare the populations of Mumbai, Cairo, and Lagos and identify which is growing fastest. Return structured fields per city." ;;
    fail)         echo "Summarise the contents of /nonexistent/path.txt for me." ;;
    browser)      echo "What are the top 3 most-liked open-source LLM releases on Hugging Face from the past week? For each give model name, parameter count, and one-line description." ;;
    *) return 1 ;;
  esac
}

describe() {
  case "$1" in
    hello)
      cat <<'EOF'
DEMO: hello
  Shape         planner -> formatter
  Demonstrates  Smallest possible DAG. The planner correctly decides
                no research is needed and routes straight to the
                formatter. Inspect n_001.json to see the planner's
                emitted plan; n_002.json shows the formatter's prompt
                contains USER_QUERY because the planner listed it.
EOF
      ;;
    shannon)
      cat <<'EOF'
DEMO: shannon
  Shape         planner -> researcher -> formatter
  Demonstrates  Single-item query. Researcher receives USER_QUERY in
                its inputs because there is nothing to fan out over.
                The researcher's prompt_sent contains USER_QUERY but
                no QUESTION block (the planner did not need to scope
                the worker).
EOF
      ;;
    populations)
      cat <<'EOF'
DEMO: populations
  Shape         planner -> researcher x 3 (parallel) -> formatter
  Demonstrates  Per-worker scoping via metadata.question. Each
                researcher sees only its own city (in QUESTION:), NOT
                the full USER_QUERY. Inspect each researcher node:
                  - inputs    -> []
                  - QUESTION  -> "current population of <one city>"
                If you saw all three cities in INPUTS that would be
                the pre-patch leak that S8/S9 used to have.
EOF
      ;;
    structured)
      cat <<'EOF'
DEMO: structured
  Shape         planner -> researcher x N -> distiller -> CRITIC -> formatter
  Demonstrates  Session 9 critic auto-insertion fix. The planner
                pre-wires distiller -> formatter; the orchestrator
                detects distiller is critic:true and splices a critic
                in automatically. The auto-inserted critic gets
                USER_QUERY in its inputs so it judges against the
                real ask (not stale memory hits). Inspect the critic
                node: prompt_sent must contain USER_QUERY and the
                distiller's output; verdict + rationale appear in
                the result.output.
EOF
      ;;
    fail)
      cat <<'EOF'
DEMO: fail
  Shape         planner -> formatter (graceful-fail-by-planning)
  Demonstrates  The planner recognises a doomed input upfront (a
                nonexistent file path) and routes directly to the
                formatter with an honest "could not be done" answer
                instead of scheduling a retriever guaranteed to
                fail. Total cost: two LLM calls. No recovery branch
                fires because nothing failed at runtime.
EOF
      ;;
    browser)
      cat <<'EOF'
DEMO: browser
  Shape         planner -> browser -> distiller? -> formatter
  Demonstrates  The Session 9 Browser skill. Routes through the
                four-layer cascade (extract -> deterministic -> a11y
                -> vision). Per-turn artifacts (marked screenshots,
                legends) land in state/sessions/<sid>/browser/.
                Requires Playwright + V9 vision endpoint.
EOF
      ;;
  esac
}

precheck() {
  if ! curl -sf http://localhost:8109/v1/routers >/dev/null; then
    echo "[demo] V9 gateway is not responding at http://localhost:8109" >&2
    echo "       start it:  cd $SCRIPT_DIR/../llm_gatewayV9 && uv run main.py" >&2
    exit 1
  fi
}

run_pytest() {
  echo
  echo "===================================================================="
  echo "  Unit tests"
  echo "===================================================================="
  echo "  recovery (22) + recovery-amnesia (3) + critic-autoinsert (4) = 29"
  echo "===================================================================="
  ( cd "$CODE_DIR" && uv run --quiet pytest tests/ -v --no-header )
}

run_one() {
  local id="$1"
  local q log sid
  q=$(query_for "$id") || { echo "[demo] unknown query: $id" >&2; usage; exit 2; }

  echo
  echo "===================================================================="
  describe "$id"
  echo "===================================================================="
  log="$LOG_DIR/$id.log"
  ( cd "$CODE_DIR" && uv run python flow.py "$q" 2>&1 ) | tee "$log"

  sid=$(ls -t "$CODE_DIR/state/sessions" 2>/dev/null | head -1)
  if [[ -n "$sid" ]]; then
    echo
    echo "[demo] log     -> $log"
    echo "[demo] session -> $CODE_DIR/state/sessions/$sid/"
    echo "[demo] To see a node's exact rendered prompt:"
    echo "       python3 -c \"import json; print(json.load(open('$CODE_DIR/state/sessions/$sid/nodes/n_001.json'))['prompt_sent'])\""
  fi
}

case "${1:-all}" in
  -h|--help|help) usage; exit 0 ;;
  tests)          precheck; run_pytest ;;
  wipe)
    # Full reset so the next demo starts from a cold cache:
    #   - per-session graph + node JSON dumps
    #   - FAISS vector index and its id map (otherwise old session memory
    #     leaks into every fresh prompt's MEMORY HITS block and the
    #     planner makes weird choices because of stale facts)
    #   - the memory.json blob the FAISS index points at
    #   - artifact bytes
    #   - the demo log directory
    rm -rf \
      "$CODE_DIR/state/sessions" \
      "$CODE_DIR/state/artifacts" \
      "$CODE_DIR/state/index.faiss" \
      "$CODE_DIR/state/index_ids.json" \
      "$CODE_DIR/state/memory.json" \
      "$LOG_DIR"
    mkdir -p "$LOG_DIR"
    echo "[demo] cleared: state/sessions, state/artifacts, FAISS index,"
    echo "                state/memory.json, logs/"
    ;;
  hello|shannon|populations|structured|fail|browser)
    precheck; run_one "$1"
    ;;
  all)
    precheck
    run_pytest
    for id in hello shannon populations structured fail; do
      run_one "$id"
    done
    echo
    echo "[demo] Done. To also exercise the Browser skill: ./run_demo.sh browser"
    ;;
  *)
    echo "[demo] unknown command: $1" >&2
    usage
    exit 2
    ;;
esac
