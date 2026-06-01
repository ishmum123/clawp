# Spec: `clawp` — subscription-billed drop-in for `claude -p`

Status: draft. Design + facts verified against live Claude Code runs (v2.1.159, macOS, tmux 3.5a). All four original open questions resolved (see Resolved questions).

## Scope revision (authoritative)

`clawp` **is** the drop-in for `claude -p` — not a `-p` mode added alongside the old interface. **All legacy is removed**: the scratch-file mechanism (`INSTRUCTION`/`NUDGE` injection, the old `.cw/` dir, `file_ready`/`read_reply`, file-first `run_turn`, nudging) and the `ask`/`stop` subcommand framing are deleted. The transcript-based, verbatim-prompt capture is the *only* implementation. Where this document says `clawp -p`, read it as "clawp" — `-p`/`--print` is accepted as a harmless synonym but is not required.

## Goal

`clawp [flags] [prompt]` (and `echo prompt | clawp`) behaves like `claude -p`, but runs through the interactive TUI so it bills against the **Claude subscription**, not the Agent SDK credit pool that `claude -p` draws from.

## Non-goals

- **No legacy compatibility in any sense** — no `ask`/`stop` subcommands, no scratch-file injection.
- `--include-partial-messages` (token-by-token streaming) — unreproducible; see Confirmed #6.
- Usage/cost reporting — dropped by decision (the data is available but out of scope).
- 100% flag parity — only the flags below are honored; unsupported ones error rather than silently no-op.

---

## Confirmed by verification

These are measured facts, not assumptions.

1. **Transcript path is deterministic from the session id.**
   `~/.claude/projects/<cwd-with-slashes-as-dashes>/<session-id>.jsonl`.
   Launching `claude --session-id <uuid> …` writes exactly that file — no discovery needed.

2. **`--session-id` works in interactive mode and is billing-neutral.**
   Verified launch was `claude --session-id <uuid> --permission-mode acceptEdits` (no `-p`) → interactive → subscription billing. The flag only names the conversation; it adds zero tokens. Reading/tailing the transcript is a passive local read (zero tokens, zero billing impact).

3. **The final answer is in the transcript, verbatim.**
   The last `assistant` message's `text` block holds the raw API answer with markdown and code fences intact (e.g. backticks preserved). This is the source of truth — **no scratch-file injection needed**, so the prompt goes out byte-verbatim.

4. **Completion marker, and the `stop_reason` taxonomy.** On assistant messages:
   - **terminal (turn done):** `end_turn`, `max_tokens`, `stop_sequence`, `refusal`
   - **continuation (keep waiting):** `tool_use` (followed by a `role:"user"` `tool_result`), and defensively `pause_turn` / `null`
   Use a **terminal whitelist**, not "anything that isn't `tool_use`" — blacklisting continuations would fire early if a new continuation type appears; whitelisting fails slow (falls through to the screen backstop) instead of wrong. Observed across normal + web-search turns: only `tool_use` and `end_turn` actually occur. `pause_turn` did **not** appear — Claude Code runs web search as a client-side `WebSearch` `tool_use` loop, not the server-side tool that emits `pause_turn`; it's whitelisted defensively only.

5. **The transcript is flushed incrementally during the turn**, not dumped at the end.
   Observed line growth at t=1,2,9,12s. → live `stream-json` is feasible by tailing from a recorded offset.

6. **Complete messages only — no partial records.** The jsonl stores whole messages, never intra-generation token deltas. → `--include-partial-messages` cannot be reproduced.

7. **Transcript interleaves harness noise with API turns.**
   Noise records to skip: `last-prompt`, `mode`, `permission-mode`, `attachment`, `file-history-snapshot`, `ai-title`, `system`, `queue-operation`, `progress`. Real turns: `user`, `assistant`, and `tool_result` (carried inside a `role:"user"` message). `assistant` messages may contain `thinking` blocks — exclude those from the final answer. **Also skip compaction records** (can appear mid-turn): `system` with `subtype: "compact_boundary"`, and `user` records flagged `isCompactSummary: true` — the latter is a synthetic summary, not the model's reply.

8. **Flag billing gate (from `claude --help`).**
   Print-only flags say "only works with --print": `--output-format`, `--input-format`, `--max-turns`, `--include-partial-messages`, `--fallback-model`, `--json-schema`, `--replay-user-messages`. The interactive TUI will not honor these → clawp must emulate or reject them, never forward them.

9. **Resume is faithful and append-only.** `claude --resume <id>` in an interactive pane reloads full prior context (verified: resumed turn answered a fact stated only before the kill) and **keeps appending to the same `<id>.jsonl`** — default resume does not fork (only `--fork-session` creates a new id/file). The first N bytes were byte-identical before/after resume (prefix hash unchanged) → stored offsets stay valid across a kill+resume.

10. **Compaction is append-only on disk.** A real ~180K-token compaction (`compactMetadata.trigger: "manual"`) kept all pre-compaction records intact, then appended a `compact_boundary` marker + an `isCompactSummary` user record + subsequent turns. History is never rewritten/truncated → **offset-forward reads survive compaction**. Manual (`/compact`) and auto differ only in `compactMetadata.trigger`; same writer path.

