#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for clearing input-box text a ticket proves was abandoned.

Text sitting in the input box is the one thing this watchdog refuses to type
over, and that refusal is load-bearing: it is what keeps a rescue from eating a
message somebody was half-way through writing. But it also means one stray line
in the box shields a session from every rescue that will ever be offered it. A
real drop went unrescued that way on 2026-09-06: the box held the word
`continue`, the hook ticket arrived on time, and the watchdog correctly held
until the ticket expired three minutes later.

`is_stalled_injection` already covers the case where the leftover is our own
retry text, by exact equality. It cannot cover this one, because nothing about
the *text* distinguishes an abandoned line from a message queued while Claude
worked. So the judgement is made entirely on time, and these cases are mostly
about the ways it must decline to make it:

  * text that appeared after the drop is a reply to the error, not a leftover;
  * text that has not yet outlasted the drop by the full grace has an author who
    may still be at the keyboard;
  * no ticket means no drop time, and a guess is not good enough to type over
    somebody's draft.

    python3 tests/test_abandoned_box.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Every path the watchdog writes is derived from this at import time. Set before
# the import so the sweep cases below cannot touch a real log, ticket or state
# file -- one of them writes a ticket, and a stray one is a rescue nobody asked
# for.
HOME = tempfile.mkdtemp(prefix="ccw-abandoned-")
os.environ["CC_AUTORESUME_HOME"] = HOME
import watchdog  # noqa: E402
from watchdog import abandoned_box, track_input_box  # noqa: E402

watchdog.log = lambda *_a, **_k: None

GRACE = 120
DROP = 1_000_000.0                     # when the stream died, per the ticket
TICKET = {"tty": "/dev/ttys004", "ts": DROP}

RESULTS = []


def check(name, expect, got):
    RESULTS.append((name, expect, got))


def box(text, since):
    """Session state whose box has held `text` unchanged since `since`."""
    return {"box_text": text, "box_since": since}


# ------------------------------------------------------------------ must clear

# The case this exists for. `continue` was in the box before the stream died and
# nobody touched it for the whole grace -- there is no author waiting on it.
clear, _why = abandoned_box(box(u"continue", DROP - 30), TICKET, GRACE, DROP + GRACE)
check("the 2026-09-06 case: leftover 'continue'", True, clear)

# Exactly on the boundary counts: the grace is "this long", not "longer than".
clear, _why = abandoned_box(box(u"continue", DROP - 1), TICKET, GRACE, DROP + GRACE)
check("at the grace boundary to the second", True, clear)

# Text put there long before the turn even started is the same case, more so.
clear, _why = abandoned_box(box(u"continue", DROP - 3600), TICKET, GRACE, DROP + 200)
check("text older than the turn that died", True, clear)

# Sampled in the same sweep the drop landed in: still before it, still eligible.
clear, _why = abandoned_box(box(u"continue", DROP), TICKET, GRACE, DROP + GRACE + 5)
check("sampled at the drop instant", True, clear)

# A second, different leftover after an earlier one was cleared. track_input_box
# drops the cleared_box marker when the text changes, so this is clearable again.
st = box(u"continue", DROP - 30)
st["cleared_box"] = u"an older leftover"
clear, _why = abandoned_box(st, TICKET, GRACE, DROP + GRACE)
check("a different text after an earlier clear", True, clear)

# --------------------------------------------------------------- must not fire

# Everything below leaves the box alone. Clearing any of them destroys text the
# user still has a claim on, which is worse than a session that stays parked.

# Typed after the drop: the user watched the error appear and is replying to it.
clear, why = abandoned_box(box(u"什么情况？", DROP + 5), TICKET, GRACE, DROP + GRACE)
check("typed after the drop", False, clear)
check("  ... and says whose it is", True, "theirs" in why)

# A message queued while Claude worked, whose author is still there. They get
# the full grace to notice the dead session and deal with it themselves.
clear, why = abandoned_box(box(u"按甲做，重跑 ch-6/sec-1", DROP - 10), TICKET, GRACE, DROP + 30)
check("queued message, still inside the grace", False, clear)
check("  ... and counts the grace out loud", True, "30s of the 120s" in why)

# One second short is short.
clear, _why = abandoned_box(box(u"continue", DROP - 10), TICKET, GRACE, DROP + GRACE - 1)
check("one second short of the grace", False, clear)

# No ticket: the polling path infers the drop from pixels and has no drop time,
# so it never gets to make this call at all.
clear, why = abandoned_box(box(u"continue", DROP - 3600), None, GRACE, DROP + 9999)
check("no ticket, however old the text", False, clear)
check("  ... and says why it cannot judge", True, "no drop time" in why)

