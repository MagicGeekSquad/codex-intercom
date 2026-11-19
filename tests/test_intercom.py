#!/usr/bin/env python3
"""Tests for codex_intercom.py without a real Codex: stop analysis and attention on synthetic logs,
hooks/steering, ownership, listen (multi-session, latest-only, exactly-once, sends not started),
the exec transport (fake codex + fake queue store), popups/gate and install/uninstall.

usage: python3 tests/test_intercom.py [path/to/codex_intercom.py]
       (default: skills/codex-intercom/scripts/codex_intercom.py in this repo)
"""
import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
MB = (os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else
      os.path.join(HERE, os.pardir, "skills", "codex-intercom", "scripts", "codex_intercom.py"))
spec = importlib.util.spec_from_file_location("codex_intercom", MB)
cm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cm)

FAKE = os.path.join(HERE, "fake_codex.py")
TMP = tempfile.mkdtemp(prefix="intercom-test-", dir=os.environ.get("TMPDIR"))
FAKE_BIN = os.path.join(TMP, "bin")
os.mkdir(FAKE_BIN)
os.symlink(FAKE, os.path.join(FAKE_BIN, "codex"))  # doctor must never probe the installed Codex
BOX = os.path.join(TMP, "data")
WORKDIR = os.path.realpath(os.path.join(TMP, "work"))
os.makedirs(WORKDIR)
SESS = os.path.join(TMP, "sessions")
os.environ.update({"FAKE_DIR": TMP, "CODEX_QUEUE_DB": os.path.join(TMP, "queue.sqlite"), "FAKE_MB": MB,
                   "FAKE_BOX": BOX, "CODEX_SESSIONS_DIR": SESS,
                   "PATH": FAKE_BIN + os.pathsep + os.environ.get("PATH", "")})
os.environ.pop("CODEX_INTERCOM_MANAGER", None)
os.environ.pop("CODEX_INTERCOM_DIR", None)
FAILS = []
FAST = ("--settle", "0.6", "--poll", "0.2", "--timeout", "25")  # safety net: a test may not hang


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label, flush=True)
    if not cond:
        FAILS.append(label)


def set_mode(m):
    with open(os.path.join(TMP, "mode"), "w") as f:
        f.write(m)


def E(t, ev, **kw):
    d = {"t": t, "ev": ev, "turn": kw.pop("turn", "T1")}
    d.update(kw)
    return d


def an(events, now):
    return cm.analyze(sorted(events, key=lambda e: e["t"]), now)


def hook(sid, event, turn="t1", **extra):
    data = {"session_id": sid, "hook_event_name": event, "turn_id": turn, "cwd": WORKDIR,
            "transcript_path": "/x/rollout-2026-11-13T00-00-00-%s.jsonl" % sid}
    data.update(extra)
    p = subprocess.run([sys.executable, MB, "--dir", BOX, "hook"], input=json.dumps(data), capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout) if p.stdout.strip() else None


def cli(mgr, *a, stdin=None, data_dir=BOX):
    p = subprocess.run([sys.executable, MB, "--dir", data_dir, "--manager", mgr] + list(a),
                       input=stdin, capture_output=True, text=True)
    assert p.stdout.strip(), "no output: rc=%s %s" % (p.returncode, p.stderr)
    out = json.loads(p.stdout)
    if isinstance(out, dict):
        out["_rc"] = p.returncode
    return out


def local_cli(mgr, *args):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        rc = cm.main(["--dir", BOX, "--manager", mgr, *args])
    return dict(json.loads(output.getvalue()), _rc=rc)


def send(mgr, sid, text, *extra):
    return cli(mgr, "send", "--thread", "codex://threads/" + sid, "--message", text, "--codex", FAKE,
               "--app-timeout", "2", *extra)


def listen(mgr, *extra):
    return cli(mgr, "listen", *(FAST + extra))


def bg(fn, *a):
    res = {}
    th = threading.Thread(target=lambda: res.update(fn(*a)))
    th.start()
    return th, res


def queued(sid):
    con = sqlite3.connect(os.environ["CODEX_QUEUE_DB"])
    try:
        return con.execute("select count(*) from queued_items where thread_id=?", (sid,)).fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        con.close()


def app_take(sid):
    """Simulate the App taking this session's queued messages."""
    con = sqlite3.connect(os.environ["CODEX_QUEUE_DB"])
    try:
        con.execute("delete from queued_items where thread_id=?", (sid,))
        con.commit()
    except sqlite3.Error:
        pass
    finally:
        con.close()


def app_consumes(sid, delay=0.5, until=1.5):
    def run():
        t0 = time.time()
        while time.time() - t0 < until:
            time.sleep(delay)
            app_take(sid)
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th


def sid_n(n):
    return "00000000-0000-7000-8000-%012d" % n


def inbox(sid):
    return sorted(os.listdir(os.path.join(BOX, sid, "inbox")))


def state(sid):
    return json.load(open(os.path.join(BOX, sid, "state.json")))


def record(sid):
    return json.load(open(os.path.join(BOX, sid, "session.json")))


def last_resume_args():
    with open(os.path.join(TMP, "exec_calls.jsonl")) as f:
        return json.loads(f.readlines()[-1])["args"]


def age_manager(mid):
    p = os.path.join(BOX, "_managers", mid + ".json")
    d = json.load(open(p))
    d["last_active"] = time.time() - cm.MANAGED_TTL - 10
    d["listener"] = None
    json.dump(d, open(p, "w"))


def turn(sid, name, msg):
    hook(sid, "UserPromptSubmit", turn=name, prompt=name)
    hook(sid, "Stop", turn=name, last_assistant_message=msg)


def by_thread(out, key="stops"):
    return {s["thread"]: s for s in out.get(key, [])}


def append_rollout(sid, *rows):
    d = os.path.join(SESS, "2026", "11", "13")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "rollout-2026-11-13T00-00-00-%s.jsonl" % sid), "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def rollout_event(kind, **payload):
    return {"type": "event_msg", "payload": dict(payload, type=kind)}


def fake_rollout(sid, turn, items, model=None, effort=None):
    """Append the start and visible activity of a Codex-style rollout turn."""
    append_rollout(sid, rollout_event("task_started", turn_id=turn))
    if model or effort:
        append_rollout(sid, {"type": "turn_context", "payload": {"turn_id": turn, "model": model, "effort": effort}})
    append_rollout(sid, *(rollout_event("item_completed", thread_id=sid, turn_id=turn, item=it) for it in items))


def token_count(unit):
    fields = {"input_tokens": 10, "cached_input_tokens": 5, "output_tokens": 3,
              "reasoning_output_tokens": 2, "total_tokens": 13}
    return rollout_event("token_count", info={"total_token_usage": {k: v * unit for k, v in fields.items()},
                                              "last_token_usage": fields}, rate_limits=None)


def say(text):
    return {"type": "AgentMessage", "content": [{"type": "Text", "text": text}], "phase": "commentary"}


def ran(cmd, code=0):
    return {"type": "CommandExecution", "command": ["/bin/zsh", "-lc", cmd], "status": "completed", "exit_code": code}


# ================================================================== A. analyze() on synthetic logs
s, i = an([], 100)
check(s and i["reason"] == "unknown" and i["t"] is None, "A no events -> unknown")
base = [E(0, "UserPromptSubmit"), E(0.1, "PreModelRequest"), E(3, "PostModelRequest", status="completed"),
        E(3.1, "PreToolUse", tool="Bash", tool_use_id="a"), E(5, "PostToolUse", tool="Bash", tool_use_id="a"),
        E(5.1, "PreModelRequest")]
s, i = an(base, 10)
check(not s and i["in_flight"] == "model", "A mid-turn, model in flight -> working")
done = base + [E(8, "PostModelRequest", status="completed"), E(8.1, "Stop", msg="全部完成。要继续吗？")]
s, i = an(done, 9)
check(s and i["reason"] == "done" and i["t"] == 8.1 and i["last_message"].endswith("要继续吗？"),
      "A Stop -> done, t = the Stop, with last message")
cont = base + [E(8, "PostModelRequest", status="completed"), E(8.1, "Stop", continued=True), E(8.2, "PreModelRequest")]
check(not an(cont, 9)[0], "A Stop turned into a continuation is not a stop")
ask = base + [E(8.1, "PreToolUse", tool="request_user_input", tool_use_id="q1",
                questions=[{"question": "选哪个？", "options": ["红", "蓝"]}])]
s, i = an(ask, 8.5)
check(s and i["reason"] == "asking" and i["questions"][0]["options"] == ["红", "蓝"], "A unanswered question -> asking")
check(not an(base + [E(8.1, "PreToolUse", tool="request_user_input", tool_use_id="q1", denied=True)], 8.5)[0],
      "A question denied by the gate is not asking")
s, i = an(base + [E(9, "PostToolUse", tool="request_user_input", tool_use_id="q2"),
                  E(9.05, "PreToolUse", tool="request_user_input", tool_use_id="q2")], 10)
check(not s, "A out-of-order Pre/Post for a question counts as answered")
pop = base + [E(6, "PreToolUse", tool="request_user_input_async", tool_use_id="call_p1",
                questions=[{"question": "要删除旧副本吗？", "options": ["是", "否"]}]),
              E(6.01, "PostToolUse", tool="request_user_input_async", tool_use_id="call_p1"),
              E(6.1, "PreModelRequest")]
check(not an(pop, 7)[0], "A popup asked, Codex keeps working -> not a stop")
pop_stop = pop + [E(9, "PostModelRequest", status="completed"), E(9.1, "Stop", msg="先做别的，等你回复。")]
s, i = an(pop_stop, 10)
check(s and i["reason"] == "asking" and i["questions"][0]["question"] == "要删除旧副本吗？"
      and i["last_message"].startswith("先做别的"), "A turn ends with the popup open -> asking (+ last message)")
s, i = an(pop + [E(8, "UserPromptSubmit", replies=["call_p1"]), E(9, "PostModelRequest", status="completed"),
                 E(9.1, "Stop", msg="done")], 10)
