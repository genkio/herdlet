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

        self.run_cli("hook", stdin=json.dumps({"hook_event_name": "SessionEnd"}), env_extra=env)
        proc = self.run_cli("get", "--id", "hk1")
        self.assertEqual(proc.returncode, 1)

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


if __name__ == "__main__":
    unittest.main()
