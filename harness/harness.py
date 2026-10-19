#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""SDD harness launcher on herdr. Python 3 stdlib only. Needs git; start/run/wait/merge also need herdr.

  harness.py status                        board across every worktree                 (anyone)
  harness.py start <slug> ["intent"]       new worktree + branch + feature folder       (orchestrator or human, base branch)
                     [--detach]            ... plus a dedicated orchestrator agent in it
  harness.py run <role> <slug> ["msg"]     run a role agent in the feature's worktree   (orchestrator, from anywhere)
  harness.py wait <role> <slug>            keep waiting for a running role agent        (orchestrator)
  harness.py approve <slug>                approve a spec and notify the orchestrator   (human)
  harness.py merge <slug>                  merge an approved, reviewed feature          (human, base branch)
  harness.py check [slug] [--tests]        structural checks (+ tests)                  (everyone)

Rules: harness/docs/sdd.md. Config: harness/config.json.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
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
    if not isinstance(cf, list) or not all(isinstance(x, str) and Path(x).parts and not x.startswith("/") and ".." not in Path(x).parts for x in cf):
        die(f"{p}: copy_files must be a list of paths or globs relative to the repo root")
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


def worktree_workspace(root: Path, wt: Path) -> str:
    """The herdr workspace id of this worktree; opens the workspace if it is closed."""
    wts = herdr("worktree", "list", "--cwd", root).get("worktrees", [])
    ws_id = next((w.get("open_workspace_id") for w in wts if Path(w.get("path", "")).resolve() == wt.resolve()), None)
    if ws_id:
        return ws_id
    return herdr("worktree", "open", *repo_source(root), "--path", wt, "--no-focus")["workspace"]["workspace_id"]


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


def report(wt: Path, role: str, slug: str, settled: bool, note: str = "") -> int:
    name = agent_name(role, slug)
    st, vd = feature_state(feature_dir(wt, slug))
    state = agent_state(name)
    if not settled:
        result = "STILL_WORKING"
    elif state == "blocked" or note:
        result = "NEEDS_ATTENTION"
    elif DONE_WHEN[role](st, vd):
        result = "DONE"
    else:
        result = "NEEDS_ATTENTION"
    print(f"{name}: state={state} status={st or '?'} verdict={vd or '-'} -> {result}" + (f" [{note}]" if note else ""))
    if result == "STILL_WORKING":
        print(f"next: harness.py wait {role} {slug}")
        return EXIT_WORKING
    print("--- last output ---")
    print(agent_tail(name).rstrip())
    print("---")
    if result == "DONE":
        print(f"next: {next_action(st, vd, load_cfg(wt)['gates'])}")
        return EXIT_DONE
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


