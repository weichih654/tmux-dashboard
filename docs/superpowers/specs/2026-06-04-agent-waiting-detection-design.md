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
- Runs inside `fetch_preview` flow (content already captured — no extra subprocess).
- Pane JSON gains `"waiting": bool`.

### Pattern table (case-sensitive unless noted)

| Source | Patterns (regex, per line) |
|---|---|
| Claude Code | `Enter to select`, `❯\s*\d+\.`, `Do you want to`, `Esc to cancel` |
| Codex | `Yes \(y\)`, `No.*\(n\)`, `Allow.*\?`, `Press Enter to confirm` |
| Copilot | `\d+\.\s*Yes`, `Allow this`, `(?i)esc to cancel` |
| Opencode | permission-dialog markers (verify against live tool during implementation) |
| Generic | `\(y/n\)`, `\[Y/n\]`, `\[y/N\]`, `(?i)proceed\?`, `(?i)continue\?` |

## Frontend (`tmux_dashboard.html`)

- `pane.waiting` → red breathing glow (clone of `activity-breathe`, red palette) +
  `⏸ waiting` badge. Overrides amber busy glow.
- Collapsed window header rolls up a red waiting badge (same mechanism as `win-activity`).
- **Browser notification**:
  - Request `Notification` permission on first load; declined → silently skip.
  - Fire only on transition not-waiting → waiting, deduped per pane (no re-fire while
    a pane stays waiting).
  - Body: `{pane title} 在等你回覆`; clicking focuses the dashboard tab.
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
