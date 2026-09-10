The Browser skill fetches and interacts with web pages. It walks a
four-layer cascade starting from the cheapest path (HTML extraction)
and escalating only when needed (deterministic selectors, accessibility
tree, then visual set-of-marks with a vision model). The escalation
is internal; you pass `url` and `goal`, the skill chooses the layer.

Inputs: `metadata.url` (required), `metadata.goal` (required, free-text
description of what to extract or do). Output: `BrowserOutput` with
`content` (for extraction goals) or `actions` plus `final_url` (for
interaction goals), and `path` reporting the cascade layer that
actually ran. When the page is gated by CAPTCHA or login, the skill
returns `error_code="gateway_blocked"` and no content; the Planner
should route around by trying a different source URL or by handing
back to the user.
