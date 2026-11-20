"""Tests for claude-reports: python3 -m unittest discover tests

Synthetic transcripts in a temporary folder, a fake tmux and a fake browser on PATH.
"""

import http.client
import importlib.machinery
import importlib.util
import json
import os
import socket
import subprocess
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "claude-reports"


def load_module():
    loader = importlib.machinery.SourceFileLoader("claude_reports", str(SCRIPT))
    spec = importlib.util.spec_from_loader("claude_reports", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


cr = load_module()

SID = "11111111-2222-3333-4444-555555555555"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def rec(kind, content, ts="2026-11-20T10:00:00.000Z", **extra):
    return {"type": kind, "message": {"role": kind, "content": content}, "timestamp": ts,
            "cwd": "/home/u/proj", "gitBranch": "main", "entrypoint": "cli",
            "sessionId": SID, **extra}


def prompt(text, ts="2026-11-20T10:00:00.000Z", **extra):
    return rec("user", text, ts, origin={"kind": "human"}, **extra)


def say(text, ts="2026-11-20T10:01:00.000Z"):
    return rec("assistant", [{"type": "text", "text": text}], ts)


def tool(name, ts="2026-11-20T10:01:00.000Z", **inp):
    return rec("assistant", [{"type": "tool_use", "name": name, "input": inp}], ts)


def result(ts="2026-11-20T10:01:30.000Z"):
    return rec("user", [{"type": "tool_result", "content": "ok"}], ts)


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "state"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.log"
        # tmux: answers `display` for the pane, logs everything else.
        self.fake("tmux", f"""#!/bin/sh
echo "tmux $*" >> {self.log}
case "$1" in
    display) us=$(printf '\\037'); echo "work${{us}}2${{us}}1${{us}}${{FAKE_ACTIVE:-11}}${{us}}✳ Fix the login bug" ;;
    list-clients)
        case "$*" in
            *client_flags*) printf 'work\\t%s\\n' "${{FAKE_FLAGS:-attached,UTF-8}}" ;;
            *client_activity*) printf '100\\t/dev/pts/1\\t1\\n' ;;
            *) echo /dev/pts/1 ;;
        esac ;;
    list-panes) printf '%%7\\twork:2.1\\n' ;;
esac
""")
        self.fake("browser", f'#!/bin/sh\necho "browser $*" >> {self.log}\n')
        self.fake("qdbus6", f'#!/bin/sh\necho "qdbus6 $*" >> {self.log}\n')
        self.tasks = self.tmp / "tasks"
        self.tasks.mkdir()
        self.port = free_port()
        self.env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
                    "CLAUDE_REPORTS_DIR": str(self.state),
                    "CLAUDE_REPORTS_BROWSER": str(self.bin / "browser"),
                    "CLAUDE_REPORTS_ASSETS": str(self.tmp / "no-assets"),
                    "TMUX_PANE": "%7", "TMUX": "/tmp/fake,1,0",
                    "CLAUDE_TASKS_DIR": str(self.tasks), "CLAUDE_REPORTS_PORT": str(self.port),
                    "CLAUDE_REPORTS_IDLE": "3"}
        self.env.pop("CLAUDE_REPORTS_FOCUS", None)

    def fake(self, name, body):
        p = self.bin / name
        p.write_text(body)
        p.chmod(0o755)

    def transcript(self, records, sid=SID):
        p = self.tmp / f"{sid}.jsonl"
        p.write_text("".join(json.dumps(r) + "\n" for r in records) + '{"half-written')
        return p

    def run_cr(self, *args, stdin="", env=None):
        return subprocess.run([str(SCRIPT), *args], input=stdin, capture_output=True, text=True,
                              env={**self.env, **(env or {})}, timeout=20)

    def hook(self, records, sid=SID, env=None, **payload):
        t = self.transcript(records, sid)
        payload = {"session_id": sid, "transcript_path": str(t), "hook_event_name": "Stop",
                   **payload}
        return self.run_cr("hook", stdin=json.dumps(payload), env=env)

    def meta(self, sid=SID):
        return json.loads((self.state / f"{sid}.json").read_text())

    def md(self, sid=SID):
        return (self.state / f"{sid}.md").read_text()

    def calls(self):
        return self.log.read_text() if self.log.exists() else ""

    def mark_open(self, sid=SID, pane="7"):
        """Record the session as open in a pane, the way claude-tasks does (a live process)."""
        pid = os.getpid()
        stat = Path(f"/proc/{pid}/stat").read_text()
        start = stat[stat.rindex(")") + 2:].split()[19]
        (self.tasks / pane).write_text(f"running\t0\t/home/u/proj\t{sid}\t{pid} {start}\n")