check(s and i["reason"] == "done", "A popup answered (reply names the call id) before the stop -> done")
s, i = an(pop_stop + [E(60, "UserPromptSubmit", turn="T2"), E(70, "Stop", turn="T2", msg="next")], 71)
check(s and i["reason"] == "done" and i["t"] == 70, "A a later turn started (popup skipped/cancelled) -> its own stop")
err = base + [E(6, "PostModelRequest", status="failed", error="Selected model is at capacity.")]
check(not an(err, 10)[0], "A failed request, quiet < grace -> not yet")
s, i = an(err, 22)
check(s and i["reason"] == "error" and i["failures"] == 1 and i["t"] == 6, "A failed request + 16s quiet -> error")
check(an(err + [E(5.9, "PreModelRequest")], 30)[1]["reason"] != "error", "A retry in flight is not an error")
retry = [E(0, "UserPromptSubmit"), E(1, "PreModelRequest"), E(1.2, "PostModelRequest", status="failed", error="x"),
         E(6, "PreModelRequest"), E(6.2, "PostModelRequest", status="failed", error="x"),
         E(16, "PreModelRequest"), E(16.2, "PostModelRequest", status="failed", error="x")]
s, i = an(retry, 33)
check(s and i["reason"] == "error" and i["failures"] == 3 and i["t"] == 1.2, "A repeated failures -> error (3), t = first")
s2, i2 = an(retry + [E(40, "PreModelRequest"), E(40.2, "PostModelRequest", status="failed", error="x")], 70)
check(s2 and i2["t"] == 1.2 and i2["failures"] == 4, "A more retries failing: still the same stop (same t)")
check(an(retry + [E(17, "SessionEnd", turn=None)], 18)[1]["reason"] == "error", "A SessionEnd right after failure -> error")
perm = base + [E(6, "PostModelRequest", status="completed"), E(6.1, "PreToolUse", tool="Bash", tool_use_id="b"),
               E(6.2, "PermissionRequest", tool="Bash", detail="curl example.com")]
check(not an(perm, 20)[0], "A permission request, quiet < grace -> not yet")
s, i = an(perm, 40)
check(s and i["reason"] == "approval" and "curl" in i["detail"] and i["t"] == 6.2, "A permission request + 34s -> approval")
check(an(base + [E(6, "Interrupt")], 7)[1]["reason"] == "interrupted", "A Interrupt -> interrupted")
check(an(base + [E(6, "SessionEnd", turn=None)], 7)[1]["reason"] == "ended", "A SessionEnd -> ended")
between = base + [E(6, "PostModelRequest", status="completed")]
started = [E(0, "UserPromptSubmit")]
s, i = an(started, 301)
check(not an(started, 299)[0] and i["reason"] == "stalled" and i["t"] == 0, "A nothing after the prompt: stalled after 300s")
check(not an(between, 1006)[0] and an(between, 1808)[1]["in_flight"] == "tool",
      "A response done, no Stop -> tools running (inferred): stalled only after 1800s")
check(not an(base[:-1], 1000)[0], "A right after a tool result: long threshold too")
check(not an(between + [E(380, "PostModelRequest", sub="explorer", status="completed")], 406)[0],
      "A subagent activity keeps the session alive")
three = [E(1, "UserPromptSubmit", turn="a"), E(2, "Stop", turn="a"), E(3, "UserPromptSubmit", turn="b"),
         E(4, "Stop", turn="b", continued=True), E(5, "Interrupt", turn="b"), E(6, "UserPromptSubmit", turn="c"),
         E(7, "Stop", turn="c")]
check(cm.stops_between(three, 0, 7) == 2 and cm.stops_between(three, 2, 7) == 1, "A stops_between counts real stops only")

# ================================================================== A2. assess(): what listen would report
ABOX = cm.Session(os.path.join(TMP, "abox"), sid_n(99))


def att(events, rec, now, queue=None, **cfg):
    c = dict(cm.DEFAULT_CFG, **cfg)
    return cm.assess(ABOX, dict({"expect": []}, **rec), lambda: sorted(events, key=lambda e: e["t"]), now, c, queue)


turns3 = [E(10, "UserPromptSubmit", turn="a"), E(11, "Stop", turn="a", msg="one"),
          E(20, "UserPromptSubmit", turn="b"), E(21, "Stop", turn="b", msg="two"),
          E(30, "UserPromptSubmit", turn="c"), E(31, "Stop", turn="c", msg="three")]
a = att(turns3, {"reported": 5}, 40)["attention"]
check(a and a["last_message"] == "three" and a["earlier_stops"] == 2, "A2 three unreported stops -> latest + earlier_stops 2")
check(att(turns3, {"reported": 31}, 40)["attention"] is None, "A2 latest stop already reported -> nothing")
a = att(turns3, {"reported": 21}, 40)["attention"]
check(a and "earlier_stops" not in a, "A2 one new stop -> no earlier_stops")
x = {"id": "m1", "t": 35, "qid": "q1"}
r = att(turns3, {"reported": 21, "expect": [x]}, 40, queue={"q1": sid_n(99)})
check(r["attention"] is None and r["waiting"], "A2 our send not started yet -> the old stop is not the answer")
r = att(turns3, {"reported": 21, "expect": [x]}, 336, queue={"q1": sid_n(99)})
check(r["attention"] and r["attention"]["reason"] == "not_started" and r["attention"]["codex_queue"] == 1
      and r["attention"]["session_state"]["reason"] == "done", "A2 not started within 300s -> not_started (+ state)")
other = turns3 + [E(36, "UserPromptSubmit", turn="d"), E(37, "Stop", turn="d", msg="someone else's turn")]
r = att(other, {"reported": 21, "expect": [x]}, 40, queue={"q1": sid_n(99)})
check(r["attention"] is None, "A2 another turn ran while ours is still queued -> keep waiting")
ours = other + [E(38, "UserPromptSubmit", turn="e"), E(39, "Stop", turn="e", msg="ours")]
r = att(ours, {"reported": 21, "expect": [x]}, 40, queue={})
check(r["attention"]["last_message"] == "ours" and r["attention"]["earlier_stops"] == 2 and r["started"],
      "A2 ours taken + started -> its stop, earlier ones counted")
r = att(ours, {"reported": 21, "expect": [dict(x, queuing=True)]}, 40, queue={})
check(r["attention"] is None, "A2 send still being queued counts as not started")
fresh = {"id": "m2", "t": 330, "qid": "q2"}
r = att(turns3, {"reported": 21, "expect": [x, fresh]}, 336, queue=dict.fromkeys(("q1", "q2"), sid_n(99)))
check([q["id"] for q in r["attention"]["queued"]] == ["m1"], "A2 each pending send has its own start deadline")
dead = subprocess.Popen(["true"])
dead.wait()
ex = {"pid": dead.pid, "started": 10, "log": os.path.join(TMP, "no-such.log")}
mid = [E(9.6, "UserPromptSubmit", turn="setup"), E(9.7, "Stop", turn="setup"),  # a turn just before the spawn
       E(11, "UserPromptSubmit", turn="x"), E(12, "PreModelRequest", turn="x")]
end = cm.current({"exec": ex}, lambda: mid, 13, cm.DEFAULT_CFG)["exec_end"]
check(end and end["t"] == 12.001 and end["reason"] == "ended", "A2 exec gone mid-turn -> synthetic end after its last event")
later = mid + [E(13, "Stop", turn="x"), E(20, "UserPromptSubmit", turn="y")]
check(cm.current({"exec": ex}, lambda: later, 21, cm.DEFAULT_CFG)["exec_end"] is None,
      "A2 exec run over and a later turn started -> no synthetic end")
end = cm.current({"exec": ex}, lambda: [E(5, "Stop")], 13, cm.DEFAULT_CFG)["exec_end"]
check(end["reason"] == "not_started" and end["exec_exit"], "A2 exec gone without a turn -> before_start")
quiet_run = dict(ex, reported_end=end)
app_turn = [E(5, "Stop"), E(20, "UserPromptSubmit", turn="app")]
check(cm.current({"exec": quiet_run}, lambda: app_turn, 21, cm.DEFAULT_CFG)["info"]["reason"] == "working",
      "A2 a reported before-start exit does not stop the next App turn")
check(cm.current({"exec": dict(ex, pid=os.getpid())}, lambda: mid, 13, cm.DEFAULT_CFG)["exec_end"] is None,
      "A2 exec still running -> nothing")

# ================================================================== A3. progress snapshot from the rollout
RS = sid_n(98)
rs_box = cm.Session(BOX, RS)
fake_rollout(RS, "old-turn", [say("上一轮的话"), ran("make old")])
fake_rollout(RS, "cur-turn", [say("先读代码"), ran("rg foo"), ran("pytest -q", 1),
                              {"type": "FileChange", "changes": {os.path.join(cm.HOME, "p", "a.py"): {"type": "update"}}},
                              say("测试失败 1 个，正在修"), {"type": "ContextCompaction"}])
with open(cm.rollout_paths(RS)[-1], "a") as f:
    f.write('{"type":"event_msg","payload":{"type":"item_completed","turn_id":"cur-turn","item":{"type":"Agent\n')
p = cm.rollout_snapshot(rs_box, "cur-turn", include_activity=True).get("activity")
check(p and p["messages"] == ["先读代码", "测试失败 1 个，正在修"] and p["commands_run"] == 2
      and p["recent_commands"][-1] == {"cmd": "pytest -q", "status": "completed", "exit_code": 1}
      and p["files_changed"] == ["~/p/a.py"] and p["compactions"] == 1 and "partial" not in p,
      "A3 rollout: this turn's commentary, commands, files (other turns and broken lines skipped)")
check(cm.rollout_snapshot(rs_box, "no-such-turn", include_activity=True) == {}
      and cm.rollout_snapshot(cm.Session(BOX, sid_n(97)), "cur-turn", include_activity=True) == {},
      "A3 rollout: nothing known -> no activity or usage")
