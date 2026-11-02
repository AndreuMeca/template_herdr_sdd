#!/usr/bin/env python3
"""The launcher's own tests: python3 harness/selftest.py [-v].

Stdlib unittest, one temporary git repo (main checkout + one feature worktree) per test, no herdr: the launcher
talks to herdr only through its HERDR adapter, whose methods the tests replace (`stub_herdr`), or a stand-in
`herdr` binary on PATH answers. Not named test_*.py on purpose, so a project's own pytest run does not collect it.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from argparse import Namespace
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("harness", HERE / "harness.py")
h = importlib.util.module_from_spec(_spec)
sys.modules["harness"] = h  # dataclasses resolve their annotations through sys.modules
_spec.loader.exec_module(h)

R1 = "- R1: WHEN f is called, THE SYSTEM SHALL return 1."
SPEC = """---
status: {status}
---
# Feat

## Intent
do x

## Requirements
{reqs}

## Design
src/m.py

## Verification
| R | Test |
|---|---|
| R1 | tests/test_feat.py (feat:R1) |

## Open questions
{questions}
"""
AGENTS = {r: {"harness": "claude", "permissions": "full"} for r in ("spec_author", "implementer", "reviewer")}


class Repo:
    """A temporary main checkout with the harness copied in, plus one feature worktree `feat` on branch `feat`."""

    def __init__(self, test_cmd="true", gates=None, rounds=3):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-selftest-"))
        self.root = self.tmp / "repo"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        shutil.copytree(HERE, self.root / "harness", ignore=shutil.ignore_patterns("__pycache__", "selftest.py"))
        self.write_cfg(test_cmd=test_cmd, gates=gates or {}, max_review_rounds=rounds)
        (self.root / "src").mkdir()
        (self.root / "src" / "m.py").write_text("def f():\n    return 1\n")
        (self.root / "tests").mkdir()
        (self.root / "tests" / ".keep").write_text("")
        (self.root / ".gitignore").write_text(".env\ndata/\n")
        self.commit("init")
        self.wt = self.tmp / "wt"
        self.git("worktree", "add", "-q", "-b", "feat", str(self.wt))
        self.fdir = self.wt / "harness" / "features" / "feat"
        shutil.copytree(self.wt / "harness" / "features" / "_template", self.fdir)
        for p in self.fdir.iterdir():
            text = p.read_text().replace("{{title}}", "Feat").replace("{{slug}}", "feat").replace("{{intent}}", "do x")
            p.write_text(text)
        self.commit("feat: new feature", cwd=self.wt)

    def write_cfg(self, **extra):
        cfg = {"base_branch": "main", "test_cmd": "true", "copy_files": [".env"], "agents": AGENTS, **extra}
        (self.root / "harness" / "config.json").write_text(json.dumps(cfg))

    def git(self, *args, cwd=None) -> str:
        p = subprocess.run(["git", *args], cwd=cwd or self.root, check=True, text=True, capture_output=True)
        return p.stdout.strip()

    def commit(self, msg, cwd=None):
        self.git("add", "-A", cwd=cwd)
        self.git("commit", "-q", "-m", msg, cwd=cwd)

    def spec(self, status, reqs=R1, questions=""):
        (self.fdir / "spec.md").write_text(SPEC.format(status=status, reqs=reqs, questions=questions))

    def tasks(self, lines="- [ ] T1: test f (R1)"):
        (self.fdir / "tasks.md").write_text("# Tasks\n" + lines + "\n")

    def implemented(self, with_test=True):
        """What a finished implementer leaves: ticked tasks, a log, a test carrying the token."""
        self.tasks("- [x] T1: test f (R1)")
        (self.fdir / "log.md").write_text("# Log\n## now implementer\n- done: all\n")
        if with_test:
            test = "# feat:R1\ndef test_f():\n    from src.m import f\n    assert f() == 1\n"
            (self.wt / "tests" / "test_feat.py").write_text(test)

    def review(self, verdict):
        (self.fdir / "review.md").write_text(f"---\nverdict: {verdict}\n---\n## Findings\nnone\n## Checked\n")

    def ref(self, rev="HEAD"):
        self.git("update-ref", "refs/harness/feat/review-start", rev, cwd=self.wt)

    def check(self):
        return h.Checker(self.wt, "feat", "main").errors()

    def cfg(self):
        return h.Config.load(self.wt)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def agent(raw: dict, role: str = "implementer"):
    """An AgentConfig parsed the way the launcher parses config.json; `permissions` defaults to full."""
    return h.AgentConfig.parse(role, {"permissions": "full", **raw}, "test")


def fake_driver(case) -> int:
    """pid of a live process whose command line reads like a driver's; killed when the test ends."""
    code = "import time; time.sleep(60)"
    p = subprocess.Popen([sys.executable, "-c", code, "harness.py", "drive", "feat"])
    case.addCleanup(p.wait)
    case.addCleanup(p.kill)
    return p.pid


def ps_works() -> bool:
    """`ps` can be run here (a sandbox may block it; the launcher then trusts a live pid)."""
    try:
        return subprocess.run(["ps", "-p", str(os.getpid())], capture_output=True).returncode == 0
    except OSError:
        return False


def no_herdr(*args):
    raise AssertionError(f"herdr must not be called in tests: {args}")


# A stand-in `herdr` binary for the adapter tests: answers from a JSON state file, logs every argv it gets, and
# behaves like the real CLI at the boundary the launcher relies on (JSON result on stdout, JSON error on stderr).
FAKE_HERDR = r"""#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_HERDR_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
state_path = os.environ["FAKE_HERDR_STATE"]
state = json.load(open(state_path))
agents = state.setdefault("agents", {})


def save():
    json.dump(state, open(state_path, "w"))


def ok(result):
    print(json.dumps({"id": "fake", "result": result}))
    sys.exit(0)


def err(code):
    sys.stderr.write(json.dumps({"error": {"code": code, "message": "fake " + code}}) + "\n")
    sys.exit(1)


def opt(flag):
    return args[args.index(flag) + 1] if flag in args else None


cmd = " ".join(args[:2])
if cmd == "agent get":
    a = agents.get(args[2])
    ok({"agent": {"agent_status": a["status"]}}) if a else err("agent_not_found")
elif cmd == "agent start":
    agents[args[2]] = {"status": state.get("start_status", "idle"), "pane": opt("--pane")}
    save()
    ok({"agent": {"pane_id": opt("--pane")}})
elif cmd == "agent prompt":
    a = agents.get(args[2])
    if not a:
        err("agent_not_found")
    if state.get("prompt_error"):
        err(state["prompt_error"])
    a["status"] = state.get("after_prompt", "idle")
    save()
    ok({"agent": {"agent_status": a["status"]}})
elif cmd == "agent wait":
    if args[2] not in agents:
        err("agent_not_found")
    if state.get("wait_error"):
        err(state["wait_error"])
    ok({"agent": {"agent_status": "idle"}})
elif cmd == "agent read":
    print(state.get("screen", ""))
elif cmd == "agent send-keys":
    a = agents.get(args[2])
    if a and args[3] == "enter":  # the dialog was answered
        a["status"] = "idle"
        save()
    ok({"type": "ok"})
elif cmd == "tab create":
    ok({"tab": {"tab_id": "w1:t2"}, "root_pane": {"pane_id": "w1:p2"}})
elif cmd == "tab list":
    ok({"tabs": state.get("tabs", [])})
elif cmd == "pane list":
    ok({"panes": state.get("panes", [])})
elif cmd == "worktree list":
    ok({"source": state.get("source", {}), "worktrees": state.get("worktrees", [])})
elif cmd == "worktree create":
    ok({"worktree": {"path": state["new_worktree"]}, "workspace": {"workspace_id": "w9"},
        "tab": {"tab_id": "w9:t1"}, "root_pane": {"pane_id": "w9:p1"}})
elif cmd == "worktree open":
    ok({"workspace": {"workspace_id": "w9"}})
elif cmd == "worktree remove":
    wt = next((w["path"] for w in state.get("worktrees", []) if w.get("open_workspace_id") == opt("--workspace")), None)
    if wt:
        subprocess.run(["git", "worktree", "remove", "--force", wt], cwd=state["repo"], check=False)
    ok({"type": "worktree_removed"})
elif cmd == "workspace list":
    ok({"workspaces": state.get("workspaces", [])})
elif cmd == "pane run":
    if state.get("touch_on_pane_run"):  # the driver that would start here writes its lock first thing
        with open(state["touch_on_pane_run"], "a") as fh:  # a new line per launch, like a new driver's stamp
            fh.write("started\n")
    ok({"type": "ok"})
else:
    ok({"type": "ok"})
"""


