# Design: `--keep-alive` (warm pane reuse for `--resume`)

Status: **superseded by [stream-json-input.md](stream-json-input.md)** — the warm pane
now falls out of process lifetime (one clawp process per conversation, stdin = the
turns) rather than a flag, which also removes the reaping/idle-sweep problem. Kept for
context.

## Problem

`run_print_turn` kills the tmux pane in its `finally` on **every** call
(`clawp.py:500`), so nothing stays warm. Each `--resume` turn pays a full
`claude` TUI boot (up to `READY_TIMEOUT`, ~seconds). For a chatty caller
(e.g. a conversational server) that per-turn launch latency dominates.

The reuse machinery already exists: the pane is named deterministically
`clawp-{session_id[:8]}`, and `ensure_session` no-ops when that session is
already alive (`clawp.py:159`). The only thing forcing a relaunch is the
unconditional kill.

## Patch

Two changes — gate the kill behind a flag:

```python
# build_parser()
ap.add_argument("--keep-alive", action="store_true",
                help="leave the tmux pane running for fast --resume reuse")

# run_print_turn(), finally at clawp.py:496
        finally:
            seconds = round(time.time() - t0, 1)
            _log_turn(db_path, turn_id, session_id, args.prompt,
                      answer or "", seconds, meta)
            if not args.keep_alive:           # <-- only behavioral change
                tmux("kill-session", "-t", name)
```

## Flow

```
turn 0:  clawp --session-id <id> --keep-alive  "…"   # launches, leaves pane up
turn 1+: clawp --resume     <id> --keep-alive  "…"   # has_session hit → no boot
```

`session_lock` still serializes turns per pane, so concurrency safety is
unchanged.

## Caveats

- **Pane leak.** Panes now outlive the call; someone must reap them. Options:
  caller kills on teardown (`tmux kill-session -t clawp-<id8>`), or clawp grows
  a TTL reaper that kills panes idle past a threshold at next launch.
- **First call still boots.** Only turn 2+ is fast; pair with pre-warming the
  session when the caller knows a conversation is starting.
- **Reused pane ignores later `claude_args`.** On reuse `ensure_session`
  returns early, so flags only take effect at first launch. Correct for
  `--resume` (same session config), but a footgun if a caller varies
  passthrough flags mid-conversation.

## Caller side (e.g. Consult `server.clawp.js`)

Reap on disconnect so killed connections don't leak panes:

```js
// cleanup()
import { execFile } from "child_process";
execFile("tmux", ["kill-session", "-t", `clawp-${sessionId.slice(0, 8)}`],
         () => {}); // best effort
```
