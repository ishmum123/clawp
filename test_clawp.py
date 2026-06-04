#!/usr/bin/env python3
"""Unit tests for clawp's pure screen-parsing helpers. No live claude needed.

Run: python3 test_clawp.py
"""
import io
import json
import sys

import clawp

# A finished turn: assistant block, "done" summary (no ellipsis), idle prompt.
DONE = (
    "❯ list two primes\n\n"
    "⏺ 2\n  3\n\n"
    "✻ Baked for 2s\n\n"
    "────────\n❯ \n────────\n"
    "  Model: Opus 4.8 | Ctx: 0 | Ctx Used: 0.0%\n  -- INSERT --\n"
)

# Mid-work: a running tool + an animated spinner (both carry an ellipsis).
WORKING = (
    "❯ do a thing\n\n"
    "⏺ Bash(sleep 8 && echo hi)\n  ⎿  Running…\n\n"
    "✽ Imagining… (3s · ↓ 117 tokens)\n\n"
    "────────\n❯ \n────────\n  Model: Opus 4.8 | Ctx: 0\n  -- INSERT --\n"
)

# A blocking full-screen dialog (trust prompt) — no status bar.
TRUST = (
    " Quick safety check: Is this a project you created or one you trust?\n"
    " ❯ 1. Yes, I trust this folder\n   2. No, exit\n"
    " Enter to confirm · Esc to cancel\n"
)

# A permission dialog phrased the way Claude Code asks.
PERMISSION = (
    "❯ run the tests\n\n⏺ Bash(pytest)\n\n"
    " Do you want to proceed?\n ❯ 1. Yes\n   2. Yes, and don't ask again\n   3. No\n"
)


def check(name, cond):
    assert cond, "FAILED: " + name
    print("ok:", name)


check("spinner detected while working", clawp.has_spinner(WORKING))
check("no spinner on a finished turn", not clawp.has_spinner(DONE))

check("trust dialog looks blocked", clawp.looks_blocked(TRUST))
check("permission dialog looks blocked", clawp.looks_blocked(PERMISSION))
check("finished turn is not blocked", not clawp.looks_blocked(DONE))

check("finished turn is at idle prompt", clawp.at_idle_prompt(DONE))
check("trust dialog is not idle prompt", not clawp.at_idle_prompt(TRUST))
check("permission dialog is not idle prompt", not clawp.at_idle_prompt(PERMISSION))

check("scrape pulls the last assistant block", clawp.scrape_reply(DONE) == "2\n3")

# Idle status bar varies by version/plan: 2.1.159 shows "Model: …" (with
# non-breaking spaces); 2.1.143 on Claude Max shows only the mode/effort hints.
IDLE_SERVER = (
    " ▐▛███▜▌   Claude Code v2.1.143\n▝▜█████▛▘  Opus 4.7 with max effort · Claude Max\n"
    "────────\n❯ \n────────\n  ⏵⏵ accept edits on (shift+tab to cycle) · ◈ max · /effort\n"
)
IDLE_LOCAL = (
    "────────\n❯\xa0\n────────\n"
    "  Model:\xa0Opus\xa04.8\xa0|\xa0Ctx:\xa00\xa0|\xa0Ctx\xa0Used:\xa00.0%   ◐ medium · /effort\n"
)
check("idle detected on Max plan without 'Model:'", clawp.at_idle_prompt(IDLE_SERVER))
check("idle detected with 'Model:' status bar", clawp.at_idle_prompt(IDLE_LOCAL))

# First --full-auto launch on a box that never accepted bypass mode shows a
# full-screen warning whose default option is "No, exit".
BYPASS = (" WARNING: Claude Code running in Bypass Permissions mode\n"
          " ❯ 1. No, exit\n   2. Yes, I accept\n")
BYPASS_ON_2 = (" WARNING: Claude Code running in Bypass Permissions mode\n"
               "   1. No, exit\n ❯ 2. Yes, I accept\n")
check("bypass warning is a blocking dialog", clawp.looks_blocked(BYPASS))
check("bypass warning is not the idle prompt", not clawp.at_idle_prompt(BYPASS))
check("accept-confirm false while 'No, exit' is selected",
      not clawp._accept_highlighted(BYPASS))