def check_feature(root: Path, slug: str) -> list[str]:
    """Rules 1-5 of docs/sdd.md §7 for one feature in this checkout. Returns violations."""
    fdir = feature_dir(root, slug)
    spec_path, tasks_path = fdir / "spec.md", fdir / "tasks.md"
    if not spec_path.exists():
        return [f"{slug}: missing spec.md"]
    st = fm_read(spec_path).get("status", "")
    if st not in STATUSES:  # rule 1
        return [f"{slug}: spec.md status '{st or '(none)'}' is not one of {', '.join(STATUSES)}"]
    if RANK[st] < 1:
        return []
    errs = []
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
        errs += check_feature(root, s)
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
    rows = [("SLUG", "STATUS", "VERDICT", "BRANCH@PATH", "NEXT")]
    for slug, f in sorted(feats.items()):
        where = f"{f['branch'] or '(detached)'}@{str(f['wt']).replace(home, '~', 1)}"
        rows.append((slug, f["status"] or "?", f["verdict"] or "-", where, next_action(f["status"], f["verdict"], gates)))
    widths = [max(len(r[i]) for r in rows) for i in range(5)]
    for r in rows:
        print("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip())
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

    if cfg["copy_files"]:
        print("copy_files:", copy_ignored(root, wt, cfg["copy_files"]))
    if cfg.get("setup_cmd"):
        p = subprocess.run(cfg["setup_cmd"], shell=True, cwd=wt, text=True, capture_output=True, stdin=subprocess.DEVNULL)
        print("setup_cmd:", "ok" if p.returncode == 0 else f"FAILED (exit {p.returncode}); fix it in the worktree\n{(p.stdout + p.stderr).strip()[-800:]}")

    print(f"worktree: {wt}\nworkspace: {ws_id}  branch: {a.slug}  feature: harness/features/{a.slug}/")
    if not a.detach:
        print(f"next: harness.py run spec_author {a.slug}")
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


def cmd_run(a):
    root = repo_root()
    cfg = load_cfg(root)
    f = live_worktree(root, a.slug)
    wt, st, vd = f["wt"], f["status"], f["verdict"]
    if st not in RUN_GATE[a.role]:
        die(f"cannot run {a.role} while status is '{st or '(none)'}' (allowed: {', '.join(sorted(RUN_GATE[a.role]))}); next: {next_action(st, vd, cfg['gates'])}")
    if git("status", "--porcelain", cwd=wt):
        die(f"worktree {wt} has uncommitted changes; a role agent must not start on top of them")
    if a.role == "reviewer" or (a.role == "implementer" and vd == "APPROVED"):
        drop_review(wt, a.slug)
    text = f"Read harness/roles/{a.role}.md and follow it for harness/features/{a.slug}/."
    if a.msg:
        text += " " + " ".join(a.msg.split())
    settled, note = prompt_or_start(root, wt, a.role, a.slug, text, a.timeout, cfg["agents"][a.role])
    return report(wt, a.role, a.slug, settled, note)


def cmd_wait(a):
    root = repo_root()
    wt = live_worktree(root, a.slug)["wt"]
    name = agent_name(a.role, a.slug)
    try:
        herdr("agent", "wait", name, "--timeout", a.timeout)
        settled, note = True, ""
    except HerdrError as e:
        if e.code == "timeout":
            settled, note = False, ""
        elif e.code == "agent_not_found":
            die(f"no agent '{name}'; start it with: harness.py run {a.role} {a.slug}")
        else:
            settled, note = True, e.code
    return report(wt, a.role, a.slug, settled, note)


def cmd_approve(a):
    root = repo_root()
    f = locate(root, a.slug)
    if f["status"] != "spec_ready":
        die(f"'{a.slug}' is '{f['status'] or '(none)'}', expected spec_ready")
    errs = check_feature(f["wt"], a.slug)
    if errs:
        print("\n".join(errs))
        die("fix the spec before approving")
    spec = f["dir"] / "spec.md"
    fm_set(spec, "status", "approved")
    rel = spec.relative_to(f["wt"])
    git("add", rel, cwd=f["wt"])
    git("commit", "-q", "-m", f"{a.slug}: spec approved", "--", rel, cwd=f["wt"])
    print(f"approved {a.slug} (committed on '{f['branch']}')")
    try:
        herdr("agent", "prompt", a.slug, "Spec approved. Continue.")
        print(f"detached orchestrator '{a.slug}' notified")
    except HerdrError as e:
        if e.code == "agent_not_found":
            print(f"next: harness.py run implementer {a.slug}")
        else:
            print(f"warning: could not notify detached orchestrator '{a.slug}' ({e.code}: {e.message})")
    return 0


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

    p = sh(["git", "merge", "--no-edit", base], cwd=wt, check=False)
    if p.returncode != 0:
        sh(["git", "merge", "--abort"], cwd=wt, check=False)
        die(f"merging '{base}' into '{a.slug}' conflicts. Ask the orchestrator:\n"
            f"  herdr agent prompt {a.slug} \"run implementer {a.slug} 'Merge {base} into this branch, resolve conflicts, keep check --tests green, commit'\"\nthen run merge again")
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

    s = sub.add_parser("approve", help="approve a spec and notify the orchestrator (human)")
    s.add_argument("slug")
    s.set_defaults(fn=cmd_approve)

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
