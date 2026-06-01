#!/usr/bin/env python3
"""Unit tests for clawp's pure screen-parsing helpers. No live claude needed.

Run: python3 test_clawp.py
"""
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

# stream_event mapping
check("stream_event skips noise", clawp.stream_event({"type": "system"}) is None)
check("stream_event skips compaction",
      clawp.stream_event({"type": "system", "subtype": "compact_boundary"}) is None)
check("stream_event maps assistant text",
      clawp.stream_event(asst("end_turn", [THINK, txt("hello")]))
      == {"type": "assistant", "text": "hello"})
check("stream_event skips thinking-only assistant",
      clawp.stream_event(asst("tool_use", [THINK, TOOL_USE])) is None)
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

print("\nall passed")
