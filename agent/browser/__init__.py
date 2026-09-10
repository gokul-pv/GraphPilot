"""S9 Browser skill — set-of-marks (Layer 3) driver.

Framework-free: Playwright + Pillow + httpx → llm_gatewayV9 /v1/vision.
No LangChain, no browser-use dependency. See ../README.md for the layered
cascade (extract / a11y / vision) this slots into.
"""
from .client import GatewayResult, V9Client, V9VisionClient, VisionResult
from .dom import Element, PageSnapshot, enumerate_interactives
from .driver import (
    ACTION_SCHEMA,
    A11yDriver,
    BaseDriver,
    DriverConfig,
    DriverResult,
    SetOfMarksDriver,
    StepRecord,
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_A11Y,
    SYSTEM_PROMPT_VISION,
)
from .highlight import annotate, to_data_url

__all__ = [
    "A11yDriver",
    "ACTION_SCHEMA",
    "BaseDriver",
    "DriverConfig",
    "DriverResult",
    "Element",
    "GatewayResult",
    "PageSnapshot",
    "SetOfMarksDriver",
    "StepRecord",
    "SYSTEM_PROMPT",
    "SYSTEM_PROMPT_A11Y",
    "SYSTEM_PROMPT_VISION",
    "V9Client",
    "V9VisionClient",
    "VisionResult",
    "annotate",
    "enumerate_interactives",
    "to_data_url",
]
