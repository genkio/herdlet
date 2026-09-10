import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "herdlet.py")


class HerdletTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.sock = os.path.join(cls.tmp.name, "h.sock")
        cls.daemon = subprocess.Popen(
            [sys.executable, BIN, "--socket", cls.sock, "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            if os.path.exists(cls.sock):
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("daemon did not start")

    @classmethod
    def tearDownClass(cls):
        cls.daemon.terminate()
        cls.daemon.wait(timeout=5)
        cls.tmp.cleanup()

    @classmethod
    def run_cli(cls, *args, stdin=None, env_extra=None):
        env = dict(os.environ)
        env.pop("TMUX_PANE", None)
        env.pop("HERDLET_ID", None)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, BIN, "--socket", cls.sock, *args],
            capture_output=True, text=True, input=stdin, env=env, timeout=15)

    def parse(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        return json.loads(proc.stdout)

    def test_ping(self):
        resp = self.parse(self.run_cli("ping"))
        self.assertEqual(resp["result"]["type"], "pong")

    def test_report_get_merge(self):
        resp = self.parse(self.run_cli("report", "--id", "m1", "--state", "working",
                                       "--message", "npm test", "--agent", "claude"))
        self.assertEqual(resp["result"]["type"], "reported")
        # absent message preserves, state updates
        self.parse(self.run_cli("report", "--id", "m1", "--state", "done"))
        rec = self.parse(self.run_cli("get", "--id", "m1"))["result"]
        self.assertEqual(rec["state"], "done")
        self.assertEqual(rec["message"], "npm test")
        self.assertEqual(rec["agent"], "claude")
        # empty string clears
        self.parse(self.run_cli("report", "--id", "m1", "--state", "idle", "--message", ""))
        rec = self.parse(self.run_cli("get", "--id", "m1"))["result"]
        self.assertIsNone(rec["message"])

    def test_get_unknown_fails(self):
        proc = self.run_cli("get", "--id", "nope")
        self.assertEqual(proc.returncode, 1)

    def test_list(self):
        self.parse(self.run_cli("report", "--id", "l1", "--state", "working"))
        proc = self.run_cli("list", "--json")
        agents = json.loads(proc.stdout)
        self.assertIn("l1", [a["id"] for a in agents])

    def test_wait_already_satisfied(self):
        self.parse(self.run_cli("report", "--id", "w1", "--state", "done"))
        resp = self.parse(self.run_cli("wait", "--id", "w1", "--state", "done", "--timeout", "2"))
        self.assertTrue(resp["result"]["already"])

    def test_wait_blocks_until_report(self):
        self.parse(self.run_cli("report", "--id", "w2", "--state", "working"))
        timer = threading.Timer(0.4, lambda: self.run_cli(
            "report", "--id", "w2", "--state", "done"))
        timer.start()
        start = time.time()
        resp = self.parse(self.run_cli("wait", "--id", "w2", "--state", "done", "--timeout", "5"))
        elapsed = time.time() - start
        timer.join()
        self.assertEqual(resp["result"]["type"], "waited")
        self.assertFalse(resp["result"]["already"])
        self.assertGreaterEqual(elapsed, 0.3)
        self.assertLess(elapsed, 4)

    def test_wait_multi_state(self):
        self.parse(self.run_cli("report", "--id", "w3", "--state", "working"))
        timer = threading.Timer(0.3, lambda: self.run_cli(
            "report", "--id", "w3", "--state", "blocked"))
        timer.start()
        resp = self.parse(self.run_cli(
            "wait", "--id", "w3", "--state", "done,blocked", "--timeout", "5"))
        timer.join()
        self.assertEqual(resp["result"]["state"], "blocked")

    def test_wait_timeout_exit_2(self):
        proc = self.run_cli("wait", "--id", "ghost", "--state", "done", "--timeout", "0.3")
        self.assertEqual(proc.returncode, 2)

    def test_wait_any_of_multiple_ids(self):
        self.parse(self.run_cli("report", "--id", "any1", "--state", "working"))
        self.parse(self.run_cli("report", "--id", "any2", "--state", "working"))
        timer = threading.Timer(0.3, lambda: self.run_cli(
            "report", "--id", "any2", "--state", "done"))
        timer.start()
        resp = self.parse(self.run_cli(
            "wait", "--id", "any1,any2", "--state", "done", "--timeout", "5"))
        timer.join()
        self.assertEqual(resp["result"]["id"], "any2")

    def test_wait_prefix_wakes_on_new_agent(self):
        self.parse(self.run_cli("report", "--id", "wp/one", "--state", "working"))
        timer = threading.Timer(0.3, lambda: self.run_cli(
            "report", "--id", "wp/two", "--state", "blocked"))
        timer.start()
        resp = self.parse(self.run_cli(
            "wait", "--prefix", "wp/", "--state", "blocked", "--timeout", "5"))
        timer.join()
        self.assertEqual(resp["result"]["id"], "wp/two")

    def test_wait_prefix_already_satisfied(self):
        self.parse(self.run_cli("report", "--id", "wq/one", "--state", "done"))
        resp = self.parse(self.run_cli(
            "wait", "--prefix", "wq/", "--state", "done", "--timeout", "2"))
        self.assertTrue(resp["result"]["already"])
        self.assertEqual(resp["result"]["id"], "wq/one")

    def test_wait_multi_id_timeout_exit_2(self):
        proc = self.run_cli("wait", "--id", "ghost1,ghost2", "--state", "done", "--timeout", "0.3")
        self.assertEqual(proc.returncode, 2)

    def test_wait_requires_id_or_prefix(self):
        proc = self.run_cli("wait", "--state", "done", "--timeout", "1")
        self.assertEqual(proc.returncode, 1)

    def test_approve_unknown_id(self):
        proc = self.run_cli("approve", "--id", "nope")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("unknown agent", proc.stderr)

    def test_approve_rejects_non_digit_option(self):
        proc = self.run_cli("approve", "--id", "x", "--option", "yes")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("single digit", proc.stderr)

    def test_approve_wait_unknown_id(self):
        proc = self.run_cli("approve", "--id", "nope", "--wait")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("unknown agent", proc.stderr)

    def test_hook_captures_session_id(self):
        env = {"HERDLET_ID": "sess1"}
        self.run_cli("hook", stdin=json.dumps(
            {"hook_event_name": "UserPromptSubmit", "prompt": "go",
             "session_id": "abc-123", "cwd": "/tmp"}), env_extra=env)
        rec = self.parse(self.run_cli("get", "--id", "sess1"))["result"]
        self.assertEqual(rec["session"], "abc-123")
        # events without a session_id preserve the recorded one
        self.run_cli("hook", stdin=json.dumps({"hook_event_name": "Stop"}), env_extra=env)
        rec = self.parse(self.run_cli("get", "--id", "sess1"))["result"]
        self.assertEqual(rec["session"], "abc-123")

    def test_ack_done_to_idle(self):
        self.parse(self.run_cli("report", "--id", "ack1", "--state", "done",
                                "--message", "built it"))
        proc = self.run_cli("ack", "--id", "ack1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rec = self.parse(self.run_cli("get", "--id", "ack1"))["result"]
        self.assertEqual(rec["state"], "idle")
        self.assertEqual(rec["message"], "built it")

    def test_ack_ignores_non_done(self):
        self.parse(self.run_cli("report", "--id", "ack2", "--state", "working"))
        proc = self.run_cli("ack", "--id", "ack2")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("nothing to ack", proc.stdout)
        rec = self.parse(self.run_cli("get", "--id", "ack2"))["result"]
        self.assertEqual(rec["state"], "working")

    def test_ack_continues_past_unknown_ids(self):
        self.parse(self.run_cli("report", "--id", "ack3", "--state", "done"))
        proc = self.run_cli("ack", "--id", "ghost-one,ack3,ghost-two")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("unknown agent", proc.stderr)
        rec = self.parse(self.run_cli("get", "--id", "ack3"))["result"]
        self.assertEqual(rec["state"], "idle")

    def test_registry_survives_daemon_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock = os.path.join(tmp, "p.sock")

            def start():
                proc = subprocess.Popen(
                    [sys.executable, BIN, "--socket", sock, "serve"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for _ in range(50):
                    if os.path.exists(sock):
                        break
                    time.sleep(0.05)
                return proc

            def cli(*args):
                return subprocess.run(
                    [sys.executable, BIN, "--socket", sock, *args],
                    capture_output=True, text=True, timeout=15)

            daemon = start()
            try:
                cli("report", "--id", "p1", "--state", "done",
                    "--message", "built it", "--session", "s-123")
            finally:
                daemon.terminate()
                daemon.wait(timeout=5)
            daemon = start()
            try:
                rec = json.loads(cli("get", "--id", "p1").stdout)["result"]
                self.assertEqual(rec["state"], "done")
                self.assertEqual(rec["message"], "built it")
                self.assertEqual(rec["session"], "s-123")
            finally:
                daemon.terminate()
                daemon.wait(timeout=5)

    def test_resume_requires_session(self):
        self.parse(self.run_cli("report", "--id", "res1", "--state", "done"))
        proc = self.run_cli("resume", "--id", "res1")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("session", proc.stderr)

    def test_resume_requires_pane(self):
        self.parse(self.run_cli("report", "--id", "res2", "--state", "done",
                                "--session", "abc-123"))
        proc = self.run_cli("resume", "--id", "res2")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("pane", proc.stderr)

    def test_resume_unknown_agent_kind(self):
        self.parse(self.run_cli("report", "--id", "res3", "--state", "done",
                                "--session", "abc", "--agent", "mystery"))
        proc = self.run_cli("resume", "--id", "res3")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("resume syntax", proc.stderr)

    def test_wait_match_validation(self):
        proc = self.run_cli("wait", "--id", "a,b", "--match", "x", "--timeout", "1")
        self.assertEqual(proc.returncode, 1)
        proc = self.run_cli("wait", "--id", "a", "--state", "done", "--match", "x")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("mutually exclusive", proc.stderr)
        proc = self.run_cli("wait", "--id", "a", "--match", "(", "--timeout", "1")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("invalid regex", proc.stderr)

    def test_wait_needs_state_or_match(self):
        proc = self.run_cli("wait", "--id", "a", "--timeout", "1")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--state", proc.stderr)

    def test_wait_edge_ignores_already_satisfied(self):
        self.parse(self.run_cli("report", "--id", "e1", "--state", "done"))
        proc = self.run_cli("wait", "--id", "e1", "--state", "done", "--edge", "--timeout", "0.3")
        self.assertEqual(proc.returncode, 2)

    def test_wait_edge_wakes_on_fresh_transition(self):
        self.parse(self.run_cli("report", "--id", "e2", "--state", "done"))
        timer = threading.Timer(0.3, lambda: self.run_cli(
            "report", "--id", "e2", "--state", "done"))
        timer.start()
        resp = self.parse(self.run_cli(
            "wait", "--id", "e2", "--state", "done", "--edge", "--timeout", "5"))
        timer.join()
        self.assertFalse(resp["result"]["already"])

    def test_wait_edge_match_dies(self):
        proc = self.run_cli("wait", "--id", "a", "--match", "x", "--edge", "--timeout", "1")
        self.assertEqual(proc.returncode, 1)

    def test_subscribe_pushes_events(self):
        conn = socket.socket(socket.AF_UNIX)
        conn.settimeout(5)
        conn.connect(self.sock)
        stream = conn.makefile("rwb")
        stream.write(b'{"id":"s","method":"subscribe","params":{"id":"s1"}}\n')
        stream.flush()
        ack = json.loads(stream.readline())
        self.assertEqual(ack["result"]["type"], "subscribed")
        self.run_cli("report", "--id", "s1", "--state", "working")
        event = json.loads(stream.readline())
        self.assertEqual(event["type"], "agent.state_changed")
        self.assertEqual(event["id"], "s1")
        conn.close()

    def test_hook_claude_lifecycle(self):
        env = {"HERDLET_ID": "hk1"}
        prompt = json.dumps({"hook_event_name": "UserPromptSubmit",
                             "prompt": "fix the   auth bug", "cwd": "/tmp"})
        self.run_cli("hook", stdin=prompt, env_extra=env)
        rec = self.parse(self.run_cli("get", "--id", "hk1"))["result"]
        self.assertEqual(rec["state"], "working")
        self.assertEqual(rec["message"], "fix the auth bug")

        # tool events keep the prompt as message
        self.run_cli("hook", stdin=json.dumps(
            {"hook_event_name": "PreToolUse", "tool_name": "Bash"}), env_extra=env)
        rec = self.parse(self.run_cli("get", "--id", "hk1"))["result"]
        self.assertEqual(rec["message"], "fix the auth bug")

        self.run_cli("hook", stdin=json.dumps(
            {"hook_event_name": "Notification", "message": "needs permission"}), env_extra=env)
        rec = self.parse(self.run_cli("get", "--id", "hk1"))["result"]
        self.assertEqual(rec["state"], "blocked")

        self.run_cli("hook", stdin=json.dumps({"hook_event_name": "Stop"}), env_extra=env)
        rec = self.parse(self.run_cli("get", "--id", "hk1"))["result"]
        self.assertEqual(rec["state"], "done")

        # SessionEnd keeps the record (state -> ended) with its session ref, so
        # `list` still shows it and `resume` can bring it back; `remove` clears it.
        self.run_cli("hook", stdin=json.dumps(
            {"hook_event_name": "SessionEnd", "session_id": "sess-hk1"}), env_extra=env)
        rec = self.parse(self.run_cli("get", "--id", "hk1"))["result"]
        self.assertEqual(rec["state"], "ended")
        self.assertEqual(rec["session"], "sess-hk1")

        # ack on an ended (dead + collected) record removes it
        self.run_cli("ack", "--id", "hk1")
        self.assertEqual(self.run_cli("get", "--id", "hk1").returncode, 1)

    def test_hook_skip_env(self):
        self.parse(self.run_cli("report", "--id", "sk1", "--state", "done"))
        self.run_cli("hook", stdin=json.dumps({"hook_event_name": "SessionEnd"}),
                     env_extra={"HERDLET_ID": "sk1", "HERDLET_SKIP": "1"})
        rec = self.parse(self.run_cli("get", "--id", "sk1"))["result"]
        self.assertEqual(rec["state"], "done")

    def test_hook_never_fails(self):
        proc = self.run_cli("hook", stdin="not json at all")
        self.assertEqual(proc.returncode, 0)
        proc = self.run_cli("hook", stdin="")
        self.assertEqual(proc.returncode, 0)

    def test_remove(self):
        self.parse(self.run_cli("report", "--id", "r1", "--state", "idle"))
        self.parse(self.run_cli("remove", "--id", "r1"))
        proc = self.run_cli("get", "--id", "r1")
        self.assertEqual(proc.returncode, 1)

    def test_serve_if_needed_exits_clean(self):
        proc = self.run_cli("serve", "--if-needed")
        self.assertEqual(proc.returncode, 0)

    def test_list_prefix_filter(self):
        self.parse(self.run_cli("report", "--id", "px/one", "--state", "idle"))
        self.parse(self.run_cli("report", "--id", "px-other", "--state", "idle"))
        proc = self.run_cli("list", "--json", "--prefix", "px/")
        ids = [a["id"] for a in json.loads(proc.stdout)]
        self.assertEqual(ids, ["px/one"])

    def test_setup_idempotent(self):
        with tempfile.TemporaryDirectory() as home:
            env = {"HOME": home}
            proc = self.run_cli("setup", "--allow-tmux", env_extra=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)

            with open(os.path.join(home, ".claude", "settings.json")) as fh:
                cfg = json.load(fh)
            for event in ("SessionStart", "SessionEnd", "UserPromptSubmit",
                          "PostToolUse", "Notification", "PreCompact", "Stop"):
                commands = [h["command"] for g in cfg["hooks"][event] for h in g["hooks"]]
                self.assertTrue(any("herdlet hook" in c for c in commands), event)
            matchers = [g.get("matcher") for g in cfg["hooks"]["Notification"]]
            self.assertIn("permission_prompt|elicitation_dialog", matchers)
            self.assertIn("Bash(herdlet:*)", cfg["permissions"]["allow"])
            self.assertIn("Bash(tmux:*)", cfg["permissions"]["allow"])

            with open(os.path.join(home, ".codex", "hooks.json")) as fh:
                codex = json.load(fh)
            self.assertIn("--agent codex --event Stop",
                          codex["hooks"]["Stop"][0]["hooks"][0]["command"])
            self.assertTrue(os.path.exists(
                os.path.join(home, ".claude", "skills", "herdlet", "SKILL.md")))
            self.assertTrue(os.path.exists(
                os.path.join(home, ".codex", "skills", "herdlet", "SKILL.md")))

            with open(os.path.join(home, ".claude", "settings.json")) as fh:
                before = fh.read()
            proc = self.run_cli("setup", "--allow-tmux", env_extra=env)
            self.assertEqual(proc.returncode, 0)
            self.assertIn("already wired", proc.stdout)
            with open(os.path.join(home, ".claude", "settings.json")) as fh:
                self.assertEqual(fh.read(), before)

    def test_setup_preserves_existing_hooks(self):
        with tempfile.TemporaryDirectory() as home:
            claude_dir = os.path.join(home, ".claude")
            os.makedirs(claude_dir)
            existing = {"model": "opus", "hooks": {"Stop": [
                {"hooks": [{"type": "command", "command": "my-other-hook.sh"}]}]}}
            with open(os.path.join(claude_dir, "settings.json"), "w") as fh:
                json.dump(existing, fh)
            self.run_cli("setup", env_extra={"HOME": home})
            with open(os.path.join(claude_dir, "settings.json")) as fh:
                cfg = json.load(fh)
            self.assertEqual(cfg["model"], "opus")
            stop_cmds = [h["command"] for g in cfg["hooks"]["Stop"] for h in g["hooks"]]
            self.assertIn("my-other-hook.sh", stop_cmds)
            self.assertTrue(any("herdlet hook" in c for c in stop_cmds))
            self.assertTrue(os.path.exists(
                os.path.join(claude_dir, "settings.json.herdlet-bak")))

    def test_hook_stop_clears_stale_message(self):
        env = {"HERDLET_ID": "sm1"}
        self.run_cli("hook", stdin=json.dumps(
            {"hook_event_name": "Notification", "message": "needs permission"}),
            env_extra=env)
        rec = self.parse(self.run_cli("get", "--id", "sm1"))["result"]
        self.assertEqual(rec["state"], "blocked")
        self.assertEqual(rec["message"], "needs permission")
        # Stop ends the turn: the stale "needs permission" must not ride into done
        self.run_cli("hook", stdin=json.dumps({"hook_event_name": "Stop"}), env_extra=env)
        rec = self.parse(self.run_cli("get", "--id", "sm1"))["result"]
        self.assertEqual(rec["state"], "done")
        self.assertIsNone(rec["message"])

    def test_wait_matched_lists_all_ready(self):
        # a herd wait returns every currently-matching agent, not just the first,
        # so a master can batch-collect instead of re-issuing the wait per straggler
        self.parse(self.run_cli("report", "--id", "mb/one", "--state", "done"))
        self.parse(self.run_cli("report", "--id", "mb/two", "--state", "done"))
        resp = self.parse(self.run_cli(
            "wait", "--prefix", "mb/", "--state", "done", "--timeout", "2"))
        self.assertTrue(resp["result"]["already"])
        ids = sorted(a["id"] for a in resp["result"]["matched"])
        self.assertEqual(ids, ["mb/one", "mb/two"])

    def test_blocked_reemit_wakes_late_edge_waiter(self):
        # a wait --edge that STARTS after an agent is already blocked ignores the
        # stored state and would starve forever; the daemon's periodic re-emit is
        # what wakes it. run a dedicated daemon with a fast re-emit to prove it.
        with tempfile.TemporaryDirectory() as tmp:
            sock = os.path.join(tmp, "re.sock")
            env = dict(os.environ)
            env.pop("TMUX_PANE", None)
            env.pop("HERDLET_ID", None)
            env["HERDLET_BLOCKED_REEMIT"] = "0.4"
            daemon = subprocess.Popen(
                [sys.executable, BIN, "--socket", sock, "serve"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            try:
                for _ in range(50):
                    if os.path.exists(sock):
                        break
                    time.sleep(0.05)

                def cli(*args):
                    return subprocess.run(
                        [sys.executable, BIN, "--socket", sock, *args],
                        capture_output=True, text=True, env=env, timeout=15)

                cli("report", "--id", "b1", "--state", "blocked")
                start = time.time()
                proc = cli("wait", "--id", "b1", "--state", "blocked",
                           "--edge", "--timeout", "5")
                elapsed = time.time() - start
                self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
                resp = json.loads(proc.stdout)
                self.assertFalse(resp["result"]["already"])  # woke on a re-emit, not already-path
                self.assertLess(elapsed, 3)                  # ~0.4s re-emit, not the 5s timeout
            finally:
                daemon.terminate()
                daemon.wait(timeout=5)

    def test_wait_timeout_ok_is_a_result(self):
        proc = self.run_cli("wait", "--id", "ghost", "--state", "done",
                            "--timeout", "0.3", "--timeout-ok")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)["result"]
        self.assertEqual(result["type"], "timeout")
        self.assertEqual(result["agents"], ["ghost"])
        self.assertEqual(result["states"], ["done"])

    def test_wait_timeout_ok_keeps_prefix_and_multi_id(self):
        proc = self.run_cli("wait", "--id", "g1,g2", "--prefix", "gp/",
                            "--state", "done,blocked", "--timeout", "0.3",
                            "--timeout-ok")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)["result"]
        self.assertEqual(result["agents"], ["g1", "g2"])
        self.assertEqual(result["prefix"], "gp/")
        self.assertEqual(result["states"], ["done", "blocked"])

    def test_wait_timeout_without_flag_still_exits_2(self):
        # the documented chunk loop depends on this; --timeout-ok must be opt-in
        proc = self.run_cli("wait", "--id", "ghost", "--state", "done", "--timeout", "0.3")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "timeout")

    def test_hook_precompact_counts_and_keeps_state(self):
        env = {"HERDLET_ID": "pc1"}
        self.run_cli("hook", stdin=json.dumps(
            {"hook_event_name": "UserPromptSubmit", "prompt": "big job"}), env_extra=env)
        self.run_cli("hook", stdin=json.dumps({"hook_event_name": "PreCompact"}),
                     env_extra=env)
        rec = self.parse(self.run_cli("get", "--id", "pc1"))["result"]
        self.assertEqual(rec["state"], "working")   # the turn goes on
        self.assertEqual(rec["compacts"], 1)
        self.assertEqual(rec["message"], "big job")  # the prompt survives
        self.run_cli("hook", stdin=json.dumps({"hook_event_name": "PreCompact"}),
                     env_extra=env)
        rec = self.parse(self.run_cli("get", "--id", "pc1"))["result"]
        self.assertEqual(rec["compacts"], 2)
        self.assertEqual(rec["message"], "big job")

    def test_hook_precompact_for_an_unknown_id_registers_nothing(self):
        # a compaction says nothing about state, so it must not create a record
        self.run_cli("hook", stdin=json.dumps({"hook_event_name": "PreCompact"}),
                     env_extra={"HERDLET_ID": "pc-ghost"})
        self.assertEqual(self.run_cli("get", "--id", "pc-ghost").returncode, 1)

    def test_hook_records_transcript_path(self):
        self.run_cli("hook", stdin=json.dumps(
            {"hook_event_name": "UserPromptSubmit", "prompt": "go",
             "transcript_path": "/tmp/does-not-matter.jsonl"}),
            env_extra={"HERDLET_ID": "tr1"})
        rec = self.parse(self.run_cli("get", "--id", "tr1"))["result"]
        self.assertEqual(rec["transcript"], "/tmp/does-not-matter.jsonl")

    def test_peek_transcript_prints_assistant_text(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.jsonl")
            with open(path, "w") as fh:
                for stamp, blocks in (
                    ("2026-09-06T10:00:00.000Z",
                     [{"type": "text", "text": "first answer"}]),
                    ("2026-09-06T10:00:01.000Z",
                     [{"type": "thinking", "thinking": "hidden"},
                      {"type": "tool_use", "name": "Bash", "input": {}}]),
                    ("2026-09-06T10:00:02.000Z",
                     [{"type": "thinking", "thinking": "hidden"},
                      {"type": "text", "text": "second answer"}]),
                ):
                    fh.write(json.dumps({
                        "type": "assistant", "timestamp": stamp,
                        "message": {"role": "assistant", "content": blocks}}) + "\n")
                fh.write(json.dumps({"type": "user", "message": {
                    "role": "user", "content": "ignored"}}) + "\n")
                fh.write("not json\n")

            self.run_cli("hook", stdin=json.dumps(
                {"hook_event_name": "UserPromptSubmit", "prompt": "go",
                 "transcript_path": path}), env_extra={"HERDLET_ID": "tr2"})

            proc = self.run_cli("peek", "--id", "tr2", "--transcript")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("--- assistant 2026-09-06T10:00:02.000Z ---", proc.stdout)
            self.assertIn("second answer", proc.stdout)
            self.assertNotIn("first answer", proc.stdout)   # default is 1 message
            self.assertNotIn("hidden", proc.stdout)         # thinking is dropped

            proc = self.run_cli("peek", "--id", "tr2", "--transcript", "--lines", "5")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertLess(proc.stdout.index("first answer"),
                            proc.stdout.index("second answer"))  # oldest first

    def test_peek_transcript_without_one_dies(self):
        self.parse(self.run_cli("report", "--id", "tr3", "--state", "working"))
        proc = self.run_cli("peek", "--id", "tr3", "--transcript")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no transcript recorded for tr3", proc.stderr)
        self.assertIn("use plain peek", proc.stderr)

    def test_send_requires_exactly_one_source(self):
        proc = self.run_cli("send", "--id", "%1")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("nothing to send: pass text or --file PATH", proc.stderr)
        proc = self.run_cli("send", "--id", "%1", "--file", "/tmp/x", "hello")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not both", proc.stderr)

    def test_send_empty_file_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "empty.md")
            open(path, "w").close()
            proc = self.run_cli("send", "--id", "%1", "--file", path)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("is empty", proc.stderr)

    def test_peek_transcript_unknown_agent(self):
        proc = self.run_cli("peek", "--id", "no-such-agent", "--transcript")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("unknown agent 'no-such-agent'", proc.stderr)

    def test_spawn_rejects_bad_env_pairs(self):
        for bad in ("NOEQUALS", "2BAD=x", "has-dash=x", "=x"):
            proc = self.run_cli("spawn", "--id", "x/y", "--model", "opus",
                                "--effort", "high", "--env", bad,
                                env_extra={"TMUX_PANE": "%1"})
            self.assertEqual(proc.returncode, 1, bad)
            self.assertIn(f"invalid --env pair: {bad}", proc.stderr)


    def test_send_file_must_exist(self):
        proc = self.run_cli("send", "--id", "%1", "--file",
                            "/tmp/herdlet-no-such-file.txt")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("cannot read", proc.stderr)

    def test_spawn_rejects_other_agents(self):
        proc = self.run_cli("spawn", "--id", "x/y", "--model", "opus",
                            "--effort", "high", "--agent", "opencode")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("spawn supports claude and codex only", proc.stderr)

    def test_spawn_rejects_sandbox_for_claude(self):
        proc = self.run_cli("spawn", "--id", "x/y", "--model", "opus",
                            "--effort", "high", "--sandbox", "read-only")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--sandbox is only valid with --agent codex", proc.stderr)

    def test_spawn_rejects_permission_mode_for_codex(self):
        proc = self.run_cli("spawn", "--id", "x/y", "--model", "gpt-5.6-sol",
                            "--effort", "low", "--agent", "codex",
                            "--permission-mode", "auto")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("use --sandbox and --approval instead", proc.stderr)

    def test_spawn_rejects_approval_for_claude(self):
        proc = self.run_cli("spawn", "--id", "x/y", "--model", "opus",
                            "--effort", "high", "--approval", "never")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--approval is only valid with --agent codex", proc.stderr)

    def test_spawn_outside_tmux_dies(self):
        proc = self.run_cli("spawn", "--id", "x/y", "--model", "opus",
                            "--effort", "high")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("spawn must run inside tmux", proc.stderr)

    def test_list_shows_model_column(self):
        self.run_cli("report", "--id", "mc1", "--state", "idle")
        proc = self.run_cli("list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("MODEL", proc.stdout.splitlines()[0])

    def fake_tmux(self):
        """A tmux stub on PATH, so `send` can type without a real server."""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, "tmux")
        with open(path, "w") as fh:
            fh.write('#!/bin/sh\nprintf "%s\\n" "$*" >> "$(dirname "$0")/log"\n')
        os.chmod(path, 0o755)
        return {"PATH": d + os.pathsep + os.environ["PATH"]}, os.path.join(d, "log")

    def test_pair_links_both_records(self):
        self.parse(self.run_cli("report", "--id", "pr/a", "--state", "idle"))
        self.parse(self.run_cli("report", "--id", "pr/b", "--state", "idle"))
        resp = self.parse(self.run_cli("pair", "--id", "pr/a", "--with", "pr/b",
                                       "--topic", "plans/pr.md"))
        topic = os.path.abspath("plans/pr.md")
        self.assertEqual(resp["result"]["topic"], topic)
        for one, other in (("pr/a", "pr/b"), ("pr/b", "pr/a")):
            rec = self.parse(self.run_cli("get", "--id", one))["result"]
            self.assertEqual(rec["peers"], [other])
            self.assertEqual(rec["topics"], {other: topic})
        # idempotent: no duplicate peer entries
        self.parse(self.run_cli("pair", "--id", "pr/a", "--with", "pr/b",
                                "--topic", "plans/pr.md"))
        rec = self.parse(self.run_cli("get", "--id", "pr/a"))["result"]
        self.assertEqual(rec["peers"], ["pr/b"])

    def test_pair_refuses_an_unregistered_id(self):
        self.parse(self.run_cli("report", "--id", "pu/a", "--state", "idle"))
        proc = self.run_cli("pair", "--id", "pu/a", "--with", "pu/ghost",
                            "--topic", "t.md")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("unknown agent 'pu/ghost'", proc.stderr)
        rec = self.parse(self.run_cli("get", "--id", "pu/a"))["result"]
        self.assertEqual(rec["peers"], [])

    def test_unpair_removes_both_sides(self):
        for aid in ("up/a", "up/b"):
            self.parse(self.run_cli("report", "--id", aid, "--state", "idle"))
        self.parse(self.run_cli("pair", "--id", "up/a", "--with", "up/b",
                                "--topic", "t.md"))
        self.parse(self.run_cli("unpair", "--id", "up/a", "--with", "up/b"))
        for aid in ("up/a", "up/b"):
            rec = self.parse(self.run_cli("get", "--id", aid))["result"]
            self.assertEqual(rec["peers"], [])
            self.assertEqual(rec["topics"], {})

    def test_remove_drops_the_id_from_its_peers(self):
        for aid in ("rm/a", "rm/b"):
            self.parse(self.run_cli("report", "--id", aid, "--state", "idle"))
        self.parse(self.run_cli("pair", "--id", "rm/a", "--with", "rm/b",
                                "--topic", "t.md"))
        self.parse(self.run_cli("remove", "--id", "rm/a"))
        rec = self.parse(self.run_cli("get", "--id", "rm/b"))["result"]
        self.assertEqual(rec["peers"], [])
        self.assertEqual(rec["topics"], {})

    def test_list_shows_peers_column_only_when_someone_has_one(self):
        self.parse(self.run_cli("report", "--id", "lp/a", "--state", "idle"))
        self.parse(self.run_cli("report", "--id", "lp/b", "--state", "idle"))
        proc = self.run_cli("list", "--prefix", "lp/")
        self.assertNotIn("PEERS", proc.stdout)
        self.parse(self.run_cli("pair", "--id", "lp/a", "--with", "lp/b",
                                "--topic", "t.md"))
        proc = self.run_cli("list", "--prefix", "lp/")
        self.assertIn("PEERS", proc.stdout.splitlines()[0])
        self.assertIn("lp/b", proc.stdout)

    def test_send_to_a_non_peer_is_refused(self):
        self.parse(self.run_cli("report", "--id", "sc/a", "--state", "idle"))
        self.parse(self.run_cli("report", "--id", "sc/other", "--state", "idle",
                                "--pane", "%9"))
        env, log = self.fake_tmux()
        env["HERDLET_ID"] = "sc/a"
        proc = self.run_cli("send", "--id", "sc/other", "hello", env_extra=env)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("not paired with sc/other; raise it in your report "
                      "to the master", proc.stderr)
        self.assertFalse(os.path.exists(log))  # nothing typed anywhere

    def test_send_from_a_caller_with_no_record_says_so(self):
        # an unregistered caller has no peers either, but the cause is a missing
        # registration, not a scope decision
        self.parse(self.run_cli("report", "--id", "nr/a", "--state", "idle"))
        self.parse(self.run_cli("report", "--id", "nr/b", "--state", "idle",
                                "--pane", "%9"))
        self.parse(self.run_cli("remove", "--id", "nr/a"))
        env, log = self.fake_tmux()
        env["HERDLET_ID"] = "nr/a"
        proc = self.run_cli("send", "--id", "nr/b", "hello", env_extra=env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no record for nr/a; is the daemon current?", proc.stderr)
        self.assertFalse(os.path.exists(log))

    def test_send_from_an_unnamed_caller_is_unchanged(self):
        # the master (or a human in a bare pane) has no HERDLET_ID and no peers
        self.parse(self.run_cli("report", "--id", "sm/b", "--state", "idle",
                                "--pane", "%3"))
        env, log = self.fake_tmux()
        proc = self.run_cli("send", "--id", "sm/b", "orders", env_extra=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(log) as fh:
            self.assertIn("send-keys -t %3 -l -- orders", fh.read())

    def test_peer_send_logs_the_thread_and_emits_an_event(self):
        with tempfile.TemporaryDirectory() as d:
            topic = os.path.join(d, "loop.md")
            with open(topic, "w") as fh:
                fh.write("# Repro loop\n\nthe brief\n")
            self.parse(self.run_cli("report", "--id", "ps/dev", "--state", "working"))
            self.parse(self.run_cli("report", "--id", "ps/test", "--state", "idle",
                                    "--pane", "%8"))
            self.parse(self.run_cli("pair", "--id", "ps/dev", "--with", "ps/test",
                                    "--topic", topic))

            conn = socket.socket(socket.AF_UNIX)
            conn.settimeout(5)
            conn.connect(self.sock)
            stream = conn.makefile("rwb")
            stream.write(b'{"id":"s","method":"subscribe","params":{}}\n')
            stream.flush()
            stream.readline()  # subscribed ack

            env, log = self.fake_tmux()
            env["HERDLET_ID"] = "ps/dev"
            proc = self.run_cli("send", "--id", "ps/test",
                                "fixed in abc123,\nplease retest", env_extra=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)

            event = json.loads(stream.readline())
            conn.close()
            self.assertEqual(event, {"type": "peer_send", "from": "ps/dev",
                                     "to": "ps/test", "topic": topic,
                                     "chars": len("fixed in abc123,\nplease retest")})
            with open(log) as fh:
                self.assertIn("%8", fh.read())

            # a second send adds a line but not a second heading
            self.run_cli("send", "--id", "ps/test", "and the log?", env_extra=env)
            with open(topic) as fh:
                body = fh.read()
            self.assertEqual(body.count("## Thread"), 1)
            lines = [l for l in body.splitlines() if l.startswith("- ")]
            self.assertRegex(
                lines[0],
                r"^- \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4} ps/dev -> ps/test: "
                r"fixed in abc123, please retest$")
            self.assertTrue(lines[1].endswith("ps/dev -> ps/test: and the log?"))
            self.assertIn("the brief", body)  # the topic's own content survives

    def test_a_report_never_touches_peers(self):
        for aid in ("mk/a", "mk/b"):
            self.parse(self.run_cli("report", "--id", aid, "--state", "idle"))
        self.parse(self.run_cli("pair", "--id", "mk/a", "--with", "mk/b",
                                "--topic", "t.md"))
        self.parse(self.run_cli("report", "--id", "mk/a", "--state", "working",
                                "--message", "on it"))
        rec = self.parse(self.run_cli("get", "--id", "mk/a"))["result"]
        self.assertEqual(rec["peers"], ["mk/b"])
        self.assertEqual(rec["topics"], {"mk/b": os.path.abspath("t.md")})

    def test_a_peer_send_does_not_wake_a_wait_on_the_master(self):
        with tempfile.TemporaryDirectory() as d:
            self.parse(self.run_cli("report", "--id", "nw/master", "--state", "working"))
            self.parse(self.run_cli("report", "--id", "nw/dev", "--state", "working"))
            self.parse(self.run_cli("report", "--id", "nw/test", "--state", "idle",
                                    "--pane", "%8"))
            self.parse(self.run_cli("pair", "--id", "nw/dev", "--with", "nw/test",
                                    "--topic", os.path.join(d, "t.md")))
            env, _ = self.fake_tmux()
            env["HERDLET_ID"] = "nw/dev"
            sent = []
            timer = threading.Timer(0.3, lambda: sent.append(self.run_cli(
                "send", "--id", "nw/test", "retest please", env_extra=env)))
            timer.start()
            proc = self.run_cli("wait", "--id", "nw/master", "--state", "done",
                                "--timeout", "1")
            timer.join()
            self.assertEqual(sent[0].returncode, 0, sent[0].stderr)
            self.assertEqual(proc.returncode, 2)

    def test_periodic_sweep_prunes_over_max_age(self):
        # a long-running daemon GCs records that age past the cap, no restart needed
        with tempfile.TemporaryDirectory() as tmp:
            sock = os.path.join(tmp, "sw.sock")
            env = dict(os.environ)
            env.pop("TMUX_PANE", None)
            env.pop("HERDLET_ID", None)
            env["HERDLET_MAX_AGE"] = "1"
            env["HERDLET_PRUNE_INTERVAL"] = "0.4"
            daemon = subprocess.Popen(
                [sys.executable, BIN, "--socket", sock, "serve"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            try:
                for _ in range(50):
                    if os.path.exists(sock):
                        break
                    time.sleep(0.05)

                def cli(*args):
                    return subprocess.run(
                        [sys.executable, BIN, "--socket", sock, *args],
                        capture_output=True, text=True, env=env, timeout=15)

                r = cli("report", "--id", "sweepme", "--state", "idle")
                self.assertEqual(r.returncode, 0, r.stderr)
                time.sleep(2.0)  # ages past the 1s cap; the 0.4s sweep drops it
                self.assertEqual(cli("get", "--id", "sweepme").returncode, 1)
            finally:
                daemon.terminate()
                daemon.wait(timeout=5)


def _load_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("herdlet_mod", BIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class AnnotateTest(unittest.TestCase):
    """Liveness annotation is pure over (records, panes), so test it directly
    without tmux or a daemon."""

    def setUp(self):
        self.h = _load_module()

    def rec(self, state, updated_ago=0.0, pane="%1"):
        return {"state": state, "pane": pane, "updated": time.time() - updated_ago,
                "message": "", "agent": "claude", "session": "s", "cwd": "/tmp"}

    def test_fresh_worker_at_shell_stays_working(self):
        # the friction: a just-spawned / actively-hooking worker whose pane shows
        # a shell (wrapper script, `claude -p | tee`, shell tool call) is alive
        agents = [self.rec("working", updated_ago=0)]
        panes = {"%1": {"session": "h", "window_index": "1", "window_name": "w",
                        "command": "zsh"}}
        out = self.h.annotate(agents, panes)
        self.assertEqual(out[0]["state"], "working")

    def test_quiet_worker_at_shell_is_stale(self):
        # a genuinely dead agent stops reporting: old record + pane back at a shell
        agents = [self.rec("working", updated_ago=self.h.STALE_AFTER + 30)]
        panes = {"%1": {"session": "h", "window_index": "1", "window_name": "w",
                        "command": "zsh"}}
        out = self.h.annotate(agents, panes)
        self.assertEqual(out[0]["state"], "stale")

    def test_missing_pane_is_gone(self):
        agents = [self.rec("working", updated_ago=0, pane="%404")]
        out = self.h.annotate(agents, {"%1": {"session": "h", "window_index": "1",
                                              "window_name": "w", "command": "node"}})
        self.assertEqual(out[0]["state"], "gone")

    def test_worker_running_agent_binary_never_stale(self):
        # pane_current_command is the agent runtime, not a shell -> always alive
        agents = [self.rec("working", updated_ago=self.h.STALE_AFTER + 30)]
        panes = {"%1": {"session": "h", "window_index": "1", "window_name": "w",
                        "command": "node"}}
        out = self.h.annotate(agents, panes)
        self.assertEqual(out[0]["state"], "working")


class LoadPruneTest(unittest.TestCase):
    def test_ancient_terminal_records_pruned_on_load(self):
        h = _load_module()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.state")
            now = time.time()
            with open(path, "w") as fh:
                json.dump({"agents": {
                    "old/ended": {"state": "ended", "updated": now - h.TERMINAL_TTL - 100,
                                  "session": "x", "pane": "%1", "message": "", "agent": "c",
                                  "cwd": "/tmp"},
                    "recent/ended": {"state": "ended", "updated": now,
                                     "session": "y", "pane": "%2", "message": "", "agent": "c",
                                     "cwd": "/tmp"},
                    "live/working": {"state": "working", "updated": now - h.TERMINAL_TTL - 100,
                                     "session": "z", "pane": "%3", "message": "", "agent": "c",
                                     "cwd": "/tmp"},
                }}, fh)
            bus = h.Bus(state_path=path)
            self.assertNotIn("old/ended", bus.agents)     # ancient + terminal -> pruned
            self.assertIn("recent/ended", bus.agents)      # terminal but fresh -> kept
            self.assertIn("live/working", bus.agents)      # non-terminal -> kept regardless of age

    def test_load_prunes_any_record_over_max_age(self):
        os.environ["HERDLET_MAX_AGE"] = "100"
        try:
            h = _load_module()
            self.assertEqual(h.MAX_AGE, 100.0)
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "s.state")
                now = time.time()
                with open(path, "w") as fh:
                    json.dump({"agents": {
                        "stale/working": {"state": "working", "updated": now - 500,
                                          "session": "x", "pane": "%1", "message": "",
                                          "agent": "c", "cwd": "/tmp"},
                        "stale/blocked": {"state": "blocked", "updated": now - 500,
                                          "session": "y", "pane": "%2", "message": "",
                                          "agent": "c", "cwd": "/tmp"},
                        "fresh/working": {"state": "working", "updated": now - 5,
                                          "session": "z", "pane": "%3", "message": "",
                                          "agent": "c", "cwd": "/tmp"},
                    }}, fh)
                bus = h.Bus(state_path=path)
                # over the age cap -> pruned even though not terminal
                self.assertNotIn("stale/working", bus.agents)
                self.assertNotIn("stale/blocked", bus.agents)
                self.assertIn("fresh/working", bus.agents)
        finally:
            del os.environ["HERDLET_MAX_AGE"]


class VersionSkewTest(unittest.TestCase):
    """A daemon left over from an older install serves an older protocol."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sock = os.path.join(self.tmp.name, "v.sock")
        old = os.path.join(self.tmp.name, "old.py")
        with open(BIN) as src, open(old, "w") as dst:
            dst.write(src.read().replace('__version__ = "', '__version__ = "0.0.0-', 1))
        self.daemon = subprocess.Popen(
            [sys.executable, old, "--socket", self.sock, "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._stop)
        for _ in range(50):
            if os.path.exists(self.sock):
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("daemon did not start")

    def _stop(self):
        self.daemon.terminate()
        self.daemon.wait(timeout=5)

    def cli(self, *args, stdin=None):
        env = dict(os.environ)
        env.pop("TMUX_PANE", None)
        env.pop("HERDLET_ID", None)
        return subprocess.run([sys.executable, BIN, "--socket", self.sock, *args],
                              capture_output=True, text=True, input=stdin,
                              env=env, timeout=15)

    def test_every_read_command_warns(self):
        self.cli("report", "--id", "v1", "--state", "idle")
        for cmd in (("list",), ("get", "--id", "v1"), ("ping",),
                    ("wait", "--id", "v1", "--state", "done", "--timeout", "0.2"),
                    ("ack", "--id", "v1")):
            proc = self.cli(*cmd)
            self.assertIn("daemon is 0.0.0-", proc.stderr, cmd)
            self.assertIn("pkill -f 'herdlet.*serve'", proc.stderr, cmd)

    def test_hook_stays_silent(self):
        proc = self.cli("hook", stdin=json.dumps(
            {"hook_event_name": "UserPromptSubmit", "prompt": "go",
             "session_id": "abc"}))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(proc.stdout, "")


class SnapshotTest(unittest.TestCase):
    def test_a_record_without_compacts_still_answers_the_question(self):
        h = _load_module()
        bus = h.Bus()
        bus.agents["legacy"] = {"state": "idle", "pane": "%1", "message": None,
                                "agent": "claude", "session": None, "cwd": None,
                                "updated": time.time()}
        self.assertEqual(bus.snapshot("legacy")["compacts"], 0)

    def test_a_record_without_peers_reads_as_unpaired(self):
        h = _load_module()
        bus = h.Bus()
        bus.agents["legacy"] = {"state": "idle", "pane": "%1", "message": None,
                                "agent": "claude", "session": None, "cwd": None,
                                "updated": time.time()}
        snap = bus.snapshot("legacy")
        self.assertEqual(snap["peers"], [])
        self.assertEqual(snap["topics"], {})

    def test_a_real_count_is_not_overwritten_by_the_default(self):
        h = _load_module()
        bus = h.Bus()
        bus.report("a", {"state": "working"})
        bus.report("a", {"compact": True})
        bus.report("a", {"compact": True})
        self.assertEqual(bus.snapshot("a")["compacts"], 2)


class LimitSweepTest(unittest.TestCase):
    """The sweep is pure over (records, captured pane text), so inject a fake
    capture instead of driving real panes."""

    def setUp(self):
        self.h = _load_module()
        self.bus = self.h.Bus()
        self.quiet("w", "working", "%1")

    def quiet(self, agent_id, state, pane="%1", ago=None):
        """A record in `state` that has been silent for longer than one tick."""
        self.bus.report(agent_id, {"state": state, "pane": pane})
        ago = self.h.LIMIT_INTERVAL + 1 if ago is None else ago
        self.bus.agents[agent_id]["updated"] = time.time() - ago
        return self.bus.agents[agent_id]

    def sweep(self, capture):
        import asyncio
        out = io.StringIO()
        with contextlib.redirect_stdout(out):   # the daemon log line
            asyncio.run(self.bus.limit_sweep(capture))
        self.logged = out.getvalue()

    def test_banner_flips_working_to_limited(self):
        self.sweep(lambda pane: "Usage limit reached · continuing automatically")
        rec = self.bus.agents["w"]
        self.assertEqual(rec["state"], "limited")
        self.assertEqual(rec["message"], "usage limit banner in pane")
        self.assertIn("limited w %1", self.logged)

    def test_real_claude_banner_wordings_all_match(self):
        for banner in ("You've hit your session limit · resets 3pm",
                       "You've reached your weekly limit",
                       "Usage limit reached · finishing up",
                       "You're out of usage credits. /model to switch models.",
                       "Your org is out of usage · contact your admin",
                       "Your seat type doesn't include usage credits",
                       "Your usage limit has reset · press enter to continue",
                       "Continuing automatically when your limit resets · esc to cancel"):
            self.setUp()
            self.sweep(lambda pane, b=banner: b)
            self.assertEqual(self.bus.agents["w"]["state"], "limited", banner)

    def test_codex_hard_limit_matches(self):
        self.assertTrue(self.h.limit_regex().search("You've hit your usage limit"))

    def test_codex_reset_info_does_not_match(self):
        text = "You have 2 usage limit resets available. Run /usage to use one."
        self.assertIsNone(self.h.limit_regex().search(text))

    def test_fast_mode_limits_are_not_a_stop(self):
        # fast mode falls back to the normal one, so the agent keeps working
        for banner in ("You've hit your fast limit · resets in 2h",
                       "Fast mode overloaded and is temporarily unavailable · resets in 1h",
                       "Fast limit reached and temporarily disabled · resets in 1h"):
            self.setUp()
            self.sweep(lambda pane, b=banner: b)
            self.assertEqual(self.bus.agents["w"]["state"], "working", banner)

    def test_approaching_is_a_warning_not_a_stop(self):
        self.sweep(lambda pane:
                   "Approaching your 5-hour usage limit - Claude will wrap up")
        self.assertEqual(self.bus.agents["w"]["state"], "working")

    def test_ordinary_output_is_left_alone(self):
        self.sweep(lambda pane: "running the test suite\n48 passed")
        self.assertEqual(self.bus.agents["w"]["state"], "working")

    def test_only_the_bottom_of_the_pane_counts(self):
        # scrollback holding an old banner is not a live one; Claude Code draws
        # the real banner right above the input box
        old = "Usage limit reached\n" + "\n".join(f"line {i}" for i in range(20))
        self.sweep(lambda pane: old)
        self.assertEqual(self.bus.agents["w"]["state"], "working")
        self.sweep(lambda pane: "\n".join(f"line {i}" for i in range(20))
                   + "\n\n\n  Usage limit reached · continuing shortly\n\n> ")
        self.assertEqual(self.bus.agents["w"]["state"], "limited")

    def test_a_record_that_just_reported_is_left_alone(self):
        # an auto-resumed worker is reporting again while the banner is still on
        # screen: it must not be flipped back to limited every tick
        self.quiet("w", "working", ago=0)
        self.sweep(lambda pane: "Usage limit reached")
        self.assertEqual(self.bus.agents["w"]["state"], "working")

    def test_a_state_change_during_the_capture_is_not_clobbered(self):
        def capture(pane):
            self.bus.report("w", {"state": "done", "message": "finished"})
            return "Usage limit reached"

        self.sweep(capture)
        self.assertEqual(self.bus.agents["w"]["state"], "done")
        self.assertEqual(self.bus.agents["w"]["message"], "finished")

    def test_the_state_re_read_alone_stops_the_clobber(self):
        # same race, but the record is also aged past a tick, so the freshness
        # guard cannot be what saves it: only re-reading the state can
        def capture(pane):
            self.bus.report("w", {"state": "done", "message": "finished"})
            self.bus.agents["w"]["updated"] = time.time() - self.h.LIMIT_INTERVAL - 1
            return "Usage limit reached"

        self.sweep(capture)
        self.assertEqual(self.bus.agents["w"]["state"], "done")
        self.assertEqual(self.bus.agents["w"]["message"], "finished")
        self.assertEqual(self.logged, "")

    def test_blocked_is_not_scraped(self):
        # a blocked record is already a wake signal, and flipping it would
        # cancel its re-announce
        self.quiet("b", "blocked", "%2")
        seen = []
        self.sweep(lambda pane: seen.append(pane) or "")
        self.assertNotIn("%2", seen)

    def test_idle_and_done_panes_are_not_scraped(self):
        for state in ("idle", "done", "ended", "limited"):
            self.bus = self.h.Bus()
            self.quiet("w", state)
            seen = []
            self.sweep(lambda pane: seen.append(pane) or "")
            self.assertEqual(seen, [], state)

    def test_spawning_is_scraped(self):
        self.quiet("s", "spawning", "%3")
        seen = []
        self.sweep(lambda pane: seen.append(pane) or "")
        self.assertIn("%3", seen)

    def test_record_without_a_pane_is_skipped(self):
        self.bus = self.h.Bus()
        self.quiet("nopane", "working", pane=None)
        seen = []
        self.sweep(lambda pane: seen.append(pane) or "")
        self.assertEqual(seen, [])

    def test_capture_failure_never_kills_the_sweep(self):
        # tmux missing, or the pane gone: treat as no match, keep going
        def boom(pane):
            raise OSError("no server running")

        self.sweep(boom)
        self.assertEqual(self.bus.agents["w"]["state"], "working")
        self.sweep(lambda pane: "")
        self.assertEqual(self.bus.agents["w"]["state"], "working")

    def test_one_bad_record_does_not_stop_the_rest(self):
        self.quiet("z", "working", "%9")
        seen = []

        def capture(pane):
            seen.append(pane)
            if pane == "%1":
                raise OSError("gone")
            return ""

        self.sweep(capture)
        self.assertEqual(sorted(seen), ["%1", "%9"])

    def test_a_broken_env_pattern_falls_back_to_the_default(self):
        os.environ["HERDLET_LIMIT_PATTERN"] = "(unclosed"
        try:
            h = _load_module()
            self.assertIsNone(h.LIMIT_RE)   # nothing compiled at import
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertTrue(h.limit_regex().search("Usage limit reached"))
            self.assertIn("bad HERDLET_LIMIT_PATTERN", out.getvalue())
        finally:
            del os.environ["HERDLET_LIMIT_PATTERN"]

    def test_a_client_command_compiles_nothing(self):
        # the pattern is the daemon's business; a client must not pay for it or
        # print about it
        self.assertIsNone(_load_module().LIMIT_RE)

    def test_limited_wakes_waiters(self):
        woken = []
        self.bus.waiters.append(
            (lambda i, s: s == "limited", _FakeFuture(woken)))
        self.sweep(lambda pane: "Usage limit reached")
        self.assertEqual(len(woken), 1)

    def test_limited_is_live_not_terminal(self):
        self.assertNotIn("limited", self.h.TERMINAL)
        self.assertNotIn("spawning", self.h.TERMINAL)
        rec = {"state": "limited", "updated": time.time() - self.h.TERMINAL_TTL - 100}
        self.assertFalse(self.h.Bus()._prunable(rec, time.time()))

    def test_limited_at_a_dead_shell_still_goes_stale(self):
        # the pane fell back to a shell AND the record went quiet: the process
        # died, so this needs `resume`, not a wait for the reset
        rec = {"state": "limited", "pane": "%1", "message": "", "agent": "claude",
               "session": "s", "cwd": "/tmp",
               "updated": time.time() - self.h.STALE_AFTER - 30}
        panes = {"%1": {"session": "h", "window_index": "1", "window_name": "w",
                        "command": "zsh"}}
        self.assertEqual(self.h.annotate([rec], panes)[0]["state"], "stale")

    def test_limited_agent_still_running_is_not_stale(self):
        rec = {"state": "limited", "pane": "%1", "message": "", "agent": "claude",
               "session": "s", "cwd": "/tmp",
               "updated": time.time() - self.h.STALE_AFTER - 30}
        panes = {"%1": {"session": "h", "window_index": "1", "window_name": "w",
                        "command": "node"}}
        self.assertEqual(self.h.annotate([rec], panes)[0]["state"], "limited")


class _FakeFuture:
    def __init__(self, sink):
        self.sink = sink

    def done(self):
        return False

    def set_result(self, value):
        self.sink.append(value)


class SendRoutingTest(unittest.TestCase):
    def setUp(self):
        self.h = _load_module()
        self.calls = []
        self.h.tmux = lambda *args, **kw: self.calls.append((args, kw)) or ""

    def verbs(self):
        return [args[0] for args, _ in self.calls]

    def test_short_text_is_typed(self):
        self.h.send_text("%1", "run the tests")
        self.assertEqual(self.verbs(), ["send-keys", "send-keys"])

    def test_long_single_line_text_is_pasted(self):
        # the friction: one giant argv either trips tmux's 16 KiB command limit
        # or races the Enter, so anything long goes through a buffer paste
        text = "a" * 6144
        self.h.send_text("%1", text)
        self.assertEqual(self.verbs(), ["load-buffer", "paste-buffer", "send-keys"])
        self.assertEqual(self.calls[0][1]["input"], text)

    def test_multiline_text_is_pasted(self):
        self.h.send_text("%1", "line one\nline two")
        self.assertEqual(self.verbs(), ["load-buffer", "paste-buffer", "send-keys"])

    def test_boundary_is_the_documented_length(self):
        self.h.send_text("%1", "a" * self.h.SEND_PASTE_OVER)
        self.assertEqual(self.verbs()[0], "send-keys")
        self.calls.clear()
        self.h.send_text("%1", "a" * (self.h.SEND_PASTE_OVER + 1))
        self.assertEqual(self.verbs()[0], "load-buffer")

    def test_no_enter_skips_the_submit(self):
        self.h.send_text("%1", "typed only", no_enter=True)
        self.assertEqual(self.verbs(), ["send-keys"])


class SpawnLineTest(unittest.TestCase):
    def setUp(self):
        self.h = _load_module()

    def test_house_launch_line(self):
        line = self.h.spawn_line("proj/dev", "opus", "high", "ship the thing", "auto")
        self.assertEqual(line, (
            "CC_IMESSAGE_SKIP=1 CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1 "
            "HERDLET_ID=proj/dev claude -n 'proj/dev: ship the thing' "
            "--model opus --effort high --permission-mode auto"))

    def test_values_are_shell_quoted(self):
        line = self.h.spawn_line("proj/dev", "opus", "high",
                                 "don't; rm -rf /", "auto",
                                 env=["API_KEY=a b'c"])
        import shlex
        words = shlex.split(line)
        # the whole nasty title is ONE word, so the shell never sees `rm -rf /`
        self.assertIn("proj/dev: don't; rm -rf /", words)
        self.assertIn("API_KEY=a b'c", words)

    def test_codex_launch_line(self):
        line = self.h.spawn_line("proj/dev", "gpt-5.6-sol", "low", "probe",
                                 "auto", program="codex", sandbox="read-only")
        self.assertEqual(line, (
            "CC_IMESSAGE_SKIP=1 CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1 "
            "HERDLET_ID=proj/dev codex -m gpt-5.6-sol "
            "-c model_reasoning_effort=low --sandbox read-only -a on-request"))
        self.assertNotIn("probe", line)

    def test_codex_launch_line_defaults_to_workspace_write(self):
        line = self.h.spawn_line("proj/dev", "gpt-5.6-sol", "low", "probe",
                                 "auto", program="codex")
        self.assertIn("--sandbox workspace-write", line)

    def test_codex_launch_line_uses_selected_approval(self):
        line = self.h.spawn_line("proj/dev", "gpt-5.6-sol", "low", "probe",
                                 "auto", program="codex", approval="never")
        self.assertIn("-a never", line)

    def test_codex_prompt_matcher(self):
        self.assertTrue(self.h.codex_prompt_ready("Ask Codex to do anything\n"))
        self.assertTrue(self.h.codex_prompt_ready("  › Ask Codex to do anything  \n"))
        self.assertTrue(self.h.codex_prompt_ready("ASK CODEX TO DO ANYTHING\n"))
        self.assertFalse(self.h.codex_prompt_ready("› 1. Yes, continue\n2. No, quit"))

    def test_codex_trust_prompt_matcher(self):
        menu = ("Do you trust the contents of this directory?\n\n"
                "› 1. Yes, continue\n  2. No, quit\n")
        self.assertTrue(self.h.codex_trust_prompt(menu))
        self.assertFalse(self.h.codex_trust_prompt("› Ask Codex to do anything\n"))

    def test_extra_env_comes_before_the_program(self):
        line = self.h.spawn_line("p/d", "sonnet", "low", "t", "auto",
                                 env=["FOO=bar", "BAZ=qux"])
        self.assertLess(line.index("FOO=bar"), line.index("claude"))
        self.assertLess(line.index("BAZ=qux"), line.index("claude"))

    def test_stub_program_replaces_the_claude_flags(self):
        line = self.h.spawn_line("p/d", "sonnet", "low", "t", "auto",
                                 program="sleep", program_args=["300"])
        self.assertTrue(line.endswith("sleep 300"))
        self.assertNotIn("--model", line)
        self.assertIn("HERDLET_ID=p/d", line)

    def test_split_argv(self):
        self.assertEqual(
            self.h.spawn_split_argv("%3", "/tmp", "LINE"),
            ("split-window", "-d", "-h", "-P", "-F", "#{pane_id}",
             "-t", "%3", "-c", "/tmp", "LINE"))
        self.assertIn("-v", self.h.spawn_split_argv("%3", "/tmp", "LINE", True))

    def test_new_window_fallback_argv(self):
        self.assertEqual(
            self.h.spawn_window_argv("work", "proj/dev", "/tmp", "LINE"),
            ("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", "work",
             "-n", "proj/dev", "-c", "/tmp", "LINE"))

    def test_title_comes_from_the_brief_heading(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "b.md")
            with open(path, "w") as fh:
                fh.write("\n\n#  Rewrite the   parser  \n\nsome text\n")
            self.assertEqual(self.h.brief_title(path), "Rewrite the parser")
            self.assertIsNone(self.h.brief_title(os.path.join(d, "missing.md")))

    def test_model_cell(self):
        self.assertEqual(self.h.model_cell({"model": "opus", "effort": "high"}),
                         "opus/high")
        self.assertEqual(self.h.model_cell({"model": "opus"}), "opus")
        self.assertEqual(self.h.model_cell({}), "")


class SpawnAllowlistTest(unittest.TestCase):
    def setUp(self):
        self.h = _load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_claude_allowlist_merges_and_is_idempotent(self):
        directory = os.path.join(self.tmp.name, ".claude")
        os.makedirs(directory)
        path = os.path.join(directory, "settings.local.json")
        with open(path, "w") as fh:
            json.dump({"keep": True, "permissions": {"allow": ["Read"]}}, fh)

        touched = self.h.write_spawn_allowlist(
            "claude", self.tmp.name, ["git status", "pnpm test", "git status"])
        self.assertEqual(touched, path)
        with open(path) as fh:
            first = fh.read()
        self.assertEqual(json.loads(first), {
            "keep": True,
            "permissions": {"allow": [
                "Read", "Bash(git status:*)", "Bash(pnpm test:*)"]},
        })
        self.assertIn('\n  "permissions": {\n    "allow": [', first)
        self.assertTrue(first.endswith("\n"))

        self.assertIsNone(self.h.write_spawn_allowlist(
            "claude", self.tmp.name, ["git status", "pnpm test"]
        ))
        with open(path) as fh:
            self.assertEqual(fh.read(), first)

    def test_codex_allowlist_appends_argv_rules_and_is_idempotent(self):
        directory = os.path.join(self.tmp.name, ".codex", "rules")
        os.makedirs(directory)
        path = os.path.join(directory, "herdlet.rules")
        with open(path, "w") as fh:
            fh.write('prefix_rule(pattern=["rg"], decision="allow")\n')

        touched = self.h.write_spawn_allowlist(
            "codex", self.tmp.name,
            ["git status", "git log --format='%h %s'", "git status"])
        self.assertEqual(touched, path)
        with open(path) as fh:
            first = fh.read()
        self.assertEqual(first.splitlines(), [
            'prefix_rule(pattern=["rg"], decision="allow")',
            'prefix_rule(pattern=["git", "status"], decision="allow")',
            'prefix_rule(pattern=["git", "log", "--format=%h %s"], decision="allow")',
        ])

        self.assertIsNone(self.h.write_spawn_allowlist(
            "codex", self.tmp.name, ["git status", "git log --format='%h %s'"]
        ))
        with open(path) as fh:
            self.assertEqual(fh.read(), first)


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _DaemonCase(unittest.TestCase):
    """A private daemon plus the module in-process, so tmux can be faked."""

    def setUp(self):
        self.h = _load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sock = os.path.join(self.tmp.name, "d.sock")
        self.daemon = subprocess.Popen(
            [sys.executable, BIN, "--socket", self.sock, "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._stop)
        for _ in range(50):
            if os.path.exists(self.sock):
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("daemon did not start")

    def _stop(self):
        self.daemon.terminate()
        self.daemon.wait(timeout=5)

    def record(self, agent_id):
        return self.h.call(self.sock, "agent.get", {"id": agent_id}).get("result")


class SpawnCommandTest(_DaemonCase):
    """cmd_spawn end to end with tmux and send_text faked out."""

    def setUp(self):
        super().setUp()
        self.h.os.environ["TMUX_PANE"] = "%0"
        self.addCleanup(self.h.os.environ.pop, "TMUX_PANE", None)
        self.runs = []
        self.sent = []
        self.h.send_text = lambda pane, text, no_enter=False: self.sent.append((pane, text))
        self.panes = {"%0", "%7"}
        self.h.tmux = self._tmux
        self.h.tmux_run = self._tmux_run

    def _tmux(self, *args, check=False, input=None):
        out = self._tmux_run(*args, input=input)
        return out.stdout if out.returncode == 0 else None

    def _tmux_run(self, *args, input=None, timeout=5):
        self.runs.append(args)
        if args[0] == "split-window":
            return self.split_result()
        if args[0] == "new-window":
            return subprocess.CompletedProcess(args, 0, "%9\n", "")
        if args[0] == "display-message" and "#{session_name}" in args:
            return subprocess.CompletedProcess(args, 0, "work\n", "")
        if args[0] == "display-message" and "#{pane_id}" in args:
            pane = args[args.index("-t") + 1]
            # tmux answers a missing target with success and an empty line
            return subprocess.CompletedProcess(
                args, 0, (pane + "\n") if pane in self.panes else "\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def split_result(self):
        return subprocess.CompletedProcess((), 0, "%7\n", "")

    def args(self, **kw):
        base = dict(socket=self.sock, id="proj/dev", model="opus", effort="high",
                    agent="claude", title=None, brief=None, cwd=None,
                    permission_mode="auto", env=None, vertical=False,
                    ready_timeout=0.5, json=False, program="claude",
                    program_args=None, sandbox=None, approval=None, allow=None)
        base.update(kw)
        return _Args(**base)

    def spawn(self, **kw):
        """cmd_spawn with its output captured: (code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.h.cmd_spawn(self.args(**kw))
        return code, out.getvalue(), err.getvalue()

    def ready_in(self, delay, state="working", pane="%7"):
        threading.Timer(delay, lambda: self.h.call(
            self.sock, "agent.report",
            {"id": "proj/dev", "state": state, "pane": pane})).start()

    def test_registers_before_the_first_hook(self):
        code, out, err = self.spawn()
        rec = self.record("proj/dev")
        self.assertEqual(rec["state"], "spawning")
        self.assertEqual((rec["pane"], rec["model"], rec["effort"]),
                         ("%7", "opus", "high"))
        self.assertEqual(rec["message"], "spawning: worker")
        self.assertEqual(out.strip(), "spawned proj/dev in %7 (opus/high)")
        self.assertEqual(code, 0)   # not ready is not a failure

    def test_codex_registers_and_uses_the_input_prompt_for_readiness(self):
        original = self.h.tmux

        def tmux(*args, **kw):
            if args[0] == "capture-pane":
                return "› Ask Codex to do anything\n"
            return original(*args, **kw)

        self.h.tmux = tmux
        code, out, err = self.spawn(agent="codex", model="gpt-5.6-sol",
                                    effort="low", permission_mode=None,
                                    program=None, sandbox="read-only")
        rec = self.record("proj/dev")
        self.assertEqual(rec["state"], "spawning")
        self.assertEqual(rec["agent"], "codex")
        self.assertEqual((rec["model"], rec["effort"]), ("gpt-5.6-sol", "low"))
        self.assertEqual(err, "")
        self.assertEqual(code, 0)

    def test_ready_worker_gets_its_brief_with_an_absolute_path(self):
        brief = os.path.join(self.tmp.name, "b.md")
        with open(brief, "w") as fh:
            fh.write("# Do the thing\n")
        self.ready_in(0.15)
        code, out, err = self.spawn(brief=os.path.relpath(brief),
                                    cwd="/tmp", ready_timeout=3)
        self.assertEqual(code, 0)
        self.assertEqual(self.sent, [("%7", f"Read {brief} and do it.")])
        self.assertEqual(self.record("proj/dev")["cwd"], "/tmp")
        self.assertEqual(err, "")

    def test_title_and_state_come_from_the_brief_and_survive(self):
        brief = os.path.join(self.tmp.name, "b.md")
        with open(brief, "w") as fh:
            fh.write("# Do the thing\n")
        self.spawn(brief=brief)
        self.assertEqual(self.record("proj/dev")["message"], "spawning: Do the thing")

    def test_a_hook_that_lands_first_is_not_overwritten(self):
        self.h.call(self.sock, "agent.report",
                    {"id": "proj/dev", "state": "working", "pane": "%7",
                     "message": "already going"})
        code, out, err = self.spawn()
        rec = self.record("proj/dev")
        self.assertEqual(rec["state"], "working")   # not pushed back to spawning
        self.assertEqual(rec["model"], "opus")      # but the new fields land
        self.assertEqual(code, 0)

    def test_a_stale_record_on_another_pane_does_not_count_as_ready(self):
        # `ack` turns a finished worker into `idle`, and idle survives MAX_AGE:
        # re-spawning that id must not read the leftover as a live worker and
        # fire the brief into a pane where the agent has not booted
        brief = os.path.join(self.tmp.name, "b.md")
        with open(brief, "w") as fh:
            fh.write("# Do the thing\n")
        self.h.call(self.sock, "agent.report",
                    {"id": "proj/dev", "state": "idle", "pane": "%2",
                     "message": "last run"})
        code, out, err = self.spawn(brief=brief)
        rec = self.record("proj/dev")
        self.assertEqual(rec["state"], "spawning")   # overwritten, not trusted
        self.assertEqual(rec["pane"], "%7")
        self.assertEqual(self.sent, [])              # brief withheld
        self.assertIn("the brief was not sent", err)
        self.assertEqual(code, 0)

    def test_no_space_falls_back_to_a_new_window(self):
        self.split_result = lambda: subprocess.CompletedProcess(
            (), 1, "", "no space for new pane")
        self.panes.add("%9")
        code, out, err = self.spawn()
        self.assertIn("new-window", [r[0] for r in self.runs])
        self.assertEqual(self.record("proj/dev")["pane"], "%9")
        self.assertIn("window full, opened a new window in session work", out)
        self.assertIn("spawned proj/dev in %9 (opus/high)", out)
        self.assertEqual(code, 0)

    def test_timeout_with_a_live_pane_is_not_a_failure(self):
        code, out, err = self.spawn()
        self.assertIn("warning: proj/dev did not report in 0.5s; "
                      "pane %7 is alive, peek/approve it by pane id", err)
        self.assertNotIn("the brief was not sent", err)  # no brief was given
        self.assertEqual(code, 0)

    def test_timeout_with_a_brief_says_the_brief_was_not_sent(self):
        brief = os.path.join(self.tmp.name, "b.md")
        with open(brief, "w") as fh:
            fh.write("# Do the thing\n")
        code, out, err = self.spawn(brief=brief)
        self.assertIn("; the brief was not sent, send it once the prompt is "
                      "cleared", err)
        self.assertEqual(self.sent, [])
        self.assertEqual(code, 0)

    def test_timeout_with_a_dead_pane_is_a_failure(self):
        self.panes.discard("%7")
        code, out, err = self.spawn()
        self.assertIn("herdlet: pane %7 is gone: the launch command exited; "
                      "check the model/effort flags", err)
        self.assertEqual(code, 1)

    def test_ready_worker_never_checks_the_pane(self):
        self.ready_in(0.15, state="idle")
        code, out, err = self.spawn(ready_timeout=3)
        self.assertEqual(code, 0)
        self.assertNotIn("display-message",
                         [r[0] for r in self.runs if "#{pane_id}" in r])

    def test_zero_ready_timeout_skips_the_wait(self):
        # timeout_ms 0 means "no timeout" to the daemon, so the wait must not
        # be issued at all
        code, out, err = self.spawn(ready_timeout=0)
        self.assertEqual(self.record("proj/dev")["state"], "spawning")
        self.assertIn("did not report in 0s", err)
        self.assertEqual(code, 0)

    def test_zero_ready_timeout_still_sees_a_worker_that_is_already_up(self):
        self.h.call(self.sock, "agent.report",
                    {"id": "proj/dev", "state": "working", "pane": "%7"})
        code, out, err = self.spawn(ready_timeout=0)
        self.assertEqual(err, "")
        self.assertEqual(code, 0)

    def as_spawner(self, spawner="proj/master"):
        self.h.os.environ["HERDLET_ID"] = spawner
        self.addCleanup(self.h.os.environ.pop, "HERDLET_ID", None)
        self.h.call(self.sock, "agent.report",
                    {"id": spawner, "state": "working", "pane": "%0"})
        return spawner

    def test_the_spawner_is_linked_downward_to_what_it_spawned(self):
        # a spawned master is a worker to the scope check, so without the link
        # it could not `send` to its own children. one-directional: the child
        # gets no shortcut back into its master's pane
        spawner = self.as_spawner()
        brief = os.path.join(self.tmp.name, "b.md")
        with open(brief, "w") as fh:
            fh.write("# Do the thing\n")
        self.spawn(brief=brief)
        self.assertEqual(self.record(spawner)["peers"], ["proj/dev"])
        self.assertEqual(self.record(spawner)["topics"], {"proj/dev": brief})
        self.assertEqual(self.record("proj/dev")["peers"], [])
        self.assertEqual(self.record("proj/dev")["topics"], {})
        self.assertEqual(self.h.peer_scope(self.sock, "proj/dev"),
                         (spawner, brief))

    def test_a_spawned_child_cannot_send_up_to_its_spawner(self):
        spawner = self.as_spawner()
        self.spawn()
        self.h.os.environ["HERDLET_ID"] = "proj/dev"
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as exc:
            self.h.peer_scope(self.sock, spawner)
        self.assertEqual(exc.exception.code, 3)
        self.assertIn(f"not paired with {spawner}", err.getvalue())

    def test_a_briefless_spawn_gets_a_default_topic(self):
        spawner = self.as_spawner()
        self.spawn(cwd="/tmp")
        self.assertEqual(self.record(spawner)["topics"],
                         {"proj/dev": "/tmp/plans/proj-dev-thread.md"})

    def test_an_unregistered_spawner_is_not_paired(self):
        # a human-launched master has no record and no HERDLET_ID: unrestricted
        code, out, err = self.spawn()
        self.assertEqual(code, 0)
        self.assertEqual(self.record("proj/dev")["peers"], [])
        self.assertEqual(err.count("herdlet:"), 0)

    def test_json_payload(self):
        brief = os.path.join(self.tmp.name, "b.md")
        with open(brief, "w") as fh:
            fh.write("# Do the thing\n")
        self.ready_in(0.15)
        code, out, err = self.spawn(brief=brief, json=True, ready_timeout=3)
        payload = json.loads(out)["result"]
        self.assertEqual(payload, {
            "type": "spawned", "id": "proj/dev", "pane": "%7", "model": "opus",
            "effort": "high", "title": "Do the thing", "cwd": os.getcwd(),
            "ready": True, "brief_sent": True, "note": None})
        self.assertEqual(code, 0)

    def test_json_payload_reports_a_withheld_brief(self):
        brief = os.path.join(self.tmp.name, "b.md")
        with open(brief, "w") as fh:
            fh.write("# Do the thing\n")
        code, out, err = self.spawn(brief=brief, json=True)
        payload = json.loads(out)["result"]
        self.assertFalse(payload["ready"])
        self.assertFalse(payload["brief_sent"])


class ApproveTimeoutTest(_DaemonCase):
    def setUp(self):
        self.captured = []
        super().setUp()
        self.h.tmux = lambda *a, **kw: self.captured.append(a) or "pane text"
        self.h.call(self.sock, "agent.report",
                    {"id": "ap/one", "state": "blocked", "pane": "%4"})

    def args(self, **kw):
        base = dict(socket=self.sock, id="ap/one", option="1", lines=5, settle=0.0,
                    wait=True, state="done", timeout=0.4, timeout_ok=False)
        base.update(kw)
        return _Args(**base)

    def approve(self, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.h.cmd_approve(self.args(**kw))
        return code, out.getvalue()

    def test_approve_marks_the_record_working_without_wait(self):
        code, out = self.approve(wait=False)
        record = self.record("ap/one")
        self.assertEqual(record["state"], "working")
        self.assertEqual(record["message"], "approved option 1")
        self.assertEqual(code, 0)

    def test_wait_timeout_exits_2_by_default(self):
        code, out = self.approve()
        self.assertIn("state: timeout", out)
        self.assertEqual(code, 2)

    def test_timeout_ok_exits_0(self):
        code, out = self.approve(timeout_ok=True)
        self.assertIn("state: timeout", out)   # still says so, just exits 0
        self.assertEqual(code, 0)

    def test_a_hung_daemon_still_fails_under_timeout_ok(self):
        real = self.h.call

        def hang(sock, method, params, timeout=5.0):
            if method == "wait":
                raise TimeoutError("hung")
            return real(sock, method, params, timeout)

        self.h.call = hang
        self.assertEqual(self.approve(timeout_ok=True)[0], 2)


class TimeoutResultTest(unittest.TestCase):
    def setUp(self):
        self.h = _load_module()

    def test_non_timeout_responses_pass_through(self):
        resp = {"id": "1", "result": {"type": "waited"}}
        self.assertIs(self.h.timeout_as_result(resp), resp)
        err = {"id": "1", "error": {"code": "not_found"}}
        self.assertIs(self.h.timeout_as_result(err), err)

    def test_match_timeout_keeps_the_regex(self):
        out = self.h.timeout_as_result(
            {"error": {"code": "timeout", "id": "dev", "match": "ERROR"}})
        self.assertEqual(out["result"], {"type": "timeout", "agents": ["dev"],
                                         "states": [], "match": "ERROR"})


class TranscriptParseTest(unittest.TestCase):
    def setUp(self):
        self.h = _load_module()

    def rows(self, *entries):
        lines = []
        for stamp, blocks in entries:
            lines.append(json.dumps({"type": "assistant", "timestamp": stamp,
                                     "message": {"role": "assistant",
                                                 "content": blocks}}))
        return "\n".join(lines) + "\n"

    def write(self, body):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "t.jsonl")
        with open(path, "w") as fh:
            fh.write(body)
        return path

    def test_only_text_blocks_oldest_first(self):
        path = self.write(self.rows(
            ("t1", [{"type": "text", "text": "one"}]),
            ("t2", [{"type": "tool_use", "name": "Bash", "input": {}}]),
            ("t3", [{"type": "thinking", "thinking": "secret"},
                    {"type": "text", "text": "two"},
                    {"type": "text", "text": "three"}])))
        self.assertEqual(self.h.transcript_messages(path, 5),
                         [("t1", "one"), ("t3", "two\nthree")])

    def test_count_takes_the_tail(self):
        path = self.write(self.rows(
            ("t1", [{"type": "text", "text": "one"}]),
            ("t2", [{"type": "text", "text": "two"}])))
        self.assertEqual(self.h.transcript_messages(path, 1), [("t2", "two")])

    def test_codex_messages_are_oldest_first_and_deduplicated(self):
        path = self.write("\n".join((
            json.dumps({"timestamp": "t1", "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "one"},
                    {"type": "reasoning", "summary": "hidden"},
                    {"type": "output_text", "text": "two"}]}}),
            json.dumps({"timestamp": "t2", "type": "event_msg", "payload": {
                "type": "task_complete", "last_agent_message": "one\ntwo"}}),
            json.dumps({"timestamp": "t3", "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "three"}]}}),
            json.dumps({"timestamp": "t4", "type": "event_msg", "payload": {
                "type": "task_complete", "last_agent_message": "four"}}),
        )) + "\n")
        self.assertEqual(self.h.transcript_messages(path, 5),
                         [("t1", "one\ntwo"), ("t3", "three"), ("t4", "four")])

    def test_mixed_formats_and_garbage_lines_survive(self):
        path = self.write(
            json.dumps({"type": "assistant",
                        "message": {"role": "assistant", "content": "plain"}}) + "\n"
            + "not json\n\n"
            + json.dumps({"timestamp": "t2", "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "codex"}]}}) + "\n"
            + json.dumps({"type": "user", "message": {"role": "user",
                                                      "content": "hi"}}) + "\n")
        self.assertEqual(self.h.transcript_messages(path, 5),
                         [(None, "plain"), ("t2", "codex")])


if __name__ == "__main__":
    unittest.main()
