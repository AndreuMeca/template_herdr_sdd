#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""SDD harness launcher on herdr. Python 3 stdlib only. Needs git; start/run/wait/drive/merge also need herdr.

  harness.py config                        the setup in a few lines: roles, models, gates, commands (anyone)
                     [--apply]             ... and write the orchestrator's model/permissions for Claude Code (human)
  harness.py status                        board across every worktree, grouped by whose move it is (anyone)
  harness.py start <slug> ["intent"]       new worktree + branch + feature folder (orchestrator or human, base branch)
                     [--detach]            ... plus a dedicated orchestrator agent in it
  harness.py drive <slug> ["msg"] --tab    run the whole loop in a `driver` tab until a human is needed (orchestrator)
  harness.py run <role> <slug> ["msg"]     run one role agent in the feature's worktree (orchestrator, from anywhere)
  harness.py wait <role> <slug>            keep waiting for a running role agent        (orchestrator)
  harness.py approve <slug>                approve a spec; the driver or orchestrator continues (human)
  harness.py merge <slug>                  merge an approved, reviewed feature          (human, base branch)
  harness.py check [slug] [--tests]        structural checks (+ tests)                  (everyone)
  harness.py doctor                        tools, config, leftovers                     (anyone)

Rules: harness/docs/sdd.md. Config: harness/config.json. Own tests: python3 harness/selftest.py.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

STATUSES = ["pending", "spec_ready", "approved", "in_progress", "review", "done", "blocked"]
RANK = {"pending": 0, "blocked": 0, "spec_ready": 1, "approved": 2, "in_progress": 3, "review": 4, "done": 5}
VERDICTS = ("APPROVED", "CHANGES_REQUESTED")
ROLES = {"spec_author": "spec", "implementer": "impl", "reviewer": "rev"}
AGENT_ROLES = ("orchestrator", *ROLES)  # every entry of agents in config.json
RUN_GATE = {
    "spec_author": {"pending", "spec_ready", "blocked"},
    "implementer": {"approved", "in_progress", "review", "blocked"},
    "reviewer": {"review"},
}
DONE_WHEN = {  # on-disk evidence that a role finished its job
    "spec_author": lambda st, vd: st in ("spec_ready", "blocked"),
    "implementer": lambda st, vd: st in ("review", "blocked"),
    "reviewer": lambda st, vd: vd in VERDICTS,
}
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,23}$")
REQ_RE = re.compile(r"^- (R\d+):\s*(.*)$")
TASK_RE = re.compile(r"^- \[([ xX])\] (T\d+):\s*(.*)$")
# sdd §3: WHILE/WHEN/WHERE clauses (combinable), an optional `IF ..., THEN`, then `THE <subject> SHALL <response>`
EARS_RE = re.compile(r"^(?:(?:WHILE|WHEN|WHERE) .+?, )*(?:IF .+?, THEN )?THE \S.*? SHALL \S")
EARS_HINT = "THE ... SHALL / WHEN|WHILE|WHERE ..., THE ... SHALL / IF ..., THEN THE ... SHALL"
# the example placeholders of harness/features/_template/: a requirement or task still holding one was never written
TEMPLATE_PLACEHOLDERS = ("<trigger>", "<response>", "<unwanted condition>", "<task>")
EXIT_DONE, EXIT_ERROR, EXIT_ATTENTION, EXIT_WORKING = 0, 1, 2, 3
DEFAULT_TIMEOUT_MS = 100_000  # below Claude Code's 120 s default shell timeout
DRIVE_WAIT_MS = 300_000  # the driver waits for a role in slices of this length
DEFAULT_ROLE_MINUTES = 180  # the driver stops for a human when one role run takes longer (max_role_minutes)
FIX_TRIES = 3  # how often the driver sends a failed check back to the same role before stopping
REVIEW_REF = "refs/harness/{slug}/review-start"  # set by `run reviewer`, dropped by `run implementer` and `merge`
SYNC_MSG = (
    "The base branch '{base}' was merged into this branch and the merge conflicts. "
    "Resolve the conflicts, keep `check --tests` green, commit."
)
# config.json speaks one vocabulary for every CLI; CLIS translates it (harness/help/config.md has the same tables)
EFFORT_LEVELS = ("low", "medium", "high", "extra", "max")
PERMISSION_LEVELS = ("ask", "edits", "guarded", "full")
ROLE_PERMISSIONS = ("guarded", "full")  # a role agent runs unattended: it must never stop to ask
PERMISSION_WORDS = {
    "ask": "asks before edits and commands",
    "edits": "edits freely, asks before commands",
    "guarded": "never asks, the CLI blocks risky actions",
    "full": "never asks, no limits",
}
CLAUDE_MODES = {"ask": "default", "edits": "acceptEdits", "guarded": "auto", "full": "bypassPermissions"}
PROVIDER_EFFORTS = {"minimal": "low", "xhigh": "extra"}  # provider words people may type, and ours
PERMISSION_FLAGS = ("--dangerously-", "--permission-mode", "--sandbox", "--ask-for-approval", "--yolo")


@dataclass(frozen=True)
class Cli:
    """Everything the launcher knows about one agent CLI: how it takes our effort and permission words, how
    `doctor --models` asks it one question, and which startup dialogs it may show that are safe to accept.
    Adding a CLI is adding one entry to CLIS (and one column to the tables in harness/help/config.md)."""

    effort_flag: tuple  # the flag(s) for a reasoning effort, "{}" standing for the CLI's own level
    efforts: dict  # our effort level -> the CLI's
    permissions: dict  # our permission level -> the CLI's flags; a level missing here is one the CLI lacks
    guarded_by: str = ""  # what limits the agent under `guarded`, for `config` to say
    probe: tuple = ()  # a one-shot command for `doctor --models`; "{model}" and "{prompt}" are filled in
    startup_dialogs: tuple = ()  # screen text of first-run dialogs the launcher accepts (option below the default)

    def effort_args(self, level: str) -> list:
        """The flags for one of our effort levels; empty when unset (the CLI's default)."""
        return [part.format(self.efforts[level]) for part in self.effort_flag] if level else []

    def permission_args(self, level: str) -> list:
        """The flags for one of our permission levels; empty when unset (the CLI's default)."""
        return list(self.permissions.get(level, [])) if level else []

    def probe_cmd(self, model: str, prompt: str) -> list:
        return [part.format(model=model, prompt=prompt) for part in self.probe]

    def shows_startup_dialog(self, screen_text: str) -> bool:
        return any(text in screen_text for text in self.startup_dialogs)


SAME_EFFORTS = {"low": "low", "medium": "medium", "high": "high"}
CLIS = {
    "claude": Cli(
        effort_flag=("--effort", "{}"),
        efforts={**SAME_EFFORTS, "extra": "xhigh", "max": "max"},
        permissions={
            "ask": ["--permission-mode", "manual"],
            "edits": ["--permission-mode", "acceptEdits"],
            "guarded": ["--permission-mode", "auto"],
            "full": ["--dangerously-skip-permissions"],
        },
        guarded_by="auto mode's classifier",
        probe=("claude", "-p", "--model", "{model}", "{prompt}"),
        startup_dialogs=("trust this folder", "Bypass Permissions mode", "Yes, I accept"),
    ),
    "codex": Cli(
        effort_flag=("-c", "model_reasoning_effort={}"),
        efforts={**SAME_EFFORTS, "extra": "xhigh", "max": "xhigh"},  # codex has nothing above xhigh
        permissions={
            "ask": ["--sandbox", "workspace-write", "--ask-for-approval", "on-request"],
            "guarded": ["--sandbox", "workspace-write", "--ask-for-approval", "never", "--add-dir", "{repo}/.git"],
            "full": ["--dangerously-bypass-approvals-and-sandbox"],
        },
        guarded_by="its sandbox: writes only in the worktree",
        probe=("codex", "exec", "-m", "{model}", "--skip-git-repo-check", "{prompt}"),
    ),
    "pi": Cli(
        effort_flag=("--thinking", "{}"),
        efforts={**SAME_EFFORTS, "extra": "xhigh", "max": "max"},
        permissions={"full": []},  # pi has no permission modes: it never asks
    ),
}
SIDEBAR_ROWS = '[ui.sidebar.spaces]\nrows = [["state_icon", "workspace"], ["branch", "git_status"], ["$harness"]]'


# ----------------------------------------------------------------------------- shell / git
class Die(SystemExit):
    """What `die` raises: the exit code, plus the message for a caller that reports it elsewhere."""

    def __init__(self, msg: str, code: int):
        super().__init__(code)
        self.msg = msg


def die(msg: str, code: int = EXIT_ERROR):
    print(f"error: {msg}", file=sys.stderr)
    raise Die(msg, code)


def sh(args, cwd=None, check=True, stdin=None):
    p = subprocess.run([str(a) for a in args], cwd=cwd, text=True, capture_output=True, stdin=stdin)
    if check and p.returncode != 0:
        die(f"{' '.join(str(a) for a in args)}\n{(p.stderr or p.stdout).strip()}")
    return p


def shell(cmd: str, cwd: Path) -> tuple[int, str]:
    """Run a configured shell command; (exit code, the tail of its output)."""
    p = subprocess.run(cmd, shell=True, cwd=cwd, text=True, capture_output=True, stdin=subprocess.DEVNULL)
    return p.returncode, (p.stdout + p.stderr).strip()[-1500:]


class Git:
    """git in one checkout (the main one or a feature worktree). Every git call of the launcher goes through here."""

    def __init__(self, path):
        self.path = Path(path)

    @classmethod
    def here(cls) -> Git:
        """The checkout the current directory belongs to."""
        return cls(Path(sh(["git", "rev-parse", "--show-toplevel"]).stdout.strip()))

    def proc(self, *args, check: bool = True) -> subprocess.CompletedProcess:
        return sh(["git", *args], cwd=self.path, check=check)

    def run(self, *args, check: bool = True) -> str:
        """stdout of `git <args>`, stripped; dies on failure unless check=False."""
        return self.proc(*args, check=check).stdout.strip()

    def ok(self, *args) -> bool:
        return self.proc(*args, check=False).returncode == 0

    def paths(self, *args) -> set[str]:
        """Paths from a git command run with -z (NUL-separated, never quoted)."""
        return {p for p in self.proc(*args, "-z", check=False).stdout.split("\0") if p}

    # --- where we are
    def head(self) -> str:
        return self.run("rev-parse", "HEAD")

    def branch(self) -> str:
        return self.run("branch", "--show-current")

    def is_main(self) -> bool:
        common = self.run("rev-parse", "--git-common-dir")
        return common == ".git" or Path(common).resolve() == (self.path / ".git").resolve()

    def main_checkout(self) -> Path:
        """The main checkout of the repo this checkout belongs to (itself when it is one)."""
        return Path(self.run("rev-parse", "--path-format=absolute", "--git-common-dir")).parent

    def git_dir(self) -> Path:
        """This checkout's own git dir (a worktree's lives under the main checkout's .git)."""
        return Path(self.run("rev-parse", "--path-format=absolute", "--git-dir"))

    def worktrees(self) -> list[tuple[Path, str]]:
        """[(path, branch)] from `git worktree list --porcelain`; the main checkout comes first."""
        items, path, branch = [], None, ""
        for line in self.run("worktree", "list", "--porcelain").splitlines() + [""]:
            if line.startswith("worktree "):
                path = Path(line[len("worktree ") :])
            elif line.startswith("branch refs/heads/"):
                branch = line[len("branch refs/heads/") :]
            elif line == "" and path is not None:
                items.append((path, branch))
                path, branch = None, ""
        return items

    # --- refs
    def ref_exists(self, ref: str) -> bool:
        return self.ok("rev-parse", "-q", "--verify", ref)

    def set_ref(self, ref: str, rev: str = "HEAD"):
        self.run("update-ref", ref, rev)

    def drop_ref(self, ref: str):
        self.proc("update-ref", "-d", ref, check=False)

    # --- the working tree
    def porcelain(self) -> list[tuple[str, str]]:
        """[(XY, path)] from `git status --porcelain`, untracked files listed one by one, paths unquoted."""
        fields = self.proc("status", "--porcelain", "-z", "--untracked-files=all").stdout.split("\0")
        items, i = [], 0
        while i < len(fields):
            entry, i = fields[i], i + 1
            if len(entry) < 4:
                continue
            xy = entry[:2]
            if xy[0] in "RC":
                i += 1  # the original path follows as its own field
            items.append((xy, entry[3:]))
        return items

    def dirty(self) -> bool:
        return bool(self.run("status", "--porcelain"))

    def changed_paths(self, base: str) -> list[str]:
        """Paths the branch changed since it forked from (or last merged) the base branch, plus uncommitted and
        untracked ones."""
        committed = self.paths("diff", "--name-only", f"{base}...HEAD")
        return sorted(committed | {p for _, p in self.porcelain()})

    def is_ignored(self, path) -> bool:
        return self.ok("check-ignore", "-q", path)

    def show(self, rev: str, path: str) -> str:
        """A file as it is at `rev`; empty when it is not there."""
        return self.run("show", f"{rev}:{path}", check=False)

    def commit(self, msg: str, *paths):
        """Stage `paths` and commit them (only them, when given)."""
        if paths:
            self.run("add", *paths)
        self.run("commit", "-q", "-m", msg, *(["--", *paths] if paths else []))

    def merge_in(self, base: str) -> tuple[bool, bool]:
        """Merge the base branch into this checkout's branch. Returns (merged cleanly, HEAD moved); a conflict is
        aborted."""
        before = self.head()
        if not self.ok("merge", "--no-edit", base):
            self.proc("merge", "--abort", check=False)
            return False, False
        return True, self.head() != before


