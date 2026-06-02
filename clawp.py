#!/usr/bin/env python3
"""clawp - a subscription-billed drop-in for `claude -p`.

Name: claw (claude wrapper) + p (the `claude -p` it drops in for).

`clawp [flags] [prompt]` (or `echo prompt | clawp`) behaves like `claude -p`:
the prompt comes from a positional arg or stdin, the answer goes to stdout, and
a clean run is silent on stderr (only errors/degraded turns are reported), with
proper exit codes. The session-id is exposed via json/stream-json output and
clawp.sqlite, not stderr. It runs the *interactive* `claude` TUI in a detached
tmux pane (NEVER `claude -p`),
so usage bills against your Claude subscription rather than the Agent SDK credit
pool that `claude -p` draws from.

How: generate (or resume) a session-id, launch `claude --session-id <uuid> ...`
in an ephemeral pane, send the prompt verbatim, and capture the answer from the
transcript jsonl claude writes to
`~/.claude/projects/*/<session-id>.jsonl`. Completion is dual-signalled: a new
assistant record with a terminal stop_reason, or a screen-idle backstop. The
pane is reaped on every exit path.

Output modes (`--output-format`): text (default), json (one result object),
stream-json (live NDJSON events). `--resume <id>` continues a prior session.
`--full-auto` runs with bypassPermissions for unattended use.

Usage:
    clawp "your prompt"
    echo "prompt" | clawp
    clawp --output-format json "..."
    clawp --output-format stream-json "..."
    clawp --resume <session-id> "..."
    clawp --model opus "..."
    clawp --history [-n 10]
"""
import argparse
import contextlib
import datetime
import fcntl
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid

POLL = 0.6              # seconds between captures
STABLE_NEEDED = 3       # consecutive identical captures => screen has gone quiet
STALL_SECS = 180        # screen unchanged this long (e.g. frozen spinner) => hang
MAX_TURN = 1800         # absolute per-turn cap (seconds)
READY_TIMEOUT = 45      # seconds to reach the idle prompt after launch
SEND_SETTLE = 0.8       # pause after send_text before polling for a reply
PANE_W, PANE_H = 220, 50

ASSIST_RE = re.compile(r"^⏺\s?")           # assistant message start (scrape fallback)
PROMPT_RE = re.compile(r"^❯\s")            # input box / echoed prompt
DONE_RE = re.compile(r"\bfor\s+\d+s\b")    # completion line: "✻ Baked for 2s"

# Phrases that mean a blocking dialog (permission / trust) owns the screen.
DIALOG_MARKERS = (
    "Do you want", "don't ask again", "tell Claude what to do differently",
    "trust this folder", "Quick safety check", "No, and tell Claude",
)

# Print-only flags (claude --help: "only works with --print"); the interactive
# TUI won't honor them, so reject loudly rather than forward.
PRINT_ONLY_FLAGS = ("--input-format", "--max-turns", "--include-partial-messages",
                    "--fallback-model", "--json-schema", "--replay-user-messages")
# Session flags forwarded verbatim to the interactive launch.
PASSTHROUGH_FLAGS = ("--model", "--effort", "--add-dir", "--system-prompt",
                     "--append-system-prompt", "--allowedTools",
                     "--disallowedTools", "--permission-mode", "--mcp-config",
                     "--agents", "--settings")


def tmux(*args, stdin_text=None):
    return subprocess.run(["tmux", *args], input=stdin_text,
                          capture_output=True, text=True)


def has_session(name):
    return tmux("has-session", "-t", name).returncode == 0


def capture(name):
    return tmux("capture-pane", "-p", "-S", "-", "-t", name).stdout


def bottom_lines(screen, n=12):
    lines = screen.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return lines[-n:]


def has_spinner(screen):
    # Active work shows a gerund line with an ellipsis ("✳ Flambéing…") and/or a
    # "⎿ Running…" tool line; the idle "done" summary ("✻ Baked for 2s") has
    # none. An ellipsis in the bottom region means claude is still going.
    return any("…" in l for l in bottom_lines(screen))


def looks_blocked(screen):
    return any(m in screen for m in DIALOG_MARKERS)


def at_idle_prompt(screen):
    # The status bar ("Model: ...") is present on a normal interactive screen
    # and absent when a full-screen dialog owns it.
    return "Model:" in screen and not looks_blocked(screen)