class Case(unittest.TestCase):
    repo_kwargs: dict = {}
    fake_herdr = False  # True: a stand-in `herdr` on PATH instead of stubbed launcher functions

    def setUp(self):
        self.r = Repo(**self.repo_kwargs)
        self.addCleanup(self.r.close)
        if self.fake_herdr:
            self.install_fake_herdr()
        else:
            self.stub_herdr("call", no_herdr)
            self.stub_herdr("state", lambda name: "idle")
            self.stub_herdr("sidebar", lambda *a, **k: None)

    def stub(self, name, value):
        orig = getattr(h, name)
        setattr(h, name, value)
        self.addCleanup(setattr, h, name, orig)

    def stub_herdr(self, method, value):
        """Replace one method of the launcher's HERDR adapter for this test."""
        setattr(h.HERDR, method, value)
        self.addCleanup(vars(h.HERDR).pop, method, None)

    def setenv(self, name, value):
        old = os.environ.get(name)
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
        self.addCleanup(lambda: os.environ.__setitem__(name, old) if old is not None else os.environ.pop(name, None))

    def install_fake_herdr(self):
        bindir = self.r.tmp / "bin"
        bindir.mkdir()
        exe = bindir / "herdr"
        exe.write_text(FAKE_HERDR)
        exe.chmod(0o755)
        self.state_path = self.r.tmp / "herdr-state.json"
        self.log_path = self.r.tmp / "herdr-log.jsonl"
        self.log_path.write_text("")
        self.state = {
            "repo": str(self.r.root),
            "new_worktree": str(self.r.tmp / "new-wt"),
            "worktrees": [{"path": str(self.r.wt), "branch": "feat", "open_workspace_id": "w1"}],
            "source": {"source_checkout_path": str(self.r.root), "source_workspace_id": "w0"},
        }
        self.save_state()
        self.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
        self.setenv("FAKE_HERDR_STATE", str(self.state_path))
        self.setenv("FAKE_HERDR_LOG", str(self.log_path))
        self.setenv("HERDR_WORKSPACE_ID", None)
        self.stub("time", types.SimpleNamespace(sleep=lambda s: None, time=time.time))  # no real waits

    def save_state(self):
        self.state_path.write_text(json.dumps(self.state))

    def calls(self, *prefix) -> list:
        """Every argv the fake herdr received that starts with `prefix`."""
        lines = [json.loads(line) for line in self.log_path.read_text().splitlines()]
        return [args for args in lines if args[: len(prefix)] == list(prefix)]

    def chdir(self, path):
        cwd = os.getcwd()
        os.chdir(path)
        self.addCleanup(os.chdir, cwd)

    @staticmethod
    def output(fn, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fn(*args)
        return code, out.getvalue()


# ----------------------------------------------------------------------------- pure functions
class PureFunctions(unittest.TestCase):
    def test_frontmatter(self):
        p = Path(tempfile.mkdtemp()) / "spec.md"
        p.write_text("---\nstatus: pending\n---\n# T\n")
        self.assertEqual(h.Frontmatter.read(p), {"status": "pending"})
        h.Frontmatter.set(p, "status", "review")
        h.Frontmatter.set(p, "extra", "1")
        self.assertEqual(h.Frontmatter.read(p), {"status": "review", "extra": "1"})
        self.assertTrue(p.read_text().endswith("# T\n"))
        p.write_text("# no frontmatter\n")
        self.assertEqual(h.Frontmatter.read(p), {})
        h.Frontmatter.set(p, "status", "x")
        self.assertEqual(h.Frontmatter.read(p), {"status": "x"})
        self.assertEqual(h.Frontmatter.read(p.parent / "missing.md"), {})

    def test_section_and_requirements(self):
        text = "## A\n1\n## Requirements\n<!-- c -->\n- R1: SHALL x\n- R2: withdrawn\nnote\n## B\n"
        self.assertEqual(h.Markdown.section(text, "a"), ["1"])
        self.assertEqual(h.FeatureFiles.requirement_lines(text), ["- R1: SHALL x", "- R2: withdrawn"])

    def test_next_action_and_groups(self):
        gates = {"approve": "human", "merge": "auto"}
        self.assertEqual(h.next_action("pending", "", gates), "run spec_author")
        self.assertEqual(h.next_action("spec_ready", "", gates), "approve (human)")
        self.assertEqual(h.next_action("review", "", gates), "run reviewer")
        self.assertEqual(h.next_action("review", "APPROVED", gates), "merge (auto)")
        self.assertEqual(h.next_action("review", "CHANGES_REQUESTED", gates), "run reviewer")
        self.assertTrue(h.needs_human(h.next_action("blocked", "", gates)))
        self.assertTrue(h.needs_human(h.next_action("bogus", "", gates)))
        self.assertFalse(h.needs_human("run implementer"))

    def test_state_text(self):
        gates = {"approve": "human", "merge": "auto"}
        self.assertEqual(h.state_text("spec_ready", "", gates), "spec ready · needs you: approve")
        self.assertEqual(h.state_text("review", "APPROVED", gates), "review approved · auto-merge")
        self.assertEqual(h.state_text("in_progress", "", gates), "in progress · implementer next")
        self.assertEqual(h.state_text("in_progress", "", gates, running="implementer"), "implementer · working")
        self.assertEqual(h.state_text("blocked", "", gates), "blocked · needs you")
        self.assertEqual(h.state_text("done", "", gates), "done")

    def test_effort_levels_per_cli(self):
        native = {
            "claude": ["low", "medium", "high", "xhigh", "max"],
            "codex": ["low", "medium", "high", "xhigh", "xhigh"],  # codex has nothing above xhigh
            "pi": ["low", "medium", "high", "xhigh", "max"],
        }
        flag = {"claude": ["--effort", "{}"], "codex": ["-c", "model_reasoning_effort={}"], "pi": ["--thinking", "{}"]}
        for kind, values in native.items():
            for ours, theirs in zip(h.EFFORT_LEVELS, values):
                want = [part.format(theirs) for part in flag[kind]]
                self.assertEqual(h.CLIS[kind].effort_args(ours), want, (kind, ours))
        self.assertEqual(h.CLIS["claude"].effort_args(""), [])

    def test_permission_levels_per_cli(self):
        repo = Path("/r")
        cases = {
            ("claude", "guarded"): ["--permission-mode", "auto"],
            ("claude", "full"): ["--dangerously-skip-permissions"],
            ("codex", "guarded"): [
                "--sandbox",
                "workspace-write",
                "--ask-for-approval",
                "never",
                "--add-dir",
                "/r/.git",
            ],
            ("codex", "full"): ["--dangerously-bypass-approvals-and-sandbox"],
        }
        for (kind, level), flags in cases.items():
            self.assertEqual(
                agent({"harness": kind, "permissions": level}).cli_args(repo), ["--", *flags], (kind, level)
            )
        self.assertEqual(agent({"harness": "pi", "permissions": "full"}).cli_args(repo), [], "pi never asks: no flag")

    def test_patterns_args_prompt(self):
        self.assertTrue(h.valid_pattern(".env"))
        self.assertTrue(h.valid_pattern("data/*.db"))
        for bad in ("/etc/passwd", "../x", "", ".", 3):
            self.assertFalse(h.valid_pattern(bad), bad)
        repo = Path("/r")
        self.assertEqual(agent({"harness": "pi"}).cli_args(repo), [], "nothing to pass: no `--`")
        self.assertEqual(
            agent({"harness": "gemini", "model": "m", "args": ["-x"]}).cli_args(repo), ["--", "--model", "m", "-x"]
        )
        self.assertEqual(
            agent({"harness": "claude", "effort": "extra"}).cli_args(repo),
            ["--", "--effort", "xhigh", "--dangerously-skip-permissions"],
        )
        self.assertEqual(
            agent({"harness": "codex", "model": "g", "effort": "high", "permissions": "full", "args": ["-y"]}).cli_args(
                repo
            ),
            [
                "--",
                "--model",
                "g",
                "-c",
                "model_reasoning_effort=high",
                "--dangerously-bypass-approvals-and-sandbox",
                "-y",
            ],
            "model, effort, permissions, then args",
        )
        self.assertEqual(agent({"harness": "gemini", "effort": "high", "permissions": "full"}).cli_args(repo), [])
        self.assertEqual(
            agent({"harness": "gemini", "args": ["--add-dir", "{repo}/.git", "-c", "x={a}"]}).cli_args(repo),
            ["--", "--add-dir", "/r/.git", "-c", "x={a}"],
            "{repo} is the main checkout; other braces stay as written",
        )
        self.assertEqual(
            h.RoleRun.prompt_for("reviewer", "s", " a\n b "),
            "Read harness/roles/reviewer.md and follow it for harness/features/s/. a b",
        )


# ----------------------------------------------------------------------------- config and worktree files
class ConfigTests(Case):
    def test_defaults_and_main_checkout(self):
        cfg = self.r.cfg()
        self.assertEqual(
            (cfg.gates, cfg.max_review_rounds, cfg.setup_cmd, cfg.copy_files),
            ({"approve": "human", "merge": "human"}, 3, "", (".env",)),
        )
        (self.r.wt / "harness" / "config.json").write_text(
            json.dumps({"base_branch": "main", "test_cmd": "false", "agents": AGENTS})
        )
        self.assertEqual(
            h.Config.load(self.r.wt).test_cmd, "true", "config comes from the main checkout, not the branch"
        )

    def test_shipped_config_is_the_safe_default(self):
        cfg = h.Config.parse(json.loads((HERE / "config.json").read_text()), HERE / "config.json")
        self.assertEqual(cfg.gates, {"approve": "human", "merge": "human"}, "a stranger's clone stops at both gates")
        for role in h.ROLES:
            self.assertEqual((cfg.agent(role).permissions, cfg.agent(role).model), ("guarded", ""), role)

    def test_rejects_bad_files(self):
        bad = [
            {"test_cmd": "true", "agents": AGENTS},
            {"base_branch": "main", "test_cmd": "true", "agents": {}},
            {"base_branch": "main", "test_cmd": "true", "agents": AGENTS, "gates": {"approve": "maybe"}},
            {"base_branch": "main", "test_cmd": "true", "agents": AGENTS, "copy_files": ["/x"]},
            {"base_branch": "main", "test_cmd": "true", "agents": AGENTS, "max_review_rounds": -1},
            {"base_branch": "main", "test_cmd": "true", "agents": AGENTS, "max_review_rounds": True},
            {"base_branch": "main", "test_cmd": "true", "agents": AGENTS, "teardown_cmd": 1},
        ]
        for raw in bad:
            (self.r.root / "harness" / "config.json").write_text(json.dumps(raw))
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                h.Config.load(self.r.root)
        (self.r.root / "harness" / "config.json").write_text("{")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            h.Config.load(self.r.root)

    def test_rejects_agents_outside_the_vocabulary(self):
        full = {"harness": "claude", "permissions": "full"}
        bad = {
            "effort 'turbo' is not ours": (
                {"reviewer": {**full, "effort": "turbo"}},
                "effort must be one of low, medium",
            ),
            "a provider word": ({"reviewer": {**full, "effort": "xhigh"}}, "here it is 'extra'"),
            "effort as a number": ({"reviewer": {**full, "effort": 3}}, "effort must be one of"),
            "unknown permissions": (
                {"reviewer": {"harness": "claude", "permissions": "yolo"}},
                "'yolo' is not one of them; a role agent runs unattended: use guarded or full",
            ),
            "the old ask level": ({"reviewer": {"harness": "claude", "permissions": "ask"}}, "use guarded or full"),
            "a role without permissions": ({"reviewer": {"harness": "claude"}}, "it is required"),
            "pi cannot be guarded": ({"reviewer": {"harness": "pi", "permissions": "guarded"}}, "not available for pi"),
            "the flag by hand": (
                {"reviewer": {**full, "args": ["--dangerously-skip-permissions"]}},
                "args sets permissions itself",
            ),
        }
        for why, (agents, msg) in bad.items():
            self.r.write_cfg(agents={**AGENTS, **agents})
            err = io.StringIO()
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
                h.Config.load(self.r.root)
            self.assertIn(msg, err.getvalue(), why)
        self.r.write_cfg(
            agents={**AGENTS, "reviewer": {"harness": "gemini", "permissions": "full", "args": ["--yolo"]}}
        )
        self.assertEqual(
            h.Config.load(self.r.root).agent("reviewer").harness, "gemini", "an untranslated CLI: flags in args"
        )

    def test_worktreeinclude(self):
        (self.r.root / ".worktreeinclude").write_text("# comment\n\n.env\ndata\n../up\n")
        pats, bad = h.WorktreeSetup(self.r.root, self.r.root, self.r.cfg()).patterns()
        self.assertEqual((pats, bad), ([".env", "data"], ["../up"]))

    def test_copy_ignored(self):
        root = self.r.root
        (root / ".env").write_text("S=1")
        (root / "data" / "sub").mkdir(parents=True)
        (root / "data" / "sub" / "db").write_text("db")
        os.symlink("sub/db", root / "data" / "link")
        dst = self.r.tmp / "dst"
        dst.mkdir()
        line = h.WorktreeSetup(root, dst, self.r.cfg()).copy_ignored([".env", "data", "data/*", "src/m.py", "nope"])
        self.assertIn("copied .env, data", line)
        self.assertIn("not in the main checkout: nope", line)
        self.assertIn("skipped, not gitignored: src/m.py", line)
        self.assertEqual((dst / ".env").read_text(), "S=1")
        self.assertEqual((dst / "data" / "sub" / "db").read_text(), "db")
        self.assertTrue((dst / "data" / "link").is_symlink())
        self.assertFalse((dst / "src").exists())
        self.assertIn(
            "copied .env, data",
            h.WorktreeSetup(root, dst, self.r.cfg()).copy_ignored([".env", "data"]),
            "a second run must not fail",
        )


# ----------------------------------------------------------------------------- git helpers
class GitHelpers(Case):
    def test_main_checkout_and_refs(self):
        self.assertEqual(h.Git(self.r.wt).main_checkout().resolve(), self.r.root.resolve())
        self.assertTrue(h.Git(self.r.root).is_main())
        self.assertFalse(h.Git(self.r.wt).is_main())
        ref = "refs/harness/feat/review-start"
        self.assertFalse(h.Git(self.r.wt).ref_exists(ref))
        self.r.ref()
        self.assertTrue(h.Git(self.r.root).ref_exists(ref), "refs are shared across worktrees")
        h.Git(self.r.wt).drop_ref(ref)
        self.assertFalse(h.Git(self.r.wt).ref_exists(ref))

    def test_features_everywhere(self):
        feats = h.Feature.all(self.r.root)
        f = feats["feat"]
        self.assertEqual((f.slug, f.branch, f.status, f.verdict, f.live), ("feat", "feat", "pending", "", True))
        self.assertEqual(f.wt.resolve(), self.r.wt.resolve())
        self.assertEqual(h.Feature.find(self.r.wt, "feat").dir, f.dir)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            h.Feature.find(self.r.root, "nope")

    def test_sync_base(self):
        self.assertEqual(h.Git(self.r.wt).merge_in("main"), (True, False))
        (self.r.root / "new.txt").write_text("x")
        self.r.commit("main: new")
        self.assertEqual(h.Git(self.r.wt).merge_in("main"), (True, True))
        self.assertTrue((self.r.wt / "new.txt").exists())
        (self.r.root / "src" / "m.py").write_text("def f():\n    return 2\n")
        self.r.commit("main: 2")
        (self.r.wt / "src" / "m.py").write_text("def f():\n    return 3\n")
        self.r.commit("feat: 3", cwd=self.r.wt)
        self.assertEqual(h.Git(self.r.wt).merge_in("main"), (False, False))
        self.assertEqual(self.r.git("status", "--porcelain", cwd=self.r.wt), "", "a conflict is aborted cleanly")

    def test_porcelain_and_changed_paths(self):
        (self.r.wt / "src" / "m.py").write_text("changed\n")
        (self.r.wt / "new dir").mkdir()
        (self.r.wt / "new dir" / "f.txt").write_text("")
        self.assertEqual(sorted(p for _, p in h.Git(self.r.wt).porcelain()), ["new dir/f.txt", "src/m.py"])
        self.assertEqual(
            h.Git(self.r.wt).changed_paths("main"),
            ["harness/features/feat/spec.md", "harness/features/feat/tasks.md", "new dir/f.txt", "src/m.py"],
        )

    def test_approve_commits_and_review_rounds(self):
        self.assertFalse(h.FeatureFiles(self.r.wt, "feat").approved_once("main"))
        self.r.spec("spec_ready")
        self.r.tasks()
        self.r.commit("feat: spec", cwd=self.r.wt)
        h.FeatureFiles(self.r.wt, "feat").approve("feat")
        self.assertTrue(h.FeatureFiles(self.r.wt, "feat").approved_once("main"))
        self.assertEqual(h.FeatureFiles(self.r.wt, "feat").review_rounds("main"), 0)
        self.r.review("CHANGES_REQUESTED")
        self.r.commit("feat: review", cwd=self.r.wt)
        h.FeatureFiles(self.r.wt, "feat").drop_review()
        self.r.review("APPROVED")
        self.r.commit("feat: review", cwd=self.r.wt)
        self.assertEqual(h.FeatureFiles(self.r.wt, "feat").review_rounds("main"), 1, "an APPROVED round does not count")
        self.assertIn("feat: clear previous review", self.r.git("log", "--format=%s", "main..HEAD", cwd=self.r.wt))

    def test_review_rounds_count_changes_requested_rounds_not_commits(self):
        rounds = lambda: h.FeatureFiles(self.r.wt, "feat").review_rounds("main")  # noqa: E731
        self.r.review("CHANGES_REQUESTED")
        self.r.commit("feat: review", cwd=self.r.wt)
        (self.r.fdir / "review.md").write_text((self.r.fdir / "review.md").read_text() + "- one more finding\n")
        self.r.commit("feat: review, one more finding", cwd=self.r.wt)
        self.assertEqual(rounds(), 1, "two commits in one round are one round")
        h.FeatureFiles(self.r.wt, "feat").drop_review()
        self.assertEqual(rounds(), 1, "a cleared round still counts")
        self.r.review("CHANGES_REQUESTED")
        self.r.commit("feat: review", cwd=self.r.wt)
        h.FeatureFiles(self.r.wt, "feat").drop_review()
        self.assertEqual(rounds(), 2)
        self.r.review("CHANGES_REQUESTED")
        self.r.commit("feat: review", cwd=self.r.wt)
        self.assertEqual(rounds(), 3, "the verdict on disk counts too")

    def test_block_feature(self):
        h.FeatureFiles(self.r.wt, "feat").block("why")
        self.assertEqual(h.FeatureFiles(self.r.wt, "feat").state()[0], "blocked")
        log = (self.r.fdir / "log.md").read_text()
        self.assertTrue(log.startswith("# Log\n"))
        self.assertIn("launcher\n- blockers: why", log)
        self.assertEqual(self.r.git("status", "--porcelain", cwd=self.r.wt), "")

    def test_driver_lock(self):
        self.assertEqual(h.DriverLock(self.r.wt).running(), 0)
        lock = h.DriverLock(self.r.wt).path
        pid = fake_driver(self)
        lock.write_text(str(pid))
        self.assertEqual(h.DriverLock(self.r.wt).running(), pid)
        lock.write_text("999999999")
        self.assertEqual(h.DriverLock(self.r.wt).running(), 0, "a stale lock counts as no driver")
        if ps_works():
            lock.write_text(str(os.getpid()))
            self.assertEqual(h.DriverLock(self.r.wt).running(), 0, "a live pid that is not a driver: the OS reused it")

    def test_acquire_driver_lock(self):
        lock = h.DriverLock(self.r.wt).path
        self.assertEqual(h.DriverLock(self.r.wt).acquire(), 0)
        self.assertEqual(lock.read_text(), str(os.getpid()))
        lock.unlink()
        pid = fake_driver(self)
        lock.write_text(str(pid))
        self.assertEqual(h.DriverLock(self.r.wt).acquire(), pid, "a live driver keeps its lock")
        lock.write_text("999999999")
        self.assertEqual(h.DriverLock(self.r.wt).acquire(), 0, "a dead driver's lock is taken over")
        self.assertEqual(lock.read_text(), str(os.getpid()))

    def test_a_second_driver_refuses_to_run(self):
        h.DriverLock(self.r.wt).path.write_text(str(fake_driver(self)))
        f = h.Feature.find(self.r.root, "feat")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = h.Driver(self.r.root, self.r.cfg(), f, "", "").run()
        self.assertEqual(code, h.EXIT_ERROR)
        self.assertIn("is already running feat", err.getvalue())
        self.assertFalse(h.DriverLock(self.r.wt).stamp.exists(), "it stops before announcing a start")


# ----------------------------------------------------------------------------- check rules
class CheckRules(Case):
    def approved(self):
        """A feature at in_progress whose spec was approved on the branch."""
        self.r.spec("spec_ready")
        self.r.tasks()
        self.r.commit("feat: spec", cwd=self.r.wt)
        h.FeatureFiles(self.r.wt, "feat").approve("feat")
        self.r.spec("in_progress")
        self.r.implemented()
        self.r.commit("feat: T1", cwd=self.r.wt)

    def test_pending_clean(self):
        self.assertEqual(self.r.check(), [])

    def test_rules_7_and_8_before_approval(self):
        (self.r.wt / "src" / "m.py").write_text("print(2)\n")
        (self.r.wt / "harness" / "harness.py").write_text("# x\n")
        errs = self.r.check()
        self.assertIn("feat: touches harness/ outside its folder: harness/harness.py", errs)
        self.assertIn("feat: changes files outside its folder before the spec is approved: src/m.py", errs)

    def test_rule_2(self):
        reqs = "- R1: WHEN x, THE SYSTEM SHALL y.\n- R1: WHEN x, THE SYSTEM SHALL z.\n- R2: returns 1."
        self.r.spec("spec_ready", reqs=reqs, questions="- why?")
        self.r.tasks("- [ ] T1: test (R1, R2)")
        errs = self.r.check()
        self.assertIn("feat: duplicate requirement id R1", errs)
        self.assertIn("feat: R2 has no SHALL", errs)
        self.assertIn("feat: ## Open questions is not empty (answer them or set status blocked)", errs)
        self.r.spec("spec_ready", reqs="- R1: withdrawn")
        self.assertIn("feat: no active requirement line '- R<n>: ... SHALL ...' under ## Requirements", self.r.check())

    def test_rule_2_ears(self):
        good = [
            "THE SYSTEM SHALL log every request.",
            "WHEN a user logs in, THE SYSTEM SHALL set a cookie.",
            "WHILE offline, THE SYSTEM SHALL queue writes.",
            "WHERE dark mode is enabled, THE SYSTEM SHALL use the dark palette.",
            "IF the token is expired, THEN THE SYSTEM SHALL return 401.",
            "WHILE offline, WHEN a write arrives, THE SYSTEM SHALL queue it.",
            "WHEN a, b, or c happens, THE API SHALL return 200 within 2 s.",
        ]
        bad = {
            "WHEN x, THE SYSTEM SHALL y and SHALL z.": "has 2 SHALLs; one per requirement",
            "IF x, THE SYSTEM SHALL y.": "follows no EARS pattern",
            "SHALL return 1.": "follows no EARS pattern",
            "The system SHALL return 1.": "follows no EARS pattern",
            "The system shall return 1.": "has no SHALL",
        }
        lines = [f"- R{i}: {t}" for i, t in enumerate([*good, *bad], 1)]
        self.r.spec("spec_ready", reqs="\n".join(lines))
        self.r.tasks(f"- [ ] T1: all ({', '.join(f'R{i}' for i in range(1, len(lines) + 1))})")
        errs = self.r.check()
        for i, (text, why) in enumerate(bad.items(), len(good) + 1):
            self.assertTrue(any(e.startswith(f"feat: R{i} {why}") for e in errs), (text, errs))
        self.assertEqual(len(errs), len(bad), errs)

    def test_fixture_requirements_are_ears(self):
        for text in (R1, "- R2: THE SYSTEM SHALL z."):
            self.assertTrue(h.EARS_RE.match(h.REQ_RE.match(text)[2]), text)

    def test_rule_3(self):
        self.r.spec("spec_ready", reqs=R1 + "\n- R2: THE SYSTEM SHALL z.")
        self.r.tasks("- [ ] T1: a (R1, R9)\n- [ ] T2: b")
        errs = self.r.check()
        self.assertIn("feat: T1 cites unknown R9", errs)
        self.assertIn("feat: T2 cites no requirement", errs)
        self.assertIn("feat: R2 is not covered by any task", errs)
        (self.r.fdir / "tasks.md").unlink()
        self.assertIn("feat: missing tasks.md", self.r.check())

    def test_spec_ready_clean_and_in_progress_with_code(self):
        self.r.spec("spec_ready")
        self.r.tasks()
        self.assertEqual(self.r.check(), [])
        self.approved()
        self.assertEqual(self.r.check(), [])

    def test_rules_4_and_5_at_review(self):
        self.approved()
        self.r.spec("review")
        self.r.tasks("- [ ] T1: test f (R1)")
        (self.r.fdir / "log.md").unlink()
        (self.r.wt / "tests" / "test_feat.py").unlink()
        errs = self.r.check()
        self.assertIn("feat: no test carries the token feat:R1", errs)
        self.assertIn("feat: T1 is not ticked", errs)
        self.assertIn("feat: missing log.md", errs)

    def test_rule_4_matches_the_whole_token(self):
        self.approved()
        self.r.spec("review")
        test = self.r.wt / "tests" / "test_feat.py"
        missing = "feat: no test carries the token feat:R1"
        test.write_text("# feat:R10\n# xfeat:R1\n# my-feat:R1\n# feat:R1x\ndef test_f():\n    assert True\n")
        self.assertIn(missing, self.r.check(), "longer tokens do not cover R1")
        for line in ("# covers feat:R1", '"""(feat:R1)"""', "feat:R1"):
            test.write_text(line + "\ndef test_f():\n    assert True\n")
            self.assertNotIn(missing, self.r.check(), line)

    def test_template_placeholders(self):
        tmpl = self.r.wt / "harness" / "features" / "_template"
        for name in ("spec.md", "tasks.md"):
            (self.r.fdir / name).write_text((tmpl / name).read_text().replace("{{slug}}", "feat"))
        h.Frontmatter.set(self.r.fdir / "spec.md", "status", "spec_ready")
        errs = self.r.check()
        for err in (
            "feat: R1 still has the template placeholder <trigger>",
            "feat: R2 still has the template placeholder <unwanted condition>",
            "feat: T1 still has the template placeholder <task>",
            "feat: T2 still has the template placeholder <task>",
        ):
            self.assertIn(err, errs)
        self.assertFalse([e for e in errs if "T3" in e], "the template's last task is meant to be kept")

    def test_placeholders_mirror_the_template(self):
        tmpl = HERE / "features" / "_template"
        text = (tmpl / "spec.md").read_text() + (tmpl / "tasks.md").read_text()
        for token in h.TEMPLATE_PLACEHOLDERS:
            self.assertIn(token, text)

    def test_rule_9_reviewer_scope(self):
        self.approved()
        self.r.spec("review")
        self.r.commit("feat: review ready", cwd=self.r.wt)
        self.r.ref()
        self.r.review("CHANGES_REQUESTED")
        self.r.spec("in_progress")
        (self.r.wt / "src" / "m.py").write_text("# touched by reviewer\n")
        self.r.commit("feat: review", cwd=self.r.wt)
        self.assertEqual(
            self.r.check(),
            ["feat: the reviewer changed src/m.py (only review.md and the status line of spec.md are allowed)"],
        )
        (self.r.wt / "src" / "m.py").write_text("def f():\n    return 1\n")
        self.r.commit("feat: put back", cwd=self.r.wt)
        self.assertEqual(self.r.check(), [], "touched and put back is fine")
        (self.r.root / ".gitignore").write_text(".env\ndata/\nmore\n")
        self.r.commit("main: more")
        self.r.git("merge", "-q", "--no-edit", "main", cwd=self.r.wt)
        self.assertEqual(self.r.check(), [], "the base branch merged in is not the reviewer's change")
        (self.r.wt / "notes.txt").write_text("n")
        self.assertEqual(
            self.r.check(),
            ["feat: the reviewer changed notes.txt (only review.md and the status line of spec.md are allowed)"],
        )
        (self.r.wt / "notes.txt").unlink()
        with (self.r.fdir / "spec.md").open("a") as fh:
            fh.write("extra\n")
        self.assertIn("feat: the reviewer changed more than the status line in spec.md", self.r.check())

    def test_rule_10_stray_verdict(self):
        self.approved()
        self.r.spec("review")
        self.r.review("APPROVED")
        self.r.commit("feat: T2", cwd=self.r.wt)
        self.assertEqual(
            self.r.check(),
            ["feat: review.md says APPROVED but the launcher never started a reviewer round; run the reviewer"],
        )
        self.r.ref("HEAD~1")
        self.assertEqual(self.r.check(), [])

    def test_rule_11_requirements_frozen(self):
        self.approved()
        self.r.spec("in_progress", reqs="- R1: WHEN f is called, THE SYSTEM SHALL return 2.")
        self.r.commit("feat: sneaky", cwd=self.r.wt)
        self.assertEqual(len(self.r.check()), 1)
        self.assertIn("## Requirements differs from the approved spec", self.r.check()[0])
        self.r.spec("in_progress")
        self.assertEqual(self.r.check(), [])

    def test_implementer_round_drops_the_ref(self):
        self.approved()
        self.r.spec("review")
        self.r.commit("feat: review ready", cwd=self.r.wt)
        self.r.ref()
        self.r.review("CHANGES_REQUESTED")
        self.r.spec("in_progress")
        self.r.commit("feat: review", cwd=self.r.wt)
        self.assertIsNone(
            h.RoleRun(self.r.cfg(), self.r.root, self.r.wt, "implementer", "feat").prepare("CHANGES_REQUESTED")
        )
        self.assertFalse(h.Git(self.r.wt).ref_exists("refs/harness/feat/review-start"))
        (self.r.wt / "src" / "m.py").write_text("print(3)\n")
        self.r.commit("feat: T2", cwd=self.r.wt)
        self.assertEqual(self.r.check(), [])

    def test_main_checkout_and_done_skip_scope(self):
        self.r.spec("review")
        self.r.implemented()
        main_feat = self.r.root / "harness" / "features" / "feat"
        shutil.copytree(self.r.fdir, main_feat)
        shutil.copy(self.r.wt / "tests" / "test_feat.py", self.r.root / "tests" / "test_feat.py")
        h.Frontmatter.set(main_feat / "spec.md", "status", "done")
        self.r.commit("feat: done")
        (self.r.root / "harness" / "harness.py").write_text("# local edit\n")
        self.assertEqual(h.Checker(self.r.root, "feat", "main").errors(), [], "no scope rules on the main checkout")
        self.assertEqual(h.Checker(self.r.root, "nope", "main").errors(), ["nope: missing spec.md"])
        h.Frontmatter.set(self.r.fdir / "spec.md", "status", "bogus")
        self.assertEqual(len(self.r.check()), 1)
        self.assertIn("is not one of", self.r.check()[0])


# ----------------------------------------------------------------------------- outcome / prepare
class RoleRuns(Case):
    def test_evaluate_outcomes(self):
        cfg = self.r.cfg()
        self.assertEqual(
            h.RoleRun(cfg, self.r.root, self.r.wt, "spec_author", "feat").outcome("agent_blocked").result,
            "NEEDS_ATTENTION",
        )
        self.stub_herdr("state", lambda name: "blocked")
        self.assertEqual(
            h.RoleRun(cfg, self.r.root, self.r.wt, "spec_author", "feat").outcome("").result, "NEEDS_ATTENTION"
        )
        self.stub_herdr("state", lambda name: "idle")
        out = h.RoleRun(cfg, self.r.root, self.r.wt, "spec_author", "feat").outcome("")
        result, note, errs = out.result, out.note, out.errors
        self.assertEqual((result, errs), ("NEEDS_ATTENTION", []))
        self.assertIn("is not what spec_author must leave", note)
        self.r.spec("spec_ready")
        self.r.tasks()
        out = h.RoleRun(cfg, self.r.root, self.r.wt, "spec_author", "feat").outcome("")
        result, note, errs = out.result, out.note, out.errors
        self.assertEqual((result, note), ("NEEDS_ATTENTION", "check fails"))
        self.assertEqual(len(errs), 1)
        self.assertIn(
            "uncommitted changes left behind: harness/features/feat/spec.md, harness/features/feat/tasks.md",
            errs[0][:120],
        )
        self.r.commit("feat: spec", cwd=self.r.wt)
        self.assertEqual(h.RoleRun(cfg, self.r.root, self.r.wt, "spec_author", "feat").outcome("").result, "DONE")

    def test_evaluate_discards_implementer_verdict(self):
        self.r.spec("review")
        self.r.implemented()
        self.r.review("APPROVED")
        self.r.commit("feat: T1", cwd=self.r.wt)
        out = h.RoleRun(self.r.cfg(), self.r.root, self.r.wt, "implementer", "feat").outcome("")
        result, note, errs = out.result, out.note, out.errors
        self.assertEqual((result, note, errs), ("DONE", "discarded a verdict the implementer wrote", []))
        self.assertFalse((self.r.fdir / "review.md").exists())
        self.assertEqual(self.r.git("log", "--format=%s", "-1", cwd=self.r.wt), "feat: clear previous review")

    def test_evaluate_review_cap(self):
        self.r.write_cfg(max_review_rounds=1)
        self.r.spec("review")
        self.r.implemented()
        self.r.commit("feat: T1", cwd=self.r.wt)
        self.r.ref()
        self.r.review("CHANGES_REQUESTED")
        self.r.spec("in_progress")
        self.r.commit("feat: review", cwd=self.r.wt)
        out = h.RoleRun(self.r.cfg(), self.r.root, self.r.wt, "reviewer", "feat").outcome("")
        result, note = out.result, out.note
        self.assertEqual((result, note), ("NEEDS_ATTENTION", "blocked after 1 review rounds"))
        self.assertEqual(h.FeatureFiles(self.r.wt, "feat").state(), ("blocked", "CHANGES_REQUESTED"))
        self.assertFalse(h.Git(self.r.wt).ref_exists("refs/harness/feat/review-start"))
        self.assertIn("max_review_rounds", (self.r.fdir / "log.md").read_text())

    def test_prepare_refuses_a_dirty_worktree(self):
        (self.r.wt / "x.txt").write_text("")
        err = h.RoleRun(self.r.cfg(), self.r.root, self.r.wt, "spec_author", "feat").prepare("")
        self.assertIn("uncommitted changes", err)

    def review_ready(self):
        self.r.spec("review")
        self.r.implemented()
        self.r.commit("feat: T1", cwd=self.r.wt)

    def test_prepare_reviewer_clears_the_verdict_and_marks(self):
        self.review_ready()
        self.r.review("CHANGES_REQUESTED")
        self.r.commit("feat: old review", cwd=self.r.wt)
        self.assertIsNone(
            h.RoleRun(self.r.cfg(), self.r.root, self.r.wt, "reviewer", "feat").prepare("CHANGES_REQUESTED")
        )
        self.assertFalse((self.r.fdir / "review.md").exists(), "the old verdict is gone")
        self.assertEqual(
            self.r.git("rev-parse", "refs/harness/feat/review-start", cwd=self.r.wt),
            self.r.git("rev-parse", "HEAD", cwd=self.r.wt),
        )

    def driver(self):
        return h.Driver(self.r.root, self.r.cfg(), h.Feature.find(self.r.root, "feat"), "", "")

    def test_before_review_merges_the_base_and_sends_red_back(self):
        self.review_ready()
        (self.r.root / "new.txt").write_text("x")
        self.r.commit("main: new")
        self.assertEqual(self.driver().before_review("go"), ("reviewer", "go"))
        self.assertTrue((self.r.wt / "new.txt").exists(), "the base branch was merged in")
        self.r.write_cfg(test_cmd="false")
        role, text = self.driver().before_review("go")
        self.assertEqual(role, "implementer")
        self.assertIn("tests failed (exit 1: false)", text)

    def test_before_review_conflict_goes_to_the_implementer(self):
        self.review_ready()
        (self.r.root / "src" / "m.py").write_text("def f():\n    return 2\n")
        self.r.commit("main: 2")
        (self.r.wt / "src" / "m.py").write_text("def f():\n    return 3\n")
        self.r.commit("feat: 3", cwd=self.r.wt)
        self.assertEqual(self.driver().before_review("go"), ("implementer", h.SYNC_MSG.format(base="main")))
        self.assertEqual(self.r.git("status", "--porcelain", cwd=self.r.wt), "")


# ----------------------------------------------------------------------------- the driver, with stubbed agents
class DriverTests(Case):
    def setUp(self):
        super().setUp()
        self.calls: list[tuple[str, str]] = []
        self.notes: list[str] = []
        self.actions: dict[str, list] = {"spec_author": [], "implementer": [], "reviewer": []}
        self.stub_herdr("prompt_or_start", self.fake_prompt)
        self.stub_herdr("wait", lambda name, ms: (True, ""))
        self.stub_herdr("notify", lambda pane, text: self.notes.append(text))
        self.merged = []
        self.stub("Driver", type("Driver", (h.Driver,), {"spawn_merge": lambda driver: self.fake_merge(driver)}))

    def fake_prompt(self, root, wt, role, slug, text, timeout_ms, acfg):
        self.calls.append((role, text))
        self.actions[role].pop(0)()
        return True, ""

    def fake_merge(self, driver):
        self.merged.append(driver.slug)
        driver.lock.release()
        raise h._Stop(h.EXIT_DONE)

    # role behaviours
    def spec_author(self, reqs=R1, tasks="- [ ] T1: test f (R1)"):
        self.r.spec("spec_ready", reqs=reqs)
        self.r.tasks(tasks)
        self.r.commit("feat: spec", cwd=self.r.wt)

    def implementer(self, with_test=True):
        self.r.spec("review")
        self.r.implemented(with_test)
        self.r.commit("feat: T1", cwd=self.r.wt)

    def reviewer(self, verdict="APPROVED"):
        self.r.review(verdict)
        if verdict != "APPROVED":
            self.r.spec("in_progress")
        self.r.commit("feat: review", cwd=self.r.wt)

    def reviewer_who_cannot_commit(self):
        self.r.review("APPROVED")  # left uncommitted, as a sandboxed reviewer does

    def test_uncommitted_review_stops_before_the_merge(self):
        for gates in ({"approve": "auto", "merge": "auto"}, {"approve": "auto", "merge": "human"}):
            with self.subTest(gates=gates):
                self.setUp()
                self.r.write_cfg(gates=gates)
                self.actions["spec_author"] = [self.spec_author]
                self.actions["implementer"] = [self.implementer]
                self.actions["reviewer"] = [self.reviewer_who_cannot_commit]
                code, _ = self.drive()
                self.assertEqual(code, h.EXIT_ATTENTION)
                self.assertIn("uncommitted changes left behind: harness/features/feat/review.md", self.notes[-1])
                self.assertFalse(self.merged, "no merge is announced for a dirty worktree")
                self.doCleanups()

    def drive(self, msg="", notify="pane"):
        f = h.Feature.find(self.r.root, "feat")
        return self.output(h.Driver(self.r.root, self.r.cfg(), f, msg, notify).run)

    def stuck_role(self, max_minutes, settle_after):
        """A spec author whose prompt never settles by itself; wait_agent settles after `settle_after` waits; each
        wait moves the clock 10 minutes."""
        self.r.write_cfg(max_role_minutes=max_minutes)
        clock, waits = [0.0], [0]

        def wait(name, ms):
            clock[0] += 600
            waits[0] += 1
            return waits[0] >= settle_after, ""

        def prompt(root, wt, role, slug, text, timeout_ms, acfg):
            self.fake_prompt(root, wt, role, slug, text, timeout_ms, acfg)
            return False, ""

        self.stub_herdr("wait", wait)
        self.stub_herdr("prompt_or_start", prompt)
        self.stub("time", types.SimpleNamespace(time=lambda: clock[0], sleep=lambda s: None))
        self.actions["spec_author"] = [self.spec_author]
        return waits

    def test_a_role_past_max_role_minutes_stops_the_driver(self):
        waits = self.stuck_role(max_minutes=30, settle_after=99)
        code, _ = self.drive()
        self.assertEqual(code, h.EXIT_ATTENTION)
        self.assertIn("needs you: spec_author still working after 30 min (max_role_minutes 30)", self.notes[-1])
        self.assertEqual(waits[0], 3)

    def test_no_role_limit_waits_until_the_role_settles(self):
        waits = self.stuck_role(max_minutes=0, settle_after=20)
        code, _ = self.drive()  # human approve gate: stops at spec_ready once the spec author settles
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual(waits[0], 20)
        self.assertIn("spec ready, needs you", self.notes[-1])

    def test_auto_gates_run_to_the_merge(self):
        self.r.write_cfg(gates={"approve": "auto", "merge": "auto"})
        self.actions["spec_author"] = [self.spec_author]
        self.actions["implementer"] = [self.implementer]
        self.actions["reviewer"] = [self.reviewer]
        code, out = self.drive()
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual([role for role, _ in self.calls], ["spec_author", "implementer", "reviewer"])
        self.assertEqual(self.merged, ["feat"])
        self.assertTrue(h.FeatureFiles(self.r.wt, "feat").approved_once("main"))
        self.assertIn("run reviewer", out)
        self.assertEqual(h.DriverLock(self.r.wt).running(), 0)

    def test_human_gate_stops_and_notifies(self):
        self.actions["spec_author"] = [self.spec_author]
        code, out = self.drive()
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("harness: feat spec ready, needs you", self.notes[0])
        self.assertEqual(h.FeatureFiles(self.r.wt, "feat").state()[0], "spec_ready")
        self.assertEqual(h.DriverLock(self.r.wt).running(), 0)
        self.assertFalse(self.merged)

    def test_message_at_the_gate_goes_to_the_judged_role(self):
        self.spec_author()
        revised = R1 + "\n- R2: THE SYSTEM SHALL log it."
        self.actions["spec_author"] = [lambda: self.spec_author(reqs=revised, tasks="- [ ] T1: test f (R1, R2)")]
        code, _ = self.drive(msg="please add R2")
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual([role for role, _ in self.calls], ["spec_author"])
        self.assertIn("please add R2", self.calls[0][1])
        self.assertIn("spec ready, needs you", self.notes[-1], "the revised spec waits at the gate again")

    def test_failed_check_goes_back_then_on(self):
        self.r.write_cfg(gates={"approve": "auto", "merge": "auto"})
        self.actions["spec_author"] = [self.spec_author]
        self.actions["implementer"] = [lambda: self.implementer(with_test=False), self.implementer]
        self.actions["reviewer"] = [self.reviewer]
        code, out = self.drive()
        self.assertEqual(code, h.EXIT_DONE)
        self.assertEqual([role for role, _ in self.calls], ["spec_author", "implementer", "implementer", "reviewer"])
        self.assertIn("no test carries the token feat:R1", self.calls[2][1])
        self.assertEqual(self.merged, ["feat"])

    def test_changes_requested_loops_then_blocks_at_the_cap(self):
        self.r.write_cfg(gates={"approve": "auto", "merge": "auto"}, max_review_rounds=2)
        self.actions["spec_author"] = [self.spec_author]
        self.actions["implementer"] = [self.implementer, self.implementer_again]
        self.actions["reviewer"] = [
            lambda: self.reviewer("CHANGES_REQUESTED"),
            lambda: self.reviewer("CHANGES_REQUESTED"),
        ]
        code, _ = self.drive()
        self.assertEqual(code, h.EXIT_ATTENTION)
        self.assertEqual(
            [role for role, _ in self.calls], ["spec_author", "implementer", "reviewer", "implementer", "reviewer"]
        )
        self.assertEqual(h.FeatureFiles(self.r.wt, "feat").state()[0], "blocked")
        self.assertIn("blocked, needs you", self.notes[-1])
        self.assertFalse(self.merged)

    def implementer_again(self):
        self.r.spec("review")
        self.r.commit("feat: addressed", cwd=self.r.wt)


# ----------------------------------------------------------------------------- commands on the board
class Commands(Case):
    def test_status_groups(self):
        self.chdir(self.r.root)
        code, out = self.output(h.cmd_status, Namespace())
        self.assertEqual(code, 0)
        self.assertIn("1 to run", out, "nothing runs: the orchestrator's move")
        self.assertIn("run spec_author", out)
        h.DriverLock(self.r.wt).path.write_text(str(fake_driver(self)))
        _, out = self.output(h.cmd_status, Namespace())
        self.assertIn("1 working", out)
        self.assertIn("(driver running)", out)
        h.DriverLock(self.r.wt).path.unlink()
        self.r.spec("spec_ready")
        self.r.tasks()
        _, out = self.output(h.cmd_status, Namespace())
        self.assertIn("1 needs you", out)
        self.assertIn("approve (human)", out)

    def test_doctor_shows_the_setup_first(self):
        self.chdir(self.r.wt)  # from a worktree it still shows the main checkout's config
        agents = {
            **AGENTS,
            "implementer": {"harness": "codex", "effort": "max", "permissions": "guarded", "args": ["-x"]},
            "reviewer": {"harness": "gemini", "effort": "high", "permissions": "full"},
        }
        self.r.write_cfg(gates={"approve": "auto"}, agents=agents)
        orig = h.Doctor.herdr_state
        h.Doctor.herdr_state = lambda doc: None  # no herdr server in tests
        self.addCleanup(setattr, h.Doctor, "herdr_state", orig)
        _, out = self.output(h.cmd_doctor, Namespace(no_tests=True, models=False))
        self.assertTrue(out.startswith("harness/config.json\n"), "the setup comes first")
        self.assertIn("gates              approve: auto   merge: human", out)
        self.assertIn("implementer        codex   (default model)", out)
        self.assertIn("effort       max (codex: xhigh)", out)
        self.assertIn("permissions  guarded: never asks, the CLI blocks risky actions; its sandbox", out)
        self.assertIn("args         -x", out)
        self.assertIn("effort       high (not translated for gemini: put its flag in args)", out)
        self.assertIn("permissions  full (not translated for gemini: put its flags in args)", out)
        self.assertIn("orchestrator       the agent you started (not in this file)", out)
        self.assertNotIn("settings.local.json", out)
        self.assertIn("copy_files         .env", out)

    def test_start_creates_the_feature(self):
        self.chdir(self.r.root)

        def create(root, slug, base, trust):
            path = self.r.tmp / "wt-new"
            self.r.git("worktree", "add", "-q", "-b", slug, str(path), base)
            return {"worktree": {"path": str(path)}, "workspace": {"workspace_id": "w9"}}

        self.stub_herdr("create_worktree", create)
        code, out = self.output(h.cmd_start, Namespace(slug="newf", intent="do y", trust=False))
        self.assertEqual(code, 0)
        self.assertTrue((self.r.tmp / "wt-new" / "harness" / "features" / "newf" / "spec.md").exists())
        self.assertIn("next: harness.py drive newf --tab", out)
        self.assertFalse((self.r.root / ".claude").exists(), "nothing tool-specific is written")

    def test_check_command_finds_the_worktree(self):
        self.chdir(self.r.root)
        code, out = self.output(h.cmd_check, Namespace(slug="feat", tests=False, no_tests=True))
        self.assertEqual(code, 0)
        self.assertIn(f"checking feat in {self.r.wt.resolve()}", out)
        self.assertIn("OK (feat)", out)
        (self.r.wt / "src" / "m.py").write_text("x\n")
        code, out = self.output(h.cmd_check, Namespace(slug="feat", tests=False, no_tests=True))
        self.assertEqual(code, 1)
        self.assertIn("before the spec is approved: src/m.py", out)

    def test_approve_requires_spec_ready_and_green_check(self):
        self.chdir(self.r.root)
        self.stub_herdr("resume", lambda *a: 0)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            h.cmd_approve(Namespace(slug="feat"))
        self.r.spec("spec_ready", reqs="- R1: returns 1.")
        self.r.tasks()
        self.r.commit("feat: spec", cwd=self.r.wt)
        with (
            self.assertRaises(SystemExit),
            contextlib.redirect_stderr(io.StringIO()),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            h.cmd_approve(Namespace(slug="feat"))
        self.r.spec("spec_ready")
        self.r.commit("feat: spec fixed", cwd=self.r.wt)
        code, _ = self.output(h.cmd_approve, Namespace(slug="feat"))
        self.assertEqual(code, 0)
        self.assertEqual(h.FeatureFiles(self.r.wt, "feat").state()[0], "approved")
        self.assertTrue(h.FeatureFiles(self.r.wt, "feat").approved_once("main"))

    def test_merge_reports_a_failure_to_the_notify_pane(self):
        self.chdir(self.r.root)
        notes = []
        self.stub_herdr("notify", lambda pane, text: notes.append(text))
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
            h.cmd_merge(Namespace(slug="feat", notify="wB:p2"))
        self.assertEqual(ctx.exception.code, h.EXIT_ERROR)
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].startswith("harness: feat merge failed: 'feat' is status=pending verdict=-"), notes[0])
        self.assertIn("harness-merge-feat.log", notes[0])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            h.cmd_merge(Namespace(slug="feat", notify=""))
        self.assertEqual(len(notes), 1, "nothing to notify without --notify")

    def test_merge_lands_and_cleans_up(self):
        self.chdir(self.r.root)
        self.stub_herdr("workspace_of", lambda wt: None)
        self.r.spec("spec_ready")
        self.r.tasks()
        self.r.commit("feat: spec", cwd=self.r.wt)
        h.FeatureFiles(self.r.wt, "feat").approve("feat")
        self.r.spec("review")
        self.r.implemented()
        self.r.commit("feat: T1", cwd=self.r.wt)
        self.r.ref()
        self.r.review("APPROVED")
        self.r.commit("feat: review", cwd=self.r.wt)
        notes = []
        self.stub_herdr("notify", lambda pane, text: notes.append((pane, text)))
        code, out = self.output(h.cmd_merge, Namespace(slug="feat", notify="wB:p2"))
        self.assertEqual(code, 0)
        self.assertIn("merged feat into main", out)
        self.assertEqual(notes[-1][0], "wB:p2")
        self.assertIn("harness: feat merged into main at", notes[-1][1])
        self.assertEqual(
            h.Frontmatter.read(self.r.root / "harness" / "features" / "feat" / "spec.md")["status"], "done"
        )
        self.assertTrue((self.r.root / "tests" / "test_feat.py").exists())
        self.assertFalse(self.r.wt.exists())
        self.assertNotIn("feat", self.r.git("branch", "--format=%(refname:short)"))
        self.assertFalse(h.Git(self.r.root).ref_exists("refs/harness/feat/review-start"))