snap = cm.progress_snapshot(rs_box, [E(1, "UserPromptSubmit", turn="cur-turn"), E(2, "PostToolUse", turn="cur-turn"),
                                     E(3, "PostModelRequest", turn="cur-turn", status="failed")],
                            {"in_flight": "tool", "silence_s": 5}, 61)
check(snap["running_s"] == 60 and snap["tool_calls"] == 1 and snap["failed_requests"] == 1
      and snap["activity"]["commands_run"] == 2, "A3 snapshot: hook counts + rollout activity")

RU = sid_n(96)
ru_box = cm.Session(BOX, RU)
fake_rollout(RU, "spent-1", [], "model-one", "high")
append_rollout(RU, token_count(1), token_count(2), rollout_event("task_complete", turn_id="spent-1"))
fake_rollout(RU, "spent-2", [], "model-two", "medium")
append_rollout(RU, token_count(3), rollout_event("token_count", info=None), token_count(5))
spent_1 = cm.rollout_snapshot(ru_box, "spent-1")["usage"]
spent_2 = cm.rollout_snapshot(ru_box)["usage"]
check(spent_1 == {"model": "model-one", "effort": "high",
                  "turn": {"input": 20, "cached_input": 10, "output": 6, "reasoning": 4, "total": 26},
                  "session": {"input": 20, "cached_input": 10, "output": 6, "reasoning": 4, "total": 26}},
      "A3 usage: first turn's totals exclude the next turn")
check(spent_2 == {"model": "model-two", "effort": "medium",
                  "turn": {"input": 30, "cached_input": 15, "output": 9, "reasoning": 6, "total": 39},
                  "session": {"input": 50, "cached_input": 25, "output": 15, "reasoning": 10, "total": 65}},
      "A3 usage: turn delta differs from latest model request and session total")
append_rollout(RU, rollout_event("turn_aborted", turn_id="spent-2"))
fake_rollout(RU, "no-sample", [], "model-three")
append_rollout(RU, rollout_event("token_count", info=None))
check(cm.rollout_snapshot(ru_box)["usage"] == {"model": "model-three", "session": spent_2["session"]},
      "A3 usage: omit missing effort and turn counters")

RT = sid_n(95)
rt_box = cm.Session(BOX, RT)
append_rollout(RT, token_count(5), {"type": "response_item", "payload": "x" * cm.ROLLOUT_TAIL})
fake_rollout(RT, "tail-turn", [], "tail-model", "low")
append_rollout(RT, token_count(6))
with patch.object(cm, "ROLLOUT_USAGE_LIMIT", cm.ROLLOUT_TAIL + 1):
    check(cm.rollout_snapshot(rt_box, "tail-turn")["usage"] == {
        "model": "tail-model", "effort": "low",
        "session": {"input": 60, "cached_input": 30, "output": 18, "reasoning": 12, "total": 78}},
        "A3 usage: a missing baseline in a truncated rollout omits turn totals")
append_rollout(RT, {"type": "response_item", "payload": "x" * cm.ROLLOUT_TAIL},
               token_count(7), rollout_event("task_complete", turn_id="tail-turn"))
with patch.object(cm, "ROLLOUT_USAGE_LIMIT", cm.ROLLOUT_TAIL + 1):
    cold_box = cm.Session(os.path.join(TMP, "cold-data"), RT)
    check(cm.rollout_snapshot(cold_box, "tail-turn") == {
        "usage": {"session": {"input": 70, "cached_input": 35, "output": 21, "reasoning": 14, "total": 91}}},
        "A3 usage: a terminal boundary can attribute a truncated turn's final session total")

# ================================================================== B. hooks + steering
MBM = "mgr-b"
S = sid_n(1)
r = send(MBM, S, "hello idle", "--via", "app")
check(r["mode"] == "queue" and r["transport"] == "app" and "Queued message" in r["codex_queue"] and r.get("claimed")
      and r["listening"] is False and r.get("next"), "B unknown/idle session -> codex queue, claimed, no listener yet")
with open(os.path.join(HERE, "..", "skills", "codex-intercom", "SKILL.md"), encoding="utf-8") as f:
    app_row = next(line for line in f if "`mode: queue`，`transport: app`" in line)
check(r["mode"] == "queue" and r["transport"] == "app" and queued(S) == 1
      and "消息已交给 App 通道，可能还在排队；用 `listen` 或 `status` 看这一轮的状态" in app_row,
      "B review 10: SKILL describes App handoff before a queued turn starts")
check(record(S)["manager"] == MBM and record(S)["expect"][0]["qid"], "B the send is recorded as not started (with its queue id)")
app_take(S)
check(hook(S, "UserPromptSubmit", prompt="hello idle") is None and state(S)["status"] == "working",
      "B UserPromptSubmit marks working")
r = send(MBM, S, "请改用方案 B", "--from", "Claude Code", "--effort", "high")
check(r["mode"] == "steer" and len(inbox(S)) == 1, "B working -> steer into inbox")
check(r.get("ignored_exec_options") == ["--effort"] and "exec_settings" not in r,
      "B steer reports that the requested effort did not affect the current turn")
out = hook(S, "PostToolUse", tool_name="Bash", tool_use_id="x1")
ctx = out and out["hookSpecificOutput"]["additionalContext"]
check(bool(ctx) and "请改用方案 B" in ctx and "Claude Code" in ctx and inbox(S) == [], "B PostToolUse injects it")
send(MBM, S, "最后再加一句总结")
out = hook(S, "Stop", stop_hook_active=False, last_assistant_message="done")
check(out and out.get("decision") == "block" and out["reason"] == "最后再加一句总结", "B Stop -> continuation prompt")
check(hook(S, "Stop", stop_hook_active=True, last_assistant_message="really done") == {} and state(S)["status"] == "idle",
      "B second Stop -> {} and idle")
hook(S, "UserPromptSubmit", turn="t2", prompt="next")
send(MBM, S, "only for main thread")
hook(S, "PostToolUse", turn="sub", tool_name="Bash", transcript_path=None, agent_id="a1", agent_type="explorer")
check(len(inbox(S)) == 1 and state(S)["turn_id"] == "t2", "B subagent hook does not drain or touch state")
hook(S, "Interrupt", turn="t2")
r = listen(MBM)
st = by_thread(r).get(S, {})
check(r["reason"] == "attention" and st.get("reason") == "interrupted" and st.get("earlier_stops") == 1
      and "only for main" in st["undelivered"][0]["preview"], "B interrupt: listen reports it with the undelivered message")
check(record(S)["expect"] == [], "B started sends are dropped once reported")
out = hook(S, "UserPromptSubmit", turn="t3", prompt="go on")
check(out and "only for main thread" in out["hookSpecificOutput"]["additionalContext"],
      "B undelivered message rides along with the next prompt")
for k in range(5):
    send(MBM, S, "msg-%d" % k)
with ThreadPoolExecutor(max_workers=8) as ex:
    outs = list(ex.map(lambda _: hook(S, "PostToolUse", turn="t3", tool_name="Bash"), range(8)))
texts = [o["hookSpecificOutput"]["additionalContext"] for o in outs if o]
counts = [sum(t.count("msg-%d\n" % k) + t.endswith("msg-%d" % k) for t in texts) for k in range(5)]
check(counts == [1] * 5, "B concurrent drains deliver each message exactly once")
p = subprocess.run([sys.executable, MB, "--dir", BOX, "hook"], input="not json", capture_output=True, text=True)
check(p.returncode == 0 and p.stdout == "", "B invalid hook stdin is ignored")
hook(S, "Stop", turn="t3", last_assistant_message="t3 done")
S3 = sid_n(3)
hook(S3, "UserPromptSubmit", prompt="x")
hook(S3, "PreModelRequest")
hook(S3, "PostModelRequest", status="failed", error="Selected model is at capacity.")
time.sleep(1.2)
r = send(MBM, S3, "hi again", "--via", "app", "--error-grace", "1")
check(r["mode"] == "queue", "B errored turn (state.json still working) -> queue, not steer")

# ================================================================== C. ownership
S = sid_n(20)
send("mgr-c1", S, "task", "--via", "app", "--label", "auth")
n = queued(S)
r = send("mgr-c2", S, "hijack", "--via", "app")
check(r["_rc"] == 2 and r["error"] == "owned_by_another_manager" and r["owner"] == "mgr-c1" and queued(S) == n,
      "C another manager's send is refused, nothing sent")
r = cli("mgr-c2", "claim", "--thread", S)
check(r["_rc"] == 2 and r["owner"] == "mgr-c1", "C another manager's claim is refused too")
r = send("mgr-c2", S, "take over", "--via", "app", "--take-over")
check(r["_rc"] == 0 and r.get("taken_over_from") == "mgr-c1" and record(S)["manager"] == "mgr-c2"
      and len(record(S)["expect"]) == 1, "C --take-over: new owner, the old owner's pending sends dropped")
r = listen("mgr-c1")
check(r["reason"] == "attention" and r["stops"] == [] and r["lost"][0]["reason"] == "taken_over"
      and r["lost"][0]["by"] == "mgr-c2" and r["lost"][0]["label"] == "auth", "C the old owner's listen reports the take-over")
check(cli("mgr-c1", "list")["sessions"] == [], "C ... and the session leaves its list")
check(listen("mgr-c1")["reason"] == "nothing_to_watch", "C listen with nothing owned returns at once")
age_manager("mgr-c2")
r = cli("mgr-c3", "claim", "--thread", S)
check(r["_rc"] == 0 and r.get("previous_owner") == "mgr-c2", "C a lapsed owner (24h idle, no listener) is replaced freely")
r = cli("mgr-c1", "release", "--thread", S)
check(r["_rc"] == 2, "C release by a non-owner is refused")
r = cli("mgr-c3", "release", "--thread", S)
st = cli("mgr-c3", "status", "--thread", S)
check(r["released"] and st["owner"] is None and st["managed"] is False and cli("mgr-c3", "list")["sessions"] == [],
      "C owner releases: unowned, unlisted")