check("accept-confirm true once 'Yes, I accept' is selected",
      clawp._accept_highlighted(BYPASS_ON_2))

# Fresh-launch idle shows a "What's new" box whose truncated rows end in "…".
# Those live inside the box border (│); the real spinner line never does, so an
# ellipsis sharing a line with a box rule must not read as a spinner.
IDLE_WELCOME = (
    "│   ▝▜█████▛▘   │ What's new                                            │\n"
    "│              │ Added plugin dependency enforcement: disable-chain hi… │\n"
    "│  Claude Max  │ Added worktree.bgIsolation for repos where worktrees… │\n"
    "╰────────────────────────────────────────────────────────────────────╯\n"
    "──────────\n❯\xa0Try \"write a test for <filepath>\"\n──────────\n"
    "  ⏵⏵ accept edits on (shift+tab to cycle)                ◉ xhigh · /effort\n"
)
check("welcome-box ellipses are not a spinner", not clawp.has_spinner(IDLE_WELCOME))
check("welcome-box launch screen is at idle prompt", clawp.at_idle_prompt(IDLE_WELCOME))


# --- transcript parser (pure, no live claude) --------------------------------
# Record shapes mirror the spec's Confirmed section.

def asst(stop_reason, blocks):
    return {"type": "assistant",
            "message": {"role": "assistant", "stop_reason": stop_reason,
                        "content": blocks}}

THINK = {"type": "thinking", "thinking": "let me reason"}
def txt(s):
    return {"type": "text", "text": s}
TOOL_USE = {"type": "tool_use", "name": "Bash", "input": {}}
# A tool_use carrying real name+input, the way a status-label consumer reads it.
TOOL_USE_B = {"type": "tool_use", "id": "toolu_1", "name": "Bash",
              "input": {"command": "bash scripts/query.sh"}}

# stop_reason classification
check("end_turn is terminal", clawp.is_terminal_assistant(asst("end_turn", [txt("hi")])))
check("max_tokens is terminal", clawp.is_terminal_assistant(asst("max_tokens", [txt("x")])))
check("refusal is terminal", clawp.is_terminal_assistant(asst("refusal", [txt("x")])))
check("tool_use is continuation", not clawp.is_terminal_assistant(asst("tool_use", [TOOL_USE])))
check("pause_turn is continuation", not clawp.is_terminal_assistant(asst("pause_turn", [txt("x")])))
check("null stop_reason is continuation", not clawp.is_terminal_assistant(asst(None, [txt("x")])))
check("user record is not terminal assistant",
      not clawp.is_terminal_assistant({"type": "user", "message": {"role": "user"}}))

# noise / compaction skipping
check("system record is noise", clawp.is_noise({"type": "system"}))
check("progress record is noise", clawp.is_noise({"type": "progress"}))
check("compact_boundary is noise",
      clawp.is_noise({"type": "system", "subtype": "compact_boundary"}))
check("isCompactSummary user is noise",
      clawp.is_noise({"type": "user", "isCompactSummary": True,
                   "message": {"role": "user", "content": []}}))
check("real assistant is not noise", not clawp.is_noise(asst("end_turn", [txt("x")])))
check("real user is not noise",
      not clawp.is_noise({"type": "user", "message": {"role": "user", "content": []}}))

# thinking-block exclusion in extracted text
check("assistant_text excludes thinking",
      clawp.assistant_text(asst("end_turn", [THINK, txt("answer "), txt("here")]))
      == "answer here")
check("assistant_text ignores tool_use blocks",
      clawp.assistant_text(asst("tool_use", [TOOL_USE])) == "")

# final-answer extraction from a record list
RECORDS = [
    {"type": "system", "subtype": "permission-mode"},
    {"type": "user", "message": {"role": "user", "content": [txt("q")]}},
    asst("tool_use", [THINK, TOOL_USE]),
    {"type": "user", "message": {"role": "user",
     "content": [{"type": "tool_result", "content": "ok"}]}},
    asst("end_turn", [THINK, txt("the final answer")]),
    {"type": "system", "subtype": "compact_boundary"},   # trailing noise after end_turn
]
check("final_answer returns last terminal assistant text",
      clawp.final_answer(RECORDS) == "the final answer")
