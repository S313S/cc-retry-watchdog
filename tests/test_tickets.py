#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for claiming a hook ticket that names a daemon pty.

A background job's turn does not run in the terminal tab you watch it through:
the CLI daemon hosts it in a `claude bg-spare` process on a pty of its own, and
that pty is what the StopFailure hook finds when it walks up its parents. So the
ticket arrives naming a tty no window owns, and for two days every one of them
expired unread while the sessions they belonged to sat parked on a drop.

The job's state file is the only bridge between the two ends. These cases are
mostly about refusing to cross it on a guess: a ticket that lands on the wrong
tab types into somebody else's session, and losing one merely costs the few
seconds the polling path takes to see the same screen.

    python3 tests/test_tickets.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import watchdog  # noqa: E402
from watchdog import (job_for_session, read_triggers,  # noqa: E402
                      rehome_daemon_tickets, session_showing_job,
                      unclaimed_note)

SID = "e4d6eb49-bdb1-4412-a679-3db04d9fa147"
JOB = {"sessionId": SID,
       "name": "report formatting review",
       "cwd": "/Users/x/Work/MarketingResearch"}

# What the watchdog's sweep hands these functions: one row per terminal tab.
# Terminal.app builds the title as "<cwd basename> — <glyph> <job name> — ...",
# which is the only place a tab says which job it is showing.
TAB = {"app": "Terminal", "key": "107355:1", "tty": "/dev/ttys014",
       "title": u"MarketingResearch — ◑ report formatting review — Claude — 145×32"}
OTHER = {"app": "Terminal", "key": "105911:1", "tty": "/dev/ttys004",
         "title": u"MarketingResearch — ◐ dispatch session coordinator — claude — 145×32"}
ELSEWHERE = {"app": "Terminal", "key": "108008:1", "tty": "/dev/ttys011",
             "title": u"cc-retry-watchdog — ◐ auto-send please logic — claude — 145×33"}

RESULTS = []


def check(name, expect, got):
    RESULTS.append((name, expect, got))


def ticket(tty="/dev/ttys015", session_id=SID):
    return {"tty": tty, "session_id": session_id, "_path": "/tmp/ticket-%s.json" % session_id[:8]}


