import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
from scripts import load_script  # noqa: E402
from agentmail import config, memwatch  # noqa: E402

G = 1 << 30
S = memwatch.settings(config.load())
RUN_A = {"cli": "claude", "id": "c0c0c0c0-0000", "name": "run-a", "live": True, "pid": 100}
RM = {"cli": "claude", "id": "rm-0000", "name": "rm-280", "live": True, "pid": 200}


def proc(pid, ppid, comm, gb, cgroup="/user.slice/app.slice/tmux-spawn-1.scope"):
    return {"pid": pid, "ppid": ppid, "comm": comm, "mem": int(gb * G), "cgroup": cgroup}


def use(ram_pct, swap_pct=0.0, total=64 * G, swap_total=16 * G):
    return {"ram_total": total, "ram_used": total * ram_pct / 100, "ram_pct": ram_pct, "swap_total": swap_total,
            "swap_used": swap_total * swap_pct / 100, "swap_pct": swap_pct}


# A night of memory pressure, roughly: an agent's batch, a docker container, two small agents.
PROCS = [proc(100, 1, "claude", 0.4), proc(101, 100, "bash", 0.0), proc(102, 101, "pp", 5.3),
         proc(103, 1, "pp", 5.2, "/user.slice/app.slice/pp-rejudge.scope"),           # nohup'd, own scope
         proc(104, 1, "pp", 5.1),                                                       # nohup'd, agent's pane
         proc(200, 1, "claude", 0.3, "/user.slice/app.slice/tmux-spawn-2.scope"),
         proc(300, 1, "uvicorn", 2.6, "/system.slice/docker-6ac6.scope")]


def own(procs=PROCS, env=None):
    env = {103: RUN_A["id"]} if env is None else env
    return memwatch.owners(procs, {100: RUN_A, 200: RM}, [RUN_A, RM], env_of=env.get, env_min=0)


def sent(state, u, own_, caps, s, now):
    """memwatch.decide, then every action it returns recorded as sent: agent-memwatch's pass when each send works."""
    acts, st = memwatch.decide(state, u, own_, caps, s, now)
    return acts, memwatch.record(st, [a[-1] for a in acts], now)


class Reading(unittest.TestCase):
    def test_usage_from_meminfo(self):
        with tempfile.NamedTemporaryFile("w", suffix=".meminfo", delete=False) as f:
            f.write("MemTotal:       65536000 kB\nMemFree:  100 kB\nMemAvailable:   13107200 kB\n"
                    "SwapTotal:      16777216 kB\nSwapFree:        8388608 kB\nHugePages_Total:       0\n")
        u = memwatch.usage(memwatch.meminfo(f.name))
        self.assertAlmostEqual(u["ram_pct"], 80.0)
        self.assertAlmostEqual(u["swap_pct"], 50.0)
        self.assertEqual(u["ram_used"], (65536000 - 13107200) * 1024)

    def test_reads_this_machine(self):
        procs = memwatch.processes()
        self.assertTrue(any(p["pid"] == 1 or p["comm"] for p in procs))
        me = [p for p in procs if p["comm"].startswith("python")]
        self.assertTrue(me and all(p["cgroup"].startswith("/") for p in me))


class Attribution(unittest.TestCase):
    def test_batch_is_charged_to_the_agent_that_started_it(self):
        o = own()
        self.assertEqual(o[0]["label"], "claude run-a")
        # ancestry (102), environment after nohup into its own scope (103), the agent's pane cgroup (104)
        self.assertAlmostEqual(o[0]["mem"] / G, 0.4 + 5.3 + 5.2 + 5.1, places=3)
        self.assertEqual(o[0]["top"][0][:2], ("pp", 3))
        self.assertEqual(o[1]["label"], "docker-6ac6.scope")
        self.assertIsNone(o[1]["session"])
        self.assertEqual(o[2]["label"], "claude rm-280")

    def test_without_the_environment_a_scoped_orphan_falls_back_to_its_unit(self):
        o = own(env={})
        self.assertIn("pp-rejudge.scope", [g["label"] for g in o])

    def test_codex_matched_by_working_directory(self):
        cx = {"cli": "codex", "id": "01a0", "name": "loop", "live": True, "cwd": "/w"}
        procs = [proc(400, 1, "codex", 0.2), proc(401, 1, "codex", 0.2)]
        got = memwatch.session_pids([cx], procs, cwd_of={400: "/w", 401: "/other"}.get)
        self.assertEqual(got, {400: cx})