class Parse(Env):
    def test_final_reply_is_text_after_last_tool_call(self):
        s = cr.parse(self.transcript([
            prompt("fix it"),
            say("Looking at the code."),
            tool("Edit", file_path="/home/u/proj/a.py"),
            result(),
            say("Done: fixed a.py."),
            say("Also ran the tests."),
        ]))
        self.assertEqual(s["final"], ["Done: fixed a.py.", "Also ran the tests."])
        self.assertEqual(list(s["files"]), ["/home/u/proj/a.py"])
        self.assertFalse(s["pending_tool"] or s["interrupted"])

    def test_noise_is_not_a_prompt(self):
        s = cr.parse(self.transcript([
            prompt("<command-name>/effort</command-name><command-args></command-args>"),
            prompt("<command-name>/rename</command-name><command-args>x</command-args>"),
            prompt("<local-command-stdout>ok</local-command-stdout>"),
            prompt("<system-reminder>hi</system-reminder>real question"),
            rec("user", "meta", isMeta=True),
            rec("user", "from a hook", origin={"kind": "hook"}),
            prompt("<pasted_content id=\"a1\">pasted</pasted_content>"),
        ]))
        self.assertEqual(s["prompts"], ["real question", "pasted"])

    def test_titles(self):
        s = cr.parse(self.transcript([
            {"type": "ai-title", "aiTitle": "AI title"},
            {"type": "custom-title", "customTitle": "my-name"},
            prompt("q"),
        ]))
        self.assertEqual(s["titles"], {"ai": "AI title", "custom": "my-name"})

    def test_ended_mid_tool_call_is_unfinished(self):
        s = cr.parse(self.transcript([prompt("go"), say("Starting."), tool("Bash", command="x")]))
        self.assertTrue(s["pending_tool"])
        self.assertEqual(s["final"], [])
        self.assertEqual(s["said"], "Starting.")

    def test_interrupted(self):
        s = cr.parse(self.transcript([prompt("go"), say("Hm"),
                                      prompt("[Request interrupted by user]")]))
        self.assertTrue(s["interrupted"])
        self.assertEqual(s["prompts"], ["go"])

    def test_new_prompt_resets_reply(self):
        s = cr.parse(self.transcript([prompt("one"), say("first answer"), prompt("two"),
                                      say("second answer")]))
        self.assertEqual(s["final"], ["second answer"])


