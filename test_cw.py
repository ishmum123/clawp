#!/usr/bin/env python3
"""Unit tests for cw's pure screen-parsing helpers. No live claude needed.

Run: python3 test_cw.py
"""
import cw

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


check("spinner detected while working", cw.has_spinner(WORKING))
check("no spinner on a finished turn", not cw.has_spinner(DONE))

check("trust dialog looks blocked", cw.looks_blocked(TRUST))
check("permission dialog looks blocked", cw.looks_blocked(PERMISSION))
check("finished turn is not blocked", not cw.looks_blocked(DONE))

check("finished turn is at idle prompt", cw.at_idle_prompt(DONE))
check("trust dialog is not idle prompt", not cw.at_idle_prompt(TRUST))
check("permission dialog is not idle prompt", not cw.at_idle_prompt(PERMISSION))

check("scrape pulls the last assistant block", cw.scrape_reply(DONE) == "2\n3")

print("\nall passed")
