#!/usr/bin/env python3
"""clawp - a subscription-billed drop-in for `claude -p`.

Name: claw (claude wrapper) + p (the `claude -p` it drops in for).

`clawp [flags] [prompt]` (or `echo prompt | clawp`) behaves like `claude -p`:
the prompt comes from a positional arg or stdin, the answer goes to stdout, and
a clean run is silent on stderr (only errors/degraded turns are reported), with
proper exit codes. The session-id is exposed via json/stream-json output and
clawp.sqlite, not stderr. It runs the *interactive* TUI in a detached tmux pane
(NEVER `claude -p` / `kimi -p`),
so usage bills against your subscription rather than the credit pool that the
print modes draw from.

How: generate (or resume) a session-id, launch the chosen client
(`claude --session-id <uuid> ...` or `kimi [--session <id>] ...`) in an
ephemeral pane, send the prompt verbatim, and capture the answer from the
transcript jsonl the client writes to disk. Completion is dual-signalled: a new
transcript record with a terminal stop reason, or a screen-idle backstop. The
pane is reaped on every exit path.

Output modes (`--output-format`): text (default), json (one result object),
stream-json (live NDJSON events). `--resume <id>` continues a prior session.
`--full-auto` runs with bypass/auto permissions for unattended use.

Usage:
    clawp "your prompt"
    echo "prompt" | clawp
    clawp --client kimi "your prompt"
    clawp --output-format json "..."
    clawp --output-format stream-json "..."
    clawp --resume <session-id> "..."
    clawp --model opus "..."
    clawp --history [-n 10]
"""
import argparse
import contextlib
import dataclasses
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
STABLE_NEEDED = 5       # identical captures => quiet; *POLL must outlast a
                        # natural mid-turn streaming pause (~2.4s measured)
STALL_SECS = 180        # screen unchanged this long (e.g. frozen spinner) => hang
MAX_TURN = 1800         # absolute per-turn cap (seconds)
READY_TIMEOUT = 45      # seconds to reach the idle prompt after launch
SEND_SETTLE = 0.8       # pause after send_text before polling for a reply
PANE_W, PANE_H = 220, 50
OUTPUT_TOKEN_FLOOR = 64000  # floor for the pane's CLAUDE_CODE_MAX_OUTPUT_TOKENS
                           # (unless the caller already set one): the 32K
                           # interactive default can be wholly spent on Opus
                           # max-effort thinking before a large tool_use lands,
                           # truncating the turn (max_tokens) before its file write.

ASSIST_RE = re.compile(r"^⏺\s?")           # assistant message start (scrape fallback)
PROMPT_RE = re.compile(r"^❯\s")            # input box / echoed prompt
DONE_RE = re.compile(r"\bfor\s+\d+s\b")    # completion line: "✻ Baked for 2s"

# Phrases that mean a blocking dialog (permission / trust / bypass) owns the
# screen.
DIALOG_MARKERS = (
    "Do you want", "don't ask again", "tell Claude what to do differently",
    "trust this folder", "Quick safety check", "No, and tell Claude",
    "Bypass Permissions mode", "Yes, I accept",
)
# Kimi approval dialogs keep the idle status bar at the bottom, so they are
# detected by their own prompt text rather than by the absence of the bar.
KIMI_DIALOG_MARKERS = (
    "Run this command?", "Allow this edit?", "Allow this Bash command?",
    "Approve once", "Approve for this session", "Reject with feedback",
    "↑/↓ select", "1/2/3/4 choose",
)
# Substrings that mark the idle interactive status bar. Its text varies by
# version/plan — "Model: …" on 2.1.159, "shift+tab to cycle" / "◈ max ·
# /effort" on 2.1.143 Claude Max — so any one of these means we reached the bar.
IDLE_MARKERS = ("Model:", "shift+tab", "/effort")

# Kimi-specific idle-bar/status substrings. The model version prefix changes
# (K2.7 Code, K2.8 Code, ...), so the generic "Code thinking" marker catches
# future releases while the specific one is kept for explicitness.
KIMI_IDLE_MARKERS = ("K2.7 Code thinking", "Code thinking", "context:")
KIMI_SESSION_RE = re.compile(r"Session:\s+(session_[a-f0-9-]+)")
KIMI_VERSION_RE = re.compile(r"Version:\s+([\d.]+)")

# A prompt whose first non-blank char is one of these can't be sent as a message:
# the interactive TUI reads a leading '/' as a command (no reply — clawp would hang)
# and '!' as a shell command (it RUNS the rest — an RCE that `claude -p` does NOT
# have). '@' (file) and '#' (memory) still deliver the message, so they're allowed.
TUI_COMMAND_PREFIXES = ("/", "!")

