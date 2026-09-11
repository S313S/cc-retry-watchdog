#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for the stuck-session decision.

Every fixture below is a hand-reproduced Claude Code terminal layout. The point
is not coverage of the code but coverage of the *screens*: the cost of a false
positive is typing into someone's working session, so each "must not fire" case
is a real situation that previously looked like a drop.

    python3 tests/test_analyze.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from watchdog import analyze, is_stalled_injection, session_state  # noqa: E402

TAIL = 80
BOX = "─" * 60
STATUS = ("  [Opus 5] ░░░░░░░░░░ 116.0k (12%)\n"
          "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents")

AGENTS = ('  ⏺ main\n  ◯ general-purpose  Reading run_sectioned_task in sectioning.py                    2m 24s · ↓ 66.9k tokens')

CASES = []


def case(name, expect, screen, require_error=True):
    CASES.append((name, expect, screen, require_error))


# ---------------------------------------------------------------- must fire

# The duration line carries " · ..." suffixes: the wall-clock finish time
# (always, in practice), a token budget, hidden-message and still-running
# notes. Anchoring the pattern at the duration made every one of them read as
# "the turn moved on", which took the polling path out of service entirely --
# a real session sat parked for 71 minutes on the first fixture below.
case("parked: duration line carries the done-at time", True, u"""\
● API Error: Connection lost mid-response. The response above may be incomplete.

✳ Cogitated for 1h 5m 49s · done 19:14

{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("parked: duration line carries budget and hidden-message notes", True, u"""\
● API Error: Server error mid-response. The response above may be incomplete.

* Churned for 25s · 12.3k / 50k (25%) · 2 nudges · 3 messages hidden (/focus to show)

{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("parked: only a duration line after the error", True, u"""\
⏺ Let me write that into the design doc.

● API Error: Connection closed mid-response. The response
  above may be incomplete.

* Churned for 1m 1s

{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("parked: 'Jump to bottom' overlay after the error", True, u"""\
❯ please, continue

● API Error: Connection closed mid-response. The response
  above may be incomplete.
                    Jump to bottom (click) ↓

{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("parked: a previous manual retry is visible above", True, u"""\
* Worked for 3m 31s

❯ please, continue

● API Error: Connection closed mid-response. The response above may be incomplete.

* Brewed for 2m 45s

{box}
❯
{box}
  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents""".format(box=BOX))

case("parked: error fits on one line", True, u"""\
● API Error: Connection closed mid-response. The response above may be incomplete.
✻ Cogitated for 12s
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("parked: input box shows only the placeholder hint", True, u"""\
● API Error: Connection closed mid-response. The response above may be incomplete.
{box}
❯ Try "edit <filepath> to..."
{box}
{status}""".format(box=BOX, status=STATUS))

case("parked: 'Connection lost' wording instead of 'closed'", True, u"""\
● API Error: Connection lost mid-response. The response above may be incomplete.

* Baked for 7m 22s

{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("parked: a manual retry of a 'lost' drop dropped again", True, u"""\
● API Error: Connection lost mid-response. The response above may be incomplete.

* Baked for 7m 22s

❯ please, continue

● API Error: Connection lost mid-response. The response
  above may be incomplete.

* Crunched for 27s

{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("parked: 'The response stopped arriving'", True, u"""\
⏺ Running the migration now.

● API Error: The response stopped arriving. The response above may be incomplete.

* Simmered for 3m 4s
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("parked: server error mid-response", True, u"""\
● API Error: Server error mid-response. The response above may be incomplete.
* Deliberated for 41s
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("parked: laptop slept mid-response", True, u"""\
● API Error: Your computer went to sleep mid-response. The response
  above may be incomplete.
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("parked: nothing produced yet -- 'Try again.'", True, u"""\
❯ summarize the worklog

● API Error: Connection lost before a response was produced. Try again.
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("parked: pre-2.1.226 'while thinking' wording", True, u"""\
● API Error: Response stalled while thinking, before producing a response. Try again.
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("parked: transport error raised around the stream", True, u"""\
● API Error: Connection to the API was lost (ECONNRESET). This is usually
  temporary — try again.
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("hook ticket: idle session, no error on screen", True, u"""\
⏺ Wrote docs/design/schema.md.

* Churned for 25s
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS), require_error=False)


# A background agent outlives the turn the drop killed, so the agent panel keeps
# drawing under the error. This read as "the turn moved on" and cost a real
# session eleven hours parked on a drop nobody retried.

case("parked: a background agent is still running", True, u"""\
⏺ D-008 卡与诊断已落档，造红 agent 正在补 case。

● API Error: Connection lost mid-response. The response above may be incomplete.

✻ Churned for 59m 4s
✻ Waiting for 1 background agent to finish

{box}
❯
{box}
{status}
{agents}""".format(box=BOX, status=STATUS, agents=AGENTS))

case("parked: several background agents still running", True, u"""\
● API Error: The response stopped arriving. The response above may be incomplete.

✻ Waiting for 3 background agents to finish

{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("parked: a background agent reported back", True, u"""\
● API Error: Connection lost mid-response. The response above may be incomplete.

⏺ Agent "诊断：第二轮执行期的时间去向" finished · 6m 32s
✻ Waiting for 1 background agent to finish

{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("parked: backgrounded-agent receipt under the error", True, u"""\
● API Error: Server error mid-response. The response above may be incomplete.

  ⎿ Backgrounded agent (↓ to manage · ctrl+o to expand)

{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))


# A recap is drawn because nobody has typed for a while, which is exactly what
# a drop leaves behind -- so it says nothing about whether the turn closed
# cleanly. Reading it as a tombstone blinded the polling path to precisely the
# drops that had sat longest: one tab was reported
# `ok: recap after the error -- turn ended normally` while it sat parked on an
# unrescued drop and a human typed the retry in by hand.
case("parked: a recap was drawn over the drop", True, u"""\
● Monitor event: "repeatability trial results"

● That's the last stale monitor expiring -- its subject finished and was
  reported in full, so there's nothing to re-arm.

● API Error: Connection lost mid-response. The response above may be incomplete.

✳ Baked for 10s · done 18:09

※ recap: Goal was rerunning QUOTE-1's coding to deliver a before/after table;
  it's blocked because the proxy node keeps cutting the engine's connection
  (production batch: 0 of 17 sessions). Next: you switch that node, then I
  replay the comparison run. (disable recaps in /config)
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

# The recap's prose is model-written and summarizes the session, not the last
# turn: it can say the task is done while the turn that said so was cut off
# mid-sentence. Only the structure above it is evidence.
case("parked: recap text claims the task is complete", True, u"""\
● API Error: Connection closed mid-response. The response above may be incomplete.
* Churned for 8s
※ recap: task complete. (disable recaps in /config)
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))


# ---------------------------------------------------------------- must NOT fire

# The mirror image of the four above: the main loop is the one drawing again, so
# the turn genuinely moved on and a retry would trample it.

case("the main loop came back and launched an agent", False, u"""\
● API Error: Connection lost mid-response. The response above may be incomplete.

⏺ Agent(造红：seam 补 section-wall-clock case)

{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("the main loop came back and kept writing", False, u"""\
● API Error: Connection lost mid-response. The response above may be incomplete.

⏺ 诊断很硬，五条根因（带文件:行），先造红。

{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("finished normally, waiting for the user", False, u"""\
File: docs/design/semantic-recall.md (worklog snapshot alongside).

* Churned for 25s

※ recap: picked FTS5 keyword prefilter + main-engine dedup, no embeddings.
  (disable recaps in /config)
                              Image in clipboard · ctrl+v to paste
{box}
❯
{box}
  [Fable 5] ░░░░░░░░░░ 70.8k (7%)
  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents""".format(box=BOX))

case("built-in retry is running", False, u"""\
* API error · Retrying in 0s · attempt 1/15
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("built-in retry counting down after the drop", False, u"""\
● API Error: Connection closed mid-response. The response above may be incomplete.
* API error · Retrying in 7s · attempt 3/15
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("already recovered and kept writing", False, u"""\
● API Error: Connection closed mid-response. The response above may be incomplete.

❯ please, continue

⏺ Right, continuing from where it cut off. Schema is in docs/design/.

* Churned for 44s
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

# The guard that replaced the recap rule: a turn that really moved on leaves
# the echoed user message and its reply *above* the recap, and those still
# disqualify it. This is the case the old rule was reaching for.
case("recovered, ran another turn, then a recap", False, u"""\
● API Error: Connection closed mid-response. The response above may be incomplete.

❯ please, continue

⏺ Right, continuing from where it cut off. Schema is in docs/design/.

* Churned for 44s
※ recap: schema drafted; next is the migration. (disable recaps in /config)
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("actively working (spinner with a timer)", False, u"""\
● API Error: Connection closed mid-response. The response above may be incomplete.
✻ Sprouting… (15s · ↓ 447 tokens · esc to interrupt)
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("user has half-typed something", False, u"""\
● API Error: Connection closed mid-response. The response above may be incomplete.
* Churned for 8s
{box}
❯ can you also add
{box}
{status}""".format(box=BOX, status=STATUS))

case("the text merely appears in the conversation", False, u"""\
⏺ The exact wording is "API Error: Connection closed mid-response. The
  response above may be incomplete." -- that is the mid-stream case.

* Churned for 30s
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("waiting on a permission prompt", False, u"""\
● API Error: Connection closed mid-response. The response above may be incomplete.

Do you want to proceed?
  1. Yes
  2. No
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("subagent hit the error but the main loop runs on", False, u"""\
⏺ Agent terminated early due to an API error: API Error: Connection closed
  mid-response. The response above may be incomplete.

✻ Accomplishing… (32s · ↓ 1.2k tokens · esc to interrupt)
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("a plain shell, not Claude Code", False, u"""\
user@host ~ % ls
Desktop  Documents  Downloads
user@host ~ % """)

case("error has scrolled out of view", False, u"""\
⏺ Next I will write the conclusions into the doc.
* Churned for 12s
※ recap: done.
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("hook ticket but the session is busy again", False, u"""\
⏺ Continuing.
✻ Sprouting… (15s · ↓ 447 tokens · esc to interrupt)
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS), require_error=False)

case("hook ticket but the user is typing", False, u"""\
⏺ Continuing.
* Churned for 8s
{box}
❯ wait, do it differently
{box}
{status}""".format(box=BOX, status=STATUS), require_error=False)

# An "API Error:" that is a real failure, not a dropped stream. Retrying these
# burns a turn at best; for the 400s it re-sends what the server just rejected.

case("api error: the user pressed esc", False, u"""\
● API Error: Request was aborted.
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("api error: bad credentials", False, u"""\
● API Error: 401 Invalid API key · Please run /login
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("api error: malformed tool_use in history", False, u"""\
● API Error: 400 duplicate tool_use ID in conversation history.
  Run /rewind to recover the conversation.
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("api error: context window exhausted", False, u"""\
● API Error: The model has reached its context window limit.
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("api error: bare fallback with no detail", False, u"""\
● API Error: Please wait a moment and try again.
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))


# The rule above the input box carries the session's name now. It is chrome,
# but it reads as content, and content after the error means the turn moved on
# -- so this one line silently took the whole polling path out of service.
TITLED = u"─" * 52 + u" report formatting review ─"

case("parked: the input box rule carries the session name", True, u"""\
● API Error: Connection lost mid-response. The response above may be incomplete.

✳ Brewed for 6m 46s · done 8:48

{titled}
❯
{box}
{status}""".format(titled=TITLED, box=BOX, status=STATUS))

case("parked: named rule below a background-agent line", True, u"""\
● API Error: Connection lost mid-response. The response above may be incomplete.

✻ Waiting for 2 background agents to finish

{titled}
❯
{box}
{status}
{agents}""".format(titled=TITLED, box=BOX, status=STATUS, agents=AGENTS))

# Must not fire: the bound on the label is what keeps a real output line from
# passing as a rule. A drawn separator the model printed itself is short; a
# sentence that happens to open with one is not.
case("moved on: output after the error opens with a drawn rule", False, u"""\
● API Error: Connection lost mid-response. The response above may be incomplete.

{rule} the retry landed, here is what the second pass found in the loader path

{box}
❯
{box}
{status}""".format(rule=u"─" * 12, box=BOX, status=STATUS))


# ---------------------------------------------------------- stalled injections
#
# An injection that lands as a paste leaves the retry text sitting unsubmitted
# in the input box. The box is then non-empty forever, which is exactly what the
# watchdog refuses to type into -- so the session can never be rescued again
# until someone clears it. Recognizing that state is what makes it recoverable;
# recognizing it too eagerly would delete text the user typed.

STALL_CASES = []


def stall(name, expect, body):
    STALL_CASES.append((name, expect, u"""\
● API Error: Connection lost mid-response. The response above may be incomplete.
{box}
❯ {body}
{box}
{status}""".format(box=BOX, status=STATUS, body=body)))


stall("our retry text, stalled in the box", True, u"please, continue")
stall("same, with the non-breaking space the TUI draws", True, u"\xa0please, continue")
stall("empty box is not a stall", False, u"")
# Must not fire: everything below is the user's own text and clearing it is theft.
stall("the user typed their own message", False, u"回到 D-008，先跑复验")
stall("the user's text merely starts with it", False, u"please, continue with the refactor")
stall("the user's text merely contains it", False, u"as I said: please, continue")

# The sessions.json snapshot maps every sweep verdict to a coarse state that
# cc-needs-you reads. A reworded verdict must fail here, not confuse a tool
# downstream. One case per verdict wording scan_once can emit.
STATE_CASES = [
    ("skipped: tty excluded",                                        "skipped"),
    ("skipped: title excluded",                                      "skipped"),
    ("skipped: no claude process here",                              "skipped"),
    ("ok: session busy (esc to interrupt)",                          "working"),
    ("ok: session busy (Retrying in\\s+\\d+s); ticket voided",       "working"),
    ("stood down at the last moment: session busy (esc to interrupt)", "working"),
    # Someone is composing in a healthy session: leave them alone.
    ("ok: input box not empty (draft), skipping",                     "typing"),
    # Same sentence, opposite meaning: the turn behind this box died on a drop
    # and the draft is the only reason it has not been rescued. Reported as
    # `typing` it read as "you are on it" and cc-needs-you suppressed the
    # notification -- the state most in need of a human was the one guaranteed
    # to stay quiet. One session sat like this from 00:16 until the next
    # morning while its draft, `修 D-059，句级实体闸`, went stale in the box.
    ("ok: input box not empty (修 D-059，句级实体闸), skipping; "
     "the drop behind it is still unrescued",                         "blocked"),
    ("ok: input box not empty (please, continue), skipping; cleared a stalled injection; "
     "the drop behind it is still unrescued",                         "blocked"),
    # A stalled injection we have not managed to clear is still blocked.
    ("ok: input box not empty (please, continue), skipping; clear failed; "
     "the drop behind it is still unrescued",                         "blocked"),
    ("ok: no such error this turn",                                  "idle"),
    ("ok: output after the error (Let me try) -- turn moved on",     "idle"),
    ("ok: no input box found -- probably not a Claude Code UI",      "not_claude_ui"),
    ("ok: blank screen",                                             "not_claude_ui"),
    ("ok: empty content area",                                       "not_claude_ui"),
    ("stuck but hit the retry cap (6) -- needs a human",             "gave_up"),
    ("looks stuck (1/2 confirmations)",                              "dropped"),
    ("stuck but cooling down (12s left)",                            "dropped"),
    ("[DRY-RUN] would inject 'please, continue'",                    "dropped"),
    ("injected 'please, continue' (hook, retry #1)",                 "dropped"),
    ("injection failed: osascript timed out",                        "dropped"),
]


def main():
    ok = fail = 0
    for name, expect, screen, require_error in CASES:
        got, reason, _ = analyze(screen, TAIL, require_error=require_error)
        if got == expect:
            ok += 1
            print("  PASS  %-46s -> %-5s (%s)" % (name, got, reason))
        else:
            fail += 1
            print("  FAIL  %-46s expected %s got %s (%s)" % (name, expect, got, reason))
    for name, expect, screen in STALL_CASES:
        got = is_stalled_injection(screen, TAIL, "please, continue")
        if got == expect:
            ok += 1
            print("  PASS  stalled=%-5s %s" % (got, name))
        else:
            fail += 1
            print("  FAIL  stalled: %-46s expected %s got %s" % (name, expect, got))
    for msg, expect in STATE_CASES:
        got = session_state(msg)
        if got == expect:
            ok += 1
            print("  PASS  state=%-13s %s" % (got, msg[:50]))
        else:
            fail += 1
            print("  FAIL  state: %-46s expected %s got %s" % (msg[:46], expect, got))
    print("\n%d passed / %d failed" % (ok, fail))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