# ----------------------------------------------------------------------------- config / worktree files
def valid_pattern(x) -> bool:
    return isinstance(x, str) and bool(Path(x).parts) and not x.startswith("/") and ".." not in Path(x).parts


@dataclass(frozen=True)
class AgentConfig:
    """One `agents.<role>` entry of config.json, in the file's own words (see EFFORT_LEVELS, PERMISSION_LEVELS);
    its Cli (when the launcher knows the CLI) turns them into flags."""

    role: str
    kind: str
    model: str = ""
    effort: str = ""
    permissions: str = ""
    args: tuple = ()

    @classmethod
    def parse(cls, role: str, raw, where: str) -> AgentConfig:
        """The entry after checking it; dies naming the first problem. The flags `effort` and `permissions` translate
        to are never written by hand in `args`."""
        if not isinstance(raw, dict) or not raw.get("kind"):
            die(f"{where}.kind is required")
        kind, effort, perm = raw["kind"], raw.get("effort", ""), raw.get("permissions", "")
        cli = CLIS.get(kind)
        if not isinstance(effort, str) or (effort and effort not in EFFORT_LEVELS):
            hint = f" ('{effort}' is the provider's word; here it is '{PROVIDER_EFFORTS[effort]}')" if (
                effort in PROVIDER_EFFORTS
            ) else ""  # fmt: skip
            die(f"{where}.effort must be one of {', '.join(EFFORT_LEVELS)}{hint}")
        if not isinstance(perm, str) or (perm and perm not in PERMISSION_LEVELS):
            die(f"{where}.permissions must be one of {', '.join(PERMISSION_LEVELS)}")
        if role in ROLES and perm not in ROLE_PERMISSIONS:
            why = f"'{perm}' would stop it at its first prompt" if perm else "it is required"
            die(f"{where}.permissions: {why}; a role agent runs unattended: use {' or '.join(ROLE_PERMISSIONS)}")
        if cli and perm and perm not in cli.permissions:
            die(f"{where}.permissions '{perm}' is not available for {kind} (it has: {', '.join(cli.permissions)})")
        args = raw.get("args") or []
        if not isinstance(args, list):
            die(f"{where}.args must be a list of strings")
        own = [a for a in args if str(a).startswith(PERMISSION_FLAGS)]
        if cli and perm and own:
            die(f"{where}.args sets permissions itself ({own[0]}); `permissions: {perm}` already does, drop the flag")
        return cls(role, kind, raw.get("model") or "", effort, perm, tuple(str(a) for a in args))

    @property
    def cli(self) -> Cli | None:
        return CLIS.get(self.kind)

    def cli_args(self, repo: Path) -> list:
        """The CLI flags after `--`: model, effort, permissions (translated by its Cli; a CLI not in CLIS gets
        neither, `config` says so), then `args` verbatim. `{repo}` in any of them becomes the main checkout's path."""
        cli = self.cli
        model = ["--model", self.model] if self.model else []
        flags = [*cli.effort_args(self.effort), *cli.permission_args(self.permissions)] if cli else []
        flags = [x.replace("{repo}", str(repo)) for x in [*flags, *self.args]]
        return ["--", *model, *flags] if (model or flags) else []

    def claude_settings(self) -> dict:
        """What Claude Code should start with in this repo when this is the orchestrator: its model, and the
        permission mode of its `permissions` level (CLAUDE_MODES; unset = ask = Claude Code's default)."""
        out: dict = {"permissions": {"defaultMode": CLAUDE_MODES[self.permissions or "ask"]}}
        if self.model:
            out["model"] = self.model
        return out

    def describe(self) -> list[str]:
        """How `config` shows its effort and permissions: our words, then what its CLI is given."""
        cli, effort, perm = self.cli, self.effort, self.permissions
        if not effort:
            e = "effort       - (the CLI's default)"
        elif not cli:
            e = f"effort       {effort} (not translated for {self.kind}: put its flag in args)"
        else:
            native = cli.efforts[effort]
            e = f"effort       {effort}" + (f" ({self.kind}: {native})" if native != effort else "")
        if not perm:
            p = "permissions  - (the CLI's default)"
        elif not cli:
            p = f"permissions  {perm} (not translated for {self.kind}: put its flags in args)"
        else:
            flags = " ".join(cli.permissions[perm]) or "no flag needed"
            how = f"; {cli.guarded_by}" if perm == "guarded" and cli.guarded_by else ""
            p = f"permissions  {perm}: {PERMISSION_WORDS[perm]}{how} ({flags})"
        return [e, p] + (["args         " + " ".join(self.args)] if self.args else [])


@dataclass(frozen=True)
class Config:
    """harness/config.json, checked. Always the main checkout's copy: a branch cannot change what it is checked
    with."""

    base_branch: str
    test_cmd: str
    setup_cmd: str
    teardown_cmd: str
    copy_files: tuple
    max_review_rounds: int
    max_role_minutes: int
    gates: dict  # "approve" / "merge" -> "human" | "auto"
    agents: dict  # role -> AgentConfig

    @classmethod
    def load(cls, root: Path) -> Config:
        p = Git(root).main_checkout() / "harness" / "config.json"
        try:
            raw = json.loads(p.read_text())
        except FileNotFoundError:
            die(f"missing {p}")
        except json.JSONDecodeError as e:
            die(f"{p}: {e}")
        return cls.parse(raw, p)

    @classmethod
    def parse(cls, raw: dict, p: Path) -> Config:
        """The config after checking every key; dies naming the first problem."""
        for key in ("base_branch", "test_cmd", "agents"):
            if key not in raw:
                die(f"{p}: missing '{key}'")
        agents = raw["agents"] if isinstance(raw["agents"], dict) else {}
        agents = {role: AgentConfig.parse(role, agents.get(role), f"{p}: agents.{role}") for role in AGENT_ROLES}
        gates = {gate: (raw.get("gates") or {}).get(gate, "human") for gate in ("approve", "merge")}
        for gate, who in gates.items():
            if who not in ("human", "auto"):
                die(f"{p}: gates.{gate} must be 'human' or 'auto'")
        copy_files = raw.get("copy_files", [])
        if not isinstance(copy_files, list) or not all(valid_pattern(x) for x in copy_files):
            die(f"{p}: copy_files must be a list of paths or globs relative to the repo root")
        for key, default in (("max_review_rounds", 3), ("max_role_minutes", DEFAULT_ROLE_MINUTES)):
            n = raw.get(key, default)
            if not isinstance(n, int) or isinstance(n, bool) or n < 0:
                die(f"{p}: {key} must be an integer >= 0 (0 = no limit)")
        for key in ("setup_cmd", "teardown_cmd"):
            if not isinstance(raw.get(key, ""), str):
                die(f'{p}: {key} must be a shell command or ""')
        return cls(
            base_branch=raw["base_branch"],
            test_cmd=raw["test_cmd"],
            setup_cmd=raw.get("setup_cmd", ""),
            teardown_cmd=raw.get("teardown_cmd", ""),
            copy_files=tuple(copy_files),
            max_review_rounds=raw.get("max_review_rounds", 3),
            max_role_minutes=raw.get("max_role_minutes", DEFAULT_ROLE_MINUTES),
            gates=gates,
            agents=agents,
        )

    def agent(self, role: str) -> AgentConfig:
        return self.agents[role]


class ClaudeSettings:
    """.claude/settings.local.json (per machine, gitignored) against agents.orchestrator, so the Claude the human
    opens in this repo starts with that model and permission mode. `report` only reads: raising the permissions of
    the next session is the human's call (`config --apply` runs `apply`), never an agent's."""

    NOT_CLAUDE = "orchestrator is not Claude Code: .claude/settings.local.json left alone"
    APPLY_CMD = "uv run harness/harness.py config --apply"

    def __init__(self, root: Path, orchestrator: AgentConfig):
        self.path = root / ".claude" / "settings.local.json"
        self.orchestrator = orchestrator

    def compare(self) -> tuple[dict, dict, str] | None:
        """(current, wanted, summary); other keys in the file are kept. None when the orchestrator is not Claude
        Code."""
        if self.orchestrator.kind != "claude":
            return None
        want = self.orchestrator.claude_settings()
        try:
            current = json.loads(self.path.read_text()) if self.path.exists() else {}
        except json.JSONDecodeError:
            current = {}
        new = dict(current)
        new["permissions"] = {**(current.get("permissions") or {}), **want["permissions"]}
        if "model" in want:
            new["model"] = want["model"]
        mode = want["permissions"]["defaultMode"]
        return current, new, f"model {want.get('model', '(Claude Code default)')}, permissions {mode}"

    def report(self) -> str:
        """One read-only line: is the file in step with agents.orchestrator?"""
        s = self.compare()
        if s is None:
            return self.NOT_CLAUDE
        current, new, what = s
        if new == current:
            return f"Claude Code in this repo: {what} (in step with .claude/settings.local.json)"
        return (
            f"Claude Code in this repo should run with {what}, which .claude/settings.local.json does not say yet; "
            f"to apply, run yourself: {self.APPLY_CMD}, then restart Claude Code"
        )

    def apply(self) -> str:
        """`config --apply`: write the file. Returns one report line."""
        s = self.compare()
        if s is None:
            return self.NOT_CLAUDE
        current, new, what = s
        if new == current:
            return f"Claude Code in this repo: {what} (.claude/settings.local.json unchanged)"
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(new, indent=2) + "\n")
        return f"Claude Code in this repo: {what} (.claude/settings.local.json updated; restart Claude Code to apply)"


