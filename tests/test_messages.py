#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""The message allowlist, checked against real Claude Code strings.

Every line below was lifted verbatim from the Claude Code bundle (the strings
are compiled into the binary at ~/.local/share/claude/versions/<v>), not
invented. Two generations are covered: the message set was rewritten in
2.1.226, where "closed" became "lost" and the two generic messages split into
one per cause. Anything the CLI renders as `API Error: ...` that is *not* a
transport failure belongs in MUST_NOT -- retrying those only burns a turn.

To re-derive this list after a Claude Code upgrade:

    python3 - <<'EOF'
    import os, re, glob
    for p in sorted(glob.glob(os.path.expanduser(
            "~/.local/share/claude/versions/*"))):
        d = open(p, "rb").read()
        for m in re.finditer(rb'\$\{Uw\}\s*:?\s*([^`$"\']{0,110})', d):
            print(p.rsplit("/", 1)[1], "|", m.group(1).decode("utf-8", "replace"))
    EOF

`Uw` is the minified binding for the literal "API Error"; grep the bundle for
`Uw="API Error"` to confirm it before trusting the output.

    python3 tests/test_messages.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from watchdog import ERR_TEXT  # noqa: E402
from hook_stopfailure import PATTERNS  # noqa: E402

# A dropped turn. The watchdog exists for exactly these.
MUST_FIRE = [
    # ---- 2.1.225 and earlier -------------------------------------------
    "API Error: Response stalled mid-stream. The response above may be incomplete.",
    "API Error: Server error mid-response. The response above may be incomplete.",
    "API Error: Connection closed mid-response. The response above may be incomplete.",
    "API Error: Response stalled while thinking, before producing a response. Try again.",
    "API Error: Connection closed while thinking, before producing a response. Try again.",
    # ---- 2.1.227 and later ---------------------------------------------
    "API Error: The response stopped arriving. The response above may be incomplete.",
    "API Error: Server error mid-response. The response above may be incomplete.",
    "API Error: Your computer went to sleep mid-response. The response above may be incomplete.",
    "API Error: Connection lost mid-response. The response above may be incomplete.",
    "API Error: The response stalled before a response was produced. Try again.",
    "API Error: Your computer went to sleep before a response was produced. Try again.",
    "API Error: Connection lost before a response was produced. Try again.",
    # ---- raised around the stream rather than inside it -----------------
    u"API Error: Connection to the API was lost (ECONNRESET). "
    u"This is usually temporary — try again.",
    u"API Error: Connection to the API was lost (ETIMEDOUT). "
    u"This is usually temporary — try again.",
]

# A real failure. Retrying wastes a turn and, for the 400s, repeats a request
# the server already rejected. Never fire.
MUST_NOT_FIRE = [
    "API Error: Request was aborted.",                          # user pressed esc
    u"API Error: 401 Invalid API key \xb7 Please run /login",
    "API Error: 400 due to tool use concurrency issues.",
    "API Error: 400 duplicate tool_use ID in conversation history.",
    "API Error: The model has reached its context window limit.",
    "API Error: Please wait a moment and try again.",            # bare-error fallback
    u"API Error: Usage credits required for 1M context \xb7 ...",
    "API Error: Claude Opus is not available with the Claude Pro plan.",
    "API Error",
    # near misses -- the words are there, the failure is not
    "I hit a connection error while fetching that; the response above is fine.",
    "The docs mention Connection lost mid-response as a known symptom.",
]


def main():
    failed = 0
    for text in MUST_FIRE:
        by_screen = bool(ERR_TEXT.search(text))
        by_hook = any(p in text.lower() for p in PATTERNS)
        ok = by_screen and by_hook
        failed += not ok
        print("  %s  screen=%-5s hook=%-5s  %s"
              % ("PASS" if ok else "FAIL", by_screen, by_hook, text[:66]))

    print()
    for text in MUST_NOT_FIRE:
        by_screen = bool(ERR_TEXT.search(text))
        # The hook also sees the raw JSON, so a bare mention can reach it; only
        # the screen path claims to be safe against prose. Both must stay quiet
        # for the API Error lines, which is what these cases are.
        by_hook = any(p in text.lower() for p in PATTERNS)
        ok = not (by_screen or by_hook)
        failed += not ok
        print("  %s  screen=%-5s hook=%-5s  %s"
              % ("PASS" if ok else "FAIL", by_screen, by_hook, text[:66]))

    total = len(MUST_FIRE) + len(MUST_NOT_FIRE)
    print("\n%d passed / %d failed" % (total - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
