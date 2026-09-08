#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cc-retry-watchdog -- watch running Claude Code sessions and, when one is
killed by a mid-stream connection drop, type the retry prompt into it for you.

Claude Code retries failures that happen *before* a response starts streaming.
It does not retry a stream that dies halfway: the partial reply is finalized,
`API Error: Connection lost mid-response.` is printed (one of several wordings
-- see DROP_CAUSES below), and the session just sits there until a human types
something. This watchdog is that human.

Two independent ways of noticing a dead turn:

  1. The StopFailure hook (accurate, ~1s). Claude Code fires StopFailure when an
     API error ends a turn. The hook cannot resume the turn itself -- it is
     fire-and-forget, its stdout and exit code are ignored -- but it can leave a
     ticket naming the tty that just died. See hook_stopfailure.py. For a
     background job that tty is the pty the CLI daemon hosts the job on, which
     no window owns, so the ticket is moved onto the tab showing that job --
     see job_for_session below.
  2. Screen polling (fallback, ~5s). Read what each terminal is showing and
     recognize the layout of a session parked on that error.

Backends: macOS Terminal.app and iTerm2 via AppleScript, plus tmux anywhere.

Refuses to act unless ALL of these hold:
  * the error is the last thing that happened this turn (polling path), or a
    hook ticket says so (hook path);
  * the session is idle -- no spinner, no `esc to interrupt`, and crucially no
    built-in `Retrying in Ns - attempt n/m` (never interrupt its own recovery);
  * the input box is empty, so half-typed text is never clobbered;
  * a claude process is actually running there;
  * cooldown has elapsed and the per-session retry cap is not exhausted.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

IS_MAC = sys.platform == "darwin"

# Code lives in the repo; runtime state lives elsewhere so `git pull` never
# collides with logs, and so a checkout stays clean.
CODE_DIR = os.path.dirname(os.path.abspath(os.path.realpath(__file__)))
BASE = os.environ.get("CC_AUTORESUME_HOME") or os.path.expanduser("~/.claude/cc-autoresume")
try:
    os.makedirs(BASE, exist_ok=True)
except Exception:
    BASE = CODE_DIR

CONFIG_PATH = os.path.join(BASE, "config.json")
STATE_PATH = os.path.join(BASE, "state.json")
LOG_PATH = os.path.join(BASE, "watchdog.log")
PAUSE_FLAG = os.path.join(BASE, "PAUSED")       # this file exists => never inject
TRIG_DIR = os.path.join(BASE, "triggers")       # tickets dropped by the hook
SNAPSHOT_PATH = os.path.join(BASE, "sessions.json")  # what the last sweep saw, for other tools
LOG_MAX_BYTES = 2 * 1024 * 1024

DEFAULTS = {
    "retry_text": "please, continue",
    "poll_interval_sec": 5,
    "confirm_polls": 2,          # consecutive stuck observations before acting
    "cooldown_sec": 30,          # min seconds between two injections per session
    "max_consecutive": 6,        # per-session auto-retry cap (resets on recovery)
    "dry_run": False,            # log what would happen, inject nothing
    "notify": True,              # desktop notification on injection (macOS)
    "watch_terminal_app": True,
    "watch_iterm": True,
    "watch_tmux": True,
    "exclude_title_regex": "",   # skip sessions whose title matches
    "exclude_tty": [],           # e.g. ["/dev/ttys003"]
    "tail_lines": 80,            # only inspect this many lines from the bottom
    "use_hook_triggers": True,   # trust tickets left by the StopFailure hook
    "trigger_ttl_sec": 180,      # tickets older than this are discarded
    "fast_poll_sec": 1,          # poll interval while a ticket is pending
    "snapshot": True,            # write sessions.json after every sweep (read by cc-needs-you)
}

# ---------------------------------------------------------------- patterns

# Every wording below is a literal lifted from the Claude Code bundle, not a
# guess. The message set was rewritten in 2.1.226: "closed" became "lost", and
# the two generic messages split into one per cause. Both generations are
# matched so the watchdog survives an upgrade -- or a rollback.
#
#   <= 2.1.225                       >= 2.1.227
#   Response stalled mid-stream      The response stopped arriving
#   Connection closed mid-response   Connection lost mid-response
#   Server error mid-response        Server error mid-response
#   (none)                           Your computer went to sleep mid-response
#
# They all mean one thing: a transport failure finalized the turn and no retry
# is coming. Every *other* "API Error:" -- 401, the 400s, aborted, context
# window, the bare "Please wait a moment" -- is a real failure that a retry
# would only burn a turn on, so this is an explicit allowlist of causes and
# never a match on the "API Error:" prefix alone.
DROP_CAUSES = (
    # Content had already streamed: "... The response above may be incomplete."
    r"Connection\s+(?:lost|closed)\s+mid-?response",
    r"Server\s+error\s+mid-?response",
    r"Your\s+computer\s+went\s+to\s+sleep\s+mid-?response",
    r"(?:The\s+)?Response\s+stalled\s+mid-?stream",
    r"The\s+response\s+stopped\s+arriving",
    # Nothing had been produced yet: "... Try again."
    r"(?:The\s+)?Response\s+stalled\s+(?:while\s+thinking|before\s+a\s+response)",
    r"Connection\s+(?:lost|closed)\s+(?:while\s+thinking|before\s+a\s+response)",
    r"Your\s+computer\s+went\s+to\s+sleep\s+before\s+a\s+response",
    # Transport error raised around the stream rather than inside it
    # ("Connection to the API was lost (ECONNRESET). ... try again.")
    r"Connection\s+to\s+the\s+API\s+was\s+lost\s*\(",
)
# Tolerant of the line wrap that splits the message on narrow terminals.
ERR_TEXT = re.compile(r"API\s+Error:\s*(?:%s)" % "|".join(DROP_CAUSES), re.I)
# Start of the error line: an optional bullet glyph (do not hardcode which) + text.
ERR_HEAD = re.compile(r"^\s*(?:[^\w\s]{1,2}\s+)?API\s+Error:", re.U)

