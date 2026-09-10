# cua-driver: A Practical Guide for Agent Builders

[Repository overview](../README.md) · [Computer-use guide](computer-use.md)

A field guide for any agent that needs to drive a real desktop. Distilled from a hands-on session installing [trycua/cua](https://github.com/trycua/cua)'s `cua-driver` on macOS, scanning the Calculator UI, and computing 7 × 8 = 56 entirely through accessibility primitives — no LLM, no sandbox, no VM.

Audience: another agent (or another engineer) who needs to (a) understand what cua-driver does, (b) install and use it, (c) decide what to delegate to it versus what to build around it.

---

## 1. What cua-driver is, and what it is not

cua-driver is a single Rust binary (~30 MB universal macOS build; cross-platform Windows + Linux variants from the same source) that exposes the operating system's native accessibility and input APIs as a stable JSON tool surface. It runs on your real host. It is not a sandbox, not a VM, not a remote desktop, not a Python library.

A useful framing: cua-driver is to a computer-use agent what a chess engine's move-execution layer is to a chess agent. It does not decide what to do. It accepts a structured action and performs it against the operating system, and it answers structured perception queries about what is on screen. Everything above it — goal decomposition, planning, perception interpretation, error recovery — you build.

The wider `cua` project also publishes a Python SDK (`pip install cua`) that boots an actual macOS VM via Apple's Virtualization framework (`Sandbox.ephemeral(Image.macos())`). That path is heavyweight and isolates the agent from the host. The driver path documented here is the direct one: the agent operates on the same Mac (or PC) the user is using, with the user's apps, files, and credentials.

### What the OS exposes determines what cua-driver can do

| OS | API cua-driver speaks | Inspector tool to verify support |
|---|---|---|
| macOS | Accessibility (`AXUIElement`) + CoreGraphics events | Accessibility Inspector (Xcode utilities) |
| Windows | UI Automation (UIA) + `SendInput` | AccessibilityInsights for Windows |
| Linux | AT-SPI 2 over D-Bus + `XTestFakeButtonEvent` (X11) / equivalent on Wayland | Accerciser |

Rule of thumb: if the platform inspector can see a control, cua-driver can address it semantically. If not, you are reduced to pixel-coordinate clicks.

---

## 2. Installation

Sudo-free. Drops `CuaDriver.app` in `/Applications`, symlinks `cua-driver` into `~/.local/bin`, and appends a PATH line to your shell rc if needed.

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh)"
```

Defaults to the Rust backend, which is the cross-platform implementation. A Swift macOS-only backend exists (`--backend=swift`) but offers no agent-relevant advantage today; stay on Rust unless you have a specific reason.

Verify:

```bash
~/.local/bin/cua-driver --version
~/.local/bin/cua-driver list-tools     # 34 tools
~/.local/bin/cua-driver doctor         # full system check
```

### Permissions (macOS specifically)

macOS gates accessibility and screen capture behind TCC (Transparency, Consent, Control). cua-driver needs two grants attached to the identity `com.trycua.driver`:

1. **Accessibility** — required for `get_window_state`, `click` (element_index path), `type_text`, `press_key`, `hotkey`, `set_value`, and `get_accessibility_tree` past the app-level snapshot.
2. **Screen Recording** — required for any `capture_mode` that returns a PNG (`som`, `vision`) and for `zoom`.

Grant them by running:

```bash
~/.local/bin/cua-driver permissions grant
```

This launches CuaDriver.app via LaunchServices so the system dialog reads "Cua Driver" rather than "Terminal". The grant attaches to whichever bundle ID triggered the prompt; granting via the terminal would attach to the terminal and silently fail later.

Operations that need *no* TCC grants and work immediately after install:

- `launch_app` (uses LaunchServices)
- `list_apps`, `list_windows`
- `get_accessibility_tree` (desktop-level snapshot: app list + window bounds, no inner UI walk)
- `kill_app`

Operations that fail silently without grants — they return `element_count: 0` and an empty `tree_markdown` rather than raising:

- `get_window_state` in any `capture_mode`
- All element_index-addressed actions (because the cache is never populated)
- All screenshot output

Windows has no equivalent gating for most apps. Linux has none for X11; Wayland portals add some.

---

## 3. The execution model

cua-driver has two execution surfaces, and confusing them costs hours.

### 3.1 In-process (`cua-driver call <tool> <json>`)

Spawns a fresh process, performs one tool call, exits. Convenient for shell scripts and tests. Critical limitation: the per-window element-index cache lives in process memory. The moment that process exits, the cache is gone. So this sequence fails:

```bash
cua-driver call get_window_state '{"pid":1234,"window_id":5678}'   # builds cache, exits
cua-driver call click '{"pid":1234,"window_id":5678,"element_index":5}'
# Error: Element index 5 not found in cache for pid=1234 window_id=5678. Call get_window_state first.
```

### 3.2 Daemon (`cua-driver serve`)

Long-running process, listens on a Unix domain socket at `~/Library/Caches/cua-driver/cua-driver.sock` (macOS path; analogous locations on Win/Linux). Subsequent `cua-driver call` invocations proxy through the socket, so all calls share the same in-memory element-index cache. This is the mode any real agent should use.

```bash
cua-driver serve &        # background; or run under launchd / systemd
cua-driver status         # confirms socket + daemon pid
```

The daemon is also the runtime for the MCP server (`cua-driver mcp`) — that subcommand is just an stdio adapter that translates MCP tool calls into socket calls against the same daemon.

### 3.3 MCP (`cua-driver mcp`)

For agents that already speak MCP (Claude Code, Codex, Cursor, OpenCode, etc.), `cua-driver mcp` exposes the same 34 tools as MCP tools. Identical surface, identical cache invariants. Use the same daemon underneath via auto-relaunch.

```bash
# Generate config for your client:
cua-driver mcp-config --client claude
cua-driver mcp-config --client codex
cua-driver mcp-config --client cursor      # JSON for ~/.cursor/mcp.json
```

For a custom agent you are writing in Python, you have three options ordered by overhead:

1. **Shell out to `cua-driver call`** through the daemon (what we did in the demo). Simplest, ~30 ms overhead per call.
2. **Speak MCP** to `cua-driver mcp` over stdio. Useful if you are already using an MCP client library.
3. **Speak the raw socket protocol** directly. Lowest latency, no documentation guarantees — only worth it if you are running thousands of calls per minute.

Default to option 1 unless profiling forces you elsewhere.

---

## 4. The tool surface

cua-driver exposes 34 tools. Run `cua-driver list-tools` for the live list and `cua-driver describe <tool>` for the full JSON schema of any one. Functionally they group into six categories.

### Discovery (no TCC needed)

| Tool | Purpose |
|---|---|
| `list_apps` | Running and installed-but-not-running apps, with per-app state flags. |
| `list_windows` | All top-level windows currently known to WindowServer. |
| `get_accessibility_tree` | Desktop snapshot — running apps + on-screen windows with bounds, z-order, owner pid. The lightweight cousin of `get_window_state`. |
| `get_screen_size` | Logical size of the main display in points plus backing scale factor. |
| `get_cursor_position` | Current mouse position. |

### Perception (TCC needed)

| Tool | Purpose |
|---|---|
| `get_window_state` | The workhorse. Walks an app's AX tree for a given `(pid, window_id)`, returns Markdown tree with every actionable element tagged `[element_index N]`. Three modes: `som` (AX + screenshot), `ax` (AX only — faster, no Screen Recording grant needed for the AX part), `vision` (screenshot only — for vision-LM agents that ignore AX). |
| `zoom` | Cropped JPEG of a window region with auto-padding, for closer inspection. |

### Action

| Tool | Address modes | Notes |
|---|---|---|
| `click` | `element_index` or `(x, y)` window-local pixels | Optional `modifier` keys, `count` for multi-click. |
| `double_click`, `right_click` | Same | Convenience over `click`. |
| `drag` | Pixel-only: `(from_x, from_y) → (to_x, to_y)` | No semantic drag. |
| `scroll` | Targets focused region of a pid | Synthesizes keystrokes. |
| `type_text` | Inserts via `AXSetAttribute(kAXSelectedText)` | More reliable than `press_key` for long strings. |
| `press_key`, `hotkey` | Single key or combination | Delivered via `CGEventPostToPid` on macOS. |
| `set_value` | Direct AX value set on text fields, sliders, etc. | Faster than typing for forms. |
| `launch_app` | LaunchServices on macOS | Never steals focus; see § 6.1. |
| `kill_app` | Force terminate by pid | `kill -9` equivalent. |
| `bring_to_front` | Windows-only | Stubs on macOS/Linux; see § 6.1. |

### Browser-internal

| Tool | Purpose |
|---|---|
| `page` | Drives the DOM of an Electron / VS Code / Cursor / Tauri app via Chrome DevTools Protocol or WebKit Inspector. Requires the app to have been launched with `electron_debugging_port` or `webkit_inspector_port`. |

This is the escape hatch for web content that AX cannot see; see § 7.2.

### Session & overlay

| Tool | Purpose |
|---|---|
| `start_session`, `end_session` | Declares a named identity for a run; lets multiple concurrent agents coexist without clobbering each other's cursor overlay and per-session config. |
| `set_agent_cursor_enabled`, `set_agent_cursor_style`, `set_agent_cursor_motion`, `move_cursor`, `get_agent_cursor_state` | A virtual "agent cursor" overlay distinct from the real macOS cursor. Useful for demos and for users who want to see where the agent is acting without losing their own pointer. Default-on for MCP sessions, off otherwise. |

### Recording & replay

| Tool | Purpose |
|---|---|
| `start_recording`, `stop_recording`, `get_recording_state` | Records every tool call into a turn-numbered trajectory directory. |
| `replay_trajectory` | Re-invokes every recorded call in order. The standard way to build deterministic regression tests for an agent. |

### Configuration & introspection

`get_config`, `set_config`, `check_permissions`, `check_for_update`.

---

## 5. The canonical loop

Every interaction with cua-driver follows the same three-phase shape per turn per window. Memorize it.

```
phase 1: SCAN     → get_window_state(pid, window_id)         returns element_index map
phase 2: ACT      → click / type_text / press_key / ...      addresses by element_index
phase 3: VERIFY   → get_window_state(pid, window_id) again   confirms the state changed
```

Two invariants from the schema, both load-bearing:

**Invariant A.** Call `get_window_state` once per turn per `(pid, window_id)` before any element-indexed action. The cache is built by that call.

**Invariant B.** Every `get_window_state` snapshot replaces the previous index map. An `element_index` from snapshot N is not guaranteed to still mean the same element in snapshot N+1. Treat indices as turn-scoped tokens.

The second invariant exists because UIs reflow. A dialog opens, a menu pops up, a list re-sorts — the AX walk visits nodes in a different order and indices shift. Re-scanning after every action is the cost of using semantic addressing instead of pixel coordinates.

### What this looked like in practice

The complete sequence to compute 7 × 8 on macOS Calculator:

```bash
# 0. ensure daemon is running so the element cache survives across calls
~/.local/bin/cua-driver serve &