class Build(Env):
    def parsed(self, records):
        return cr.parse(self.transcript(records))

    def test_title_priority(self):
        s = self.parsed([{"type": "ai-title", "aiTitle": "AI"}, prompt("first line\nmore")])
        self.assertEqual(cr.build(SID, s, {"pane_title": "Pane"}, 0)[0]["title"], "AI")
        s["titles"] = {"custom": "Mine", "ai": "AI"}
        self.assertEqual(cr.build(SID, s, {"pane_title": "Pane"}, 0)[0]["title"], "Mine")
        s["titles"] = {}
        self.assertEqual(cr.build(SID, s, {"pane_title": "Pane"}, 0)[0]["title"], "Pane")
        self.assertEqual(cr.build(SID, s, {}, 0)[0]["title"], "first line")

    def test_report_is_about_the_latest_prompt(self):
        s = self.parsed([prompt("fix it", "2026-11-20T10:00:00.000Z"),
                         tool("Write", file_path="/home/u/proj/old.py"), result(),
                         say("Fixed."),
                         prompt("and this?", "2026-11-20T10:15:00.000Z"),
                         tool("Write", file_path="/home/u/proj/src/b.py"),
                         tool("Edit", file_path="/etc/x.conf"), result(),
                         say("Also done.", "2026-11-20T10:20:00.000Z")])
        ended = datetime.fromisoformat("2026-11-20T10:21:00+00:00").timestamp()
        meta, md = cr.build(SID, s, {"target": "work:2.1", "session": "work"}, ended)
        self.assertEqual(meta["files"], ["src/b.py", "/etc/x.conf"])  # this turn only
        self.assertFalse(meta["unfinished"])
        self.assertIn("| **tmux** | `work:2.1` |", md)
        self.assertIn("(21 min), prompt 2", md)
        self.assertIn("## Asked\n\n> and this?", md)
        self.assertIn("## Report\n\nAlso done.", md)
        self.assertIn("## Earlier in this session\n\n### 1. fix it\n\nFixed.", md)
        self.assertNotIn("old.py", md)
        self.assertLess(md.index("## Report"), md.index("## Files changed"))

    def test_last_message_from_hook_wins_when_transcript_lags(self):
        s = self.parsed([prompt("go"), say("Checking."), tool("Bash", command="x"), result()])
        meta, md = cr.build(SID, s, {}, 0, last_message="All green.")
        self.assertFalse(meta["unfinished"])
        self.assertIn("## Report\n\nAll green.", md)

    def test_transcript_reply_kept_when_it_ends_with_last_message(self):
        s = self.parsed([prompt("go"), tool("Bash", command="x"), result(),
                         say("Part one."), say("Part two.")])
        md = cr.build(SID, s, {}, 0, last_message="Part two.")[1]
        self.assertIn("Part one.\n\nPart two.", md)

    def test_unfinished_shows_last_words(self):
        s = self.parsed([prompt("go"), say("Working on it."), tool("Bash", command="x")])
        meta, md = cr.build(SID, s, {}, 0)
        self.assertTrue(meta["unfinished"])
        self.assertIn("**Unfinished:**", md)
        self.assertIn("Last thing Claude wrote", md)
        self.assertIn("Working on it.", md)

    def test_background_tasks_note(self):
        s = self.parsed([prompt("go"), say("Started the build, will report back.")])
        meta, md = cr.build(SID, s, {}, 0, last_message="Started the build, will report back.",
                            background=2)
        self.assertEqual(meta["background"], 2)
        self.assertIn("**Still working:** 2 background tasks", md)

    def test_earlier_turns_newest_first_and_capped(self):
        records = []
        for i in range(1, 26):
            records += [prompt(f"q{i}"), tool("Bash", command="x"), result(), say(f"answer {i}")]
        s = self.parsed(records + [prompt("last")])
        turns = cr.earlier_turns(s)
        self.assertEqual(len(turns), cr.MAX_TURNS)
        self.assertEqual(turns[0], {"n": 25, "prompt": "q25", "reply": "answer 25"})
        self.assertEqual(turns[-1]["n"], 6)
        md = cr.build(SID, s, {}, 0)[1]
        self.assertIn("### 25. q25\n\nanswer 25", md)
        self.assertIn("*5 older prompts not shown*", md)

    def test_earlier_reply_falls_back_to_last_words(self):
        s = self.parsed([prompt("one"), say("Let me look."), tool("Bash", command="x"),
                         prompt("[Request interrupted by user]"), prompt("two"), say("ok")])
        self.assertEqual(cr.earlier_turns(s), [{"n": 1, "prompt": "one", "reply": "Let me look."}])

    def test_backticks_and_pipes_in_table(self):
        self.assertEqual(cr.code("a`b"), "``a`b``")
        s = self.parsed([prompt("q"), say("a")])
        s["branch"] = "feat|x"
        md = cr.build(SID, s, {}, 0)[1]
        self.assertIn("feat\\|x", md)