# Any of these on screen means the session is working. Never touch it.
BUSY_PATTERNS = [
    re.compile(r"esc to interrupt", re.I),
    re.compile(r"Retrying in\s+\d+s", re.I),        # built-in retry in progress
    re.compile(r"attempt\s+\d+\s*/\s*\d+", re.I),   # built-in retry counter
    re.compile(r"…\s*\(\s*\d+[smh]"),               # "Sprouting… (15s ·"
    re.compile(r"\(\s*\d+[smh]\s*·"),               # "(15s ·"
    re.compile(r"ctrl\+b to run in background", re.I),
]

PROMPT_LINE = re.compile(r"^\s*[❯>]\s?(.*)$")
# Placeholder hint inside the input box counts as empty.
PLACEHOLDER = re.compile(r'^\s*(?:Try\s+"|/\s*$|$)')
RULE_LINE = re.compile(r"^\s*[─━═┄╌—_\-╭╮╰╯│\s]{6,}$")
# The same rule, with the session's name hung off it:
#     ─────────────────────────────── report formatting review ─
# Claude Code started drawing this above the input box, and RULE_LINE only
# matches a bare line, so the label made it read as content -- which put every
# session parked on a drop into "output after the error, the turn moved on"
# and took the polling path out of service completely. Anchored on a long run
# of box-drawing dashes: prose cannot open with ten of them, so a real output
# line still disqualifies the turn the way it should.
TITLED_RULE = re.compile(r"^\s*[─━═┄╌—]{10,}[^─━═┄╌—]{0,60}[─━═┄╌—]*\s*$")

# Decoration allowed to appear after the error. Anything else means the turn
# moved on and must not be retried.
CHROME_AFTER_ERR = [
    RULE_LINE,
    TITLED_RULE,
    # The turn-duration line. Claude Code renders it as
    #     `${verb} for ${duration}${doneAt ? ` · done ${doneAt}` : ""}`
    # and hangs further " · ..." segments off the same line: the wall-clock
    # finish time, a token budget, "N messages hidden (/focus to show)",
    # "<task> still running". `doneAt` is empty only for an invalid timestamp,
    # so in practice the suffix is always there -- anchoring at the duration
    # is what silently killed the whole polling path (SENT[poll] stayed 0
    # while the hook did 160 rescues) and left one real session parked for 71
    # minutes on "Cogitated for 1h 5m 49s · done 19:14".
    re.compile(r"^\s*\S{0,2}\s*[A-Za-z]+ for(?:\s+\d+[hms])+(?:\s*·.*)?$"),  # "* Brewed for 2m 45s"
    re.compile(r"^\s*Jump to bottom", re.I),
    re.compile(r"(?:ctrl\+v to paste|/clear to save|to edit in Vim|Auto-update failed|Run claude doctor)\s*$", re.I),
    re.compile(r"^\s*❯\s*$"),
    # A background agent outlives the turn the drop killed and keeps drawing
    # below the error. The main loop is parked -- that is decoration, not a
    # turn that moved on. Anything the main loop itself emits still
    # disqualifies it, so this stays safe.
    re.compile(r"^\s*\S{0,2}\s*Waiting for \d+ background agents? to finish\s*$", re.I),
    re.compile(r"Backgrounded agent \(", re.I),
    re.compile(r'^\s*\S{0,2}\s*Agent\s+".*"\s+finished\b', re.I),
]

# If any of these follow the error, the turn actually finished normally.
DISQUALIFY_AFTER_ERR = [
    re.compile(r"^\s*[※*·]?\s*recap:", re.I),
    re.compile(r"disable recaps in /config", re.I),
]


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-2000:]
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (now(), msg))
    except Exception:
        pass
    print("[%s] %s" % (now(), msg), flush=True)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return dict(default) if isinstance(default, dict) else default


def save_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        log("WARN could not write %s: %s" % (path, e))


def run(argv, timeout=20):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            return None, (p.stderr or "").strip() or "exit %d" % p.returncode
        return p.stdout, None
    except subprocess.TimeoutExpired:
        return None, "timed out"
    except FileNotFoundError:
        return None, "not installed"
    except Exception as e:
        return None, str(e)


def osa(script, timeout=20):
    if not IS_MAC:
        return None, "not macOS"
    return run(["osascript", "-e", script], timeout=timeout)


def app_running(name):
    out, _ = osa('tell application "System Events" to return (exists process "%s")' % name)
    return bool(out) and out.strip() == "true"


# ---------------------------------------------------------------- reading screens

MARK = "<<<CCA-SESSION>>>"

# Index-based iteration with a try at every level: windows and tabs opening or
# closing mid-scan must not abort the whole read.
ITERM_READ = '''
tell application "iTerm2"
  set out to ""
  repeat with wi from 1 to (count of windows)
    try
      repeat with ti from 1 to (count of tabs of window wi)
        try
          repeat with si from 1 to (count of sessions of tab ti of window wi)
            try
              set s to session si of tab ti of window wi
              set out to out & "%s" & (id of s as string) & "|" & (tty of s) & "|" & "" & "|" & (name of s) & linefeed & (text of s) & linefeed
            end try
          end repeat
        end try
      end repeat
    end try
  end repeat
  return out
end tell
''' % MARK

