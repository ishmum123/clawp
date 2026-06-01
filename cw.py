#!/usr/bin/env python3
"""cw - drive the interactive `claude` TUI through tmux, capture full-fidelity
replies via a side channel, and store them in sqlite.

Why a side channel: scraping the TUI yields *rendered* text (markdown/code is
mangled). Instead we append an instruction to each prompt telling claude to
Write its raw verbatim answer to a per-turn scratch file; the wrapper reads that
file (full fidelity) and inserts it into sqlite with a parameterized query.

Billing: runs claude in normal interactive mode (never `-p`), so usage counts
against your Claude subscription, not the Agent SDK credit pool.

Permissions: launches with `--permission-mode acceptEdits` so file writes land
without a prompt. For unattended command-heavy work use `--full-auto`
(`bypassPermissions`) - claude then runs everything without asking, so only
point it at prompts/projects you trust. If a permission dialog does appear and
full-auto is off, the turn is reported `blocked` rather than hanging or having
nudge keystrokes typed into the dialog.

Completion: the scratch file is the signal (cheap stat, unique per turn, full
fidelity). The screen is consulted only when the file is missing: the spinner
animates while claude works, so a stable screen at the idle prompt with no
spinner means it went idle without writing -> nudge (after a floor). A frozen
screen (no change for STALL_SECS) is treated as a hang.

Usage:
    python3 cw.py ask "your prompt"            # create/reuse default session
    python3 cw.py ask "prompt" -s name         # named session
    python3 cw.py ask "prompt" --full-auto     # bypass all permission prompts
    python3 cw.py history [-n 10] [-s name]    # recent stored turns
    python3 cw.py stop [-s name]               # kill the session
"""
import argparse
import contextlib
import datetime
import fcntl
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid

POLL = 0.6              # seconds between captures
STABLE_NEEDED = 3       # consecutive identical captures => screen has gone quiet
NUDGE_FLOOR = 10        # seconds after sending before any nudge is allowed
STALL_SECS = 180        # screen unchanged this long (e.g. frozen spinner) => hang
MAX_TURN = 1800         # absolute per-turn cap (seconds)
READY_TIMEOUT = 45      # seconds to reach the idle prompt after launch
MAX_NUDGES = 2          # times to re-ask if idle without a written file
PANE_W, PANE_H = 220, 50

ASSIST_RE = re.compile(r"^⏺\s?")           # assistant message start (scrape fallback)
PROMPT_RE = re.compile(r"^❯\s")            # input box / echoed prompt
DONE_RE = re.compile(r"\bfor\s+\d+s\b")    # completion line: "✻ Baked for 2s"

# Phrases that mean a blocking dialog (permission / trust) owns the screen.
DIALOG_MARKERS = (
    "Do you want", "don't ask again", "tell Claude what to do differently",
    "trust this folder", "Quick safety check", "No, and tell Claude",
)

INSTRUCTION = (
    "\n\n[wrapper instruction] When you have completely finished answering "
    "everything above, use the Write tool to save your full answer to {path} "
    "as raw, verbatim markdown (exactly what you would display, including any "
    "code fences and backticks). Overwrite the file if it exists, put nothing "
    "else in it, and do this as your very last action."
)
NUDGE = (
    "[wrapper] If you have finished answering, use the Write tool now to save "
    "your full answer to {path} as raw verbatim markdown (overwrite it, nothing "
    "else) as your final action."
)


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


def ensure_session(name, cwd, permission_mode):
    """Create the claude session if absent; clear trust prompt; wait until idle."""
    if has_session(name):
        return
    tmux("new-session", "-d", "-s", name, "-x", str(PANE_W), "-y", str(PANE_H),
         "-c", cwd, "claude", "--permission-mode", permission_mode)
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


def file_ready(path):
    return os.path.exists(path) and os.path.getsize(path) > 0


def read_reply(path):
    with open(path, encoding="utf-8") as f:
        return f.read().rstrip("\n")


def _meta(via, nudges, low_fidelity, timed_out, blocked=False, note=""):
    return {"via": via, "nudges": nudges, "low_fidelity": int(low_fidelity),
            "timed_out": int(timed_out), "blocked": int(blocked), "note": note}


def run_turn(name, file_path):
    """Wait for the turn to settle. Returns (reply_text, meta)."""
    start = time.time()
    last = None
    stable = 0
    nudges = 0
    last_change = start
    while time.time() - start < MAX_TURN:
        if file_ready(file_path):                       # file-first: happy path
            return read_reply(file_path), _meta("file", nudges, False, False)
        screen = capture(name)
        now = time.time()
        if screen == last:
            stable += 1
        else:
            stable = 1
            last = screen
            last_change = now
        if now - last_change >= STALL_SECS:             # frozen => hang
            return scrape_reply(screen), _meta("scrape", nudges, True, True,
                                               note="stalled")
        if has_spinner(screen):                         # still working
            time.sleep(POLL)
            continue
        if stable >= STABLE_NEEDED:                     # screen quiet, not working
            if not at_idle_prompt(screen):              # a dialog/unknown owns it
                return scrape_reply(screen), _meta("scrape", nudges, True, False,
                                                   blocked=True, note="not at idle prompt")
            if now - start >= NUDGE_FLOOR:
                if nudges < MAX_NUDGES:
                    send_text(name, NUDGE.format(path=file_path))
                    nudges += 1
                    stable = 0
                    last = None
                    last_change = time.time()
                    time.sleep(1.0)
                    continue
                return scrape_reply(screen), _meta("scrape", nudges, True, False,
                                                   note="no file after nudges")
        time.sleep(POLL)
    # absolute cap hit
    if file_ready(file_path):
        return read_reply(file_path), _meta("file", nudges, False, True)
    return (scrape_reply(last) if last else ""), _meta("scrape", nudges, True, True,
                                                       note="max turn")


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