def ensure_session(name, cwd, claude_args):
    """Launch the claude session if absent; clear trust prompt; wait until idle.

    claude_args is the full argv after `claude` (e.g. ["--session-id", uuid,
    "--permission-mode", "acceptEdits"] or ["--resume", id, ...]).
    """
    if has_session(name):
        return
    tmux("new-session", "-d", "-s", name, "-x", str(PANE_W), "-y", str(PANE_H),
         "-c", cwd, "claude", *claude_args)
    deadline = time.time() + READY_TIMEOUT
    trusted = False
    while time.time() < deadline:
        screen = capture(name)
        if not trusted and ("trust this folder" in screen
                            or "Quick safety check" in screen):
            tmux("send-keys", "-t", name, "Enter")   # default = "Yes, I trust"
            trusted = True
            time.sleep(1.0)
            continue
        if at_idle_prompt(screen) and not has_spinner(screen):
            return
        time.sleep(POLL)
    raise TimeoutError(f"claude session '{name}' did not become ready in "
                       f"{READY_TIMEOUT}s")


def clear_input(name):
    # Drop any ghost prompt-suggestion or stray text so the paste lands clean.
    tmux("send-keys", "-t", name, "Escape")
    tmux("send-keys", "-t", name, "C-u")


def send_text(name, text):
    clear_input(name)
    tmux("load-buffer", "-", stdin_text=text)
    tmux("paste-buffer", "-p", "-t", name)   # -p = bracketed paste (multiline-safe)
    time.sleep(0.4)
    tmux("send-keys", "-t", name, "Enter")


def scrape_reply(screen):
    """Lossy fallback: pull the last assistant block out of a rendered capture."""
    lines = screen.splitlines()
    idx = next((i for i, l in enumerate(lines) if ASSIST_RE.match(l)), None)
    if idx is None:
        return ""
    out = [ASSIST_RE.sub("", lines[idx], count=1)]
    for l in lines[idx + 1:]:
        s = l.strip()
        if (s.startswith("────") or PROMPT_RE.match(s) or DONE_RE.search(s)
                or s.startswith("Model:") or "-- INSERT --" in s):
            break
        out.append(l[2:] if l.startswith("  ") else l)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out).strip()


# --- transcript parser -------------------------------------------------------
# All jsonl-schema coupling lives here (beside scrape_reply); a claude format
# change is a one-place fix.

TERMINAL_STOP = {"end_turn", "max_tokens", "stop_sequence", "refusal"}
CONTINUATION_STOP = {"tool_use", "pause_turn", None}   # whitelist terminal, not this
NOISE_TYPES = {"last-prompt", "mode", "permission-mode", "attachment",
               "file-history-snapshot", "ai-title", "system", "queue-operation",
               "progress"}

VERSION_RE = re.compile(r"Claude Code v([\d.]+)")


def find_transcript(session_id):
    # session-id is globally unique, so a uuid glob beats encoding cwd->dashes.
    hits = glob.glob(os.path.expanduser(
        f"~/.claude/projects/*/{session_id}.jsonl"))
    return hits[0] if hits else None


def read_records(path):
    # Writer flushes incrementally, so a tail read can catch a half-written
    # final record; stop at the first undecodable line.
    out = []
    if not path or not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                break
    return out


def is_compaction(rec):
    if rec.get("subtype") == "compact_boundary":
        return True
    return bool(rec.get("isCompactSummary"))


def is_noise(rec):
    return rec.get("type") in NOISE_TYPES or is_compaction(rec)


def is_terminal_assistant(rec):
    if rec.get("type") != "assistant":
        return False
    return (rec.get("message") or {}).get("stop_reason") in TERMINAL_STOP


def assistant_text(rec):
    # Exclude thinking blocks; only text blocks are the answer.
    blocks = (rec.get("message") or {}).get("content") or []
    parts = [b.get("text", "") for b in blocks
             if isinstance(b, dict) and b.get("type") == "text"]
    return "".join(parts).strip()


def final_answer(records):
    for rec in reversed(records):
        if is_terminal_assistant(rec):
            return assistant_text(rec)
    return ""


def stream_event(rec):
    if is_noise(rec):
        return None
    t = rec.get("type")
    if t == "assistant":
        text = assistant_text(rec)
        if not text:               # tool_use-only / thinking-only: nothing to emit
            return None
        return {"type": "assistant", "text": text}
    if t == "user":
        msg = rec.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content):
            return {"type": "tool_result"}
        return None                # echoed user prompt: skip
    return None


def claude_version(screen):
    # Best-effort scrape of the banner for diagnostics; "" if not present.
    m = VERSION_RE.search(screen)
    return m.group(1) if m else ""


# --- end transcript parser ---------------------------------------------------


