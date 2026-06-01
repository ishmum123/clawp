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
  │                    clears trust prompt (one Enter), waits for idle prompt
  ├─ offset = len(read_records(transcript))   # before send; swallows prior turn's tail
  ├─ send_text ── prompt VERBATIM via bracketed paste (no injection)
  ├─ capture loop (POLL=0.6s) — DUAL SIGNAL completion:
  │     transcript: assistant record w/ terminal stop_reason ─> capture text[] (the answer)
  │     screen backstop: idle prompt + no spinner + stable    ─> done (unknown future stop_reason)
  │     blocked (permission/trust dialog)  ─> exit 3
  │     stall / MAX_TURN                    ─> exit 4
  └─ _log_turn → INSERT into responses (sqlite) ── kill pane (every exit path)
```

Transcript path is deterministic from the session-id: `~/.claude/projects/*/<session-id>.jsonl`. Spinner = an ellipsis (`…`) in the bottom screen region; idle = `Model:` in status bar and no dialog markers. Schema-break fallback: settled but zero parsed answer → text mode falls back to lossy `scrape_reply` (flagged `low_fidelity`); json/stream-json loud-fail.

**Permission modes:** default `acceptEdits`; `--full-auto` → `bypassPermissions` (unattended, trusted prompts only). `--permission-mode` passthrough overrides the default.

## Commands

```sh
python3 test_clawp.py              # full test suite (pure parsing helpers, no live claude)
python3 clawp.py "prompt"          # run a turn (text output)
python3 clawp.py --output-format json "..."
python3 clawp.py --resume <id> "..."
python3 clawp.py --history -n 20   # view recent logged turns
```

No build, lint, or package config. `test_clawp.py` is a plain script of `assert`-backed `check()` calls; run the whole file, there are no individual tests to select.

## CLI surface

`clawp [flags] [prompt]`, mirroring `claude [options] [prompt]`. No subcommands. Prompt from positional arg else stdin; both empty → exit 2. Only `--history` is clawp's own (log viewer). `-p`/`--print` is an accepted no-op synonym. Print-only flags (`--input-format`, `--max-turns`, `--include-partial-messages`, `--fallback-model`, `--json-schema`, `--replay-user-messages`) error rather than silently no-op; session flags (`--model`, `--add-dir`, etc.) pass through to the launch.

## Conventions

- Standard library only — do not add third-party dependencies.
- Keep it a single file; new logic goes in `clawp.py`.
- Tuning constants live at the top of `clawp.py` (`POLL`, `STABLE_NEEDED`, `STALL_SECS`, `MAX_TURN`, `READY_TIMEOUT`, `SEND_SETTLE`, `PANE_W/H`). Adjust there, not inline.
- All jsonl-schema knowledge stays in the one localized transcript-parser section (between the `--- transcript parser ---` markers, beside `scrape_reply`). A claude format change is a one-place fix; depend on the minimum set of fields and ignore the rest.
- `init_db` migrates older DBs via `ALTER TABLE ... ADD COLUMN` wrapped in `suppress(OperationalError)`; when adding a `responses` column, extend the migrate loop **and** the `CREATE TABLE` **and** the `INSERT` placeholder count together.
- Always insert into SQLite with parameterized queries.
- Comments explain *why* (non-obvious TUI/timing/schema behavior), never *what*. Match the existing near-zero density; no docstrings on self-explanatory helpers.

## Forbidden

- **Never** invoke `claude -p` / `--print` / any non-interactive mode — breaks the subscription-billing premise.
- **Never inject** anything into the prompt — it goes out byte-verbatim; the answer comes from the transcript, not a scratch file.
- Never type into a permission/trust dialog to dismiss it (except the one-time trust-folder Enter in `ensure_session`); report the turn `blocked` instead.

## Project graph

`.graph/index.json` is a committed hierarchical summary of the repo, maintained by the `project-graph` skill. Use it to orient before reading source.

- **Before pushing**, run `/project-graph update` so the graph reflects modified/added/deleted files, and commit the refreshed `.graph/index.json` alongside the change.
- If the skill is missing, install it: `npx skills add ishmum123/project-graph` (the [vercel-labs/skills](https://github.com/vercel-labs/skills) CLI; installs into `.claude/skills/`).
- Rebuild from scratch with `/project-graph build` if the graph is stale or corrupt.

## Auto-generated / ignored

`clawp.sqlite`, `__pycache__/`, `*.pyc` — all gitignored.