11. **A blocked turn is indistinguishable from a working one in the jsonl alone.** When a permission/trust dialog appears (e.g. `acceptEdits` doesn't cover web search/bash), the last record is a normal-looking `assistant` `stop_reason: "tool_use"` and no `end_turn` ever arrives. → transcript-only detection would hang; the screen backstop is **required**, not optional.

---

## CLI surface

`clawp` takes `clawp [flags] [prompt]`, mirroring `claude [options] [prompt]`. No subcommands. The single exception is the `--history` viewer flag (clawp's own log reader).

```
clawp "prompt"                        # text output, fresh session, stdout
clawp -p "prompt"                     # -p/--print accepted as a no-op synonym
echo "prompt" | clawp                 # prompt from stdin
clawp --output-format json "..."      # single JSON result object
clawp --output-format stream-json "..."   # NDJSON event stream
clawp --resume <session-id> "..."     # continue a specific session
clawp --model opus "..."              # pass-through session flag
clawp --history [-n N]                # view recent logged turns from clawp.sqlite
```

**Prompt source:** positional arg, else stdin (matches `claude -p`). If both empty → error (exit 2). `--history` takes no prompt.

**stdout/stderr contract:** final result to stdout only; status/diagnostics to stderr. The session-id is printed to stderr (and is the `session_id` field in json mode) so callers can resume.

### Flags

| Flag | Handling |
|------|----------|
| `-p, --print` | accepted, no-op (clawp is always print) |
| `--output-format text\|json\|stream-json` | emulated from the transcript (see Output modes) |
| `--resume <id>` | resume a specific session (see Lifecycle); `-c`/`--continue` is **not** supported |
| `--session-id <uuid>` | use this id instead of a generated one |
| `--full-auto` | `bypassPermissions` (default is `acceptEdits`) |
| `--cwd <dir>` | directory claude runs in (default: cwd); long-form only, no `-c` short alias |
| `--db <path>` | sqlite path (default `./clawp.sqlite`) |
| `--history`, `-n <N>` | view recent logged turns instead of running a prompt |
| `--model`, `--add-dir`, `--system-prompt`, `--append-system-prompt`, `--allowedTools`, `--disallowedTools`, `--permission-mode`, `--mcp-config`, `--agents`, `--settings` | **pass through** to the interactive launch |
| `--input-format`, `--max-turns`, `--include-partial-messages`, `--fallback-model`, `--json-schema`, `--replay-user-messages` | **error (exit 2)**: print-only feature unsupported by clawp |
| unknown flag | error, do not ignore |

---

## Session lifecycle

**Default `clawp -p` is stateless** (matches `claude -p`): each call is a fresh conversation.

- **v1 — ephemeral per call (correctness first):** generate a uuid → launch `claude --session-id <uuid> <passthrough flags>` in a detached tmux pane → run the turn → kill the pane. The conversation persists on disk under `<uuid>.jsonl`, so it remains resumable. Deterministic id, no discovery, crash-safe. Cost: launch + readiness latency every call.
- **`--resume <id>` / `-c`:** relaunch `claude --resume <id>` (or most-recent for `-c`) in a pane, run, kill. Resumed turns reload prior context (cached where possible) — inherent to continuation.

**Warm pool — later optimization, not v1.** A pool of live panes avoids per-call launch latency. It maps cleanly onto the **stateful/resume** path (a pane stays bound to one session-id). It does *not* fit the stateless default cleanly: a live pane carries one session-id for its life, so reusing it for a fresh stateless call needs `/clear` (which starts a new, auto-assigned id → reintroduces discovery). Decision: ship ephemeral first; add a warm pool for the resume path once v1 is proven.

**Concurrency:** unique session-id per call → unique tmux name → clawp's per-session flock no longer serializes independent calls. `clawp -p` fan-out becomes possible, bounded by machine resources.

---

## Output modes

All three are built by reading/tailing the transcript jsonl (Confirmed #1, #3, #5, #7). No prompt injection.

- **text** (default): on turn-done, print the final assistant `text` block(s) to stdout, verbatim.
- **json**: emit one object on completion:
  ```json
  {"type":"result","subtype":"success","session_id":"<uuid>",
   "result":"<final text>","is_error":false,"duration_ms":<measured>}
  ```
  Real fields: `session_id` (clawp assigns it), `duration_ms` (clawp wall-clock), `result`, `is_error`/`subtype`. Usage/cost intentionally omitted.
- **stream-json**: tail the transcript from the offset recorded before sending; map each real record to an NDJSON event (`user` / `assistant` / `tool_result`), filtering noise + compaction records (#7) and `thinking` blocks; **terminate on the terminal `stop_reason` (#4), not on "file stopped growing"** — trailing `system`/summary records are appended *after* `end_turn` (see Capture algorithm) and must not leak into the stream. Live, because of #5. Does **not** emit token-level partials (#6).

---

## Completion & capture algorithm

1. Resolve transcript path from the (assigned or resumed) session-id (#1).
2. **Record the offset at send time** — current EOF (line/byte) of the jsonl, *immediately before* sending (0 if new). Taking it at send time (not turn-end) naturally swallows the trailing `system`/summary records the *previous* turn appended after its `end_turn`. Offset is safe across kill+resume (#9) and compaction (#10).
3. Send the prompt verbatim via bracketed paste (clawp's existing `send_text`).
4. Tail the jsonl from the offset, skipping noise + compaction records (#7). For stream-json, emit mapped events as they arrive.
5. **Done — dual signal** (a stuck `tool_use` is indistinguishable from a working one in the jsonl, #11):
   - **fast/clean path:** an `assistant` record after our prompt has a **terminal** `stop_reason` from the whitelist (#4). Capture its `text` block(s) (excluding `thinking`) as the answer.
   - **backstop:** screen goes idle (clawp's `at_idle_prompt` + no spinner) with no new transcript activity for a stability window → handles unknown future `stop_reason`s (fail-slow) and dialogs.
6. **Blocked / stall:** a permission/trust dialog produces no `end_turn` (#11). clawp's `looks_blocked`/spinner/stall detection → report `blocked`/`timed_out`, exit non-zero. `--full-auto` (bypassPermissions) is required for unattended runs that may touch web/bash.
7. **Schema-break trigger:** reached idle/turn-end but parsed **zero** usable assistant text from the new records → invoke the format-coupling fallback (see below).

---

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | success (turn-done captured) |
| 2 | usage error (no prompt, unsupported flag) |
| 3 | blocked (permission/trust dialog, not in `--full-auto`) |
| 4 | timeout / stall (no terminal stop_reason within caps) |
| 5 | launch/session failure |

(`claude -p` exits non-zero on error; clawp currently always exits 0 — this must change for print mode.)

---

## Transcript format coupling

The jsonl schema is undocumented and internal — a Claude Code update could rename/restructure it. Posture: **loud-fail + scrape fallback (text only) + version in diagnostics**, plus two design rules that shrink the blast radius.

**Coupling surface (depend on nothing else):**
- path: `~/.claude/projects/<cwd-dashed>/<session-id>.jsonl`, append-only NDJSON
- per record: `type`; for turns a `message` with `role`, `content[]`, `stop_reason`
- block types read: `text`, `thinking` (exclude), `tool_use` / `tool_result` (continuation signal)
- the terminal `stop_reason` whitelist (#4); `subtype: "compact_boundary"` + `isCompactSummary` (skip, #7/#10)

**Policy:**
1. **Defensive parse, loud fail (mandatory).** Validate only the fields above. Never emit empty/wrong output silently — a drop-in that lies is the worst outcome.
2. **Degradation by mode** when the schema-break trigger fires (Capture step 7):
   - **text** → fall back to clawp's existing `scrape_reply()`, flag the turn `low_fidelity`.
   - **json / stream-json** → loud error (can't be faithfully scraped).
3. **Version in diagnostics, not behavior gating.** Capture the Claude Code version (visible in the pane banner) and include it in the error/flag (e.g. "transcript schema unrecognized on Claude Code v2.1.159"). Do **not** hard-pin — claude updates too often.

**Design rules:**
- **Minimal field dependency** — parse only the listed fields; ignore the rest so unrelated schema changes don't touch clawp.
- **Single localized parser** — keep all transcript-format knowledge in one module/function (with the scrape fallback beside it); a future schema change is then a one-place fix.

---

## Resolved questions

All four original open questions are closed by the Confirmed items above:

| # | Question | Outcome |
|---|----------|---------|
| 1 | Resume fidelity (context + same file) | ✅ Confirmed #9 — context restored, same file, no fork, append-only |
| 2 | Non-`end_turn` terminals | ✅ Confirmed #4 — only `tool_use`/`end_turn` occur; terminal whitelist + dual-signal; `pause_turn` absent (web search is client `tool_use`) |
| 3 | Offset isolation across resume / compaction | ✅ Confirmed #9 + #10 — append-only across both; offset taken at send time |
| 4 | Format-coupling risk | Posture decided — see Transcript format coupling |

**Residual (not blocking v1):** live append *during* a compaction event was inferred from the on-disk artifact (#10), not watched live; a `/compact` run could confirm belt-and-suspenders.

---

## Build order (smallest provable slice first)

1. **`clawp -p "prompt"` / stdin → text, ephemeral session, transcript capture, exit codes.** Prove: `out=$(clawp -p "…")` returns the verbatim answer, `$?` is correct, subscription billed / credit pool untouched.
2. **`--resume`/`-c`** on the ephemeral path (resume fidelity verified, #9).
3. **`--output-format json`**.
4. **`--output-format stream-json`** (live tail).
5. **Pass-through session flags** (`--model`, etc.) with hard errors on print-only flags.
6. **Warm pool** for the resume path (latency optimization).