# Two Terminal.app scripting traps, both of which fail *silently*:
#   1. `repeat with w in windows` yields nothing -- must index as `window wi`.
#   2. `set tb to tab ti of window wi` then `contents of tb` fails -- the full
#      specifier has to be repeated every time.
# Also: tty is not a unique key. A window whose process exited still reports its
# old tty, and that number gets recycled by new windows, so two entries collide.
# Key on "window id : tab index" instead, and use the tab's own process list.
TERMINAL_READ = '''
tell application "Terminal"
  set out to ""
  repeat with wi from 1 to (count of windows)
    try
      set wname to name of window wi
      set widd to (id of window wi) as string
      repeat with ti from 1 to (count of tabs of window wi)
        try
          set out to out & "%s" & widd & ":" & ti & "|" & (tty of tab ti of window wi) & "|" & ((processes of tab ti of window wi) as string) & "|" & wname & linefeed & (contents of tab ti of window wi) & linefeed
        end try
      end repeat
    end try
  end repeat
  return out
end tell
''' % MARK


def parse_sessions(raw, app):
    """Split one batched AppleScript read into [{app,key,tty,procs,title,screen}]."""
    sessions = []
    if not raw:
        return sessions
    for chunk in raw.split(MARK)[1:]:
        nl = chunk.find("\n")
        if nl < 0:
            continue
        header, body = chunk[:nl], chunk[nl + 1:]
        parts = header.split("|", 3)
        if len(parts) < 4:
            continue
        sessions.append({
            "app": app,
            "key": parts[0].strip(),
            "tty": parts[1].strip(),
            "procs": parts[2].strip(),
            "title": parts[3].strip(),
            "screen": body,
        })
    return sessions


_empty_warned = set()


def window_count(app):
    out, err = osa('tell application "%s" to return (count of windows) as string' % app,
                   timeout=15)
    if err or not out:
        return -1
    try:
        return int(out.strip())
    except ValueError:
        return -1


def _read_app(app, script, out):
    raw, err = osa(script, timeout=25)
    if err:
        log("WARN could not read %s: %s" % (app, err))
        return
    got = parse_sessions(raw, app)
    # AppleScript `try` swallows errors, so "the app has windows but we parsed no
    # sessions out of them" is a bug signal worth surfacing. An app sitting there
    # with no windows open is not -- checking the count first keeps this from
    # crying wolf.
    if not got:
        if window_count(app) > 0 and app not in _empty_warned:
            _empty_warned.add(app)
            log("WARN %s has open windows but 0 sessions were read -- the script "
                "may be failing silently (raw output %d bytes)" % (app, len(raw or "")))
    else:
        _empty_warned.discard(app)
    out += got


def tmux_sessions():
    """Enumerate tmux panes. Works on Linux/WSL/macOS alike."""
    fmt = "#{pane_id}\t#{pane_tty}\t#{pane_current_command}\t#{session_name}:#{window_index}.#{pane_index}"
    out, err = run(["tmux", "list-panes", "-a", "-F", fmt], timeout=10)
    if err or not out:
        return []
    sessions = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        pane_id, tty, cmd, title = parts[0], parts[1], parts[2], parts[3]
        screen, cerr = run(["tmux", "capture-pane", "-p", "-t", pane_id], timeout=10)
        if cerr:
            continue
        sessions.append({
            "app": "tmux",
            "key": pane_id,
            "tty": tty,
            "procs": cmd,
            "title": title,
            "screen": screen or "",
        })
    return sessions


def collect_sessions(cfg):
    out = []
    if IS_MAC and cfg["watch_iterm"] and app_running("iTerm2"):
        _read_app("iTerm2", ITERM_READ, out)
    if IS_MAC and cfg["watch_terminal_app"] and app_running("Terminal"):
        _read_app("Terminal", TERMINAL_READ, out)
    if cfg.get("watch_tmux", True):
        out += tmux_sessions()
    return out


def claude_ttys():
    """ttys that currently have a claude process on them."""
    ttys = set()
    try:
        p = subprocess.run(["ps", "-Ao", "tty=,command="], capture_output=True,
                           text=True, timeout=10)
        for line in p.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) < 2:
                continue
            tty, cmd = parts
            # Claude Code rewrites its own process title; it shows up as either
            # "claude" or "Claude" depending on state.
            head = cmd.split()[0] if cmd.split() else ""
            if os.path.basename(head).lower() == "claude":
                ttys.add(tty if tty.startswith("/dev/") else "/dev/" + tty)
    except Exception as e:
        log("WARN ps failed: %s" % e)
    return ttys


def has_claude(sess, live_ttys):
    """Is a claude process really running in this terminal session?

    Terminal.app reports every process on a tab, which is exact -- and it has to
    be used, because a Terminal window whose process exited still reports its old
    tty and that number gets recycled, so a tty match there can be a false
    positive.

    tmux reports only `pane_current_command`, which is the executable name: a
    Claude Code started through a node wrapper reads as "node". So accept either
    that name or a claude process on the pane's tty. Pane ids are unique, so the
    tty-recycling problem does not apply here.

    iTerm2 exposes no per-session process list at all, so the tty lookup is all
    there is; its session ids are unique and closed sessions disappear.
    """
    if sess["app"] == "Terminal":
        return "claude" in (sess.get("procs") or "").lower()
    if sess["app"] == "tmux":
        return "claude" in (sess.get("procs") or "").lower() or sess["tty"] in live_ttys
    return sess["tty"] in live_ttys


# ---------------------------------------------------------------- hook tickets