# 1. launch (LaunchServices; no focus steal; window_id returned for free)
PID=$(~/.local/bin/cua-driver call launch_app \
  '{"bundle_id":"com.apple.calculator"}' | jq -r .pid)
WID=$(~/.local/bin/cua-driver call list_windows '{}' \
  | jq -r ".windows[] | select(.pid==$PID) | .window_id" | head -1)

# 2. activate — necessary on macOS because launch_app keeps the app backgrounded
#    and a backgrounded Calculator's button subtree is not yet realized in AX.
#    bring_to_front is Windows-only, so we use AppleScript.
osascript -e 'tell application "Calculator" to activate'
sleep 1

# 3. scan
~/.local/bin/cua-driver call get_window_state \
  "{\"pid\":$PID,\"window_id\":$WID,\"capture_mode\":\"ax\",\"query\":\"button\"}"

# 4. act — indices from the scan above: 7=5, ×=8, 8=6, ==19
for i in 5 8 6 19; do
  ~/.local/bin/cua-driver call click \
    "{\"pid\":$PID,\"window_id\":$WID,\"element_index\":$i}"
done

# 5. verify — read AXStaticText off the display
~/.local/bin/cua-driver call get_window_state \
  "{\"pid\":$PID,\"window_id\":$WID,\"capture_mode\":\"ax\",\"query\":\"56\"}" \
  | jq -r .tree_markdown