@contextlib.contextmanager
def session_lock(name):
    """Serialize turns per pane; a second concurrent run fails fast."""
    path = os.path.join("/tmp", f"clawp-{name}.lock")
    f = open(path, "w")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(f"[clawp] pane '{name}' is busy (another clawp is "
                             "running against it)")
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def init_db(db_path):
    con = sqlite3.connect(db_path)
    con.execute("""CREATE TABLE IF NOT EXISTS responses(
        id TEXT PRIMARY KEY, ts TEXT, session TEXT, prompt TEXT, reply TEXT,
        seconds REAL, low_fidelity INTEGER, timed_out INTEGER, blocked INTEGER,
        note TEXT)""")
    con.commit()
    return con


def _meta(low_fidelity, timed_out, blocked=False, note=""):
    return {"low_fidelity": int(low_fidelity), "timed_out": int(timed_out),
            "blocked": int(blocked), "note": note}


def _log_turn(db_path, turn_id, session_id, prompt, reply, seconds, meta):
    con = init_db(db_path)
    con.execute("INSERT INTO responses VALUES (?,?,?,?,?,?,?,?,?,?)", (
        turn_id, datetime.datetime.now(datetime.timezone.utc).isoformat(),
        session_id, prompt, reply, seconds, meta["low_fidelity"],
        meta["timed_out"], meta["blocked"], meta["note"]))
    con.commit()
    con.close()


class CwError(SystemExit):
    def __init__(self, code, msg):
        super().__init__(code)
        sys.stderr.write(f"[clawp] {msg}\n")


def _passthrough_args(args):
    out = []
    for flag in PASSTHROUGH_FLAGS:
        val = getattr(args, flag.lstrip("-").replace("-", "_"))
        if val is not None:
            out += [flag, val]
    return out


def run_print_turn(args, cwd, db_path):
    fresh = args.resume is None
    session_id = args.session_id or args.resume or str(uuid.uuid4())
    name = "clawp-" + session_id[:8]
    mode = "bypassPermissions" if args.full_auto else "acceptEdits"

    claude_args = ["--session-id", session_id] if fresh \
        else ["--resume", session_id]
    # explicit --permission-mode passthrough overrides the default mode.
    if args.permission_mode is None:
        claude_args += ["--permission-mode", mode]
    claude_args += _passthrough_args(args)

    fmt = args.output_format
    stream = fmt == "stream-json"
    t0 = time.time()
    turn_id = (datetime.datetime.now().strftime("%Y%m%d-%H%M%S-")
               + uuid.uuid4().hex[:4])
    answer = None
    meta = _meta(False, False)
    seconds = 0.0
    with session_lock(name):
        try:
            try:
                ensure_session(name, cwd, claude_args)
            except TimeoutError as e:
                meta = _meta(False, True, note="launch failed")
                raise CwError(5, str(e))

            path = find_transcript(session_id)
            # Offset (record count, consistent with read_records) taken at send
            # time swallows the previous turn's trailing system/summary records;
            # file may not exist yet on a fresh session (offset 0).
            offset = len(read_records(path))

            send_text(name, args.prompt)
            time.sleep(SEND_SETTLE)

            start = time.time()
            last_screen = None
            stable = 0
            last_change = start
            seen = offset
            while time.time() - start < MAX_TURN:
                if path is None:
                    path = find_transcript(session_id)
                records = read_records(path)
                new = records[seen:]
                # Stop AT the terminal record: a record flushed after end_turn
                # in the same batch must not leak into the stream.
                done = False
                for rec in new:
                    seen += 1
                    if is_terminal_assistant(rec):
                        answer = assistant_text(rec)
                        if stream:
                            ev = stream_event(rec)
                            if ev is not None:
                                print(json.dumps(ev), flush=True)
                        done = True
                        break
                    if stream:
                        ev = stream_event(rec)
                        if ev is not None:
                            print(json.dumps(ev), flush=True)
                if done:
                    break

                screen = capture(name)
                now = time.time()
                if screen == last_screen:
                    stable += 1
                else:
                    stable = 1
                    last_screen = screen
                    last_change = now
                if looks_blocked(screen):
                    meta = _meta(False, False, blocked=True, note="blocked")
                    raise CwError(3, "blocked: permission/trust dialog "
                                  "(use --full-auto for unattended runs)")
                if now - last_change >= STALL_SECS:
                    meta = _meta(False, True, note="stalled")
                    raise CwError(4, "stall: screen frozen, no terminal "
                                  "stop_reason")
                if has_spinner(screen):
                    time.sleep(POLL)
                    continue
                # idle backstop: quiet, idle, no spinner, no terminal record.
                if stable >= STABLE_NEEDED and at_idle_prompt(screen):
                    break
                time.sleep(POLL)
            else:
                meta = _meta(False, True, note="max turn")
                raise CwError(4, "timeout: turn exceeded MAX_TURN")

            # Schema-break fallback: settled but parsed no usable answer. Slice
            # by offset so a resumed/multi-turn transcript can't return a PRIOR
            # turn's answer.
            if not answer:
                answer = final_answer(read_records(path)[offset:])
            if not answer:
                ver = claude_version(capture(name))
                vsfx = f" on Claude Code v{ver}" if ver else ""
                if fmt == "text":
                    answer = scrape_reply(capture(name))
                    meta = _meta(True, False, note="schema unrecognized" + vsfx)
                else:
                    raise CwError(5, "transcript schema unrecognized" + vsfx
                                  + " (zero usable answer; json/stream-json "
                                  "cannot be faithfully scraped)")
        finally:
            seconds = round(time.time() - t0, 1)
            _log_turn(db_path, turn_id, session_id, args.prompt, answer or "",
                      seconds, meta)
            tmux("kill-session", "-t", name)

    # Clean text runs stay silent on stderr, matching `claude -p`; the session-id
    # lives in the json/stream-json output and clawp.sqlite. Only surface stderr
    # when the turn was degraded (schema fallback / low fidelity).
    if meta["low_fidelity"] or meta["note"]:
        sys.stderr.write(f"[clawp] {meta['note'] or 'low_fidelity'} "
                         f"(session={session_id})\n")
    if fmt == "text":
        print(answer)
    else:
        print(json.dumps({"type": "result", "subtype": "success",
                          "session_id": session_id, "result": answer,
                          "is_error": False,
                          "duration_ms": int(seconds * 1000)}), flush=stream)
    return 0


