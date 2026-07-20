import json
import os
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
                          "PostToolUse", "Notification", "Stop"):
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


if __name__ == "__main__":
    unittest.main()
