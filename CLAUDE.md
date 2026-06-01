# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`cw` drives the interactive `claude` TUI through tmux and captures full-fidelity replies into SQLite. Single file (`cw.py`), Python standard library only.

**Why it exists:** scripted use of the interactive TUI bills against the Claude *subscription*, while `claude -p` draws from the Agent SDK credit pool. `cw` must therefore **never** call `claude -p`.

**Why the file side channel:** scraping the rendered TUI mangles markdown/code. Instead each prompt gets an appended instruction telling claude to `Write` its raw verbatim answer to a per-turn scratch file (`.cw/<turn-id>.md`); the wrapper reads that file at full fidelity and stores it with a parameterized insert. Screen scraping is only a lossy fallback (`via=scrape`).

## Architecture

```
ask(prompt) ── session_lock (flock /tmp/cw-<name>.lock, one turn/session)
  │
  ├─ ensure_session ── tmux new-session "claude --permission-mode <mode>"
  │                    clears trust prompt, waits for idle
  ├─ send_text ── prompt + INSTRUCTION(scratch_path) via bracketed paste
  ├─ run_turn ── poll loop (POLL=0.6s):
  │     file_ready?  ──yes──> read_reply        (via=file, happy path)
  │     spinner?     ──yes──> keep waiting
  │     frozen STALL_SECS? ─> scrape (hang)
  │     quiet + idle prompt + past NUDGE_FLOOR ─> nudge (up to MAX_NUDGES)
  │     quiet + NOT idle (dialog owns screen)  ─> scrape, blocked=1
  └─ init_db + INSERT into responses
```

**Completion signal priority:** scratch file (cheap stat, full fidelity) first; screen state only consulted when the file is absent. Spinner = an ellipsis (`…`) in the bottom screen region; idle = `Model:` in status bar and no dialog markers.

**Permission modes:** default `acceptEdits` (file writes land silently); `--full-auto` → `bypassPermissions` (runs anything, only for trusted prompts). Mode is fixed at session creation — `stop` and recreate to change it.

## Commands

```sh
python3 test_cw.py                 # full test suite (pure screen-parsing helpers, no live claude)
python3 cw.py ask "prompt"         # run a turn
python3 cw.py ask "p" -s name --full-auto
python3 cw.py history -n 20
python3 cw.py stop -s name
```

No build, lint, or package config. There is no test runner — `test_cw.py` is a plain script of `assert`-backed `check()` calls; run the whole file, there are no individual tests to select.

## Conventions

- Standard library only — do not add third-party dependencies.
- Keep it a single file; new logic goes in `cw.py`.
- Tuning constants live at the top of `cw.py` (`POLL`, `STABLE_NEEDED`, `NUDGE_FLOOR`, `STALL_SECS`, `MAX_TURN`, `READY_TIMEOUT`, `MAX_NUDGES`). Adjust there, not inline.
- `init_db` migrates older DBs by `ALTER TABLE ... ADD COLUMN` wrapped in `suppress(OperationalError)`; when adding a `responses` column, extend the migrate loop **and** the `CREATE TABLE` **and** the `INSERT` placeholder count together.
- Always insert into SQLite with parameterized queries.
- Comments explain *why* (non-obvious TUI/timing behavior), never *what*. Match the existing near-zero density; no docstrings on self-explanatory helpers.

## Project graph

`.graph/index.json` is a committed hierarchical summary of the repo, maintained by the `project-graph` skill. Use it to orient before reading source.

- **Before pushing**, run `/project-graph update` so the graph reflects modified/added/deleted files, and commit the refreshed `.graph/index.json` alongside the change.
- If the skill is missing, install it: `npx skills add ishmum123/project-graph` (the [vercel-labs/skills](https://github.com/vercel-labs/skills) CLI; installs into `.claude/skills/`).
- Rebuild from scratch with `/project-graph build` if the graph is stale or corrupt.

## Forbidden

- **Never** invoke `claude -p` / `--print` / any non-interactive mode — breaks the subscription-billing premise.
- Never type keystrokes into a permission/trust dialog to dismiss it (except the one-time trust-folder Enter in `ensure_session`); report the turn `blocked` instead.

## Auto-generated / ignored

`cw.sqlite`, `.cw/` (per-turn scratch files), `__pycache__/`, `*.pyc` — all gitignored.