# → - AXStaticText = "‎56"
```

That is the whole loop. Everything else an agent does on top — goal interpretation, multi-step planning, retry on failure, vision-based perception when AX is empty — is your code, not the driver's.

---

## 6. Empirical traps we hit, and how to handle them

Things the documentation does not warn about loudly enough. All confirmed by running the calculator demo.

### 6.1 Background launches return a stub AX tree

`launch_app` on macOS uses LaunchServices and explicitly does *not* steal focus. The response field `self_activation_suppressed: true` confirms this. The side effect: the app's main window is not yet realized in the AX hierarchy, so `get_window_state` returns only the system menu bar (188 elements in our case) and zero buttons.

`bring_to_front` looks like it should fix this, but its schema is explicit: macOS returns an error pointing at `NSRunningApplication.activate`. The input tools (`click`, `type_text`) reach backgrounded windows via `CGEventPostToPid`, so they don't need foreground — but the AX walk does.

Workaround: activate via AppleScript before the first scan.

```bash
osascript -e 'tell application "Calculator" to activate'
```

After activation the same `get_window_state` call returned 237 elements including all 19 calculator buttons. The activation is one-time; subsequent scans work without re-activating as long as the window remains realized.

### 6.2 Element-index cache is process-scoped

Demonstrated explicitly in § 3.1. The error message is `Element index N not found in cache for pid=... window_id=.... Call get_window_state first.` If you see this and you did call `get_window_state` in the immediately preceding shell command, you are not running the daemon. Run `cua-driver serve`.

### 6.3 Two AXWindow nodes for the same window

Our scan returned the Calculator AXWindow tree twice, at indices [0–20] and again at [212–236] with identical content. The second copy appeared under an `AXMenuBarItem "Help"` parent, which is clearly wrong — likely a quirk of how the Rust walker traverses the AX tree when both `kAXMainWindowAttribute` and `kAXWindowsAttribute` resolve to the same window.

Practical consequence: when you parse the markdown tree to extract element indices, use the *first* occurrence of each button label, not the last. The first occurrence is the one anchored at the canonical `AXWindow` and is the one most likely to remain stable across snapshots. The duplicated indices do work — pressing `element_index 213` also clicks "All Clear" — but they are an implementation detail to avoid relying on.

### 6.4 `query` filter trims display but preserves indices

`get_window_state` accepts a `query` argument that case-insensitively filters the rendered `tree_markdown` to matching lines plus their ancestor chain. The `element_index` values are unchanged; only the Markdown output is trimmed. This is the right way to handle large trees: scan with `query: "button"` to get only actionable items in the output, while the daemon still caches the full 237-element index map for action.

For Calculator, an unfiltered scan returns ~9 KB of Markdown (mostly menu bar items: "About This Mac", every "Recent Items" entry, every system Apple-menu action). The filtered scan returns ~3 KB and is much easier for an LLM to read.

### 6.5 Permissions status reports "unknown" without a daemon

`cua-driver permissions status` queries the running daemon's TCC state (under `com.trycua.driver`). If the daemon isn't running, the status tool refuses to report the terminal's TCC state — which would be wrong, since it would not match the driver's actual permissions. Read this as a feature, not a bug: it forces you to test against the real identity. To verify grants without starting the daemon, just run any tool that requires them and check the result.

### 6.6 Silent failure on missing permissions

A failed `get_window_state` does not raise an error. It returns:

```json
{ "element_count": 0, "pid": ..., "tree_markdown": "", "window_id": ... }
```

with a stderr warning like `could not create image from window` or `screencapture failed`. An agent that does not check `element_count > 0` will happily try to address `element_index 5` from an empty cache and get the unhelpful "not found in cache" error. Always guard:

```python
state = call("get_window_state", {"pid": pid, "window_id": wid})
if state["element_count"] == 0:
    raise PermissionsError("cua-driver got an empty AX tree — check TCC grants")
