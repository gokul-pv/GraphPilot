"""DOM enumeration for the set-of-marks driver.

Single page-side JavaScript pass via Playwright's `page.evaluate()` returns
all visible interactive elements with their CSS-pixel bounding rectangles.
Each element gets a stable per-DOM id derived from its position in the
DOMNodeId space so that the same element keeps the same id across turns
as long as the DOM hasn't reflowed beneath it.

Why not CDP DOMSnapshot? browser-use uses it for full-DOM fidelity and
shadow-DOM coverage. For 90% of pages the predicate set below (tag list +
ARIA role + cursor:pointer + onclick) catches the same elements with far
less code. We can upgrade to CDP DOMSnapshot when a target genuinely
requires it.

This module returns `Element` records that `highlight.py` paints onto a
screenshot and `driver.py` references by `id` to dispatch actions.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

from playwright.async_api import Page


# Inline JS — written once, evaluated on every turn. Returns a list of
# {id, tag, role, name, x, y, w, h, dpr} dicts in CSS pixels.
#
# `id` is the 1-based ordinal of the element among visible interactives —
# regenerated each turn. The legend uses these ids and the action dispatcher
# looks them up by ordinal against the freshly-captured list, so turn-to-
# turn DOM reflow is handled honestly.
_ENUMERATE_JS = r"""
() => {
  const SELS = [
    'a[href]', 'button', 'input:not([type=hidden])', 'textarea', 'select',
    '[role=button]', '[role=link]', '[role=tab]', '[role=menuitem]',
    '[role=textbox]', '[role=searchbox]', '[role=combobox]', '[role=switch]',
    '[role=checkbox]', '[role=radio]', '[contenteditable=true]',
    '[contenteditable=""]', '[onclick]', 'label[for]', 'summary',
  ].join(', ');
  // SVG primitives that inherit cursor:pointer from a parent — we never
  // want to number these directly; the wrapping <button>/<a> is what the
  // model should click.
  const SVG_PRIMITIVE = new Set([
    'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon',
    'g', 'use', 'symbol', 'defs', 'mask', 'pattern', 'svg', 'tspan', 'text',
  ]);

  const candidates = Array.from(document.querySelectorAll(SELS));
  // Cursor sweep — but exclude SVG primitives, container-y elements, and
  // anything already covered.
  const candSet = new Set(candidates);
  for (const el of document.querySelectorAll('*')) {
    if (candSet.has(el)) continue;
    if (SVG_PRIMITIVE.has(el.tagName.toLowerCase())) continue;
    if (el.children.length > 0) continue;
    const cs = getComputedStyle(el);
    if (cs.cursor === 'pointer') { candidates.push(el); candSet.add(el); }
  }

  // Dedupe by outermost-wins: drop any candidate whose ancestor is also a
  // candidate. browser-use does the same — picking the inner icon never
  // helps and almost always hurts.
  const set = new Set(candidates);
  const outermost = [];
  for (const el of candidates) {
    let p = el.parentElement;
    let inner = false;
    while (p) {
      if (set.has(p)) { inner = true; break; }
      p = p.parentElement;
    }
    if (!inner) outermost.push(el);
  }

  const out = [];
  let id = 0;
  for (const el of outermost) {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    if (r.bottom < 0 || r.right < 0) continue;
    if (r.top > window.innerHeight || r.left > window.innerWidth) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') continue;
    // For icon-only buttons, surface every label-ish attribute we can find,
    // including children's aria-label and the immediate label-sibling.
    let name = (
      el.getAttribute('aria-label') ||
      el.getAttribute('aria-labelledby-text') ||  // populated below for label refs
      ''
    );
    if (!name) {
      const lblId = el.getAttribute('aria-labelledby');
      if (lblId) {
        const ref = document.getElementById(lblId);
        if (ref) name = ref.innerText || '';
      }
    }
    if (!name) {
      name = (el.innerText || el.value ||
              el.getAttribute('placeholder') ||
              el.getAttribute('title') ||
              el.getAttribute('alt') ||
              el.getAttribute('data-tooltip') ||
              el.getAttribute('data-testid') ||
              el.getAttribute('name') || '');
    }
    if (!name) {
      // Last resort: descendant aria-label / title.
      const inner = el.querySelector('[aria-label], [title]');
      if (inner) name = inner.getAttribute('aria-label') || inner.getAttribute('title') || '';
    }
    name = name.trim().replace(/\s+/g, ' ').slice(0, 80);
    out.push({
      id: ++id,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      name: name,
      x: Math.round(r.x),
      y: Math.round(r.y),
      w: Math.round(r.width),
      h: Math.round(r.height),
    });
  }
  return { elements: out, dpr: window.devicePixelRatio || 1,
           viewport: { w: window.innerWidth, h: window.innerHeight } };
}
"""


@dataclass
class Element:
    id: int
    tag: str
    role: str
    name: str
    x: int      # CSS pixels, top-left of bbox
    y: int
    w: int
    h: int

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2

    def legend_line(self) -> str:
        """browser-use-style: `[id]<tag role="role">name</tag>`"""
        role = f' role="{self.role}"' if self.role else ""
        name = self.name or ""
        return f"[{self.id}]<{self.tag}{role}>{name}</{self.tag}>"


@dataclass
class PageSnapshot:
    elements: list[Element]
    dpr: float
    viewport_w: int
    viewport_h: int

    def by_id(self, mark: int) -> Element | None:
        for e in self.elements:
            if e.id == mark:
                return e
        return None

    def legend(self, max_chars: int = 40_000) -> str:
        lines = [e.legend_line() for e in self.elements]
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n[truncated]"
        return text


async def enumerate_interactives(page: Page) -> PageSnapshot:
    raw = await page.evaluate(_ENUMERATE_JS)
    els = [Element(**e) for e in raw["elements"]]
    return PageSnapshot(
        elements=els,
        dpr=float(raw["dpr"]),
        viewport_w=int(raw["viewport"]["w"]),
        viewport_h=int(raw["viewport"]["h"]),
    )