class WorktreeSetup:
    """What a new worktree needs beyond what git tracks: the gitignored files of copy_files (plus .worktreeinclude),
    copied from the main checkout, then setup_cmd."""

    def __init__(self, root: Path, wt: Path, cfg: Config):
        self.root, self.wt, self.cfg = root, wt, cfg

    def patterns(self) -> tuple[list[str], list[str]]:
        """copy_files from config.json plus the lines of .worktreeinclude; returns (patterns, rejected lines)."""
        pats, bad = list(self.cfg.copy_files), []
        wi = self.root / ".worktreeinclude"
        if wi.exists():
            for line in wi.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if not valid_pattern(line):
                    bad.append(line)
                elif line not in pats:
                    pats.append(line)
        return pats, bad

    @staticmethod
    def copy_path(s: Path, d: Path):
        """Copy a file, symlink or whole folder; the copy replaces whatever is at `d`."""
        d.parent.mkdir(parents=True, exist_ok=True)
        if d.is_symlink() or d.is_file():
            d.unlink()
        elif d.is_dir():
            shutil.rmtree(d)
        if s.is_dir():
            shutil.copytree(s, d, symlinks=True)
        else:
            shutil.copy2(s, d, follow_symlinks=False)

    def copy_ignored(self, patterns: list[str]) -> str:
        """Copy the gitignored paths matching `patterns` from the main checkout into the worktree. One report line."""
        src, dst = self.root, self.wt
        hits, missing = set(), []
        for pat in patterns:
            found = [s.relative_to(src) for s in src.glob(pat)]
            if not found:
                missing.append(pat)
            hits.update(found)
        done, skipped, failed = set(), [], []
        for rel in sorted(hits):  # parents sort before their children
            if any(p in done for p in rel.parents):
                continue  # inside a folder already copied whole
            if not Git(src).is_ignored(rel):
                skipped.append(str(rel))  # tracked or not ignored: the worktree already has git's version
                continue
            try:
                self.copy_path(src / rel, dst / rel)
                done.add(rel)
            except OSError as e:
                failed.append(f"{rel} ({e})")
        parts = [f"copied {', '.join(str(r) for r in sorted(done))}" if done else "nothing copied"]
        if missing:
            parts.append(f"not in the main checkout: {', '.join(missing)}")
        if skipped:
            parts.append(f"skipped, not gitignored: {', '.join(skipped)}")
        if failed:
            parts.append(f"FAILED: {'; '.join(failed)}")
        return "; ".join(parts)

    def run(self):
        """Copy the gitignored files, then run setup_cmd; prints one line for each."""
        pats, bad = self.patterns()
        if pats or bad:
            line = self.copy_ignored(pats) if pats else "nothing listed"
            print("copy_files:", line + (f"; rejected .worktreeinclude lines: {', '.join(bad)}" if bad else ""))
        if self.cfg.setup_cmd:
            code, out = shell(self.cfg.setup_cmd, self.wt)
            print("setup_cmd:", "ok" if code == 0 else f"FAILED (exit {code}); fix it in the worktree\n{out[-800:]}")


# ----------------------------------------------------------------------------- frontmatter / features
def fm_read(path: Path) -> dict:
    try:
        return fm_parse(path.read_text())
    except FileNotFoundError:
        return {}


def fm_parse(text: str) -> dict:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def fm_set(path: Path, key: str, value: str):
    lines = path.read_text().splitlines()
    if not lines or lines[0].strip() != "---":
        lines = ["---", f"{key}: {value}", "---", *lines]
    else:
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                lines.insert(i, f"{key}: {value}")
                break
            if lines[i].split(":", 1)[0].strip() == key:
                lines[i] = f"{key}: {value}"
                break
    path.write_text("\n".join(lines) + "\n")


def section(text: str, heading: str) -> list[str]:
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().lower() == heading.lower()
            continue
        if inside:
            out.append(line)
    return out


def requirement_lines(spec_text: str) -> list[str]:
    text = re.sub(r"<!--.*?-->", "", spec_text, flags=re.S)
    return [line.strip() for line in section(text, "Requirements") if REQ_RE.match(line.strip())]


def feature_dir(root: Path, slug: str) -> Path:
    return root / "harness" / "features" / slug


def feature_state(fdir: Path) -> tuple[str, str]:
    return fm_read(fdir / "spec.md").get("status", ""), fm_read(fdir / "review.md").get("verdict", "")


@dataclass(frozen=True)
class Feature:
    slug: str
    wt: Path  # the checkout holding its folder: its own worktree while live, else the base checkout
    branch: str  # that checkout's branch
    status: str
    verdict: str

    @property
    def dir(self) -> Path:
        return feature_dir(self.wt, self.slug)

    @property
    def live(self) -> bool:
        return self.branch == self.slug


def features_everywhere(root: Path) -> dict[str, Feature]:
    """slug -> Feature across every worktree; a worktree on the feature's own branch wins over the base copy."""
    found: dict[str, Feature] = {}
    for wt, branch in Git(root).worktrees():
        if not wt.exists():
            continue
        for spec in sorted((wt / "harness" / "features").glob("*/spec.md")):
            slug = spec.parent.name
            if slug.startswith("_"):
                continue
            if slug not in found or branch == slug:
                st, vd = feature_state(spec.parent)
                found[slug] = Feature(slug, wt, branch, st, vd)
    return found


def locate(root: Path, slug: str) -> Feature:
    f = features_everywhere(root).get(slug)
    if not f:
        die(f"no feature '{slug}' in any worktree (see: harness.py status)")
    return f


def live_worktree(root: Path, slug: str) -> Feature:
    f = locate(root, slug)
    if not f.live:
        die(
            f"'{slug}' has no live worktree (status {f.status or '?'} on '{f.branch or 'detached'}'); "
            f"create one with: harness.py start {slug}"
        )
    return f


def next_action(st: str, vd: str, gates: dict | None = None) -> str:
    gates = gates or {}
    if st == "review":
        return f"merge ({gates.get('merge', 'human')})" if vd == "APPROVED" else "run reviewer"
    return {
        "pending": "run spec_author",
        "spec_ready": f"approve ({gates.get('approve', 'human')})",
        "approved": "run implementer",
        "in_progress": "run implementer",
        "blocked": "human: read spec.md Open questions / log.md",
        "done": "-",
    }.get(st, "fix status (see docs/sdd.md)")


def needs_human(nxt: str) -> bool:
    return "human" in nxt or nxt.startswith("fix")


def state_text(st: str, vd: str, gates: dict, running: str = "") -> str:
    """One short line for the sidebar and the board: where the feature stands and whose move it is."""
    if running:
        return f"{running} · working"
    nxt = next_action(st, vd, gates)
    if nxt.startswith("run "):
        return f"{st.replace('_', ' ')} · {nxt[4:]} next"
    if nxt.startswith("approve"):
        return "spec ready · " + ("needs you: approve" if "human" in nxt else "auto-approve")
    if nxt.startswith("merge"):
        return "review approved · " + ("needs you: merge" if "human" in nxt else "auto-merge")
    if st == "blocked":
        return "blocked · needs you"
    return "done" if st == "done" else f"{st or '?'} · fix status"


def approve_commits(wt: Path, slug: str, base: str) -> list[str]:
    """The commits `approve` made on this branch, newest first."""
    return (
        Git(wt)
        .run("log", "--format=%H", "--grep", f"^{slug}: spec approved$", f"{base}..HEAD", check=False)
        .splitlines()
    )


def approved_once(wt: Path, slug: str, base: str) -> bool:
    return bool(approve_commits(wt, slug, base))


def review_rounds(wt: Path, slug: str, base: str) -> int:
    """Review rounds on this branch that ended in CHANGES_REQUESTED: the verdicts the launcher cleared (`drop_review`
    commits, one per round however often the reviewer committed), plus the verdict on disk now."""
    review = f"harness/features/{slug}/review.md"
    clears = Git(wt).run(
        "log", "--format=%H", "--grep", f"^{slug}: clear previous review$", f"{base}..HEAD", "--", review,
        check=False,
    ).splitlines()  # fmt: skip
    verdicts = [fm_parse(Git(wt).show(f"{sha}^", review)).get("verdict", "") for sha in clears]
    verdicts.append(feature_state(feature_dir(wt, slug))[1])
    return verdicts.count("CHANGES_REQUESTED")


def block_feature(wt: Path, slug: str, reason: str):
    """Set status blocked, record why in log.md, commit. Used when the launcher itself stops a feature."""
    fdir = feature_dir(wt, slug)
    fm_set(fdir / "spec.md", "status", "blocked")
    log = fdir / "log.md"
    head = "" if log.exists() else "# Log\n"
    with log.open("a") as fh:
        fh.write(f"{head}\n## {datetime.now():%Y-%m-%d %H:%M} launcher\n- blockers: {reason}\n")
    Git(wt).commit(f"{slug}: blocked by the launcher", f"harness/features/{slug}")


def drop_review(root: Path, slug: str):
    review = feature_dir(root, slug) / "review.md"
    rel = review.relative_to(root)
    if Git(root).proc("ls-files", "--error-unmatch", rel, check=False).returncode == 0:
        Git(root).run("rm", "-q", rel)
        Git(root).run("commit", "-q", "-m", f"{slug}: clear previous review", "--", rel)  # already staged by rm
    elif review.exists():
        review.unlink()


def approve_spec(wt: Path, slug: str, branch: str):
    spec = feature_dir(wt, slug) / "spec.md"
    fm_set(spec, "status", "approved")
    rel = spec.relative_to(wt)
    Git(wt).commit(f"{slug}: spec approved", rel)
    print(f"approved {slug} (committed on '{branch}')")


# ----------------------------------------------------------------------------- herdr
class HerdrError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


def herdr(*args) -> dict:
    """Run a herdr CLI command; return its JSON `result`. Errors (JSON on stderr) raise HerdrError."""
    if shutil.which("herdr") is None:
        die("herdr not found in PATH (https://herdr.dev/docs/install/)")
    p = subprocess.run(["herdr", *[str(a) for a in args]], text=True, capture_output=True)
    if p.returncode != 0:
        try:
            err = json.loads(p.stderr.strip().splitlines()[-1])["error"]
        except Exception:
            raise HerdrError("cli_error", (p.stderr or p.stdout).strip()[:500]) from None
        raise HerdrError(err.get("code", "error"), err.get("message", ""))
    try:
        return json.loads(p.stdout).get("result", {})
    except Exception:
        return {"raw": p.stdout}


def find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r is not None:
                return r
    return None


def repo_source(root: Path) -> list:
    """How to name the repo to `herdr worktree create|open`. The caller's own workspace when it is open on the
    main checkout, so new worktrees nest under it like the sidebar's "new worktree"; else the path, for which
    herdr may open a second workspace on the main checkout."""
    ws = os.environ.get("HERDR_WORKSPACE_ID")
    if ws:
        try:
            src = herdr("worktree", "list", "--workspace", ws).get("source", {})
        except HerdrError:
            src = {}
        if src.get("source_checkout_path") and Path(src["source_checkout_path"]).resolve() == root.resolve():
            return ["--workspace", ws]
    return ["--cwd", root]


def workspace_of(wt: Path) -> str | None:
    """The herdr workspace id open on this worktree, or None. Never opens one."""
    try:
        wts = herdr("worktree", "list", "--cwd", Git(wt).main_checkout()).get("worktrees", [])
    except HerdrError:
        return None
    return next((w.get("open_workspace_id") for w in wts if Path(w.get("path", "")).resolve() == wt.resolve()), None)


def worktree_workspace(root: Path, wt: Path) -> str:
    """The herdr workspace id of this worktree; opens the workspace if it is closed."""
    return (
        workspace_of(wt)
        or herdr("worktree", "open", *repo_source(root), "--path", wt, "--no-focus")["workspace"]["workspace_id"]
    )


def sidebar(wt: Path, text: str):
    """Show the feature's state on its workspace in herdr's sidebar (Space token `$harness`, see help/config.md).
    Never fatal."""
    if shutil.which("herdr") is None:
        return
    with contextlib.suppress(HerdrError):
        ws_id = workspace_of(wt)
        if ws_id:
            herdr("workspace", "report-metadata", ws_id, "--source", "harness", "--token", f"harness={text[:80]}")