# Print-only flags (claude --help: "only works with --print"); the interactive
# TUI won't honor them, and each changes the result/protocol if ignored, so reject
# loudly rather than forward. --max-budget-usd is claude's current work-bound knob;
# --max-turns is its removed predecessor, kept so old-claude scripts still fail loud.
# (--include-partial-messages is handled separately: it only affects stream
# granularity, so it's a tolerated no-op under stream-json.)
PRINT_ONLY_FLAGS = ("--max-turns", "--max-budget-usd", "--fallback-model",
                    "--json-schema", "--replay-user-messages")
# Session flags forwarded verbatim to the interactive launch.
PASSTHROUGH_FLAGS = ("--model", "--effort", "--add-dir", "--system-prompt",
                     "--append-system-prompt", "--allowedTools",
                     "--disallowedTools", "--tools", "--permission-mode",
                     "--mcp-config", "--agents", "--settings")
# Boolean session flags: forwarded as a bare flag (no value) when set. Separate
# from PASSTHROUGH_FLAGS because those each carry one value; these carry none.
BOOL_PASSTHROUGH_FLAGS = ("--disable-slash-commands",)


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
    # none. The fresh-launch "What's new" box also carries "…" in its truncated
    # rows, but those sit inside the box border (│), so ignore boxed ellipses.
    return any("…" in l and "│" not in l for l in bottom_lines(screen))


def looks_blocked(screen):
    return any(m in screen for m in DIALOG_MARKERS)


def has_idle_bar(screen):
    # The interactive status bar; absent when a dialog owns the screen. Its exact
    # text varies by version/plan (see IDLE_MARKERS).
    return any(m in screen for m in IDLE_MARKERS)


def at_idle_prompt(screen):
    return has_idle_bar(screen) and not looks_blocked(screen)


def turn_blocked(screen):
    # Mid-turn block test for the capture loop. A real permission/trust dialog owns
    # the screen: a marker is present AND the status bar is gone. Robust on a warm
    # pane reused across turns, where a prior answer's marker text — or the current
    # answer quoting "Do you want…" — sits in the capture while the bar is still
    # there; looks_blocked alone would false-positive on that.
    return looks_blocked(screen) and not has_idle_bar(screen)


def _accept_highlighted(screen):
    # The selected menu row carries the ❯ cursor; true only once "Yes, I accept"
    # is the highlighted row, so we never confirm the default "No, exit".
    return any("❯" in l and "Yes, I accept" in l for l in screen.splitlines())


def _accept_bypass_dialog(name):
    # Move the highlight off the default "No, exit", then confirm only once
    # "Yes, I accept" is selected — never a blind Enter, which would quit claude.
    tmux("send-keys", "-t", name, "Down")
    for _ in range(4):
        time.sleep(0.3)
        if _accept_highlighted(capture(name)):
            tmux("send-keys", "-t", name, "Enter")
            return True
    return False


def _output_token_value():
    # tmux new-session inherits the tmux SERVER's env, not clawp's, so a var set
    # for clawp never reaches the pane unless forwarded with -e. Forward the
    # caller's value when set; otherwise raise the pane to OUTPUT_TOKEN_FLOOR.
    return os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") or str(OUTPUT_TOKEN_FLOOR)


# --- client adapters ---------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Client:
    """Backend selector: one instance per supported client."""
    name: str
    command: str
    env_token_var: str


class ClaudeAdapter:
    CLIENT = Client(name="claude", command="claude",
                    env_token_var="CLAUDE_CODE_MAX_OUTPUT_TOKENS")

    @staticmethod
    def launch_args(args, session_id, fresh):
        return _launch_args(args, session_id, fresh)

    @staticmethod
    def find_transcript(session_id, cwd=None):
        return find_transcript(session_id)

    @staticmethod
    def is_noise(rec):
        return is_noise(rec)

    @staticmethod
    def is_terminal(rec):
        return is_terminal_assistant(rec)

    @staticmethod
    def is_truncated(rec):
        return is_truncated(rec)

    @staticmethod
    def answer_text(rec):
        return assistant_text(rec)

    @staticmethod
    def final_answer(records):
        return final_answer(records)

    @staticmethod
    def turn_in_flight(records):
        return turn_in_flight(records)

    @staticmethod
    def stream_event(rec, records=None):
        return stream_event(rec)

    @staticmethod
    def has_idle_bar(screen):
        return has_idle_bar(screen)

    @staticmethod
    def looks_blocked(screen):
        return looks_blocked(screen)

    @staticmethod
    def is_blocked(screen):
        return looks_blocked(screen) and not has_idle_bar(screen)

    @staticmethod
    def has_spinner(screen):
        return has_spinner(screen)

    @staticmethod
    def version(screen):
        return claude_version(screen)


# --- Kimi wire-format helpers ------------------------------------------------

KIMI_NOISE_TYPES = {"metadata", "config.update", "tools.set_active_tools",
                    "permission.set_mode", "usage.record", "context.append_message"}


def _kimi_loop_event(rec):
    if rec.get("type") == "context.append_loop_event":
        return rec.get("event") or {}
    return None