def main():
    root = tempfile.mkdtemp(prefix="ccw-jobs-")
    saved, watchdog.JOBS_DIR = watchdog.JOBS_DIR, root
    quiet, watchdog.log = watchdog.log, lambda *_a, **_k: None
    try:
        os.makedirs(os.path.join(root, SID[:8]))
        with open(os.path.join(root, SID[:8], "state.json"), "w") as f:
            json.dump(JOB, f)
        # A job whose name is too short to identify a tab by.
        thin = "abcd1234-0000-0000-0000-000000000000"
        os.makedirs(os.path.join(root, thin[:8]))
        with open(os.path.join(root, thin[:8], "state.json"), "w") as f:
            json.dump({"sessionId": thin, "name": "rc", "cwd": "/Users/x/Work/Owli"}, f)

        # -------------------------------------------------- reading the job
        check("the job behind a session id",
              JOB["name"], (job_for_session(SID) or {}).get("name"))
        check("no job dir for this session", None, job_for_session(""))
        check("no job dir on disk", None,
              job_for_session("ffffffff-0000-0000-0000-000000000000"))
        # The dir name is only the first 8 characters, so it can collide.
        check("the 8-char prefix belongs to a different session", None,
              job_for_session("e4d6eb49-9999-9999-9999-999999999999"))
        check("a name too generic to match a tab by", None, job_for_session(thin))

        # ---------------------------------------------- finding its terminal
        job = job_for_session(SID)
        check("the tab showing the job",
              TAB["tty"], (session_showing_job(job, [OTHER, TAB, ELSEWHERE]) or {}).get("tty"))
        check("no tab is showing it", None,
              session_showing_job(job, [OTHER, ELSEWHERE]))
        # Same task name, different project: the cwd is what tells them apart.
        twin = dict(TAB, tty="/dev/ttys002",
                    title=u"Owli — ◑ report formatting review — claude — 145×32")
        check("same name under another project", None,
              session_showing_job(job, [twin]))
        check("two tabs claim the same job -- decline rather than guess", None,
              session_showing_job(job, [TAB, dict(TAB, key="9:1", tty="/dev/ttys020")]))

        # ------------------------------------------------ moving the ticket
        trig = {"/dev/ttys015": ticket()}
        alias = rehome_daemon_tickets(trig, [OTHER, TAB])
        check("the ticket moves onto the tab", [TAB["tty"]], sorted(trig))
        check("and says where it came from", {"/dev/ttys015": TAB["tty"]}, alias)
        check("the moved ticket records the job",
              True, "report formatting review" in trig[TAB["tty"]].get("_via", ""))

        # A ticket whose tty a window does own is already home.
        own = {TAB["tty"]: ticket(tty=TAB["tty"])}
        rehome_daemon_tickets(own, [TAB])
        check("a ticket on a real tab is left alone", [TAB["tty"]], sorted(own))

        # Never overwrite the tab's own ticket with a job's.
        both = {"/dev/ttys015": ticket(), TAB["tty"]: ticket(tty=TAB["tty"])}
        rehome_daemon_tickets(both, [TAB])
        check("an exact ticket wins over a rehomed one",
              ["/dev/ttys014", "/dev/ttys015"], sorted(both))

        # Nothing to move it onto: it expires, but says what it was.
        orphan = {"/dev/ttys015": ticket()}
        rehome_daemon_tickets(orphan, [OTHER])
        check("no tab, so the ticket stays put", ["/dev/ttys015"], sorted(orphan))
        check("and the log line names the job",
              True, "report formatting review" in unclaimed_note(orphan["/dev/ttys015"]))
        check("a ticket with no job behind it still reads plainly",
              "no terminal session reports this tty",
              unclaimed_note({"tty": "/dev/ttys009", "session_id": None}))

        # ------------------------------------------------ the lease on a held
        # ticket. A drop arrived with a human's draft half-typed in the input
        # box. The watchdog held -- correctly, it will not clobber a draft --
        # re-checked every four seconds, and then discarded the ticket because
        # three minutes had passed. The draft was the only thing in the way and
        # it would have cleared itself the moment the human sent or erased it;
        # the drop was never rescued and the retry was typed in by hand.
        TTL = 180
        HELD = u"ok: input box not empty (\u5207\u597d\u4e86\uff0c\u8dd1 B \u7ec4), skipping"
        MOVED = "ok: no such error this turn"

        for name, age, verdict, alive, extended in [
            ("a fresh ticket is kept, and taken on faith", 10, "", True, False),
            ("a ticket whose session moved on expires on time",
             TTL + 20, MOVED, False, False),
            ("a ticket never evaluated expires on time", TTL + 20, "", False, False),
            ("a draft in the box keeps the ticket alive", TTL + 20, HELD, True, True),
            ("the kept ticket is no longer taken on faith", TTL + 20, HELD, True, True),
            ("a held ticket is still let go eventually",
             TTL * 20 + 60, HELD, False, False),
        ]:
            trig_root = tempfile.mkdtemp(prefix="ccw-trig-")
            saved_trig, watchdog.TRIG_DIR = watchdog.TRIG_DIR, trig_root
            watchdog._LAST_VERDICT.clear()
            watchdog._LEASE_NOTES.clear()
            try:
                tty = "/dev/ttys015"
                path = os.path.join(trig_root, "t.json")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(json.dumps({"tty": tty, "session_id": SID,
                                        "ts": time.time() - age}))
                if verdict:
                    watchdog._LAST_VERDICT[tty] = verdict
                got = read_triggers(TTL)
                if "taken on faith" in name:
                    check(name, extended, bool(got.get(tty, {}).get("_extended")))
                else:
                    check(name, alive, tty in got)
                    check(name + " (file)", alive, os.path.exists(path))
            finally:
                watchdog.TRIG_DIR = saved_trig
                shutil.rmtree(trig_root, ignore_errors=True)

        # A held ticket is re-read every second. Rehoming it logged a line every
        # time -- 46 identical lines in the three minutes of the incident, and
        # with the longer lease it would have been nine hundred.
        watchdog._REHOME_NOTES.clear()
        lines = []
        watchdog.log = lambda m: lines.append(m)
        for _ in range(5):
            trigs = {"/dev/ttys015": ticket()}
            rehome_daemon_tickets(trigs, [TAB])
        watchdog.log = lambda *_a, **_k: None
        check("rehoming a held ticket logs once, not once per sweep", 1, len(lines))
    finally:
        watchdog.JOBS_DIR, watchdog.log = saved, quiet
        shutil.rmtree(root, ignore_errors=True)

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