# Why the last sweep declined to act, keyed by tty, as (verdict, tab title), so
# an expiring ticket can say what it was waiting on and which session it was.
# A ticket that dies without a word is undiagnosable afterwards -- which is how
# one drop sat unrecovered for eleven hours with nothing in the log but
# "expired".
_LAST_VERDICT = {}
_TICKET_NOTES = {}

# The verdict analyze() files when the box already holds typed text, read back
# so an expiring ticket can quote what blocked it. Matching on the phrase is
# how the sweep and the snapshot already recognise this state; this only pulls
# the draft itself back out of it.
DRAFT_VERDICT = re.compile(r"input box not empty \((.*)\), skipping")


def ticket_note(path, tty, note):
    """Log why a pending ticket is being held. Once per distinct reason."""
    if _TICKET_NOTES.get(path) == note:
        return
    _TICKET_NOTES[path] = note
    log("ticket held | %s | %s" % (tty, note))
    for stale in [k for k in _TICKET_NOTES if not os.path.exists(k)]:
        del _TICKET_NOTES[stale]


def announce_expiry(rec, verdict, title):
    """Tell the user about a rescue that was abandoned and is theirs to finish.

    Most expiries need no banner: the session recovered on its own, or the turn
    moved on, or the tab is gone and there is nothing to type into anyway. One
    does. A ticket held because the input box already had typed text in it was
    a drop the watchdog could have cleared and deliberately did not, because
    injecting would have submitted `retry_text` glued onto the user's own
    half-written line. Until now that decision was correct and completely
    silent: the ticket held for its whole TTL and vanished, and the only trace
    was a log line nobody reads. The session then sits parked forever, because
    the draft that blocked this rescue blocks every later one too -- and the
    fix is two seconds of the user's time, if they are told.

    Deliberately not gated on act/dry_run/PAUSE: this reports a fact rather
    than taking an action, and a paused watchdog is exactly when knowing that
    a drop went unrescued matters most.
    """
    draft = DRAFT_VERDICT.search(verdict or "")
    if not draft:
        return False
    where = (title or "").strip() or rec.get("tty") or "a Claude Code session"
    notify("Claude Code drop left unrescued",
           "%s\nyour draft is blocking the retry: %s" % (where[:60], draft.group(1)))
    log("NOTIFIED drop left unrescued | %s | draft in the box: %s"
        % (where, draft.group(1)))
    return True