@contextlib.contextmanager
def session_lock(name):
    """Serialize turns per session; a second concurrent run fails fast."""
    path = os.path.join("/tmp", f"cw-{name}.lock")
    f = open(path, "w")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(f"[cw] session '{name}' is busy (another cw is "
                             "running against it)")
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def init_db(db_path):
    con = sqlite3.connect(db_path)
    con.execute("""CREATE TABLE IF NOT EXISTS responses(
        id TEXT PRIMARY KEY, ts TEXT, session TEXT, prompt TEXT, reply TEXT,
        seconds REAL, via TEXT, nudges INTEGER, low_fidelity INTEGER,
        timed_out INTEGER, blocked INTEGER, note TEXT)""")
    for col in ("blocked INTEGER", "note TEXT"):           # migrate older DBs
        with contextlib.suppress(sqlite3.OperationalError):
            con.execute(f"ALTER TABLE responses ADD COLUMN {col}")
    con.commit()
    return con


def ask(prompt, session, cwd, db_path, full_auto, debug=False):
    mode = "bypassPermissions" if full_auto else "acceptEdits"
    with session_lock(session):
        ensure_session(session, cwd, mode)
        turn_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        scratch_dir = os.path.join(cwd, ".cw")
        os.makedirs(scratch_dir, exist_ok=True)
        file_path = os.path.join(scratch_dir, turn_id + ".md")

        t0 = time.time()
        send_text(session, prompt + INSTRUCTION.format(path=file_path))
        time.sleep(0.8)
        reply, meta = run_turn(session, file_path)
        seconds = round(time.time() - t0, 1)

    con = init_db(db_path)
    con.execute("INSERT INTO responses VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
        turn_id, datetime.datetime.now(datetime.timezone.utc).isoformat(),
        session, prompt, reply, seconds, meta["via"], meta["nudges"],
        meta["low_fidelity"], meta["timed_out"], meta["blocked"], meta["note"]))
    con.commit()
    con.close()

    flags = [k for k in ("low_fidelity", "timed_out", "blocked") if meta[k]]
    if meta["nudges"]:
        flags.append(f"{meta['nudges']} nudge(s)")
    if meta["note"]:
        flags.append(meta["note"])
    sys.stderr.write(f"[cw] {session} {seconds}s via={meta['via']}"
                     + (f" [{', '.join(flags)}]" if flags else "")
                     + f" id={turn_id}\n")
    if debug:
        sys.stderr.write(f"[cw] scratch={file_path}\n")
    return reply


def history(db_path, n, session):
    if not os.path.exists(db_path):
        sys.stderr.write("[cw] no database yet\n")
        return
    con = sqlite3.connect(db_path)
    q = "SELECT ts, session, seconds, via, substr(prompt,1,70) FROM responses"
    params = []
    if session:
        q += " WHERE session=?"
        params.append(session)
    q += " ORDER BY ts DESC LIMIT ?"
    params.append(n)
    for ts, sess, secs, via, p in con.execute(q, params):
        print(f"{ts}  [{sess}]  {secs}s  {via:6}  {p!r}")
    con.close()


def main():
    ap = argparse.ArgumentParser(prog="cw")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ask", help="send one prompt, print the reply")
    a.add_argument("prompt")
    a.add_argument("-s", "--session", default="cw")
    a.add_argument("-c", "--cwd", default=os.getcwd())
    a.add_argument("-d", "--db", default=os.path.join(os.getcwd(), "cw.sqlite"))
    a.add_argument("--full-auto", action="store_true",
                   help="bypass all permission prompts (claude runs anything)")
    a.add_argument("--debug", action="store_true")

    h = sub.add_parser("history", help="show recent stored turns")
    h.add_argument("-n", type=int, default=10)
    h.add_argument("-s", "--session", default=None)
    h.add_argument("-d", "--db", default=os.path.join(os.getcwd(), "cw.sqlite"))

    s = sub.add_parser("stop", help="kill the claude session")
    s.add_argument("-s", "--session", default="cw")

    args = ap.parse_args()
    if args.cmd == "ask":
        print(ask(args.prompt, args.session, args.cwd, args.db,
                  args.full_auto, args.debug))
    elif args.cmd == "history":
        history(args.db, args.n, args.session)
    elif args.cmd == "stop":
        tmux("kill-session", "-t", args.session)
        sys.stderr.write(f"[cw] killed session {args.session}\n")


if __name__ == "__main__":
    main()
