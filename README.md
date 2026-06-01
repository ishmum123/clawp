# cw

Drive the interactive `claude` CLI through tmux and capture its replies into SQLite — so scripted use bills against your **Claude subscription** instead of the Agent SDK credit pool that `claude -p` draws from (as of 2026-06-15).

`cw` never calls `claude -p`. It runs the normal interactive TUI inside a tmux session, sends prompts, and has claude write its raw answer to a scratch file that `cw` ingests into SQLite at full fidelity.

## Requirements

- [`claude`](https://claude.com/claude-code) (Claude Code), logged in and on your `PATH`
- `tmux`
- `python3` (standard library only)

## Install

It's a single file:

```sh
chmod +x cw.py && ln -s "$PWD/cw.py" /usr/local/bin/cw
```

…or just run `python3 cw.py …`.

## Usage

```sh
cw ask "summarize the difference between TCP and UDP"
cw ask "explain this stack trace: …" -s debugging        # named session, own context
cw ask "add type hints to calc.py" -c ~/proj --full-auto # run tools/edits unattended
cw history -n 20                                          # recent stored turns
cw stop -s debugging                                      # end a session
```

Replies print to stdout, status to stderr, and every turn is stored in `cw.sqlite`.

### Options (`ask`)

| flag | meaning |
|------|---------|
| `-s, --session NAME` | Conversation name (default `cw`). Each is a persistent claude session with its own context, reused across calls. |
| `-c, --cwd DIR` | Directory claude runs in (default: current). Gives claude that project's files. |
| `-d, --db PATH` | SQLite file (default `./cw.sqlite`). |
| `--full-auto` | Launch with `bypassPermissions` — claude runs commands/edits without asking. Only use on prompts and projects you trust. |

## How it works

1. Launches `claude --permission-mode acceptEdits` in a detached tmux pane (interactive → subscription billing).
2. Sends your prompt plus a one-line instruction to `Write` the full raw reply to a per-turn file.
3. Watches for that file. The on-screen spinner distinguishes "still working" from "idle"; if claude goes idle without writing, `cw` nudges it.
4. Reads the file (raw markdown, code intact) and stores it in SQLite.

The file side channel exists because scraping the rendered terminal loses markdown and code formatting — the file holds exactly what claude produced.

Stored columns: `id, ts, session, prompt, reply, seconds, via, nudges, low_fidelity, timed_out, blocked, note`. `via=file` is a clean capture; `via=scrape` is the lossy fallback.

## Limitations & known behavior

- **Sweet spot is Q&A and light, mostly-edit tasks.** Tool-using turns work, but `cw` rebuilds by hand — against a TUI — what `claude -p --output-format stream-json` gives natively. The more autonomous the work, the more the fragility shows.
- **Permission mode is fixed at session creation.** To switch a running session to/from `--full-auto`, `cw stop` it first.
- **First-pass write adherence isn't 100%.** Sometimes claude answers but skips the file; the nudge recovers it (~10s), shown in the `nudges` column.
- **If a permission prompt appears** (restrictive config, no `--full-auto`), the turn is reported `blocked` rather than answered. Use `--full-auto` for unattended runs.
- **One turn at a time per session** — concurrent `ask`s on one session fail fast; use different `-s` names for parallelism.
- Developed against macOS, tmux 3.5a, Claude Code v2.1.x.

## Roadmap

- Capture the work product (git diff / files touched), not just the final message
- Handle context autocompaction and rate-limit waits explicitly
- Retry/backoff on errors instead of timing out
- Slash-command support (`/clear`, `/compact`, custom commands)

## License

MIT — see [LICENSE](LICENSE).
