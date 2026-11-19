#!/usr/bin/env python3
"""Stand-in for the `codex` CLI in tests. Mode comes from $FAKE_DIR/mode:
  queue ...        -> insert into the fake queue store ($CODEX_QUEUE_DB), print the real message shape
  exec resume ...  -> writer: fail with "active writer"; fail: other error; quiet: exit 0, no turn;
                      run: drain the queue, fire UserPromptSubmit + Stop hooks, exit 0;
                      crash: drain, fire UserPromptSubmit, exit without a Stop
  exec ...         -> new-* modes: emit startup JSONL and optionally run the first turn
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid

D = os.environ["FAKE_DIR"]
DB = os.environ["CODEX_QUEUE_DB"]
mode_file = os.path.join(D, "mode")
mode = open(mode_file).read().strip() if os.path.exists(mode_file) else "run"
args = sys.argv[1:]

if args == ["--version"]:
    print("codex-cli fake")
    sys.exit(0)


def db():
    con = sqlite3.connect(DB)
    con.execute("create table if not exists queued_items (id text primary key, thread_id text, payload_json text,"
                " queue_order integer, created_at_ms integer, updated_at_ms integer)")
    return con


def hook(tid, ev, turn="exec-turn", **kw):
    data = dict(session_id=tid, hook_event_name=ev, turn_id=turn, cwd=os.getcwd(),
                transcript_path="/x/rollout-%s.jsonl" % tid, **kw)
    hook_dir = os.environ.get("CODEX_INTERCOM_DIR") or os.environ["FAKE_BOX"]
    subprocess.run([sys.executable, os.environ["FAKE_MB"], "--dir", hook_dir, "hook"],
                   input=json.dumps(data), capture_output=True, text=True, check=True)


def wait_for_finish(name):
    deadline = time.monotonic() + 10
    while not os.path.isfile(os.path.join(D, name)):
        if time.monotonic() >= deadline:
            sys.exit("fake exec did not receive " + name)
        time.sleep(0.02)


if args[:1] == ["queue"]:
    tid, msg = args[args.index("--thread") + 1], args[args.index("--message") + 1]
    qid = str(uuid.uuid4())
    con = db()
    con.execute("insert into queued_items values (?,?,?,?,?,?)",
                (qid, tid, json.dumps({"text": msg}), time.time_ns() % 10 ** 9, 0, 0))
    con.commit()
    print("Queued message %s for thread %s." % (qid, tid))
    sys.exit(0)

if args[:2] == ["exec", "resume"]:
    tid, prompt = args[-2], args[-1]
    with open(os.path.join(D, "exec_calls.jsonl"), "a") as f:
        f.write(json.dumps({"args": args, "cwd": os.getcwd(), "prompt": prompt, "mode": mode,
                            "hook_dir": os.environ.get("CODEX_INTERCOM_DIR")}, ensure_ascii=False) + "\n")
    if mode not in ("resume-race", "resume-stop-only"):
        time.sleep(1)
    if mode == "writer":
        print("Error: thread/resume: thread/resume failed: thread %s already has an active writer (code -32600)" % tid)
        sys.exit(1)
    if mode == "fail":
        print("Error: something else broke")
        sys.exit(1)
    if mode == "quiet":  # exits cleanly without ever starting a turn
        sys.exit(0)
    con = db()
    con.execute("delete from queued_items where thread_id=?", (tid,))
    con.commit()

    if mode == "resume-stop-only":
        hook(tid, "Stop", turn="resume-turn", last_assistant_message="resume without prompt hook")
        sys.exit(0)
    hook(tid, "UserPromptSubmit", prompt=prompt)
    if mode == "resume-race":
        wait_for_finish("finish-resume-race")
    else:
        time.sleep(1)
    if mode == "crash":  # dies mid-turn: no Stop
        sys.exit(0)
    hook(tid, "Stop", last_assistant_message="exec done", stop_hook_active=False)
    if mode == "resume-race":
        print(json.dumps({"type": "turn.completed"}), flush=True)
    sys.exit(0)

if args[:2] == ["exec", "--json"]:
    tid = str(uuid.uuid4())
    with open(os.path.join(D, "new_calls.jsonl"), "a") as f:
        f.write(json.dumps({"args": args, "thread": tid, "pid": os.getpid(),
                            "devnull": os.path.samestat(os.fstat(0), os.stat(os.devnull)),
                            "detached": os.getsid(0) == os.getpid(),
                            "hook_dir": os.environ.get("CODEX_INTERCOM_DIR")}) + "\n")
    print("fake exec startup", file=sys.stderr, flush=True)
    print(json.dumps({"type": "thread.started", "thread_id": "invalid-id"}), flush=True)
    print(json.dumps({"type": "other.event", "thread_id": str(uuid.uuid4())}), flush=True)
    if mode == "new-fail":
        print("fake startup failure", file=sys.stderr, flush=True)
        sys.exit(7)
    if mode == "new-timeout":
        time.sleep(30)
        sys.exit(9)
    os.chdir(args[args.index("-C") + 1])
    if mode not in ("new-quiet", "new-race-empty", "new-completed-no-hooks", "new-stop-only", "new-stop-only-wait"):
        hook(tid, "UserPromptSubmit", prompt=args[-1])
    if mode in ("new-finished", "new-stop-only", "new-stop-only-wait"):
        hook(tid, "Stop", last_assistant_message="第一轮已完成")
    # Emit the id after the relevant hooks, so claim always sees the intended ordering.
    started_line = json.dumps({"type": "thread.started", "thread_id": tid.upper()})
    if mode == "new-started":
        print(started_line[:24], end="", flush=True)
        time.sleep(0.2)
        print(started_line[24:], flush=True)
        wait_for_finish("finish-new")
        hook(tid, "Stop", last_assistant_message="第一轮已完成")
    else:
        print(started_line, flush=True)
    if mode == "new-stop-only-wait":
        wait_for_finish("finish-new-stop-only-wait")
    if mode in ("new-race-empty", "new-race-started"):
        wait_for_finish("finish-" + mode)
        if mode == "new-race-empty":
            hook(tid, "UserPromptSubmit", prompt=args[-1])
        hook(tid, "Stop", last_assistant_message="第一轮已完成")
    if mode in ("new-race-empty", "new-race-started", "new-completed-no-hooks", "new-completed-no-stop"):
        print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "仅写入 exec 日志"}}),
              flush=True)
        print(json.dumps({"type": "turn.completed"}), flush=True)
    sys.exit(0)

print("fake codex: unsupported %s" % args)
sys.exit(2)