class Hook(Env):
    def test_writes_report_with_tmux_info(self):
        r = self.hook([prompt("fix login"), say("Fixed.")], last_assistant_message="Fixed.")
        self.assertEqual(r.returncode, 0, r.stderr)
        m = self.meta()
        self.assertEqual((m["tmux"], m["tmux_session"], m["title"]), ("work:2.1", "work", "Fix the login bug"))
        self.assertTrue(m["unread"])
        self.assertIn("Fixed.", self.md())
        self.assertIn("refresh-client -S -t /dev/pts/1", self.calls())

    def test_skips_sessions_without_prompts(self):
        self.hook([prompt("<command-name>/model</command-name><command-args></command-args>")])
        self.assertFalse((self.state / f"{SID}.json").exists())

    def test_sdk_runs_only_inside_tmux(self):
        records = [{**prompt("q"), "entrypoint": "sdk-cli"}, say("a")]
        self.hook(records, env={"TMUX_PANE": ""})
        self.assertFalse((self.state / f"{SID}.json").exists())
        self.hook(records)
        self.assertTrue((self.state / f"{SID}.json").exists())

    def test_each_reply_rewrites_and_is_unread_again(self):
        self.hook([prompt("one"), say("first")], last_assistant_message="first")
        self.run_cr("read-all")
        self.assertFalse(self.meta()["unread"])
        self.hook([prompt("one"), say("first"), prompt("two")], last_assistant_message="second")
        self.assertTrue(self.meta()["unread"])
        self.assertIn("## Report\n\nsecond", self.md())
        self.assertEqual(len(list(self.state.glob("*.md"))), 1)
        turns = json.loads((self.state / f"{SID}.turns.json").read_text())
        self.assertEqual(turns, [{"n": 1, "prompt": "one", "reply": "first"}])

    def test_reply_in_the_pane_you_watch_is_read(self):
        self.hook([prompt("q"), say("a")], env={"FAKE_FLAGS": "attached,focused,UTF-8"})
        self.assertFalse(self.meta()["unread"])
        # same focus, but another pane of the window is active
        self.hook([prompt("q"), say("a")], env={"FAKE_FLAGS": "attached,focused", "FAKE_ACTIVE": "01"})
        self.assertTrue(self.meta()["unread"])
        # pane active, but the terminal doesn't have focus
        self.hook([prompt("q"), say("a")], env={"FAKE_FLAGS": "attached,UTF-8"})
        self.assertTrue(self.meta()["unread"])

    def test_vanished_pane_gives_no_tmux_info(self):
        self.fake("tmux", "#!/bin/sh\ncase \"$1\" in display) printf '\\037\\037\\037\\037\\n' ;; esac\n")
        self.hook([prompt("q"), say("a")])
        self.assertEqual(self.meta()["tmux"], "")
        self.assertNotIn("**tmux**", self.md())

    def test_session_end_does_not_rewrite(self):
        self.hook([prompt("q"), say("a")], last_assistant_message="a")
        self.run_cr("read-all")
        self.hook([prompt("q"), say("a")], hook_event_name="SessionEnd")
        self.assertFalse(self.meta()["unread"])

    def test_prunes_old_reports(self):
        self.hook([prompt("q"), say("a")], sid="old")
        m = self.meta("old")
        m["ended"] = time.time() - 30 * 86400
        (self.state / "old.json").write_text(json.dumps(m))
        self.hook([prompt("q"), say("a")])
        self.assertFalse((self.state / "old.json").exists())
        self.assertFalse((self.state / "old.md").exists())

    def test_bad_input_never_fails(self):
        for payload in ("not json", '{"session_id": "../x", "transcript_path": "/etc/passwd"}',
                        json.dumps({"session_id": SID, "transcript_path": "/nope"})):
            r = self.run_cr("hook", stdin=payload)
            self.assertEqual(r.returncode, 0)
        self.assertEqual(list(self.state.glob("*.json")), [])

    def test_errors_are_logged_not_raised(self):
        t = self.transcript([prompt("q")])
        t.chmod(0)
        r = self.run_cr("hook", stdin=json.dumps({"session_id": SID, "transcript_path": str(t)}))
        self.assertEqual(r.returncode, 0)
        if os.geteuid() != 0:
            self.assertIn("PermissionError", (self.state / "errors.log").read_text())


