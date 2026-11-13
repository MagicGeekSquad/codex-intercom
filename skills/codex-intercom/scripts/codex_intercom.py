#!/usr/bin/env python3
"""codex-intercom: a communication link between Claude Code sessions and Codex sessions, built only
on documented Codex hooks and CLI commands.

Model
  manager    the caller that drives Codex sessions, normally one Claude Code session (id: --manager,
             else $CODEX_INTERCOM_MANAGER, else $CLAUDE_CODE_SESSION_ID, else "cli"). It owns every
             Codex session it sends to or claims. Another manager is refused unless it passes
             --take-over. While a session is owned (and its manager active), the gate hook keeps
             Codex from asking through App popups.
  attention  an owned session needs its manager when its latest turn has stopped and that stop was
             not reported yet, or when a message sent to it did not start a turn in time. This is
             derived every time from the hook event log, Codex's queue store, the sends that have
             not started yet and the per-session report cursor, so nothing is lost while nobody
             listens: the next `listen` reports it.

Commands
  send       deliver a message and claim the session. Working -> steered into the running turn
             (injected at the next tool-call boundary, or as a continuation when the turn is about
             to end). Stopped -> `codex queue`; if the Codex App does not pick it up (it only serves
             sessions it has open), the queue is run with `codex exec resume` in the session's
             working directory (transport "exec").
  listen     block until any session this manager owns needs attention; print ONE JSON line
             covering all of them (the latest stop of each, plus how many earlier stops went
             unreported), advance the report cursors, exit. A session that keeps working is
             checked in on every --checkin (default 30 min; per session via send/claim --checkin)
             with a progress snapshot. Run it in the background, one per manager; re-arm it while
             `watching` is not empty.
  list       this manager's sessions and their state      status   one session in detail
  claim      own a session without sending               release  give it back (popups allowed)
  doctor / install / uninstall / gc / hook / gate

Stop reasons
  done         Stop hook: the turn finished; `last_message` is the final assistant message
  asking       an App popup (request_user_input_async) still open when the turn stopped, or a
               Plan-mode request_user_input not answered yet
  approval     PermissionRequest, then nothing else for --approval-grace seconds
  error        model request failed, none in flight, quiet for --error-grace seconds (capacity /
               rate limit / connection; Codex fires no Stop hook in that case)
  interrupted  Interrupt hook
  ended        the `codex exec` process of an exec-transport run exited mid-turn
  stalled      no events for too long (tools running: --long-stall; model request in flight:
               --model-stall; otherwise --stall); e.g. the App crashed or closed mid-turn
  not_started  a sent message did not start a turn within --start-timeout seconds, or the exec run
               meant to start it exited first

Data: --dir, else $CODEX_INTERCOM_DIR, else ~/.codex/intercom/
  <session>/events.jsonl   hook event log           <session>/state.json  working / idle (steering)
  <session>/session.json   owner, report cursor, sends not started yet, exec run
  <session>/inbox/ delivered/  steered messages      <session>/exec-*.log  exec-transport output
  _managers/<manager>.json owned sessions, listener, last activity
Debug: create an empty file named DEBUG in the data dir to log raw hook inputs to debug.log.

Python 3.9+ stdlib only (macOS /usr/bin/python3 works).
"""
import argparse
import fcntl
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
DEFAULT_DIR = os.path.join(HOME, ".codex", "intercom")
INSTALL_PATH = os.path.join(HOME, ".codex", "hooks", "codex_intercom.py")
HOOKS_JSON = os.path.join(HOME, ".codex", "hooks.json")
HOOK_SCRIPT = "~/.codex/hooks/codex_intercom.py"  # as written into hooks.json (Codex runs it via a shell)
SYSTEM_PYTHON = "/usr/bin/python3"
OUR_SCRIPTS = ("codex_intercom.py", "codex_mailbox.py")  # current name + legacy name
# (event, matcher, timeout, async, subcommand). Sync where the hook must act (deliver / continue /
# deny) or where its record is the only sign of a stop (PostModelRequest: async records get lost when
# `codex exec` exits). Deliberately minimal: tool runs are inferred (a finished model response with no
# Stop means tools are running), exec runs are watched by pid, and compaction / session end are left
# to the stall timeouts.
HOOK_SPECS = [
    ("UserPromptSubmit", None, 5, False, "hook"),   # turn start (a sent message started), cwd, replies
    ("PostToolUse", ".*", 5, False, "hook"),        # steer delivery point
    ("Stop", None, 5, False, "hook"),               # done + end-of-turn delivery
    ("Interrupt", None, 2, False, "hook"),          # user pressed stop
    ("PostModelRequest", None, 5, False, "hook"),   # the only sign of model errors
    ("PreModelRequest", None, 5, True, "hook"),     # retry in flight vs. stopped on error
    ("PermissionRequest", ".*", 5, True, "hook"),   # waiting for an approval
    ("PreToolUse", "^request_user_input(_async)?$", 5, False, "gate"),  # App questions: log, deny if owned
]
MANAGERS = "_managers"
MANAGED_TTL = 24 * 3600  # a manager counts as gone after this long without activity (and no listener)
LEGACY_MARK = "managed.json"  # previous version's "managed" mark (honoured until it lapses)
LEGACY_FILES = ("watch.json", LEGACY_MARK)
GATE_REASON = ("这个会话目前由外部代理（Claude Code，代表用户）通过消息管理，App 里的提问弹窗没有人处理，"
               "还会挡住后续消息。不要用弹窗提问：还有不依赖答案的工作可以先做完；需要用户决定时，"
               "把问题和可选项直接写在回复里，然后结束本轮，答复会作为下一条消息发来。")
RARE_EVENTS = {"Interrupt", "PreToolUse", "PermissionRequest"}  # may legitimately not show up for weeks
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
SAFE_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
ASK_TOOL = "request_user_input"  # Plan-mode question: blocks the turn until answered
ASYNC_ASK_TOOL = "request_user_input_async"  # App popup: returns at once, the popup stays open, and the
# App holds queued messages until the user answers / skips / cancels it. The answer arrives as a user
# message "<send_user_message_question_reply> [{"questionItemId": "[\"request_user_input_async\",
# \"call_…\",0]", ...}]" through UserPromptSubmit.
ASK_TOOLS = (ASK_TOOL, ASYNC_ASK_TOOL)
REPLY_ID_RE = re.compile(r'request_user_input(?:_async)?\\*",\s*\\*"([A-Za-z0-9_.:-]+)')
EXEC_NUDGE = "请处理上一条消息。"  # exec resume needs a prompt; the queued message runs in the same turn
LAST_MSG_CHARS = 4000
MAX_EVENTS_BYTES = 1 << 20
KEEP_EVENTS = 2000
KEEP_EXEC_LOGS = 5
KEEP_EXPECT = 50
DEFAULT_CFG = {"error_grace": 15.0, "approval_grace": 30.0, "stall": 300.0, "model_stall": 900.0,
               "long_stall": 1800.0, "start_timeout": 300.0}
DEFAULT_CHECKIN = 1800.0  # progress check-in for a session that keeps working (send/claim --checkin overrides)
ROLLOUT_TAIL = 2 << 20  # bytes of the rollout read for a progress snapshot


# ---------------------------------------------------------------------------- helpers

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def ts_iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def base_dir(explicit=None):
    return explicit or os.environ.get("CODEX_INTERCOM_DIR") or DEFAULT_DIR


def manager_id(explicit=None):
    raw = (explicit or os.environ.get("CODEX_INTERCOM_MANAGER") or os.environ.get("CLAUDE_CODE_SESSION_ID")
           or "cli")
    return raw if SAFE_ID_RE.fullmatch(raw) else "h-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def manager_source(explicit=None):
    if explicit:
        return "--manager"
    for name in ("CODEX_INTERCOM_MANAGER", "CLAUDE_CODE_SESSION_ID"):
        if os.environ.get(name):
            return "$" + name
    return "default"


def tilde(path):
    return "~" + path[len(HOME):] if path and path.startswith(HOME + os.sep) else path


def parse_thread(value):
    m = UUID_RE.search(value or "")
    if not m:
        sys.exit("error: no session UUID found in %r" % (value,))
    return m.group(0).lower()


def write_json_atomic(path, obj, indent=None):
    tmp = "%s.tmp-%s" % (path, uuid.uuid4().hex[:8])
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
        if indent:
            f.write("\n")
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def clip(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n] + "…"


def file_sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def span(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return "%d 秒" % seconds
    if seconds < 3600:
        return "%d 分钟" % (seconds // 60)
    return "%d 小时 %d 分钟" % (seconds // 3600, seconds % 3600 // 60)


def public(item):
    """Drop internal keys (cursor time, commit hints) before printing."""
    return {k: v for k, v in item.items() if not k.startswith("_") and k != "t"}


@contextmanager
def flocked(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (TypeError, ValueError, OSError):
        return False
    return True


def exec_alive(pid):
    """pid_alive, but first reap our own exited child (a zombie still answers kill 0)."""
    try:
        done_pid, _ = os.waitpid(int(pid), os.WNOHANG)
        if done_pid:
            return False
    except ChildProcessError:
        pass
    except (TypeError, ValueError, OSError):
        return False
    return pid_alive(pid)


def our_process_alive(rec):
    """A recorded listener counts only while its process exists and (when ps can tell) is ours."""
    if not rec or not pid_alive(rec.get("pid")):
        return False
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(rec["pid"])], capture_output=True, text=True,
                             timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return "codex_intercom" in out if out.strip() else True


def queue_db_path():
    env = os.environ.get("CODEX_QUEUE_DB")
    if env:
        return env
    cands = glob.glob(os.path.join(HOME, ".codex", "queue_*.sqlite"))
    cands.sort(key=lambda p: int((re.search(r"queue_(\d+)\.sqlite$", p) or re.search("()", "")).group(1) or 0))
    return cands[-1] if cands else None


def codex_queue_map():
    """Read-only look at Codex's queue store: {queued item id: session id}. None if unreadable."""
    path = queue_db_path()
    if not path or not os.path.exists(path):
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)
        try:
            return dict(con.execute("select id, thread_id from queued_items").fetchall())
        finally:
            con.close()
    except sqlite3.Error:
        return None


def queued_count(queue, sid):
    return None if queue is None else sum(1 for tid in queue.values() if tid == sid)


def parse_duration(value):
    """600, 90s, 10m, 1h -> seconds; 0 / off -> 0 (disabled)."""
    s = str(value).strip().lower()
    if s in ("0", "off", "none", "no"):
        return 0.0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smh]?)", s)
    if not m:
        raise argparse.ArgumentTypeError("duration like 600, 90s, 10m, 1h, or off")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def rollout_paths(sid):
    root = os.environ.get("CODEX_SESSIONS_DIR") or os.path.join(HOME, ".codex", "sessions")
    return sorted(glob.glob(os.path.join(root, "*", "*", "*", "rollout-*-%s.jsonl" % sid)))