check("final_answer empty when no terminal record",
      clawp.final_answer([asst("tool_use", [TOOL_USE])]) == "")

# turn_in_flight: log-primary guard that stops the screen backstop from settling
# while the transcript shows Claude still owes a reply. The exact dead window that
# returned exit 5: ...tool_use -> tool_result -> trailing noise, no answer record.
NOISE_AFTER = [{"type": "ai-title"}, {"type": "last-prompt"},
               {"type": "permission-mode"}]
TOOL_RESULT = {"type": "user", "message": {"role": "user",
               "content": [{"type": "tool_result", "content": "ok"}]}}
check("in-flight while last real record is a tool_result (post-tool think phase)",
      clawp.turn_in_flight([asst("tool_use", [TOOL_USE]), TOOL_RESULT] + NOISE_AFTER))
check("in-flight while the assistant is still in tool_use",
      clawp.turn_in_flight([asst("tool_use", [TOOL_USE])] + NOISE_AFTER))
check("in-flight right after the echoed user prompt, before any reply",
      clawp.turn_in_flight([{"type": "user",
       "message": {"role": "user", "content": [txt("q")]}}]))
check("NOT in-flight once a terminal assistant answer is the last real record",
      not clawp.turn_in_flight(
          [TOOL_RESULT, asst("end_turn", [txt("done")])] + NOISE_AFTER))
check("NOT in-flight on an empty record slice", not clawp.turn_in_flight([]))

# stream_event mapping
check("stream_event skips noise", clawp.stream_event({"type": "system"}) is None)
check("stream_event skips compaction",
      clawp.stream_event({"type": "system", "subtype": "compact_boundary"}) is None)
check("stream_event maps assistant text",
      clawp.stream_event(asst("end_turn", [THINK, txt("hello")]))
      == {"type": "assistant", "text": "hello"})
check("stream_event skips thinking-only assistant",
      clawp.stream_event(asst("tool_use", [THINK])) is None)
check("stream_event emits tool_use as an assistant/message event",
      clawp.stream_event(asst("tool_use", [THINK, TOOL_USE_B]))
      == {"type": "assistant", "message": {"content": [TOOL_USE_B]}})
check("stream_event keeps interstitial text alongside tool_use",
      clawp.stream_event(asst("tool_use", [txt("Let me check."), TOOL_USE_B]))
      == {"type": "assistant", "message": {"content": [
          {"type": "text", "text": "Let me check."}, TOOL_USE_B]}})
check("stream_event maps tool_result",
      clawp.stream_event({"type": "user", "message": {"role": "user",
       "content": [{"type": "tool_result", "content": "ok"}]}})
      == {"type": "tool_result"})
check("stream_event skips echoed user prompt",
      clawp.stream_event({"type": "user", "message": {"role": "user",
       "content": [txt("my prompt")]}}) is None)

# claude_version regex (banner scrape for diagnostics)
check("claude_version pulls version from banner",
      clawp.claude_version("  ✻ Welcome to Claude Code v2.1.159\n") == "2.1.159")
check("claude_version empty when banner absent",
      clawp.claude_version("Model: Opus 4.8 | Ctx: 0") == "")


# stream ordering: a record flushed after the terminal end_turn in the SAME
# batch must not leak into the stream. Mirrors run_print_turn's emit loop.
def emit_batch(new):
    emitted = []
    answer = None
    seen = 0
    done = False
    for rec in new:
        seen += 1
        if clawp.is_terminal_assistant(rec):
            answer = clawp.assistant_text(rec)
            ev = clawp.stream_event(rec)
            if ev is not None:
                emitted.append(ev)
            done = True
            break
        ev = clawp.stream_event(rec)
        if ev is not None:
            emitted.append(ev)
    return emitted, answer, seen, done

TRAILING_USER = {"type": "user", "message": {"role": "user",
                 "content": [{"type": "tool_result", "content": "late"}]}}
BATCH = [
    {"type": "user", "message": {"role": "user",
     "content": [{"type": "tool_result", "content": "ok"}]}},
    asst("end_turn", [txt("the answer")]),
    TRAILING_USER,                              # flushed after end_turn
    asst("end_turn", [txt("never reached")]),
]
_ev, _ans, _seen, _done = emit_batch(BATCH)
check("stream stops at terminal, no trailing leak",
      _ev == [{"type": "tool_result"}, {"type": "assistant", "text": "the answer"}])