```

---

## 7. Cross-platform reality

The Rust backend ships the same 34 tools on all three platforms. Tool *availability* is uniform. Tool *fidelity* is not, because the underlying OS APIs are not equivalent.

### 7.1 What works well everywhere

Anything built on standard platform UI toolkits exposes its full structure:

- macOS: AppKit, SwiftUI, Catalyst, Mac Catalyst-ported iOS apps
- Windows: WinUI 3, WPF, UWP, WinForms, most modern Win32 with UIA providers
- Linux: GTK 3/4, Qt 5/6 with `QT_ACCESSIBILITY=1`

This covers virtually every native productivity app shipped by the OS vendor itself (Finder/Explorer/Files, Calculator, Mail, Notes, Settings/Control Panel, Activity Monitor / Task Manager / System Monitor, the file picker, system dialogs, Office on Mac and Windows).

### 7.2 The Electron and Tauri problem, and how `page` solves it

A large fraction of modern desktop apps — VS Code, Cursor, Slack, Discord, Notion, Linear desktop, 1Password — render their UI as HTML inside an embedded Chromium. macOS sees the outer window as `AXWindow` and its children as a single opaque `AXGroup` or `AXWebArea`. `get_window_state` returns the menu bar and the window frame, with no addressable buttons inside.

cua-driver's answer is to launch these apps with a debugging port enabled and drive their DOM directly via Chrome DevTools Protocol.

```bash
cua-driver call launch_app '{
  "bundle_id":"com.microsoft.VSCode",
  "electron_debugging_port": 9222
}'
# then:
cua-driver call page '{"pid": <vscode_pid>, "action": "click", "selector": ".tabs-container .tab.active"}'
```

The `page` tool gives you the full DOM: CSS selectors, JavaScript evaluation, element waiting, navigation. For Tauri / WebKit apps, use `webkit_inspector_port` instead. Without these flags the apps are pixel-only.

For browsers themselves (Chrome, Safari, Firefox), the same trick applies to the rendered page. Chrome already supports `--remote-debugging-port`; Safari requires enabling Develop > Allow Remote Automation; Firefox supports CDP through `--remote-debugging-port` only in recent versions.

### 7.3 Where AX simply does not exist

Three categories where the OS itself has no structural information to share:

1. **Games and full-screen renderers.** Anything drawing with OpenGL, Metal, Vulkan, DirectX, custom Skia paths, immediate-mode UI (Dear ImGui, Nuklear). The AX tree contains an `AXWindow` and nothing inside it.
2. **HTML `<canvas>` and WebGL content.** Figma, Google Maps, Photopea, web-based games. Even inside a browser with CDP, the canvas is opaque to selectors — you get the canvas DOM node but no addressable structure inside it.
3. **Apps that disable AX deliberately.** Some banking apps, DRM'd video players, a few macOS system surfaces (Touch ID prompt, the login screen) gate AX behind additional entitlements that an unprivileged driver does not have.

For these you have exactly two perception strategies, both pixel-based: template matching (OpenCV) and vision-LM perception (give the screenshot to a multimodal model and ask where to click). The action side still works — `click {pid, window_id, x, y}` reaches anything visible — but the agent is now responsible for the perception that the AX tree was giving you for free.

### 7.4 Linux specifics

Two extra wrinkles on Linux that do not exist on macOS or Windows:

1. **Wayland vs. X11.** cua-driver's input synthesis on Linux uses `XTestFakeKeyEvent` / `XTestFakeButtonEvent`, which only work under X11 or under XWayland for legacy apps. Native Wayland apps require the user to grant input via Wayland portals (`org.freedesktop.portal.RemoteDesktop`), which is interactive and per-session. Plan for X11 sessions in production until portal flow matures.
2. **Qt accessibility is opt-in.** Qt apps expose AT-SPI only when launched with `QT_ACCESSIBILITY=1` or when the user has globally enabled `org.a11y.Status` on the D-Bus. Without it, Qt apps look like opaque windows even though the app could expose its structure. If you control how the target app launches, set the env var; if not, document the limitation.

### 7.5 Windows specifics

UIA is the most uniformly available of the three accessibility APIs — virtually every modern Windows app exposes structure through it. The one tool with platform-specific behavior is `bring_to_front`, which actually works on Windows (via `SetForegroundWindow`) and is a no-op on macOS and Linux. On Windows it is sometimes the cleanest way to recover from a popup stealing focus, since UIA queries respect the foreground.

### 7.6 Summary table

| Capability | macOS | Windows | Linux |
|---|---|---|---|
| Semantic scan of native apps | yes | yes | yes (X11/GTK/Qt-with-env) |
| Pixel-coordinate click on anything | yes | yes | yes (X11; Wayland with portal grant) |
| Web content via CDP/WebKit Inspector | yes | yes | yes |
| Backgrounded action (no focus steal) | yes (`CGEventPostToPid`) | partial (`SendInput` needs foreground for some apps) | varies |
| `bring_to_front` | no-op | yes | no-op |
| Permission gate at install | TCC (Accessibility + Screen Recording) | none for most apps | none on X11; portal on Wayland |
| Element index cache | per-daemon | per-daemon | per-daemon |

---

## 8. Where cua-driver stops, and where your agent starts

A useful exercise: take a concrete goal and list which steps cua-driver owns versus which you build.

**Goal:** "Open the project's Linear board and mark issue ENG-1234 as Done."

| Step | Who owns it | Why |
|---|---|---|
| Decide that Linear's web app is the right tool | Agent (LLM) | Goal interpretation. |
| Decide whether to use Linear's desktop Electron app or the web app in Chrome | Agent | Depends on what's installed. `list_apps` answers the question. |
| Launch the right app | cua-driver `launch_app` | With `electron_debugging_port: 9222` if Electron, or as a bookmarked URL via the system `open` if web. |
| Wait for the app to load | Agent | cua-driver has no semantic "wait for ready"; poll `get_window_state` until expected elements appear. |
| Find the search box | cua-driver `get_window_state` or `page` | Returns the element index or DOM selector. |
| Decide which element among 200+ is the search box | Agent | Heuristics ("AXTextField near top of window", `placeholder="Search"`), or an LLM prompt over the tree markdown. |
| Type "ENG-1234" | cua-driver `type_text` | |
| Press Enter | cua-driver `press_key` | |
| Identify the result row | Agent | Pattern-match the post-search AX tree against expectations. |
| Click the result | cua-driver `click` with `element_index` | |
| Find the status dropdown | Agent + cua-driver | Same dance: re-scan, decide, click. |
| Select "Done" | cua-driver `click` | |
| Verify status changed | Agent | Re-scan, parse, check. Or look for the toast notification. |
| Retry on AX-tree-mismatch | Agent | cua-driver does not retry; it reports the immediate result. |

The pattern is consistent: cua-driver does perception and action; the agent does interpretation, planning, sequencing, verification, and recovery.

### The five layers above cua-driver you must build

1. **Goal decomposition.** Map a natural-language goal to an ordered sequence of app-level subgoals. LLM-driven; the prompt should ground in `list_apps` output so the LLM knows what is installed.
2. **Perception interpretation.** The AX tree markdown is too verbose for direct LLM use on large apps. Either pre-filter with `query`, summarize with a cheap model into a shortlist, or extract structured data via heuristics (regex over `[N] AXButton "Label"`). The cost-quality tradeoff here is your single biggest knob.
3. **Action sequencing.** Translate "click the Done button in the status dropdown" into the scan → act → verify loop. Handle the cache-invalidation invariant correctly: re-scan after every state-changing action.
4. **Error recovery.** Element gone? Re-scan and re-plan. Permission denied? Surface to the user. Modal popped up that we didn't expect? Recognize it, dismiss it, retry. cua-driver gives clean exit codes and error strings; your agent decides what to do with them.
5. **Vision fallback.** When `element_count == 0` or the target element is not in the tree (canvas, game, opaque app), switch to `capture_mode: "vision"`, send the PNG to a multimodal LLM, get pixel coordinates back, click with `{x, y}` instead of `element_index`. This is also the right path for grounding-quality questions ("does the page look like it loaded correctly?").

### Recording for debugging and tests

`start_recording` and `replay_trajectory` are the underrated tools in the surface. Workflow:

1. During development, every agent run records to a turn-numbered directory.
2. When the agent fails, you have an exact trajectory of `(tool, args)` pairs to inspect.
3. You can `replay_trajectory` to reproduce deterministically against the same starting UI state.
4. For regression tests, save known-good trajectories and assert that re-running them still terminates in the expected screen state.

Treat this as the cua-driver equivalent of HTTP-recorded fixtures.

---

## 9. End-to-end agent architecture sketch

A minimum-viable agent on top of cua-driver looks roughly like this:

```
┌───────────────────────────────────────────────────────────────┐
│ Planner LLM                                                   │
│  - input: user goal + `list_apps` summary                      │
│  - output: ordered list of (app, subgoal) pairs                │
└────────────────────┬──────────────────────────────────────────┘
                     │
                     ▼
