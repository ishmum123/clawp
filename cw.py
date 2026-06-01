#!/usr/bin/env python3
"""cw - drive the interactive `claude` TUI through tmux, capture full-fidelity
replies via a side channel, and store them in sqlite.

Why a side channel: scraping the TUI yields *rendered* text (markdown/code is
mangled). Instead we append an instruction to each prompt telling claude to
Write its raw verbatim answer to a scratch file; the wrapper reads that file
(full fidelity) and inserts it into sqlite with a parameterized query.

Billing: runs claude in normal interactive mode (never `-p`), so usage counts
against your Claude subscription, not the Agent SDK credit pool. `acceptEdits`
permission mode lets the Write land without a prompt; the end user sets up
nothing.

Completion: the scratch file is the signal - a unique path per turn, so a cheap
stat tells us claude finished (full fidelity, no screen parsing). The screen is
consulted only when the file is missing: the spinner animates while claude
works, so a stable screen with no spinner means it went idle without writing,
and we nudge (after a short floor) until the file appears.

Usage:
    python3 cw.py ask "your prompt"           # create/reuse default session
    python3 cw.py ask "prompt" -s name        # named session
    python3 cw.py history [-n 10] [-s name]   # recent stored turns
    python3 cw.py stop [-s name]              # kill the session
"""
import argparse
import datetime
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
READY_TIMEOUT = 45      # seconds to reach the idle prompt after launch
ANSWER_TIMEOUT = 300    # seconds to wait for a reply (last-ditch fallback)
MAX_NUDGES = 2          # times to re-ask if idle without a written file
PANE_W, PANE_H = 220, 50

PROMPT_RE = re.compile(r"^❯\s")            # input box / echoed prompt
ASSIST_RE = re.compile(r"^⏺\s?")           # assistant message start (scrape fallback)
DONE_RE = re.compile(r"\bfor\s+\d+s\b")    # completion line: "✻ Baked for 2s"

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


def is_ready(screen):
    return "Model:" in screen and "❯" in screen


def bottom_lines(screen, n=12):
    lines = screen.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return lines[-n:]


def has_spinner(screen):
    # Active work shows a gerund line with an ellipsis ("✳ Flambéing…"); the
    # idle "done" summary ("✻ Baked for 2s") has none. An ellipsis in the bottom
    # region therefore means claude is still going.
    return any("…" in l for l in bottom_lines(screen))


def ensure_session(name, cwd):
    """Create the claude session if absent; clear trust prompt; wait until idle."""
    if has_session(name):
        return
    tmux("new-session", "-d", "-s", name, "-x", str(PANE_W), "-y", str(PANE_H),
         "-c", cwd, "claude", "--permission-mode", "acceptEdits")
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
        if is_ready(screen):
            return
        time.sleep(POLL)
    raise TimeoutError(f"claude session '{name}' did not become ready in "
                       f"{READY_TIMEOUT}s")


def send_text(name, text):
    tmux("load-buffer", "-", stdin_text=text)
    tmux("paste-buffer", "-p", "-t", name)   # -p = bracketed paste (multiline-safe)
    time.sleep(0.4)
    tmux("send-keys", "-t", name, "Enter")


def file_ready(path):
    return os.path.exists(path) and os.path.getsize(path) > 0


def read_reply(path):
    with open(path, encoding="utf-8") as f:
        return f.read().rstrip("\n")