# A ticket whose timestamp is missing or unreadable is not a drop time either.
for label, rec in (("missing", {"tty": "/dev/ttys004"}),
                   ("zero", {"tty": "/dev/ttys004", "ts": 0}),
                   ("unparseable", {"tty": "/dev/ttys004", "ts": "just now"})):
    clear, _why = abandoned_box(box(u"continue", DROP - 3600), rec, GRACE, DROP + 9999)
    check("ticket with a %s timestamp" % label, False, clear)

# First sighting is this sweep -- the watchdog may have started after the drop,
# and cannot claim the text predates something it never saw.
clear, why = abandoned_box({"box_text": u"continue"}, TICKET, GRACE, DROP + 9999)
check("box never sampled before now", False, clear)
check("  ... and says it never sampled it", True, "never sampled" in why)

# Turned off.
clear, why = abandoned_box(box(u"continue", DROP - 3600), TICKET, 0, DROP + 9999)
check("grace of 0 disables it", False, clear)
check("  ... and says it is disabled", True, "disabled" in why)
clear, _why = abandoned_box(box(u"continue", DROP - 3600), TICKET, -1, DROP + 9999)
check("a negative grace disables it too", False, clear)

# Already cleared this exact text and it is still on screen: Ctrl-U did not take
# it. Stop, rather than keep sending keys at somebody's session until the ticket
# expires.
st = box(u"Press up to edit queued messages", DROP - 30)
st["cleared_box"] = u"Press up to edit queued messages"
clear, why = abandoned_box(st, TICKET, GRACE, DROP + GRACE)
check("a clear that did not take is not retried", False, clear)
check("  ... and says the text stayed", True, "stayed" in why)

# ------------------------------------------------- what track_input_box records

# Unchanged text keeps its clock running. This is the whole mechanism: an edit
# is the only thing that says a human is present.
st = {}
track_input_box(st, u"continue", 100.0)
track_input_box(st, u"continue", 160.0)
check("unchanged text keeps its first sighting", 100.0, st["box_since"])

# Any edit restarts it, however small -- and re-opens the text for clearing,
# since it is no longer the text a clear was already spent on.
st = {"box_text": u"contin", "box_since": 100.0, "cleared_box": u"contin"}
track_input_box(st, u"continue", 160.0)
check("an edit restarts the clock", 160.0, st["box_since"])
check("an edit forgets the spent clear", True, "cleared_box" not in st)

# An empty box is a state worth timing too: it is what a cleared box looks like,
# and the next sweep has to see it as new rather than as the old text still up.
st = {"box_text": u"continue", "box_since": 100.0}
track_input_box(st, None, 160.0)
check("losing the box is a change", 160.0, st["box_since"])
track_input_box(st, None, 200.0)
check("no box, still no box, same clock", 160.0, st["box_since"])

# The tracker runs on every session the sweep evaluates, so it has to survive a
# session it has never seen before without a prepared state dict.
st = {}
track_input_box(st, None, 50.0)
check("a session seen for the first time", 50.0, st["box_since"])


# ------------------------------------------- the branch, wired into a sweep

# The unit cases above decide *whether* to clear. These drive the whole sweep to
# check what the decision is wired to: the dry-run and cooldown gates, the one
# attempt per text, and the verdict wordings other tools read back.