def rollout_progress(sid, turn_id):
    """Best effort: what Codex did in this turn, from the tail of its rollout file (Codex's own format,
    which may change; anything unreadable is skipped). None if nothing was found."""
    paths = rollout_paths(sid)
    if not paths or not turn_id:
        return None
    try:
        with open(paths[-1], "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - ROLLOUT_TAIL)
            f.seek(start)
            lines = f.read().split(b"\n")
    except OSError:
        return None
    if start:
        lines = lines[1:]  # the first line was cut
    msgs, cmds, files, mcp = [], [], [], []
    ncmd = compactions = 0
    began = False
    for raw in lines:
        if turn_id.encode() not in raw:
            continue
        try:
            o = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        p = o.get("payload") if isinstance(o, dict) else None
        if not isinstance(p, dict) or o.get("type") != "event_msg" or p.get("turn_id") != turn_id:
            continue
        if p.get("type") == "task_started":
            began = True
        if p.get("type") != "item_completed" or not isinstance(p.get("item"), dict):
            continue
        it = p["item"]
        kind = it.get("type")
        if kind == "AgentMessage":
            text = " ".join(c.get("text", "") for c in it.get("content") or [] if isinstance(c, dict))
            if text.strip():
                msgs.append(clip(text, 300))
        elif kind == "CommandExecution":
            ncmd += 1
            cmd = it.get("command")
            cmd = cmd[-1] if isinstance(cmd, list) and cmd else cmd
            entry = {"cmd": clip(cmd or "", 160), "status": it.get("status")}
            code = it.get("exit_code", it.get("exitCode"))
            if code is not None:
                entry["exit_code"] = code
            cmds.append(entry)
        elif kind == "FileChange" and isinstance(it.get("changes"), dict):
            for path in it["changes"]:
                if tilde(path) not in files:
                    files.append(tilde(path))
        elif kind == "McpToolCall":
            mcp.append("%s.%s" % (it.get("server"), it.get("tool")))
        elif kind == "ContextCompaction":
            compactions += 1
    if not (msgs or cmds or files or mcp or compactions):
        return None
    out = {"messages": msgs[-3:], "commands_run": ncmd, "recent_commands": cmds[-5:], "files_changed": files[-10:]}
    if mcp:
        out["mcp_calls"] = len(mcp)
    if compactions:
        out["compactions"] = compactions
    if start and not began:
        out["partial"] = True  # the turn began before the part of the file that was read
    return out


def progress_snapshot(sid, events, info, now):
    """A working session's progress: from the hook log, plus Codex's own record (best effort)."""
    main = [e for e in events if not e.get("sub")]
    ups = [e for e in main if e.get("ev") == "UserPromptSubmit"]
    start = ups[-1] if ups else None
    tm = [e for e in main if e["t"] >= start["t"]] if start else main
    snap = {"turn_id": start.get("turn") if start else info.get("turn_id"),
            "running_s": int(now - start["t"]) if start else None,
            "in_flight": info.get("in_flight"), "silence_s": info.get("silence_s"), "last_event": info.get("last_event"),
            "tool_calls": sum(1 for e in tm if e.get("ev") == "PostToolUse"),
            "model_requests": sum(1 for e in tm if e.get("ev") == "PostModelRequest"),
            "failed_requests": sum(1 for e in tm if e.get("ev") == "PostModelRequest" and e.get("status") == "failed")}
    activity = rollout_progress(sid, snap["turn_id"])
    if activity:
        snap["activity"] = activity
    return snap


def rollout_cwd(sid):
    """Working directory of the session's latest turn, from its rollout file (fallback only)."""
    cwd = None
    for path in rollout_paths(sid):
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"cwd"' in line and ('"turn_context"' in line or '"session_meta"' in line):
                    m = re.search(r'"cwd":"((?:[^"\\]|\\.)*)"', line)
                    if m:
                        cwd = json.loads('"%s"' % m.group(1))
    return cwd


class EventLog(object):
    """Reads an events.jsonl incrementally: it is append-only, and rotation replaces the file."""

    def __init__(self, path):
        self.path = path
        self.ino = None
        self.off = 0
        self.items = []

    def read(self):
        try:
            st = os.stat(self.path)
        except OSError:
            self.ino, self.off, self.items = None, 0, []
            return self.items
        if st.st_ino != self.ino or st.st_size < self.off:
            self.ino, self.off, self.items = st.st_ino, 0, []
        if st.st_size > self.off:
            try:
                with open(self.path, "rb") as f:
                    f.seek(self.off)
                    chunk = f.read(st.st_size - self.off)
            except OSError:
                return self.items
            end = chunk.rfind(b"\n")  # a line still being written waits for the next read
            if end >= 0:
                new = []
                for line in chunk[:end + 1].splitlines():
                    try:
                        e = json.loads(line.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if isinstance(e, dict) and isinstance(e.get("t"), (int, float)):
                        new.append(e)
                self.off += end + 1
                if new:
                    self.items = sorted(self.items + new, key=lambda e: e["t"])
        return self.items


class Manager(object):
    """Owner of Codex sessions: an index of them, its listener, and when it was last active."""

    def __init__(self, base, mid):
        self.base = base
        self.id = mid
        self.dir = os.path.join(base, MANAGERS)
        self.path = os.path.join(self.dir, mid + ".json")
        self.lock_path = os.path.join(self.dir, mid + ".lock")

    def data(self):
        d = read_json(self.path, {}) or {}
        if not isinstance(d.get("sessions"), dict):
            d["sessions"] = {}
        return d

    def save(self, d):
        write_json_atomic(self.path, d)

    @contextmanager
    def locked(self):
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        with flocked(self.lock_path):
            yield

    def listener(self, d=None):
        rec = (d if d is not None else self.data()).get("listener")
        return rec if our_process_alive(rec) else None

    def active(self, now=None, d=None):
        d = d if d is not None else self.data()
        return (now or time.time()) - d.get("last_active", 0) < MANAGED_TTL or bool(self.listener(d))

    def add(self, sid):
        with self.locked():
            d = self.data()
            d["sessions"].setdefault(sid, {"since": now_iso()})
            d["last_active"] = time.time()
            self.save(d)

    def remove(self, sids):
        with self.locked():
            d = self.data()
            for sid in sids:
                d["sessions"].pop(sid, None)
            self.save(d)

    def touch(self):
        with self.locked():
            d = self.data()
            d["last_active"] = time.time()
            self.save(d)


class Session(object):
    def __init__(self, base, sid):
        self.base = base
        self.sid = sid
        self.root = os.path.join(base, sid)
        self.inbox = os.path.join(self.root, "inbox")
        self.delivered = os.path.join(self.root, "delivered")
        self.state_path = os.path.join(self.root, "state.json")
        self.events_path = os.path.join(self.root, "events.jsonl")
        self.rec_path = os.path.join(self.root, "session.json")
        self.lock_path = os.path.join(self.root, ".lock")

    def ensure(self):
        os.makedirs(self.base, mode=0o700, exist_ok=True)
        for d in (self.root, self.inbox, self.delivered):
            os.makedirs(d, mode=0o700, exist_ok=True)

    @contextmanager
    def locked(self):
        self.ensure()
        with flocked(self.lock_path):
            yield

    # --- session record: owner, report cursor, sends not started yet, exec run
    def record(self):
        r = read_json(self.rec_path, {}) or {}
        if not isinstance(r.get("expect"), list):
            r["expect"] = []
        return r

    def save_record(self, r):
        write_json_atomic(self.rec_path, r)

    def edit_record(self, fn):
        with self.locked():
            r = self.record()
            fn(r)
            self.save_record(r)

    def is_managed(self, now=None):
        """Owned by an active manager: the gate refuses App popup questions. A mark left by the
        previous version (managed.json, no manager id) still counts until it lapses."""
        mid = self.record().get("manager")
        if mid:
            return Manager(self.base, mid).active(now)
        legacy = read_json(os.path.join(self.root, LEGACY_MARK)) or {}
        return (now or time.time()) - legacy.get("last_active", 0) < MANAGED_TTL

    def drop_legacy_mark(self):
        try:
            os.remove(os.path.join(self.root, LEGACY_MARK))
        except OSError:
            pass

    # --- steering state
    def state(self):
        return read_json(self.state_path, {}) or {}

    def set_state(self, status, turn_id=None, event=None):
        st = self.state()
        st.update({"status": status, "updated_at": now_iso(), "last_event": event})
        if turn_id:
            st["turn_id"] = turn_id
        write_json_atomic(self.state_path, st)

    # --- event log (append-only; one os.write per line, so concurrent hooks don't interleave)
    def log_event(self, rec):
        self.ensure()
        line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(self.events_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)

    def rotate_events(self):
        """Caller holds the lock. Keep the log bounded."""
        try:
            if os.path.getsize(self.events_path) <= MAX_EVENTS_BYTES:
                return
            with open(self.events_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-KEEP_EVENTS:]
        except OSError:
            return
        tmp = "%s.tmp-%s" % (self.events_path, uuid.uuid4().hex[:8])
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.replace(tmp, self.events_path)

    def events(self):
        return EventLog(self.events_path).read()

    def last_cwd(self):
        for e in reversed(self.events()):
            if e.get("cwd") and not e.get("sub"):
                return e["cwd"]
        return rollout_cwd(self.sid)

    # --- inbox
    def pending_names(self):
        try:
            names = os.listdir(self.inbox)
        except OSError:
            return []
        return sorted(n for n in names if n.endswith(".json"))

    def inbox_messages(self):
        return [m for m in (read_json(os.path.join(self.inbox, n)) for n in self.pending_names()) if m]

    def put(self, msg):
        write_json_atomic(os.path.join(self.inbox, msg["id"] + ".json"), msg)

    def receipt(self, msg_id):
        return read_json(os.path.join(self.delivered, msg_id + ".json"))

    def drain(self, via, turn_id):
        """Caller holds the lock. Move every inbox message to delivered/ with a receipt."""
        out = []
        for name in self.pending_names():
            src = os.path.join(self.inbox, name)
            msg = read_json(src)
            if msg is None or not isinstance(msg.get("text"), str):
                os.replace(src, os.path.join(self.delivered, name + ".corrupt"))
                continue
            msg.update({"delivered_at": now_iso(), "delivered_via": via, "delivered_turn_id": turn_id})
            write_json_atomic(os.path.join(self.delivered, name), msg)
            os.remove(src)
            out.append(msg)
        return out


# ---------------------------------------------------------------------------- stop detection

def analyze(events, now, cfg=None):
    """State of the session's latest turn (from its last UserPromptSubmit on). Returns (stopped, info).
    A stop's info carries `reason` and `t`: a time that stays the same for that stop while nothing
    new happens, so it can be compared with the report cursor."""
    cfg = cfg or DEFAULT_CFG
    if not events:
        return True, {"reason": "unknown", "t": None,
                      "summary": "没有这个 session 的 hook 事件记录（hooks 未生效，或安装后它还没运行过）"}
    main = [e for e in events if not e.get("sub")]
    starts = [e["t"] for e in main if e.get("ev") == "UserPromptSubmit"]
    seg = [e for e in events if e["t"] >= starts[-1]] if starts else list(events)
    seg_main = [e for e in seg if not e.get("sub")]
    answered = {e.get("tool_use_id") for e in main if e.get("ev") == "PostToolUse" and e.get("tool_use_id")}

    prev = None
    for e in seg_main:  # explicit stop events, earliest first
        ev = e.get("ev")
        if (ev == "SessionEnd" and prev and prev.get("ev") == "PostModelRequest"
                and prev.get("status") == "failed"):
            err = prev.get("error") or "unknown error"  # e.g. `codex exec` exiting after a failed turn
            return True, {"reason": "error", "turn_id": prev.get("turn"), "t": prev["t"], "at": ts_iso(prev["t"]),
                          "error": err, "failures": 1, "summary": "Codex 模型请求失败后停下：%s" % clip(err, 200)}
        prev = e
        if ev == "Stop" and not e.get("continued"):
            msg = e.get("msg") or ""
            info = {"turn_id": e.get("turn"), "t": e["t"], "at": ts_iso(e["t"]), "last_message": msg}
            popups = open_questions(main, until=e["t"], kinds=(ASYNC_ASK_TOOL,))
            if popups:
                qs = flat_questions(popups)
                info.update(reason="asking", questions=qs,
                            summary="Codex 本轮结束，App 里有提问弹窗等用户处理：%s"
                                    % clip("；".join(q.get("question", "") for q in qs), 200))
            else:
                info.update(reason="done", summary="Codex 本轮已结束：%s" % clip(msg[-200:], 200))
            return True, info
        if ev == "Interrupt":
            return True, {"reason": "interrupted", "turn_id": e.get("turn"), "t": e["t"], "at": ts_iso(e["t"]),
                          "summary": "Codex 本轮被中断"}
        if ev == "SessionEnd":
            info = {"reason": "ended", "t": e["t"], "at": ts_iso(e["t"]),
                    "summary": "codex exec 进程已退出，本轮没有正常结束" if e.get("exec_exit") else "Codex 会话已结束"}
            if e.get("exec_log"):
                info["exec_log"] = e["exec_log"]
            return True, info
        if (ev == "PreToolUse" and e.get("tool") == ASK_TOOL and not e.get("denied")
                and e.get("tool_use_id") not in answered):
            qs = e.get("questions") or []
            return True, {"reason": "asking", "turn_id": e.get("turn"), "t": e["t"], "at": ts_iso(e["t"]),
                          "questions": qs,
                          "summary": "Codex 在等用户回答问题：%s" % clip("；".join(q.get("question", "") for q in qs), 200)}

    last_any = seg[-1]
    silence = now - last_any["t"]
    lm = seg_main[-1] if seg_main else {}
    main_silence = now - lm["t"] if lm else silence
    turn = lm.get("turn")
    tm = [e for e in main if turn and e.get("turn") == turn]

    def count(name):
        return sum(1 for e in tm if e.get("ev") == name)

    model_inflight = count("PreModelRequest") - count("PostModelRequest")
    tools_inflight = sorted({e.get("tool") for e in tm if e.get("ev") == "PreToolUse" and e.get("tool_use_id")
                             and not e.get("denied") and e.get("tool_use_id") not in answered})
    compact_inflight = count("PreCompact") - count("PostCompact")  # only in logs from older installs
    # A model response finished without a Stop, or a tool just returned (parallel calls may still run):
    # tools are running. Only question tools are logged, so this is inferred rather than tracked.
    tool_running = lm.get("ev") in ("PostToolUse", "PreToolUse") or (
        lm.get("ev") == "PostModelRequest" and lm.get("status") == "completed")

    if (lm.get("ev") == "PostModelRequest" and lm.get("status") == "failed"
            and model_inflight <= 0 and main_silence >= cfg["error_grace"]):
        streak = []
        for e in reversed([x for x in tm if x.get("ev") == "PostModelRequest"]):
            if e.get("status") != "failed":
                break
            streak.append(e)
        first = streak[-1] if streak else lm  # retries keep failing: still the same stop
        err = lm.get("error") or "unknown error"
        failures = len(streak) or 1
        return True, {"reason": "error", "turn_id": turn, "t": first["t"], "at": ts_iso(lm["t"]), "error": err,
                      "failures": failures,
                      "summary": "Codex 模型请求失败后停下（连续 %d 次）：%s" % (failures, clip(err, 200))}

    perms = [e for e in seg_main if e.get("ev") == "PermissionRequest"]
    if perms and main_silence >= cfg["approval_grace"]:
        p = perms[-1]
        if all(e.get("ev") == "PreToolUse" for e in seg_main if e["t"] > p["t"]):
            return True, {"reason": "approval", "turn_id": turn, "t": p["t"], "at": ts_iso(p["t"]),
                          "tool": p.get("tool"), "detail": p.get("detail"),
                          "summary": "Codex 在等待批准：%s %s" % (p.get("tool"), clip(p.get("detail") or "", 160))}

    if model_inflight > 0:
        limit, doing = cfg["model_stall"], "model"
    elif tools_inflight or tool_running:
        limit, doing = cfg["long_stall"], "tool"
    elif compact_inflight > 0:
        limit, doing = cfg["long_stall"], "compact"
    else:
        limit, doing = cfg["stall"], "between steps"
    info = {"turn_id": turn, "last_event": last_any.get("ev"), "last_event_at": ts_iso(last_any["t"]),
            "silence_s": int(silence), "in_flight": doing}
    if silence >= limit:
        info.update({"reason": "stalled", "t": last_any["t"], "at": ts_iso(last_any["t"]),
                     "summary": "Codex 已 %d 秒没有任何事件（%s，最后事件 %s），可能已停下"
                                % (silence, doing, last_any.get("ev"))})
        return True, info
    info["reason"] = "working"
    return False, info


def stops_between(main, lo, hi):
    """Turn ends (Stop / Interrupt) strictly between two times: stops nobody was told about."""
    return sum(1 for e in main if lo < e["t"] < hi
               and ((e.get("ev") == "Stop" and not e.get("continued")) or e.get("ev") == "Interrupt"))


def exec_end(main, ex):
    """A synthetic SessionEnd once the exec-transport process is gone (there is no SessionEnd hook):
    `before_start` if it never started a turn; None if its turn is over and a later one followed."""
    if not ex or not ex.get("pid") or exec_alive(ex["pid"]):
        return None
    started = ex.get("started") or 0
    try:
        mtime = os.path.getmtime(ex.get("log") or "")
    except OSError:
        mtime = started
    ups = [e for e in main if e.get("ev") == "UserPromptSubmit" and e["t"] > started]  # its turn starts after spawn
    if not ups:
        return {"t": round(max(mtime, started), 3), "ev": "SessionEnd", "exec_exit": True, "before_start": True,
                "exec_log": tilde(ex.get("log"))}
    first = ups[0]
    if any(e.get("turn") != first.get("turn") for e in ups[1:]):
        return None
    last = max(e["t"] for e in main if e["t"] >= first["t"])
    return {"t": round(max(mtime, last) + 0.001, 3), "ev": "SessionEnd", "exec_exit": True,
            "exec_log": tilde(ex.get("log"))}


def current(rec, events, now, cfg):
    """analyze() of the latest turn, counting an exec run that died mid-turn as its end."""
    end = exec_end([e for e in events if not e.get("sub")], rec.get("exec"))
    evs = sorted(events + [end], key=lambda e: e["t"]) if end and not end.get("before_start") else events
    stopped, info = analyze(evs, now, cfg)
    return stopped, info, end


def assess(box, rec, events, now, cfg, queue):
    """Everything listen / list / status need about one session. `queue`: Codex's queue store as
    {item id: session id}, or None when unreadable (then a send counts as started on the next
    UserPromptSubmit alone). Returns a dict:
      stopped, info   analyze() of the latest turn
      waiting         sends that have not started a turn yet; started: those that have
      attention       what listen reports to the owner right now, or None
      exec_end        the synthetic end of a finished exec run, if any"""
    main = [e for e in events if not e.get("sub")]
    prompts = [e["t"] for e in main if e.get("ev") == "UserPromptSubmit"]
    waiting, started = [], []
    for x in rec.get("expect") or []:
        in_queue = bool(x.get("qid")) and queue is not None and x["qid"] in queue
        if not x.get("queuing") and not in_queue and any(t > x["t"] for t in prompts):
            started.append(x)
        else:
            waiting.append(x)
    stopped, info, end = current(rec, events, now, cfg)
    reported = rec.get("reported") or 0
    att = None
    if waiting:  # a send of ours has not started: the session's current state isn't the answer
        first = min(x["t"] for x in waiting)
        ex = rec.get("exec") or {}
        if end and end.get("before_start") and (ex.get("started") or 0) >= first - 1:
            att = {"reason": "not_started", "t": end["t"], "at": ts_iso(end["t"]), "exec_exit": True,
                   "exec_log": end.get("exec_log"), "summary": "codex exec 进程退出了，发出的消息没有开始处理"}
        elif now - first >= cfg["start_timeout"]:
            t = round(first + cfg["start_timeout"], 3)
            att = {"reason": "not_started", "t": t, "at": ts_iso(t),
                   "summary": "%d 秒内 Codex 没有开始处理发出的消息" % (now - first)}
        if att:
            att["queued"] = [{"id": x.get("id"), "preview": x.get("preview"), "transport": x.get("transport")}
                             for x in waiting]
            att["codex_queue"] = queued_count(queue, box.sid)
            if stopped and info.get("reason") != "unknown":  # what holds it up, e.g. approval / asking
                att["session_state"] = {"reason": info["reason"], "summary": info.get("summary")}
            blocked = blocking_popups(main)
            if blocked:
                att["blocked_by_question"] = flat_questions(blocked)
    elif stopped and info.get("t") and info["t"] > reported:
        att = dict(info)
        n = stops_between(main, reported, info["t"])
        if n:
            att["earlier_stops"] = n
    if att is not None:
        undelivered = box.inbox_messages()
        if undelivered:
            att["undelivered"] = [{"id": m["id"], "preview": clip(m.get("text", ""), 80)} for m in undelivered]
    return {"stopped": stopped, "info": info, "waiting": waiting, "started": started, "attention": att,
            "exec_end": end}


# ---------------------------------------------------------------------------- hook side

def context_text(msgs):
    parts = [
        "【用户插话】你正在执行当前任务时，用户发来了新消息。把它当作用户本人的最新指示："
        "立即纳入考虑；如果与当前计划冲突，以这条消息为准并相应调整。"
    ]
    for m in msgs:
        via = "（经由 %s 转发）" % m["from"] if m.get("from") else ""
        parts.append("--- 消息%s，发送于 %s ---\n%s" % (via, m.get("created_at", "?"), m["text"]))
    return "\n\n".join(parts)


def ui_text(msgs):
    first = " ".join(msgs[0]["text"].split())
    if len(first) > 80:
        first = first[:80] + "…"
    more = "（另有 %d 条）" % (len(msgs) - 1) if len(msgs) > 1 else ""
    return "已插入消息：%s%s" % (first, more)


def continuation_text(msgs):
    return "\n\n".join(m["text"] for m in msgs)


def is_main_thread(data):
    """Subagent hooks carry the parent's session_id, but also agent_id/agent_type and their own
    transcript file (verified on codex 0.167)."""
    if data.get("agent_id") or data.get("agent_type"):
        return False
    tp = data.get("transcript_path")
    if not tp:
        return True
    return data.get("session_id", "") in os.path.basename(tp)


def summarize_questions(tool_input):
    """Both shapes: Plan mode {question, options:[{label}]} and App popup {title, options:[str]}."""
    qs = tool_input.get("questions") if isinstance(tool_input, dict) else None
    out = []
    for q in qs if isinstance(qs, list) else []:
        if isinstance(q, dict):
            opts = [o.get("label") if isinstance(o, dict) else o for o in q.get("options") or []]
            out.append({"question": clip(q.get("question") or q.get("title") or "", 300),
                        "options": [str(o) for o in opts if o][:6]})
    return out


def open_questions(main, until=None, kinds=ASK_TOOLS):
    """Questions asked (PreToolUse of an ask tool, not denied) and not closed before `until`. A
    Plan-mode question closes with its PostToolUse; a popup closes with a reply naming its call id,
    or (if it was skipped / cancelled without a reply) once a later user prompt starts another turn."""
    evs = [e for e in main if until is None or e["t"] < until]
    asks = [e for e in evs if e.get("ev") == "PreToolUse" and e.get("tool") in kinds and not e.get("denied")]
    if not asks:
        return []
    posted = {e.get("tool_use_id") for e in evs if e.get("ev") == "PostToolUse"}
    prompts = [e for e in evs if e.get("ev") == "UserPromptSubmit"]
    replied = {rid for e in prompts for rid in e.get("replies") or []}
    out = []
    for a in asks:
        tid = a.get("tool_use_id")
        if a.get("tool") == ASK_TOOL:
            closed = tid in posted
        else:
            closed = tid in replied or any(p["t"] > a["t"] and p.get("turn") != a.get("turn") for p in prompts)
        if not closed:
            out.append(a)
    return out


def flat_questions(asks):
    return [q for a in asks for q in a.get("questions") or []]


def blocking_popups(main):
    """Open popup questions while the session sits idle: the App won't start queued messages."""
    if not main:
        return []
    last = main[-1]
    idle = (last.get("ev") == "Stop" and not last.get("continued")) or last.get("ev") in ("Interrupt", "SessionEnd")
    return open_questions(main, kinds=(ASYNC_ASK_TOOL,)) if idle else []


def describe_tool_input(ti):
    if isinstance(ti, dict):
        for k in ("description", "command", "justification"):
            v = ti.get(k)
            if v:
                return clip(" ".join(v) if isinstance(v, list) else v, 300)
        return clip(json.dumps(ti, ensure_ascii=False), 300)
    return clip(ti, 300) if ti else None


def event_record(data, main):
    ev = data.get("hook_event_name")
    rec = {"t": round(time.time(), 3), "ev": ev}
    if data.get("turn_id"):
        rec["turn"] = data["turn_id"]
    if not main:
        rec["sub"] = data.get("agent_type") or data.get("agent_id") or "subagent"
    if ev == "UserPromptSubmit":
        prompt = str(data.get("prompt") or "")
        if data.get("cwd"):
            rec["cwd"] = data["cwd"]
        if "send_user_message_question_reply" in prompt:
            rec["replies"] = sorted(set(REPLY_ID_RE.findall(prompt)))
    if ev in ("PreToolUse", "PostToolUse", "PermissionRequest"):
        rec["tool"] = data.get("tool_name")
        if data.get("tool_use_id"):
            rec["tool_use_id"] = data["tool_use_id"]
    if ev == "PreToolUse" and data.get("tool_name") in ASK_TOOLS:
        rec["questions"] = summarize_questions(data.get("tool_input"))
    if ev == "PermissionRequest":
        rec["detail"] = describe_tool_input(data.get("tool_input"))
    if ev == "PostModelRequest":
        rec["status"] = data.get("status")
        if data.get("error"):
            rec["error"] = clip(data["error"], 1000)
    if ev == "Stop" and isinstance(data.get("last_assistant_message"), str):
        rec["msg"] = data["last_assistant_message"][-LAST_MSG_CHARS:]
    if ev in ("PreCompact", "PostCompact"):
        rec["trigger"] = data.get("trigger")
    return rec


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()


def handle_hook(base, data):
    event = data.get("hook_event_name")
    sid = (data.get("session_id") or "").lower()
    turn = data.get("turn_id")
    if not UUID_RE.fullmatch(sid):
        return None
    main = is_main_thread(data)
    box = Session(base, sid)
    rec = event_record(data, main)
    if event == "PreToolUse" and data.get("tool_name") in ASK_TOOLS and box.is_managed():
        rec["denied"] = True  # (older installs logged PreToolUse here) the gate refuses it: no popup

    if not main or event not in ("PostToolUse", "UserPromptSubmit", "Stop", "Interrupt", "SessionEnd"):
        box.log_event(rec)  # status-only events (and everything from subagents)
        return None

    if event in ("PostToolUse", "UserPromptSubmit"):
        box.log_event(rec)
        st = box.state()
        if not box.pending_names() and st.get("status") == "working" and st.get("turn_id") == turn:
            return None  # fast path: nothing to deliver, state already current
        with box.locked():
            if event == "UserPromptSubmit":
                box.rotate_events()
            box.set_state("working", turn, event)
            msgs = box.drain(event, turn)
        if msgs:
            return {
                "systemMessage": ui_text(msgs),
                "hookSpecificOutput": {"hookEventName": event, "additionalContext": context_text(msgs)},
            }
        return None

    if event == "Stop":
        with box.locked():
            msgs = box.drain(event, turn)
            box.set_state("working" if msgs else "idle", turn, event)
            rec["continued"] = bool(msgs)
            box.log_event(rec)
        if msgs:
            return {"decision": "block", "reason": continuation_text(msgs), "systemMessage": ui_text(msgs)}
        return {}

    # Interrupt / SessionEnd. Undelivered messages stay in the inbox and ride along with the next
    # user prompt (UserPromptSubmit); listen reports them as `undelivered`.
    box.log_event(rec)
    with box.locked():
        box.set_state("idle", turn, event)
    return None


def log_error(base, what):
    try:
        os.makedirs(base, mode=0o700, exist_ok=True)
        with open(os.path.join(base, "errors.log"), "a", encoding="utf-8") as f:
            f.write("%s %s\n%s\n" % (now_iso(), what, traceback.format_exc()))
    except OSError:
        pass


def debug_log(base, data):
    if os.path.exists(os.path.join(base, "DEBUG")):
        with open(os.path.join(base, "debug.log"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": now_iso(), "input": data}, ensure_ascii=False) + "\n")


def cmd_gate(args):
    """Sync PreToolUse hook for the question tools (App popup / Plan mode). Records the question,
    and while the session is owned by an active manager refuses it, so Codex asks in its reply
    instead. Any error fails open (no output), like Codex itself does."""
    base = base_dir(args.dir)
    try:
        data = json.loads(sys.stdin.read())
    except ValueError:
        return 0
    sid = (data.get("session_id") or "").lower()
    if data.get("hook_event_name") != "PreToolUse" or not UUID_RE.fullmatch(sid):
        return 0
    box = Session(base, sid)
    deny = False
    try:
        debug_log(base, data)
        deny = data.get("tool_name") in ASK_TOOLS and box.is_managed()
        rec = event_record(data, is_main_thread(data))
        if deny:
            rec["denied"] = True
        box.log_event(rec)
    except Exception:
        log_error(base, "gate")
    if deny:
        emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                     "permissionDecisionReason": GATE_REASON}})
    return 0


def cmd_hook(args):
    base = base_dir(args.dir)
    event = None
    try:
        data = json.loads(sys.stdin.read())
        event = data.get("hook_event_name")
        debug_log(base, data)
        out = handle_hook(base, data)
    except Exception:
        log_error(base, event)
        out = {} if event == "Stop" else None
    if out is not None:
        emit(out)
    return 0


# ---------------------------------------------------------------------------- ownership

def claim(box, mgr, take_over=False, label=None, checkin=None):
    """Make `mgr` the owner of the session. Returns (ok, info). An active other owner is refused
    unless take_over; a lapsed one (no activity for MANAGED_TTL, no listener) is replaced."""
    now = time.time()
    info = {}
    with box.locked():
        rec = box.record()
        cur = rec.get("manager")
        if cur and cur != mgr.id:
            other = Manager(box.base, cur)
            od = other.data()
            listening = bool(other.listener(od))
            if other.active(now, od):
                if not take_over:
                    return False, {
                        "ok": False, "error": "owned_by_another_manager", "thread": box.sid, "owner": cur,
                        "owner_listening": listening, "owner_last_active": ts_iso(od.get("last_active")),
                        "summary": "这个 Codex session 正由另一个管理者（%s）管理；确需接管时加 --take-over" % cur}
                info["taken_over_from"] = cur
            else:
                info["previous_owner"] = cur  # lapsed
        if cur != mgr.id:
            # A new owner starts fresh: earlier stops and the previous owner's sends are not its news.
            rec.update({"manager": mgr.id, "since": now_iso(), "reported": now, "expect": []})
            info["claimed"] = True
        if label:
            rec["label"] = label
        if checkin is not None:
            rec["checkin"] = checkin
        box.save_record(rec)
        box.drop_legacy_mark()
    mgr.add(box.sid)
    return True, info


def cmd_claim(args):
    base = base_dir(args.dir)
    box, mgr = Session(base, parse_thread(args.thread)), Manager(base, manager_id(args.manager))
    ok, info = claim(box, mgr, args.take_over, args.label, args.checkin)
    if not ok:
        print(json.dumps(info, ensure_ascii=False))
        return 2
    out = {"thread": box.sid, "manager": mgr.id, "owned": True}
    out.update(info)
    rec = box.record()
    for k in ("label", "checkin"):
        if rec.get(k) is not None:
            out[k] = rec[k]
    out["listening"] = bool(mgr.listener())
    print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_release(args):
    base = base_dir(args.dir)
    box, mgr = Session(base, parse_thread(args.thread)), Manager(base, manager_id(args.manager))
    with box.locked():
        rec = box.record()
        cur = rec.get("manager")
        if cur and cur != mgr.id and not args.force:
            print(json.dumps({"ok": False, "error": "owned_by_another_manager", "thread": box.sid, "owner": cur,
                              "summary": "这个 session 由另一个管理者（%s）持有；确需强制交还时加 --force" % cur},
                             ensure_ascii=False))
            return 2
        for k in ("manager", "since", "reported", "checked", "expect", "label", "checkin"):
            rec.pop(k, None)
        box.save_record(rec)
        box.drop_legacy_mark()
    mgr.remove([box.sid])
    print(json.dumps({"thread": box.sid, "released": bool(cur), "previous_owner": cur,
                      "note": "Codex 又可以用 App 弹窗提问了"}, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------- sending

def cfg_from(args):
    return {k: getattr(args, k) for k in DEFAULT_CFG}


def run_codex_queue(codex, sid, text):
    proc = subprocess.run([codex, "queue", "--thread", sid, "--message", text], capture_output=True, text=True)
    lines = [l for l in (proc.stdout + proc.stderr).splitlines() if not l.startswith("WARNING:")]
    out = "\n".join(lines).strip()
    m = re.search(r"Queued message (\S+) for thread", out)
    return proc.returncode, out, m.group(1) if m else None


def start_exec(box, codex, prompt, watch_s=15.0):
    """Run `codex exec resume <sid> <prompt>` detached, in the session's working directory. Returns
    once it is clearly running (or failed). The App holding the session shows up as active_writer."""
    cwd = box.last_cwd()
    if not cwd or not os.path.isdir(cwd):
        return {"ok": False, "error": "不知道这个 session 的工作目录（%s），无法用 exec 运行" % cwd}
    box.ensure()
    logs = sorted(glob.glob(os.path.join(box.root, "exec-*.log")))
    for old in logs[:max(0, len(logs) - KEEP_EXEC_LOGS + 1)]:
        try:
            os.remove(old)
        except OSError:
            pass
    log = os.path.join(box.root, "exec-%s.log" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    t_spawn = time.time()
    with open(log, "wb") as lf:
        p = subprocess.Popen([codex, "exec", "resume", "--json", "--skip-git-repo-check", box.sid, prompt],
                             cwd=cwd, stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT,
                             start_new_session=True)
    running = {"ok": True, "exec_pid": p.pid, "exec_log": tilde(log), "cwd": cwd, "_log": log, "_started": t_spawn}
    while time.time() - t_spawn < watch_s:
        rc = p.poll()
        if rc is not None:
            with open(log, encoding="utf-8", errors="replace") as f:
                tail = f.read()[-4000:]
            if "active writer" in tail:
                return {"ok": False, "active_writer": True, "exec_log": tilde(log)}
            if rc == 0:
                return running
            return {"ok": False, "error": clip(tail[-400:], 400), "exec_log": tilde(log)}
        if any(e.get("ev") == "UserPromptSubmit" and e["t"] > t_spawn for e in box.events()):
            break
        time.sleep(0.5)
    return running


def expect_set(box, xid, **kw):
    """Update one not-yet-started send (None removes a key)."""
    def fn(rec):
        for x in rec["expect"]:
            if x.get("id") == xid:
                for k, v in kw.items():
                    if v is None:
                        x.pop(k, None)
                    else:
                        x[k] = v
    box.edit_record(fn)


def expect_drop(box, xid):
    box.edit_record(lambda rec: rec.update(expect=[x for x in rec["expect"] if x.get("id") != xid]))


def record_exec(box, r):
    """Remember the exec-transport run, so its end can be told (there is no SessionEnd hook)."""
    box.edit_record(lambda rec: rec.update(exec={"pid": r["exec_pid"], "started": r["_started"], "log": r["_log"]}))


def deliver_stopped(box, args, msg, result):
    """Session is stopped: hand the message to the App via codex queue, or run it with exec. The
    send is recorded first, so until its turn starts the session's old stop isn't taken as the answer."""
    sid, text, xid = box.sid, msg["text"], msg["id"]
    box.edit_record(lambda rec: rec.update(expect=(rec["expect"] + [
        {"id": xid, "t": time.time(), "preview": clip(text, 80), "queuing": True}])[-KEEP_EXPECT:]))
    if args.via == "exec":
        r = start_exec(box, args.codex, text)
        result.update({k: v for k, v in r.items() if not k.startswith("_")})
        result.update(mode="exec", transport="exec" if r.get("ok") else None)
        if r.get("ok"):
            record_exec(box, r)
            expect_set(box, xid, queuing=None, transport="exec")
        else:
            expect_drop(box, xid)
            if r.get("active_writer"):
                result["error"] = "Codex App 正打开着这个 session（active writer），请不用 --via exec"
        return r.get("ok", False)
    rc, out, qid = run_codex_queue(args.codex, sid, text)
    result.update({"mode": "queue", "ok": rc == 0, "codex_queue": out})
    if rc != 0:
        expect_drop(box, xid)
        return False
    expect_set(box, xid, queuing=None, qid=qid, transport="app")
    blocked = blocking_popups([e for e in box.events() if not e.get("sub")])
    if blocked:
        result.update(transport="blocked", blocked_by_question=flat_questions(blocked),
                      note="App 里有未处理的提问弹窗：消息已排队，用户回答、跳过或取消弹窗后才会开始")
        return True
    if args.via == "app":
        result["transport"] = "app"
        return True
    deadline = time.time() + args.app_timeout  # auto: does the App take it?
    while time.time() < deadline:
        queue = codex_queue_map()
        if queue is None:
            result.update(transport="app", note="读不到 Codex 队列，假定 App 会处理")
            return True
        if qid not in queue:
            result["transport"] = "app"
            return True
        time.sleep(0.5)
    r = start_exec(box, args.codex, EXEC_NUDGE)
    if r.get("active_writer"):
        result.update(transport="app", note="App 打开着这个 session 但还没取走消息，它空下来就会处理")
    elif r.get("ok"):
        record_exec(box, r)
        expect_set(box, xid, transport="exec")
        result.update(transport="exec", exec_pid=r.get("exec_pid"), exec_log=r.get("exec_log"), cwd=r.get("cwd"))
    else:
        result.update(transport="queued", exec_error=r.get("error"),
                      note="App 没有处理，exec 也没能启动；消息留在 Codex 队列里，App 打开这个 session 时会处理")
    return True


def cmd_send(args):
    sid = parse_thread(args.thread)
    text = args.message if args.message is not None else sys.stdin.read()
    if not text.strip():
        sys.exit("error: empty message")
    cfg = cfg_from(args)
    base = base_dir(args.dir)
    box, mgr = Session(base, sid), Manager(base, manager_id(args.manager))
    ok, own = claim(box, mgr, args.take_over, args.label, args.checkin)
    if not ok:
        print(json.dumps(own, ensure_ascii=False))
        return 2
    msg = {"id": "%d-%s" % (time.time_ns(), uuid.uuid4().hex[:6]), "text": text,
           "from": args.sender, "created_at": now_iso()}
    result = {"thread": sid, "id": msg["id"], "manager": mgr.id}
    result.update(own)

    steered = False
    if not args.queue:
        with box.locked():
            if (box.state().get("status") == "working"
                    and not current(box.record(), box.events(), time.time(), cfg)[0]):
                box.put(msg)
                steered = True
                result.update({"mode": "steer", "ok": True, "turn_id": box.state().get("turn_id")})
    ok = True if steered else deliver_stopped(box, args, msg, result)
    listening = bool(mgr.listener())
    result["listening"] = listening
    if ok and not listening:
        result["next"] = "这个管理者没有 listen 在运行：用 run_in_background 启动 listen，Codex 停下时会收到通知"
    print(json.dumps(result, ensure_ascii=False))
    return 0 if ok else 1


# ---------------------------------------------------------------------------- listening

def scan(base, mgr, cfg, logs, now, default_checkin=DEFAULT_CHECKIN):
    """One pass over the manager's sessions. Returns (owned ids, reports, lost, watching); a working
    session in `watching` carries `_elapsed` (since its manager last heard of it) and `_interval`
    (its check-in interval, 0 = none)."""
    sids = sorted(mgr.data()["sessions"])
    queue_cache = []

    def queue():
        if not queue_cache:
            queue_cache.append(codex_queue_map())
        return queue_cache[0]

    reports, lost, watching = [], [], []
    for sid in sids:
        box = Session(base, sid)
        rec = box.record()
        head = {"thread": sid}
        if rec.get("label"):
            head["label"] = rec["label"]
        owner = rec.get("manager")
        if owner != mgr.id:
            item = dict(head, reason="taken_over" if owner else "released")
            if owner:
                item.update(by=owner, summary="这个 session 已被另一个管理者（%s）接管" % owner)
            else:
                item["summary"] = "这个 session 已被交还，不再由你管理"
            lost.append(item)
            continue
        log = logs.get(sid)
        if log is None:
            log = logs[sid] = EventLog(box.events_path)
        events = log.read()
        a = assess(box, rec, events, now, cfg, queue() if rec["expect"] else None)
        if a["attention"]:
            item = dict(head)
            item.update(a["attention"])
            item["_drop"] = [x.get("id") for x in a["started"]]
            if a["attention"]["reason"] == "not_started":
                item["_drop"] += [x.get("id") for x in a["waiting"]]
            item["_exec_over"] = bool(a["exec_end"])
            reports.append(item)
        elif a["waiting"]:
            watching.append(dict(head, state="waiting_start", since=ts_iso(min(x["t"] for x in a["waiting"])),
                                 _interval=0, _elapsed=0))
        elif not a["stopped"]:
            starts = [e["t"] for e in events if not e.get("sub") and e.get("ev") == "UserPromptSubmit"]
            heard = max(rec.get("checked") or 0, rec.get("reported") or 0, starts[-1] if starts else 0)
            interval = rec.get("checkin")
            watching.append(dict(head, state="working", in_flight=a["info"].get("in_flight"),
                                 silence_s=a["info"].get("silence_s"), _elapsed=now - heard,
                                 _interval=default_checkin if interval is None else interval,
                                 _events=events, _info=a["info"]))
    return sids, reports, lost, watching


def commit(base, mgr, reports, lost, progress=(), at=None):
    """After printing: advance each reported session's cursor to the stop that was reported (later
    stops stay reportable), drop sends that have started (or were reported as not started), and
    restart the check-in clock of the sessions whose progress was reported."""
    for it in progress:
        Session(base, it["thread"]).edit_record(lambda rec: rec.update(checked=at or time.time()))
    for it in reports:
        box = Session(base, it["thread"])
        with box.locked():
            rec = box.record()
            if rec.get("manager") != mgr.id:
                continue
            if it.get("t"):
                rec["reported"] = max(rec.get("reported") or 0, it["t"])
            drop = set(it.get("_drop") or [])
            rec["expect"] = [x for x in rec["expect"] if x.get("id") not in drop]
            if it.get("_exec_over") and rec.get("exec") and not exec_alive(rec["exec"].get("pid")):
                rec["exec"] = None
            box.save_record(rec)
    if lost:
        mgr.remove([l["thread"] for l in lost])


def listen_loop(base, mgr, cfg, timeout=0, settle=2.0, poll=1.0, checkin=DEFAULT_CHECKIN):
    """Returns (output, reports, lost, progress) once something needs the manager."""
    t0 = time.time()
    parent = os.getppid()
    logs, first = {}, None

    def result(reason, reports=(), lost=(), watching=(), progress=(), summary=None):
        out = {"manager": mgr.id, "reason": reason, "stops": [public(r) for r in reports]}
        if progress:
            out["progress"] = [public(p) for p in progress]
        if lost:
            out["lost"] = list(lost)
        out["watching"] = [public(w) for w in watching]
        out["waited_s"] = int(time.time() - t0)
        if summary:
            out["summary"] = summary
        return out

    while True:
        now = time.time()
        owned, reports, lost, working = scan(base, mgr, cfg, logs, now, checkin)
        if not owned:
            return result("nothing_to_watch", summary="这个管理者没有在管理任何 Codex session"), [], [], []
        due = [w for w in working if w["_interval"] and w["_elapsed"] >= w["_interval"]]
        if reports or lost or due:
            first = first or now
            if now - first >= settle:  # let stops (and check-ins) that land together come in one report
                # Waking the manager anyway: bring along every session at least halfway to its check-in.
                ids = {w["thread"] for w in working if w["_interval"] and w["_elapsed"] >= w["_interval"] / 2}
                progress = []
                for w in working:
                    if w["thread"] in ids:
                        item = {k: v for k, v in w.items() if not k.startswith("_") and k not in ("in_flight", "silence_s")}
                        item.update(progress_snapshot(w["thread"], w["_events"], w["_info"], now))
                        progress.append(item)
                rest = [w for w in working if w["thread"] not in ids]
                parts = ["%s：%s" % (r.get("label") or r["thread"][:8], r["reason"]) for r in reports + lost]
                parts += ["%s：仍在工作（本轮 %s）" % (p.get("label") or p["thread"][:8], span(p.get("running_s") or 0))
                          for p in progress]
                reason = "attention" if reports or lost else "checkin"
                return (result(reason, reports, lost, rest, progress, "；".join(parts)), reports, lost, progress)
        else:
            first = None
        if timeout and now - t0 >= timeout:
            return (result("timeout", watching=working, summary="等待 %d 秒超时，没有 session 需要处理" % (now - t0)),
                    [], [], [])
        if os.getppid() != parent:  # whoever started us (Claude Code) is gone
            return result("orphaned", watching=working), [], [], []
        time.sleep(poll)


def cmd_listen(args):
    base = base_dir(args.dir)
    mgr = Manager(base, manager_id(args.manager))
    me = os.getpid()
    with mgr.locked():
        d = mgr.data()
        other = mgr.listener(d)
        if other and other.get("pid") != me:
            print(json.dumps({"manager": mgr.id, "reason": "already_listening", "listener_pid": other["pid"],
                              "since": other.get("since"), "stops": [],
                              "summary": "这个管理者已有 listen 在运行（pid %s），由它通知" % other["pid"]},
                             ensure_ascii=False))
            return 0
        d["listener"] = {"pid": me, "since": now_iso()}
        d["last_active"] = time.time()
        mgr.save(d)
    try:
        out, reports, lost, progress = listen_loop(base, mgr, cfg_from(args), args.timeout, args.settle, args.poll,
                                                   args.checkin)
        at = time.time()
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        commit(base, mgr, reports, lost, progress, at)  # printed first: a crash in between repeats, never loses
    finally:
        with mgr.locked():
            d = mgr.data()
            if (d.get("listener") or {}).get("pid") == me:
                d["listener"] = None
            d["last_active"] = time.time()
            mgr.save(d)
    return 0


# ---------------------------------------------------------------------------- inspection

def owned_session_dirs(base):
    out = []
    for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        if UUID_RE.fullmatch(name) and (read_json(os.path.join(base, name, "session.json"), {}) or {}).get("manager"):
            out.append(name)
    return out


def cmd_list(args):
    base = base_dir(args.dir)
    mgr = Manager(base, manager_id(args.manager))
    cfg = cfg_from(args)
    now = time.time()
    queue = codex_queue_map()
    sids = owned_session_dirs(base) if args.all else sorted(mgr.data()["sessions"])
    rows = []
    for sid in sids:
        box = Session(base, sid)
        rec = box.record()
        a = assess(box, rec, box.events(), now, cfg, queue)
        info = a["info"]
        row = {"thread": sid}
        if rec.get("label"):
            row["label"] = rec["label"]
        if rec.get("manager") != mgr.id:
            row["owner"] = rec.get("manager")
        if a["waiting"]:
            row["state"] = "waiting_start"
        elif not a["stopped"]:
            row.update(state="working", in_flight=info.get("in_flight"), silence_s=info.get("silence_s"))
        elif info["reason"] == "unknown":
            row["state"] = "unknown"
        else:
            row.update(state="stopped", reason=info["reason"], at=info.get("at"))
        row["unreported"] = bool(a["attention"])
        if a["waiting"]:
            row["pending_sends"] = len(a["waiting"])
        if info.get("last_message"):
            row["last_message"] = clip(info["last_message"][-400:], 160)
        rows.append(row)
    print(json.dumps({"manager": mgr.id, "listening": bool(mgr.listener()), "sessions": rows},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_status(args):
    sid = parse_thread(args.thread)
    base = base_dir(args.dir)
    box = Session(base, sid)
    me = manager_id(args.manager)
    now = time.time()
    rec = box.record()
    evs = box.events()
    queue = codex_queue_map()
    a = assess(box, rec, evs, now, cfg_from(args), queue)
    info = dict(a["info"], stopped=a["stopped"])
    info.pop("t", None)
    if info.get("last_message"):
        info["last_message"] = clip(info["last_message"][-600:], 300)
    owner = rec.get("manager")
    om = Manager(base, owner) if owner else None
    od = om.data() if om else {}
    try:
        done = sorted(n for n in os.listdir(box.delivered) if n.endswith(".json"))[-5:]
    except OSError:
        done = []
    main = [e for e in evs if not e.get("sub")]
    ex = rec.get("exec")
    print(json.dumps({
        "thread": sid,
        "label": rec.get("label"),
        "owner": {"manager": owner, "is_me": owner == me, "active": om.active(now, od), "since": rec.get("since"),
                  "listening": bool(om.listener(od))} if owner else None,
        "managed": bool(om and om.active(now, od)),
        "codex": info,
        "progress": None if a["stopped"] or a["waiting"] else progress_snapshot(sid, evs, a["info"], now),
        "unreported": public(a["attention"]) if a["attention"] and owner else None,
        "reported_upto": ts_iso(rec.get("reported")),
        "checkin_s": rec.get("checkin"),
        "last_checkin": ts_iso(rec.get("checked")),
        "pending_sends": [{"id": x.get("id"), "preview": x.get("preview"), "transport": x.get("transport"),
                           "sent_at": ts_iso(x.get("t"))} for x in a["waiting"]],
        "open_questions": flat_questions(open_questions(main)),
        "blocked_by_question": bool(blocking_popups(main)),
        "exec": dict(ex, alive=pid_alive(ex.get("pid")), log=tilde(ex.get("log"))) if ex else None,
        "codex_queue": queued_count(queue, sid),
        "inbox": [{"id": m["id"], "preview": clip(m.get("text", ""), 80)} for m in box.inbox_messages()],
        "recent_delivered": [{k: (box.receipt(n[:-5]) or {}).get(k) for k in ("id", "delivered_via", "delivered_at")}
                             for n in done],
        "state": box.state() or None,
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_gc(args):
    base = base_dir(args.dir)
    cutoff = time.time() - args.days * 86400
    removed, legacy, managers = 0, 0, 0
    for name in os.listdir(base) if os.path.isdir(base) else []:
        if not UUID_RE.fullmatch(name):
            continue
        box = Session(base, name)
        for old in LEGACY_FILES:  # state files of the previous version, once they can't matter
            path = os.path.join(box.root, old)
            try:
                if os.path.getmtime(path) < time.time() - MANAGED_TTL:
                    os.remove(path)
                    legacy += 1
            except OSError:
                pass
        if box.pending_names() or box.is_managed():
            continue
        mtimes = []
        for p in (box.state_path, box.events_path, box.rec_path, box.root):
            try:
                mtimes.append(os.path.getmtime(p))
            except OSError:
                pass
        if mtimes and max(mtimes) < cutoff:
            shutil.rmtree(box.root, ignore_errors=True)
            removed += 1
    for path in glob.glob(os.path.join(base, MANAGERS, "*.json")):
        m = Manager(base, os.path.basename(path)[:-5])
        gone = False
        with m.locked():
            d = m.data()
            d["sessions"] = {s: v for s, v in d["sessions"].items() if os.path.isdir(os.path.join(base, s))}
            if not d["sessions"] and not m.listener(d) and d.get("last_active", 0) < cutoff:
                os.remove(m.path)
                gone = True
            else:
                m.save(d)
        if gone:
            managers += 1
            try:
                os.remove(m.lock_path)
            except OSError:
                pass
    print(json.dumps({"removed": removed, "managers_removed": managers, "legacy_files_removed": legacy}))
    return 0


# ---------------------------------------------------------------------------- install / doctor

def is_our_hook(h):
    cmd = h.get("command") or ""
    return any(s in cmd for s in OUR_SCRIPTS)


def strip_our_hooks(hooks):
    for ev in list(hooks):
        groups = []
        for g in hooks[ev] or []:
            hs = [h for h in g.get("hooks") or [] if not is_our_hook(h)]
            if hs:
                groups.append(dict(g, hooks=hs))
            elif not g.get("hooks"):
                groups.append(g)
        if groups:
            hooks[ev] = groups
        else:
            del hooks[ev]


def add_our_hooks(hooks, target):
    for ev, matcher, timeout, is_async, sub in HOOK_SPECS:
        h = {"type": "command", "command": hook_command_for(target, sub), "timeout": timeout}
        if is_async:
            h["async"] = True
        g = {"matcher": matcher, "hooks": [h]} if matcher else {"hooks": [h]}
        hooks.setdefault(ev, []).append(g)


def hook_python():
    """Interpreter written into hooks.json. Codex may run hooks with a minimal PATH, so it is an absolute
    path: $CODEX_INTERCOM_PYTHON, else the system /usr/bin/python3, else the interpreter running this."""
    p = os.environ.get("CODEX_INTERCOM_PYTHON")
    if p:
        return p
    return SYSTEM_PYTHON if os.access(SYSTEM_PYTHON, os.X_OK) else sys.executable


def hook_command_for(target, sub="hook"):
    script = HOOK_SCRIPT if os.path.abspath(target) == INSTALL_PATH else shlex.quote(target)
    return "%s %s %s" % (shlex.quote(hook_python()), script, sub)


def write_hooks_json(path, data, dry_run):
    backup = None
    if not dry_run:
        if os.path.exists(path):
            backup = "%s.bak-%s" % (path, datetime.now().strftime("%Y%m%d-%H%M%S"))
            shutil.copy2(path, backup)
        write_json_atomic(path, data, indent=2)
    return backup


def cmd_install(args):
    me = os.path.abspath(__file__)
    target = os.path.abspath(args.target)
    copied = False
    if os.path.realpath(target) != os.path.realpath(me) and (not os.path.exists(target) or file_sha(target) != file_sha(me)):
        if not args.dry_run:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            tmp = target + ".tmp"
            shutil.copyfile(me, tmp)
            os.chmod(tmp, 0o755)
            os.replace(tmp, target)
        copied = True
    data = read_json(args.hooks_json, None)
    if data is None and os.path.exists(args.hooks_json):
        sys.exit("error: %s is not valid JSON; fix it first" % args.hooks_json)
    data = data or {"hooks": {}}
    before = json.dumps(data, sort_keys=True)
    hooks = data.setdefault("hooks", {})
    strip_our_hooks(hooks)
    add_our_hooks(hooks, target)
    desc = data.get("description")
    if isinstance(desc, str) and "codex_mailbox" in desc:
        data["description"] = re.sub(r"codex_mailbox[^；;。]*", "codex_intercom：Claude Code 与 Codex 的通信（插话投递、停止通知）", desc)
    changed = json.dumps(data, sort_keys=True) != before
    backup = write_hooks_json(args.hooks_json, data, args.dry_run) if changed else None
    print(json.dumps({
        "dry_run": args.dry_run, "script": tilde(target), "script_updated": copied,
        "hooks_json": tilde(args.hooks_json), "hooks_changed": changed, "backup": tilde(backup) if backup else None,
        "next": ("在 Codex 里信任 codex_intercom 的 hooks：Codex App 在 Settings > Hooks；Codex CLI 启动时会提示 "
                 "Hooks need review（也可以用 /hooks）。新增或命令变化的条目需要重新信任"
                 if changed else "hooks 未变化，无需重新信任"),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_uninstall(args):
    data = read_json(args.hooks_json, None)
    changed, backup = False, None
    if data:
        before = json.dumps(data, sort_keys=True)
        strip_our_hooks(data.setdefault("hooks", {}))
        changed = json.dumps(data, sort_keys=True) != before
        if changed:
            backup = write_hooks_json(args.hooks_json, data, args.dry_run)
    removed = False
    if args.remove_script and os.path.exists(args.target) and not args.dry_run:
        os.remove(args.target)
        removed = True
    print(json.dumps({"dry_run": args.dry_run, "hooks_changed": changed, "backup": tilde(backup) if backup else None,
                      "script_removed": removed,
                      "note": "数据目录未动：%s" % tilde(base_dir(args.dir))}, ensure_ascii=False, indent=2))
    return 0


def tail_lines(path, n):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200 * n))
            return f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return []


def cmd_doctor(args):
    checks = []

    def add(level, name, detail):
        checks.append({"level": level, "check": name, "detail": detail})

    me = os.path.abspath(__file__)
    target = os.path.abspath(args.target)
    if not os.path.exists(target):
        add("error", "script", "未安装：%s 不存在，运行 install" % tilde(target))
    elif os.path.realpath(target) != os.path.realpath(me) and file_sha(target) != file_sha(me):
        add("warn", "script", "%s 与当前版本不同，运行 install 更新" % tilde(target))
    else:
        add("ok", "script", tilde(target))

    data = read_json(args.hooks_json, None)
    if data is None:
        add("error", "hooks.json", "%s 不存在或不是合法 JSON" % tilde(args.hooks_json))
    else:
        hooks = data.get("hooks") or {}
        commands = {hook_command_for(target, s[4]) for s in HOOK_SPECS}
        missing, wrong = [], []
        for ev, matcher, timeout, is_async, sub in HOOK_SPECS:
            command = hook_command_for(target, sub)
            label = ev if sub == "hook" else "%s(%s)" % (ev, sub)
            ours = [(g, h) for g in hooks.get(ev) or [] for h in g.get("hooks") or [] if h.get("command") == command]
            if not ours:
                missing.append(label)
            elif not any(bool(h.get("async")) == is_async and (g.get("matcher") or None) == matcher for g, h in ours):
                wrong.append(label)
        legacy = sorted({ev for ev, gs in hooks.items() for g in gs or [] for h in g.get("hooks") or []
                         if is_our_hook(h) and h.get("command") not in commands})
        if missing:
            add("error", "hooks", "hooks.json 缺少：%s（运行 install）" % ", ".join(missing))
        if wrong:
            add("warn", "hooks", "这些事件的 async/matcher 与预期不同：%s（运行 install）" % ", ".join(wrong))
        if legacy:
            add("warn", "hooks", "还有旧版或其他路径的条目：%s（运行 install 会清理）" % ", ".join(legacy))
        if not (missing or wrong or legacy):
            add("ok", "hooks", "%d 个事件已注册" % len(HOOK_SPECS))

    codex = shutil.which("codex")
    if not codex:
        add("error", "codex", "找不到 codex 命令")
    else:
        try:
            v = subprocess.run([codex, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
        except (OSError, subprocess.SubprocessError) as e:
            v = "无法获取版本：%s" % e
        add("ok", "codex", "%s (%s)" % (tilde(codex), v.splitlines()[-1] if v else "?"))

    base = base_dir(args.dir)
    if os.path.isdir(base) and not os.access(base, os.W_OK):
        add("error", "data dir", "%s 不可写。在 Claude Code 沙箱里运行时就是这样：send / listen 要关闭沙箱运行"
            % tilde(base))
    q = queue_db_path()
    add("ok" if q else "warn", "codex queue store", tilde(q) if q else "找不到 Codex 队列库，无法判断 App 是否取走消息")
    mid, src = manager_id(args.manager), manager_source(args.manager)
    if src == "default":
        add("warn", "manager", "没有 $CLAUDE_CODE_SESSION_ID（不在 Claude Code 里运行？），管理者记为 %r" % mid)
    else:
        add("ok", "manager", "%s（来自 %s）" % (mid, src))

    # Trust can't be read reliably (the hash format is private), so look at what actually arrived.
    seen = {}
    cutoff = time.time() - args.days * 86400
    for path in glob.glob(os.path.join(base, "*", "events.jsonl")):
        try:
            if os.path.getmtime(path) < cutoff:
                continue
        except OSError:
            continue
        for line in tail_lines(path, 400):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                seen[e.get("ev")] = max(seen.get(e.get("ev"), 0), e.get("t", 0))
    never = sorted({s[0] for s in HOOK_SPECS if s[0] not in seen and s[0] not in RARE_EVENTS})
    if never:
        add("warn", "events", "近 %d 天没收到这些事件：%s。如果期间用过 Codex，多半是这些 hooks 还没被信任"
                              "（Codex App：Settings > Hooks；Codex CLI：/hooks）" % (args.days, ", ".join(never)))
    else:
        add("ok", "events", "近 %d 天各类常见事件都收到过" % args.days)
    report = {"ok": not any(c["level"] == "error" for c in checks), "checks": checks,
              "last_seen": {ev: ts_iso(t) for ev, t in sorted(seen.items(), key=lambda kv: str(kv[0]))}}
    if args.thread:
        sid = parse_thread(args.thread)
        box = Session(base, sid)
        evs = box.events()
        a = assess(box, box.record(), evs, time.time(), DEFAULT_CFG, codex_queue_map())
        info = dict(a["info"], stopped=a["stopped"])
        info.pop("last_message", None)
        info.pop("t", None)
        report["thread"] = {"id": sid, "events": len(evs), "last_event_at": ts_iso(evs[-1]["t"]) if evs else None,
                            "codex": info, "cwd": box.last_cwd(), "owner": box.record().get("manager")}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


# ---------------------------------------------------------------------------- CLI

REPLACED = {
    "wait": "wait 已由 listen 取代：每个管理者只跑一个 listen（run_in_background），覆盖它管理的所有 session；"
            "send 照旧。详见 SKILL.md。",
    "manage": "manage 已由 claim 取代（交还用 release）。详见 SKILL.md。",
}


def cmd_removed(args):
    print(json.dumps({"ok": False, "error": "%s_removed" % args.old, "summary": REPLACED[args.old]}, ensure_ascii=False))
    return 2


def add_cfg_args(s):
    s.add_argument("--error-grace", dest="error_grace", type=float, default=DEFAULT_CFG["error_grace"],
                   help="quiet seconds after a failed model request before reporting `error`")
    s.add_argument("--approval-grace", dest="approval_grace", type=float, default=DEFAULT_CFG["approval_grace"],
                   help="quiet seconds after PermissionRequest before reporting `approval`")
    s.add_argument("--stall", type=float, default=DEFAULT_CFG["stall"],
                   help="quiet seconds (nothing in flight) before reporting `stalled`")
    s.add_argument("--model-stall", dest="model_stall", type=float, default=DEFAULT_CFG["model_stall"],
                   help="quiet seconds while a model request is in flight before `stalled`")
    s.add_argument("--long-stall", dest="long_stall", type=float, default=DEFAULT_CFG["long_stall"],
                   help="quiet seconds while a tool / compaction runs before `stalled`")
    s.add_argument("--start-timeout", dest="start_timeout", type=float, default=DEFAULT_CFG["start_timeout"],
                   help="seconds after a send without its turn starting before `not_started`")


def add_install_args(s):
    s.add_argument("--target", default=INSTALL_PATH, help="where the hook script lives (default ~/.codex/hooks/)")
    s.add_argument("--hooks-json", dest="hooks_json", default=HOOKS_JSON, help="Codex hooks.json to edit")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dir", help="data dir (default: $CODEX_INTERCOM_DIR or ~/.codex/intercom)")
    p.add_argument("--manager", help="manager id (default: $CODEX_INTERCOM_MANAGER, else $CLAUDE_CODE_SESSION_ID, "
                                     "else 'cli')")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="send a message and claim the session (steer if working, else queue / exec)")
    s.add_argument("--thread", required=True, help="session UUID or codex://threads/<uuid>")
    s.add_argument("--message", help="message text (default: read stdin)")
    s.add_argument("--from", dest="sender", default=None, help="optional sender label shown to the model")
    s.add_argument("--label", help="short name for this session, shown by listen / list")
    s.add_argument("--take-over", dest="take_over", action="store_true",
                   help="take the session from another active manager")
    s.add_argument("--checkin", type=parse_duration, default=None,
                   help="progress check-in interval for this session while it works (10m, 1h, off); "
                        "default: listen's --checkin")
    s.add_argument("--queue", action="store_true", help="never steer; always start a new turn")
    s.add_argument("--via", choices=("auto", "app", "exec"), default="auto",
                   help="new-turn transport: auto = codex queue, then exec if the App doesn't take it; "
                        "app = codex queue only; exec = codex exec resume only")
    s.add_argument("--app-timeout", dest="app_timeout", type=float, default=20.0,
                   help="(auto) seconds to wait for the App to take the queued message before using exec")
    s.add_argument("--codex", default=shutil.which("codex") or "codex", help="codex binary")
    add_cfg_args(s)
    s.set_defaults(fn=cmd_send)

    s = sub.add_parser("listen", help="block until an owned session needs attention; print one JSON line")
    s.add_argument("--timeout", type=float, default=0, help="give up after N seconds (0 = no limit)")
    s.add_argument("--settle", type=float, default=2.0,
                   help="seconds to wait after the first stop so stops landing together come in one report")
    s.add_argument("--poll", type=float, default=1.0, help=argparse.SUPPRESS)
    s.add_argument("--checkin", type=parse_duration, default=DEFAULT_CHECKIN,
                   help="report the progress of a session still working after this long without news "
                        "(default 30m; a session's own send/claim --checkin wins; off = never)")
    add_cfg_args(s)
    s.set_defaults(fn=cmd_listen)

    s = sub.add_parser("list", help="this manager's sessions and their state")
    s.add_argument("--all", action="store_true", help="every owned session, whoever owns it")
    add_cfg_args(s)
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("status", help="one session in detail")
    s.add_argument("--thread", required=True)
    add_cfg_args(s)
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("claim", help="own a session without sending (listen watches it; no App popups)")
    s.add_argument("--thread", required=True)
    s.add_argument("--label")
    s.add_argument("--checkin", type=parse_duration, default=None, help="as for send")
    s.add_argument("--take-over", dest="take_over", action="store_true")
    s.set_defaults(fn=cmd_claim)

    s = sub.add_parser("release", help="give a session back: not watched, App popups allowed again")
    s.add_argument("--thread", required=True)
    s.add_argument("--force", action="store_true", help="release even if another manager owns it")
    s.set_defaults(fn=cmd_release)

    s = sub.add_parser("doctor", help="check installation and recent hook activity")
    s.add_argument("--thread", help="also show this session's state")
    s.add_argument("--days", type=float, default=14, help="look-back window for event activity")
    add_install_args(s)
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("install", help="copy the script to ~/.codex/hooks and register hooks (backs up hooks.json)")
    s.add_argument("--dry-run", dest="dry_run", action="store_true")
    add_install_args(s)
    s.set_defaults(fn=cmd_install)

    s = sub.add_parser("uninstall", help="remove the hooks from hooks.json (backs it up first)")
    s.add_argument("--dry-run", dest="dry_run", action="store_true")
    s.add_argument("--remove-script", dest="remove_script", action="store_true", help="also delete the hook script")
    add_install_args(s)
    s.set_defaults(fn=cmd_uninstall)

    s = sub.add_parser("gate", help="Codex PreToolUse gate for popup questions (stdin JSON)")
    s.set_defaults(fn=cmd_gate)

    s = sub.add_parser("gc", help="remove unowned session dirs untouched for N days, idle managers, legacy files")
    s.add_argument("--days", type=float, default=7)
    s.set_defaults(fn=cmd_gc)

    s = sub.add_parser("hook", help="Codex hook entry point (stdin JSON)")
    s.set_defaults(fn=cmd_hook)

    for old in ("wait", "manage"):  # commands of the previous version: say what replaced them
        s = sub.add_parser(old, help=argparse.SUPPRESS)
        s.set_defaults(fn=cmd_removed, old=old)

    args, extra = p.parse_known_args(argv)
    if extra and args.fn is not cmd_removed:
        p.error("unrecognized arguments: %s" % " ".join(extra))
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
