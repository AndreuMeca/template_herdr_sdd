#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""SDD harness launcher on herdr. Python 3 stdlib only. Needs git; start/run/wait/drive/merge also need herdr.

  harness.py status                        board across every worktree, grouped by whose move it is (anyone)
  harness.py start <slug> ["intent"]       new worktree + branch + feature folder       (orchestrator or human, base branch)
                     [--detach]            ... plus a dedicated orchestrator agent in it
  harness.py drive <slug> ["msg"] --tab    run the feature's whole loop in a `driver` tab until it needs a human (orchestrator)
  harness.py run <role> <slug> ["msg"]     run one role agent in the feature's worktree (orchestrator, from anywhere)
  harness.py wait <role> <slug>            keep waiting for a running role agent        (orchestrator)
  harness.py approve <slug>                approve a spec; the driver or orchestrator continues (human)
  harness.py merge <slug>                  merge an approved, reviewed feature          (human, base branch)
  harness.py check [slug] [--tests]        structural checks (+ tests)                  (everyone)
  harness.py doctor                        tools, config, leftovers                     (anyone)

Rules: harness/docs/sdd.md. Config: harness/config.json.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

STATUSES = ["pending", "spec_ready", "approved", "in_progress", "review", "done", "blocked"]
RANK = {"pending": 0, "blocked": 0, "spec_ready": 1, "approved": 2, "in_progress": 3, "review": 4, "done": 5}
VERDICTS = ("APPROVED", "CHANGES_REQUESTED")
ROLES = {"spec_author": "spec", "implementer": "impl", "reviewer": "rev"}
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
EXIT_DONE, EXIT_ERROR, EXIT_ATTENTION, EXIT_WORKING = 0, 1, 2, 3
DEFAULT_TIMEOUT_MS = 100_000  # below Claude Code's 120 s default shell timeout
DRIVE_WAIT_MS = 300_000  # the driver waits for a role in slices of this length
FIX_TRIES = 3  # how often the driver sends a failed check back to the same role before stopping
REVIEW_REF = "refs/harness/{slug}/review-start"  # set by `run reviewer`, dropped by `run implementer` and `merge`
SYNC_MSG = "The base branch '{base}' was merged into this branch and the merge conflicts. Resolve the conflicts, keep `check --tests` green, commit."
SIDEBAR_ROWS = '[ui.sidebar.spaces]\nrows = [["state_icon", "workspace"], ["branch", "git_status"], ["$harness"]]'


# ----------------------------------------------------------------------------- shell / git
def die(msg: str, code: int = EXIT_ERROR):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def sh(args, cwd=None, check=True, stdin=None):
    p = subprocess.run([str(a) for a in args], cwd=cwd, text=True, capture_output=True, stdin=stdin)
    if check and p.returncode != 0:
        die(f"{' '.join(str(a) for a in args)}\n{(p.stderr or p.stdout).strip()}")
    return p


def git(*args, cwd=None, check=True) -> str:
    return sh(["git", *args], cwd=cwd, check=check).stdout.strip()


def repo_root(cwd=None) -> Path:
    return Path(git("rev-parse", "--show-toplevel", cwd=cwd))


def is_main_checkout(root: Path) -> bool:
    common = git("rev-parse", "--git-common-dir", cwd=root)
    return common == ".git" or Path(common).resolve() == (root / ".git").resolve()


def current_branch(cwd) -> str:
    return git("branch", "--show-current", cwd=cwd)


def main_checkout(cwd) -> Path:
    """The main checkout of the repo this path belongs to (itself when it is one)."""
    return Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd)).parent


def ref_exists(ref: str, cwd) -> bool:
    return sh(["git", "rev-parse", "-q", "--verify", ref], cwd=cwd, check=False).returncode == 0


def sync_base(wt: Path, base: str) -> tuple[bool, bool]:
    """Merge the base branch into the worktree's branch. Returns (merged cleanly, HEAD moved); a conflict is aborted."""
    before = git("rev-parse", "HEAD", cwd=wt)
    p = sh(["git", "merge", "--no-edit", base], cwd=wt, check=False)
    if p.returncode != 0:
        sh(["git", "merge", "--abort"], cwd=wt, check=False)
        return False, False
    return True, git("rev-parse", "HEAD", cwd=wt) != before


def worktrees(root: Path) -> list[tuple[Path, str]]:
    """[(path, branch)] from `git worktree list --porcelain`; the main checkout comes first."""
    items, path, branch = [], None, ""
    for line in git("worktree", "list", "--porcelain", cwd=root).splitlines() + [""]:
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):])
        elif line.startswith("branch refs/heads/"):
            branch = line[len("branch refs/heads/"):]
        elif line == "" and path is not None:
            items.append((path, branch))
            path, branch = None, ""
    return items


def valid_pattern(x) -> bool:
    return isinstance(x, str) and bool(Path(x).parts) and not x.startswith("/") and ".." not in Path(x).parts


def include_patterns(root: Path, cfg: dict) -> tuple[list[str], list[str]]:
    """copy_files from config.json plus the lines of .worktreeinclude; returns (patterns, rejected lines)."""
    pats, bad = list(cfg["copy_files"]), []
    wi = root / ".worktreeinclude"
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


