# Design: `--input-format stream-json` (claude-compatible streaming input)

Status: approved, building. **Supersedes `keep-alive.md`** — warmth now falls out of
process lifetime, not a flag.

## Goal

Make clawp speak `claude -p`'s streaming-input protocol: one long-running clawp
process reads NDJSON user turns from stdin, drives ONE warm tmux pane for the whole
conversation, emits claude-shaped stream-json, and reaps the pane when stdin closes.
Still subscription-billed. A consumer written for
`claude -p --input-format stream-json --output-format stream-json` works against
clawp by swapping the binary.

This replaces `--keep-alive`: process lifetime = conversation lifetime, so warmth,
teardown, and reaping all fall out of the pipe — no flag, no idle sweep. If the
parent dies, stdin hits EOF and clawp reaps the pane on its way out.

## Scope: sequential text turns only

clawp drives the interactive TUI, so it implements the request→response *subset* of
streaming-input mode. These are walls (TUI consequences), not omissions — state them
in `--help`:

- **No token streaming** — clawp reads completed transcript records; finest grain is
  a whole message.
- **No queuing / interrupts** — turns run one at a time; clawp blocks on each.
- **No image content** — can't paste images into a TUI.
- **No usage/cost** in `result` — clawp isn't the API.
- **Mid-stream block/stall ends the whole session** — a permission/trust dialog owns
  the pane and clawp can't answer it, so the next paste would land *in the dialog*.
  Emit an error result and reap.
- **A prompt starting with `/` or `!` is refused** (`prefix_rejection`) — the TUI reads
  `/` as a command and `!` as a shell command that would execute on the host;
  `claude -p` does neither. `@`/`#` and mid-text are fine. Streaming refuses per turn
  and the conversation continues; single-shot exits 2.

## Protocol

Input (one JSON object per stdin line):

```json
{"type":"user","message":{"role":"user","content":"text…"},"parent_tool_use_id":null}
```

`content`: string, or a content-block array (text blocks joined, non-text dropped).
Non-`user` / malformed lines ignored. EOF ends the session.

Output, claude-shaped:

- once at start: `{"type":"system","subtype":"init","session_id":…,"model":…,"cwd":…}`
- per turn: a *complete* envelope wrapping the whole answer as one delta —
  `message_start` → `content_block_start` → one `content_block_delta`
  (`text_delta` = full text) → `content_block_stop` → `message_stop`, then
  `{"type":"result","subtype":"success","session_id":…,"result":…}`.

Full envelope (not a lone delta) so a real SSE consumer's state machine doesn't
desync.

## clawp.py changes

1. Extract the per-turn capture/emit body from `run_print_turn` into `_run_turn(...)`,
   callable repeatedly against a live pane. Single-shot path calls it once — behavior
   unchanged.
2. `--input-format stream-json`: `ensure_session` once → emit init → `for line in
   sys.stdin:` parse → `_run_turn` → emit per-turn result → on EOF, kill pane once +
   log. `session_lock` held for the whole process.
3. Make `--input-format` a real arg (drop from `PRINT_ONLY_FLAGS`); keep
   `--include-partial-messages` / `--replay-user-messages` rejected.
4. **Issue-1 fix** (mid-turn block detection): a real dialog owns the screen — a
   marker present AND the idle status bar gone. Stateless `turn_blocked(screen) =
   looks_blocked(screen) and not has_idle_bar(screen)`, so neither a prior turn's
   marker left in scrollback nor the current answer quoting "Do you want…" false-
   blocks. The idle backstop likewise keys on `has_idle_bar` + `seen > offset` (a
   record arrived this turn) so a warm pane's stale pre-send screen can't settle
   into a false completion. Also fixes this latent bug in the single-shot path. No
   baseline snapshot needed — simpler than first sketched.
5. Long-session perf: read the transcript incrementally (seek from last offset) and
   bound the capture window, so an N-turn chat isn't quadratic.

## Predicted problems → handling

| Problem | Handling |
|---|---|
| Token streaming | Out of scope; whole answer in one well-formed delta envelope. |
| Queuing / interrupts | Out of scope; sequential, documented. |
| Mid-stream block / stall | Ends the session (error result, reap). |
| Command-prefix content | Confirmed live: the TUI eats a leading `/` (hangs, no reply) and *executes* a leading `!` as a shell command (an RCE `claude -p` does **not** have). `prefix_rejection` refuses `/` and `!` before send; `@`/`#` and mid-text slashes deliver fine. |
| Images / non-text blocks | Dropped; text joined. |
| usage / cost absent | Documented gap. |
| Lone-delta desync | Emit full `message_start`…`message_stop` envelope. |
| Long-session perf | Incremental transcript read + bounded capture. |
| Live-TUI multi-turn edges | `clear_input` each turn; covered by the live adversarial test. |
| Consumer rewrite / refactor regression | server.js-shape consumer; keep single-shot identical; add parser unit tests. |

## Validation

- **Unit (`test_clawp.py`):** NDJSON line → extracted prompt — string content,
  text-block content, malformed/non-`user`/empty → ignored.
- **Live (manual):** pipe two user messages; turn-1 answer contains "Do you want" →
  turn-2 returns its *own* answer (not blocked, not stale), pane dies on stdin EOF.
  Add a `/`-leading turn for command-prefix handling.
