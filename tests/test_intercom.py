#!/usr/bin/env python3
"""Tests for codex_intercom.py without a real Codex: stop analysis and attention on synthetic logs,
hooks/steering, ownership, listen (multi-session, latest-only, exactly-once, sends not started),
the exec transport (fake codex + fake queue store), popups/gate and install/uninstall.

usage: python3 tests/test_intercom.py [path/to/codex_intercom.py]
       (default: skills/codex-intercom/scripts/codex_intercom.py in this repo)
"""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
MB = (os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else
      os.path.join(HERE, os.pardir, "skills", "codex-intercom", "scripts", "codex_intercom.py"))
spec = importlib.util.spec_from_file_location("codex_intercom", MB)
cm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cm)

FAKE = os.path.join(HERE, "fake_codex.py")
TMP = tempfile.mkdtemp(prefix="intercom-test-", dir=os.environ.get("TMPDIR"))
BOX = os.path.join(TMP, "data")
WORKDIR = os.path.realpath(os.path.join(TMP, "work"))
os.makedirs(WORKDIR)
SESS = os.path.join(TMP, "sessions")
os.environ.update({"FAKE_DIR": TMP, "CODEX_QUEUE_DB": os.path.join(TMP, "queue.sqlite"), "FAKE_MB": MB,
                   "FAKE_BOX": BOX, "CODEX_SESSIONS_DIR": SESS})
os.environ.pop("CODEX_INTERCOM_MANAGER", None)
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


def cli(mgr, *a):
    p = subprocess.run([sys.executable, MB, "--dir", BOX, "--manager", mgr] + list(a), capture_output=True, text=True)
    assert p.stdout.strip(), "no output: rc=%s %s" % (p.returncode, p.stderr)
    out = json.loads(p.stdout)
    if isinstance(out, dict):
        out["_rc"] = p.returncode
    return out


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


def fake_rollout(sid, turn, items):
    """Append one turn to a Codex-style rollout file (only the parts the progress snapshot reads)."""
    d = os.path.join(SESS, "2026", "11", "13")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "rollout-2026-11-13T00-00-00-%s.jsonl" % sid), "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}}) + "\n")
        for it in items:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "thread_id": sid,
                                                                 "turn_id": turn, "item": it}}, ensure_ascii=False) + "\n")


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
    return cm.assess(ABOX, dict({"expect": []}, **rec), sorted(events, key=lambda e: e["t"]), now, c, queue)


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
dead = subprocess.Popen(["true"])
dead.wait()
ex = {"pid": dead.pid, "started": 10, "log": os.path.join(TMP, "no-such.log")}
mid = [E(9.6, "UserPromptSubmit", turn="setup"), E(9.7, "Stop", turn="setup"),  # a turn just before the spawn
       E(11, "UserPromptSubmit", turn="x"), E(12, "PreModelRequest", turn="x")]
end = cm.exec_end(mid, ex)
check(end and end["t"] == 12.001 and not end.get("before_start"), "A2 exec gone mid-turn -> synthetic end after its last event")
check(cm.exec_end(mid + [E(13, "Stop", turn="x"), E(20, "UserPromptSubmit", turn="y")], ex) is None,
      "A2 exec run over and a later turn started -> no synthetic end")
check(cm.exec_end([E(5, "Stop")], ex).get("before_start"), "A2 exec gone without a turn -> before_start")
check(cm.exec_end(mid, dict(ex, pid=os.getpid())) is None, "A2 exec still running -> nothing")

# ================================================================== A3. progress snapshot from the rollout
RS = sid_n(98)
fake_rollout(RS, "old-turn", [say("上一轮的话"), ran("make old")])
fake_rollout(RS, "cur-turn", [say("先读代码"), ran("rg foo"), ran("pytest -q", 1),
                              {"type": "FileChange", "changes": {os.path.join(cm.HOME, "p", "a.py"): {"type": "update"}}},
                              say("测试失败 1 个，正在修"), {"type": "ContextCompaction"}])
with open(cm.rollout_paths(RS)[-1], "a") as f:
    f.write('{"type":"event_msg","payload":{"type":"item_completed","turn_id":"cur-turn","item":{"type":"Agent\n')
p = cm.rollout_progress(RS, "cur-turn")
check(p and p["messages"] == ["先读代码", "测试失败 1 个，正在修"] and p["commands_run"] == 2
      and p["recent_commands"][-1] == {"cmd": "pytest -q", "status": "completed", "exit_code": 1}
      and p["files_changed"] == ["~/p/a.py"] and p["compactions"] == 1 and "partial" not in p,
      "A3 rollout: this turn's commentary, commands, files (other turns and broken lines skipped)")
check(cm.rollout_progress(RS, "no-such-turn") is None and cm.rollout_progress(sid_n(97), "cur-turn") is None,
      "A3 rollout: nothing known -> None")
snap = cm.progress_snapshot(RS, [E(1, "UserPromptSubmit", turn="cur-turn"), E(2, "PostToolUse", turn="cur-turn"),
                                 E(3, "PostModelRequest", turn="cur-turn", status="failed")],
                            {"in_flight": "tool", "silence_s": 5}, 61)
check(snap["running_s"] == 60 and snap["tool_calls"] == 1 and snap["failed_requests"] == 1
      and snap["activity"]["commands_run"] == 2, "A3 snapshot: hook counts + rollout activity")