# ================================================================== D. listen
ML = "mgr-l"
S1, S2, S3, S4 = sid_n(11), sid_n(12), sid_n(13), sid_n(14)
for sid, lab in ((S1, "one"), (S2, "two"), (S3, "three")):
    cli(ML, "claim", "--thread", sid, "--label", lab)
    hook(sid, "UserPromptSubmit", prompt="work")
th, res = bg(listen, ML, "--settle", "1.5")
time.sleep(1.2)
check(not res, "D listen blocks while every session works")
hook(S1, "Stop", last_assistant_message="one done")
time.sleep(0.4)
hook(S2, "Stop", last_assistant_message="two done")
th.join(timeout=15)
got = by_thread(res)
check(set(got) == {S1, S2} and got[S1]["last_message"] == "one done" and got[S2]["label"] == "two",
      "D two stops close together -> one notification with both")
check([w["thread"] for w in res.get("watching", [])] == [S3] and res["watching"][0]["state"] == "working",
      "D ... and the session still working is listed under watching")
r = listen(ML, "--timeout", "1.5")
check(r["reason"] == "timeout" and r["stops"] == [], "D exactly once: reported stops are not repeated")
for k in range(3):
    turn(S1, "r%d" % k, "round %d" % k)
t0 = time.time()
r = listen(ML)
st = by_thread(r).get(S1, {})
check(st.get("last_message") == "round 2" and st.get("earlier_stops") == 2 and time.time() - t0 < 5,
      "D stops while nobody listened: next listen reports the latest at once, earlier_stops 2")
th, res = bg(listen, ML)
time.sleep(1)
r = listen(ML)
check(r["reason"] == "already_listening" and r["listener_pid"], "D one listener per manager")
cli(ML, "claim", "--thread", S4, "--label", "four")
turn(S4, "x1", "four done")
th.join(timeout=15)
check(set(by_thread(res)) == {S4}, "D a session claimed while listening is picked up")
hook(S3, "Stop", last_assistant_message="three done")
cli(ML, "release", "--thread", S1)
r = listen(ML)
check(set(by_thread(r)) == {S3} and "lost" not in r, "D own release is not reported as lost")
check("usage" not in by_thread(r)[S3], "D listen stop omits usage without a rollout")

S5 = sid_n(15)
cli(ML, "claim", "--thread", S5)
turn(S5, "u1", "user turn, unreported")
r = send(ML, S5, "new task", "--via", "app")
th, res = bg(listen, ML)
time.sleep(1.5)
check(not res, "D our message hasn't started: the session's older stop is held back")
app_take(S5)
turn(S5, "u2", "new task done")
th.join(timeout=15)
st = by_thread(res).get(S5, {})
check(st.get("last_message") == "new task done" and st.get("earlier_stops") == 1,
      "D ... then its own stop is reported, the held-back one counted")
S6 = sid_n(16)
cli(ML, "claim", "--thread", S6)
send(ML, S6, "queued behind someone", "--via", "app")
turn(S6, "v1", "someone else's turn")
r = listen(ML, "--timeout", "1.5")
check(r["reason"] == "timeout", "D another turn ran while ours waits in the queue -> not reported yet")
app_take(S6)
turn(S6, "v2", "ours done")
r = listen(ML)
check(by_thread(r).get(S6, {}).get("last_message") == "ours done", "D ... reported once ours ran")
S7 = sid_n(17)
cli(ML, "claim", "--thread", S7)
turn(S7, "w0", "previous turn done")
before_timeout = record(S7)["reported"]
send(ML, S7, "nobody takes this", "--via", "app")
r = listen(ML, "--start-timeout", "1.5")
st = by_thread(r).get(S7, {})
check(st.get("reason") == "not_started" and st["queued"][0]["preview"] == "nobody takes this" and st["codex_queue"] == 1,
      "D a send that never starts -> not_started with the queued message")
after_timeout = record(S7)["reported"]
r = listen(ML, "--start-timeout", "1.5", "--timeout", "1.5")
check(after_timeout == before_timeout and by_thread(r).get(S7, {}).get("last_message") == "previous turn done",
      "D review 2: start timeout leaves the held-back Stop reportable (cursor advanced: %s, next: %s)"
      % (after_timeout > before_timeout, r["reason"]))
r = listen(ML, "--start-timeout", "1.5", "--timeout", "1.5")
check(r["reason"] == "timeout", "D not_started is reported once")
S8 = sid_n(18)
cli(ML, "claim", "--thread", S8)
hook(S8, "UserPromptSubmit", prompt="x")
hook(S8, "PreModelRequest")
hook(S8, "PostModelRequest", status="failed", error="stream disconnected")
time.sleep(1.1)
r = listen(ML, "--error-grace", "1")
check(by_thread(r).get(S8, {}).get("reason") == "error", "D model error -> error")
hook(S8, "PreModelRequest")
hook(S8, "PostModelRequest", status="failed", error="stream disconnected")
time.sleep(1.1)
r = listen(ML, "--error-grace", "1", "--timeout", "1.5")
check(r["reason"] == "timeout", "D the same error streak (retries keep failing) is not reported again")
rows = {x["thread"]: x for x in cli(ML, "list")["sessions"]}
check(S1 not in rows and rows[S3]["state"] == "stopped" and rows[S3]["unreported"] is False
      and rows[S7]["state"] == "stopped" and rows[S4]["label"] == "four", "D list shows the owned sessions and their state")

# ================================================================== D2. progress check-ins
MC = "mgr-ck"
SA, SB, SC = sid_n(50), sid_n(51), sid_n(52)
fake_rollout(SA, "ck1", [say("正在跑第 3/10 组测试"), ran("pytest -k group3")], "progress-model", "high")
append_rollout(SA, token_count(1))
cli(MC, "claim", "--thread", SA, "--label", "long", "--checkin", "3")
cli(MC, "claim", "--thread", SB)  # no interval of its own: listen's --checkin applies
hook(SA, "UserPromptSubmit", turn="ck1", prompt="long task")
hook(SB, "UserPromptSubmit", turn="b1", prompt="another long task")
t0 = time.time()
r = listen(MC, "--checkin", "100")
pg = by_thread(r, "progress")
check(r["reason"] == "checkin" and set(pg) == {SA} and time.time() - t0 >= 2.5
      and pg[SA]["activity"]["messages"] == ["正在跑第 3/10 组测试"] and pg[SA]["running_s"] >= 2
      and pg[SA]["label"] == "long", "D2 still working after its interval -> checkin with a progress snapshot")
check(pg[SA]["usage"]["model"] == "progress-model" and pg[SA]["usage"]["turn"]["total"] == 13
      and cli(MC, "status", "--thread", SA)["usage"] == pg[SA]["usage"],
      "D2 check-in and status report the working turn's usage")
check([w["thread"] for w in r["watching"]] == [SB], "D2 ... a session far from its own check-in stays under watching")
r = listen(MC, "--checkin", "5")  # SB due ~1.5s from now; SA by then past half of its 3s
pg = by_thread(r, "progress")
check(r["reason"] == "checkin" and set(pg) == {SA, SB}, "D2 one check-in due -> those halfway there come along")
check("usage" not in pg[SB], "D2 a progress entry without a rollout omits usage")
r = listen(MC, "--checkin", "4", "--timeout", "1.2")
check(r["reason"] == "timeout", "D2 the next check-in waits for the interval again")
time.sleep(1.5)
append_rollout(SA, token_count(2), rollout_event("task_complete", turn_id="ck1"))
fake_rollout(SA, "future-turn", [], "future-model", "low")
append_rollout(SA, token_count(3))
hook(SA, "Stop", turn="ck1", last_assistant_message="long done")
r = listen(MC, "--checkin", "4")
check(set(by_thread(r)) == {SA} and set(by_thread(r, "progress")) == {SB}, "D2 a stop brings along a check-in halfway due")
stop_usage = by_thread(r)[SA]["usage"]
check(stop_usage["model"] == "progress-model" and stop_usage["turn"]["total"] == 26
      and cli(MC, "status", "--thread", SA)["usage"] == stop_usage,
      "D2 stop and status do not use token counts from a later rollout turn")
cli(MC, "release", "--thread", SB)
cli(MC, "claim", "--thread", SC, "--checkin", "off")
hook(SC, "UserPromptSubmit", turn="c1", prompt="no check-ins please")
r = listen(MC, "--checkin", "1", "--timeout", "2.5")
check(r["reason"] == "timeout" and r["watching"][0]["thread"] == SC, "D2 --checkin off: no check-ins for that session")
st = cli(MC, "status", "--thread", SC)
check(st["progress"] and st["progress"]["turn_id"] == "c1" and st["checkin_s"] == 0, "D2 status shows progress while working")
check("usage" not in st, "D2 no rollout -> no usage in status")

# ================================================================== D3. long turns and the per-session usage cache
MU, SL = "mgr-long-usage", sid_n(53)
long_turn = "long-usage"
fake_rollout(SL, "preceding", [], "preceding-model", "low")
append_rollout(SL, token_count(2), rollout_event("task_complete", turn_id="preceding"))
fake_rollout(SL, long_turn, [say("older commentary")], "long-model", "high")
append_rollout(SL, token_count(3), {"type": "response_item", "payload": "x" * (cm.ROLLOUT_TAIL + 100)},
               token_count(4), rollout_event("item_completed", turn_id=long_turn, item=say("recent commentary")))
cli(MU, "claim", "--thread", SL, "--checkin", "1")
long_box = cm.Session(BOX, SL)
long_box.log_event(E(time.time() - 5, "UserPromptSubmit", turn=long_turn))
with patch.object(cm, "rollout_rows", wraps=cm.rollout_rows) as read_blocks:
    st = local_cli(MU, "status", "--thread", SL)
    check(st["codex"]["reason"] == "working" and st.get("usage", {}).get("model") == "long-model"
          and st["usage"]["effort"] == "high" and st["usage"]["turn"]["total"] == 26
          and st["usage"]["session"]["total"] == 52 and read_blocks.call_count > 1,
          "D3 status recovers the model, effort and baseline of a still-running long turn")
