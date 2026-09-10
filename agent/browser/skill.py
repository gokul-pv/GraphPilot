"""Session 9: the Browser skill — cascade wrapper around the layered drivers.

The wrapper translates the orchestrator's NodeSpec contract into the
typed BrowserOutput / AgentResult contract, and owns the layer cascade:

    Layer 1  — HTML extract via trafilatura (no LLM)
    Layer 2a — deterministic selectors (only if metadata.selectors is given)
    Layer 2b — A11yDriver        (text-only, V9 /v1/chat)
    Layer 3  — SetOfMarksDriver  (vision, V9 /v1/vision)

Escalation rule: a layer escalates when its output is empty or evidently
insufficient. The skill stops at the first layer that produces a useful
answer.

Gateway-access is a first-class failure: if Layer 1's fetch returns a
known CAPTCHA / login-wall / hCaptcha marker, the skill returns
immediately with error_code="gateway_blocked" and does not attempt
the later layers. The orchestrator's recovery path picks this up via
the failure_report (it contains the literal token "gateway_blocked")
and re-invokes the Planner.

This file is the ONLY new code in the integration. The four files
already on disk (client.py, dom.py, highlight.py, driver.py) are
ported verbatim from S9SharedCode/code/browser/ and untouched.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import trafilatura
from playwright.async_api import async_playwright

from schemas import AgentResult, BrowserOutput, NodeSpec

from .client import V9Client
from .driver import A11yDriver, DriverConfig, DriverResult, SetOfMarksDriver


# ── gateway-block detection ──────────────────────────────────────────────────
# Kept here (next to the cascade that uses it) rather than mutating the
# ported dom.py.  Short, obvious patterns — when this list grows past a
# screenful we should consolidate, but for now explicit is better.
_GATEWAY_BLOCK_MARKERS = (
    # Generic CAPTCHA / hCaptcha / reCAPTCHA. Needles MUST be specific
    # enough that an article ABOUT captchas does not false-positive — we
    # match on class/attribute strings the widgets emit, not their names.
    ("captcha",                "Let's confirm you are human"),
    ("captcha",                "Enter the characters you see below"),
    ("captcha",                "Robot Check"),
    ("captcha",                "Please verify you are a human"),
    ("captcha",                "/errors/validateCaptcha"),
    ("hcaptcha",               'class="h-captcha"'),
    ("hcaptcha",               "data-hcaptcha-widget-id"),
    ("recaptcha",              'class="g-recaptcha"'),
    ("recaptcha",              "g-recaptcha-response"),
    # Cloudflare interstitials.
    ("cloudflare",             "Checking your browser before accessing"),
    ("cloudflare",             "cf-browser-verification"),
    ("cloudflare",             "cf-challenge-running"),
    # Login walls.  Conservative — only the literal sign-in-required pages.
    ("login_wall",             "You must be logged in"),
    ("login_wall",             "Sign in to continue"),
    ("login_wall",             "Please log in to continue"),
)


def detect_gateway_block(html: str) -> str | None:
    """Return the block type when `html` looks like a gateway-access page
    (CAPTCHA / Cloudflare / login wall), else None. Conservative — false
    positives would mis-route real content to recovery."""
    if not html:
        return None
    h = html.lower()
    for kind, needle in _GATEWAY_BLOCK_MARKERS:
        if needle.lower() in h:
            return kind
    return None


# ── Layer 1: pure-HTTP extraction ────────────────────────────────────────────
_UA = (
    "Mozilla/5.0 (compatible; S9-Browser-Skill/0.1; +llm_gatewayV9)"
)


async def _fetch_html(url: str, timeout: float = 30.0) -> tuple[str, str]:
    """Returns (html, final_url)."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                 headers={"User-Agent": _UA}) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.text, str(r.url)


def _extract(html: str) -> str:
    text = trafilatura.extract(
        html, include_links=True, include_formatting=False, favor_recall=True,
    )
    return (text or "").strip()


def _is_useful_extract(content: str, goal: str) -> bool:
    """Coarse usefulness check. We trust the gateway/recovery to catch
    genuine no-content failures; this gate only filters obvious nothing
    (< ~200 chars) or the case where the page rendered but the goal asks
    for an interaction (`click`, `fill`, `select`, etc.) — for those goals
    extraction is never sufficient regardless of content length."""
    if len(content) < 200:
        return False
    interactive_verbs = ("click", "fill", "select", "type", "drag",
                         "filter", "sort", "submit", "navigate")
    if any(v in goal.lower() for v in interactive_verbs):
        return False
    return True