# ================================================================== B. hooks + steering
MBM = "mgr-b"
S = sid_n(1)
r = send(MBM, S, "hello idle", "--via", "app")
check(r["mode"] == "queue" and r["transport"] == "app" and "Queued message" in r["codex_queue"] and r.get("claimed")
      and r["listening"] is False and r.get("next"), "B unknown/idle session -> codex queue, claimed, no listener yet")
check(record(S)["manager"] == MBM and record(S)["expect"][0]["qid"], "B the send is recorded as not started (with its queue id)")
app_take(S)
check(hook(S, "UserPromptSubmit", prompt="hello idle") is None and state(S)["status"] == "working",
      "B UserPromptSubmit marks working")
r = send(MBM, S, "请改用方案 B", "--from", "Claude Code")
check(r["mode"] == "steer" and len(inbox(S)) == 1, "B working -> steer into inbox")
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
turn(S7, "w0", "idle")
send(ML, S7, "nobody takes this", "--via", "app")
r = listen(ML, "--start-timeout", "1.5")
st = by_thread(r).get(S7, {})
check(st.get("reason") == "not_started" and st["queued"][0]["preview"] == "nobody takes this" and st["codex_queue"] == 1,
      "D a send that never starts -> not_started with the queued message")
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
fake_rollout(SA, "ck1", [say("正在跑第 3/10 组测试"), ran("pytest -k group3")])
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
check([w["thread"] for w in r["watching"]] == [SB], "D2 ... a session far from its own check-in stays under watching")
r = listen(MC, "--checkin", "5")  # SB due ~1.5s from now; SA by then past half of its 3s
pg = by_thread(r, "progress")
check(r["reason"] == "checkin" and set(pg) == {SA, SB}, "D2 one check-in due -> those halfway there come along")
r = listen(MC, "--checkin", "4", "--timeout", "1.2")
check(r["reason"] == "timeout", "D2 the next check-in waits for the interval again")
time.sleep(1.5)
hook(SA, "Stop", turn="ck1", last_assistant_message="long done")
r = listen(MC, "--checkin", "4")
check(set(by_thread(r)) == {SA} and set(by_thread(r, "progress")) == {SB}, "D2 a stop brings along a check-in halfway due")
cli(MC, "release", "--thread", SB)
cli(MC, "claim", "--thread", SC, "--checkin", "off")
hook(SC, "UserPromptSubmit", turn="c1", prompt="no check-ins please")
r = listen(MC, "--checkin", "1", "--timeout", "2.5")
check(r["reason"] == "timeout" and r["watching"][0]["thread"] == SC, "D2 --checkin off: no check-ins for that session")
st = cli(MC, "status", "--thread", SC)
check(st["progress"] and st["progress"]["turn_id"] == "c1" and st["checkin_s"] == 0, "D2 status shows progress while working")

# ================================================================== E. new-turn transport (fake codex)
S = sid_n(30)
hook(S, "UserPromptSubmit", prompt="setup")  # records the session cwd
hook(S, "Stop", last_assistant_message="ok")
app_consumes(S)
t0 = time.time()
r = send("mgr-e1", S, "app takes it")
check(r["transport"] == "app" and time.time() - t0 < 2.5, "E auto: App takes the queued message -> transport app")
time.sleep(1.5)
set_mode("writer")
r = send("mgr-e1", S, "app holds it but is busy")
check(r["transport"] == "app" and "note" in r, "E auto: exec sees active writer -> stays with the App")
S = sid_n(31)
hook(S, "UserPromptSubmit", prompt="setup")
hook(S, "Stop", last_assistant_message="ok")
set_mode("run")
open(os.path.join(TMP, "exec_calls.jsonl"), "w").close()
r = send("mgr-e2", S, "nobody serves it")
calls = [json.loads(l) for l in open(os.path.join(TMP, "exec_calls.jsonl"))]
check(r.get("transport") == "exec" and queued(S) == 0, "E auto: App doesn't take it -> exec drains the queue")
check(calls and os.path.realpath(calls[-1]["cwd"]) == WORKDIR and calls[-1]["prompt"] == cm.EXEC_NUDGE,
      "E exec runs in the session's cwd with the nudge prompt")
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
r = send("mgr-e2", S, "broken exec")
check(r.get("ok") is True and r.get("transport") == "queued" and r.get("exec_error"),
      "E exec failure leaves the message queued for the App")
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
r = send("mgr-f3", S, "顺便把日志也看一下")
check(r["transport"] == "blocked" and r["blocked_by_question"] and time.time() - t0 < 2
      and not open(os.path.join(TMP, "exec_calls.jsonl")).read(),
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
r = cli("mgr-g", "gc", "--days", "0")
check(r["legacy_files_removed"] >= 1 and not os.path.exists(os.path.join(BOX, sid_n(12), "watch.json"))
      and os.path.isdir(os.path.join(BOX, sid_n(12))) and not os.path.isdir(os.path.join(BOX, sid_n(11))),
      "G gc removes legacy files and unowned old sessions, keeps owned ones")

print("\n%d failure(s); tmp=%s" % (len(FAILS), TMP))
sys.exit(1 if FAILS else 0)