with open(long_box.usage_path) as f:
    saved_usage = json.load(f)[long_turn]
with open(cm.rollout_paths(SL)[-1], "rb") as f:
    f.seek(saved_usage["start"])
    start_marker = json.loads(f.readline())["payload"]
check(start_marker == {"type": "task_started", "turn_id": long_turn} and saved_usage["baseline"]["total"] == 26,
      "D3 usage cache persists the exact turn start offset and pre-turn total")
append_rollout(SL, token_count(5))
with patch.object(cm, "rollout_rows", wraps=cm.rollout_rows) as read_blocks:
    r = local_cli(MU, "listen", "--settle", "0", "--timeout", "1")
    entry = by_thread(r, "progress").get(SL, {})
    check(r["reason"] == "checkin" and entry.get("usage", {}).get("model") == "long-model"
          and entry["usage"]["effort"] == "high" and entry["usage"]["turn"]["total"] == 39
          and entry["usage"]["session"]["total"] == 65 and read_blocks.call_count == 1,
          "D3 listen check-in reuses the cached turn metadata and reads just the tail for updated usage")
check(entry.get("activity", {}).get("messages") == ["recent commentary"] and entry["activity"]["partial"],
      "D3 activity still summarizes only the tail of a long turn")
filler = {"type": "response_item", "payload": "x" * cm.ROLLOUT_TAIL}
large_append = [filler] * (cm.ROLLOUT_USAGE_LIMIT // cm.ROLLOUT_TAIL + 1)
append_rollout(SL, *large_append, token_count(6), rollout_event("task_complete", turn_id=long_turn))
fake_rollout(SL, "next", [], "next-model", "low")
append_rollout(SL, token_count(7))
hook(SL, "Stop", turn=long_turn, last_assistant_message="long turn done")
r = listen(MU, "--settle", "0")
st = by_thread(r).get(SL, {})
long_usage = st.get("usage", {})
check(st.get("reason") == "done" and long_usage.get("model") == "long-model"
      and long_usage.get("effort") == "high" and long_usage.get("turn", {}).get("total") == 52
      and long_usage.get("session", {}).get("total") == 78
      and cli(MU, "status", "--thread", SL).get("usage") == long_usage,
      "D3 review 6: listen and status retain the cached turn after a >64 MiB append (usage keys: %s)"
      % sorted(long_usage))

SU = sid_n(54)
fake_rollout(SU, "outside-limit", [], "unreachable-model", "high")
append_rollout(SU, token_count(1), filler, filler, token_count(2))
cli(MU, "claim", "--thread", SU)
hook(SU, "UserPromptSubmit", turn="outside-limit", prompt="long task")
with patch.object(cm, "ROLLOUT_USAGE_LIMIT", cm.ROLLOUT_TAIL * 2):
    with patch.object(cm, "rollout_rows", wraps=cm.rollout_rows) as read_blocks:
        st = local_cli(MU, "status", "--thread", SU)
        check(st["_rc"] == 0 and st["codex"]["reason"] == "working" and "usage" not in st
              and read_blocks.call_count == 2,
              "D3 usage omits fields whose evidence lies beyond the read limit")

SU = sid_n(59)
partial_box = cm.Session(BOX, SU)
append_rollout(SU, rollout_event("task_started", turn_id="context-only"), filler, filler,
               {"type": "turn_context", "payload": {"turn_id": "context-only", "model": "partial", "effort": "high"}})
with patch.object(cm, "ROLLOUT_USAGE_LIMIT", cm.ROLLOUT_TAIL + 1):
    partial_usage = cm.rollout_snapshot(partial_box, "context-only").get("usage")
    append_rollout(SU, filler, filler, token_count(2), rollout_event("task_complete", turn_id="context-only"))
    partial_done = cm.rollout_snapshot(partial_box, "context-only").get("usage", {})
check(partial_usage == {"model": "partial", "effort": "high"} and partial_done.get("model") == "partial"
      and partial_done.get("effort") == "high" and partial_done.get("session", {}).get("total") == 26
      and "turn" not in partial_done, "D3 review 6: retain known context even when the turn baseline was outside the window")

# ================================================================== E. new-turn transport (fake codex)
S = sid_n(30)
hook(S, "UserPromptSubmit", prompt="setup")  # records the session cwd
hook(S, "Stop", last_assistant_message="ok")
exec_calls = os.path.join(TMP, "exec_calls.jsonl")
open(exec_calls, "w").close()
app_consumes(S)
t0 = time.time()
r = send("mgr-e1", S, "app takes it", "--model", "app-choice")
check(r["transport"] == "app" and time.time() - t0 < 2.5, "E auto: App takes the queued message -> transport app")
with open(exec_calls) as f:
    check(not f.read() and r.get("ignored_exec_options") == ["--model"] and "exec_settings" not in r
          and record(S).get("exec_settings") == {"model": "app-choice"},
          "E App delivery ignores the model for this turn and saves it for later exec")
time.sleep(1.5)
set_mode("writer")
r = send("mgr-e1", S, "app holds it but is busy")
check(r["transport"] == "app" and "note" in r, "E auto: exec sees active writer -> stays with the App")
S = sid_n(31)
hook(S, "UserPromptSubmit", prompt="setup")
hook(S, "Stop", last_assistant_message="ok")
fake_rollout(S, "setup", [], "auto-model", "low")
set_mode("run")
open(os.path.join(TMP, "exec_calls.jsonl"), "w").close()
r = send("mgr-e2", S, "nobody serves it", "--effort", "high")
calls = [json.loads(l) for l in open(os.path.join(TMP, "exec_calls.jsonl"))]
check(r.get("transport") == "exec" and queued(S) == 0, "E auto: App doesn't take it -> exec drains the queue")
check(calls and os.path.realpath(calls[-1]["cwd"]) == WORKDIR and calls[-1]["prompt"] == cm.EXEC_NUDGE,
      "E exec runs in the session's cwd with the nudge prompt")
check(r.get("exec_settings") == {"model": {"value": "auto-model", "source": "rollout"},
                                 "effort": {"value": "high", "source": "explicit"}}
      and calls[-1]["args"] == ["exec", "resume", "--json", "--skip-git-repo-check", "-m", "auto-model",
                                "-c", 'model_reasoning_effort="high"', S, cm.EXEC_NUDGE],
      "E auto fallback passes the rollout model and explicit effort to exec")
time.sleep(1.5)
r = listen("mgr-e2")
check(by_thread(r).get(S, {}).get("last_message") == "exec done", "E listen reports the exec turn's stop")
hook(S, "UserPromptSubmit", turn="app2", prompt="later, in the App")
r = listen("mgr-e2", "--timeout", "1.5")
check(r["reason"] == "timeout" and r["watching"][0]["state"] == "working",
      "E a finished exec run doesn't mark a later App turn as ended")
hook(S, "Stop", turn="app2", last_assistant_message="app2 done")
set_mode("writer")
r = send("mgr-e2", S, "direct", "--via", "exec")
check(r["_rc"] == 1 and r.get("active_writer") and record(S)["expect"] == [],
      "E --via exec with the App holding the session -> error, nothing pending")
set_mode("fail")
r = send("mgr-e2", S, "broken exec", "--model", "queued-model", "--effort", "medium")
check(r.get("ok") is True and r.get("transport") == "queued" and r.get("exec_error"),
      "E exec failure leaves the message queued for the App")
check(r.get("ignored_exec_options") == ["--model", "--effort"] and "exec_settings" not in r,
      "E queued delivery reports both options unapplied after exec fails")
S = sid_n(32)
hook(S, "UserPromptSubmit", prompt="setup")
hook(S, "Stop", last_assistant_message="ok")
set_mode("crash")
r = send("mgr-e3", S, "will crash", "--via", "exec")
t0 = time.time()
r2 = listen("mgr-e3")
st = by_thread(r2).get(S, {})
check(r.get("transport") == "exec" and st.get("reason") == "ended" and st.get("exec_log") and time.time() - t0 < 10,
      "E exec process dies mid-turn -> ended (pid watch, no SessionEnd hook)")
S = sid_n(33)
hook(S, "UserPromptSubmit", prompt="setup")
hook(S, "Stop", last_assistant_message="ok")
set_mode("quiet")
r = send("mgr-e4", S, "exec exits without a turn")
r2 = listen("mgr-e4")
st = by_thread(r2).get(S, {})
check(r.get("transport") == "exec" and st.get("reason") == "not_started" and st.get("exec_exit"),
      "E exec exits before starting a turn -> not_started at once")
set_mode("run")

# ================================================================== E2. new session (fake codex)
MN = "mgr-new"
set_mode("new-started")
msg = "执行第一轮\n保留第二行\n"
r = cli(MN, "new", "--codex", FAKE, "--label", "first", "--model", "test-model", "--effort", "high",
        "--cwd", WORKDIR, "--search", "--checkin", "off", stdin=msg)
with open(os.path.join(TMP, "new_calls.jsonl")) as f:
    call = json.loads(f.readline())
sid = call["thread"]
expected = ["exec", "--json", "-m", "test-model", "-c", 'model_reasoning_effort="high"',
            "-c", 'web_search="live"', "--skip-git-repo-check", "-C", WORKDIR, "--", msg]
check(call["args"] == expected and call["devnull"] and call["detached"] and call["hook_dir"] == BOX,
      "E2 new passes stdin as an argument, detaches with /dev/null and sets the hook data directory")
check(r["ok"] and r["_rc"] == 0 and r["thread"] == sid and r["claimed"] and r["label"] == "first"
      and not r["listening"] and r["cwd"] == WORKDIR and r["exec_pid"] == call["pid"]
      and os.path.dirname(r["exec_log"]) == os.path.join(BOX, sid),
      "E2 new parses the real split thread.started line, claims it and moves the session log")
st = cli(MN, "status", "--thread", sid)
check(st["codex"]["reason"] == "working" and st["pending_sends"] == [] and st["checkin_s"] == 0
      and st["exec"]["alive"] and st["exec"]["log"] == r["exec_log"] and st["exec"]["pid"] == r["exec_pid"],
      "E2 a prompt logged before claim is already working, and the detached run survives new")
open(os.path.join(TMP, "finish-new"), "w").close()
st = by_thread(listen(MN, "--start-timeout", "0.1")).get(sid, {})
check(st.get("reason") == "done" and st.get("last_message") == "第一轮已完成",
      "E2 listen reports done when the prompt started before claim")
check(record(sid).get("exec_settings") == {"model": "test-model", "effort": "high"},
      "E2 new saves both explicit settings in the session record")
set_mode("quiet")
r = send(MN, sid, "resume after new", "--via", "exec")
check(r.get("exec_settings") == {"model": {"value": "test-model", "source": "session"},
                                 "effort": {"value": "high", "source": "session"}}
      and last_resume_args()[4:-2] == expected[2:6],
      "E2 resume after new uses the saved settings with the same argument quoting")
listen(MN, "--settle", "0")

set_mode("new-finished")
r = cli(MN, "new", "--codex", FAKE, "--message=--首轮消息")
with open(os.path.join(TMP, "new_calls.jsonl")) as f:
    call = json.loads(f.readlines()[-1])
sid = call["thread"]
check(r["ok"] and r["thread"] == sid and r["cwd"] == os.getcwd()
      and call["args"] == ["exec", "--json", "--skip-git-repo-check", "-C", os.getcwd(), "--", "--首轮消息"]
      and "exec_settings" not in record(sid),
      "E2 new uses the current directory and omits model, effort and search by default")
box = cm.Session(BOX, sid)
events = box.events()
check([e["ev"] for e in events] == ["UserPromptSubmit", "Stop"]
      and all(record(sid)["reported"] < e["t"] for e in events)
      and record(sid)["expect"][0]["t"] < events[0]["t"],
      "E2 the pre-claim stop and prompt both follow the initial cursor")
st = by_thread(listen(MN, "--start-timeout", "0.1")).get(sid, {})
check(st.get("reason") == "done" and st.get("last_message") == "第一轮已完成"
      and listen(MN, "--timeout", "0.3")["reason"] == "timeout",
      "E2 a turn already finished before claim reports done once")

for mode, reason in [("new-crash", "ended"), ("new-quiet", "not_started")]:
    set_mode(mode)
    r = cli(MN, "new", "--codex", FAKE, "--message", "run")
    st = by_thread(listen(MN)).get(r["thread"], {})
    check(r["ok"] and st.get("reason") == reason and st.get("exec_log") == r["exec_log"],
          "E2 new run exits after thread id: " + reason)

MF = "mgr-new-fail"
set_mode("new-fail")
r = cli(MF, "new", "--codex", FAKE, "--message", "will fail")
check(r["_rc"] == 1 and not r["ok"] and not r["thread"] and not r["claimed"] and "7" in r["error"]
      and "fake startup failure" in r["log_tail"] and not cm.pid_alive(r["exec_pid"]),
      "E2 early exit returns its error and stderr without claiming")

set_mode("new-timeout")
new_out = io.StringIO()
with patch.object(cm, "NEW_START_TIMEOUT", 0.8), contextlib.redirect_stdout(new_out):
    rc = cm.main(["--dir", BOX, "--manager", MF, "new", "--codex", FAKE, "--message", "will time out"])
r = json.loads(new_out.getvalue())
check(rc == 1 and not r["ok"] and not r["thread"] and not r["claimed"] and "超时" in r["error"]
      and "fake exec startup" in r["log_tail"] and not cm.pid_alive(r["exec_pid"])
      and listen(MF)["reason"] == "nothing_to_watch",
      "E2 startup timeout kills and reaps the live child without claiming")
failed_log = r["exec_log"]
set_mode("run")

# ================================================================== E4. exec settings precedence and rollout lookup
MS, SS = "mgr-exec-settings", sid_n(55)
set_mode("quiet")
turn(SS, "setup", "ready")
fake_rollout(SS, "setup", [], "rollout-model", "low")
box = cm.Session(BOX, SS)
box.edit_record(lambda rec: rec.update(exec_settings={"model": "saved-model", "effort": "medium"}))
r = send(MS, SS, "override model only", "--via", "exec", "--model", "explicit-model")
check(r.get("exec_settings") == {"model": {"value": "explicit-model", "source": "explicit"},
                                 "effort": {"value": "medium", "source": "session"}}
      and record(SS).get("exec_settings") == {"model": "explicit-model", "effort": "medium"},
      "E4 explicit model replaces the saved model and preserves the saved effort")
r = send(MS, SS, "reuse model", "--via", "exec", "--effort", "high")
check(r.get("exec_settings") == {"model": {"value": "explicit-model", "source": "session"},
                                 "effort": {"value": "high", "source": "explicit"}}
      and last_resume_args() == ["exec", "resume", "--json", "--skip-git-repo-check", "-m", "explicit-model",
                                 "-c", 'model_reasoning_effort="high"', SS, "reuse model"],
      "E4 later send uses the saved model and an explicit effort overrides session and rollout")

SS = sid_n(56)
turn(SS, "setup", "ready")
fake_rollout(SS, "earlier", [], "older-model", "medium")
fake_rollout(SS, "setup", [], "long-model", None)
append_rollout(SS, filler, filler)
box = cm.Session(BOX, SS)
r = send(MS, SS, "resume long rollout", "--via", "exec")
check(r.get("exec_settings") == {"model": {"value": "long-model", "source": "rollout"},
                                 "effort": {"value": "medium", "source": "rollout"}}
      and last_resume_args()[4:-2] == ["-m", "long-model", "-c", 'model_reasoning_effort="medium"'],
      "E4 long rollout supplies the latest nonempty value of each setting")
with patch.object(cm, "rollout_rows", wraps=cm.rollout_rows) as read_blocks:
    check(cm.rollout_settings(box) == {"model": "long-model", "effort": "medium"} and not read_blocks.called,
          "E4 unchanged rollout reuses the settings cache without reading blocks")
fake_rollout(SS, "later", [], "newer-model", None)
with patch.object(cm, "rollout_rows", wraps=cm.rollout_rows) as read_blocks:
    check(cm.rollout_settings(box) == {"model": "newer-model", "effort": "medium"} and read_blocks.call_count == 1,
          "E4 appended context replaces only its nonempty value using the cached offset")
with patch.object(cm, "ROLLOUT_USAGE_LIMIT", cm.ROLLOUT_TAIL):
    cached_settings = cm.rollout_settings(box)
    check(cached_settings == {"model": "newer-model", "effort": "medium"},
          "E4 review 8: settings already cached survive the lookup window moving past them (values: %s)"
          % cached_settings)
fake_rollout(SS, "wide-append", [], "model-in-append", None)
append_rollout(SS, *large_append)
r = send(MS, SS, "resume after large append", "--via", "exec")
check(r.get("exec_settings") == {"model": {"value": "model-in-append", "source": "rollout"},
                                 "effort": {"value": "medium", "source": "rollout"}}
      and last_resume_args()[4:-2] == ["-m", "model-in-append", "-c", 'model_reasoning_effort="medium"'],
      "E4 review 8: exec uses settings from the cache and the entire >64 MiB append (sources: %s)"
      % {key: item["source"] for key, item in r.get("exec_settings", {}).items()})

SS = sid_n(57)
turn(SS, "setup", "ready")
fake_rollout(SS, "setup", [], "model-without-effort", None)
r = send(MS, SS, "resume null effort", "--via", "exec")
check(r.get("exec_settings") == {"model": {"value": "model-without-effort", "source": "rollout"},
                                 "effort": {"value": None, "source": "config"}}
      and last_resume_args()[4:-2] == ["-m", "model-without-effort"],
      "E4 null effort without earlier evidence omits the config override")
SS = sid_n(58)
turn(SS, "setup", "ready")
r = send(MS, SS, "resume with config", "--via", "exec")
check(r.get("exec_settings") == {key: {"value": None, "source": "config"} for key in ("model", "effort")}
      and last_resume_args() == ["exec", "resume", "--json", "--skip-git-repo-check", SS, "resume with config"],
      "E4 missing session and rollout settings fall back to config without either flag")
set_mode("run")

# ================================================================== E3. exec exit races and missing hook diagnostics
@contextlib.contextmanager
def capture_exec_children():
    children = []
    popen = subprocess.Popen

    def capture_child(argv, *args, **kwargs):
        proc = popen(argv, *args, **kwargs)
        if argv[:2] == [FAKE, "exec"]:
            children.append(proc)
        return proc

    try:
        with patch.object(cm.subprocess, "Popen", side_effect=capture_child):
            yield children
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)