# ── the skill ────────────────────────────────────────────────────────────────
class BrowserSkill:
    NAME = "browser"

    def __init__(self, *, gateway_url: str = "http://localhost:8109",
                 agent_tag: str = "browser",
                 a11y_provider_pin: str | None = "gemini",
                 vision_provider_pin: str | None = None,
                 artifacts_root: str | None = None,
                 max_steps_a11y: int = 12,
                 max_steps_vision: int = 12,
                 wall_clock_s: float = 90.0,
                 session: str | None = None):
        self.gateway_url = gateway_url
        self.agent_tag = agent_tag
        self.a11y_provider_pin = a11y_provider_pin
        self.vision_provider_pin = vision_provider_pin
        self.artifacts_root = Path(artifacts_root) if artifacts_root else None
        self.max_steps_a11y = max_steps_a11y
        self.max_steps_vision = max_steps_vision
        self.wall_clock_s = wall_clock_s
        # Forwarded to V9 so the gateway ledger can attribute each call to
        # the orchestrator session that drove it.
        self.session = session

    # ── public entry point ─────────────────────────────────────────────────
    async def run(self, node: NodeSpec) -> AgentResult:
        url = node.metadata.get("url") or (node.inputs[0] if node.inputs else "")
        goal = node.metadata.get("goal") or "extract main content"
        # Optional escape hatch: skip the natural cascade and pin to a specific
        # layer. Used by the spec's vision-escalation smoke test when the
        # natural cascade would short-circuit on a richer earlier layer.
        # Values: 'extract' | 'a11y' | 'vision'. Anything else is ignored.
        force_path = node.metadata.get("force_path")
        if not url:
            return self._pack_error("", goal, "interaction_failed",
                                    "no url given (metadata.url or inputs[0])")
        t0 = time.time()
        client = V9Client(base_url=self.gateway_url, agent=self.agent_tag,
                          session=self.session)
        artifacts_dir = (
            str(self.artifacts_root / f"browser_{int(t0)}")
            if self.artifacts_root else None
        )

        # ── Layer 1: extract ────────────────────────────────────────────────
        # When the bare GET fails (403/405 from a WAF, connection refused, …)
        # we do NOT bail out: anti-bot edges often serve a CAPTCHA *page* to
        # a real browser session that they refuse via bare GET. Falling
        # through to the Playwright layers lets _drive() detect the rendered
        # CAPTCHA and surface gateway_blocked properly.
        layer1_http_error: str | None = None
        try:
            html, final_url = await _fetch_html(url)
        except httpx.HTTPError as e:
            layer1_http_error = f"layer1 fetch failed: {e}"
            html, final_url = "", url

        if html:
            block = detect_gateway_block(html)
            if block:
                return self._pack_error(url, goal, "gateway_blocked",
                                        f"gateway_blocked: {block} marker on {final_url}",
                                        elapsed=time.time() - t0)
            content = _extract(html)
            if _is_useful_extract(content, goal):
                return self._pack(url, goal, "extract", turns=0,
                                  content=content, final_url=final_url,
                                  elapsed=time.time() - t0)

        # ── Layer 2a: deterministic selectors (only if caller gave any) ────
        # Tightly scoped: this branch only fires when the Planner / caller
        # supplies `metadata.selectors`. The convention is metadata.selectors
        # = list of {action: "click"|"fill", selector, value?}. When no
        # selectors are given we go straight to a11y. We never try to *guess*
        # selectors here — that would re-create the brittleness Layer 2b
        # exists to solve.
        selectors = node.metadata.get("selectors") or []
        if selectors:
            det = await self._try_deterministic(url, goal, selectors)
            if det is not None:
                return det if det.success else self._pack_error(
                    url, goal, "interaction_failed",
                    det.error or "deterministic path failed",
                    elapsed=time.time() - t0,
                )

        # ── Layer 2b: a11y ──────────────────────────────────────────────────
        if force_path == "vision":
            # Skip a11y entirely — caller wants Layer 3 explicitly.
            a11y_result = DriverResult(success=False, note="skipped by force_path=vision")
        else:
            a11y_result = await self._drive(
                A11yDriver, url, goal, client, artifacts_dir,
                self.a11y_provider_pin, self.max_steps_a11y,
            )
        if getattr(a11y_result, "gateway_blocked", False):
            return self._pack_error(url, goal, "gateway_blocked",
                                    a11y_result.note or "gateway_blocked after JS render",
                                    elapsed=time.time() - t0)
        if a11y_result.success:
            return self._pack_driver("a11y", url, goal, a11y_result,
                                     final_url=a11y_result.final_url,
                                     elapsed=time.time() - t0)

        # ── Layer 3: vision ─────────────────────────────────────────────────
        vis_result = await self._drive(
            SetOfMarksDriver, url, goal, client, artifacts_dir,
            self.vision_provider_pin, self.max_steps_vision,
        )
        if getattr(vis_result, "gateway_blocked", False):
            return self._pack_error(url, goal, "gateway_blocked",
                                    vis_result.note or "gateway_blocked after JS render",
                                    elapsed=time.time() - t0)
        if vis_result.success:
            return self._pack_driver("vision", url, goal, vis_result,
                                     final_url=vis_result.final_url,
                                     elapsed=time.time() - t0)

        last_err = (vis_result.note or a11y_result.note
                    or layer1_http_error or "all layers exhausted")
        return self._pack_error(url, goal, "interaction_failed",
                                f"all layers exhausted; last: {last_err}",
                                elapsed=time.time() - t0)

    # ── per-layer driver runs ──────────────────────────────────────────────
    async def _drive(self, DriverCls, url, goal, client, artifacts_dir,
                     provider_pin, max_steps):
        # Place each layer's per-turn artifacts under its own subdir so
        # turn_##_* filenames from one layer don't overwrite another's.
        if artifacts_dir:
            from pathlib import Path as _P
            sub = _P(artifacts_dir) / DriverCls.LAYER_NAME
            sub.mkdir(parents=True, exist_ok=True)
            artifacts_dir = str(sub)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(
                viewport={"width": 1366, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                locale="en-US",
            )
            await ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',"
                "{get:()=>undefined});"
            )
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                # Last-chance gateway-block check on the rendered page (some
                # walls only show up after JS executes).
                kind = detect_gateway_block(await page.content())
                if kind:
                    await browser.close()
                    out = DriverResult(
                        success=False,
                        note=f"gateway_blocked ({kind}) detected after JS render at {page.url}",
                    )
                    # Annotation BrowserSkill reads to propagate the code.
                    out.gateway_blocked = True
                    return out
                await asyncio.sleep(1.0)
                cfg = DriverConfig(
                    goal=goal, max_steps=max_steps, max_failures=3,
                    artifacts_dir=artifacts_dir, provider=provider_pin,
                )
                drv = DriverCls(page, client, cfg)
                # Augment the result with final_url + extracted text so
                # _pack_driver can fill BrowserOutput uniformly.
                result = await drv.run()
                result.final_url = page.url
                result.extracted = ""
                try:
                    result.extracted = _extract(await page.content())
                except Exception:                          # noqa: BLE001
                    pass
                result.turns = len(drv.steps)
                result.actions = [
                    {"turn": s.turn, "actions": s.actions, "outcome": s.outcome}
                    for s in drv.steps
                ]
                return result
            finally:
                await browser.close()

    async def _try_deterministic(self, url, goal, selectors) -> AgentResult | None:
        """Runs caller-supplied selector instructions through Playwright. Each
        step is `{action, selector, value?}`. Returns AgentResult on success
        or None to let the cascade fall through to a11y."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(
                viewport={"width": 1366, "height": 900},
            )
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                for i, step in enumerate(selectors, start=1):
                    sel = step.get("selector")
                    if not sel:
                        await browser.close()
                        return None
                    loc = page.locator(sel).first
                    try:
                        await loc.wait_for(state="visible", timeout=8000)
                    except Exception:                          # noqa: BLE001
                        await browser.close()
                        return None
                    if step.get("action") == "fill":
                        await loc.fill(step.get("value", ""))
                    elif step.get("action") == "click":
                        await loc.click()
                    elif step.get("action") == "key":
                        await page.keyboard.press(step.get("value", "Enter"))
                content = _extract(await page.content())
                final = page.url
                await browser.close()
                return self._pack(
                    url, goal, "deterministic", turns=len(selectors),
                    content=content, final_url=final, elapsed=0.0,
                )
            except Exception:                          # noqa: BLE001
                await browser.close()
                return None

    # ── packers ────────────────────────────────────────────────────────────
    def _pack(self, url, goal, path, *, turns, content=None, actions=None,
              final_url=None, elapsed=0.0) -> AgentResult:
        out = BrowserOutput(
            url=url, goal=goal, path=path, turns=turns,
            content=content, actions=actions or [], final_url=final_url,
        )
        return AgentResult(
            success=True, agent_name=self.NAME,
            output=out.model_dump(), elapsed_s=elapsed,
        )

    def _pack_driver(self, path, url, goal, drv_result,
                     *, final_url, elapsed) -> AgentResult:
        out = BrowserOutput(
            url=url, goal=goal, path=path,
            turns=getattr(drv_result, "turns", 0) or 0,
            content=getattr(drv_result, "extracted", None) or None,
            actions=getattr(drv_result, "actions", []) or [],
            final_url=final_url,
        )
        return AgentResult(
            success=True, agent_name=self.NAME,
            output=out.model_dump(), elapsed_s=elapsed,
        )

    def _pack_error(self, url, goal, code, msg, *, elapsed=0.0) -> AgentResult:
        out = BrowserOutput(
            url=url or "", goal=goal, path="extract", turns=0, content=None,
        )
        return AgentResult(
            success=False, agent_name=self.NAME,
            output=out.model_dump(), error=msg, error_code=code,
            elapsed_s=elapsed,
        )