class Decisions(unittest.TestCase):
    def test_quiet_below_warn(self):
        self.assertEqual(memwatch.decide({}, use(70), own(), [], S, 0)[0], [])

    def test_warn_pings_the_majority_holder_once_per_half_hour(self):
        u = use(82)
        acts, st = sent({}, u, own(), [], S, 1000)
        self.assertEqual([(a[0], a[1]["label"], a[2]) for a in acts], [("ping", "claude run-a", 1)])
        self.assertIn("16.0 GB", acts[0][3])
        self.assertIn("pp x3", acts[0][3])
        self.assertEqual(sent(st, u, own(), [], S, 1000 + 29 * 60)[0], [])
        self.assertEqual(len(sent(st, u, own(), [], S, 1000 + 31 * 60)[0]), 1)

    def test_urgent_pings_again_and_emails_once_an_hour(self):
        _, st = sent({}, use(82), own(), [], S, 1000)
        acts, st = sent(st, use(91), own(), [], S, 1060)
        self.assertEqual([a[0] for a in acts], ["ping", "email"])
        self.assertIn("URGENT", acts[0][3])
        self.assertIn("it can kill the user manager, which ends every session.", acts[0][3])
        self.assertIn("claude run-a holds 16.0 GB", acts[1][1])
        self.assertIn("docker-6ac6.scope", acts[1][2])
        self.assertIn("Messaged: claude run-a.", acts[1][2])
        self.assertEqual(sent(st, use(91), own(), [], S, 1060 + 1800)[0][0][0], "ping")
        self.assertNotIn("email", [a[0] for a in sent(st, use(91), own(), [], S, 1060 + 1800)[0]])

    def test_swap_half_full_makes_a_warning_urgent(self):
        self.assertEqual(memwatch.level(use(82, swap_pct=55), S), 2)
        self.assertEqual(memwatch.level(use(82, swap_pct=40), S), 1)
        self.assertEqual(memwatch.level(use(82, swap_pct=55, swap_total=0), S), 1)

    def test_swap_alone_is_quiet(self):
        # A whole night: RAM 13-19%, swap 50-61% left over from earlier pressure, no memory pressure.
        # Counting swap alone as urgent emailed Alex 16 times, once an hour, and pinged two agents 12 times.
        self.assertEqual(memwatch.level(use(14, swap_pct=61), S), 0)
        self.assertEqual(memwatch.level(use(60, swap_pct=100), S), 0)
        procs = PROCS + [proc(201, 200, "vllm", 3.5, "/user.slice/app.slice/tmux-spawn-2.scope")]
        st, went = {}, []
        for t in range(0, 16 * 3600, 15):
            acts, st = sent(st, use(14, swap_pct=61), own(procs), [], S, t)
            went += acts
        self.assertEqual(went, [])

    def test_rearms_below_75(self):
        _, st = sent({}, use(82), own(), [], S, 1000)
        _, st = sent(st, use(70), own(), [], S, 1100)
        self.assertEqual(len(sent(st, use(82), own(), [], S, 1200)[0]), 1)

    def test_rearm_with_swap_still_full(self):
        _, st = sent({}, use(82, swap_pct=60), own(), [], S, 1000)
        _, st = sent(st, use(70, swap_pct=60), own(), [], S, 1100)
        self.assertEqual(st["sent"].keys(), {"email"})

    def test_rearm_keeps_the_email_to_once_an_hour(self):
        acts, st = sent({}, use(91), own(), [], S, 1000)
        self.assertEqual([a[0] for a in acts], ["ping", "email"])
        _, st = sent(st, use(70), own(), [], S, 1100)
        acts, st = sent(st, use(91), own(), [], S, 1200)
        self.assertEqual([a[0] for a in acts], ["ping"])
        self.assertIn("email", [a[0] for a in sent(st, use(91), own(), [], S, 1000 + 3600)[0]])

    def test_email_says_when_no_agent_was_messaged(self):
        procs = [proc(300, 1, "uvicorn", 40, "/system.slice/docker-6ac6.scope"), proc(200, 1, "claude", 1.0)]
        acts, _ = memwatch.decide({}, use(92), own(procs), [], S, 0)
        self.assertEqual([a[0] for a in acts], ["email"])
        self.assertIn("none was messaged", acts[0][2])
        self.assertNotIn("Messaged:", acts[0][2])

    def test_small_agents_and_non_agents_are_not_messaged(self):
        procs = [proc(300, 1, "uvicorn", 40, "/system.slice/docker-6ac6.scope"), proc(200, 1, "claude", 1.0)]
        acts, _ = memwatch.decide({}, use(85), own(procs), [], S, 0)
        self.assertEqual(acts, [])

    def test_cap_warning_independent_of_desktop_level(self):
        g = own()[0]
        acts, st = sent({}, use(40), own(), [(g, 29 * G, 32 * G)], S, 0)
        self.assertEqual([(a[0], a[2]) for a in acts], [("ping", "cap")])
        self.assertIn("29.0 GB of its 32.0 GB", acts[0][3])
        self.assertEqual(sent({}, use(40), own(), [(g, 20 * G, 32 * G)], S, 0)[0], [])
        _, st = sent(st, use(40), own(), [], S, 60)  # a re-arm keeps the cap's repeat limit
        self.assertIn("cap:" + g["key"], st["sent"])

    def test_only_what_was_sent_is_recorded(self):
        """An urgent email that agent-mail failed to send is due again on the next pass; the ping that went is not."""
        acts, st = memwatch.decide({}, use(91), own(), [], S, 1000)
        self.assertEqual([(a[0], a[-1]) for a in acts], [("ping", "session:%s:2" % RUN_A["id"]), ("email", "email")])
        self.assertEqual(st, {"sent": {}})   # deciding records nothing
        st = memwatch.record(st, [acts[0][-1]], 1000)
        self.assertEqual([a[0] for a in memwatch.decide(st, use(91), own(), [], S, 1015)[0]], ["email"])