def notify(pane: str, text: str):
    """Prompt the agent hosted in `pane` (the one that launched the driver). Reported, never fatal."""
    try:
        herdr("agent", "prompt", pane, text)
    except HerdrError as e:
        print(f"(could not notify pane {pane}: {e.code}: {e.message})")


def wait_agent(name: str, timeout_ms: int) -> tuple[bool, str]:
    """(settled, note) after waiting for the agent to leave the working state, at most timeout_ms."""
    try:
        herdr("agent", "wait", name, "--timeout", timeout_ms)
        return True, ""
    except HerdrError as e:
        if e.code == "timeout":
            return False, ""
        return True, e.code


def driver_lock(wt: Path) -> Path:
    return Git(wt).git_dir() / "harness-driver.pid"


def driver_stamp(wt: Path) -> Path:
    return driver_lock(wt).with_name("harness-driver.started")


def stamp_value(stamp: Path) -> tuple:
    """(mtime, content) of the start stamp, or () when there is none. A new driver changes it even if it exits at
    once."""
    try:
        return stamp.stat().st_mtime_ns, stamp.read_text()
    except FileNotFoundError:
        return ()


def is_driver_process(pid: int) -> bool:
    """False when `pid` runs something other than `harness.py drive` (the OS reused a dead driver's pid). When
    `ps` cannot say (missing, or blocked by a sandbox), trust the live pid."""
    try:
        p = subprocess.run(["ps", "-p", str(pid), "-o", "command="], text=True, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return True
    if p.returncode != 0:
        return bool(p.stderr.strip())  # silent: no such process (it just died); an error: unknown, trust the pid
    return "harness.py" in p.stdout and " drive" in p.stdout


def driver_running(wt: Path) -> int:
    """pid of a live driver for this worktree, else 0."""
    try:
        pid = int(driver_lock(wt).read_text().strip())
        os.kill(pid, 0)
    except (FileNotFoundError, ValueError, ProcessLookupError):
        return 0
    except OSError:
        pass  # alive, owned by someone else
    return pid if is_driver_process(pid) else 0


def acquire_driver_lock(wt: Path) -> int:
    """Take the driver lock atomically: 0 when it is ours now, else the pid of the driver holding it. A lock left by
    a dead driver (or by a pid the OS has reused) is replaced."""
    lock = driver_lock(wt)
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pid = driver_running(wt)
            if pid:
                return pid
            with contextlib.suppress(FileNotFoundError):
                lock.unlink()
            continue
        with os.fdopen(fd, "w") as fh:
            fh.write(str(os.getpid()))
        return 0
    return driver_running(wt) or -1


def driver_tab_pane(ws_id: str) -> str | None:
    """The root pane of the `driver` tab in this workspace, if any."""
    tabs = herdr("tab", "list", "--workspace", ws_id).get("tabs", [])
    tab = next((t for t in tabs if t.get("label") == "driver"), None)
    if not tab:
        return None
    panes = herdr("pane", "list", "--workspace", ws_id).get("panes", [])
    # herdr lists a tab's Sidebar pane (no shell) before its shell pane: never that one
    ours = [p for p in panes if p.get("tab_id") == tab.get("tab_id") and p.get("label") != "Sidebar"]
    if not ours:
        return None
    pane = next((p for p in ours if not p.get("agent")), ours[0])  # the driver runs in the plain shell pane
    return pane.get("pane_id")


def launch_driver(root: Path, wt: Path, slug: str, msg: str, notify_pane: str) -> str:
    """Run `drive <slug>` in the `driver` tab of the feature's workspace (created if missing). Returns the pane id."""
    ws_id = worktree_workspace(root, wt)
    pane = driver_tab_pane(ws_id)
    if not pane:
        tab = herdr("tab", "create", "--workspace", ws_id, "--cwd", wt, "--label", "driver", "--no-focus")
        pane = find_key(tab.get("root_pane", tab), "pane_id") or die(f"tab create returned no pane_id: {tab}")
        time.sleep(1.5)  # the new pane's shell needs a moment to reach its prompt
    msg = " ".join(msg.split())  # one line: the command is typed into the tab's shell
    cmd = [sys.executable, str(Path(__file__).resolve()), "drive", slug]
    cmd += [msg] if msg else []
    cmd += ["--notify", notify_pane] if notify_pane else []
    lock = driver_lock(wt)
    if lock.exists() and not driver_running(wt):
        lock.unlink()  # left by a driver that died
    stamp = driver_stamp(wt)  # every driver rewrites it first thing and never removes it: a start leaves a trace
    before = stamp_value(stamp)
    herdr("pane", "run", pane, shlex.join(cmd))
    for _ in range(20):
        if stamp_value(stamp) != before:
            return pane
        time.sleep(0.5)
    pid = driver_running(wt)
    if pid:  # another `drive --tab` won the lock first
        raise HerdrError("driver_running", f"a driver (pid {pid}) is already running {slug}")
    raise HerdrError(
        "driver_not_started", f"the driver did not start in pane {pane}; look at it: herdr pane read {pane} --lines 40"
    )


def agent_name(role: str, slug: str) -> str:
    return f"{ROLES[role]}-{slug}"


def agent_state(name: str) -> str:
    try:
        return find_key(herdr("agent", "get", name), "agent_status") or "?"
    except HerdrError:
        return "gone"


def agent_tail(name: str, lines: int = 40) -> str:
    try:
        res = herdr("agent", "read", name, "--source", "recent-unwrapped", "--lines", lines)
    except HerdrError as e:
        return f"(agent read failed: {e})"
    if "raw" in res:
        return res["raw"]
    text = find_key(res, "text") or find_key(res, "lines") or find_key(res, "content")
    if isinstance(text, list):
        text = "\n".join(str(t) for t in text)
    return str(text) if text else json.dumps(res)[:2000]


def screen(name: str, lines: int = 40) -> str:
    try:
        return agent_tail(name, lines)
    except Exception:
        return ""


def settle_startup(name: str, kind: str, timeout_s: int = 60) -> bool:
    """Answer the startup dialogs its CLI is known to show (Cli.startup_dialogs: Claude Code's trust-folder and
    bypass-permissions warnings) and wait for idle. Returns True when the agent is ready for prompts, False when it
    is still blocked on something else."""
    cli = CLIS.get(kind)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        state = agent_state(name)
        if state in ("idle", "done"):
            return True
        if state == "blocked":
            text = screen(name)
            if cli and cli.shows_startup_dialog(text):
                # such dialogs default to "No, exit"; the accepting option is the next one down
                herdr("agent", "send-keys", name, "down")
                herdr("agent", "send-keys", name, "enter")
                time.sleep(2)
                continue
            return False
        time.sleep(1)
    return agent_state(name) in ("idle", "done")


def start_agent(name: str, agent: AgentConfig, pane_id: str, repo: Path, tries: int = 1) -> bool:
    """Start a role or orchestrator agent in a pane; True when it is up (or already reported as not ready yet)."""
    for attempt in range(tries):  # a new pane's shell may need a moment to reach its prompt
        try:
            herdr(
                "agent",
                "start",
                name,
                "--kind",
                agent.kind,
                "--pane",
                pane_id,
                "--timeout",
                120_000,
                *agent.cli_args(repo),
            )
            return True
        except HerdrError as e:
            if e.code == "agent_not_ready":
                return True
            if attempt == tries - 1:
                print(f"warning: agent start -> {e.code}: {e.message}")
            else:
                time.sleep(1.5)
    return False


def prompt_or_start(
    root: Path, wt: Path, role: str, slug: str, text: str, timeout_ms: int, agent: AgentConfig
) -> tuple[bool, str]:
    """Prompt the role agent, starting it in its own tab of the worktree's workspace if it does not exist yet.
    Returns (settled, note)."""
    name = agent_name(role, slug)
    for attempt in (1, 2):
        try:
            herdr("agent", "prompt", name, text, "--wait", "--timeout", timeout_ms)
            return True, ""
        except HerdrError as e:
            if e.code == "agent_not_found" and attempt == 1:
                ws_id = worktree_workspace(root, wt)
                tab = herdr("tab", "create", "--workspace", ws_id, "--cwd", wt, "--label", role, "--no-focus")
                pane_id = find_key(tab.get("root_pane", tab), "pane_id") or die(
                    f"tab create returned no pane_id: {tab}"
                )
                if not start_agent(name, agent, pane_id, Git(root).main_checkout()):
                    raise
                if not settle_startup(name, agent.kind):
                    return True, "blocked at startup"
                continue
            if e.code == "timeout":
                return False, ""
            if e.code in ("agent_blocked", "agent_prompt_stalled"):
                return True, e.code
            raise
    return False, "unreachable"


# ----------------------------------------------------------------------------- check
def scope_errors(wt: Path, slug: str, st: str, vd: str, base: str) -> list[str]:
    """Rules 7-11 of docs/sdd.md §7: what the branch, the spec phase and the reviewer may touch."""
    mine = f"harness/features/{slug}/"
    errs = []
    for p in Git(wt).changed_paths(base):
        if p.startswith("harness/") and not p.startswith(mine):
            errs.append(f"{slug}: touches harness/ outside its folder: {p}")
        elif st in ("pending", "spec_ready", "approved") and not p.startswith(mine):
            errs.append(f"{slug}: changes files outside its folder before the spec is approved: {p}")
    ref = REVIEW_REF.format(slug=slug)
    if vd and Git(wt).ref_exists(ref):
        errs += reviewer_errors(wt, slug, base, ref)
    if st == "review" and vd == "APPROVED" and not Git(wt).ref_exists(ref):  # rule 10
        errs.append(
            f"{slug}: review.md says APPROVED but the launcher never started a reviewer round; run the reviewer"
        )
    if st in ("approved", "in_progress", "review"):  # rule 11: the approved requirements are the contract
        approved = approve_commits(wt, slug, base)
        if approved:
            then = requirement_lines(Git(wt).show(approved[0], f"{mine}spec.md"))
            if then != requirement_lines((wt / mine / "spec.md").read_text()):
                errs.append(
                    f"{slug}: ## Requirements differs from the approved spec; "
                    "never change a requirement after approval (ask for a new spec round instead)"
                )
    return errs


def reviewer_errors(wt: Path, slug: str, base: str, ref: str) -> list[str]:
    """Rule 9: since `ref`, the reviewer's commits (not from the base branch, no merges) and the working tree changed
    nothing but review.md and the status line of spec.md. A path touched and put back is fine."""
    mine = f"harness/features/{slug}/"
    errs = []
    touched = Git(wt).paths("log", "--format=", "--name-only", "--no-merges", f"{ref}..HEAD", f"^{base}")
    status = {p: xy for xy, p in Git(wt).porcelain()}
    for p in sorted(touched | status.keys()):
        diff = Git(wt).run("diff", ref, "--", p, check=False).splitlines()  # working tree vs the reviewer's start
        if not diff and status.get(p) != "??":
            continue
        changed = [line for line in diff if line[:1] in "+-" and not line.startswith(("+++", "---"))]
        if p == mine + "spec.md":
            if any(line[1:].split(":")[0].strip() != "status" for line in changed):
                errs.append(f"{slug}: the reviewer changed more than the status line in spec.md")
        elif p != mine + "review.md":
            errs.append(f"{slug}: the reviewer changed {p} (only review.md and the status line of spec.md are allowed)")
    return errs


def spec_errors(slug: str, text: str, st: str) -> tuple[list[str], dict, dict]:
    """Rule 2. Returns (errors, all requirements, active requirements)."""
    errs, reqs = [], {}
    for line in section(text, "Requirements"):
        m = REQ_RE.match(line.strip())
        if m:
            if m[1] in reqs:
                errs.append(f"{slug}: duplicate requirement id {m[1]}")
            reqs[m[1]] = m[2].strip()
    active = {k: v for k, v in reqs.items() if v.rstrip(".").strip().lower() != "withdrawn"}
    if not active:
        errs.append(f"{slug}: no active requirement line '- R<n>: ... SHALL ...' under ## Requirements")
    for k, v in active.items():
        shalls = len(re.findall(r"\bSHALL\b", v))
        if shalls == 0:
            errs.append(f"{slug}: {k} has no SHALL")
        elif shalls > 1:
            errs.append(f"{slug}: {k} has {shalls} SHALLs; one per requirement")
        elif not EARS_RE.match(v):
            errs.append(f"{slug}: {k} follows no EARS pattern ({EARS_HINT})")
    errs += placeholder_errors(slug, active.items())
    if st != "blocked" and any(line.strip() for line in section(text, "Open questions")):
        errs.append(f"{slug}: ## Open questions is not empty (answer them or set status blocked)")
    return errs, reqs, active


def placeholder_errors(slug: str, items) -> list[str]:
    """Rules 2-3: (id, text) pairs that still hold the template's first placeholder."""
    errs = []
    for item_id, text in items:
        found = [t for t in TEMPLATE_PLACEHOLDERS if t in text]
        if found:
            token = min(found, key=text.find)
            errs.append(f"{slug}: {item_id} still has the template placeholder {token}")
    return errs


def task_errors(slug: str, tasks_path: Path, reqs: dict, active: dict) -> tuple[list[str], list[tuple[str, str, str]]]:
    """Rule 3. Returns (errors, tasks as (box, id, text))."""
    if not tasks_path.exists():
        return [f"{slug}: missing tasks.md"], []
    errs, tasks, covered = [], [], set()
    for line in tasks_path.read_text().splitlines():
        m = TASK_RE.match(line.strip())
        if not m:
            continue
        tasks.append((m[1], m[2], m[3]))
        cited = set(re.findall(r"\bR\d+\b", m[3]))
        if not cited:
            errs.append(f"{slug}: {m[2]} cites no requirement")
        errs += [f"{slug}: {m[2]} cites unknown {c}" for c in sorted(cited - reqs.keys())]
        covered |= cited
    if not tasks:
        errs.append(f"{slug}: no task line '- [ ] T<n>: ... (R<n>)' in tasks.md")
    errs += placeholder_errors(slug, [(tid, text) for _, tid, text in tasks])
    errs += [
        f"{slug}: {r} is not covered by any task" for r in sorted(active.keys() - covered, key=lambda x: int(x[1:]))
    ]
    return errs, tasks


def token_pattern(slug: str, r: str) -> str:
    """git grep -E pattern for the token `<slug>:<R>` as a whole: not inside `x<slug>:`, `a-<slug>:`, `<R>0` or `<R>x`.
    Slugs are [a-z0-9-] and ids R<digits>, so neither needs escaping."""
    return f"(^|[^A-Za-z0-9_-]){slug}:{r}([^A-Za-z0-9_]|$)"


def done_errors(root: Path, slug: str, active: dict, tasks: list) -> list[str]:
    """Rules 4-5: every active R has its token in a test, every task is ticked, log.md exists."""
    errs = []
    for r in sorted(active, key=lambda x: int(x[1:])):
        pattern = token_pattern(slug, r)
        p = Git(root).proc("grep", "--untracked", "-q", "-E", pattern, "--", ".", ":!harness", check=False)
        if p.returncode != 0:
            errs.append(f"{slug}: no test carries the token {slug}:{r}")
    errs += [f"{slug}: {tid} is not ticked" for box, tid, _ in tasks if box == " "]
    if not (feature_dir(root, slug) / "log.md").exists():
        errs.append(f"{slug}: missing log.md")
    return errs


def check_feature(root: Path, slug: str, base: str) -> list[str]:
    """Rules 1-5 and 7-11 of docs/sdd.md §7 for one feature in this checkout. Returns violations."""
    fdir = feature_dir(root, slug)
    spec_path, tasks_path = fdir / "spec.md", fdir / "tasks.md"
    if not spec_path.exists():
        return [f"{slug}: missing spec.md"]
    st = fm_read(spec_path).get("status", "")
    if st not in STATUSES:  # rule 1
        return [f"{slug}: spec.md status '{st or '(none)'}' is not one of {', '.join(STATUSES)}"]
    errs = []
    if st != "done" and not Git(root).is_main():  # rules 7-11 apply to a feature branch
        errs += scope_errors(root, slug, st, fm_read(fdir / "review.md").get("verdict", ""), base)
    if RANK[st] < 1:
        return errs
    text = re.sub(r"<!--.*?-->", "", spec_path.read_text(), flags=re.S)
    spec_errs, reqs, active = spec_errors(slug, text, st)
    task_errs, tasks = task_errors(slug, tasks_path, reqs, active)
    errs += spec_errs + task_errs
    if RANK[st] >= 4:
        errs += done_errors(root, slug, active, tasks)
    return errs


def run_tests(root: Path, cfg: Config) -> list[str]:
    code, out = shell(cfg.test_cmd, root)
    return [] if code == 0 else [f"tests failed (exit {code}: {cfg.test_cmd})\n{out}"]


def run_check(root: Path, slugs: list[str], tests: bool | None) -> list[str]:
    """tests: None = only if any feature is at review or later; True/False = force."""
    cfg = Config.load(root)
    errs, need_tests = [], bool(tests)
    for s in slugs:
        errs += check_feature(root, s, cfg.base_branch)
        if tests is None and RANK.get(fm_read(feature_dir(root, s) / "spec.md").get("status", ""), 0) >= 4:
            need_tests = True
    if need_tests:
        errs += run_tests(root, cfg)
    return errs


# ----------------------------------------------------------------------------- running a role
def preflight(cfg: Config, wt: Path, role: str, slug: str, st: str, vd: str, tests_always: bool = False) -> str | None:
    """What stops `role` from starting now, or None. Prepares the branch: before a review the base branch is merged in
    and the result checked; the review ref marks where the reviewer starts; a new implementer round drops it."""
    base = cfg.base_branch
    if st not in RUN_GATE[role]:
        allowed = ", ".join(sorted(RUN_GATE[role]))
        nxt = next_action(st, vd, cfg.gates)
        return f"cannot run {role} while status is '{st or '(none)'}' (allowed: {allowed}); next: {nxt}"
    if Git(wt).dirty():
        return f"worktree {wt} has uncommitted changes; a role agent must not start on top of them"
    name = agent_name(role, slug)
    if agent_state(name) == "working":
        return f"{name} is still working; wait for it: harness.py wait {role} {slug}"
    ref = REVIEW_REF.format(slug=slug)
    if role == "reviewer":
        ok, moved = Git(wt).merge_in(base)
        if not ok:
            fix = f'harness.py run implementer {slug} "{SYNC_MSG.format(base=base)}"'
            return f"merging '{base}' into '{slug}' conflicts; run: {fix}"
        if moved or tests_always:
            errs = run_check(wt, [slug], True)
            if errs:
                why = f" after merging '{base}'" if moved else ""
                return (
                    f"check --tests fails on '{slug}'{why}:\n"
                    + "\n".join(errs)
                    + f'\nrun: harness.py run implementer {slug} "check --tests fails; fix it, commit"'
                )
    if role == "reviewer" or (role == "implementer" and vd == "APPROVED"):
        drop_review(wt, slug)
    if role == "reviewer":
        Git(wt).set_ref(ref)
    elif role == "implementer":
        Git(wt).drop_ref(ref)
    return None


def role_prompt(role: str, slug: str, msg: str) -> str:
    text = f"Read harness/roles/{role}.md and follow it for harness/features/{slug}/."
    return text + (" " + " ".join(msg.split()) if msg else "")


def evaluate(wt: Path, role: str, slug: str, settled: bool, note: str, cfg: Config) -> tuple[str, str, list[str], str]:
    """(result, note, check errors, agent state) of a role run. DONE needs the on-disk evidence AND a green structural
    check; a reviewer whose CHANGES_REQUESTED is the max_review_rounds-th blocks the feature for the human."""
    name = agent_name(role, slug)
    st, vd = feature_state(feature_dir(wt, slug))
    state = agent_state(name)
    errs: list[str] = []
    if not settled:
        return "STILL_WORKING", note, errs, state
    if state == "blocked" or note:
        return "NEEDS_ATTENTION", note, errs, state
    if not DONE_WHEN[role](st, vd):
        return "NEEDS_ATTENTION", f"status {st or '?'} / verdict {vd or '-'} is not what {role} must leave", errs, state
    if role == "implementer" and vd == "APPROVED" and not Git(wt).ref_exists(REVIEW_REF.format(slug=slug)):
        drop_review(wt, slug)  # a verdict only the reviewer may write; the reviewer runs next
        note, vd = "discarded a verdict the implementer wrote", ""
    left = [p for _, p in Git(wt).porcelain()]
    if left:  # docs/sdd.md §6: a role that stops with uncommitted changes has not finished
        more = " ..." if len(left) > 8 else ""
        errs.append(f"{slug}: uncommitted changes left behind: {', '.join(left[:8])}{more}; commit them")
    errs += check_feature(wt, slug, cfg.base_branch)
    if errs:
        return "NEEDS_ATTENTION", "check fails", errs, state
    cap = cfg.max_review_rounds
    if role == "reviewer" and vd == "CHANGES_REQUESTED" and cap and review_rounds(wt, slug, cfg.base_branch) >= cap:
        block_feature(
            wt,
            slug,
            f"{cap} review rounds ended in CHANGES_REQUESTED (max_review_rounds); the last findings are in review.md",
        )
        Git(wt).drop_ref(REVIEW_REF.format(slug=slug))
        return "NEEDS_ATTENTION", f"blocked after {cap} review rounds", errs, state
    return "DONE", note, errs, state


def report(wt: Path, role: str, slug: str, settled: bool, note: str, cfg: Config) -> int:
    name = agent_name(role, slug)
    result, note, errs, state = evaluate(wt, role, slug, settled, note, cfg)
    st, vd = feature_state(feature_dir(wt, slug))
    print(f"{name}: state={state} status={st or '?'} verdict={vd or '-'} -> {result}" + (f" [{note}]" if note else ""))
    if result == "STILL_WORKING":
        print(f"next: harness.py wait {role} {slug}")
        return EXIT_WORKING
    if errs:
        print("\n".join(errs))
    print("--- last output ---")
    print(agent_tail(name).rstrip())
    print("---")
    sidebar(wt, state_text(st, vd, cfg.gates))
    if result == "DONE":
        print(f"next: {next_action(st, vd, cfg.gates)}")
        return EXIT_DONE
    if errs:
        print(f'send it back: harness.py run {role} {slug} "check fails: <the lines above>; fix, commit"')
    print(f"inspect: herdr agent read {name} --source recent-unwrapped --lines 120")
    print(f"answer a dialog: herdr agent send-keys {name} <key>   (or ask the human)")
    return EXIT_ATTENTION


def resume(root: Path, wt: Path, slug: str, text: str) -> int:
    """After a human gate: wake the detached orchestrator, else relaunch the driver in its tab, else say what to run."""
    if shutil.which("herdr") is None:
        print(f"next (inside herdr): harness.py drive {slug} --tab   (or: harness.py run implementer {slug})")
        return 0
    try:
        herdr("agent", "prompt", slug, text)
        print(f"detached orchestrator '{slug}' notified")
        return 0
    except HerdrError as e:
        if e.code != "agent_not_found":
            print(f"warning: could not notify detached orchestrator '{slug}' ({e.code}: {e.message})")
    try:
        ws_id = workspace_of(wt)
        if ws_id and driver_tab_pane(ws_id) and not driver_running(wt):
            pane = launch_driver(root, wt, slug, "", os.environ.get("HERDR_PANE_ID", ""))
            print(f"driver resumed in pane {pane}")
            return 0
    except HerdrError as e:
        print(f"warning: could not resume the driver ({e.code}: {e.message})")
    print(f"next: harness.py drive {slug} --tab   (or: harness.py run implementer {slug})")
    return 0


# ----------------------------------------------------------------------------- driver
class _Stop(Exception):  # noqa: N818 - a control-flow signal (the driver's exit code), not an error
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


class Driver:
    """The orchestrator's loop as a process: run the next role, send failed checks and conflicts back, approve and merge
    when the gates are auto, stop (and notify) when the feature needs a human. The first message goes to the first role
    run; at a gate or when blocked it goes to the role that produced what is being judged."""

    def __init__(self, root: Path, cfg: Config, f: Feature, msg: str, notify_pane: str):
        self.root, self.cfg, self.f, self.msg, self.notify_pane = root, cfg, f, msg, notify_pane
        self.slug, self.wt, self.base, self.gates = f.slug, f.wt, cfg.base_branch, cfg.gates
        self.fdir = f.dir
        self.lock = driver_lock(self.wt)
        self.fails = 0  # checks sent back in a row

    def run(self) -> int:
        holder = acquire_driver_lock(self.wt)
        if holder:
            who = f"a driver (pid {holder})" if holder > 0 else "another driver"
            print(f"error: {who} is already running {self.slug}", file=sys.stderr)
            return EXIT_ERROR
        driver_stamp(self.wt).write_text(f"{os.getpid()} {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        self.log(
            f"driving {self.slug} in {self.wt}" + (f"; will notify pane {self.notify_pane}" if self.notify_pane else "")
        )
        try:
            while True:
                self.step()
        except _Stop as s:
            return s.code
        finally:
            with contextlib.suppress(FileNotFoundError):
                self.lock.unlink()

    @staticmethod
    def log(text: str):
        print(f"[{datetime.now():%H:%M}]", text)

    def stop(self, text: str, code: int = EXIT_DONE):
        st, vd = feature_state(self.fdir)
        sidebar(self.wt, state_text(st, vd, self.gates))
        line = f"harness: {self.slug} {text}"
        self.log(line)
        if self.notify_pane:
            notify(self.notify_pane, line + " (board: harness.py status)")
        raise _Stop(code)

    def step(self):
        st, vd = feature_state(self.fdir)
        sidebar(self.wt, state_text(st, vd, self.gates))
        text, self.msg = self.msg, ""
        role, text = self.choose(next_action(st, vd, self.gates), st, text)
        if role is None:
            return
        if role == "reviewer":
            role, text = self.before_review(text)
        name = agent_name(role, self.slug)
        if agent_state(name) == "working":  # left working by an earlier driver or a human: let it finish first
            self.log(f"{name} is still working; waiting")
            self.wait_role(role, False, "", time.time())
            self.msg = text
            return
        st, vd = feature_state(self.fdir)
        err = preflight(self.cfg, self.wt, role, self.slug, st, vd)
        if err:
            self.stop(f"cannot run {role}: {err}", EXIT_ERROR)
        self.run_role(role, text, st, vd)

    def auto_approve(self) -> tuple[str | None, str]:
        """Approve a green spec; a red one goes back to the spec author with the errors."""
        errs = check_feature(self.wt, self.slug, self.base)
        if errs:
            return "spec_author", "check fails:\n" + "\n".join(errs) + "\nFix the spec, commit."
        approve_spec(self.wt, self.slug, self.f.branch)
        return None, ""

    def require_clean(self):
        """Stop for the human when the worktree has uncommitted changes (a role that could not commit)."""
        left = [p for _, p in Git(self.wt).porcelain()]
        if left:
            more = " ..." if len(left) > 8 else ""
            self.stop(
                f"needs you: uncommitted changes left behind: {', '.join(left[:8])}{more}; commit them "
                "(a role that cannot commit in a worktree? see the sandbox note in harness/help/config.md)",
                EXIT_ATTENTION,
            )

    def choose(self, nxt: str, st: str, text: str) -> tuple[str | None, str]:
        """The role to run now and its message; None when a gate was passed without running anyone.
        Stops at human gates."""
        if nxt.startswith("run "):
            return nxt[4:], text
        if nxt == "approve (auto)":
            return self.auto_approve()
        if nxt == "approve (human)":
            if not text:
                self.stop(
                    f"spec ready, needs you: read {self.fdir / 'spec.md'}, then `harness.py approve {self.slug}`, "
                    f'or `harness.py drive {self.slug} "<feedback for the spec author>" --tab`'
                )
            return "spec_author", text
        if nxt.startswith("merge"):
            self.require_clean()  # no role runs at this gate, so the clean-worktree check happens here
        if nxt == "merge (auto)":
            self.spawn_merge()
        if nxt == "merge (human)":
            if not text:
                self.stop(f"review approved, needs you: `harness.py merge {self.slug}` from the main checkout")
            return "implementer", text
        if st == "blocked":
            if not text:
                self.stop(
                    f"blocked, needs you: read `## Open questions` in {self.fdir / 'spec.md'} "
                    f'and {self.fdir / "log.md"}; answer with `harness.py drive {self.slug} "<answer>" --tab`'
                )
            return ("implementer" if approved_once(self.wt, self.slug, self.base) else "spec_author"), text
        if st == "done":
            self.stop("is done")
        self.stop(f"has a status the lifecycle does not know ('{st}'); fix spec.md", EXIT_ERROR)

    def before_review(self, text: str) -> tuple[str, str]:
        """The reviewer sees the branch as it will land, and only when it is green; otherwise the implementer
        goes first."""
        ok, _ = Git(self.wt).merge_in(self.base)
        if not ok:
            return "implementer", SYNC_MSG.format(base=self.base)
        errs = run_check(self.wt, [self.slug], True)
        if not errs:
            return "reviewer", text
        self.fails += 1
        if self.fails > FIX_TRIES:
            block_feature(
                self.wt, self.slug, f"check --tests still fails after {FIX_TRIES} fixes; last output in the driver tab"
            )
            self.stop(f"blocked, needs you: check keeps failing after {FIX_TRIES} fixes", EXIT_ATTENTION)
        return "implementer", "check --tests fails on the branch:\n" + "\n".join(errs)[-1500:] + "\nFix it, commit."

    def wait_role(self, role: str, settled: bool, note: str, started: float) -> str:
        """Wait until the role agent settles; returns its note. Past max_role_minutes since `started`, stop for the
        human instead (the agent is left running: whether to stop it is theirs to decide)."""
        name, limit = agent_name(role, self.slug), self.cfg.max_role_minutes
        while not settled:
            minutes = int((time.time() - started) // 60)
            if limit and minutes >= limit:
                self.stop(
                    f"needs you: {role} still working after {minutes} min (max_role_minutes {limit}); "
                    f"inspect: herdr agent read {name} --source recent-unwrapped --lines 120",
                    EXIT_ATTENTION,
                )
            settled, note = wait_agent(name, DRIVE_WAIT_MS)
        return note

    def run_role(self, role: str, text: str, st: str, vd: str):
        name = agent_name(role, self.slug)
        self.log(f"run {role}" + (f": {' '.join(text.split())[:120]}" if text else ""))
        sidebar(self.wt, state_text(st, vd, self.gates, running=role))
        prompt = role_prompt(role, self.slug, text)
        started = time.time()
        settled, note = prompt_or_start(
            self.root, self.wt, role, self.slug, prompt, DRIVE_WAIT_MS, self.cfg.agent(role)
        )
        note = self.wait_role(role, settled, note, started)
        result, note, errs, state = evaluate(self.wt, role, self.slug, True, note, self.cfg)
        self.log(f"{name}: state={state} -> {result}" + (f" [{note}]" if note else ""))
        if errs:
            print("\n".join(errs))
        if result == "DONE":
            self.fails = 0
            return
        if feature_state(self.fdir)[0] == "blocked":  # the launcher blocked it (review cap): the human decides
            self.stop(f"blocked, needs you: {note}; read {self.fdir / 'log.md'}", EXIT_ATTENTION)
        if errs and self.fails < FIX_TRIES:  # a failed check goes back; the loop picks the role from the status
            self.fails += 1
            self.msg = "check fails:\n" + "\n".join(errs) + "\nFix it, commit."
            return
        if errs:
            block_feature(self.wt, self.slug, f"check still fails after {FIX_TRIES} fixes: " + "; ".join(errs)[:500])
            self.stop(f"blocked, needs you: check keeps failing after {FIX_TRIES} fixes", EXIT_ATTENTION)
        self.stop(
            f"needs you: {role} stopped ({note or 'see its screen'}); "
            f"inspect: herdr agent read {name} --source recent-unwrapped --lines 120",
            EXIT_ATTENTION,
        )

    def spawn_merge(self):
        """`merge` removes this worktree, its workspace and this very pane: run it detached from the main checkout."""
        main = Git(self.wt).main_checkout()
        log = main / ".git" / f"harness-merge-{self.slug}.log"
        self.lock.unlink()  # `merge` refuses while a driver runs; this one is done
        cmd = [sys.executable, str(Path(__file__).resolve()), "merge", self.slug]
        cmd += ["--notify", self.notify_pane] if self.notify_pane else []  # `merge` reports its own outcome
        with log.open("w") as fh:
            subprocess.Popen(
                cmd, cwd=main, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True
            )
        self.log(f"merging {self.slug} in the background (log: {log}); this workspace closes when it is done")
        raise _Stop(EXIT_DONE)


# ----------------------------------------------------------------------------- doctor
def probe_model(cli: Cli, model: str, cwd: Path, timeout_s: int = 120) -> tuple[bool, str]:
    """(answered, output tail) of one tiny prompt to the CLI with this model."""
    try:
        p = subprocess.run(
            cli.probe_cmd(model, "Reply with the word OK."),
            cwd=cwd, text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout_s,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        return False, f"no answer within {timeout_s} s"
    except OSError as e:
        return False, str(e)
    return p.returncode == 0, (p.stdout + p.stderr).strip()[-1500:]


class Doctor:
    """Tools, config and leftovers of the repo at `root` (its main checkout). `run()` prints the report."""

    def __init__(self, root: Path, tests: bool = True, models: bool = False):
        self.root, self.tests, self.models = root, tests, models
        self.problems: list[str] = []
        self.notes: list[str] = []

    def check(self, cond: bool, msg: str, fix: str = ""):
        print(("ok    " if cond else "FAIL  ") + msg + ("" if cond or not fix else f"\n      fix: {fix}"))
        if not cond:
            self.problems.append(msg)

    def run(self) -> int:
        self.check(shutil.which("git") is not None, "git in PATH")
        self.check(shutil.which("herdr") is not None, "herdr in PATH", "https://herdr.dev/docs/install/")
        if shutil.which("uv") is None:
            self.notes.append("uv is not in PATH; run the launcher as `python3 harness/harness.py ...`")
        cfg = Config.load(self.root)
        self.check(True, "harness/config.json is valid")
        self.config(cfg)
        self.baseline(cfg)
        if self.models:
            self.models_check(cfg)
        self.git_leftovers(cfg.base_branch)
        if shutil.which("herdr") is not None:
            self.herdr_state()
        for n in self.notes:
            print("note  " + n)
        print(f"\n{len(self.problems)} problem(s)" if self.problems else "\nall good")
        return 1 if self.problems else 0

    def config(self, cfg: Config):
        self.check(
            Git(self.root).ref_exists(f"refs/heads/{cfg.base_branch}"), f"base branch '{cfg.base_branch}' exists"
        )
        for role in ("orchestrator", *ROLES):
            kind = cfg.agent(role).kind
            self.check(
                shutil.which(kind) is not None,
                f"{role}: '{kind}' CLI in PATH",
                f"install it and log in, or change agents.{role}.kind",
            )
        pats, bad = WorktreeSetup(self.root, self.root, cfg).patterns()
        for pat in pats:
            if list(self.root.glob(pat)):
                self.check(True, f"copy_files: {pat} is in the main checkout")
            else:
                self.notes.append(f"copy_files: {pat} is not in the main checkout, so new worktrees will not get it")
        for line in bad:
            self.check(False, f".worktreeinclude line rejected: {line}", "relative paths only, no '..'")
        report = ClaudeSettings(self.root, cfg.agent("orchestrator")).report()
        if ClaudeSettings.APPLY_CMD in report:
            self.notes.append(report)

    def baseline(self, cfg: Config):
        """test_cmd on the main checkout: a red baseline would be blamed on the first feature's implementer."""
        if not self.tests:
            self.notes.append("test_cmd not run (--no-tests)")
            return
        code, out = shell(cfg.test_cmd, self.root)
        fix = (
            "a red baseline gets blamed on the first feature's implementer: set up the project "
            "(e.g. pyproject.toml and tests/ for the shipped uv/pytest commands) or change test_cmd"
        )
        self.check(code == 0, f"test_cmd green on the main checkout ({cfg.test_cmd})", fix)
        if code != 0:
            print("\n".join("      | " + line for line in out.splitlines()[-15:]))

    def models_check(self, cfg: Config):
        """`--models`: one tiny prompt per (CLI, model) pair, so a wrong model id fails here, not mid-feature."""
        seen: dict[tuple, bool] = {}
        for role in (*ROLES, "orchestrator"):
            agent = cfg.agent(role)
            kind, model, cli = agent.kind, agent.model, agent.cli
            if not (cli and cli.probe) or not model:
                self.notes.append(f"{role}: {kind} model {'not checked' if model else 'is the CLI default'}")
                continue
            if (kind, model) not in seen:
                seen[(kind, model)], out = probe_model(cli, model, self.root)
                if not seen[(kind, model)]:
                    print("\n".join("      | " + line for line in out.splitlines()[-8:]))
            self.check(seen[(kind, model)], f"{role}: {kind} answers with model {model}", f"check agents.{role}.model")

    def git_leftovers(self, base: str):
        root = self.root
        feats = features_everywhere(root)
        wts = Git(root).worktrees()
        live = {branch for _, branch in wts}
        for wt, branch in wts[1:]:
            f = feats.get(branch)
            if not wt.exists():
                self.check(False, f"worktree {wt} is missing on disk", "git worktree prune")
            elif not f or f.wt != wt:
                self.check(
                    False, f"worktree {wt} ({branch or 'detached'}) has no feature folder", f"git worktree remove {wt}"
                )
            elif f.status == "done":
                self.check(
                    False,
                    f"worktree {wt}: feature {branch} is done",
                    f"git worktree remove {wt} && git branch -d {branch}",
                )
            else:
                self.check(True, f"worktree {wt}: {branch} ({f.status})")
        for b in Git(root).run("for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines():
            spec = root / "harness" / "features" / b / "spec.md"
            if b != base and b not in live and spec.exists():
                st = fm_read(spec).get("status", "?")
                fix = f"git branch -D {b}" if st == "done" else f"herdr worktree open --cwd {root} --branch {b}"
                self.check(False, f"branch '{b}' has no worktree (its feature is {st} on {base})", fix)
        for ref in Git(root).run("for-each-ref", "--format=%(refname)", "refs/harness", check=False).splitlines():
            slug = ref.split("/")[2] if ref.count("/") >= 3 else ""
            if slug not in live:
                self.check(False, f"stale {ref}", f"git update-ref -d {ref}")

    def herdr_state(self):
        root = self.root
        try:
            wss = herdr("workspace", "list").get("workspaces", [])
        except HerdrError as e:
            self.check(False, f"herdr server reachable ({e.code}: {e.message})", "open herdr")
            return
        self.check(True, "herdr server reachable")
        on_main = []
        for w in wss:
            info = w.get("worktree") or {}
            checkout = Path(info["checkout_path"]) if info.get("checkout_path") else None
            if checkout and checkout.resolve() == root.resolve():
                on_main.append(w["workspace_id"])
            linked_here = (
                info.get("is_linked_worktree")
                and info.get("repo_root")
                and Path(info["repo_root"]).resolve() == root.resolve()
            )
            if linked_here and checkout and not checkout.exists():
                self.check(
                    False,
                    f"workspace {w['workspace_id']} '{w.get('label')}' points at a removed worktree",
                    f"herdr workspace close {w['workspace_id']}",
                )
        found = f" (found {', '.join(on_main)})" if len(on_main) > 1 else ""
        self.check(
            len(on_main) <= 1,
            "one herdr workspace on the main checkout" + found,
            "close the extra ones once their worktree workspaces are merged: herdr workspace close <id>",
        )
        cfgp = Path.home() / ".config" / "herdr" / "config.toml"
        text = cfgp.read_text() if cfgp.exists() else ""
        if "$harness" not in text:
            self.notes.append(
                f"herdr's sidebar does not show feature states yet. Add to {cfgp}:\n{SIDEBAR_ROWS}\n"
                "then: herdr server reload-config"
            )
        self.notes.append("the launcher's own tests: python3 harness/selftest.py")


# ----------------------------------------------------------------------------- commands
def cmd_config(a):
    """The setup the next feature would run with, for the orchestrator to show the human before `start`."""
    root = Git.here().main_checkout()
    cfg = Config.load(root)
    print("harness/config.json")
    print(f"  base_branch        {cfg.base_branch}")
    print(f"  test_cmd           {cfg.test_cmd}")
    print(f"  setup_cmd          {cfg.setup_cmd or '-'}")
    print(f"  teardown_cmd       {cfg.teardown_cmd or '-'}")
    print(f"  copy_files         {', '.join(cfg.copy_files) or '-'}")
    print(f"  max_review_rounds  {cfg.max_review_rounds or 'no limit'}")
    print(f"  max_role_minutes   {cfg.max_role_minutes or 'no limit'}")
    print(f"  gates              approve: {cfg.gates['approve']}   merge: {cfg.gates['merge']}")
    for role in (*ROLES, "orchestrator"):
        agent = cfg.agent(role)
        kind, model = agent.kind, agent.model or "(default model)"
        missing = "   <- not in PATH" if not shutil.which(kind) else ""
        note = "   (only used by start --detach)" if role == "orchestrator" else ""
        print(f"  {role:<18} {kind:<7} {model}{missing}{note}")
        for line in agent.describe():
            print(f"  {'':<18} {line}")
    print("  effort: " + " < ".join(EFFORT_LEVELS) + "   permissions: " + ", ".join(PERMISSION_LEVELS))
    settings = ClaudeSettings(root, cfg.agent("orchestrator"))
    print(settings.apply() if a.apply else settings.report())
    print("edit harness/config.json (keys: harness/help/config.md); the launcher validates it on every command")
    return 0


def role_working(role: str, slug: str) -> bool:
    return shutil.which("herdr") is not None and agent_state(agent_name(role, slug)) == "working"


def cmd_status(a):
    root = Git.here().path
    gates = Config.load(root).gates
    feats = features_everywhere(root)
    if not feats:
        print('no features yet. start one: harness.py start <slug> "intent"')
        return 0
    home = str(Path.home())
    groups: dict[str, list[tuple]] = {"needs you": [], "to run": [], "working": [], "done": []}
    for slug, f in sorted(feats.items()):
        nxt = next_action(f.status, f.verdict, gates)
        where = f"{f.branch or '(detached)'}@{str(f.wt).replace(home, '~', 1)}"
        if f.status == "done":
            group = "done"
        elif needs_human(nxt):
            group = "needs you"
        elif f.live and driver_running(f.wt):
            group, nxt = "working", nxt + "  (driver running)"
        elif f.live and nxt.startswith("run ") and role_working(nxt[4:], slug):
            group, nxt = "working", nxt + "  (agent working)"
        else:
            group = "to run"  # nothing runs on its own: the orchestrator starts the driver or the role
        groups[group].append((slug, f.status or "?", f.verdict or "-", where, nxt))
    print(" · ".join(f"{len(v)} {k}" for k, v in groups.items() if v))
    header = ("SLUG", "STATUS", "VERDICT", "BRANCH@PATH", "NEXT")
    widths = [max(len(r[i]) for g in groups.values() for r in [header, *g]) for i in range(5)]
    print("  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(header)).rstrip())
    for group, rows in groups.items():
        if rows:
            print(group)
            for r in rows:
                print("  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip())
    return 0


def cmd_check(a):
    root = Git.here().path
    if a.slug:
        slugs = [a.slug]
        if not (feature_dir(root, a.slug) / "spec.md").exists():
            root = locate(root, a.slug).wt  # the feature lives in another worktree: check it there
            print(f"checking {a.slug} in {root}")
    else:
        slugs = sorted(
            p.name for p in (root / "harness" / "features").glob("*") if p.is_dir() and not p.name.startswith("_")
        )
    tests = True if a.tests else (False if a.no_tests else None)
    errs = run_check(root, slugs, tests)
    if errs:
        print("\n".join(errs))
        return 1
    print(f"OK ({', '.join(slugs) or 'no features'})")
    return 0


def start_preconditions(root: Path, cfg: Config, slug: str):
    if not SLUG_RE.match(slug):
        die("slug must match ^[a-z][a-z0-9-]{1,23}$ (lowercase, digits, dashes)")
    if not Git(root).is_main():
        die("run start from the main checkout, not from a worktree")
    if Git(root).branch() != cfg.base_branch:
        die(f"run start from the base branch '{cfg.base_branch}' (currently on '{Git(root).branch() or 'detached'}')")
    if Git(root).ref_exists(f"refs/heads/{slug}"):
        die(f"branch '{slug}' already exists")
    if slug in features_everywhere(root):
        die(f"feature '{slug}' already exists (see: harness.py status)")
    if not (root / "harness" / "features" / "_template" / "spec.md").exists():
        die("harness/features/_template/spec.md is missing")
    if Git(root).dirty():
        print("warning: the base checkout has uncommitted changes; the worktree starts from the last commit")


def create_worktree(root: Path, slug: str, base: str, trust: bool) -> dict:
    """herdr's response for the new worktree: its path, workspace, tab and root pane."""
    extra = ["--trust-repository"] if trust else []
    try:
        return herdr(
            "worktree",
            "create",
            *repo_source(root),
            "--branch",
            slug,
            "--base",
            base,
            "--label",
            slug,
            "--no-focus",
            *extra,
        )
    except HerdrError as e:
        hint = "" if trust else "\n(if herdr asks for repository trust, verify the repo and retry with --trust)"
        die(f"herdr worktree create -> {e.code}: {e.message}{hint}")


def seed_feature(wt: Path, slug: str, intent: str):
    """The feature folder from the template, committed on the new branch."""
    fdir = feature_dir(wt, slug)
    shutil.copytree(wt / "harness" / "features" / "_template", fdir)
    title = slug.replace("-", " ").capitalize()
    intent = intent or "To be defined with the human: ask what this feature must do before writing requirements."
    for p in fdir.iterdir():
        p.write_text(p.read_text().replace("{{title}}", title).replace("{{slug}}", slug).replace("{{intent}}", intent))
    Git(wt).run("add", f"harness/features/{slug}")
    Git(wt).run("commit", "-q", "-m", f"{slug}: new feature")  # everything staged, as before


def start_detached(root: Path, cfg: Config, slug: str, res: dict):
    """A dedicated orchestrator agent in the worktree's root pane, prompted with its role."""
    agent = cfg.agent("orchestrator")
    pane_id = res["root_pane"]["pane_id"]
    tab_id = find_key(res.get("tab", {}), "tab_id")
    if tab_id:
        with contextlib.suppress(HerdrError):
            herdr("tab", "rename", tab_id, "orchestrator")
    text = f"Read harness/roles/orchestrator.md and follow it for harness/features/{slug}/."
    started = start_agent(slug, agent, pane_id, root, tries=3)
    if started and not settle_startup(slug, agent.kind):
        print(
            "warning: the orchestrator is blocked on a dialog; look at it: "
            f"herdr agent read {slug} --source visible --lines 40"
        )
        started = False
    if started:
        try:
            herdr("agent", "prompt", slug, text)
            print(f"orchestrator '{slug}' started and prompted")
        except HerdrError as e:
            print(f'warning: prompt -> {e.code}: {e.message}\nsend it yourself: herdr agent prompt {slug} "{text}"')
    else:
        print(f'start the orchestrator yourself in pane {pane_id}, then: herdr agent prompt {slug} "{text}"')
    print(f"detached orchestrator agent: {slug}   peek: herdr agent read {slug} --source recent-unwrapped --lines 60")


def cmd_start(a):
    root = Git.here().path
    cfg = Config.load(root)
    start_preconditions(root, cfg, a.slug)
    res = create_worktree(root, a.slug, cfg.base_branch, a.trust)
    wt = Path(res["worktree"]["path"])
    seed_feature(wt, a.slug, a.intent)
    WorktreeSetup(root, wt, cfg).run()
    sidebar(wt, state_text("pending", "", cfg.gates))
    ws_id = res["workspace"]["workspace_id"]
    print(f"worktree: {wt}\nworkspace: {ws_id}  branch: {a.slug}  feature: harness/features/{a.slug}/")
    if a.detach:
        start_detached(root, cfg, a.slug, res)
    else:
        print(f"next: harness.py drive {a.slug} --tab   (or one role at a time: harness.py run spec_author {a.slug})")
    return 0


def cmd_run(a):
    root = Git.here().path
    cfg = Config.load(root)
    f = live_worktree(root, a.slug)
    pid = driver_running(f.wt)
    if pid:
        die(
            f"a driver (pid {pid}) is running {a.slug} and starts the roles itself; "
            f"stop it first (kill {pid}) to run one by hand"
        )
    err = preflight(cfg, f.wt, a.role, a.slug, f.status, f.verdict)
    if err:
        die(err)
    sidebar(f.wt, state_text(f.status, f.verdict, cfg.gates, running=a.role))
    settled, note = prompt_or_start(
        root, f.wt, a.role, a.slug, role_prompt(a.role, a.slug, a.msg), a.timeout, cfg.agent(a.role)
    )
    return report(f.wt, a.role, a.slug, settled, note, cfg)


def cmd_wait(a):
    root = Git.here().path
    cfg = Config.load(root)
    wt = live_worktree(root, a.slug).wt
    name = agent_name(a.role, a.slug)
    settled, note = wait_agent(name, a.timeout)
    if note == "agent_not_found":
        die(f"no agent '{name}'; start it with: harness.py run {a.role} {a.slug}")
    return report(wt, a.role, a.slug, settled, note, cfg)


def cmd_approve(a):
    root = Git.here().path
    cfg = Config.load(root)
    f = locate(root, a.slug)
    if f.status != "spec_ready":
        die(f"'{a.slug}' is '{f.status or '(none)'}', expected spec_ready")
    errs = check_feature(f.wt, a.slug, cfg.base_branch)
    if errs:
        print("\n".join(errs))
        die("fix the spec before approving")
    approve_spec(f.wt, a.slug, f.branch)
    sidebar(f.wt, state_text("approved", "", cfg.gates))
    text = (
        f"Spec approved. Continue: run `harness.py drive {a.slug} --tab` (or `run implementer {a.slug}`) now; "
        "nothing runs on its own."
    )
    return resume(root, f.wt, a.slug, text)


def cmd_drive(a):
    root = Git.here().path
    cfg = Config.load(root)
    f = live_worktree(root, a.slug)
    pid = driver_running(f.wt)
    if pid:
        die(f"a driver (pid {pid}) is already running {a.slug}")
    if a.tab:
        pane = launch_driver(Git(root).main_checkout(), f.wt, a.slug, a.msg, os.environ.get("HERDR_PANE_ID", ""))
        print(
            f"driver for {a.slug}: pane {pane}, tab 'driver' of its workspace. It runs the roles one after another "
            "and stops when the feature needs a human; then it prompts this pane (if an agent runs here) "
            "and `status` says what to do."
        )
        print(f"watch: herdr pane read {pane} --lines 40")
        return 0
    return Driver(root, cfg, f, a.msg, a.notify).run()


def merge_preconditions(root: Path, cfg: Config, slug: str) -> Feature:
    base = cfg.base_branch
    if not Git(root).is_main():
        die("run merge from the main checkout, never from inside a worktree")
    if Git(root).branch() != base:
        die(f"run merge from the base branch '{base}' (currently on '{Git(root).branch() or 'detached'}')")
    if Git(root).dirty():
        die("the main checkout has uncommitted changes; commit or stash them first")
    f = locate(root, slug)
    if not f.live or f.wt == root:
        die(f"no live worktree on branch '{slug}' (already merged?)")
    if f.status != "review" or f.verdict != "APPROVED":
        die(f"'{slug}' is status={f.status or '?'} verdict={f.verdict or '-'}; need status review and verdict APPROVED")
    if Git(f.wt).dirty():
        die(f"worktree {f.wt} has uncommitted changes; ask the orchestrator to commit them")
    pid = driver_running(f.wt)
    if pid:
        die(f"a driver (pid {pid}) is still running {slug}; wait until status shows 'merge (human)'")
    return f


def land(root: Path, wt: Path, slug: str, base: str):
    """Base merged into the branch and checked, the branch merged into the base, the feature marked done."""
    ok, _ = Git(wt).merge_in(base)
    if not ok:
        die(
            f"merging '{base}' into '{slug}' conflicts. Run:\n"
            f'  harness.py run implementer {slug} "{SYNC_MSG.format(base=base)}"\n'
            f"  (or: harness.py drive {slug} --tab)\nthen run merge again"
        )
    errs = run_check(wt, [slug], True)
    if errs:
        print("\n".join(errs))
        die("check failed on the merged worktree; not merging")
    Git(root).run("merge", "--no-ff", "--no-edit", slug)
    spec = feature_dir(root, slug) / "spec.md"
    fm_set(spec, "status", "done")
    rel = spec.relative_to(root)
    Git(root).commit(f"{slug}: done", rel)


def remove_worktree(root: Path, wt: Path, slug: str, cfg: Config):
    """teardown_cmd, then the worktree (with its herdr workspace when there is one), the review ref and the branch."""
    if cfg.teardown_cmd:
        code, out = shell(cfg.teardown_cmd, wt)
        print("teardown_cmd:", "ok" if code == 0 else f"FAILED (exit {code})\n{out[-800:]}")
    Git(root).drop_ref(REVIEW_REF.format(slug=slug))
    removed = False
    try:
        ws_id = workspace_of(wt) if shutil.which("herdr") else None
        if ws_id:
            herdr("worktree", "remove", "--workspace", ws_id, "--force")
            removed = True
    except HerdrError as e:
        print(f"herdr worktree remove failed ({e.code}: {e.message}); falling back to git")
    if not removed:
        Git(root).proc("worktree", "remove", "--force", wt)
    Git(root).proc("worktree", "prune", check=False)
    Git(root).run("branch", "-d", slug)


def cmd_merge(a):
    root = Git.here().path
    cfg = Config.load(root)
    try:
        f = merge_preconditions(root, cfg, a.slug)
        land(root, f.wt, a.slug, cfg.base_branch)
        remove_worktree(root, f.wt, a.slug, cfg)
    except Die as e:  # a detached merge has nobody watching its output: say why it failed
        if a.notify:
            log = root / ".git" / f"harness-merge-{a.slug}.log"
            notify(a.notify, f"harness: {a.slug} merge failed: {e.msg.splitlines()[0]} (log: {log})")
        raise
    sha = Git(root).run("rev-parse", "--short", "HEAD")
    print(f"merged {a.slug} into {cfg.base_branch} at {sha}; status done; worktree removed; branch deleted")
    if a.notify:
        notify(a.notify, f"harness: {a.slug} merged into {cfg.base_branch} at {sha}")
    return 0


def cmd_doctor(a):
    return Doctor(Git.here().main_checkout(), tests=not a.no_tests, models=a.models).run()


# ----------------------------------------------------------------------------- cli
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="harness.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("config", help="the setup in a few lines: roles, models, gates, commands (read-only)")
    s.add_argument(
        "--apply",
        action="store_true",
        help="also write the orchestrator's model and permission mode to .claude/settings.local.json (human only)",
    )
    s.set_defaults(fn=cmd_config)
    sub.add_parser("status", help="board across every worktree").set_defaults(fn=cmd_status)

    s = sub.add_parser("start", help="new worktree + branch + feature folder (orchestrator or human, base branch)")
    s.add_argument("slug")
    s.add_argument("intent", nargs="?", default="")
    s.add_argument(
        "--detach", action="store_true", help="also start a dedicated orchestrator agent inside the worktree"
    )
    s.add_argument(
        "--trust", action="store_true", help="pass --trust-repository to herdr (only after verifying the repo)"
    )
    s.set_defaults(fn=cmd_start)

    for name, fn, help_ in (
        ("run", cmd_run, "run a role agent in the feature's worktree (orchestrator, from anywhere)"),
        ("wait", cmd_wait, "keep waiting for a running role agent (orchestrator)"),
    ):
        s = sub.add_parser(name, help=help_)
        s.add_argument("role", choices=list(ROLES))
        s.add_argument("slug")
        if name == "run":
            s.add_argument("msg", nargs="?", default="", help="extra one-line message for the role agent")
        s.add_argument(
            "--timeout", type=int, default=DEFAULT_TIMEOUT_MS, help=f"ms to wait (default {DEFAULT_TIMEOUT_MS})"
        )
        s.set_defaults(fn=fn)

    s = sub.add_parser("drive", help="run the feature's whole loop until it needs a human (orchestrator)")
    s.add_argument("slug")
    s.add_argument(
        "msg", nargs="?", default="", help="message for the first role run (at a gate: for the role being judged)"
    )
    s.add_argument(
        "--tab", action="store_true", help="run it in the 'driver' tab of the feature's workspace and return at once"
    )
    s.add_argument(
        "--notify", default="", metavar="PANE", help="herdr pane (hosting an agent) to prompt when the driver stops"
    )
    s.set_defaults(fn=cmd_drive)

    s = sub.add_parser("approve", help="approve a spec; the driver or detached orchestrator continues (human)")
    s.add_argument("slug")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("doctor", help="tools, config, a green test_cmd, leftovers (anyone)")
    s.add_argument("--no-tests", action="store_true", help="skip running test_cmd on the main checkout")
    s.add_argument(
        "--models", action="store_true", help="also send each Claude/Codex role one tiny prompt with its model"
    )
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser(
        "merge", help="merge an approved, reviewed feature; mark done; remove the worktree (human, base branch)"
    )
    s.add_argument("slug")
    s.add_argument(
        "--notify", default="", metavar="PANE", help="herdr pane (hosting an agent) to prompt with the outcome"
    )
    s.set_defaults(fn=cmd_merge)

    s = sub.add_parser("check", help="structural checks (+ tests from status review, or with --tests)")
    s.add_argument("slug", nargs="?")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--tests", action="store_true")
    g.add_argument("--no-tests", action="store_true")
    s.set_defaults(fn=cmd_check)

    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except HerdrError as e:
        die(f"herdr {e.code}: {e.message}")
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
