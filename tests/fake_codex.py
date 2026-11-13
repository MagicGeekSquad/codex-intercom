#!/usr/bin/env python3
"""Stand-in for the `codex` CLI in tests. Mode comes from $FAKE_DIR/mode:
  queue ...        -> insert into the fake queue store ($CODEX_QUEUE_DB), print the real message shape
  exec resume ...  -> writer: fail with "active writer"; fail: other error; quiet: exit 0, no turn;
                      run: drain the queue, fire UserPromptSubmit + Stop hooks, exit 0;
                      crash: drain, fire UserPromptSubmit, exit without a Stop
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


def db():
    con = sqlite3.connect(DB)
    con.execute("create table if not exists queued_items (id text primary key, thread_id text, payload_json text,"
                " queue_order integer, created_at_ms integer, updated_at_ms integer)")
    return con


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
        f.write(json.dumps({"cwd": os.getcwd(), "prompt": prompt, "mode": mode}, ensure_ascii=False) + "\n")
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

    def hook(ev, **kw):
        data = dict(session_id=tid, hook_event_name=ev, turn_id="exec-turn", cwd=os.getcwd(),
                    transcript_path="/x/rollout-%s.jsonl" % tid, **kw)
        subprocess.run([sys.executable, os.environ["FAKE_MB"], "--dir", os.environ["FAKE_BOX"], "hook"],
                       input=json.dumps(data), capture_output=True, text=True)

    hook("UserPromptSubmit", prompt=prompt)
    time.sleep(1)
    if mode == "crash":  # dies mid-turn: no Stop
        sys.exit(0)
    hook("Stop", last_assistant_message="exec done", stop_hook_active=False)
    sys.exit(0)

print("fake codex: unsupported %s" % args)
sys.exit(2)