def _kimi_step_uuid(rec):
    ev = _kimi_loop_event(rec)
    if ev and ev.get("type") == "step.end":
        return ev.get("uuid")
    return None


def _kimi_step_content(step_uuid, records):
    text_parts = []
    tools = []
    for rec in records:
        ev = _kimi_loop_event(rec)
        if not ev or ev.get("stepUuid") != step_uuid:
            continue
        et = ev.get("type")
        if et == "content.part":
            part = ev.get("part", {})
            if part.get("type") == "text":
                text_parts.append(part.get("text", ""))
        elif et == "tool.call":
            tools.append({"type": "tool_use", "name": ev.get("name", ""),
                          "input": ev.get("args", {})})
    return "".join(text_parts).strip(), tools


def _kimi_read_index(index_path):
    entries = []
    if not os.path.exists(index_path):
        return entries
    with open(index_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


class KimiAdapter:
    CLIENT = Client(name="kimi", command="kimi", env_token_var="")

    @staticmethod
    def launch_args(args, session_id, fresh):
        # Kimi generates its own session IDs for fresh sessions.
        if fresh and args.session_id:
            raise CwError(2, "Kimi generates session IDs; do not use --session-id "
                          "for a fresh session (use --resume <id> to continue)")
        # Only --model is honored as a passthrough; everything else is claude-only.
        rejected = []
        for flag in PASSTHROUGH_FLAGS:
            if flag == "--model":
                continue
            val = getattr(args, flag.lstrip("-").replace("-", "_"))
            if val is not None:
                rejected.append(flag)
        for flag in BOOL_PASSTHROUGH_FLAGS:
            if getattr(args, flag.lstrip("-").replace("-", "_")):
                rejected.append(flag)
        if rejected:
            raise CwError(2, f"Kimi cannot honor {rejected[0]}; use Kimi-native "
                          "flags instead")
        out = []
        if not fresh:
            out += ["--session", session_id]
        if args.full_auto:
            out.append("--auto")
        if args.model:
            out += ["--model", args.model]
        return out

    @staticmethod
    def find_transcript(session_id, cwd=None, index_path=None):
        if index_path is None:
            index_path = os.path.expanduser("~/.kimi-code/session_index.jsonl")
        if not session_id:
            return None
        best = None
        for entry in _kimi_read_index(index_path):
            if entry.get("sessionId") == session_id:
                if cwd is None or entry.get("workDir") == cwd:
                    best = entry
        if best:
            return os.path.join(best["sessionDir"], "agents", "main", "wire.jsonl")
        return None

    @staticmethod
    def find_latest_session(cwd, index_path=None):
        if index_path is None:
            index_path = os.path.expanduser("~/.kimi-code/session_index.jsonl")
        if not cwd:
            return None
        for entry in reversed(_kimi_read_index(index_path)):
            if entry.get("workDir") == cwd:
                return entry
        return None

    @staticmethod
    def is_noise(rec):
        return rec.get("type") in KIMI_NOISE_TYPES

    @staticmethod
    def is_terminal(rec):
        ev = _kimi_loop_event(rec)
        return bool(ev and ev.get("type") == "step.end"
                    and ev.get("finishReason") == "end_turn")

    @staticmethod
    def is_truncated(rec):
        return False

    @staticmethod
    def answer_text(rec):
        return ""

    @staticmethod
    def final_answer(records):
        terminal_uuid = None
        for rec in reversed(records):
            ev = _kimi_loop_event(rec)
            if ev and ev.get("type") == "step.end" \
                    and ev.get("finishReason") == "end_turn":
                terminal_uuid = ev.get("uuid")
                break
        if not terminal_uuid:
            return ""
        text, _ = _kimi_step_content(terminal_uuid, records)
        return text

    @staticmethod
    def turn_in_flight(records):
        for rec in reversed(records):
            if KimiAdapter.is_noise(rec):
                continue
            if rec.get("type") == "turn.prompt":
                return True
            ev = _kimi_loop_event(rec)
            if ev:
                et = ev.get("type")
                if et in ("step.begin", "content.part", "tool.call"):
                    return True
                if et == "tool.result":
                    return True
                if et == "step.end":
                    return ev.get("finishReason") == "tool_use"
            return False
        return False

    @staticmethod
    def stream_event(rec, records=None):
        if KimiAdapter.is_noise(rec):
            return None
        ev = _kimi_loop_event(rec)
        if not ev:
            return None
        et = ev.get("type")
        if et == "tool.result":
            return {"type": "tool_result"}
        if et != "step.end" or not records:
            return None
        finish = ev.get("finishReason")
        if finish not in ("end_turn", "tool_use"):
            return None
        step_uuid = ev.get("uuid")
        text, tools = _kimi_step_content(step_uuid, records)
        if tools:
            content = ([{"type": "text", "text": text}] if text else []) + tools
            return {"type": "assistant", "message": {"content": content}}
        if text:
            return {"type": "assistant", "text": text}
        return None

    @staticmethod
    def has_idle_bar(screen):
        return any(m in screen for m in KIMI_IDLE_MARKERS)

    @staticmethod
    def looks_blocked(screen):
        return any(m in screen for m in KIMI_DIALOG_MARKERS)

    @staticmethod
    def is_blocked(screen):
        # Kimi shows approval prompts inline while the idle bar stays visible,
        # so block detection keys on dialog text rather than bar absence.
        return KimiAdapter.looks_blocked(screen)

    @staticmethod
    def has_spinner(screen):
        # Kimi's idle bar truncates the cwd with "…" (e.g. "…/Programming/…"),
        # which would false-positive the generic ellipsis spinner test. Only
        # count an ellipsis as a spinner if it is NOT on the idle-bar line.
        return any("…" in l and "│" not in l
                   and not any(m in l for m in KIMI_IDLE_MARKERS)
                   for l in bottom_lines(screen))

    @staticmethod
    def version(screen):
        m = KIMI_VERSION_RE.search(screen)
        return m.group(1) if m else ""


def get_client(name):
    if name == "kimi":
        return KimiAdapter
    if name == "claude":
        return ClaudeAdapter
    raise CwError(2, f"unknown client {name!r}; choose 'claude' or 'kimi'")


# --- end client adapters -----------------------------------------------------


def ensure_session(name, cwd, client, launch_args, session_id=None):
    """Launch the client session if absent; clear launch dialogs; wait until idle.

    Returns (session_id, transcript_path_hint). For Claude the hint is None and
    the caller resolves the path later via find_transcript. For Kimi the hint is
    discovered from the session index (Kimi generates its own IDs and stores the
    transcript under a workdir-keyed directory).
    """
    if has_session(name):
        if client.CLIENT.name == "kimi" and session_id:
            path = client.find_transcript(session_id, cwd)
            return session_id, path
        return session_id, None

    env_args = []
    if client.CLIENT.env_token_var:
        env_args = ["-e", client.CLIENT.env_token_var + "=" + _output_token_value()]
    tmux("new-session", "-d", "-s", name, *env_args,
         "-x", str(PANE_W), "-y", str(PANE_H),
         "-c", cwd, client.CLIENT.command, *launch_args)
    deadline = time.time() + READY_TIMEOUT

    if client.CLIENT.name == "claude":
        trusted = False
        bypassed = False
        while time.time() < deadline:
            screen = capture(name)
            if not trusted and ("trust this folder" in screen
                                or "Quick safety check" in screen):
                tmux("send-keys", "-t", name, "Enter")   # default = "Yes, I trust"
                trusted = True
                time.sleep(1.0)
                continue
            # First bypass launch on an unaccepted box warns before the prompt. The
            # user explicitly chose --full-auto, so accept it — one shot, so a missed
            # keystroke can't loop the highlight back onto the default "No, exit".
            if (not bypassed and "bypassPermissions" in launch_args
                    and "Yes, I accept" in screen):
                bypassed = True
                _accept_bypass_dialog(name)
                time.sleep(1.0)
                continue
            if client.has_idle_bar(screen) and not client.has_spinner(screen):
                return session_id, None
            time.sleep(POLL)
        raise TimeoutError(f"claude session '{name}' did not become ready in "
                           f"{READY_TIMEOUT}s")

    # Kimi: for a resume the session-id is known; just wait for idle and resolve
    # the transcript path. For a fresh session, discover the generated ID from the
    # welcome screen or the newest index entry for this cwd that did not exist
    # before launch (so a stale prior session is never mistaken for the new one).
    if session_id:
        path = client.find_transcript(session_id, cwd)
        while time.time() < deadline:
            screen = capture(name)
            if client.has_idle_bar(screen) and not client.looks_blocked(screen) \
                    and not client.has_spinner(screen):
                return session_id, path
            time.sleep(POLL)
        raise TimeoutError(f"kimi session '{name}' did not become ready in "
                           f"{READY_TIMEOUT}s")

    known_ids = {e.get("sessionId") for e in _kimi_read_index(
        os.path.expanduser("~/.kimi-code/session_index.jsonl"))}

    def _new_session_entry():
        for entry in reversed(_kimi_read_index(
                os.path.expanduser("~/.kimi-code/session_index.jsonl"))):
            if entry.get("workDir") == cwd \
                    and entry.get("sessionId") not in known_ids:
                return entry
        return None

    discovered = None
    path = None
    while time.time() < deadline:
        screen = capture(name)
        m = KIMI_SESSION_RE.search(screen)
        if m:
            candidate = m.group(1)
            if candidate not in known_ids:
                discovered = candidate
                break
        entry = _new_session_entry()
        if entry:
            discovered = entry["sessionId"]
            break
        if client.has_idle_bar(screen) and not client.looks_blocked(screen):
            entry = _new_session_entry()
            if entry:
                discovered = entry["sessionId"]
                break
        time.sleep(POLL)
    if not discovered:
        raise TimeoutError(f"kimi session '{name}' did not become ready in "
                           f"{READY_TIMEOUT}s")
    path = client.find_transcript(discovered, cwd)
    return discovered, path


def clear_input(name):
    # Drop any ghost prompt-suggestion or stray text so the paste lands clean.
    tmux("send-keys", "-t", name, "Escape")
    tmux("send-keys", "-t", name, "C-u")


def send_text(name, text):
    clear_input(name)
    # Name the buffer after the pane. The default tmux server (shared by every
    # clawp process) keeps one unnamed "most-recent" buffer, so concurrent turns
    # that load/paste it cross prompts; a per-pane named buffer can't collide.
    # -d drops it after paste so named buffers don't accumulate across sessions.
    tmux("load-buffer", "-b", name, "-", stdin_text=text)
    tmux("paste-buffer", "-d", "-p", "-b", name, "-t", name)   # -p = bracketed paste
    time.sleep(0.4)
    tmux("send-keys", "-t", name, "Enter")


def stream_input_prompt(line):
    # claude --input-format stream-json: one user message per stdin line. Pull the
    # prompt text out (sent verbatim by send_text); non-user/malformed/empty lines
    # return None and are skipped. Text blocks are joined; non-text content (images)
    # can't be pasted into the TUI, so it's dropped.
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if obj.get("type") != "user":
        return None
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        text = "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
        return text or None
    return None


def prefix_rejection(prompt):
    # A leading '/' or '!' (after any whitespace) is intercepted by the interactive
    # TUI: '/' opens command mode (no reply), '!' runs the rest as a shell command.
    # Such a prompt can't be sent verbatim as a message, so return a reason and let
    # the caller refuse before send_text — never pasting (or, for '!', running) it.
    head = prompt.lstrip()
    if head[:1] in TUI_COMMAND_PREFIXES:
        return (f"prompt starts with {head[0]!r}: the TUI reads a leading '/' as a "
                f"command and '!' as a shell command, so it can't be sent as a "
                f"message (rephrase or quote it)")
    return None


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


def is_truncated(rec):
    # max_tokens: claude hit the output-token cap and stopped mid-response (Claude
    # Code does not auto-continue), so the turn is terminal but its answer — and any
    # tool_use it was about to emit — is cut off. Flag it so a truncated turn is
    # never logged (or returned) as a clean success.
    if rec.get("type") != "assistant":
        return False
    return (rec.get("message") or {}).get("stop_reason") == "max_tokens"


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


def tool_uses(rec):
    # The tool name lives on the assistant's tool_use block; the matching
    # tool_result (a later user record) carries only tool_use_id.
    blocks = (rec.get("message") or {}).get("content") or []
    return [b for b in blocks
            if isinstance(b, dict) and b.get("type") == "tool_use"]


def stream_event(rec):
    if is_noise(rec):
        return None
    t = rec.get("type")
    if t == "assistant":
        tools = tool_uses(rec)
        text = assistant_text(rec)
        # Surface tool calls as a claude-`-p`-shaped assistant/message event so a
        # consumer can read tool name+input (e.g. for live status labels); a
        # text-only turn stays the flat {"type":"assistant","text":...} form.
        if tools:
            content = ([{"type": "text", "text": text}] if text else []) + tools
            return {"type": "assistant", "message": {"content": content}}
        if text:
            return {"type": "assistant", "text": text}
        return None                # thinking-only: nothing to emit
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
    def __init__(self, code, msg, meta=None):
        super().__init__(code)
        sys.stderr.write(f"[clawp] {msg}\n")
        self.msg = msg
        self.meta = meta            # carried so a caller's per-turn log keeps it


def _passthrough_args(args):
    out = []
    for flag in PASSTHROUGH_FLAGS:
        val = getattr(args, flag.lstrip("-").replace("-", "_"))
        if val is not None:
            out += [flag, val]
    for flag in BOOL_PASSTHROUGH_FLAGS:
        if getattr(args, flag.lstrip("-").replace("-", "_")):
            out += [flag]
    return out


def _launch_args(args, session_id, fresh):
    mode = "bypassPermissions" if args.full_auto else "acceptEdits"
    claude_args = ["--session-id", session_id] if fresh \
        else ["--resume", session_id]
    # explicit --permission-mode passthrough overrides the default mode.
    if args.permission_mode is None:
        claude_args += ["--permission-mode", mode]
    claude_args += _passthrough_args(args)
    return claude_args


def _init_event(session_id, model, cwd):
    # claude-shaped session bootstrap; a consumer reads session_id from here.
    return {"type": "system", "subtype": "init", "session_id": session_id,
            "model": model, "cwd": cwd, "tools": [], "mcp_servers": []}


def _turn_events(answer, session_id, model, duration_ms, num_turns):
    # claude --output-format stream-json for ONE completed turn. clawp only ever has
    # the whole message, so the partial-message envelope carries the full answer as a
    # single text delta; emitting the entire message_start…message_stop envelope (not
    # a lone delta) keeps a strict SSE consumer in sync. The assembled `assistant`
    # message and the `result` follow, for base (non-partial) consumers.
    msg = {"id": "msg_" + uuid.uuid4().hex[:24], "type": "message",
           "role": "assistant", "model": model,
           "content": [{"type": "text", "text": answer}], "stop_reason": "end_turn"}

    def se(event):
        return {"type": "stream_event", "session_id": session_id, "event": event}

    return [
        se({"type": "message_start", "message": {**msg, "content": []}}),
        se({"type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""}}),
        se({"type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": answer}}),
        se({"type": "content_block_stop", "index": 0}),
        se({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
        se({"type": "message_stop"}),
        {"type": "assistant", "message": msg, "session_id": session_id},
        {"type": "result", "subtype": "success", "session_id": session_id,
         "result": answer, "is_error": False, "duration_ms": duration_ms,
         "num_turns": num_turns},
    ]


def turn_in_flight(records):
    # Log-primary completion guard: the transcript itself says the turn is not
    # done when its last non-noise record is a user record (an echoed prompt or a
    # tool_result — either way Claude still owes a reply) or an assistant still in
    # continuation (tool_use / pause_turn). The screen backstop must not call a
    # turn complete over a mid-flight log: after tool use Claude can think for
    # 15-20s behind an idle-looking, spinner-less pane before the terminal answer
    # record is written, and the screen alone can't tell that apart from done.
    for rec in reversed(records):
        if is_noise(rec):
            continue
        if rec.get("type") == "assistant":
            return (rec.get("message") or {}).get("stop_reason") in CONTINUATION_STOP
        if rec.get("type") == "user":
            return True
        return False
    return False


def _run_turn(name, session_id, path, prompt, fmt, stream, client, cwd):
    # One turn against an already-live pane: send the prompt, then watch the
    # transcript (primary) with a screen backstop until it settles. Returns
    # (answer, meta, path); emits clawp's per-record events when stream=True. Raises
    # CwError (meta attached for the caller's log) on a blocked/stalled/over-long
    # turn. Caller owns the lock, ensure_session, logging, and pane teardown.
    if path is None:
        path = client.find_transcript(session_id, cwd)
    # Offset (record count) at send time swallows the previous turn's trailing
    # system/summary records; file may not exist yet on a fresh session (offset 0).
    offset = len(read_records(path))

    send_text(name, prompt)
    time.sleep(SEND_SETTLE)

    answer = None
    meta = _meta(False, False)
    start = time.time()
    last_screen = None
    stable = 0
    last_change = start
    seen = offset
    while time.time() - start < MAX_TURN:
        if path is None:
            path = client.find_transcript(session_id, cwd)
        records = read_records(path)
        new = records[seen:]
        # Stop AT the terminal record: a record flushed after end_turn in the same
        # batch must not leak into the stream.
        done = False
        for rec in new:
            seen += 1
            if client.is_terminal(rec):
                answer = client.final_answer(records[offset:seen])
                if client.is_truncated(rec):
                    meta = _meta(True, False,
                                 note="truncated at output-token cap (max_tokens)")
                if stream:
                    ev = client.stream_event(rec, records=records[offset:seen])
                    if ev is not None:
                        print(json.dumps(ev), flush=True)
                done = True
                break
            if stream:
                ev = client.stream_event(rec, records=records[offset:seen])
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
        if client.is_blocked(screen):
            raise CwError(3, "blocked: permission/trust dialog (use --full-auto "
                          "for unattended runs)",
                          meta=_meta(False, False, blocked=True, note="blocked"))
        if now - last_change >= STALL_SECS:
            raise CwError(4, "stall: screen frozen, no terminal stop_reason",
                          meta=_meta(False, True, note="stalled"))
        if client.has_spinner(screen):
            time.sleep(POLL)
            continue
        # idle backstop: quiet, idle, no spinner, no terminal record. Gate on
        # seen>offset (a record arrived this turn) so a warm pane's stale pre-send
        # screen — idle bar already present — can't settle into a false completion
        # before the turn has produced anything. And never settle while the
        # transcript shows the turn is still mid-flight (turn_in_flight): after
        # tool use the client thinks for 15-20s behind an idle, spinner-less pane
        # before the answer lands — the log, not the screen, is authoritative.
        if (stable >= STABLE_NEEDED and client.has_idle_bar(screen) and seen > offset
                and not client.turn_in_flight(records[offset:])):
            break
        time.sleep(POLL)
    else:
        raise CwError(4, "timeout: turn exceeded MAX_TURN",
                      meta=_meta(False, True, note="max turn"))

    # Schema-break fallback: settled but parsed no usable answer. Slice by offset so
    # a resumed/multi-turn transcript can't return a PRIOR turn's answer.
    if not answer:
        answer = client.final_answer(read_records(path)[offset:])
    if not answer:
        ver = client.version(capture(name))
        vsfx = f" on {client.CLIENT.name} v{ver}" if ver else ""
        if fmt == "text":
            answer = scrape_reply(capture(name))
            meta = _meta(True, False, note="schema unrecognized" + vsfx)
        else:
            raise CwError(5, "transcript schema unrecognized" + vsfx
                          + " (zero usable answer; json/stream-json cannot be "
                          "faithfully scraped)",
                          meta=_meta(False, True, note="schema unrecognized" + vsfx))
    return answer, meta, path


def _build_session(args, client):
    """Return (session_id, launch_args) for ensure_session.

    Claude pre-assigns a UUID; Kimi generates its own ID and is discovered later.
    """
    fresh = args.resume is None
    if client.CLIENT.name == "claude":
        session_id = args.session_id or args.resume or str(uuid.uuid4())
        return session_id, client.launch_args(args, session_id, fresh)
    # Kimi
    if fresh:
        if args.session_id:
            raise CwError(2, "Kimi generates session IDs; do not use --session-id "
                          "for a fresh session (use --resume <id> to continue)")
        return None, client.launch_args(args, None, fresh)
    return args.resume, client.launch_args(args, args.resume, fresh)


def _pane_name(client, session_id):
    # Serialize concurrent turns per session-id. Claude always knows its id up
    # front; Kimi only knows it for resumes, so fresh Kimi sessions use a random
    # pane name (they are independent new sessions anyway).
    if client.CLIENT.name == "claude":
        return "clawp-" + session_id[:8]
    if session_id:
        return "clawp-kimi-" + session_id[-8:]
    return "clawp-kimi-" + uuid.uuid4().hex[:8]


def run_print_turn(args, cwd, db_path):
    client = get_client(args.client)
    session_id, launch_args = _build_session(args, client)

    fmt = args.output_format
    stream = fmt == "stream-json"
    t0 = time.time()
    turn_id = (datetime.datetime.now().strftime("%Y%m%d-%H%M%S-")
               + uuid.uuid4().hex[:4])
    answer = None
    meta = _meta(False, False)
    seconds = 0.0
    name = _pane_name(client, session_id)
    with session_lock(name):
        try:
            try:
                session_id, path_hint = ensure_session(name, cwd, client,
                                                       launch_args, session_id)
            except TimeoutError as e:
                raise CwError(5, str(e), meta=_meta(False, True, note="launch failed"))
            path = path_hint or client.find_transcript(session_id, cwd)
            answer, meta, path = _run_turn(name, session_id, path, args.prompt,
                                           fmt, stream, client, cwd)
        except CwError as e:
            if e.meta is not None:
                meta = e.meta
            raise
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


def run_stream_turns(args, cwd, db_path):
    # claude-compatible --input-format stream-json: one long-lived clawp process and
    # ONE warm pane for the whole conversation. Read NDJSON user turns from stdin,
    # run each against the live pane, emit claude-shaped stream-json, and reap the
    # pane when stdin closes (EOF = conversation over). A blocked/stalled turn ends
    # the session: the dialog owns the pane and clawp can't answer it.
    client = get_client(args.client)
    session_id, launch_args = _build_session(args, client)
    model = args.model or "default"
    turns = 0
    name = _pane_name(client, session_id)
    with session_lock(name):
        try:
            try:
                session_id, path_hint = ensure_session(name, cwd, client,
                                                       launch_args, session_id)
            except TimeoutError as e:
                raise CwError(5, str(e))
            print(json.dumps(_init_event(session_id, model, cwd)), flush=True)
            path = path_hint or client.find_transcript(session_id, cwd)
            # readline (not `for line in sys.stdin`) so each turn is processed as
            # soon as the caller writes it — iterator read-ahead would buffer it.
            for line in iter(sys.stdin.readline, ""):
                prompt = stream_input_prompt(line)
                if prompt is None:          # non-user / malformed / empty line
                    continue
                turns += 1
                t0 = time.time()
                turn_id = (datetime.datetime.now().strftime("%Y%m%d-%H%M%S-")
                           + uuid.uuid4().hex[:4])
                rej = prefix_rejection(prompt)
                if rej:
                    # Refuse without sending — the pane is untouched, so the
                    # conversation survives and the next turn proceeds.
                    print(json.dumps({"type": "result", "subtype": "error",
                                      "session_id": session_id, "is_error": True,
                                      "result": rej, "num_turns": turns}), flush=True)
                    _log_turn(db_path, turn_id, session_id, prompt, "",
                              round(time.time() - t0, 1),
                              _meta(False, False, note="rejected (TUI prefix)"))
                    continue
                answer, meta = None, _meta(False, False)
                try:
                    answer, meta, path = _run_turn(name, session_id, path, prompt,
                                                   "stream-json", False, client, cwd)
                    ms = int((time.time() - t0) * 1000)
                    for ev in _turn_events(answer, session_id, model, ms, turns):
                        print(json.dumps(ev), flush=True)
                except CwError as e:
                    if e.meta is not None:
                        meta = e.meta
                    # surface the failure on the stream before the session ends
                    print(json.dumps({"type": "result", "subtype": "error",
                                      "session_id": session_id, "is_error": True,
                                      "result": e.msg}), flush=True)
                    raise
                finally:
                    seconds = round(time.time() - t0, 1)
                    _log_turn(db_path, turn_id, session_id, prompt, answer or "",
                              seconds, meta)
        finally:
            tmux("kill-session", "-t", name)
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


def build_parser():
    ap = argparse.ArgumentParser(
        prog="clawp", description="subscription-billed drop-in for `claude -p`")
    ap.add_argument("prompt", nargs="?")
    ap.add_argument("-p", "--print", action="store_true",
                    help="accepted no-op (clawp is always print)")
    ap.add_argument("--client", choices=("claude", "kimi"), default="claude",
                    help="backend client to drive (default: claude)")
    # Accepted-but-ignored `claude -p` flags, for drop-in argv compatibility.
    # --no-session-persistence must NOT be forwarded: it suppresses the
    # transcript jsonl that clawp reads the answer back from.
    ap.add_argument("--verbose", action="store_true",
                    help="accepted no-op (stream-json is always verbose)")
    ap.add_argument("--no-session-persistence", action="store_true",
                    help="accepted no-op (clawp needs the session transcript)")
    ap.add_argument("--output-format", choices=("text", "json", "stream-json"),
                    default="text")
    ap.add_argument("--input-format", choices=("text", "stream-json"),
                    default="text",
                    help="stream-json: multi-turn NDJSON turns over stdin against "
                         "one warm pane (requires --output-format stream-json)")
    # No-op under stream-json: clawp can't emit token-level partials (it reads
    # completed transcript records), but the envelope's whole-answer delta satisfies
    # a lenient SSE consumer. Rejected for text/json in _check_unsupported_flags,
    # mirroring claude's "only works with --output-format=stream-json" constraint.
    ap.add_argument("--include-partial-messages", action="store_true",
                    help="accepted no-op (stream-json only)")
    ap.add_argument("--resume")
    ap.add_argument("--session-id")
    ap.add_argument("--full-auto", action="store_true",
                    help="bypassPermissions/auto mode (default acceptEdits)")
    # No -c alias: -c/--continue is out of scope, so it must not silently bind
    # to --cwd. --cwd stays long-form only.
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("-d", "--db", default=os.path.join(os.getcwd(), "clawp.sqlite"))
    ap.add_argument("--history", action="store_true",
                    help="view recent logged turns instead of running")
    ap.add_argument("-n", type=int, default=10, help="rows for --history")
    for flag in PASSTHROUGH_FLAGS:
        ap.add_argument(flag)
    for flag in BOOL_PASSTHROUGH_FLAGS:
        ap.add_argument(flag, action="store_true")
    # These claude flags only function under `claude -p`; clawp exits 2 on them.
    for flag in PRINT_ONLY_FLAGS:
        ap.add_argument(flag, dest="_unsupported_" + flag.lstrip("-"),
                        nargs="?", const=True, default=None)

    return ap


def _check_unsupported_flags(args):
    for flag in PRINT_ONLY_FLAGS:
        if getattr(args, "_unsupported_" + flag.lstrip("-")) is not None:
            raise CwError(2, f"{flag}: unsupported by clawp (print-only feature)")
    # claude's --include-partial-messages only works with stream-json; clawp honors
    # it as a no-op there (it can't produce token-level partials, but the envelope's
    # whole-answer delta is valid stream-json) and rejects it for text/json.
    if args.include_partial_messages and args.output_format != "stream-json":
        raise CwError(2, "--include-partial-messages requires "
                      "--output-format stream-json")


def main():
    args = build_parser().parse_args()

    if args.history:
        raise SystemExit(show_history(args.db, args.n))

    _check_unsupported_flags(args)

    # Streaming input: stdin is an NDJSON turn stream, not a single prompt.
    if args.input_format == "stream-json":
        if args.output_format != "stream-json":
            raise CwError(2, "--input-format stream-json requires "
                          "--output-format stream-json")
        raise SystemExit(run_stream_turns(args, args.cwd, args.db))

    if not args.prompt or args.prompt == "-":
        if sys.stdin.isatty():
            raise CwError(2, "no prompt (positional arg or stdin)")
        args.prompt = sys.stdin.read().strip()
        if not args.prompt:
            raise CwError(2, "no prompt (positional arg or stdin)")

    rej = prefix_rejection(args.prompt)
    if rej:
        raise CwError(2, rej)

    raise SystemExit(run_print_turn(args, args.cwd, args.db))


if __name__ == "__main__":
    main()