class Command(unittest.TestCase):
    def test_dry_run_json_on_this_machine(self):
        r = subprocess.run([sys.executable, str(ROOT / "bin" / "agent-memwatch"), "--dry-run", "--json"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertIn(out["level"], (0, 1, 2))
        self.assertGreater(out["usage"]["ram_total"], 0)

    def test_act_sends_through_agent_send_and_agent_mail(self):
        mw = load_script("agent-memwatch")
        o = own()
        calls = []
        # agent-send and agent-mail, which would reach a live session and Gmail
        with mock.patch.object(mw.subprocess, "run", side_effect=lambda cmd, **k: calls.append((cmd, k.get("input")))
                               or subprocess.CompletedProcess(cmd, 0, "ok", "")), \
                contextlib.redirect_stdout(io.StringIO()):
            done = mw.act([("ping", o[0], 1, "hello", "k1"), ("email", "subj", "body", "email")],
                          dict(config.load(), allowlist=["t@x"]))
        self.assertEqual(done, ["k1", "email"])
        self.assertEqual(calls[0][0][1:], ["--from", "agent-memwatch", RUN_A["id"], "hello"])
        # agent-mail runs under this interpreter (the kit's venv, which holds the Google client libraries)
        self.assertEqual(calls[1][0][0], sys.executable)
        self.assertEqual(calls[1][0][2:], ["send", "--unrouted", "--alert", "memory", "--to", "t@x", "--subject",
                                           "[agent-memwatch] subj"])
        self.assertEqual(calls[1][1], "body")

    def test_a_ping_a_codex_agent_holds_in_its_queue_counts_as_sent(self):
        """agent-send exits HELD: the ping is in the agent's queue and arrives at its next turn, so it is recorded as
        sent and not sent again on the next pass."""
        mw = load_script("agent-memwatch")
        # agent-send, which would reach a live session
        held = "held in Codex's queue for codex e6\nits last turn was interrupted"
        with mock.patch.object(mw.subprocess, "run",
                               return_value=subprocess.CompletedProcess(["agent-send"], mw.deliver.HELD, held, "")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            done = mw.act([("ping", own()[0], 1, "hello", "k1")], dict(config.DEFAULTS))
        self.assertEqual(done, ["k1"])
        self.assertIn("its last turn was interrupted", out.getvalue())

    def test_a_pass_saves_only_the_sends_that_worked(self):
        """agent-mail fails (exit 1): the urgent email is not saved as sent, so the next pass tries it again."""
        mw = load_script("agent-memwatch")
        state = mw.jsonfile.JsonFile(Path(tempfile.mkdtemp()) / "memwatch.json", mw.jsonfile.PLAIN, None)

        def run(cmd, **kw):   # agent-send and agent-mail: they reach live agents and Gmail
            return subprocess.CompletedProcess(cmd, 1 if "send" in cmd[1:3] else 0, "", "boom")
        with mock.patch.object(mw, "sample", return_value=(use(91), own(), [])), \
                mock.patch.object(mw.subprocess, "run", side_effect=run), mock.patch.object(mw, "STATE", state), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mw.main(["--once"]), 0)
        self.assertEqual(list(state.read()["sent"]), ["session:%s:2" % RUN_A["id"]])


if __name__ == "__main__":
    unittest.main()
