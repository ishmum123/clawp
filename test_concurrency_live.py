#!/usr/bin/env python3
"""LIVE concurrency regression — per-pane tmux paste buffer.

NOT part of the pure suite (test_clawp.py), and deliberately not importable by
it: this spends real subscription turns and needs a logged-in `claude` + `tmux`.
It spawns N clawp turns at once, each told a unique sentinel token. With the
shared unnamed tmux buffer (the original bug) some turn echoes another turn's
sentinel; with the per-pane named buffer each turn returns its own.

Run:   python3 test_concurrency_live.py [N] [ROUNDS]     # defaults: 4 3
Exit:  0 = every turn returned its own sentinel; 1 = a cross (or run error).

It is timing-dependent — ROUNDS>1 because a single round can get lucky.
"""
import concurrent.futures as cf
import json
import os
import subprocess
import sys

CLAWP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clawp.py")


def one_turn(token):
    prompt = "Reply with exactly this token and nothing else: " + token
    p = subprocess.run([sys.executable, CLAWP, "--output-format", "json", prompt],
                       capture_output=True, text=True)
    try:
        return (json.loads(p.stdout).get("result") or "").strip()
    except json.JSONDecodeError:
        return "<no-json rc=%s err=%r>" % (p.returncode, p.stderr.strip()[:80])


def run_round(r, n):
    tokens = ["ZULU%d_%d" % (r, i) for i in range(n)]
    # Real OS-process concurrency: each clawp gets its own uuid/pane/lock, so the
    # only resource they share is the tmux server's buffer — exactly what we test.
    with cf.ThreadPoolExecutor(max_workers=n) as ex:
        answers = list(ex.map(one_turn, tokens))
    clean = True
    for tok, ans in zip(tokens, answers):
        stray = [o for o in tokens if o != tok and o in ans]
        ok = (tok in ans) and not stray
        clean = clean and ok
        print("  expected %12s  got %r%s" % (tok, ans, "" if ok else "  >> CROSSED"))
        if stray:
            print("      (saw another turn's sentinel: %s)" % stray)
    return clean


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    ok = True
    for r in range(rounds):
        print("round %d/%d  (N=%d)" % (r + 1, rounds, n))
        ok = run_round(r, n) and ok
    print("\nPASS: no crosses" if ok else "\nFAIL: prompt cross detected")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