def copy_ignored(src: Path, dst: Path, patterns: list[str]) -> str:
    """Copy the gitignored paths matching `patterns` from the main checkout `src` into the worktree `dst`; one report line."""
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
        if sh(["git", "check-ignore", "-q", rel], cwd=src, check=False).returncode != 0:
            skipped.append(str(rel))  # tracked or not ignored: the worktree already has git's version
            continue
        s, d = src / rel, dst / rel
        try:
            d.parent.mkdir(parents=True, exist_ok=True)
            if s.is_dir():
                shutil.copytree(s, d, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
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


def load_cfg(root: Path) -> dict:
    p = root / "harness" / "config.json"
    try:
        cfg = json.loads(p.read_text())
    except FileNotFoundError:
        die(f"missing {p}")
    except json.JSONDecodeError as e:
        die(f"{p}: {e}")
    for key in ("base_branch", "test_cmd", "agents"):
        if key not in cfg:
            die(f"{p}: missing '{key}'")
    for role in ("orchestrator", *ROLES):
        if not isinstance(cfg["agents"].get(role), dict) or not cfg["agents"][role].get("kind"):
            die(f"{p}: agents.{role}.kind is required")
    gates = cfg.setdefault("gates", {})
    for gate in ("approve", "merge"):
        gates.setdefault(gate, "human")
        if gates[gate] not in ("human", "auto"):
            die(f"{p}: gates.{gate} must be 'human' or 'auto'")
    cf = cfg.setdefault("copy_files", [])
    if not isinstance(cf, list) or not all(valid_pattern(x) for x in cf):
        die(f"{p}: copy_files must be a list of paths or globs relative to the repo root")
    rounds = cfg.setdefault("max_review_rounds", 3)
    if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 0:
        die(f"{p}: max_review_rounds must be an integer >= 0 (0 = no limit)")
    if not isinstance(cfg.setdefault("teardown_cmd", ""), str):
        die(f"{p}: teardown_cmd must be a shell command or \"\"")
    return cfg


# ----------------------------------------------------------------------------- frontmatter / features
def fm_read(path: Path) -> dict:
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return {}
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


def feature_dir(root: Path, slug: str) -> Path:
    return root / "harness" / "features" / slug


def feature_state(fdir: Path) -> tuple[str, str]:
    return fm_read(fdir / "spec.md").get("status", ""), fm_read(fdir / "review.md").get("verdict", "")


def features_everywhere(root: Path) -> dict:
    """slug -> {dir, wt, branch, status, verdict}; a worktree whose branch == slug wins over the base copy."""
    found = {}
    for wt, branch in worktrees(root):
        if not wt.exists():
            continue
        for spec in sorted((wt / "harness" / "features").glob("*/spec.md")):
            slug = spec.parent.name
            if slug.startswith("_"):
                continue
            st, vd = feature_state(spec.parent)
            if slug not in found or branch == slug:
                found[slug] = {"dir": spec.parent, "wt": wt, "branch": branch, "status": st, "verdict": vd}
    return found


def locate(root: Path, slug: str) -> dict:
    f = features_everywhere(root).get(slug)
    if not f:
        die(f"no feature '{slug}' in any worktree (see: harness.py status)")
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


def approved_once(wt: Path, slug: str, base: str) -> bool:
    """Whether `approve` ever ran on this branch (the commit it makes)."""
    return f"{slug}: spec approved" in git("log", "--format=%s", f"{base}..HEAD", cwd=wt, check=False).splitlines()


def review_rounds(wt: Path, slug: str, base: str) -> int:
    """Finished review rounds on this branch: commits that wrote review.md, minus the launcher's removals of it."""
    subjects = git("log", "--format=%s", f"{base}..HEAD", "--", f"harness/features/{slug}/review.md", cwd=wt, check=False).splitlines()
    return sum(1 for s in subjects if s != f"{slug}: clear previous review")


def block_feature(wt: Path, slug: str, reason: str):
    """Set status blocked, record why in log.md, commit. Used when the launcher itself stops a feature."""
    fdir = feature_dir(wt, slug)
    fm_set(fdir / "spec.md", "status", "blocked")
    log = fdir / "log.md"
    head = "" if log.exists() else "# Log\n"
    with log.open("a") as fh:
        fh.write(f"{head}\n## {datetime.now():%Y-%m-%d %H:%M} launcher\n- blockers: {reason}\n")
    git("add", f"harness/features/{slug}", cwd=wt)
    git("commit", "-q", "-m", f"{slug}: blocked by the launcher", "--", f"harness/features/{slug}", cwd=wt)


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
            raise HerdrError("cli_error", (p.stderr or p.stdout).strip()[:500])
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
        wts = herdr("worktree", "list", "--cwd", main_checkout(wt)).get("worktrees", [])
    except HerdrError:
        return None
    return next((w.get("open_workspace_id") for w in wts if Path(w.get("path", "")).resolve() == wt.resolve()), None)


def worktree_workspace(root: Path, wt: Path) -> str:
    """The herdr workspace id of this worktree; opens the workspace if it is closed."""
    return workspace_of(wt) or herdr("worktree", "open", *repo_source(root), "--path", wt, "--no-focus")["workspace"]["workspace_id"]


def sidebar(wt: Path, text: str):
    """Show the feature's state on its workspace in herdr's sidebar (Space token `$harness`, see help/config.md). Never fatal."""
    if shutil.which("herdr") is None:
        return
    try:
        ws_id = workspace_of(wt)
        if ws_id:
            herdr("workspace", "report-metadata", ws_id, "--source", "harness", "--token", f"harness={text[:80]}")
    except HerdrError:
        pass


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
    return Path(git("rev-parse", "--path-format=absolute", "--git-dir", cwd=wt)) / "harness-driver.pid"


def driver_running(wt: Path) -> int:
    """pid of a live driver for this worktree, else 0."""
    try:
        pid = int(driver_lock(wt).read_text().strip())
        os.kill(pid, 0)
        return pid
    except (FileNotFoundError, ValueError, ProcessLookupError):
        return 0
    except OSError:
        return pid


def driver_tab_pane(ws_id: str) -> str | None:
    """The root pane of the `driver` tab in this workspace, if any."""
    tab = next((t for t in herdr("tab", "list", "--workspace", ws_id).get("tabs", []) if t.get("label") == "driver"), None)
    if not tab:
        return None
    pane = next((p for p in herdr("pane", "list", "--workspace", ws_id).get("panes", []) if p.get("tab_id") == tab.get("tab_id")), None)
    return pane.get("pane_id") if pane else None


def launch_driver(root: Path, wt: Path, slug: str, msg: str, notify_pane: str) -> str:
    """Run `drive <slug>` in the `driver` tab of the feature's workspace (created if missing). Returns the pane id."""
    ws_id = worktree_workspace(root, wt)
    pane = driver_tab_pane(ws_id)
    if not pane:
        tab = herdr("tab", "create", "--workspace", ws_id, "--cwd", wt, "--label", "driver", "--no-focus")
        pane = find_key(tab.get("root_pane", tab), "pane_id") or die(f"tab create returned no pane_id: {tab}")
        time.sleep(1.5)  # the new pane's shell needs a moment to reach its prompt
    cmd = [sys.executable, str(Path(__file__).resolve()), "drive", slug, *([msg] if msg else []),
           *(["--notify", notify_pane] if notify_pane else [])]
    herdr("pane", "run", pane, shlex.join(cmd))
    return pane


def agent_name(role: str, slug: str) -> str:
    return f"{ROLES[role]}-{slug}"


def agent_cli_args(a: dict) -> list:
    model = ["--model", a["model"]] if a.get("model") else []
    extra = list(a.get("args") or [])
    return ["--", *model, *extra] if (model or extra) else []


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


def settle_startup(name: str, timeout_s: int = 60) -> bool:
    """Answer the known Claude Code startup dialogs (trust folder, bypass-permissions warning) and wait for idle.
    Returns True when the agent is ready for prompts, False when it is still blocked on something else."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        state = agent_state(name)
        if state in ("idle", "done"):
            return True
        if state == "blocked":
            text = screen(name)
            if "trust this folder" in text or "Bypass Permissions mode" in text or "Yes, I accept" in text:
                # both dialogs default to "No, exit"; the accepting option is the next one down
                herdr("agent", "send-keys", name, "down")
                herdr("agent", "send-keys", name, "enter")
                time.sleep(2)
                continue
            return False
        time.sleep(1)
    return agent_state(name) in ("idle", "done")


def prompt_or_start(root: Path, wt: Path, role: str, slug: str, text: str, timeout_ms: int, acfg: dict) -> tuple[bool, str]:
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
                pane_id = find_key(tab.get("root_pane", tab), "pane_id") or die(f"tab create returned no pane_id: {tab}")
                try:
                    herdr("agent", "start", name, "--kind", acfg["kind"], "--pane", pane_id,
                          "--timeout", 120_000, *agent_cli_args(acfg))
                except HerdrError as se:
                    if se.code != "agent_not_ready":
                        raise
                if not settle_startup(name):
                    return True, "blocked at startup"
                continue
            if e.code == "timeout":
                return False, ""
            if e.code in ("agent_blocked", "agent_prompt_stalled"):
                return True, e.code
            raise
    return False, "unreachable"


def evaluate(wt: Path, role: str, slug: str, settled: bool, note: str, cfg: dict) -> tuple[str, str, list[str], str]:
    """(result, note, check errors, agent state) of a role run. DONE needs the on-disk evidence AND a green structural
    check; a reviewer whose CHANGES_REQUESTED is the max_review_rounds-th blocks the feature for the human."""
    name = agent_name(role, slug)
    st, vd = feature_state(feature_dir(wt, slug))
    state = agent_state(name)
    errs: list[str] = []
    if not settled:
        result = "STILL_WORKING"
    elif state == "blocked" or note:
        result = "NEEDS_ATTENTION"
    elif not DONE_WHEN[role](st, vd):
        result, note = "NEEDS_ATTENTION", f"status {st or '?'} / verdict {vd or '-'} is not what {role} must leave"
    else:
        errs = check_feature(wt, slug, cfg["base_branch"])
        if errs:
            result, note = "NEEDS_ATTENTION", "check fails"
        else:
            result = "DONE"
            cap = cfg["max_review_rounds"]
            if role == "reviewer" and vd == "CHANGES_REQUESTED" and cap and review_rounds(wt, slug, cfg["base_branch"]) >= cap:
                block_feature(wt, slug, f"{cap} review rounds ended in CHANGES_REQUESTED (max_review_rounds); the last findings are in review.md")
                sh(["git", "update-ref", "-d", REVIEW_REF.format(slug=slug)], cwd=wt, check=False)
                result, note = "NEEDS_ATTENTION", f"blocked after {cap} review rounds"
    return result, note, errs, state


def report(wt: Path, role: str, slug: str, settled: bool, note: str, cfg: dict) -> int:
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
    sidebar(wt, state_text(st, vd, cfg["gates"]))
    if result == "DONE":
        print(f"next: {next_action(st, vd, cfg['gates'])}")
        return EXIT_DONE
    if errs:
        print(f"send it back: harness.py run {role} {slug} \"check fails: <the lines above>; fix, commit\"")
    print(f"inspect: herdr agent read {name} --source recent-unwrapped --lines 120")
    print(f"answer a dialog: herdr agent send-keys {name} <key>   (or ask the human)")
    return EXIT_ATTENTION


# ----------------------------------------------------------------------------- check
def section(text: str, heading: str) -> list[str]:
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().lower() == heading.lower()
            continue
        if inside:
            out.append(line)
    return out


def porcelain(wt: Path) -> list[tuple[str, str]]:
    """[(XY, path)] from `git status --porcelain`, untracked files listed one by one."""
    out = sh(["git", "status", "--porcelain", "--untracked-files=all"], cwd=wt).stdout
    return [(line[:2], line[3:].split(" -> ")[-1]) for line in out.splitlines() if len(line) > 3]


def changed_paths(wt: Path, base: str) -> list[str]:
    """Paths the branch changed since it forked from (or last merged) the base branch, plus uncommitted and untracked ones."""
    out = set(git("diff", "--name-only", f"{base}...HEAD", cwd=wt, check=False).splitlines()) | {p for _, p in porcelain(wt)}
    return sorted(p for p in out if p)


def scope_errors(wt: Path, slug: str, st: str, vd: str, base: str) -> list[str]:
    """Rules 7-9 of docs/sdd.md §7: what the branch, the spec phase and the reviewer may touch."""
    mine = f"harness/features/{slug}/"
    errs = []
    for p in changed_paths(wt, base):
        if p.startswith("harness/") and not p.startswith(mine):
            errs.append(f"{slug}: touches harness/ outside its folder: {p}")
        elif st in ("pending", "spec_ready", "approved") and not p.startswith(mine):
            errs.append(f"{slug}: changes files outside its folder before the spec is approved: {p}")
    ref = REVIEW_REF.format(slug=slug)
    if vd and ref_exists(ref, wt):  # the reviewer's commits: after the ref, not from the base branch, no merges
        touched = set(git("log", "--format=", "--name-only", "--no-merges", f"{ref}..HEAD", f"^{base}", cwd=wt, check=False).splitlines())
        status = {p: xy for xy, p in porcelain(wt)}
        for p in sorted((touched | status.keys()) - {""}):
            diff = git("diff", ref, "--", p, cwd=wt, check=False).splitlines()  # working tree vs the reviewer's start
            if not diff and status.get(p) != "??":
                continue  # touched and put back
            if p == mine + "spec.md":
                if any(l[1:].split(":")[0].strip() != "status" for l in diff if l[:1] in "+-" and not l.startswith(("+++", "---"))):
                    errs.append(f"{slug}: the reviewer changed more than the status line in spec.md")
            elif p != mine + "review.md":
                errs.append(f"{slug}: the reviewer changed {p} (only review.md and the status line of spec.md are allowed)")
    return errs


def check_feature(root: Path, slug: str, base: str) -> list[str]:
    """Rules 1-5 and 7-9 of docs/sdd.md §7 for one feature in this checkout. Returns violations."""
    fdir = feature_dir(root, slug)
    spec_path, tasks_path = fdir / "spec.md", fdir / "tasks.md"
    if not spec_path.exists():
        return [f"{slug}: missing spec.md"]
    st = fm_read(spec_path).get("status", "")
    if st not in STATUSES:  # rule 1
        return [f"{slug}: spec.md status '{st or '(none)'}' is not one of {', '.join(STATUSES)}"]
    errs = []
    if st != "done" and not is_main_checkout(root):  # rules 7-9 apply to a feature branch
        errs += scope_errors(root, slug, st, fm_read(fdir / "review.md").get("verdict", ""), base)
    if RANK[st] < 1:
        return errs
    text = re.sub(r"<!--.*?-->", "", spec_path.read_text(), flags=re.S)
    # rule 2
    reqs: dict[str, str] = {}
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
        if "SHALL" not in v:
            errs.append(f"{slug}: {k} has no SHALL")
    if st != "blocked" and any(l.strip() for l in section(text, "Open questions")):
        errs.append(f"{slug}: ## Open questions is not empty (answer them or set status blocked)")
    # rule 3
    tasks: list[tuple[str, str, str]] = []
    if not tasks_path.exists():
        errs.append(f"{slug}: missing tasks.md")
    else:
        covered = set()
        for line in tasks_path.read_text().splitlines():
            m = TASK_RE.match(line.strip())
            if not m:
                continue
            tasks.append((m[1], m[2], m[3]))
            cited = set(re.findall(r"\bR\d+\b", m[3]))
            if not cited:
                errs.append(f"{slug}: {m[2]} cites no requirement")
            for c in sorted(cited - reqs.keys()):
                errs.append(f"{slug}: {m[2]} cites unknown {c}")
            covered |= cited
        if not tasks:
            errs.append(f"{slug}: no task line '- [ ] T<n>: ... (R<n>)' in tasks.md")
        for r in sorted(active.keys() - covered, key=lambda x: int(x[1:])):
            errs.append(f"{slug}: {r} is not covered by any task")
    if RANK[st] < 4:
        return errs
    # rule 4
    for r in sorted(active, key=lambda x: int(x[1:])):
        p = sh(["git", "grep", "--untracked", "-q", f"{slug}:{r}", "--", ".", ":!harness"], cwd=root, check=False)
        if p.returncode != 0:
            errs.append(f"{slug}: no test carries the token {slug}:{r}")
    # rule 5
    for box, tid, _ in tasks:
        if box == " ":
            errs.append(f"{slug}: {tid} is not ticked")
    if not (fdir / "log.md").exists():
        errs.append(f"{slug}: missing log.md")
    return errs


def run_tests(root: Path, cfg: dict) -> list[str]:
    p = subprocess.run(cfg["test_cmd"], shell=True, cwd=root, text=True, capture_output=True, stdin=subprocess.DEVNULL)
    if p.returncode == 0:
        return []
    return [f"tests failed (exit {p.returncode}: {cfg['test_cmd']})\n{(p.stdout + p.stderr).strip()[-1500:]}"]


def run_check(root: Path, slugs: list[str], tests: bool | None) -> list[str]:
    """tests: None = only if any feature is at review or later; True/False = force."""
    cfg = load_cfg(root)
    errs, need_tests = [], bool(tests)
    for s in slugs:
        errs += check_feature(root, s, cfg["base_branch"])
        if tests is None and RANK.get(fm_read(feature_dir(root, s) / "spec.md").get("status", ""), 0) >= 4:
            need_tests = True
    if need_tests:
        errs += run_tests(root, cfg)
    return errs


# ----------------------------------------------------------------------------- commands
def cmd_status(a):
    root = repo_root()
    gates = load_cfg(root)["gates"]
    feats = features_everywhere(root)
    if not feats:
        print('no features yet. start one: harness.py start <slug> "intent"')
        return 0
    home = str(Path.home())
    groups: dict[str, list[tuple]] = {"needs you": [], "working": [], "done": []}
    for slug, f in sorted(feats.items()):
        nxt = next_action(f["status"], f["verdict"], gates)
        where = f"{f['branch'] or '(detached)'}@{str(f['wt']).replace(home, '~', 1)}"
        if f["status"] == "done":
            group = "done"
        elif needs_human(nxt):
            group = "needs you"
        else:
            group = "working"
            if f["branch"] == slug and driver_running(f["wt"]):
                nxt += "  (driver running)"
        groups[group].append((slug, f["status"] or "?", f["verdict"] or "-", where, nxt))
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
    root = repo_root()
    if a.slug:
        slugs = [a.slug]
        if not (feature_dir(root, a.slug) / "spec.md").exists():
            f = locate(root, a.slug)  # the feature lives in another worktree: check it there
            root = f["wt"]
            print(f"checking {a.slug} in {root}")
    else:
        slugs = sorted(p.name for p in (root / "harness" / "features").glob("*") if p.is_dir() and not p.name.startswith("_"))
    tests = True if a.tests else (False if a.no_tests else None)
    errs = run_check(root, slugs, tests)
    if errs:
        print("\n".join(errs))
        return 1
    print(f"OK ({', '.join(slugs) or 'no features'})")
    return 0


def cmd_start(a):
    root = repo_root()
    cfg = load_cfg(root)
    base = cfg["base_branch"]
    if not SLUG_RE.match(a.slug):
        die("slug must match ^[a-z][a-z0-9-]{1,23}$ (lowercase, digits, dashes)")
    if not is_main_checkout(root):
        die("run start from the main checkout, not from a worktree")
    if current_branch(root) != base:
        die(f"run start from the base branch '{base}' (currently on '{current_branch(root) or 'detached'}')")
    if sh(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{a.slug}"], cwd=root, check=False).returncode == 0:
        die(f"branch '{a.slug}' already exists")
    if a.slug in features_everywhere(root):
        die(f"feature '{a.slug}' already exists (see: harness.py status)")
    if not (root / "harness" / "features" / "_template" / "spec.md").exists():
        die("harness/features/_template/spec.md is missing")
    if git("status", "--porcelain", cwd=root):
        print("warning: the base checkout has uncommitted changes; the worktree starts from the last commit")

    extra = ["--trust-repository"] if a.trust else []
    try:
        res = herdr("worktree", "create", *repo_source(root), "--branch", a.slug, "--base", base,
                    "--label", a.slug, "--no-focus", *extra)
    except HerdrError as e:
        die(f"herdr worktree create -> {e.code}: {e.message}" + ("" if a.trust else "\n(if herdr asks for repository trust, verify the repo and retry with --trust)"))
    wt = Path(res["worktree"]["path"])
    pane_id = res["root_pane"]["pane_id"]
    ws_id = res["workspace"]["workspace_id"]

    fdir = feature_dir(wt, a.slug)
    shutil.copytree(wt / "harness" / "features" / "_template", fdir)
    title = a.slug.replace("-", " ").capitalize()
    intent = a.intent or "To be defined with the human: ask what this feature must do before writing requirements."
    for p in fdir.iterdir():
        p.write_text(p.read_text().replace("{{title}}", title).replace("{{slug}}", a.slug).replace("{{intent}}", intent))
    git("add", f"harness/features/{a.slug}", cwd=wt)
    git("commit", "-q", "-m", f"{a.slug}: new feature", cwd=wt)

    pats, bad = include_patterns(root, cfg)
    if pats or bad:
        print("copy_files:", (copy_ignored(root, wt, pats) if pats else "nothing listed")
              + (f"; rejected .worktreeinclude lines: {', '.join(bad)}" if bad else ""))
    if cfg.get("setup_cmd"):
        p = subprocess.run(cfg["setup_cmd"], shell=True, cwd=wt, text=True, capture_output=True, stdin=subprocess.DEVNULL)
        print("setup_cmd:", "ok" if p.returncode == 0 else f"FAILED (exit {p.returncode}); fix it in the worktree\n{(p.stdout + p.stderr).strip()[-800:]}")
    sidebar(wt, state_text("pending", "", cfg["gates"]))

    print(f"worktree: {wt}\nworkspace: {ws_id}  branch: {a.slug}  feature: harness/features/{a.slug}/")
    if not a.detach:
        print(f"next: harness.py drive {a.slug} --tab   (or one role at a time: harness.py run spec_author {a.slug})")
        return 0
    acfg = cfg["agents"]["orchestrator"]
    tab_id = find_key(res.get("tab", {}), "tab_id")
    if tab_id:
        try:
            herdr("tab", "rename", tab_id, "orchestrator")
        except HerdrError:
            pass
    started = False
    for attempt in range(3):  # the new pane's shell may need a moment to reach its prompt
        try:
            herdr("agent", "start", a.slug, "--kind", acfg["kind"], "--pane", pane_id, "--timeout", 120_000, *agent_cli_args(acfg))
            started = True
            break
        except HerdrError as e:
            if e.code == "agent_not_ready":
                started = True
                break
            if attempt == 2:
                print(f"warning: agent start -> {e.code}: {e.message}")
            else:
                time.sleep(1.5)
    text = f"Read harness/roles/orchestrator.md and follow it for harness/features/{a.slug}/."
    if started and not settle_startup(a.slug):
        print(f"warning: the orchestrator is blocked on a dialog; look at it: herdr agent read {a.slug} --source visible --lines 40")
        started = False
    if started:
        try:
            herdr("agent", "prompt", a.slug, text)
            print(f"orchestrator '{a.slug}' started and prompted")
        except HerdrError as e:
            print(f"warning: prompt -> {e.code}: {e.message}\nsend it yourself: herdr agent prompt {a.slug} \"{text}\"")
    else:
        print(f"start the orchestrator yourself in pane {pane_id}, then: herdr agent prompt {a.slug} \"{text}\"")
    print(f"detached orchestrator agent: {a.slug}   peek: herdr agent read {a.slug} --source recent-unwrapped --lines 60")
    return 0


def drop_review(root: Path, slug: str):
    review = feature_dir(root, slug) / "review.md"
    rel = review.relative_to(root)
    if sh(["git", "ls-files", "--error-unmatch", rel], cwd=root, check=False).returncode == 0:
        git("rm", "-q", rel, cwd=root)
        git("commit", "-q", "-m", f"{slug}: clear previous review", "--", rel, cwd=root)
    elif review.exists():
        review.unlink()


def live_worktree(root: Path, slug: str) -> dict:
    f = locate(root, slug)
    if f["branch"] != slug:
        die(f"'{slug}' has no live worktree (status {f['status'] or '?'} on '{f['branch'] or 'detached'}'); create one with: harness.py start {slug}")
    return f


def preflight(cfg: dict, wt: Path, role: str, slug: str, st: str, vd: str, tests_always: bool = False) -> str | None:
    """What stops `role` from starting now, or None. Prepares the branch: before a review the base branch is merged in
    and the result checked; the review ref marks where the reviewer starts; a new implementer round drops it."""
    base = cfg["base_branch"]
    if st not in RUN_GATE[role]:
        return f"cannot run {role} while status is '{st or '(none)'}' (allowed: {', '.join(sorted(RUN_GATE[role]))}); next: {next_action(st, vd, cfg['gates'])}"
    if git("status", "--porcelain", cwd=wt):
        return f"worktree {wt} has uncommitted changes; a role agent must not start on top of them"
    ref = REVIEW_REF.format(slug=slug)
    if role == "reviewer":
        ok, moved = sync_base(wt, base)
        if not ok:
            return f"merging '{base}' into '{slug}' conflicts; run: harness.py run implementer {slug} \"{SYNC_MSG.format(base=base)}\""
        if moved or tests_always:
            errs = run_check(wt, [slug], True)
            if errs:
                return (f"check --tests fails on '{slug}'" + (f" after merging '{base}'" if moved else "") + ":\n" + "\n".join(errs)
                        + f"\nrun: harness.py run implementer {slug} \"check --tests fails; fix it, commit\"")
    if role == "reviewer" or (role == "implementer" and vd == "APPROVED"):
        drop_review(wt, slug)
    if role == "reviewer":
        git("update-ref", ref, "HEAD", cwd=wt)
    elif role == "implementer":
        sh(["git", "update-ref", "-d", ref], cwd=wt, check=False)
    return None


def role_prompt(role: str, slug: str, msg: str) -> str:
    text = f"Read harness/roles/{role}.md and follow it for harness/features/{slug}/."
    return text + (" " + " ".join(msg.split()) if msg else "")


def cmd_run(a):
    root = repo_root()
    cfg = load_cfg(root)
    f = live_worktree(root, a.slug)
    wt, st, vd = f["wt"], f["status"], f["verdict"]
    pid = driver_running(wt)
    if pid:
        die(f"a driver (pid {pid}) is running {a.slug} and starts the roles itself; stop it first (kill {pid}) to run one by hand")
    err = preflight(cfg, wt, a.role, a.slug, st, vd)
    if err:
        die(err)
    sidebar(wt, state_text(st, vd, cfg["gates"], running=a.role))
    settled, note = prompt_or_start(root, wt, a.role, a.slug, role_prompt(a.role, a.slug, a.msg), a.timeout, cfg["agents"][a.role])
    return report(wt, a.role, a.slug, settled, note, cfg)


def cmd_wait(a):
    root = repo_root()
    cfg = load_cfg(root)
    wt = live_worktree(root, a.slug)["wt"]
    name = agent_name(a.role, a.slug)
    settled, note = wait_agent(name, a.timeout)
    if note == "agent_not_found":
        die(f"no agent '{name}'; start it with: harness.py run {a.role} {a.slug}")
    return report(wt, a.role, a.slug, settled, note, cfg)


def approve_spec(wt: Path, slug: str, branch: str):
    spec = feature_dir(wt, slug) / "spec.md"
    fm_set(spec, "status", "approved")
    rel = spec.relative_to(wt)
    git("add", rel, cwd=wt)
    git("commit", "-q", "-m", f"{slug}: spec approved", "--", rel, cwd=wt)
    print(f"approved {slug} (committed on '{branch}')")


def resume(root: Path, wt: Path, slug: str, text: str) -> int:
    """After a human gate: wake the detached orchestrator, else relaunch the driver in its tab, else say what to run."""
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


def cmd_approve(a):
    root = repo_root()
    cfg = load_cfg(root)
    f = locate(root, a.slug)
    if f["status"] != "spec_ready":
        die(f"'{a.slug}' is '{f['status'] or '(none)'}', expected spec_ready")
    errs = check_feature(f["wt"], a.slug, cfg["base_branch"])
    if errs:
        print("\n".join(errs))
        die("fix the spec before approving")
    approve_spec(f["wt"], a.slug, f["branch"])
    sidebar(f["wt"], state_text("approved", "", cfg["gates"]))
    return resume(root, f["wt"], a.slug, "Spec approved. Continue.")


def cmd_merge(a):
    root = repo_root()
    cfg = load_cfg(root)
    base = cfg["base_branch"]
    if not is_main_checkout(root):
        die("run merge from the main checkout, never from inside a worktree")
    if current_branch(root) != base:
        die(f"run merge from the base branch '{base}' (currently on '{current_branch(root) or 'detached'}')")
    if git("status", "--porcelain", cwd=root):
        die("the main checkout has uncommitted changes; commit or stash them first")
    f = locate(root, a.slug)
    if f["branch"] != a.slug or f["wt"] == root:
        die(f"no live worktree on branch '{a.slug}' (already merged?)")
    if f["status"] != "review" or f["verdict"] != "APPROVED":
        die(f"'{a.slug}' is status={f['status'] or '?'} verdict={f['verdict'] or '-'}; need status review and verdict APPROVED")
    wt = f["wt"]
    if git("status", "--porcelain", cwd=wt):
        die(f"worktree {wt} has uncommitted changes; ask the orchestrator to commit them")

    ok, _ = sync_base(wt, base)
    if not ok:
        die(f"merging '{base}' into '{a.slug}' conflicts. Run:\n"
            f"  harness.py run implementer {a.slug} \"{SYNC_MSG.format(base=base)}\"   (or: harness.py drive {a.slug} --tab)\nthen run merge again")
    errs = run_check(wt, [a.slug], True)
    if errs:
        print("\n".join(errs))
        die("check failed on the merged worktree; not merging")

    git("merge", "--no-ff", "--no-edit", a.slug, cwd=root)
    spec = feature_dir(root, a.slug) / "spec.md"
    fm_set(spec, "status", "done")
    rel = spec.relative_to(root)
    git("add", rel, cwd=root)
    git("commit", "-q", "-m", f"{a.slug}: done", "--", rel, cwd=root)

    if cfg["teardown_cmd"]:
        p = subprocess.run(cfg["teardown_cmd"], shell=True, cwd=wt, text=True, capture_output=True, stdin=subprocess.DEVNULL)
        print("teardown_cmd:", "ok" if p.returncode == 0 else f"FAILED (exit {p.returncode})\n{(p.stdout + p.stderr).strip()[-800:]}")
    sh(["git", "update-ref", "-d", REVIEW_REF.format(slug=a.slug)], cwd=root, check=False)
    removed = False
    try:
        wts = herdr("worktree", "list", "--cwd", root).get("worktrees", [])
        ws_id = next((w.get("open_workspace_id") for w in wts if Path(w.get("path", "")).resolve() == wt.resolve()), None)
        if ws_id:
            herdr("worktree", "remove", "--workspace", ws_id, "--force")
            removed = True
    except HerdrError as e:
        print(f"herdr worktree remove failed ({e.code}: {e.message}); falling back to git")
    if not removed:
        sh(["git", "worktree", "remove", "--force", wt], cwd=root)
    sh(["git", "worktree", "prune"], cwd=root, check=False)
    git("branch", "-d", a.slug, cwd=root)
    print(f"merged {a.slug} into {base} at {git('rev-parse', '--short', 'HEAD', cwd=root)}; status done; worktree removed; branch deleted")
    return 0


# ----------------------------------------------------------------------------- drive
def cmd_drive(a):
    root = repo_root()
    cfg = load_cfg(root)
    f = live_worktree(root, a.slug)
    pid = driver_running(f["wt"])
    if pid:
        die(f"a driver (pid {pid}) is already running {a.slug}")
    if a.tab:
        pane = launch_driver(main_checkout(root), f["wt"], a.slug, a.msg, os.environ.get("HERDR_PANE_ID", ""))
        print(f"driver for {a.slug}: pane {pane}, tab 'driver' of its workspace. It runs the roles one after another and stops "
              f"when the feature needs a human; then it prompts this pane (if an agent runs here) and `status` says what to do.")
        print(f"watch: herdr pane read {pane} --lines 40")
        return 0
    return drive(root, cfg, f, a.slug, a.msg, a.notify)


def drive(root: Path, cfg: dict, f: dict, slug: str, msg: str, notify_pane: str) -> int:
    """The orchestrator's loop as a process: run the next role, send failed checks and conflicts back, approve and merge
    when the gates are auto, stop (and notify) when the feature needs a human. `msg` goes to the first role run; at a
    gate or when blocked it goes to the role that produced what is being judged."""
    wt, base, gates = f["wt"], cfg["base_branch"], cfg["gates"]
    fdir = feature_dir(wt, slug)
    lock = driver_lock(wt)
    lock.write_text(str(os.getpid()))
    fails = 0  # checks sent back in a row

    def stamp() -> str:
        return f"[{datetime.now():%H:%M}]"

    def stop(text: str, code: int = EXIT_DONE) -> int:
        st, vd = feature_state(fdir)
        sidebar(wt, state_text(st, vd, gates))
        line = f"harness: {slug} {text}"
        print(stamp(), line)
        if notify_pane:
            notify(notify_pane, line + " (board: harness.py status)")
        return code

    print(stamp(), f"driving {slug} in {wt}" + (f"; will notify pane {notify_pane}" if notify_pane else ""))
    try:
        while True:
            st, vd = feature_state(fdir)
            nxt = next_action(st, vd, gates)
            sidebar(wt, state_text(st, vd, gates))
            text, msg = msg, ""
            if nxt.startswith("run "):
                role = nxt[4:]
            elif nxt == "approve (auto)":
                errs = check_feature(wt, slug, base)
                if errs:
                    role, text = "spec_author", "check fails:\n" + "\n".join(errs) + "\nFix the spec, commit."
                else:
                    approve_spec(wt, slug, f["branch"])
                    continue
            elif nxt == "approve (human)":
                if not text:
                    return stop(f"spec ready, needs you: read {fdir / 'spec.md'}, then `harness.py approve {slug}`, "
                                f"or `harness.py drive {slug} \"<feedback for the spec author>\" --tab`")
                role = "spec_author"
            elif nxt == "merge (auto)":
                main = main_checkout(wt)
                log = main / ".git" / f"harness-merge-{slug}.log"
                with log.open("w") as fh:  # detached: `merge` removes this worktree, its workspace and this very pane
                    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "merge", slug], cwd=main,
                                     stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
                print(stamp(), f"merging {slug} in the background (log: {log}); this workspace closes when it is done")
                if notify_pane:
                    notify(notify_pane, f"harness: {slug} review approved; merging it now (log: {log})")
                return EXIT_DONE
            elif nxt == "merge (human)":
                if not text:
                    return stop(f"review approved, needs you: `harness.py merge {slug}` from the main checkout")
                role = "implementer"
            elif st == "blocked":
                if not text:
                    return stop(f"blocked, needs you: read `## Open questions` in {fdir / 'spec.md'} and {fdir / 'log.md'}; "
                                f"answer with `harness.py drive {slug} \"<answer>\" --tab`")
                role = "implementer" if approved_once(wt, slug, base) else "spec_author"
            elif st == "done":
                return stop("is done")
            else:
                return stop(f"has a status the lifecycle does not know ('{st}'); fix spec.md", EXIT_ERROR)

            if role == "reviewer":  # the reviewer sees the branch as it will land, and only when it is green
                ok, _ = sync_base(wt, base)
                if not ok:
                    role, text = "implementer", SYNC_MSG.format(base=base)
                else:
                    errs = run_check(wt, [slug], True)
                    if errs:
                        role, text = "implementer", "check --tests fails on the branch:\n" + "\n".join(errs)[-1500:] + "\nFix it, commit."
                        fails += 1
                        if fails > FIX_TRIES:
                            block_feature(wt, slug, f"check --tests still fails after {FIX_TRIES} fixes; last output in the driver tab")
                            return stop(f"blocked, needs you: check keeps failing after {FIX_TRIES} fixes", EXIT_ATTENTION)
            st, vd = feature_state(fdir)
            err = preflight(cfg, wt, role, slug, st, vd)
            if err:
                return stop(f"cannot run {role}: {err}", EXIT_ERROR)
            name = agent_name(role, slug)
            print(stamp(), f"run {role}" + (f": {' '.join(text.split())[:120]}" if text else ""))
            sidebar(wt, state_text(st, vd, gates, running=role))
            settled, note = prompt_or_start(root, wt, role, slug, role_prompt(role, slug, text), DRIVE_WAIT_MS, cfg["agents"][role])
            while not settled:
                settled, note = wait_agent(name, DRIVE_WAIT_MS)
            result, note, errs, state = evaluate(wt, role, slug, True, note, cfg)
            print(stamp(), f"{name}: state={state} -> {result}" + (f" [{note}]" if note else ""))
            if errs:
                print("\n".join(errs))
            if result == "DONE":
                fails = 0
                continue
            if errs and fails < FIX_TRIES:  # a failed check goes back; the loop picks the role from the status
                fails += 1
                msg = "check fails:\n" + "\n".join(errs) + "\nFix it, commit."
                continue
            if errs:
                block_feature(wt, slug, f"check still fails after {FIX_TRIES} fixes: " + "; ".join(errs)[:500])
                return stop(f"blocked, needs you: check keeps failing after {FIX_TRIES} fixes", EXIT_ATTENTION)
            return stop(f"needs you: {role} stopped ({note or 'see its screen'}); inspect: herdr agent read {name} --source recent-unwrapped --lines 120",
                        EXIT_ATTENTION)
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


# ----------------------------------------------------------------------------- doctor
def cmd_doctor(a):
    root = main_checkout(repo_root())
    problems: list[str] = []
    notes: list[str] = []

    def check(cond: bool, msg: str, fix: str = ""):
        print(("ok    " if cond else "FAIL  ") + msg + ("" if cond or not fix else f"\n      fix: {fix}"))
        if not cond:
            problems.append(msg)

    check(shutil.which("git") is not None, "git in PATH")
    check(shutil.which("herdr") is not None, "herdr in PATH", "https://herdr.dev/docs/install/")
    if shutil.which("uv") is None:
        notes.append("uv is not in PATH; run the launcher as `python3 harness/harness.py ...`")
    cfg = load_cfg(root)
    check(True, "harness/config.json is valid")
    base = cfg["base_branch"]
    check(ref_exists(f"refs/heads/{base}", root), f"base branch '{base}' exists")
    for role in ("orchestrator", *ROLES):
        kind = cfg["agents"][role]["kind"]
        check(shutil.which(kind) is not None, f"{role}: '{kind}' CLI in PATH", "install it and log in, or change agents.%s.kind" % role)
    pats, bad = include_patterns(root, cfg)
    for pat in pats:
        if list(root.glob(pat)):
            check(True, f"copy_files: {pat} is in the main checkout")
        else:
            notes.append(f"copy_files: {pat} is not in the main checkout, so new worktrees will not get it")
    for line in bad:
        check(False, f".worktreeinclude line rejected: {line}", "relative paths only, no '..'")

    feats = features_everywhere(root)
    wts = worktrees(root)
    live = {branch for _, branch in wts}
    for wt, branch in wts[1:]:
        f = feats.get(branch)
        if not wt.exists():
            check(False, f"worktree {wt} is missing on disk", "git worktree prune")
        elif not f or f["wt"] != wt:
            check(False, f"worktree {wt} ({branch or 'detached'}) has no feature folder", f"git worktree remove {wt}")
        elif f["status"] == "done":
            check(False, f"worktree {wt}: feature {branch} is done", f"git worktree remove {wt} && git branch -d {branch}")
        else:
            check(True, f"worktree {wt}: {branch} ({f['status']})")
    for b in git("for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=root).splitlines():
        spec = root / "harness" / "features" / b / "spec.md"
        if b != base and b not in live and spec.exists():
            st = fm_read(spec).get("status", "?")
            check(False, f"branch '{b}' has no worktree (its feature is {st} on {base})",
                  f"git branch -D {b}" if st == "done" else f"herdr worktree open --cwd {root} --branch {b}")
    for ref in git("for-each-ref", "--format=%(refname)", "refs/harness", cwd=root, check=False).splitlines():
        slug = ref.split("/")[2] if ref.count("/") >= 3 else ""
        if slug not in live:
            check(False, f"stale {ref}", f"git update-ref -d {ref}")

    if shutil.which("herdr") is not None:
        try:
            wss = herdr("workspace", "list").get("workspaces", [])
        except HerdrError as e:
            wss = None
            check(False, f"herdr server reachable ({e.code}: {e.message})", "open herdr")
        if wss is not None:
            check(True, "herdr server reachable")
            on_main = [w["workspace_id"] for w in wss if (w.get("worktree") or {}).get("checkout_path")
                       and Path(w["worktree"]["checkout_path"]).resolve() == root.resolve()]
            check(len(on_main) <= 1, "one herdr workspace on the main checkout" + (f" (found {', '.join(on_main)})" if len(on_main) > 1 else ""),
                  "close the extra ones once their worktree workspaces are merged: herdr workspace close <id>")
            for w in wss:
                info = w.get("worktree") or {}
                if info.get("is_linked_worktree") and info.get("repo_root") and Path(info["repo_root"]).resolve() == root.resolve() \
                        and not Path(info.get("checkout_path", "")).exists():
                    check(False, f"workspace {w['workspace_id']} '{w.get('label')}' points at a removed worktree", f"herdr workspace close {w['workspace_id']}")
            cfgp = Path.home() / ".config" / "herdr" / "config.toml"
            text = cfgp.read_text() if cfgp.exists() else ""
            if "$harness" not in text:
                notes.append(f"herdr's sidebar does not show feature states yet. Add to {cfgp}:\n{SIDEBAR_ROWS}\nthen: herdr server reload-config")
    for n in notes:
        print("note  " + n)
    print(f"\n{len(problems)} problem(s)" if problems else "\nall good")
    return 1 if problems else 0


# ----------------------------------------------------------------------------- cli
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="harness.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="board across every worktree").set_defaults(fn=cmd_status)

    s = sub.add_parser("start", help="new worktree + branch + feature folder (orchestrator or human, base branch)")
    s.add_argument("slug")
    s.add_argument("intent", nargs="?", default="")
    s.add_argument("--detach", action="store_true", help="also start a dedicated orchestrator agent inside the worktree")
    s.add_argument("--trust", action="store_true", help="pass --trust-repository to herdr (only after verifying the repo)")
    s.set_defaults(fn=cmd_start)

    for name, fn, help_ in (("run", cmd_run, "run a role agent in the feature's worktree (orchestrator, from anywhere)"),
                            ("wait", cmd_wait, "keep waiting for a running role agent (orchestrator)")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("role", choices=list(ROLES))
        s.add_argument("slug")
        if name == "run":
            s.add_argument("msg", nargs="?", default="", help="extra one-line message for the role agent")
        s.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_MS, help=f"ms to wait (default {DEFAULT_TIMEOUT_MS})")
        s.set_defaults(fn=fn)

    s = sub.add_parser("drive", help="run the feature's whole loop until it needs a human (orchestrator)")
    s.add_argument("slug")
    s.add_argument("msg", nargs="?", default="", help="message for the first role run (at a gate: for the role being judged)")
    s.add_argument("--tab", action="store_true", help="run it in the 'driver' tab of the feature's workspace and return at once")
    s.add_argument("--notify", default="", metavar="PANE", help="herdr pane (hosting an agent) to prompt when the driver stops")
    s.set_defaults(fn=cmd_drive)

    s = sub.add_parser("approve", help="approve a spec; the driver or detached orchestrator continues (human)")
    s.add_argument("slug")
    s.set_defaults(fn=cmd_approve)

    sub.add_parser("doctor", help="tools, config and leftovers (anyone)").set_defaults(fn=cmd_doctor)

    s = sub.add_parser("merge", help="merge an approved, reviewed feature; mark done; remove the worktree (human, base branch)")
    s.add_argument("slug")
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