def read_triggers(ttl_sec, warn=True):
    """Collect tickets left by the StopFailure hook, keyed by tty.

    A ticket means the drop is a known fact rather than something inferred from
    pixels, so the screen does not have to show the error at all.
    """
    out = {}
    if not os.path.isdir(TRIG_DIR):
        return out
    stamp = time.time()
    for name in sorted(os.listdir(TRIG_DIR)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(TRIG_DIR, name)
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:
            _drop(path)
            continue
        if stamp - float(rec.get("ts") or 0) > ttl_sec:
            verdict, title = _LAST_VERDICT.get(rec.get("tty"), ("never evaluated", ""))
            log("ticket expired, discarded | %s | %s | last verdict: %s"
                % (rec.get("tty"), name, verdict))
            if warn:
                announce_expiry(rec, verdict, title)
            _drop(path)
            continue
        rec["_path"] = path
        out[rec.get("tty")] = rec        # keep only the newest per tty
    return out


def _drop(path):
    try:
        os.remove(path)
    except Exception:
        pass


def tickets_pending():
    try:
        return any(n.endswith(".json") for n in os.listdir(TRIG_DIR))
    except Exception:
        return False


# ------------------------------------------------------------- daemon jobs

# A background job does not run in the terminal tab you watch it through. The
# CLI daemon hosts its turn in a `claude bg-spare` process on a pty it made
# itself, and that pty is what the hook sees when it walks up its parents --
# so the ticket names a tty no window owns, and the rescue used to die there
# with "no terminal session reports this tty". The job's own state file is the
# one place that links the two ends: it carries the session id the ticket has,
# and the task name the terminal builds its tab title from.
JOBS_DIR = os.path.expanduser("~/.claude/jobs")


def job_for_session(session_id):
    """The daemon-hosted job this session id belongs to, or None."""
    if not session_id:
        return None
    short = str(session_id)[:8]
    try:
        with open(os.path.join(JOBS_DIR, short, "state.json"), encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        return None
    if st.get("sessionId") and st.get("sessionId") != session_id:
        return None                    # two jobs sharing an 8-char prefix
    name = (st.get("name") or "").strip()
    if len(name) < 4:
        return None                    # too generic to name a tab by
    return {"short": short, "name": name, "cwd": st.get("cwd") or ""}


def session_showing_job(job, sessions):
    """The one terminal session whose title says it is showing this job.

    Ambiguity is declined rather than guessed. Losing the ticket costs a few
    seconds -- the polling path sees the same drop on the same screen -- while
    a wrong match types into somebody else's session.
    """
    base = os.path.basename(job["cwd"].rstrip("/")) if job["cwd"] else ""
    hits = [s for s in sessions
            if job["name"] in (s.get("title") or "")
            and (not base or base in (s.get("title") or ""))]
    return hits[0] if len(hits) == 1 else None


def rehome_daemon_tickets(triggers, sessions):
    """Move tickets off daemon ptys and onto the tab showing that job.

    Returns {pty: tab tty} so the sweep's verdict can be filed under both --
    an expiring ticket reports itself by the tty it was written with.
    """
    alias = {}
    owned = set(s["tty"] for s in sessions)
    for pty in [t for t in triggers if t not in owned]:
        rec = triggers[pty]
        job = job_for_session(rec.get("session_id"))
        if not job:
            continue
        target = session_showing_job(job, sessions)
        if target is None or target["tty"] in triggers:
            continue                   # no tab, or that tab has its own ticket
        rec["_via"] = "%s %r" % (job["short"], job["name"])
        triggers.pop(pty)
        triggers[target["tty"]] = rec
        alias[pty] = target["tty"]
        log("ticket rehomed | %s -> %s | job %s | %s"
            % (pty, target["tty"], rec["_via"], target["title"]))
    return alias


def unclaimed_note(rec):
    """Why a ticket found no session, in terms the log can be read back from."""
    job = job_for_session(rec.get("session_id"))
    if job:
        return ("no terminal session reports this tty -- the daemon hosts job "
                "%s %r on it and no tab is showing that job"
                % (job["short"], job["name"]))
    return "no terminal session reports this tty"


# ---------------------------------------------------------------- the decision

def _norm(s):
    return re.sub(r"\s+", " ", s).strip()


def analyze(screen, tail_lines, require_error=True):
    """Return (stuck, reason, fingerprint).

    stuck=True means: this turn ended on the mid-response drop, the session is
    idle, and the input box is empty.

    require_error=False is the hook path -- the drop is already confirmed, so
    the error does not need to be visible, but every other guard still applies.
    """
    lines = [l.rstrip() for l in screen.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return False, "blank screen", ""
    lines = lines[-tail_lines:]
    joined = "\n".join(lines)

    # 1) must be idle
    for pat in BUSY_PATTERNS:
        if pat.search(joined):
            return False, "session busy (%s)" % pat.pattern[:28], ""

    # 2) locate the input box: the last prompt line
    prompt_idx = None
    prompt_body = ""
    for i in range(len(lines) - 1, -1, -1):
        m = PROMPT_LINE.match(lines[i])
        if m:
            prompt_idx, prompt_body = i, m.group(1)
            break
    if prompt_idx is None:
        return False, "no input box found -- probably not a Claude Code UI", ""

    # 3) user has typed something -- hands off
    if prompt_body.strip() and not PLACEHOLDER.match(prompt_body):
        # Worth naming: an injection whose text stalls in the box shields the
        # session from every later rescue, and "not empty" alone hides that.
        return False, "input box not empty (%s), skipping" % _norm(prompt_body)[:32], ""

    # content area = everything above the input box (minus its rule line)
    end = prompt_idx
    while end - 1 >= 0 and (RULE_LINE.match(lines[end - 1])
                            or TITLED_RULE.match(lines[end - 1])):
        end -= 1
    content = lines[:end]
    while content and not content[-1].strip():
        content.pop()
    if not content:
        return False, "empty content area", ""

    if not require_error:
        fp = hashlib.md5("\n".join(content[-6:]).encode("utf-8")).hexdigest()[:12]
        return True, "hook ticket: session idle, input box empty", fp

    # 4) find the last error line
    err_i = None
    for i in range(len(content) - 1, -1, -1):
        if ERR_HEAD.match(content[i]) and ERR_TEXT.search(_norm(" ".join(content[i:i + 3]))):
            err_i = i
            break
    if err_i is None:
        return False, "no such error this turn", ""

    # the error block is the error line plus its wrapped continuations
    err_end = err_i
    while err_end + 1 < len(content):
        nxt = content[err_end + 1]
        if not nxt.strip() or nxt[:1] not in (" ", "\t"):
            break
        if any(p.search(nxt) for p in CHROME_AFTER_ERR + DISQUALIFY_AFTER_ERR):
            break
        err_end += 1

    # 5) only decoration may follow; real output or a recap means it moved on
    for j in range(err_end + 1, len(content)):
        line = content[j]
        if not line.strip():
            continue
        if any(p.search(line) for p in DISQUALIFY_AFTER_ERR):
            return False, "recap after the error -- turn ended normally", ""
        if any(p.search(line) for p in CHROME_AFTER_ERR):
            continue
        return False, "output after the error (%s) -- turn moved on" % _norm(line)[:40], ""

    fp = hashlib.md5("\n".join(content[max(0, err_i - 2):]).encode("utf-8")).hexdigest()[:12]
    hit = ERR_TEXT.search(_norm(" ".join(content[err_i:err_i + 3])))
    return True, "parked on %s" % hit.group(0), fp


# ---------------------------------------------------------------- injecting

# Type the text, pause, then send Return separately: a TUI that sees text and
# newline in one read may treat it as a paste and insert a line break instead of
# submitting.
ITERM_SEND = '''
tell application "iTerm2"
  repeat with wi from 1 to (count of windows)
    try
      repeat with ti from 1 to (count of tabs of window wi)
        try
          repeat with si from 1 to (count of sessions of tab ti of window wi)
            try
              if ((id of session si of tab ti of window wi) as string) is "%(key)s" then
                tell session si of tab ti of window wi to write text "%(text)s" newline NO
                delay 0.4
                tell session si of tab ti of window wi to write text "" newline YES
                return "OK"
              end if
            end try
          end repeat
        end try
      end repeat
    end try
  end repeat
  return "NOTFOUND"
end tell
'''

TERMINAL_SEND = '''
tell application "Terminal"
  repeat with wi from 1 to (count of windows)
    try
      if ((id of window wi) as string) is "%(wid)s" then
        do script "%(text)s" in tab %(ti)s of window wi
        return "OK"
      end if
    end try
  end repeat
  return "NOTFOUND"
end tell
'''


# One tab, read on its own. Used for the last-moment re-check below, never to
# enumerate -- collect_sessions reads every tab in a single round-trip.
TERMINAL_TAB_READ = '''
tell application "Terminal"
  repeat with wi from 1 to (count of windows)
    try
      if ((id of window wi) as string) is "%(wid)s" then
        return (contents of tab %(ti)s of window wi)
      end if
    end try
  end repeat
  return ""
end tell
'''


# Ctrl-U clears the line in Claude Code's input box. Verified against a live
# session rather than assumed -- the obvious alternative, pressing return again,
# does not submit a box in this state.
TERMINAL_CLEAR = '''
tell application "Terminal"
  repeat with wi from 1 to (count of windows)
    try
      if ((id of window wi) as string) is "%(wid)s" then
        do script (character id 21) in tab %(ti)s of window wi
        return "OK"
      end if
    end try
  end repeat
  return "NOTFOUND"
end tell
'''

ITERM_CLEAR = '''
tell application "iTerm2"
  repeat with wi from 1 to (count of windows)
    try
      repeat with ti from 1 to (count of tabs of window wi)
        try
          repeat with si from 1 to (count of sessions of tab ti of window wi)
            try
              if ((id of session si of tab ti of window wi) as string) is "%(key)s" then
                tell session si of tab ti of window wi to write text (character id 21) newline NO
                return "OK"
              end if
            end try
          end repeat
        end try
      end repeat
    end try
  end repeat
  return "NOTFOUND"
end tell
'''


def input_box(screen, tail_lines):
    """What is sitting in the input box right now, or None if there is no box."""
    lines = [l.rstrip() for l in screen.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    for line in reversed(lines[-tail_lines:]):
        m = PROMPT_LINE.match(line)
        if m:
            return m.group(1).replace(u"\xa0", u" ").strip()
    return None


def is_stalled_injection(screen, tail_lines, retry_text):
    """Is our own retry text sitting unsubmitted in the input box?

    `do script` hands Terminal.app the text and the return in a single write and
    a TUI can take that for a paste: the text lands in the box and nothing is
    submitted. The box then stays non-empty forever -- which is exactly what
    this watchdog refuses to type into, so one mistimed injection shields the
    session from every later rescue. That is not hypothetical: it cost a real
    session nine and a half hours parked on a drop, with the log correctly
    reporting "input box not empty" every second of it.

    Equality, not containment: text the *user* typed that merely starts with the
    retry prompt is theirs, and must not be cleared.
    """
    body = input_box(screen, tail_lines)
    return body is not None and body == (retry_text or "").strip()


def clear_input_box(sess):
    """Send Ctrl-U to this session. Returns (ok, detail)."""
    app = sess["app"]
    if app == "tmux":
        _, err = run(["tmux", "send-keys", "-t", sess["key"], "C-u"], timeout=15)
        return (err is None), (err or "OK")
    if app == "iTerm2":
        script = ITERM_CLEAR % {"key": esc(sess["key"])}
    else:
        wid, _, ti = sess["key"].partition(":")
        script = TERMINAL_CLEAR % {"wid": esc(wid), "ti": esc(ti or "1")}
    out, err = osa(script, timeout=25)
    if err:
        return False, err
    return (out or "").strip() == "OK", (out or "").strip()


def reread_screen(sess):
    """This one session's screen, fresh. None when there is no cheap way to get it."""
    if sess["app"] == "Terminal":
        wid, _, ti = sess["key"].partition(":")
        out, err = osa(TERMINAL_TAB_READ % {"wid": esc(wid), "ti": esc(ti or "1")}, timeout=20)
        return None if err else (out or "")
    if sess["app"] == "tmux":
        out, err = run(["tmux", "capture-pane", "-p", "-t", sess["key"]], timeout=15)
        return None if err else (out or "")
    return None                    # iTerm2 exposes no cheap single-session read


def esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def send_retry(sess, text):
    app = sess["app"]
    if app == "tmux":
        _, err = run(["tmux", "send-keys", "-t", sess["key"], "-l", text], timeout=15)
        if err:
            return False, err
        time.sleep(0.4)
        _, err = run(["tmux", "send-keys", "-t", sess["key"], "Enter"], timeout=15)
        return (err is None), (err or "OK")

    if app == "iTerm2":
        script = ITERM_SEND % {"key": esc(sess["key"]), "text": esc(text)}
    else:
        wid, _, ti = sess["key"].partition(":")
        script = TERMINAL_SEND % {"wid": esc(wid), "ti": esc(ti or "1"), "text": esc(text)}
    out, err = osa(script, timeout=25)
    if err:
        return False, err
    return (out or "").strip() == "OK", (out or "").strip()


# One banner per rescue, rather than one that keeps being overwritten in place.
#
# AppleScript's `display notification` takes no identifier, so every notification
# it posts carries the same one -- the sha1 of an empty string. macOS reads a
# repeat of a known identifier as an update, and silently refreshes the record
# already on screen instead of announcing a new one. Through a run of rescues
# that looks like the notification firing only sometimes.
#
# terminal-notifier files each one separately. It is optional and not a
# dependency: without it the AppleScript path still works, it just coalesces.
_NOTIFIER = shutil.which("terminal-notifier") if IS_MAC else None


def notify(title, msg):
    if not IS_MAC:
        return
    if _NOTIFIER:
        run([_NOTIFIER, "-title", title, "-message", msg], timeout=10)
        return
    osa('display notification "%s" with title "%s"' % (esc(msg), esc(title)), timeout=10)


# ---------------------------------------------------------------- main loop

def scan_once(cfg, state, act=True):
    sessions = collect_sessions(cfg)
    live = claude_ttys()
    excl_re = re.compile(cfg["exclude_title_regex"]) if cfg.get("exclude_title_regex") else None
    triggers = (read_triggers(cfg["trigger_ttl_sec"], cfg.get("notify", True))
                if cfg.get("use_hook_triggers", True) else {})
    ticket_alias = rehome_daemon_tickets(triggers, sessions) if triggers else {}
    report = []
    seen_keys = set()
    tty_by_sid = {}

    for s in sessions:
        sid = "%s:%s" % (s["app"], s["key"])
        if sid in seen_keys:       # duplicate key in one sweep: trust the first
            continue
        seen_keys.add(sid)
        tty_by_sid[sid] = s["tty"]
        st = state.setdefault(sid, {"streak": 0, "consecutive": 0, "last_sent": 0, "last_fp": ""})

        if s["tty"] in (cfg.get("exclude_tty") or []):
            report.append((sid, s["title"], "skipped: tty excluded"))
            st["streak"] = 0
            continue
        if excl_re and excl_re.search(s["title"]):
            report.append((sid, s["title"], "skipped: title excluded"))
            st["streak"] = 0
            continue
        if not has_claude(s, live):
            report.append((sid, s["title"], "skipped: no claude process here"))
            st["streak"] = 0
            continue

        trig = triggers.get(s["tty"])
        # A ticket means StopFailure already confirmed this turn died, so the
        # error need not be on screen and the debounce is unnecessary. Idle and
        # empty-input-box guards still apply.
        stuck, reason, fp = analyze(s["screen"], cfg["tail_lines"], require_error=not trig)
        if not stuck:
            if st["streak"] or st["consecutive"]:
                st["streak"] = 0
                st["consecutive"] = 0
            if trig and "busy" in reason:
                _drop(trig["_path"])       # it recovered on its own; void the ticket
                reason += "; ticket voided"
                # The one branch that used to throw a ticket away in silence.
                # When a drop goes unrescued this line is the only thing that
                # can say the ticket arrived and what the screen looked like.
                log("ticket voided | %s | %s" % (s["tty"], reason))
            # An injection of ours that stalled in the input box blocks every
            # later rescue of this session, including this one. Clear it and let
            # the next sweep act; the ticket is deliberately left pending.
            elif (reason.startswith("input box not empty")
                    and (trig or ERR_TEXT.search(_norm(s["screen"])))
                    and is_stalled_injection(s["screen"], cfg["tail_lines"], cfg["retry_text"])):
                if not act or cfg["dry_run"] or os.path.exists(PAUSE_FLAG):
                    reason += "; would clear a stalled injection"
                elif time.time() - st.get("last_clear", 0) < cfg["cooldown_sec"]:
                    reason += "; stalled injection, clear cooling down"
                else:
                    st["last_clear"] = time.time()
                    cleared, detail = clear_input_box(s)
                    log("%s a stalled %r from the input box | %s | %s | %s"
                        % ("CLEARED" if cleared else "FAILED to clear",
                           cfg["retry_text"], sid, s["tty"], detail))
                    reason += "; cleared a stalled injection" if cleared else "; clear failed"
            report.append((sid, s["title"], "ok: %s" % reason))
            continue

        st["streak"] = st.get("streak", 0) + 1
        if not trig and st["streak"] < cfg["confirm_polls"]:
            report.append((sid, s["title"], "looks stuck (%d/%d confirmations)"
                           % (st["streak"], cfg["confirm_polls"])))
            continue

        elapsed = time.time() - st.get("last_sent", 0)
        if elapsed < cfg["cooldown_sec"]:
            report.append((sid, s["title"], "stuck but cooling down (%ds left)"
                           % int(cfg["cooldown_sec"] - elapsed)))
            continue
        if st.get("consecutive", 0) >= cfg["max_consecutive"]:
            report.append((sid, s["title"], "stuck but hit the retry cap (%d) -- needs a human"
                           % cfg["max_consecutive"]))
            continue

        if not act or cfg["dry_run"] or os.path.exists(PAUSE_FLAG):
            why = "DRY-RUN" if (cfg["dry_run"] or not act) else "PAUSED"
            report.append((sid, s["title"], "[%s] would inject %r" % (why, cfg["retry_text"])))
            log("DRY %s | %s | %s | would inject" % (sid, s["tty"], s["title"]))
            continue

        # The screen this decision rests on is up to a full sweep old, and a
        # session can wake in that gap -- a task notification is enough. Typing
        # into it then leaves the text sitting unsubmitted in the input box,
        # which shields the session from every later rescue. Re-read the one tab
        # and stand down if it moved. This can only ever cancel an injection.
        fresh = reread_screen(s)
        if fresh is not None:
            still, why, _fp = analyze(fresh, cfg["tail_lines"], require_error=False)
            if not still:
                report.append((sid, s["title"], "stood down at the last moment: %s" % why))
                st["streak"] = 0
                continue

        ok, detail = send_retry(s, cfg["retry_text"])
        st["last_sent"] = time.time()
        st["last_fp"] = fp
        st["streak"] = 0
        if trig:
            _drop(trig["_path"])           # consumed either way; never reused
        if ok:
            st["consecutive"] = st.get("consecutive", 0) + 1
            src = ("hook/job" if trig.get("_via") else "hook") if trig else "poll"
            log("SENT[%s] %s | %s | %s | retry #%d"
                % (src, sid, s["tty"], s["title"], st["consecutive"]))
            report.append((sid, s["title"], "injected %r (%s, retry #%d)"
                           % (cfg["retry_text"], src, st["consecutive"])))
            if cfg["notify"]:
                notify("Claude Code auto-resume", "%s\nsent %s" % (s["title"][:60], cfg["retry_text"]))
        else:
            log("FAIL %s | %s | injection failed: %s" % (sid, s["tty"], detail))
            report.append((sid, s["title"], "injection failed: %s" % detail))

    # A pending ticket that is not acted on must say why, every time the
    # reason changes. Silence here is what makes a missed drop unexplainable.
    verdicts = {}
    for row_sid, row_title, row_msg in report:
        row_tty = tty_by_sid.get(row_sid)
        if row_tty:
            verdicts[row_tty] = (row_msg, row_title)
    # A rehomed ticket expires under the tty it was written with, so file the
    # tab's verdict -- and its title -- under the daemon pty as well.
    for pty, tab in ticket_alias.items():
        if tab in verdicts:
            verdicts[pty] = verdicts[tab]
    _LAST_VERDICT.clear()
    _LAST_VERDICT.update(verdicts)
    for trig_tty, trig_rec in triggers.items():
        if not os.path.exists(trig_rec["_path"]):
            continue                    # consumed or voided this sweep
        filed = verdicts.get(trig_tty)
        msg = filed[0] if filed else unclaimed_note(trig_rec)
        ticket_note(trig_rec["_path"], trig_tty, msg)

    for k in list(state.keys()):
        if k not in seen_keys:
            del state[k]

    if cfg.get("snapshot", True):
        write_snapshot(sessions, report, SNAPSHOT_PATH)
    return report


# ---------------------------------------------------------------- snapshot

# The watchdog is the one process that already reads every terminal, so it
# publishes what it saw for anyone else who needs it -- cc-needs-you, the
# "which terminal is waiting for me" companion, consumes this file instead of
# running a second AppleScript sweep of its own. This is strictly an output:
# nothing in the retry decision reads it back.

# Coarse per-session state, derived from the sweep verdict. The verdict strings
# are the log's contract already; keeping the mapping here, in one place, means
# a reworded reason breaks a test rather than a downstream tool silently.
_STATE_RULES = (
    ("skipped",                     "skipped"),        # excluded, or no claude here
    ("session busy",                "working"),
    ("stood down",                  "working"),        # it moved between sweeps
    ("input box not empty",         "typing"),
    ("no such error this turn",     "idle"),           # turn over, waiting for a human
    ("output after the error",      "idle"),
    ("recap after the error",       "idle"),
    ("retry cap",                    "gave_up"),        # parked, and we will not retry again
    ("no input box found",          "not_claude_ui"),
    ("blank screen",                "not_claude_ui"),
    ("empty content area",          "not_claude_ui"),
)


def session_state(msg):
    for needle, state in _STATE_RULES:
        if needle in msg:
            return state
    return "dropped"       # stuck / cooling down / injected / would inject / injection failed


_SNAP_SINCE = {}           # sid -> (state, first seen in that state)


def write_snapshot(sessions, report, path):
    """Publish the last sweep as JSON. Never raises: a snapshot that fails
    must not cost a rescue."""
    try:
        verdict = {}
        for sid, _title, msg in report:
            verdict.setdefault(sid, msg)
        now_ts = time.time()
        rows = []
        seen = set()
        for s in sessions:
            sid = "%s:%s" % (s["app"], s["key"])
            if sid in seen:
                continue
            seen.add(sid)
            msg = verdict.get(sid, "")
            st = session_state(msg)
            prev = _SNAP_SINCE.get(sid)
            since = prev[1] if prev and prev[0] == st else now_ts
            _SNAP_SINCE[sid] = (st, since)
            rows.append({
                "sid": sid,
                "app": s["app"],
                "key": s["key"],
                "tty": s["tty"],
                "title": s["title"],
                "state": st,
                "since": round(since, 3),
                "verdict": msg,
            })
        for k in list(_SNAP_SINCE.keys()):
            if k not in seen:
                del _SNAP_SINCE[k]
        save_json(path, {"ts": round(now_ts, 3), "pid": os.getpid(), "sessions": rows})
    except Exception as e:
        log("WARN snapshot failed: %r" % e)


def main():
    args = sys.argv[1:]
    cfg = dict(DEFAULTS)
    cfg.update(load_json(CONFIG_PATH, {}))
    if not os.path.exists(CONFIG_PATH):
        save_json(CONFIG_PATH, cfg)

    if "--once" in args or "--check" in args:
        state = load_json(STATE_PATH, {})
        cfg["confirm_polls"] = 1       # a single sweep must be able to conclude
        act = "--once" in args
        rows = scan_once(cfg, state, act=act)
        save_json(STATE_PATH, state)
        if not rows:
            print("No Claude Code sessions found.")
        for sid, title, msg in rows:
            print("  %-30s %-42s %s" % (sid[:30], title[:42], msg))
        return

    log("watchdog up | poll %ds | dry_run=%s | text=%r"
        % (cfg["poll_interval_sec"], cfg["dry_run"], cfg["retry_text"]))
    state = load_json(STATE_PATH, {})
    rounds = 0
    last_beat = 0.0
    while True:
        try:
            cfg2 = dict(DEFAULTS)
            cfg2.update(load_json(CONFIG_PATH, {}))     # config is re-read live
            t0 = time.time()
            rows = scan_once(cfg2, state, act=True)
            dur = time.time() - t0
            save_json(STATE_PATH, state)
            rounds += 1
            if time.time() - last_beat > 600:
                last_beat = time.time()
                log("heartbeat | round %d | %.1fs | %d sessions" % (rounds, dur, len(rows)))
            # Drop to a 1s cadence while a ticket is waiting so the hook path
            # feels immediate.
            time.sleep(max(1, int(cfg2["fast_poll_sec"])) if tickets_pending()
                       else max(2, int(cfg2["poll_interval_sec"])))
        except KeyboardInterrupt:
            log("watchdog stopped")
            return
        except Exception as e:
            log("ERROR main loop: %r" % e)
            time.sleep(10)


if __name__ == "__main__":
    main()
