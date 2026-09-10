"""computer — Computer-Use skill.

Five-layer cascade over cua-driver:
    Layer 1   ax_extract    AX tree read, no LLM
    Layer 2a  deterministic hotkeys / element_index, no LLM
    Layer 2b  ax_llm        AX tree markdown → V9 /v1/chat
    Layer 2c  electron      CDP page tool → V9 /v1/chat
    Layer 3   vision        SoM + raw screenshot → V9 /v1/vision

Public API::
    from computer import ComputerSkill, CuaClient, WindowRef
    from computer import ComputerDriver, DriverConfig, DriverResult

Daemon start command:
    open -n -g -a CuaDriver --args serve
"""
from .client import CuaClient, CuaError, WindowRef
from .driver import ComputerDriver, DriverConfig, DriverResult, StepRecord
from .skill import ComputerSkill
from .trajectory import list_trajectories

__all__ = [
    "CuaClient",
    "CuaError",
    "WindowRef",
    "ComputerDriver",
    "DriverConfig",
    "DriverResult",
    "StepRecord",
    "ComputerSkill",
    "list_trajectories",
]