def fake_sweep(box_text, box_since, drop_ts, now_ts, cfg_extra=None, st_extra=None,
               clear_ok=True):
    """Run scan_once over one made-up session holding `box_text`, with a ticket.

    Returns (verdict, keys sent, session state).
    """
    screen = (u"\u25cf API Error: Connection lost mid-response. "
              u"The response above may be incomplete.\n"
              u"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
              u"\u276f %s\n" % box_text)
    sess = {"app": "Terminal", "key": "42:1", "tty": "/dev/ttys004",
            "title": u"MarketingResearch \u2014 Owli D-052 \u2014 Claude", "screen": screen}
    sent = []

    trig_path = os.path.join(watchdog.TRIG_DIR, "t.json")
    os.makedirs(watchdog.TRIG_DIR, exist_ok=True)
    with open(trig_path, "w", encoding="utf-8") as f:
        json.dump({"tty": "/dev/ttys004", "session_id": "s" * 36, "ts": drop_ts}, f)

    cfg = dict(watchdog.DEFAULTS)
    cfg["confirm_polls"] = 1
    cfg.update(cfg_extra or {})

    saved = (watchdog.collect_sessions, watchdog.claude_ttys, watchdog.has_claude,
             watchdog.clear_input_box, watchdog.send_retry, watchdog.reread_screen,
             watchdog.time.time)
    watchdog.collect_sessions = lambda _cfg: [sess]
    watchdog.claude_ttys = lambda: {"/dev/ttys004"}
    watchdog.has_claude = lambda _s, _live: True
    watchdog.clear_input_box = lambda _s: (sent.append("C-u"),
                                           (clear_ok, "OK" if clear_ok else "osascript timed out"))[1]
    watchdog.send_retry = lambda _s, text: (sent.append(text), (True, "OK"))[1]
    # The last-moment re-read is a live AppleScript call. Hand back the same
    # screen: these cases are about the decision, not about the tab moving.
    watchdog.reread_screen = lambda s: s["screen"]
    watchdog.time.time = lambda: now_ts
    state = {"Terminal:42:1": dict({"streak": 0, "consecutive": 0, "last_sent": 0,
                                    "last_fp": ""}, **(st_extra or {}))}
    if box_since is not None:
        state["Terminal:42:1"].update({"box_text": box_text, "box_since": box_since})
    try:
        rows = watchdog.scan_once(cfg, state, act=True)
    finally:
        (watchdog.collect_sessions, watchdog.claude_ttys, watchdog.has_claude,
         watchdog.clear_input_box, watchdog.send_retry, watchdog.reread_screen,
         watchdog.time.time) = saved
        if os.path.exists(trig_path):
            os.remove(trig_path)
    return rows[0][2], sent, state["Terminal:42:1"]


LATE = DROP + GRACE + 1

verdict, sent, st = fake_sweep(u"continue", DROP - 30, DROP, LATE)
check("sweep: an abandoned box is cleared", ["C-u"], sent)
check("sweep: and says so in the verdict", True, "cleared an abandoned message" in verdict)
check("sweep: and nothing is typed in the same pass", True, "please, continue" not in sent)
check("sweep: and the spent clear is remembered", u"continue", st.get("cleared_box"))

# The ticket survives the clear, so the next sweep -- box now empty -- rescues.
verdict, sent, _st = fake_sweep(u"", DROP - 30, DROP, LATE)
check("sweep: the emptied box is then rescued", ["please, continue"], sent)

# Must not fire: every gate below leaves the keyboard alone.
verdict, sent, _st = fake_sweep(u"按甲做，重跑 ch-6/sec-1", DROP - 10, DROP, DROP + 30)
check("sweep: inside the grace, nothing is sent", [], sent)
check("sweep: and the verdict counts it out", True, "of the 120s grace" in verdict)

verdict, sent, _st = fake_sweep(u"什么情况？", DROP + 5, DROP, LATE)
check("sweep: text typed after the drop is left", [], sent)

verdict, sent, _st = fake_sweep(u"continue", DROP - 30, DROP, LATE, {"dry_run": True})
check("sweep: dry_run clears nothing", [], sent)
check("sweep: and says what it would have done", True, "would clear an abandoned message" in verdict)

verdict, sent, _st = fake_sweep(u"continue", DROP - 30, DROP, LATE,
                                st_extra={"last_clear": LATE - 5})
check("sweep: a recent clear cools down", [], sent)
check("sweep: and says the cooldown is why", True, "abandoned message, clear cooling down" in verdict)

verdict, sent, _st = fake_sweep(u"continue", DROP - 30, DROP, LATE,
                                st_extra={"cleared_box": u"continue"})
check("sweep: a clear that did not take is not retried", [], sent)

verdict, sent, _st = fake_sweep(u"continue", DROP - 30, DROP, LATE, {"stale_box_grace_sec": 0})
check("sweep: grace 0 leaves the box alone", [], sent)

# A Ctrl-U that never went out is not an attempt: the one-shot guard must not
# burn on it, or an osascript hiccup costs the whole rescue.
verdict, sent, st = fake_sweep(u"continue", DROP - 30, DROP, LATE, clear_ok=False)
check("sweep: a clear that failed to go out", ["C-u"], sent)
check("sweep: ... says so", True, "clear failed" in verdict)
check("sweep: ... and is not counted as spent", True, "cleared_box" not in st)


def main():
    shutil.rmtree(HOME, ignore_errors=True)
    ok = fail = 0
    for name, expect, got in RESULTS:
        if got == expect:
            ok += 1
            print("  PASS  %-52s -> %s" % (name, got))
        else:
            fail += 1
            print("  FAIL  %-52s expected %r got %r" % (name, expect, got))
    print("\n%d passed / %d failed" % (ok, fail))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
