"""§8 evidence-gathering: find a target where the unforced cascade naturally
escalates to Layer 3 (vision).

Constraints:
  - No force_path. Cascade runs as shipped.
  - 60 s wall-clock budget per candidate.
  - Stop at the first natural Layer 3 success; report the rest as 'yielded
    to Layer 2b' (or whichever path won) with a one-line reason.
  - At most five candidates before declaring "Layer 3 is a narrower band
    than the field assumed".

Each candidate runs through BrowserSkill directly (not flow.py) for
exploration speed; the *successful* one will then be re-run via flow.py
for the canonical session+ledger record §8 cites.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from browser.skill import BrowserSkill
from schemas import NodeSpec


# (label, url, goal). Goals are deliberately phrased to demand interaction;
# they avoid the verbs that would short-circuit to Layer 1 extraction.
CANDIDATES = [
    ("tldraw",
     "https://www.tldraw.com/",
     "Select the ellipse tool from the toolbar, then draw a circle in the "
     "lower-right quadrant of the canvas (use a drag from roughly "
     "x=900, y=600 to x=1100, y=750)."),
    ("photopea",
     "https://www.photopea.com/",
     "Open the File menu and create a new 800x600 document. When the new "
     "blank canvas is visible, call done(success=true)."),
    ("piskel",
     "https://www.piskelapp.com/p/create/sprite",
     "Draw a single black pixel near the centre of the pixel canvas by "
     "clicking on it. When a black pixel is visible, call done(success=true)."),
    ("openprocessing",
     "https://openprocessing.org/sketch/2374527",
     "Click anywhere inside the sketch's canvas area to interact with the "
     "running sketch. Confirm the canvas has been clicked."),
    ("polymer_shadow",
     "https://web.dev/articles/shadowdom-v1",   # contains shadow-DOM custom elements in the doc
     "Find and click the table-of-contents link for the section titled "
     "'Element details', so that the page scrolls to that anchor."),
]

OUT = HERE.parent / "out" / "natural_vision_search"
TRACE = OUT / "trace.json"


async def try_candidate(label: str, url: str, goal: str) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    sess = f"s9_natural_vision_{label}_{int(time.time())}"
    sk = BrowserSkill(
        agent_tag=f"natural_vision_{label}",
        artifacts_root=str(OUT / label),
        session=sess,
    )
    t0 = time.time()
    try:
        result = await asyncio.wait_for(
            sk.run(NodeSpec(skill="browser", inputs=[],
                            metadata={"url": url, "goal": goal})),
            timeout=60.0,
        )
        out = result.output
        return {
            "label": label, "url": url, "goal": goal, "session": sess,
            "success": result.success,
            "path": out.get("path"),
            "turns": out.get("turns"),
            "error_code": result.error_code,
            "error": (result.error or "")[:200],
            "elapsed_s": round(time.time() - t0, 1),
            "final_url": out.get("final_url"),
        }
    except asyncio.TimeoutError:
        return {
            "label": label, "url": url, "goal": goal, "session": sess,
            "success": False, "path": "(timeout)", "turns": None,
            "error_code": "timeout",
            "error": f"abandoned after {round(time.time() - t0, 1)}s",
            "elapsed_s": 60.0,
            "final_url": None,
        }
    except Exception as e:                             # noqa: BLE001
        return {
            "label": label, "url": url, "goal": goal, "session": sess,
            "success": False, "path": "(error)", "turns": None,
            "error_code": "exception",
            "error": f"{type(e).__name__}: {e}"[:200],
            "elapsed_s": round(time.time() - t0, 1),
            "final_url": None,
        }


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    results = []
    print("\n=== natural Layer 3 search ===\n")
    for label, url, goal in CANDIDATES:
        print(f"→ trying {label}: {url}")
        r = await try_candidate(label, url, goal)
        results.append(r)
        print(f"   path={r['path']:>10}  turns={r['turns']}  "
              f"success={r['success']}  elapsed={r['elapsed_s']}s")
        if r.get('error'):
            print(f"   note   : {r['error'][:140]}")
        # Stop at first natural vision success.
        if r["success"] and r["path"] == "vision":
            print(f"\n[FOUND] natural Layer 3 escalation on {label!r}")
            break
    TRACE.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nfull trace → {TRACE}")
    print(f"\n=== summary ===")
    for r in results:
        mark = "✓" if (r["success"] and r["path"] == "vision") else " "
        print(f"  {mark} {r['label']:<18}  path={r['path']:>10}  "
              f"turns={r['turns']}  elapsed={r['elapsed_s']}s")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
