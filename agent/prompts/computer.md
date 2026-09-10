The Computer skill automates macOS desktop applications through a
five-layer cascade. It uses the `cua-driver call` subprocess interface
to communicate with the running cua-driver daemon and never foregrounds
the target window itself (no-foreground contract) unless explicit AppleScript
activation is required as a precondition.

Inputs (all via metadata):
  goal          Required. Free-text description of what to achieve.
                Examples: "Open TextEdit and type Hello", "Read the title
                of the front window in Safari", "Click the Send button in
                Mail".
  app           Optional. Partial app name used to resolve the window,
                e.g. "TextEdit", "Safari", "Terminal", "Visual Studio Code".
                Case-insensitive. For VS Code use "Visual Studio Code", not "VS Code".
  bundle_id     Recommended for Electron apps. macOS bundle identifier — enables the
                CDP page tool (Layer 2c) for richer DOM-level interaction.
                Known values:
                  VS Code  → "com.microsoft.VSCode"
                  Cursor   → "com.todesktop.230313mzl4w4u92"
                  Slack    → "com.tinyspeck.slackmacgap"
                  Notion   → "notion.id"
                  Discord  → "com.hnc.discord"
                Always include bundle_id when the target app is Electron-based.
  window_id     Optional. cua-driver window identifier, if the Planner
                already knows it (avoids an extra list_windows call).
  actions       Optional. List of deterministic action dicts for Layer 2a
                (bypasses the LLM for known, scripted workflows).
                Each dict: {"type": "click"|"type"|"key"|..., ...}
  force_path    Optional. Pin the cascade to a specific layer:
                "ax_extract" | "deterministic" | "ax_llm" | "electron" | "vision"

Output: ComputerOutput with:
  path      — the cascade layer that ran: "ax_extract", "deterministic",
              "ax_llm", "electron", "vision"
  turns     — number of LLM turns used (0 for layers 1/2a)
  content   — text extracted from the AX tree (layer 1/2b)
  actions   — flat list of every action dict emitted
  window_id — the cua-driver window identifier used


Preconditions:
  1. cua-driver daemon must be running:
       open -n -g -a CuaDriver --args serve
     (The -g flag keeps CuaDriver in the background without foregrounding it.)
  2. The terminal / agent process must have Accessibility permission:
       System Settings → Privacy & Security → Accessibility

When the skill cannot satisfy the goal it returns:
  error_code="permission_denied"   — daemon unreachable or AX permission missing
  error_code="window_not_found"    — target window not visible
  error_code="interaction_failed"  — all layers exhausted within step cap

The Planner should NOT use this skill for web tasks; use the browser
skill instead. Use computer for native macOS GUI automation only.