# ----------------------------------------------------------------------------- the herdr adapter, through a fake herdr
class HerdrAdapter(Case):
    fake_herdr = True
    ACFG = {"harness": "claude", "model": "m", "effort": "high", "permissions": "full"}

    def test_prompt_or_start_starts_the_agent_in_its_own_tab(self):
        settled, note = h.HERDR.prompt_or_start(
            self.r.root, self.r.wt, "implementer", "feat", "do it", 1000, agent(self.ACFG)
        )
        self.assertEqual((settled, note), (True, ""))
        self.assertEqual(
            self.calls("tab", "create"),
            [["tab", "create", "--workspace", "w1", "--cwd", str(self.r.wt), "--label", "implementer", "--no-focus"]],
        )
        self.assertEqual(
            self.calls("agent", "start"),
            [
                [
                    "agent",
                    "start",
                    "impl-feat",
                    "--kind",
                    "claude",
                    "--pane",
                    "w1:p2",
                    "--timeout",
                    "120000",
                    "--",
                    "--model",
                    "m",
                    "--effort",
                    "high",
                    "--dangerously-skip-permissions",
                ]
            ],
        )
        prompts = self.calls("agent", "prompt")
        self.assertEqual(len(prompts), 2, "the first prompt finds no agent; the second follows the start")
        self.assertEqual(prompts[-1], ["agent", "prompt", "impl-feat", "do it", "--wait", "--timeout", "1000"])

    def test_repo_placeholder_is_the_main_checkout(self):
        acfg = {"harness": "codex", "args": ["--add-dir", "{repo}/.git"]}
        h.HERDR.prompt_or_start(
            self.r.wt, self.r.wt, "reviewer", "feat", "do it", 1000, agent(acfg)
        )  # from inside the worktree
        self.assertEqual(self.calls("agent", "start")[0][-2:], ["--add-dir", f"{self.r.root.resolve()}/.git"])

    def test_startup_dialogs_are_answered(self):
        self.state.update(start_status="blocked", screen="Do you trust this folder? (y/n)")
        self.save_state()
        settled, note = h.HERDR.prompt_or_start(
            self.r.root, self.r.wt, "reviewer", "feat", "go", 1000, agent(self.ACFG)
        )
        self.assertEqual((settled, note), (True, ""))
        self.assertEqual(
            self.calls("agent", "send-keys"),
            [["agent", "send-keys", "rev-feat", "down"], ["agent", "send-keys", "rev-feat", "enter"]],
        )
        self.state.update(screen="Something unexpected?", agents={})
        self.save_state()
        settled, note = h.HERDR.prompt_or_start(
            self.r.root, self.r.wt, "spec_author", "feat", "go", 1000, agent(self.ACFG)
        )
        self.assertEqual((settled, note), (True, "blocked at startup"))
        self.state.update(screen="Do you trust this folder? (y/n)", agents={})
        self.save_state()
        codex = {"harness": "codex", "permissions": "full"}
        settled, note = h.HERDR.prompt_or_start(self.r.root, self.r.wt, "implementer", "feat", "go", 1000, agent(codex))
        self.assertEqual(note, "blocked at startup", "a dialog is answered only for the CLI known to show it")
        self.assertEqual(len(self.calls("agent", "send-keys")), 2)

    def test_prompt_outcomes_for_an_existing_agent(self):
        self.state["agents"] = {"impl-feat": {"status": "idle"}}
        self.state["prompt_error"] = "timeout"
        self.save_state()
        self.assertEqual(
            h.HERDR.prompt_or_start(self.r.root, self.r.wt, "implementer", "feat", "x", 5, agent(self.ACFG)),
            (False, ""),
        )
        self.state["prompt_error"] = "agent_blocked"
        self.save_state()
        self.assertEqual(
            h.HERDR.prompt_or_start(self.r.root, self.r.wt, "implementer", "feat", "x", 5, agent(self.ACFG)),
            (True, "agent_blocked"),
        )
        self.assertEqual(self.calls("agent", "start"), [], "an existing agent is never started again")
        self.assertEqual(h.HERDR.state("impl-feat"), "idle")
        self.assertEqual(h.HERDR.state("nobody"), "gone")
        self.assertEqual(h.HERDR.wait("nobody", 5), (True, "agent_not_found"))
        self.state["wait_error"] = "timeout"
        self.save_state()
        self.assertEqual(h.HERDR.wait("impl-feat", 5), (False, ""))

    def test_launch_driver_uses_the_driver_tab(self):
        stamp = h.DriverLock(self.r.wt).stamp
        self.state["touch_on_pane_run"] = str(stamp)
        self.save_state()
        h.DriverLock(self.r.wt).path.write_text("999999999")  # a lock left by a driver that died
        pane = h.HERDR.launch_driver(self.r.root, self.r.wt, "feat", "two\nlines of feedback", "wB:p2")
        self.assertEqual(pane, "w1:p2")
        self.assertFalse(h.DriverLock(self.r.wt).path.exists(), "the stale lock was removed before the launch")
        self.assertEqual(self.calls("tab", "create")[0][6:], ["--label", "driver", "--no-focus"])
        run = self.calls("pane", "run")[0]
        self.assertEqual(run[:3], ["pane", "run", "w1:p2"])
        self.assertIn(f"{h.__file__} drive feat 'two lines of feedback' --notify wB:p2", run[3])
        self.assertTrue(stamp.exists(), "the launch is confirmed by the driver's start stamp")
        # the tab exists: herdr lists its Sidebar pane first; the driver must go to the shell pane
        sidebar = {"pane_id": "w1:p8", "tab_id": "w1:t5", "label": "Sidebar"}
        self.state.update(
            tabs=[{"tab_id": "w1:t5", "label": "driver"}], panes=[sidebar, {"pane_id": "w1:p7", "tab_id": "w1:t5"}]
        )
        self.save_state()
        self.assertEqual(h.HERDR.launch_driver(self.r.root, self.r.wt, "feat", "", ""), "w1:p7")
        self.assertEqual(len(self.calls("tab", "create")), 1, "an existing driver tab is reused")
        self.assertTrue(self.calls("pane", "run")[-1][3].endswith("drive feat"))
        self.assertEqual(self.calls("pane", "run")[-1][2], "w1:p7", "never the Sidebar pane")
        self.state["panes"] = [sidebar, {"pane_id": "w1:p9", "tab_id": "w1:t5", "agent": "claude"}]
        self.save_state()
        self.assertEqual(
            h.HERDR.launch_driver(self.r.root, self.r.wt, "feat", "", ""), "w1:p9", "the only shell-ish pane left"
        )

    def test_launch_driver_reports_a_driver_that_did_not_start(self):
        with self.assertRaises(h.HerdrError) as ctx:
            h.HERDR.launch_driver(self.r.root, self.r.wt, "feat", "", "")
        self.assertEqual(ctx.exception.code, "driver_not_started")
        self.assertIn("herdr pane read w1:p2", ctx.exception.message)
        self.assertEqual(len(self.calls("pane", "run")), 1)

    def test_worktree_create_nests_under_the_callers_workspace(self):
        self.setenv("HERDR_WORKSPACE_ID", "w0")
        res = h.HERDR.create_worktree(self.r.root, "newf", "main", trust=True)
        self.assertEqual(res["worktree"]["path"], str(self.r.tmp / "new-wt"))
        create = self.calls("worktree", "create")[0]
        self.assertEqual(
            create[2:4], ["--workspace", "w0"], "the caller's workspace is on the main checkout: nest under it"
        )
        self.assertEqual(
            create[4:], ["--branch", "newf", "--base", "main", "--label", "newf", "--no-focus", "--trust-repository"]
        )
        self.setenv("HERDR_WORKSPACE_ID", None)
        h.HERDR.create_worktree(self.r.root, "other", "main", trust=False)
        self.assertEqual(self.calls("worktree", "create")[1][2:4], ["--cwd", str(self.r.root)])
        self.assertNotIn("--trust-repository", self.calls("worktree", "create")[1])
        self.assertEqual(h.HERDR.open_workspace(self.r.root, self.r.wt), "w1")
        closed = self.r.tmp / "closed"  # a real worktree whose herdr workspace is closed: it gets reopened
        self.r.git("worktree", "add", "-q", "-b", "closed", str(closed))
        self.assertEqual(h.HERDR.open_workspace(self.r.root, closed), "w9")
        self.assertEqual(
            self.calls("worktree", "open")[0][2:],
            ["--cwd", str(self.r.root), "--path", str(closed), "--no-focus"],
        )

    def test_sidebar_notify_and_remove_worktree(self):
        h.HERDR.sidebar(self.r.wt, "spec ready · needs you: approve")
        self.assertEqual(
            self.calls("workspace", "report-metadata"),
            [
                [
                    "workspace",
                    "report-metadata",
                    "w1",
                    "--source",
                    "harness",
                    "--token",
                    "harness=spec ready · needs you: approve",
                ]
            ],
        )
        self.state["agents"] = {"wB:p2": {"status": "idle"}}
        self.save_state()
        h.HERDR.notify("wB:p2", "harness: feat is done")
        self.assertEqual(self.calls("agent", "prompt")[-1], ["agent", "prompt", "wB:p2", "harness: feat is done"])
        self.r.git("merge", "-q", "--no-edit", "feat")  # the branch must be merged for `git branch -d`
        self.r.write_cfg(teardown_cmd="echo torn > teardown.txt")
        _, out = self.output(h.remove_worktree, self.r.root, self.r.wt, "feat", self.r.cfg())
        self.assertIn("teardown_cmd: ok", out)
        self.assertEqual(self.calls("worktree", "remove"), [["worktree", "remove", "--workspace", "w1", "--force"]])
        self.assertFalse(self.r.wt.exists())
        self.assertNotIn("feat", self.r.git("branch", "--format=%(refname:short)"))

    def test_doctor_sees_herdr_leftovers(self):
        main = str(self.r.root)
        self.state["workspaces"] = [
            {
                "workspace_id": "w0",
                "label": "main",
                "worktree": {"checkout_path": main, "is_linked_worktree": False, "repo_root": main},
            },
            {
                "workspace_id": "w7",
                "label": "dup",
                "worktree": {"checkout_path": main, "is_linked_worktree": False, "repo_root": main},
            },
            {
                "workspace_id": "w8",
                "label": "gone",
                "worktree": {"checkout_path": str(self.r.tmp / "gone"), "is_linked_worktree": True, "repo_root": main},
            },
        ]
        self.save_state()
        doc = h.Doctor(self.r.root)
        _, out = self.output(doc.herdr_state)
        self.assertIn("one herdr workspace on the main checkout (found w0, w7)", "\n".join(doc.problems))
        self.assertIn("workspace w8 'gone' points at a removed worktree", "\n".join(doc.problems))
        self.assertIn("herdr workspace close w8", out)