class Commands(Env):
    def setUp(self):
        super().setUp()
        self.hook([prompt("older"), say("a")], sid="aaaa")
        self.hook([prompt("newer"), say("b")], sid="bbbb")

    def test_list_unread_first_then_newest(self):
        self.run_cr("toggle", "bbbb")
        ids = [line.split("\t")[0] for line in self.run_cr("list").stdout.splitlines()]
        self.assertEqual(ids, ["aaaa", "bbbb"])

    def test_count(self):
        self.assertEqual(self.run_cr("count").stdout.strip(), "2")
        self.assertIn("2 reports", self.run_cr("count", "--tmux").stdout)
        self.run_cr("read-all")
        self.assertEqual(self.run_cr("count", "--tmux").stdout, "")

    def test_open_goes_through_the_server(self):
        r = self.run_cr("open", "aaaa", env={"CLAUDE_REPORTS_FOCUS": f"echo focus {{title}} >> {self.log}"})
        self.assertEqual(r.returncode, 0, r.stderr)
        token = (self.state / "token").read_text().strip()
        self.assertEqual(len(token), 64)
        self.assertEqual((self.state / "token").stat().st_mode & 0o777, 0o600)
        for _ in range(50):  # browser and focus run detached
            if "focus" in self.calls():
                break
            time.sleep(0.05)
        self.assertIn(f"browser http://127.0.0.1:{self.port}/r/aaaa?t={token}", self.calls())
        self.assertIn("focus Fix the login bug · claude-report aaaa", self.calls())
        self.assertTrue(self.meta("aaaa")["unread"])  # reading is the button's job now

    def test_open_without_server_falls_back_to_a_file(self):
        with socket.socket() as blocker:  # something else holds the port
            blocker.bind(("127.0.0.1", self.port))
            blocker.listen()
            r = self.run_cr("open", "aaaa")
        self.assertEqual(r.returncode, 0, r.stderr)
        page = self.state / "html" / "aaaa.html"
        self.assertIn(f"browser {page.as_uri()}", self.calls())
        self.assertNotIn("Kill session", page.read_text())
        self.assertFalse(self.meta("aaaa")["unread"])  # no page script to mark it read

    def test_toggle(self):
        self.run_cr("toggle", "aaaa")
        self.assertFalse(self.meta("aaaa")["unread"])
        self.run_cr("toggle", "aaaa")
        self.assertTrue(self.meta("aaaa")["unread"])

    def test_list_shows_open_sessions(self):
        self.mark_open("aaaa")
        rows = dict(line.split("\t", 1) for line in self.run_cr("list").stdout.splitlines())
        self.assertIn("▶ work:2.1", rows["aaaa"])
        self.assertIn("closed", rows["bbbb"])

    def test_delete(self):
        self.assertTrue((self.state / "aaaa.turns.json").exists())
        self.run_cr("delete", "aaaa")
        self.assertFalse(any(self.state.rglob("aaaa*")))

    def test_keys_toggle(self):
        out = self.run_cr("keys-toggle", env={"FZF_HEADER_LABEL": ""}).stdout
        self.assertTrue(out.startswith("change-header-label( keys )+change-header:j / k"))
        self.assertIn("\nq / Esc    quit", out)
        out = self.run_cr("keys-toggle", env={"FZF_HEADER_LABEL": " keys "}).stdout
        self.assertEqual(out.strip(), "change-header-label()+change-header:"
                                      "Enter open · c go to session · dd delete · ? more")

    def test_rejects_bad_ids(self):
        for cmd in ("show", "open", "delete", "resume", "toggle"):
            r = self.run_cr(cmd, "../etc")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("bad id", r.stderr)

    def test_resume_without_claude_sessions(self):
        (Path(self.tmp) / "proj").mkdir()
        m = self.meta("aaaa")
        m["cwd"] = str(self.tmp / "proj")
        (self.state / "aaaa.json").write_text(json.dumps(m))
        path = f"{self.bin}:/usr/bin:/bin"  # no claude-sessions here
        r = self.run_cr("resume", "aaaa", env={"PATH": path, "CLAUDE_REPORTS_RESUME": "cc --resume {id}"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"tmux new-window -P -F #{{pane_id}} -c {self.tmp / 'proj'}", self.calls())
        self.assertIn("send-keys -t  -l cc --resume aaaa", self.calls())


class Render(Env):
    META = {"id": SID, "title": "T"}
    MD = "# Hi\n\n<script>alert(1)</script>\n\n`<b>` and </script> in text\n"

    def test_fallback_escapes_raw_html(self):
        cr.ASSETS = self.tmp / "none"
        page = cr.render(self.META, self.MD)
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn("<title>T · claude-report 11111111</title>", page)

    def test_markdown_it_embeds_source_safely(self):
        assets = self.tmp / "static"
        (assets / "markdown").mkdir(parents=True)
        (assets / "markdown/markdown-it.min.js").write_text("")
        (assets / "markdown/github-markdown.min.css").write_text("")
        cr.ASSETS = assets
        page = cr.render(self.META, self.MD)
        start = page.index('id="src">') + len('id="src">')
        data = page[start:page.index("</script>", start)]
        self.assertEqual(json.loads(data.replace("<\\/", "</")), self.MD)
        self.assertIn("html: false", page)
        self.assertIn((assets / "markdown/markdown-it.min.js").as_uri(), page)


class Server(Env):
    def setUp(self):
        super().setUp()
        self.hook([prompt("q"), say("the answer")], last_assistant_message="the answer")
        self.proc = subprocess.Popen([str(SCRIPT), "serve"], env={**self.env, "CLAUDE_REPORTS_IDLE": "60"},
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.proc.kill)
        for _ in range(100):
            try:
                if self.req("GET", "/ping")[0] == 200:
                    break
            except OSError:
                time.sleep(0.05)
        self.token = (self.state / "token").read_text().strip()
        self.cookie = f"claude_reports={self.token}"
        self.origin = f"http://127.0.0.1:{self.port}"

    def req(self, method, path, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.putrequest(method, path, skip_host=True)
        c.putheader("Host", host or f"127.0.0.1:{self.port}")
        for k, v in (headers or {}).items():
            c.putheader(k, v)
        c.endheaders()
        r = c.getresponse()
        body = r.read().decode(errors="replace")
        c.close()
        return r.status, dict(r.getheaders()), body

    def post(self, action, sid=SID, **override):
        h = {"Origin": self.origin, "Cookie": self.cookie, "X-Claude-Reports": self.token}
        h.update({k.replace("_", "-"): v for k, v in override.items()})
        h = {k: v for k, v in h.items() if v is not None}
        status, _, body = self.req("POST", f"/api/{action}/{sid}", h)
        return status, json.loads(body)

    def test_page_needs_the_token_once_then_a_cookie(self):
        self.assertEqual(self.req("GET", f"/r/{SID}")[0], 403)
        self.assertEqual(self.req("GET", f"/r/{SID}?t=wrong")[0], 403)
        status, headers, _ = self.req("GET", f"/r/{SID}?t={self.token}")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], f"/r/{SID}")
        self.assertIn("HttpOnly; SameSite=Strict", headers["Set-Cookie"])
        status, headers, body = self.req("GET", f"/r/{SID}", {"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        nonce = headers["Content-Security-Policy"].split("'nonce-")[1].split("'")[0]
        self.assertIn(f'<script nonce="{nonce}">', body)
        self.assertIn("Kill session", body)
        self.assertIn("the answer", body)

    def with_markdown_it(self):
        assets = Path(self.env["CLAUDE_REPORTS_ASSETS"]) / "markdown"
        assets.mkdir(parents=True, exist_ok=True)
        (assets / "markdown-it.min.js").write_text("")

    def test_page_shows_earlier_turns_collapsed(self):
        self.with_markdown_it()
        self.hook([prompt("first q"), say("first a"), prompt("second q"), say("second a")],
                  last_assistant_message="second a")
        status, _, body = self.req("GET", f"/r/{SID}", {"Cookie": self.cookie})
        self.assertEqual(status, 200)
        src = json.loads(body.split('id="src">')[1].split("</script>")[0].replace("<\\/", "</"))
        turns = json.loads(body.split('id="turns">')[1].split("</script>")[0].replace("<\\/", "</"))
        self.assertNotIn("Earlier in this session", src)  # not twice
        self.assertIn("second a", src)
        self.assertEqual(turns, [{"n": 1, "prompt": "first q", "reply": "first a"}])
        self.assertIn(f"VERSION = {self.meta()['ended']}", body)
        self.assertIn("call('POST', '/api/read/' + ID)", body)  # opening marks it read

    def test_old_reports_without_turns_file_keep_their_markdown(self):
        self.with_markdown_it()
        self.hook([prompt("first q"), say("first a"), prompt("second q"), say("second a")])
        (self.state / f"{SID}.turns.json").unlink()
        body = self.req("GET", f"/r/{SID}", {"Cookie": self.cookie})[2]
        self.assertIn("Earlier in this session", body.split('id="src">')[1].split("</script>")[0])

    def test_foreign_host_header_is_refused(self):  # DNS rebinding
        for path in ("/ping", f"/r/{SID}", "/assets/x.js"):
            self.assertEqual(self.req("GET", path, {"Cookie": self.cookie}, host="evil.example:80")[0], 403)
        self.assertEqual(self.post("read", Host=None)[0], 200)  # sanity: the real host works

    def test_actions_need_origin_cookie_and_header(self):
        self.assertEqual(self.post("read", Origin="http://evil.example")[0], 403)
        self.assertEqual(self.post("read", Origin=None)[0], 403)
        self.assertEqual(self.post("read", Cookie=None)[0], 403)
        self.assertEqual(self.post("read", X_Claude_Reports=None)[0], 403)
        self.assertEqual(self.post("read", X_Claude_Reports="wrong")[0], 403)
        self.assertTrue(self.meta()["unread"])
        status, body = self.post("read")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], {"id": SID, "unread": False, "open": False, "target": "",
                                         "version": self.meta()["ended"]})
        self.assertFalse(self.meta()["unread"])
        self.assertTrue(self.post("unread")[1]["state"]["unread"])

    def test_state_needs_cookie_and_header(self):
        self.assertEqual(self.req("GET", f"/api/state/{SID}", {"Cookie": self.cookie})[0], 403)
        status, _, body = self.req("GET", f"/api/state/{SID}",
                                   {"Cookie": self.cookie, "X-Claude-Reports": self.token})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["state"]["open"])

    def test_kill_only_a_pane_that_still_runs_the_session(self):
        status, body = self.post("kill")
        self.assertEqual(status, 409)
        self.assertNotIn("kill-pane", self.calls())
        # the pane now runs a different session
        self.mark_open("99999999-0000-0000-0000-000000000000")
        self.assertEqual(self.post("kill")[0], 409)
        self.assertNotIn("kill-pane", self.calls())
        self.mark_open(SID)
        status, body = self.post("kill")
        self.assertEqual(status, 200, body)
        self.assertIn("tmux kill-pane -t %7", self.calls())

    def test_focus_switches_the_latest_client_to_the_pane(self):
        self.mark_open(SID)
        status, body = self.post("focus")
        self.assertEqual(status, 200, body)
        self.assertIn("switch-client -c /dev/pts/1 -t %7", self.calls())
        self.assertFalse(self.meta()["unread"])

    def test_assets_stay_inside_the_assets_folder(self):
        assets = Path(self.env["CLAUDE_REPORTS_ASSETS"])
        (assets / "markdown").mkdir(parents=True)
        (assets / "markdown/x.js").write_text("ok")
        (assets / "secret.txt").write_text("no")
        status, headers, body = self.req("GET", "/assets/markdown/x.js")
        self.assertEqual((status, body), (200, "ok"))
        self.assertTrue(headers["Content-Type"].startswith("text/javascript"))
        for path in ("/assets/secret.txt", "/assets/../state/token", "/assets/%2e%2e/state/token",
                     "/assets/markdown/../../state/token"):
            self.assertEqual(self.req("GET", path)[0], 404, path)


if __name__ == "__main__":
    unittest.main()