def check_exec_exit_race(mode, initial, message):
    manager = "mgr-" + mode
    set_mode(mode)
    with capture_exec_children() as children:
        if mode == "resume-race":
            thread = sid_n(34)
            turn(thread, "before-exec", "old result")
            sent = local_cli(manager, "send", "--thread", thread, "--codex", FAKE, "--message", "run",
                             "--app-timeout", "0")
        else:
            sent = local_cli(manager, "new", "--codex", FAKE, "--message", "run")
            thread = sent["thread"]
        assert sent["ok"] and sent["transport"] == "exec" and len(children) == 1, sent
        box = cm.Session(BOX, thread)
        started = box.record()["exec"]["started"]
        old_snapshots = []
        read = cm.EventLog.read

        def read_then_finish(log):
            events = read(log)
            if log.path == box.events_path and not old_snapshots:
                old_snapshots.append([e["ev"] for e in events if e["t"] > started])
                open(os.path.join(TMP, "finish-" + mode), "w").close()
                assert children[0].wait(timeout=5) == 0
            return events

        with patch.object(cm.EventLog, "read", read_then_finish):
            report = local_cli(manager, "listen", "--settle", "0", "--timeout", "1")
    stop = by_thread(report).get(thread, {})
    again = listen(manager, "--timeout", "0.2")
    check(old_snapshots == [initial] and stop.get("reason") == "done" and stop.get("last_message") == message
          and again["reason"] == "timeout" and not again["stops"] and record(thread)["reported"] == box.events()[-1]["t"],
          "E3 exec exit race preserves done and last_message exactly once: " + mode)