def run_turn(name, file_path, timeout):
    """Wait for the turn to settle.

    The file is the completion signal: a cheap stat, full fidelity, and a unique
    path per turn (no stale-file risk), so the happy path returns the instant it
    appears without ever capturing the screen. The screen is consulted only when
    the file is missing, to decide wait-vs-nudge: nudge only once claude is
    provably idle (screen stable + no spinner) and past NUDGE_FLOOR seconds.

    Returns (reply_text, meta dict)."""
    start = time.time()
    deadline = start + timeout
    last = None
    stable = 0
    nudges = 0
    while time.time() < deadline:
        if file_ready(file_path):                       # file-first: happy path
            return read_reply(file_path), {"via": "file", "nudges": nudges,
                                           "low_fidelity": False, "timed_out": False}
        screen = capture(name)
        if screen == last:
            stable += 1
        else:
            stable = 1
            last = screen
        idle = stable >= STABLE_NEEDED and not has_spinner(screen)
        if idle and time.time() - start >= NUDGE_FLOOR:
            if nudges < MAX_NUDGES:
                send_text(name, NUDGE.format(path=file_path))
                nudges += 1
                stable = 0
                last = None
                time.sleep(1.0)
                continue
            # gave up waiting for the file: fall back to a (lossy) scrape
            return scrape_reply(screen), {"via": "scrape", "nudges": nudges,
                                          "low_fidelity": True, "timed_out": False}
        time.sleep(POLL)
    # timeout fallback
    if file_ready(file_path):
        return read_reply(file_path), {"via": "file", "nudges": nudges,
                                       "low_fidelity": False, "timed_out": True}
    return (scrape_reply(last) if last else ""), {"via": "scrape", "nudges": nudges,
                                                  "low_fidelity": True, "timed_out": True}


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


def init_db(db_path):
    con = sqlite3.connect(db_path)
    con.execute("""CREATE TABLE IF NOT EXISTS responses(
        id TEXT PRIMARY KEY, ts TEXT, session TEXT, prompt TEXT, reply TEXT,
        seconds REAL, via TEXT, nudges INTEGER, low_fidelity INTEGER,
        timed_out INTEGER)""")
    con.commit()
    return con


def ask(prompt, session, cwd, db_path, timeout, debug=False):
    ensure_session(session, cwd)
    turn_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    scratch_dir = os.path.join(cwd, ".cw")
    os.makedirs(scratch_dir, exist_ok=True)
    file_path = os.path.join(scratch_dir, turn_id + ".md")

    t0 = time.time()
    send_text(session, prompt + INSTRUCTION.format(path=file_path))
    time.sleep(0.8)
    reply, meta = run_turn(session, file_path, timeout)
    seconds = round(time.time() - t0, 1)

    con = init_db(db_path)
    con.execute("INSERT INTO responses VALUES (?,?,?,?,?,?,?,?,?,?)", (
        turn_id, datetime.datetime.now(datetime.timezone.utc).isoformat(),
        session, prompt, reply, seconds, meta["via"], meta["nudges"],
        int(meta["low_fidelity"]), int(meta["timed_out"])))
    con.commit()
    con.close()

    note = []
    if meta["nudges"]:
        note.append(f"{meta['nudges']} nudge(s)")
    if meta["low_fidelity"]:
        note.append("LOW-FIDELITY scrape fallback")
    if meta["timed_out"]:
        note.append("TIMED OUT")
    sys.stderr.write(f"[cw] {session} {seconds}s via={meta['via']}"
                     + (f" ({', '.join(note)})" if note else "")
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
    a.add_argument("-t", "--timeout", type=int, default=ANSWER_TIMEOUT)
    a.add_argument("--debug", action="store_true")

    h = sub.add_parser("history", help="show recent stored turns")
    h.add_argument("-n", type=int, default=10)
    h.add_argument("-s", "--session", default=None)
    h.add_argument("-d", "--db", default=os.path.join(os.getcwd(), "cw.sqlite"))

    s = sub.add_parser("stop", help="kill the claude session")
    s.add_argument("-s", "--session", default="cw")

    args = ap.parse_args()
    if args.cmd == "ask":
        print(ask(args.prompt, args.session, args.cwd, args.db, args.timeout,
                  args.debug))
    elif args.cmd == "history":
        history(args.db, args.n, args.session)
    elif args.cmd == "stop":
        tmux("kill-session", "-t", args.session)
        sys.stderr.write(f"[cw] killed session {args.session}\n")


if __name__ == "__main__":
    main()