┌───────────────────────────────────────────────────────────────┐
│ Per-subgoal executor                                          │
│                                                                │
│   while not done:                                              │
│     1. scan = get_window_state(pid, window_id, query="...")    │
│     2. if scan.element_count == 0:                             │
│          fallback to vision: get_window_state(... "vision")    │
│          ask multimodal LLM for next action                    │
│     3. else:                                                   │
│          ask LLM: "given this tree and this subgoal,           │
│                    what's the next single action?"             │
│     4. dispatch action via cua-driver                          │
│     5. verify: re-scan, check post-condition                   │
│     6. if not advancing after N turns: replan                  │
└────────────────────┬──────────────────────────────────────────┘
                     │
                     ▼
┌───────────────────────────────────────────────────────────────┐
│ cua-driver daemon (`cua-driver serve`)                        │
│  - Unix socket: ~/Library/Caches/cua-driver/cua-driver.sock   │
│  - holds the element-index cache                              │
│  - speaks AX / UIA / AT-SPI                                   │
└───────────────────────────────────────────────────────────────┘
```

Notes on each layer:

**Planner.** Don't ask the LLM to plan in terms of clicks. Ask it to plan in terms of *intents* ("open file X", "set status to Done"). Click-level planning belongs to the executor where it can ground in the real AX tree.

**Executor.** This is where most of the engineering work is. The loop above is the simplest form; in practice you want timeouts, a maximum-action budget per subgoal, recognition of common UI states (modal dialogs, error toasts, loading spinners), and a clean way to surface "I am stuck" back to the planner.

**Verification.** Cheap verification is the difference between a fragile demo and a working agent. After every action, re-scan and check at least one post-condition: did the expected element appear, did the field's value update, did the window title change? An agent that does not verify cannot recover from a missed click.

**Vision fallback.** Reserve for AX-empty cases. Vision is 10× more expensive per call and the multimodal model's pixel localization is much weaker than the AX tree's element_index. But it is the only option for canvases, games, and unsupported apps.

### Python skeleton

A starting point. Run with `uv run python agent.py` after `uv add anthropic` (or whichever client you prefer).

```python
import json
import subprocess
from pathlib import Path
from typing import Any