check("answer is the terminal record's text", _ans == "the answer")
check("seen stops at the terminal record", _seen == 2)
check("batch reports done at terminal", _done is True)

# no-op compatibility flags: accepted for `claude -p` parity, never forwarded.
_parser = clawp.build_parser()
_args = _parser.parse_args(
    ["--verbose", "--no-session-persistence", "--model", "opus", "hi"])
check("no-op flags parse without error", _args.prompt == "hi")
check("no-op flags are not forwarded to the launch",
      clawp._passthrough_args(_args) == ["--model", "opus"])

# --tools (built-in tool allowlist) is a session flag (no print-only note in
# `claude --help`), so it's forwarded to the interactive launch verbatim. Like
# its --allowedTools/--add-dir siblings, the value is one token (comma-separated).
check("--tools is forwarded to the launch",
      clawp._passthrough_args(
          clawp.build_parser().parse_args(["--tools", "Bash,Edit", "hi"]))
      == ["--tools", "Bash,Edit"])
# `--tools ""` is claude's "disable the built-in toolset" (Skill/LSP still load).
# The empty string must reach claude verbatim — absent (None) is dropped, "" is not.
check("--tools empty-string is forwarded verbatim, not dropped like absent (None)",
      clawp._passthrough_args(
          clawp.build_parser().parse_args(["--tools", "", "hi"]))
      == ["--tools", ""])

# --disable-slash-commands ("Disable all skills") has no print-only note in
# `claude --help`, so it's a session flag forwarded to the launch — but as a bare
# flag (no value), clawp's first boolean passthrough.
check("--disable-slash-commands parses as a boolean flag defaulting off",
      clawp.build_parser().parse_args(["hi"]).disable_slash_commands is False)
check("--disable-slash-commands is forwarded as a bare flag (no value)",
      clawp._passthrough_args(
          clawp.build_parser().parse_args(["--disable-slash-commands", "hi"]))
      == ["--disable-slash-commands"])
check("--disable-slash-commands absent is not forwarded",
      clawp._passthrough_args(clawp.build_parser().parse_args(["hi"])) == [])

# The idle backstop only decides a turn when no terminal transcript record
# arrives; its stable window (STABLE_NEEDED * POLL) must outlast a natural
# mid-turn streaming pause (~2.4s measured on 2.1.159) or it truncates early.
check("idle-backstop window outlasts the observed ~2.4s mid-turn pause",
      clawp.STABLE_NEEDED * clawp.POLL > 2.4)


# send_text must isolate the paste buffer per pane. Every clawp process shares
# one tmux server, whose unnamed buffer is a single most-recent slot; concurrent
# turns loading/pasting the unnamed buffer cross prompts. So both load and paste
# must name the buffer after the pane (-b <name>) — the key everything else is
# targeted by. (Pure argv check; the live N-way race is in test_concurrency_live.py.)
def _record_send_text(name, text):
    calls, real_tmux, real_sleep = [], clawp.tmux, clawp.time.sleep
    clawp.tmux = lambda *a, **k: calls.append(a)
    clawp.time.sleep = lambda *a, **k: None
    try:
        clawp.send_text(name, text)
    finally:
        clawp.tmux, clawp.time.sleep = real_tmux, real_sleep
    return calls

_st = _record_send_text("clawp-1a2b3c4d", "hello")
def _argv(verb):
    return next(a for a in _st if a[:1] == (verb,))
