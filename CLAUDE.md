# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`clawp` is a subscription-billed drop-in for `claude -p`: `clawp [flags] [prompt]` (or `echo prompt | clawp`) takes a prompt from a positional arg or stdin, prints the answer to stdout, and sends status + the session-id to stderr with proper exit codes. Single file (`clawp.py`), Python standard library only.

**Why it exists:** scripted use of the interactive TUI bills against the Claude *subscription*, while `claude -p` draws from the Agent SDK credit pool. `clawp` must therefore **never** call `claude -p`. It runs the *interactive* TUI in an ephemeral tmux pane and reads the answer back from the transcript jsonl claude writes to disk.

## Architecture

```
clawp "prompt"
  │  session-id = uuid (or --resume <id>)
  ├─ session_lock (flock /tmp/clawp-<name>.lock, one turn/pane)
  ├─ ensure_session ── tmux new-session "claude --session-id <uuid> --permission-mode <mode>"
  │                    clears trust prompt (Enter) + bypass warning (--full-auto), waits for idle
  ├─ offset = len(read_records(transcript))   # before send; swallows prior turn's tail
  ├─ send_text ── prompt VERBATIM via bracketed paste
  ├─ capture loop (POLL=0.6s) — DUAL SIGNAL completion:
  │     transcript: assistant record w/ terminal stop_reason ─> capture text[] (the answer)
  │     screen backstop: idle prompt + no spinner + stable    ─> done (unknown future stop_reason)
  │     blocked (permission/trust dialog)  ─> exit 3
  │     stall / MAX_TURN                    ─> exit 4
  └─ _log_turn → INSERT into responses (sqlite) ── kill pane (every exit path)
```

Transcript path is deterministic from the session-id: `~/.claude/projects/*/<session-id>.jsonl`. Spinner = an ellipsis (`…`) on a non-box line in the bottom region (the launch "What's new" box also uses `…`, so boxed `│` lines don't count); idle = any `IDLE_MARKERS` substring in the status bar (`Model:` / `shift+tab` / `/effort` — the bar text varies by version/plan) and no dialog markers. Schema-break fallback: settled but zero parsed answer → text mode falls back to lossy `scrape_reply` (flagged `low_fidelity`); json/stream-json loud-fail.

**Permission modes:** default `acceptEdits`; `--full-auto` → `bypassPermissions` (unattended, trusted prompts only). `--permission-mode` passthrough overrides the default.

## Streaming input (claude-compatible multi-turn)

`--input-format stream-json` (requires `--output-format stream-json`) runs ONE long-lived clawp process against ONE warm pane for a whole conversation. `run_stream_turns` reads NDJSON user turns from stdin (`{"type":"user","message":{"role":"user","content":...}}`), runs each through the shared `_run_turn` against the live pane, and emits claude-shaped stream-json: `system/init` once, then per turn a full `message_start…message_stop` envelope whose single `content_block_delta` carries the whole answer, then `result`. The pane is reaped when stdin closes (EOF = conversation over), so process lifetime = conversation lifetime — warmth/teardown need no flag. Scope is **sequential text turns**: no token-level streaming (whole-message granularity), no queuing/interrupts, no image content; a mid-stream permission/stall dialog ends the session (clawp can't answer it). The single-turn path keeps clawp's own simplified stream-json shape — only this mode emits the claude envelope. Design: `docs/stream-json-input.md`.

**Prompt-prefix guard (`prefix_rejection`, both paths):** a prompt whose first non-blank char is `/` or `!` is refused before `send_text` — the TUI reads `/` as a command (hangs, no reply) and `!` as a shell command that **executes on the host** (`claude -p` does neither, so forwarding it would be an RCE the drop-in target lacks). `@` (file) and `#` (memory) still deliver the message, so they pass. Streaming refuses per-turn and the conversation continues; single-shot exits 2.

## Commands

```sh
python3 test_clawp.py              # full test suite (pure parsing helpers, no live claude)
python3 clawp.py "prompt"          # run a turn (text output)
python3 clawp.py --output-format json "..."
python3 clawp.py --resume <id> "..."
python3 clawp.py --history -n 20   # view recent logged turns
# streaming multi-turn: NDJSON user turns on stdin, one warm pane, claude-shaped events
python3 clawp.py --input-format stream-json --output-format stream-json
```

No build, lint, or package config. `test_clawp.py` is a plain script of `assert`-backed `check()` calls; run the whole file, there are no individual tests to select.

## CLI surface

`clawp [flags] [prompt]`, mirroring `claude [options] [prompt]`. No subcommands. Prompt from positional arg else stdin; both empty → exit 2. Only `--history` is clawp's own (log viewer). `-p`/`--print` is an accepted no-op synonym. `--input-format stream-json` enables streaming-input mode (see below). `--include-partial-messages` is a no-op under `--output-format stream-json` (clawp reads completed transcript records, so it can't emit token-level partials, but the envelope's whole-answer delta is valid stream-json) and exits 2 for text/json — mirroring claude's "only works with stream-json" rule. The remaining print-only flags (`--max-turns`, `--max-budget-usd`, `--fallback-model`, `--json-schema`, `--replay-user-messages`) exit 2; session flags (`--model`, `--add-dir`, etc.) pass through to the launch.

## Conventions

- Standard library only — do not add third-party dependencies.
- Keep it a single file; new logic goes in `clawp.py`.
- Tuning constants live at the top of `clawp.py` (`POLL`, `STABLE_NEEDED`, `STALL_SECS`, `MAX_TURN`, `READY_TIMEOUT`, `SEND_SETTLE`, `PANE_W/H`). Adjust there, not inline.
- All jsonl-schema knowledge stays in the one localized transcript-parser section (between the `--- transcript parser ---` markers, beside `scrape_reply`). A claude format change is a one-place fix; depend on the minimum set of fields and ignore the rest.
- When changing the `responses` schema, update the `CREATE TABLE` and the `INSERT` placeholder count together; if existing `clawp.sqlite` files must carry forward, add an `ALTER TABLE ... ADD COLUMN` migration in `init_db`.
- Always insert into SQLite with parameterized queries.
- Comments explain *why* (non-obvious TUI/timing/schema behavior), never *what*. Match the existing near-zero density; no docstrings on self-explanatory helpers.

## Forbidden

- **Never** invoke `claude -p` / `--print` / any non-interactive mode — breaks the subscription-billing premise.
- **Never inject** anything into the prompt — it goes out byte-verbatim; the answer comes from the transcript.
- Never type into a permission/trust dialog to dismiss it, except two one-time launch gates in `ensure_session`: the trust-folder Enter, and — only when the user chose bypass mode (`--full-auto`) — selecting "Yes, I accept" on the Bypass-Permissions warning (confirmed only once it is the highlighted row, never a blind Enter onto the default "No, exit"). Mid-turn permission prompts still report the turn `blocked`.

## Auto-generated / ignored

`clawp.sqlite`, `__pycache__/`, `*.pyc` — all gitignored.