CUA = str(Path.home() / ".local" / "bin" / "cua-driver")


class CuaError(RuntimeError):
    pass


def call(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Invoke a cua-driver tool through the running daemon. Raises on non-zero exit."""
    proc = subprocess.run(
        [CUA, "call", tool, json.dumps(args)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise CuaError(f"{tool} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {"raw": proc.stdout}


def ensure_daemon() -> None:
    """Start cua-driver serve if no daemon is running."""
    status = subprocess.run([CUA, "status"], capture_output=True, text=True)
    if "is running" not in status.stdout:
        subprocess.Popen([CUA, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Daemon is ready when the socket file appears; in practice ~200 ms.


def scan(pid: int, window_id: int, query: str | None = None) -> dict[str, Any]:
    args = {"pid": pid, "window_id": window_id, "capture_mode": "ax"}
    if query:
        args["query"] = query
    state = call("get_window_state", args)
    if state["element_count"] == 0:
        raise CuaError(
            "Empty AX tree — likely a missing TCC grant or the window is not activated. "
            "Run `cua-driver permissions grant` and ensure the app is in front."
        )
    return state


def click_element(pid: int, window_id: int, element_index: int) -> None:
    call("click", {"pid": pid, "window_id": window_id, "element_index": element_index})
```

That is enough to drive any AX-exposing app. The intelligence — choosing which `element_index` to click — is the LLM's job, fed `state["tree_markdown"]` and the current subgoal.

---

## 10. What to delegate to cua-driver, and what never to

A condensed cheat sheet.

### Delegate to cua-driver

- Launching, listing, killing apps.
- Reading the structural state of any UI it can see.
- Synthesizing input events (click, type, key, hotkey, scroll, drag).
- Driving web content via CDP when you launch with `electron_debugging_port`.
- Recording trajectories for replay and regression.
- The agent-cursor overlay for visible-action demos.

### Build outside cua-driver

- Goal interpretation and decomposition.
- Choosing which element matters from a 200-node tree.
- Verifying that an action achieved its intent.
- Recognizing modal dialogs, error states, loading spinners.
- Pixel-mode perception for canvas/game/opaque apps.
- Re-planning on failure.
- Anything stateful across sessions (user preferences, learned shortcuts).

### Never expect from cua-driver

- Semantic addressing inside HTML `<canvas>`, WebGL, or game render targets.
- Reliable behavior against apps that actively block AX (some DRM'd content, login screens).
- Synchronous waits for arbitrary UI conditions (poll yourself).
- Cross-window choreography ("drag from window A to window B" — drag is single-window).
- Cross-session memory of element indices.

---

## 11. Quick reference

```bash
# Install
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh)"

# Permissions (macOS only)
~/.local/bin/cua-driver permissions grant

# Daemon (start before any element_index work)
~/.local/bin/cua-driver serve &

# Discovery
~/.local/bin/cua-driver call list_apps '{}'
~/.local/bin/cua-driver call list_windows '{}'

# Launch
~/.local/bin/cua-driver call launch_app '{"bundle_id":"com.apple.calculator"}'
~/.local/bin/cua-driver call launch_app '{"name":"VS Code","electron_debugging_port":9222}'

# Activate (macOS workaround for background-launched windows)
osascript -e 'tell application "Calculator" to activate'

# Scan
~/.local/bin/cua-driver call get_window_state \
  '{"pid":1234,"window_id":5678,"capture_mode":"ax","query":"button"}'

# Act
~/.local/bin/cua-driver call click       '{"pid":1234,"window_id":5678,"element_index":5}'
~/.local/bin/cua-driver call type_text   '{"pid":1234,"window_id":5678,"text":"hello"}'
~/.local/bin/cua-driver call press_key   '{"pid":1234,"window_id":5678,"key":"Return"}'
~/.local/bin/cua-driver call hotkey      '{"pid":1234,"keys":["cmd","s"]}'

# Verify
~/.local/bin/cua-driver call get_window_state \
  '{"pid":1234,"window_id":5678,"capture_mode":"ax","query":"<expected text>"}'

# Vision fallback when AX is empty
~/.local/bin/cua-driver call get_window_state \
  '{"pid":1234,"window_id":5678,"capture_mode":"vision","screenshot_out_file":"/tmp/s.png"}'
~/.local/bin/cua-driver call click '{"pid":1234,"window_id":5678,"x":120,"y":340}'

# Browser DOM (Electron / VS Code launched with debugging port)
~/.local/bin/cua-driver call page '{"pid":1234,"action":"click","selector":".btn-primary"}'

# Recording
~/.local/bin/cua-driver call start_recording '{"output_dir":"/tmp/run01"}'
# ... actions ...
~/.local/bin/cua-driver call stop_recording '{}'
~/.local/bin/cua-driver call replay_trajectory '{"trajectory_dir":"/tmp/run01"}'

# Introspection
~/.local/bin/cua-driver list-tools
~/.local/bin/cua-driver describe get_window_state
~/.local/bin/cua-driver status
~/.local/bin/cua-driver doctor
```

---

## 12. Further reading and the project itself

- Source: https://github.com/trycua/cua (`libs/cua-driver/rust` for the binary we use)
- Docs: https://cua.ai/docs/cua-driver
- Releases: https://github.com/trycua/cua/releases (current: `cua-driver-rs-v0.5.2`)

Two related cua subprojects to know about, not used here:

- `cua` Python package: the sandbox/VM path (`Sandbox.ephemeral(Image.macos())`). Heavyweight; for cases where you cannot trust the agent on the host.
- `cua-agent`: a reference agent layered on top of either the sandbox or the driver. Useful as a model for how to wire an LLM into the loop. Read the source, do not depend on it for production — your agent's policies will differ.

A final note on staying honest. cua-driver is mechanical: it reports what the OS sees and performs what you tell it to. It does not know whether a click "succeeded" in any meaningful sense beyond the AXPress action returning without error. The button you pressed might have been disabled and visually unchanged, the form you typed into might have validation rules that rejected the input silently, the link you clicked might have opened in a different window the agent isn't watching. Verification — re-scanning and reading the actual state — is the only way to bridge mechanical action and semantic success. Budget for it in your loop. Agents that skip verification are agents that confidently report incorrect results.