check_exec_exit_race("new-race-empty", [], "第一轮已完成")
check_exec_exit_race("new-race-started", ["UserPromptSubmit"], "第一轮已完成")
check_exec_exit_race("resume-race", ["UserPromptSubmit"], "exec done")

MR, SR = "mgr-late-stop", sid_n(35)
cli(MR, "claim", "--thread", SR)
race_box = cm.Session(BOX, SR)
started = time.time()
race_box.log_event(E(started + 1, "UserPromptSubmit", turn="late-stop"))
race_log = os.path.join(race_box.root, "exec-test.log")
open(race_log, "w").close()
os.utime(race_log, (started + 10, started + 10))
proc = subprocess.Popen(["true"])
proc.wait()
cm.record_exec(race_box, {"exec_pid": proc.pid, "_started": started, "_log": race_log})
first = by_thread(listen(MR, "--settle", "0")).get(SR, {})
race_box.log_event(E(started + 2, "Stop", turn="late-stop", msg="late real stop"))
second = by_thread(listen(MR, "--settle", "0", "--timeout", "0.5")).get(SR, {})
check(first.get("reason") == "ended" and second.get("reason") == "done"
      and second.get("last_message") == "late real stop",
      "E3 a synthetic exit does not advance the report cursor past a later-read real Stop")

set_mode("new-stop-only")
r = cli(MF, "new", "--codex", FAKE, "--message", "run without a prompt hook")
stop = by_thread(listen(MF)).get(r["thread"], {})
check(stop.get("reason") == "done" and stop.get("last_message") == "第一轮已完成",
      "E3 a final Stop takes priority when the exec prompt hook is missing")

for mode, reason in [("new-completed-no-hooks", "not_started"), ("new-completed-no-stop", "ended")]:
    set_mode(mode)
    r = cli(MF, "new", "--codex", FAKE, "--message", "run without hook records")
    stop = by_thread(listen(MF, "--settle", "0")).get(r["thread"], {})
    check(stop.get("reason") == reason and "exec 日志显示本轮已完成，但 hooks 没有记录到，先跑 doctor" in stop["summary"]
          and "last_message" not in stop, "E3 exec completion without hook records adds a doctor hint: " + reason)
set_mode("run")

# ================================================================== E5. review regressions at the CLI boundary
MR = "mgr-live-stop-only"
set_mode("new-stop-only-wait")
with capture_exec_children() as children:
    r = local_cli(MR, "new", "--codex", FAKE, "--message", "finish before the prompt hook")
    SR = r["thread"]
    race_box = cm.Session(BOX, SR)
    real_stop = race_box.events()[0]
    before = race_box.record()["reported"]
    expiry = real_stop["t"] - before + 0.025
    time.sleep(max(0, before + expiry - time.time()))
    st = cli(MR, "status", "--thread", SR)
    first = by_thread(listen(MR, "--settle", "0", "--start-timeout", str(expiry), "--timeout", "0.5")).get(SR, {})
    after = race_box.record()["reported"]
    check(st["codex"]["reason"] == "done" and st["exec"]["alive"] and children[0].poll() is None
          and first.get("reason") == "done" and first.get("last_message") == "第一轮已完成" and after == real_stop["t"],
          "E5 review 1: live exec's real Stop wins at the start deadline (listen: %s, cursor past Stop: %s)"
          % (first.get("reason"), after > real_stop["t"]))
    open(os.path.join(TMP, "finish-new-stop-only-wait"), "w").close()
    assert children[0].wait(timeout=5) == 0
r = listen(MR, "--settle", "0", "--start-timeout", str(expiry), "--timeout", "0.3")
check(r["reason"] == "timeout" and not r["stops"], "E5 the first Stop is not repeated after exec exits")

MR = "mgr-next-send-queued"
set_mode("new-started")
finish_new = os.path.join(TMP, "finish-new")
os.remove(finish_new)
with capture_exec_children() as children:
    r = local_cli(MR, "new", "--codex", FAKE, "--message", "run first")
    SR = r["thread"]
    sent = send(MR, SR, "wait in the App queue", "--queue", "--via", "app")
    race_box = cm.Session(BOX, SR)
    next_send = next(x for x in race_box.record()["expect"] if x["id"] == sent["id"])
    open(finish_new, "w").close()
    assert children[0].wait(timeout=5) == 0
st = cli(MR, "status", "--thread", SR)
real_stop = race_box.events()[-1]
r = listen(MR, "--settle", "0", "--timeout", "0.4")
first = by_thread(r).get(SR, {})
watch = by_thread(r, "watching").get(SR, {})
check(st["codex"]["reason"] == "done" and queued(SR) == 1 and first.get("reason") == "done"
      and first.get("last_message") == "第一轮已完成" and watch.get("state") == "waiting_start",
      "E5 review 2: first managed Stop is reported while the next send waits (listen: %s)" % r["reason"])
expiry = real_stop["t"] - next_send["t"] + 0.025
r = listen(MR, "--settle", "0", "--start-timeout", str(expiry), "--timeout", "0.7")
second = by_thread(r).get(SR, {})
check(second.get("reason") == "not_started" and [x["id"] for x in second["queued"]] == [sent["id"]]
      and race_box.record()["reported"] == real_stop["t"],
      "E5 review 2: only the queued send times out and the hook cursor stays on the first Stop")
r = listen(MR, "--settle", "0", "--start-timeout", str(expiry), "--timeout", "0.3")
check(r["reason"] == "timeout" and not r["stops"], "E5 neither the first Stop nor the next send timeout is repeated")

for via in ("exec", "auto"):
    MR = "mgr-resume-stop-only-" + via
    set_mode("new-finished")
    r = cli(MR, "new", "--codex", FAKE, "--message", "first turn", "--cwd", WORKDIR)
    SR = r["thread"]
    first = by_thread(listen(MR, "--settle", "0")).get(SR, {})
    assert first.get("last_message") == "第一轮已完成", first
    set_mode("resume-stop-only")
    sent = send(MR, SR, "resume with Stop alone", "--via", via, "--app-timeout", "0")
    st = cli(MR, "status", "--thread", SR)
    events = cm.Session(BOX, SR).events()
    stop = by_thread(listen(MR, "--settle", "0", "--start-timeout", "0", "--timeout", "0.5")).get(SR, {})
    again = listen(MR, "--timeout", "0.3")
    check(sent["transport"] == "exec" and queued(SR) == 0 and [e["ev"] for e in events] == ["UserPromptSubmit", "Stop", "Stop"]
          and st["codex"].get("turn_id") == "resume-turn" and not st["pending_sends"] and stop.get("reason") == "done"
          and stop.get("last_message") == "resume without prompt hook" and again["reason"] == "timeout",
          "E5 review 3: resume Stop without its prompt supersedes the previous turn via %s (status turn: %s, listen: %s)"
          % (via, st["codex"].get("turn_id"), stop.get("reason")))
    check(cli(MR, "status", "--thread", SR)["codex"].get("last_message") == "resume without prompt hook",
          "E5 review 3: reported resume result stays current via " + via)
    hook(SR, "UserPromptSubmit", turn="app-after-resume", prompt="later in App")
    st = cli(MR, "status", "--thread", SR)
    check(st["codex"]["reason"] == "working" and not st["unreported"],
          "E5 a resume with no prompt hook does not end the next App turn via " + via)
    hook(SR, "Stop", turn="app-after-resume", last_assistant_message="later App done")
    check(by_thread(listen(MR, "--settle", "0", "--timeout", "0.5")).get(SR, {}).get("last_message") == "later App done",
          "E5 the next App turn still reports its own Stop via " + via)

MR = "mgr-ended-no-repeat"
set_mode("new-crash")
r = cli(MR, "new", "--codex", FAKE, "--message", "crash after prompt")
SR = r["thread"]
first = by_thread(listen(MR, "--settle", "0")).get(SR, {})
st = cli(MR, "status", "--thread", SR)
r = listen(MR, "--settle", "0", "--stall", "0", "--timeout", "0.4")
check(first.get("reason") == "ended" and st["codex"]["reason"] == "ended" and not st["unreported"]
      and r["reason"] == "timeout" and not r["stops"],
      "E5 review 4: one exec exit never becomes working or stalled (status: %s, next stop: %s)"
      % (st["codex"]["reason"], by_thread(r).get(SR, {}).get("reason")))
hook(SR, "SessionEnd", turn="exec-turn")  # older hook registrations can still write this event
check(listen(MR, "--settle", "0", "--stall", "0", "--timeout", "0.3")["reason"] == "timeout",
      "E5 review 4: a legacy SessionEnd cannot repeat the reported exit")
hook(SR, "Stop", turn="exec-turn", last_assistant_message="late Stop after ended")
late = by_thread(listen(MR, "--settle", "0", "--timeout", "0.5")).get(SR, {})
check(late.get("reason") == "done" and late.get("last_message") == "late Stop after ended"
      and listen(MR, "--settle", "0", "--stall", "0", "--timeout", "0.3")["reason"] == "timeout",
      "E5 review 4: a real late Stop still reports exactly once after ended")

MR = "mgr-exec-data-dir"
other_dir = os.path.join(TMP, "other-intercom-dir")
relative_dir = os.path.relpath(BOX)
with patch.dict(os.environ, {"CODEX_INTERCOM_DIR": other_dir}):
    set_mode("new-finished")
    r = cli(MR, "new", "--codex", FAKE, "--message", "dir first turn", "--cwd", WORKDIR, data_dir=relative_dir)
    SR = r["thread"]
    first = by_thread(listen(MR, "--settle", "0")).get(SR, {})
    set_mode("resume-race")  # finish signal is already present from E3
    sent = cli(MR, "send", "--thread", SR, "--codex", FAKE, "--message", "dir resume", "--app-timeout", "0",
               data_dir=relative_dir)
    stop = by_thread(listen(MR, "--settle", "0", "--start-timeout", "0", "--timeout", "0.5")).get(SR, {})