def show_history(db_path, n):
    if not os.path.exists(db_path):
        sys.stderr.write("[clawp] no database yet\n")
        return 0
    con = sqlite3.connect(db_path)
    q = ("SELECT ts, session, seconds, low_fidelity, timed_out, blocked, "
         "substr(prompt,1,70) FROM responses ORDER BY ts DESC LIMIT ?")
    for ts, sess, secs, low, to, blk, p in con.execute(q, (n,)):
        flags = [f for f, v in (("low_fidelity", low), ("timed_out", to),
                                ("blocked", blk)) if v]
        sfx = f"  [{', '.join(flags)}]" if flags else ""
        print(f"{ts}  {sess}  {secs}s{sfx}  {p!r}")
    con.close()
    return 0


def main():
    ap = argparse.ArgumentParser(
        prog="clawp", description="subscription-billed drop-in for `claude -p`")
    ap.add_argument("prompt", nargs="?")
    ap.add_argument("-p", "--print", action="store_true",
                    help="accepted no-op (clawp is always print)")
    ap.add_argument("--output-format", choices=("text", "json", "stream-json"),
                    default="text")
    ap.add_argument("--resume")
    ap.add_argument("--session-id")
    ap.add_argument("--full-auto", action="store_true",
                    help="bypassPermissions (default acceptEdits)")
    # No -c alias: -c/--continue is out of scope, so it must not silently bind
    # to --cwd. --cwd stays long-form only.
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("-d", "--db", default=os.path.join(os.getcwd(), "clawp.sqlite"))
    ap.add_argument("--history", action="store_true",
                    help="view recent logged turns instead of running")
    ap.add_argument("-n", type=int, default=10, help="rows for --history")
    for flag in PASSTHROUGH_FLAGS:
        ap.add_argument(flag)
    # These claude flags only function under `claude -p`; clawp exits 2 on them.
    for flag in PRINT_ONLY_FLAGS:
        ap.add_argument(flag, dest="_unsupported_" + flag.lstrip("-"),
                        nargs="?", const=True, default=None)

    args = ap.parse_args()

    if args.history:
        raise SystemExit(show_history(args.db, args.n))

    for flag in PRINT_ONLY_FLAGS:
        if getattr(args, "_unsupported_" + flag.lstrip("-")) is not None:
            raise CwError(2, f"{flag}: unsupported by clawp (print-only feature)")

    if not args.prompt or args.prompt == "-":
        if sys.stdin.isatty():
            raise CwError(2, "no prompt (positional arg or stdin)")
        args.prompt = sys.stdin.read().strip()
        if not args.prompt:
            raise CwError(2, "no prompt (positional arg or stdin)")

    raise SystemExit(run_print_turn(args, args.cwd, args.db))


if __name__ == "__main__":
    main()
