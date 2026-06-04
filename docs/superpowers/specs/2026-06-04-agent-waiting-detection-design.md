# Agent Waiting Detection — Design

Date: 2026-06-04
Status: Approved

## Problem

Panes running AI agents (Claude Code, Codex, Copilot CLI, Opencode) periodically stop
and wait for a user decision (yes/no, menu selection, permission approval). The
dashboard currently shows activity (output flowing) but cannot distinguish "agent is
working" from "agent is blocked waiting for me". Users miss prompts and agents sit
idle.

## Approach (chosen)

Server-side pattern matching on captured pane content. No agent-side configuration
required (Claude Code hooks integration deferred to phase 2).

Rejected alternatives:
- **Claude Code hooks** (`Notification`/`Stop` hook → `tmux set-option -p @agent_status`):
  100% accurate but only covers Claude Code and requires per-user setup. Phase 2.
- **tmux bell flag**: window-level only, too coarse.
- **process stdin state**: not viable — TUI agents always read stdin.

## State model

Per pane, server emits `waiting: true/false`. Priority in UI: waiting (red) >
busy/activity glow (amber) > idle.

## Server (`tmux_server.py`)

- New `detect_waiting(lines)` — takes the ANSI-stripped captured lines, scans only the
  **last 8 non-empty lines** (prompt UIs are multi-line: question + options + hint
  line). Patterns hitting older scrolled-away content must NOT trigger.
- Box-drawing border characters (`│ … │`, `╭───╮`) are stripped from line edges before
  matching — Claude Code renders its dialogs inside a border box, and the line anchors
  must see through it. Border-only lines don't consume the scan window.
- Runs inside `fetch_pane_state()` (one capture returns `{preview, waiting}` — no
  extra subprocess; `fetch_preview` remains as a thin shim over it).
- Pane JSON gains `"waiting": bool`.

### Pattern table (case-sensitive unless noted)

Pane content is arbitrary (logs, docs, code, shell prompts), so every pattern is
anchored to the SHAPE of a real prompt line — substring matching false-positives on
prose, logs and starship/pure `❯` shell prompts (found in review round 1).

Patterns split into two tiers (found in live use): only **STRONG** patterns decide.
Option-shaped lines ("❯ 1. …", "1. Yes") are **WEAK** — Claude Code collapses an
answered question into a one-line echo ("❯ 1. A 2. B 3. C") that lingers in the
transcript and must not keep the pane red. A live dialog always shows a STRONG line
(question line or the "Enter to select …" footer at the very bottom), so dropping
WEAK from the decision loses no real prompt.

| Tier | Source | Patterns (regex, per line) |
|---|---|---|
| STRONG | Claude Code | `^\s*Do you want to .*\?\s*$` |
| STRONG | Claude Code / Copilot | `(?:^\s*\|·\s*)Enter to select`, `(?i)(?:^\s*\|·\s*\|\()esc to cancel` (hint lines, line-start or after `·`) |
| STRONG | Codex | `(?i)^\W*yes \(y\)\s*$`, `^\W*No,?\s.*\(n\)\s*$`, `(?:^\s*\|·\s*)Press Enter to confirm` |
| STRONG | Codex / Copilot | `^\s*Allow .*\?\s*$` (approval question) |
| STRONG | Opencode | `(?i)^\s*permission required` |
| STRONG | Generic | `(?i)\(y/n\)\s*\??\s*$`, `\[Y/n\]\s*$`, `\[y/N\]\s*$` (end-of-line), `(?i)^(proceed\|continue)\?$` (whole line only — agent narration ends sentences with "…continue?") |
| WEAK | Claude Code | `^\s*❯\s*\d+\.\s+\S` (selected menu option) |
| WEAK | Copilot | `^\s*\d+\.\s+(Yes\|No)\s*$` (bare menu option) |

Canonical source of truth: `WAITING_PATTERNS` in `tmux_server.py` — keep this table
in sync when patterns change.

## Frontend (`tmux_dashboard.html`)

- `pane.waiting` → red breathing glow (clone of `activity-breathe`, red palette) +
  `⏸ waiting` badge. Overrides amber busy glow.
- Collapsed window header rolls up a red waiting badge (same mechanism as `win-activity`).
- **Browser notification**:
  - Request `Notification` permission on first load; declined → silently skip.
  - Fire only on transition not-waiting → waiting, deduped per pane (no re-fire while
    a pane stays waiting). The dedupe entry is recorded only when a notification
    actually fired, so a pane already waiting during the permission-prompt window
    still notifies once permission is granted. Entries for panes that vanish while
    waiting are reaped against the keys seen in the current render.
  - Body: `{pane title，無自訂 title 時用 command} 在等你回覆` — label is control-char
    stripped (C0+DEL+C1) and length-capped before display; clicking focuses the
    dashboard tab.
- Demo mode includes one simulated waiting pane.

## Testing

- Unit tests for `detect_waiting()`: real prompt samples from all four agents,
  false-positive samples (e.g. vim showing "(y/n)" mid-buffer, scrolled-away old
  prompt, plain shell), edge cases (empty preview, all-blank lines).
- API schema test: `waiting` field present on every pane.
- TDD: tests written and failing before implementation.

## Out of scope (YAGNI)

- Claude Code hooks (phase 2)
- Sorting waiting panes to top
- Sound alerts