with open(os.path.join(TMP, "new_calls.jsonl")) as f:
    new_call = json.loads(f.readlines()[-1])
with open(os.path.join(TMP, "exec_calls.jsonl")) as f:
    resume_call = json.loads(f.readlines()[-1])
other_events = cm.Session(other_dir, SR).events()
check(first.get("last_message") == "第一轮已完成" and sent.get("transport") == "exec"
      and new_call["hook_dir"] == resume_call["hook_dir"] == BOX and resume_call["cwd"] == WORKDIR
      and not other_events and stop.get("last_message") == "exec done",
      "E5 review 5: new and resume export the same absolute --dir over the environment (stray hooks: %d, listen: %s)"
      % (len(other_events), stop.get("reason")))
set_mode("run")

# ================================================================== F. App popup questions, gate
def gate(sid, tool="request_user_input_async"):
    data = {"session_id": sid, "hook_event_name": "PreToolUse", "turn_id": "t1", "tool_name": tool,
            "tool_use_id": "call_g1", "tool_input": {"questions": [{"title": "q"}]},
            "transcript_path": "/x/rollout-%s.jsonl" % sid}
    p = subprocess.run([sys.executable, MB, "--dir", BOX, "gate"], input=json.dumps(data), capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout) if p.stdout.strip() else None


S = sid_n(40)
check(gate(S) is None, "F unowned session: popup allowed (gate silent)")
evf = os.path.join(BOX, S, "events.jsonl")
last = json.loads(open(evf).read().splitlines()[-1])
check(last["ev"] == "PreToolUse" and last["tool"] == "request_user_input_async" and not last.get("denied")
      and last["questions"] == [{"question": "q", "options": []}], "F gate records the question")
cli("mgr-f", "claim", "--thread", S)
out = gate(S)
check(out and out["hookSpecificOutput"]["permissionDecision"] == "deny"
      and "写在回复里" in out["hookSpecificOutput"]["permissionDecisionReason"], "F owned: gate denies the popup")
check(json.loads(open(evf).read().splitlines()[-1]).get("denied") is True, "F denied question recorded as denied")
check(gate(S, tool="request_user_input") is not None, "F owned: Plan-mode question denied too")
check(gate(S, tool="Bash") is None, "F gate ignores other tools")
hook(S, "UserPromptSubmit", turn="t2", prompt="task")
hook(S, "PreToolUse", turn="t2", tool_name="request_user_input_async", tool_use_id="call_g2",
     tool_input={"questions": [{"title": "选哪个？"}]})
hook(S, "Stop", turn="t2", last_assistant_message="需要你决定：A 还是 B？")
r = listen("mgr-f")
st = by_thread(r).get(S, {})
check(st.get("reason") == "done" and "A 还是 B" in st.get("last_message", ""), "F denied popup is not an open question -> done")
cli("mgr-f", "release", "--thread", S)
check(gate(S) is None and cli("mgr-f", "status", "--thread", S)["managed"] is False, "F release: popups allowed again")
S = sid_n(41)
cli("mgr-f2", "claim", "--thread", S)
check(gate(S) is not None, "F claim alone turns the gate on")
age_manager("mgr-f2")
check(gate(S) is None, "F the gate lapses with its manager (24h idle, no listener)")
S = sid_n(43)
os.makedirs(os.path.join(BOX, S))
LEG = os.path.join(BOX, S, "managed.json")
json.dump({"since": "x", "by": "codex-intercom", "last_active": time.time()}, open(LEG, "w"))
check(gate(S) is not None, "F a fresh mark from the previous version still blocks popups")
cli("mgr-f4", "claim", "--thread", S)
check(not os.path.exists(LEG) and gate(S) is not None, "F claim replaces the old mark")
cli("mgr-f4", "release", "--thread", S)
check(gate(S) is None, "F ... and release lifts it")
r = cli("mgr-f4", "wait", "--thread", S)
check(r["_rc"] == 2 and r["error"] == "wait_removed" and "listen" in r["summary"],
      "F the removed wait command says what replaced it")

S = sid_n(42)
hook(S, "UserPromptSubmit", prompt="task")
hook(S, "PreToolUse", tool_name="request_user_input_async", tool_use_id="call_abc123",
     tool_input={"questions": [{"title": "保留哪个版本？", "options": ["新版（推荐）", "旧版"]}]})
hook(S, "PostToolUse", tool_name="request_user_input_async", tool_use_id="call_abc123")
hook(S, "Stop", last_assistant_message="已向你确认版本，等待回复。")
st = cli("mgr-f3", "status", "--thread", S)
check(st["codex"]["reason"] == "asking" and st["blocked_by_question"] is True
      and st["open_questions"] == [{"question": "保留哪个版本？", "options": ["新版（推荐）", "旧版"]}],
      "F popup open at the stop -> asking, blocked")
open(os.path.join(TMP, "exec_calls.jsonl"), "w").close()
t0 = time.time()
r = send("mgr-f3", S, "顺便把日志也看一下", "--model", "blocked-model")
check(r["transport"] == "blocked" and r["blocked_by_question"] and time.time() - t0 < 2
      and not open(os.path.join(TMP, "exec_calls.jsonl")).read()
      and r.get("ignored_exec_options") == ["--model"] and "exec_settings" not in r,
      "F send while the popup blocks -> queued, flagged, no App wait, no exec")
r = listen("mgr-f3", "--start-timeout", "1")
st = by_thread(r).get(S, {})
check(st.get("reason") == "not_started" and st.get("blocked_by_question"), "F listen: not started, blocked by the popup")
reply = ('<send_user_message_question_reply> user: [{"questionItemId":"[\\"request_user_input_async\\",'
         '\\"call_abc123\\",0]","question":"保留哪个版本？","answer":"新版（推荐）"}]')
hook(S, "UserPromptSubmit", turn="t2", prompt=reply)
ev = [json.loads(l) for l in open(os.path.join(BOX, S, "events.jsonl"))]
check(ev[-1].get("replies") == ["call_abc123"], "F reply prompt -> call id recorded")
check(cli("mgr-f3", "status", "--thread", S)["blocked_by_question"] is False, "F status: no longer blocked after the reply")

# ================================================================== G. install / doctor / uninstall
HJ = os.path.join(TMP, "hooks.json")
TARGET = os.path.join(TMP, "hooks", "codex_intercom.py")
legacy = {"description": "x；codex_mailbox：运行中插话", "hooks": {
    "SessionStart": [{"matcher": "^(startup|resume)$", "hooks": [{"type": "command", "command": "/bin/sh other.sh"}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/python3 ~/.codex/hooks/codex_mailbox.py hook"}]}],
    "PreCompact": [{"hooks": [{"type": "command", "command": "/usr/bin/python3 ~/.codex/hooks/codex_intercom.py hook",
                               "async": True}]}]}}
json.dump(legacy, open(HJ, "w"))
r = cli("mgr-g", "install", "--target", TARGET, "--hooks-json", HJ)
h = json.load(open(HJ))
cmds = [hk["command"] for gs in h["hooks"].values() for g in gs for hk in g["hooks"]]
check(r["hooks_changed"] and r["backup"] and os.path.exists(TARGET), "G install copies the script and backs up hooks.json")
check(not any("codex_mailbox" in c for c in cmds) and sum("codex_intercom" in c for c in cmds) == len(cm.HOOK_SPECS)
      and "/bin/sh other.sh" in cmds and "codex_intercom" in h["description"] and "PreCompact" not in h["hooks"],
      "G install replaces legacy entries (incl. dropped events), keeps foreign hooks (%d of ours)" % len(cm.HOOK_SPECS))
check(h["hooks"]["PreModelRequest"][0]["hooks"][0].get("async") is True
      and "async" not in h["hooks"]["PostModelRequest"][0]["hooks"][0], "G async only where intended")
check(cli("mgr-g", "install", "--target", TARGET, "--hooks-json", HJ)["hooks_changed"] is False, "G install is idempotent")
d = cli("mgr-g", "doctor", "--target", TARGET, "--hooks-json", HJ)
check([c for c in d["checks"] if c["check"] == "hooks"][0]["level"] == "ok"
      and [c for c in d["checks"] if c["check"] == "manager"][0]["detail"].startswith("mgr-g"), "G doctor: hooks ok, manager shown")
r = cli("mgr-g", "uninstall", "--target", TARGET, "--hooks-json", HJ, "--remove-script")
h = json.load(open(HJ))
check(r["hooks_changed"] and not os.path.exists(TARGET) and list(h["hooks"]) == ["SessionStart"],
      "G uninstall removes only our hooks")
d = cli("mgr-g", "doctor", "--target", TARGET, "--hooks-json", HJ)
check(d["ok"] is False, "G doctor flags a missing install")
OLDW = os.path.join(BOX, sid_n(12), "watch.json")
open(OLDW, "w").write("{}")
os.utime(OLDW, (time.time() - cm.MANAGED_TTL - 10,) * 2)
past = time.time() - cm.NEW_START_TIMEOUT - 5
os.utime(failed_log, (past, past))
r = cli("mgr-g", "gc", "--days", "0")
check(r["legacy_files_removed"] >= 1 and not os.path.exists(os.path.join(BOX, sid_n(12), "watch.json"))
      and os.path.isdir(os.path.join(BOX, sid_n(12))) and not os.path.isdir(os.path.join(BOX, sid_n(11))),
      "G gc removes legacy files and unowned old sessions, keeps owned ones")
check(r["new_exec_logs_removed"] >= 1 and not os.path.exists(failed_log),
      "G gc removes failed startup logs after they age out")

print("\n%d failure(s); tmp=%s" % (len(FAILS), TMP))
sys.exit(1 if FAILS else 0)