class DoctorChecks(Case):
    def test_an_empty_project_is_not_a_red_baseline(self):
        self.r.git("rm", "-r", "-q", "src", "tests")
        self.r.commit("only the template")
        self.assertEqual(h.Git(self.r.root).project_paths(), set())
        self.r.write_cfg(test_cmd="false", setup_cmd="touch setup-ran")
        doc = h.Doctor(self.r.root)
        _, out = self.output(doc.baseline, self.r.cfg())
        self.assertEqual((out, doc.problems), ("", []))
        self.assertIn("no project code yet", doc.notes[0])
        dst = self.r.tmp / "new-wt"
        dst.mkdir()
        _, out = self.output(h.WorktreeSetup(self.r.root, dst, self.r.cfg()).run)
        self.assertIn("setup_cmd: skipped (no project code yet", out)
        self.assertFalse((dst / "setup-ran").exists())
        (self.r.root / "main.go").write_text("package main\n")  # any file outside the template's is project code
        self.r.commit("first code")
        self.assertEqual(h.Git(self.r.root).project_paths(), {"main.go"})
        _, out = self.output(h.WorktreeSetup(self.r.root, dst, self.r.cfg()).run)
        self.assertIn("setup_cmd: ok", out)
        self.assertTrue((dst / "setup-ran").exists())
        doc = h.Doctor(self.r.root)
        _, out = self.output(doc.baseline, self.r.cfg())
        self.assertIn("FAIL  test_cmd green on the main checkout (false)", out)

    def test_project_section_placeholders(self):
        agents_md = self.r.root / "AGENTS.md"
        doc = h.Doctor(self.r.root)
        doc.project_section()
        self.assertEqual(doc.notes, [], "no AGENTS.md: nothing to say")
        shipped = (HERE.parent / "AGENTS.md").read_text()
        agents_md.write_text(shipped)
        doc.project_section()
        self.assertEqual(len(doc.notes), 1)
        self.assertIn("AGENTS.md §1 is not filled in yet", doc.notes[0])
        start, end = shipped.index("## 1."), shipped.index("## 2.")
        filled = (
            "## 1. Project\n<!-- Filled in by the orchestrator with the human before the first feature. -->\n"
            "- What it is: a CLI that renames photos by date\n- Stack and layout: Go, cmd/ and internal/\n"
            "- Conventions: gofmt; errors wrapped with %w\n\n"
        )
        agents_md.write_text(shipped[:start] + filled + shipped[end:])
        doc = h.Doctor(self.r.root)
        doc.project_section()
        self.assertEqual(doc.notes, [], "filled in; the HTML comment and later sections do not count")

    def test_baseline_runs_test_cmd_unless_skipped(self):
        self.r.write_cfg(test_cmd="echo boom; false")
        doc = h.Doctor(self.r.root)
        _, out = self.output(doc.baseline, self.r.cfg())
        self.assertIn("FAIL  test_cmd green on the main checkout (echo boom; false)", out)
        self.assertIn("boom", out)
        self.assertIn("blamed on the first feature's implementer", out)
        doc = h.Doctor(self.r.root, tests=False)
        _, out = self.output(doc.baseline, self.r.cfg())
        self.assertEqual((out, doc.problems), ("", []))
        self.assertIn("test_cmd not run (--no-tests)", doc.notes)
        self.r.write_cfg(test_cmd="true")
        doc = h.Doctor(self.r.root)
        _, out = self.output(doc.baseline, self.r.cfg())
        self.assertIn("ok    test_cmd green on the main checkout (true)", out)

    def test_models_probe_each_cli_and_model_once(self):
        bindir, log = self.r.tmp / "bin", self.r.tmp / "probes.log"
        bindir.mkdir()
        fake = (
            f'#!/bin/sh\necho "$(basename "$0") $*" >> {log}\n'
            'case "$*" in *bad*) echo "unknown model" >&2; exit 1;; esac\necho OK\n'
        )
        for cli in ("claude", "codex"):
            (bindir / cli).write_text(fake)
            (bindir / cli).chmod(0o755)
        self.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
        self.r.write_cfg(
            agents={
                "spec_author": {"harness": "claude", "model": "good", "permissions": "full"},
                "implementer": {"harness": "pi", "model": "x/y", "permissions": "full"},
                "reviewer": {"harness": "codex", "model": "bad", "permissions": "guarded"},
            }
        )
        doc = h.Doctor(self.r.root, models=True)
        _, out = self.output(doc.models_check, self.r.cfg())
        self.assertIn("ok    spec_author: claude answers with model good", out)
        self.assertIn("FAIL  reviewer: codex answers with model bad", out)
        self.assertIn("unknown model", out)
        self.assertIn("implementer: pi model not checked", doc.notes)
        probes = log.read_text().splitlines()
        self.assertEqual(len(probes), 2, "one probe per (CLI, model) pair")
        self.assertIn("codex exec -m bad --skip-git-repo-check Reply with the word OK.", probes)
        doc = h.Doctor(self.r.root)  # without --models nothing is probed
        doc.herdr_state = lambda: None
        self.stub("probe_model", lambda *a: self.fail("probed without --models"))
        self.output(doc.run)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], *sys.argv[1:]])
