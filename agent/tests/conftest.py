"""Put `agent/` on sys.path for every test in this directory.

The agent's modules import each other by top-level name (`from core.schemas
import ...`, `from services import memory`), which resolves only when `agent/`
itself is importable. Running pytest from `agent/` already satisfies that, but
pytest can also be invoked from the repository root, and each test file used to
carry its own copy of this line. One place is enough.
"""

import sys
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))