def _flag(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv else None
check("send_text loads a pane-named buffer, not the shared unnamed one",
      _flag(_argv("load-buffer"), "-b") == "clawp-1a2b3c4d")
check("send_text pastes that same pane-named buffer",
      _flag(_argv("paste-buffer"), "-b") == "clawp-1a2b3c4d")


# --- stream-json input parser ------------------------------------------------
# claude --input-format stream-json: one user message per stdin line. The parser
# pulls out the prompt verbatim; non-user/malformed/empty lines are skipped (None).
def user_msg(content):
    return json.dumps({"type": "user", "message": {"role": "user",
                       "content": content}, "parent_tool_use_id": None})

check("input parser pulls string content",
      clawp.stream_input_prompt(user_msg("hello there")) == "hello there")
check("input parser joins text blocks",
      clawp.stream_input_prompt(user_msg([txt("a "), txt("b")])) == "a b")
check("input parser drops non-text blocks, keeps text",
      clawp.stream_input_prompt(user_msg(
          [{"type": "image", "source": {}}, txt("caption")])) == "caption")
check("input parser ignores non-user records",
      clawp.stream_input_prompt(json.dumps({"type": "result", "result": "x"})) is None)
check("input parser ignores malformed json",
      clawp.stream_input_prompt("{not json") is None)
check("input parser ignores empty string content",
      clawp.stream_input_prompt(user_msg("")) is None)
check("input parser ignores image-only content",
      clawp.stream_input_prompt(user_msg([{"type": "image", "source": {}}])) is None)
check("input parser ignores missing content",
      clawp.stream_input_prompt(json.dumps({"type": "user", "message": {}})) is None)
check("input parser preserves a leading slash verbatim",
      clawp.stream_input_prompt(user_msg("/compact please")) == "/compact please")

# Leading '/' and '!' can't be sent (TUI command / shell mode); '@', '#', and a
# mid-text slash are fine. The parser still extracts verbatim above — the guard is
# a separate, explicit refusal that happens before send.
check("prefix guard rejects leading slash", clawp.prefix_rejection("/help") is not None)
check("prefix guard rejects leading bang", clawp.prefix_rejection("!touch x") is not None)
check("prefix guard rejects leading bang after whitespace",
      clawp.prefix_rejection("  !rm -rf x") is not None)
check("prefix guard allows leading at", clawp.prefix_rejection("@file.py explain") is None)
check("prefix guard allows leading hash", clawp.prefix_rejection("#note this") is None)
check("prefix guard allows a mid-text slash", clawp.prefix_rejection("what is 6/2?") is None)
check("prefix guard allows normal text", clawp.prefix_rejection("hello there") is None)
check("prefix guard allows empty", clawp.prefix_rejection("") is None)


# --- mid-turn block detection (issue-1: warm-pane / answer-prose markers) -----
# A real dialog OWNS the screen: a marker is present AND the idle status bar is
# gone. Answer prose quoting a marker ("Do you want…"), or a prior turn's marker
# left in a warm pane's scrollback, keeps the status bar — so it must NOT block.
ANSWER_WITH_MARKER = (
    "❯ what next?\n\n"
    "⏺ Good question. Do you want to start with the data model or the API?\n\n"
    "✻ Baked for 2s\n\n"
    "────────\n❯ \n────────\n"
    "  Model: Opus 4.8 | Ctx: 0 | Ctx Used: 0.0%\n  -- INSERT --\n"
)
check("has_idle_bar present on a finished turn", clawp.has_idle_bar(DONE))
check("has_idle_bar absent during a permission dialog", not clawp.has_idle_bar(PERMISSION))
check("turn_blocked: real permission dialog blocks", clawp.turn_blocked(PERMISSION))
check("turn_blocked: trust dialog blocks", clawp.turn_blocked(TRUST))
check("turn_blocked: bypass warning blocks", clawp.turn_blocked(BYPASS))
check("turn_blocked: answer quoting a marker does NOT block (idle bar present)",
      not clawp.turn_blocked(ANSWER_WITH_MARKER))
check("turn_blocked: plain finished turn does not block", not clawp.turn_blocked(DONE))
# at_idle_prompt still holds after the has_idle_bar refactor.
check("at_idle_prompt unchanged on a finished turn", clawp.at_idle_prompt(DONE))
check("at_idle_prompt false during a permission dialog", not clawp.at_idle_prompt(PERMISSION))


# --- claude-shaped stream-json output (streaming-input mode) ------------------
# One turn -> a full message envelope carrying the whole answer as one delta, the
# assembled assistant message, then the result; preceded once by a system/init.
_init = clawp._init_event("sess-123", "claude-sonnet-4-6", "/tmp/x")
check("init event is system/init carrying the session id",
      _init["type"] == "system" and _init["subtype"] == "init"
      and _init["session_id"] == "sess-123")

_evs = clawp._turn_events("the answer", "sess-123", "claude-sonnet-4-6", 1200, 2)
def _etype(e):
    return e["event"]["type"] if e["type"] == "stream_event" else e["type"]
check("turn events are the full claude envelope, in order",
      [_etype(e) for e in _evs] == [
          "message_start", "content_block_start", "content_block_delta",
          "content_block_stop", "message_delta", "message_stop",
          "assistant", "result"])
_delta = next(e for e in _evs if _etype(e) == "content_block_delta")
check("the whole answer rides a single text_delta",
      _delta["event"]["delta"] == {"type": "text_delta", "text": "the answer"})
check("assembled assistant message carries the text",
      next(e for e in _evs if e["type"] == "assistant")["message"]["content"]
      == [{"type": "text", "text": "the answer"}])
_res = _evs[-1]
check("final result carries answer, session id, not-error",
      _res["type"] == "result" and _res["subtype"] == "success"
      and _res["result"] == "the answer" and _res["session_id"] == "sess-123"
      and _res["is_error"] is False)
check("every stream_event is tagged with the session id",
      all(e["session_id"] == "sess-123" for e in _evs if e["type"] == "stream_event"))

# --input-format is now implemented (a real flag), not rejected as print-only.
check("--input-format removed from print-only rejects",
      "--input-format" not in clawp.PRINT_ONLY_FLAGS)
check("--input-format parses as a real flag",
      clawp.build_parser().parse_args(
          ["--input-format", "stream-json", "--output-format", "stream-json"]
      ).input_format == "stream-json")
check("--input-format defaults to text",
      clawp.build_parser().parse_args(["hi"]).input_format == "text")

# --include-partial-messages: claude's token-level-delta toggle, which "only works
# with --output-format=stream-json". clawp can't emit partials (it reads completed
# transcript records), but its stream-json envelope already carries a whole-answer
# delta, so the flag is a no-op there and a hard error for text/json — mirroring
# claude's own constraint. The other print-only flags change the result/protocol,
# so they still loud-fail unconditionally.
check("--include-partial-messages removed from print-only rejects",
      "--include-partial-messages" not in clawp.PRINT_ONLY_FLAGS)
check("--include-partial-messages parses as a real boolean flag",
      clawp.build_parser().parse_args(
          ["--include-partial-messages", "--output-format", "stream-json", "hi"]
      ).include_partial_messages is True)
check("--include-partial-messages defaults off",
      clawp.build_parser().parse_args(["hi"]).include_partial_messages is False)
check("--include-partial-messages is not forwarded to the launch",
      clawp._passthrough_args(clawp.build_parser().parse_args(
          ["--include-partial-messages", "--output-format", "stream-json", "hi"]))
      == [])

def _flag_check_code(argv):
    real = sys.stderr            # CwError writes to stderr on construct; mute it
    sys.stderr = io.StringIO()
    try:
        clawp._check_unsupported_flags(clawp.build_parser().parse_args(argv))
        return None
    except clawp.CwError as e:
        return e.code
    finally:
        sys.stderr = real

check("--include-partial-messages is a no-op under stream-json (no raise)",
      _flag_check_code(
          ["--include-partial-messages", "--output-format", "stream-json", "hi"])
      is None)
check("--include-partial-messages exits 2 for text output",
      _flag_check_code(["--include-partial-messages", "hi"]) == 2)
check("--include-partial-messages exits 2 for json output",
      _flag_check_code(
          ["--include-partial-messages", "--output-format", "json", "hi"]) == 2)
check("other print-only flags still exit 2 even under stream-json",
      _flag_check_code(
          ["--max-turns", "5", "--output-format", "stream-json", "hi"]) == 2)

# --max-budget-usd is claude's current print-only work-bound flag (successor to the
# removed --max-turns). clawp can't honor it and must not silently drop it (it
# bounds the agent's work → changes the result), so it joins the reject set and gets
# clawp's curated message instead of argparse's generic "unrecognized arguments".
check("--max-budget-usd is in the print-only reject set",
      "--max-budget-usd" in clawp.PRINT_ONLY_FLAGS)
check("--max-budget-usd exits 2 with clawp's curated reject",
      _flag_check_code(["--max-budget-usd", "5", "hi"]) == 2)


print("\nall passed")
