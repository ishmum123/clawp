# clawp

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A subscription-billed drop-in for `claude -p`. `clawp [flags] [prompt]` (or `echo prompt | clawp`) behaves like `claude -p` — prompt in, answer to stdout, silent on a clean run — but runs the normal interactive `claude` TUI inside a tmux pane so usage bills against your **Claude subscription** instead of the Agent SDK credit pool that `claude -p` draws from.

The name: **claw** (claude wrapper) + **p** (the `claude -p` it stands in for).

`clawp` never calls `claude -p`. It launches the interactive TUI with a known session-id, sends your prompt verbatim, and reads the answer back from the transcript jsonl that Claude Code writes to disk — full fidelity, markdown and code fences intact.

## Requirements

- [`claude`](https://claude.com/claude-code) (Claude Code) or [`kimi`](https://www.kimi.com/code/docs) (Kimi Code CLI), logged in and on your `PATH`
- `tmux`
- `python3` (standard library only)

## Install

It's a single file — clone and symlink it onto your `PATH`:

```sh
git clone https://github.com/ishmum123/clawp.git
cd clawp
chmod +x clawp.py && ln -s "$PWD/clawp.py" /usr/local/bin/clawp
```

The symlink points at the cloned file, so `git pull` updates your install. To uninstall: `rm /usr/local/bin/clawp`. You can also skip the symlink and run `python3 clawp.py …` directly.

## Usage

```sh
clawp "summarize the difference between TCP and UDP"
echo "explain this stack trace: …" | clawp                  # prompt from stdin
clawp --output-format json "list two primes"                # single JSON result object
clawp --output-format stream-json "…"                       # live NDJSON event stream
clawp --resume <session-id> "and now in Python"             # continue a prior session
clawp --model opus "…"                                       # pass-through session flag
clawp --full-auto "add type hints to calc.py"               # run tools/edits unattended
clawp --history -n 20                                        # recent stored turns

# Kimi Code CLI backend (default is claude)
clawp --client kimi "summarize the difference between TCP and UDP"
clawp --client kimi --resume session_<uuid> "and now in Python"
clawp --client kimi --model kimi-code/kimi-for-coding "…"
clawp --client kimi --full-auto "add type hints to calc.py"
```

The answer prints to stdout; a clean run is silent on stderr (only errors and degraded turns are reported there). The session-id is in the JSON output and in `clawp.sqlite`, so you can `--resume` later. Every turn is logged.

### Example

```console
$ clawp "in one sentence, what does Rust's ? operator do?"
It propagates errors by unwrapping an Ok/Some value or returning the Err/None early from the enclosing function.

$ answer=$(clawp "capital of Japan, one word")    # capture in a script
$ echo "$answer"
Tokyo

$ clawp --output-format json "list two primes as a JSON array"
{"type": "result", "subtype": "success", "session_id": "9dc74688-c41a-4b66-bddf-fddfb56b7da6", "result": "[2, 3]", "is_error": false, "duration_ms": 5900}

$ clawp --resume 9dc74688-c41a-4b66-bddf-fddfb56b7da6 "and two more"
[5, 7]
```

### Options

| flag | meaning |
|------|---------|
| `-p, --print` | accepted no-op (`clawp` is always print) — for `claude -p` compatibility |
| `--client {claude,kimi}` | backend to drive (default: `claude`) |
| `--verbose` | accepted no-op — `clawp`'s `stream-json` is already the full event stream |
| `--no-session-persistence` | accepted no-op, **not** forwarded — `clawp` reads the answer from the session transcript, so it can't run with persistence off |
| `--output-format text\|json\|stream-json` | `text` (default), one `result` JSON object, or live NDJSON events |
| `--resume <id>` | continue a specific session (reloads prior context); `-c`/`--continue` not supported |
| `--session-id <uuid>` | use this id instead of a generated one (Claude only; rejected for fresh Kimi sessions) |
| `--full-auto` | launch with `bypassPermissions`/`--auto` (default `acceptEdits`) — only on prompts/projects you trust |
| `--cwd <dir>` | directory the client runs in (default: current); long-form only |
| `-d, --db <path>` | sqlite log file (default `./clawp.sqlite`) |
| `--history`, `-n <N>` | view recent logged turns instead of running |
| `--model`, `--effort`, `--add-dir`, `--system-prompt`, `--permission-mode`, … | passed through to the interactive launch (Claude only; Kimi honors `--model` and `--full-auto` → `--auto`, rejects the rest) |

A command line written for `claude -p` runs unchanged with `--client claude` (the default): session flags pass through to the launch; `-p`/`--print`, `--verbose`, and `--no-session-persistence` are accepted and ignored; print-only flags (`--input-format`, `--max-turns`, `--include-partial-messages`, `--fallback-model`, `--json-schema`, `--replay-user-messages`) are rejected with exit 2. With `--client kimi`, only `--model` and `--full-auto` (mapped to `--auto`) are honored; other Claude-only passthrough flags are rejected with exit 2.

## How it works

1. Select the backend with `--client` and generate (or resume) a session. Launch the interactive client (`claude --session-id <uuid> --permission-mode <mode>` or `kimi [--session <id>] [--auto] [--model <alias>]`) in a detached tmux pane (interactive → subscription billing). The one-time trust-folder prompt is cleared with a single Enter for Claude; Kimi session IDs are discovered from the welcome screen or `~/.kimi-code/session_index.jsonl`.
2. Record the current length of the transcript (`~/.claude/projects/*/<session-id>.jsonl` for Claude, `~/.kimi-code/sessions/<workDirKey>/<sessionId>/agents/main/wire.jsonl` for Kimi), then send your prompt **verbatim** via bracketed paste.
3. Tail the transcript from that offset. Completion is **dual-signalled**: a new terminal record (Claude `assistant` with terminal `stop_reason`; Kimi `step.end` with `finishReason == "end_turn"`) whose text blocks are the answer, or a screen-idle backstop (idle prompt, no spinner, stable) for unknown future stop reasons and dialogs.
4. Capture the answer, log it to SQLite, and kill the pane on every exit path.

If the transcript settles but yields no usable answer (a schema change), `text` mode falls back to a lossy screen scrape flagged `low_fidelity`; `json`/`stream-json` fail loudly rather than emit something wrong.

Stored columns: `id, ts, session, prompt, reply, seconds, low_fidelity, timed_out, blocked, note`.

## Limitations & known behavior

- **Sweet spot is Q&A and light, mostly-edit tasks.** Tool-using turns work, but `clawp` reconstructs against a TUI what `claude -p --output-format stream-json` gives natively; the more autonomous the work, the more the fragility shows.
- **Per-call launch latency.** Each call spins up an ephemeral pane and waits for the idle prompt (a warm pool for the resume path is on the roadmap).
- **If a permission prompt appears** (restrictive config, no `--full-auto`), the turn is reported `blocked` (exit 3) rather than answered. Use `--full-auto` for unattended runs that touch web/bash. The first `--full-auto` launch on a machine that has never accepted bypass mode auto-clears the one-time Bypass-Permissions warning.
- **No token-level partials.** The transcript stores whole messages, so `--include-partial-messages` is accepted but a no-op (the answer lands in one chunk).
- **Don't scrub clawp's env.** It runs interactive `claude` for auth — spawn it with a restricted env (allowlist, systemd, sandbox) and it can't find your login (`Not logged in`). Include `HOME PATH USER SHELL LANG TERM`.
- **One turn at a time per pane** — a concurrent run against the same session fails fast; distinct session-ids run in parallel.
- Kimi support is verified against Kimi Code CLI v0.15.0; the wire format is localized in `KimiAdapter` so future changes are one-place fixes.
- Developed against macOS, tmux 3.5a, Claude Code v2.1.x.

## Roadmap

- Warm pane pool for the `--resume` path (cut per-call launch latency)
- Capture the work product (git diff / files touched), not just the final message
- Handle context autocompaction and rate-limit waits explicitly
- Retry/backoff on errors instead of timing out

## License

MIT — see [LICENSE](LICENSE).
