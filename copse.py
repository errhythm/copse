"""copse: task worktrees across git repos."""
import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import nullcontext
from pathlib import Path
from typing import Annotated, Optional

import typer
from importlib.metadata import PackageNotFoundError, distribution, version as _pkg_version
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

for _s in (sys.stdout, sys.stderr):  # a cp1252 pipe (Windows CI, redirects) must print "?" for ✔●◇, not raise UnicodeEncodeError
    if _s and "utf" not in (getattr(_s, "encoding", "") or "").lower() and hasattr(_s, "reconfigure"):
        _s.reconfigure(errors="replace")

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)


try:
    __version__ = _pkg_version("copse")
except PackageNotFoundError:
    __version__ = "unknown"


def _version(v: bool):
    if v:
        typer.echo(f"copse {__version__}")
        raise typer.Exit()


@app.callback()
def main(version: Annotated[bool, typer.Option("--version", callback=_version, is_eager=True, help="Show version and exit")] = False):
    pass


Root = Annotated[Optional[Path], typer.Option("--root", envvar="COPSE_ROOT", help="Task root (default ./tasks)")]
Json = Annotated[bool, typer.Option("--json", help="Machine-readable output")]
Task = Annotated[str, typer.Argument(metavar="TASK", help="Task name, e.g. an issue number; becomes the folder name")]
Repos = Annotated[Optional[list[str]], typer.Option("-r", "--repo", help="repo[:branch[:base]] (repeatable)")]
RmRepos = Annotated[Optional[list[str]], typer.Option("-r", "--repo", help="Repo id or folder name (repeatable)")]


def git(*args, cwd=None):
    try:  # no credential prompts: agents must never hang; a missing git/cwd is a per-repo failure, not a traceback
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except OSError as e:
        return 127, str(e)
    return p.returncode, (p.stdout if p.returncode == 0 else p.stderr or p.stdout).strip()


def task_root(root):
    return (root or Path.cwd() / "tasks").resolve()


def task_dir(root, task):
    if task in ("", ".", "..") or Path(task).name != task:  # "..", "/x", "a/b" would escape the task root
        fail(f"invalid task name '{task}'")
    return task_root(root) / task


def scan(base: Path, skip: Path):
    """Repos (.git is a directory) at depth 1 and 2 under base, as {relative id: path}."""
    found = {}
    def sub(d):
        try:
            return sorted(c for c in d.iterdir() if c.is_dir() and not c.name.startswith(".") and c.name != "node_modules" and c.resolve() != skip)
        except OSError:
            return []
    for d in sub(base):
        if (d / ".git").is_dir():
            found[d.name] = d
        elif not (d / ".git").exists():  # a .git file means a worktree: skip
            for c in sub(d):
                if (c / ".git").is_dir():
                    found[f"{d.name}/{c.name}"] = c
    return found


out, err = Console(highlight=False, emoji=False), Console(stderr=True, highlight=False, emoji=False)  # no force_terminal: rich drops ANSI when piped / NO_COLOR
MARK = {"created": ("✔", "green"), "removed": ("✔", "green"), "exists": ("•", "dim"), "planned": ("○", "cyan"), "failed": ("✖", "red")}


def is_tty():
    return sys.stdin.isatty()


def fail(msg):
    err.print(f"error: {msg}", style="red", markup=False)
    raise typer.Exit(2)


def emit(obj, as_json, render):
    if as_json:
        typer.echo(json.dumps(obj, indent=2))  # plain json.dumps: agents parse this, never rich
    else:
        render()


def short(p):
    return str(p).replace(str(Path.home()), "~", 1)


def rel(p):
    try:
        return os.path.relpath(p)
    except ValueError:  # Windows: path on another drive than cwd
        return p


def status_line(r, detail, w=0):  # w: repo column width across the batch, so lines align
    sym, col = MARK[r["status"]]
    out.print(f"[{col}]{sym}[/] {escape(r['repo']):{w}}  {escape(detail)}" + (f"  [red]{escape(r['error'])}[/]" if r["error"] else ""), overflow="fold")


def has_ref(repo, ref):
    return git("show-ref", "--verify", "--quiet", ref, cwd=repo)[0] == 0


# adapted from Wirasm/prp (MIT) and Tracer-Cloud/opensre (Apache-2.0): symbolic-ref origin/HEAD, else current branch
def default_base(repo, remote):
    rc, out = git("symbolic-ref", f"refs/remotes/{remote}/HEAD", cwd=repo)
    if rc == 0:  # full name: --short turns into "remotes/origin/x" when a local "origin/x" branch exists
        return out.removeprefix(f"refs/remotes/{remote}/")
    return git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)[1]


# adapted from laudiacay/fwts (MIT) and NousResearch/hermes-agent (MIT): fetch, then `worktree add --track -b` for remote-only branches
def resolve_and_add(repo, path, branch, base, remote, fetch, dry, task=None, from_="remote"):
    r = {"repo": None, "path": str(path), "branch": branch, "base": base, "status": "created", "error": None}
    if (path / ".git").exists():
        rc, common = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=path)
        if rc or Path(common).parent.resolve() != Path(repo).resolve():
            r.update(status="failed", error=f"{path} exists and is not a worktree of this repo")
        else:
            r.update(status="exists", branch=git("rev-parse", "--abbrev-ref", "HEAD", cwd=path)[1], base=None)
        return r
    if fetch and not dry:
        git("fetch", remote, cwd=repo)  # best effort: repos without the remote still work
    r["base"] = base = base or default_base(repo, remote)
    if has_ref(repo, f"refs/heads/{branch}"):
        cmd, start = ["worktree", "add", str(path), branch], branch
    elif has_ref(repo, f"refs/remotes/{remote}/{branch}"):
        start = f"{remote}/{branch}"
        cmd = ["worktree", "add", "--track", "-b", branch, str(path), f"refs/remotes/{start}"]
    else:
        # explicit "<remote>/x" base wins; else --from remote prefers <remote>/<base>, --from local uses <base>.
        # Full ref names: a local branch "origin/x" or a tag "x" must not shadow (or make ambiguous) the chosen ref.
        if base.startswith(f"{remote}/") and has_ref(repo, f"refs/remotes/{base}"):
            start, ref = base, f"refs/remotes/{base}"
        elif from_ == "remote" and has_ref(repo, f"refs/remotes/{remote}/{base}"):
            start = f"{remote}/{base}"
            ref = f"refs/remotes/{start}"
        elif has_ref(repo, f"refs/heads/{base}"):
            start, ref = base, f"refs/heads/{base}"
        else:
            start = ref = base  # a sha or tag
        cmd = ["worktree", "add", "--no-track", "-b", branch, str(path), ref]
        if git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=repo)[0]:
            r.update(status="failed", error=f"start ref '{start}' does not exist", start=start)
            return r
    if dry:
        r.update(status="planned", start=start)
        return r
    rc, out = git(*cmd, cwd=repo)
    if rc:
        r.update(status="failed", error=out)
    elif "-b" in cmd and task:  # mark branches copse created, so rm --delete-branch spares attached ones
        git("config", f"branch.{branch}.copseTask", task, cwd=repo)
        if cmd[-1].startswith("refs/"):  # the base it was cut from, for ls `merged` (a sha/tag base falls back to origin/HEAD)
            git("config", f"branch.{branch}.copseBase", cmd[-1], cwd=repo)
    if not rc and "--no-track" in cmd and git("remote", "get-url", remote, cwd=repo)[0] == 0:
        # upstream = <remote>/<same name>, configured before the remote branch exists; ls shows null until the first push
        git("config", f"branch.{branch}.remote", remote, cwd=repo)
        git("config", f"branch.{branch}.merge", f"refs/heads/{branch}", cwd=repo)
    return r


def copy_include(main, path):
    """Copy gitignored files matching <main>/.worktreeinclude into the new worktree: (copied, errors). Never overwrites, skips symlinks."""
    inc, main = Path(main) / ".worktreeinclude", Path(main)
    if not inc.is_file():
        return [], []
    def lsf(*a):
        rc, o = git("ls-files", "-z", "--others", "--ignored", *a, cwd=main)
        return set() if rc else set(o.split("\0")) - {""}  # a git error is not a file list
    copied, errors = [], []
    for f in sorted(lsf("--exclude-standard") & lsf(f"--exclude-from={inc}")):  # --exclude-from alone ignores .gitignore, so intersect: ignored AND listed
        s, d = main / f, path / f
        if s.is_symlink() or os.path.lexists(d):
            continue
        try:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
            copied.append(f)
        except OSError as e:
            errors.append(f"{f}: {e}")
    return copied, errors


def run_setup(main, path, task, rid):
    """Run the setup hook (git config copse.setup, else executable .copse/setup) in the worktree: None if no hook, else (ok, error tail)."""
    cmd, script = git("config", "--get", "copse.setup", cwd=main), Path(main) / ".copse" / "setup"
    if cmd[0] == 0 and cmd[1]:
        argv, shell = cmd[1], True
    elif script.is_file() and os.access(script, os.X_OK):
        argv, shell = [str(script)], False
    else:
        return None
    # stdin=DEVNULL: a hook that reads stdin must not hang; output to a file, not a pipe, so a backgrounded child holding it can't either
    with tempfile.TemporaryFile() as log:
        try:
            p = subprocess.run(argv, cwd=path, shell=shell, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                               env={**os.environ, "COPSE_TASK": task, "COPSE_REPO": rid, "COPSE_MAIN": str(main)})
        except OSError as e:
            return False, str(e)
        log.seek(0)
        tail = log.read().decode(errors="replace").strip()[-300:]
    return p.returncode == 0, ("" if p.returncode == 0 else f"setup exited {p.returncode}: {tail}")


# adapted from tconbeer/harlequin (MIT) and ClawBench (Apache-2.0): one module-level questionary Style, accent + dim hints
# ansi* names use the terminal's own palette (plain "cyan" is #00ffff, unreadable on light backgrounds)
STYLE = [("qmark", "fg:ansicyan bold"), ("question", "bold"), ("answer", "fg:ansicyan bold"), ("pointer", "fg:ansicyan bold"),
         ("highlighted", "fg:ansicyan bold"), ("selected", "fg:ansigreen"), ("instruction", "fg:#808080"), ("separator", "fg:#808080")]


# adapted from bentoml/OpenLLM (Apache-2.0): checkbox of aligned table-row titles, use_search_filter + use_jk_keys=False
def ask(q):
    a = q.ask()
    if a is None:  # Ctrl-C: stop at once, before the next prompt (None would also become a branch named "None")
        raise typer.Exit(2)
    return a


def pick_interactive(repos, task, branch, base, from_="remote"):
    import questionary
    from questionary import Choice, Style
    if not repos:
        fail("no git repos found 1-2 levels below here")
    w = max(map(len, repos))
    choices = [Choice(f"{k:{w}}  {git('rev-parse', '--abbrev-ref', 'HEAD', cwd=p)[1] if p else '':10}  {short(p) if p else ''}", value=k) for k, p in repos.items()]
    kw = dict(style=Style(STYLE), qmark="◇")
    # erase_when_done: questionary's own summary is "done (2 selections)" or the whole padded title; print the ids instead
    ids = ask(questionary.checkbox("Repos", choices=choices, qmark="◆", pointer="│", style=Style(STYLE), use_search_filter=True,
                                   use_jk_keys=False, instruction="(space to pick, type to filter)", erase_when_done=True))
    if not ids:
        raise typer.Exit(2)
    out.print(f"[cyan]◇[/] [bold]Repos[/] [cyan bold]{escape(', '.join(ids))}[/]")
    b = ask(questionary.text("Branch", default=branch or task, **kw))
    ba = ask(questionary.text("Base (blank = auto)", default=base or "", **kw))
    fr = ask(questionary.select("Start new branches from", choices=["remote", "local"], default=from_, pointer="›", **kw))
    return [f"{i}:{b}:{ba}" for i in ids], fr


# adapted from Textualize/rich docs examples (MIT): Panel(Table) plan, then a default-Yes confirm
def confirm_plan(plan, found, remote, from_):
    import questionary
    t = Table.grid(padding=(0, 2))
    for rid, b, ba, path in plan:  # dry run of the real resolver (no fetch), so "←" is the ref git will actually use
        r = resolve_and_add(found[rid], path, b, ba, remote, False, True, None, from_)
        how = {"planned": f"[dim]←[/] {escape(r.get('start', ''))}", "exists": "[dim]exists[/]"}.get(r["status"], f"[red]{escape(r['error'] or '')}[/]")
        t.add_row(escape(rid), f"[cyan]{escape(r['branch'])}[/]", how)
    out.print(Panel(t, title="Plan", title_align="left", border_style="dim", expand=False))
    if not questionary.confirm(f"Create {len(plan)} worktrees?", default=True, qmark="◆", style=questionary.Style(STYLE)).ask():
        raise typer.Exit(2)  # No or Ctrl-C (None): nothing created


# adapted from errhythm/ccswap (MIT): install-method detection, 24h PyPI cache, daily notice, self-upgrade
PYPI_URL = "https://pypi.org/pypi/copse/json"
CACHE_TTL = 24 * 3600


def _cache_path():
    if os.environ.get("XDG_CACHE_HOME"):
        base = Path(os.environ["XDG_CACHE_HOME"])
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path.home() / ".cache"
    return base / "copse" / "update_check.json"


def _parse_version(v):
    return tuple(int(x) for x in v.split("."))


def _editable():
    try:
        return json.loads(distribution("copse").read_text("direct_url.json") or "{}")["dir_info"].get("editable", False)
    except Exception:
        return False


def _detect_install_method():
    """'uv', 'pipx', or None when we can't tell. An editable install is None: `uv tool upgrade` on it is a no-op."""
    if _editable():
        return None
    prefix = Path(sys.prefix)
    parts = tuple(p.lower() for p in prefix.parts)
    pairs = list(zip(parts, parts[1:]))
    if ("uv", "tools") in pairs:
        return "uv"
    if ("pipx", "venvs") in pairs:
        return "pipx"
    for env_var, name in (("UV_TOOL_DIR", "uv"), ("PIPX_HOME", "pipx")):  # override: trusted only if sys.prefix is under it
        root = os.environ.get(env_var)
        if root:
            try:
                if prefix.is_relative_to(Path(root)):
                    return name
            except (ValueError, OSError):
                pass
    return None


def _interactive():
    return sys.stdout.isatty() and sys.stderr.isatty()


def _latest_version():
    path = _cache_path()
    try:
        c = json.loads(path.read_text())
        if 0 <= time.time() - c["ts"] < CACHE_TTL:  # a future ts (clock skew) must not pin the cache forever
            return c["version"]
    except Exception:
        pass
    try:
        with urllib.request.urlopen(urllib.request.Request(PYPI_URL), timeout=2) as resp:
            latest = json.loads(resp.read().decode())["info"]["version"]
    except Exception:
        latest = None
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:  # cache failures too, so an offline box pays the 2s once a day; atomic so two copse runs never read a torn file
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"ts": time.time(), "version": latest}))
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)  # os.replace can fail on Windows while another copse reads the file
        except Exception:
            pass
    return latest


def update_notice(current=None):
    """The one-line notice when PyPI has a newer copse, else None. Never raises."""
    try:
        current = current or __version__
        latest = _latest_version()
        if not latest or _parse_version(latest) <= _parse_version(current):
            return None
        direct = {"uv": "uv tool upgrade copse", "pipx": "pipx upgrade copse"}.get(_detect_install_method() or "")
        if direct and sys.platform == "win32":  # `copse update` only prints there, so name the real command
            hint = f"Run `{direct}`."
        elif direct:
            hint = "Run `copse update`."
        else:
            hint = "Run `copse update` for upgrade instructions."
        return f"copse {latest} is available (you have {current}). {hint}"
    except Exception:
        return None


def maybe_notice(json_=False):
    """Print the notice to stderr. Silent for --json, non-TTY streams and COPSE_NO_UPDATE_CHECK=1: agents never see it or pay for the fetch."""
    try:
        if json_ or os.environ.get("COPSE_NO_UPDATE_CHECK") == "1" or not _interactive():
            return
        msg = update_notice()
        if msg:
            err.print(msg, style="dim", markup=False, soft_wrap=True)  # one line, never wrapped at the terminal width
    except Exception:
        pass


def notifies(fn):
    """Run the update notice after a human-output command finishes (exit 0 or 1, not a usage error)."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            fn(*a, **kw)
        except typer.Exit as e:
            if e.exit_code != 2:
                maybe_notice(kw.get("json_", False))
            raise
        maybe_notice(kw.get("json_", False))
    return wrapper


def run_self_upgrade():
    method = _detect_install_method()
    cmd = {"uv": ["uv", "tool", "upgrade", "copse"], "pipx": ["pipx", "upgrade", "copse"]}.get(method or "")
    if cmd is None:
        err.print("error: could not detect the install method (looked for a uv tool / pipx venv).\n"
                  f"  sys.prefix:     {sys.prefix}\n  sys.executable: {sys.executable}\n"
                  "Upgrade manually with one of:\n  uv tool upgrade copse\n  pipx upgrade copse\n"
                  f"  {sys.executable} -m pip install --upgrade copse\n"
                  "For an editable install (`-e .`), run `git pull` in the clone.", style="red", markup=False)
        return 1
    if sys.platform == "win32":  # the running copse.exe is locked, so an in-process upgrade fails to replace it
        out.print(f"To upgrade copse on Windows, run:\n  {' '.join(cmd)}", markup=False)
        return 1
    try:
        rc = subprocess.run(cmd, check=False).returncode
    except FileNotFoundError:
        err.print(f"error: detected a {method} install but `{cmd[0]}` is not on PATH. Run the upgrade from a shell where it is.", style="red", markup=False)
        return 1
    if rc == 0:
        try:
            subprocess.run([sys.executable, "-m", "copse", "--version"], check=False)  # fresh process from this install, not whatever `copse` is first on PATH
        except OSError:
            pass
    return rc


@app.command()
def update():
    """Upgrade copse to the latest release."""
    raise typer.Exit(run_self_upgrade())


app.command("upgrade", hidden=True)(update)


@app.command()
@notifies
def repos(json_: Json = False):
    """List detected repos."""
    found = scan(Path.cwd(), task_root(None))
    def render():
        rows = [(k, git("rev-parse", "--abbrev-ref", "HEAD", cwd=v)[1], short(v)) for k, v in found.items()]
        wp = max(12, out.width - max([4, *(len(r[0]) for r in rows)]) - max([6, *(len(r[1]) for r in rows)]) - 4)  # 4 = two gaps
        t = Table(box=None, pad_edge=False, header_style="dim")
        for c in ("repo", "branch", "path"):
            t.add_column(c, no_wrap=True)  # one row per repo: long paths lose their head ("…/tail"), never wrap
        for k, b, p in rows:
            t.add_row(escape(k), f"[cyan]{escape(b)}[/]", escape(p if len(p) <= wp else "…" + p[1 - wp:]))
        out.print(t)
    emit([{"repo": k, "path": str(v)} for k, v in found.items()], json_, render)


@app.command()
@notifies
def new(task: Task, repo: Repos = None, branch: Annotated[Optional[str], typer.Option("-b", "--branch", help="Branch for every repo without its own; default: task name")] = None,
        base: Annotated[Optional[str], typer.Option("--base", help="Base for new branches; default: <remote>/HEAD, else current branch")] = None,
        from_: Annotated[str, typer.Option("--from", help="Start new branches from remote|local base")] = "remote",
        remote: Annotated[str, typer.Option("--remote", help="Git remote to fetch and base on")] = "origin",
        no_fetch: Annotated[bool, typer.Option("--no-fetch", help="Skip fetching the remote first")] = False,
        no_include: Annotated[bool, typer.Option("--no-include", help="Don't copy .worktreeinclude files into new worktrees")] = False,
        no_setup: Annotated[bool, typer.Option("--no-setup", help="Don't run the setup hook (copse.setup / .copse/setup)")] = False,
        dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would happen; change nothing")] = False, root: Root = None, json_: Json = False):
    """Create worktrees for TASK, one per repo."""
    if from_ not in ("remote", "local"):
        fail("--from must be 'remote' or 'local'")
    troot, tdir = task_root(root), task_dir(root, task)
    found = scan(Path.cwd(), troot)
    interactive = False
    if not repo:
        if not is_tty():
            fail("no -r given and stdin is not a TTY; pass -r repo[:branch[:base]]")
        repo, from_ = pick_interactive(found, task, branch, base, from_)
        interactive = True
    plan = []
    for spec in repo:
        name, b, ba = (spec.split(":") + [None, None])[:3]
        hit = [k for k in found if k == name] or [k for k in found if k.rsplit("/", 1)[-1] == name]
        if spec.count(":") > 2:
            fail(f"bad spec '{spec}', expected repo[:branch[:base]]")
        if len(hit) != 1:
            fail(f"unknown or ambiguous repo '{name}' (see `copse repos`)")
        plan.append((hit[0], b or branch or task, ba or base or None))
    bases = [k.rsplit("/", 1)[-1] for k in found]  # over all repos, so a dir name never changes between runs
    plan = [(rid, b, ba, tdir / (rid.rsplit("/", 1)[-1] if bases.count(rid.rsplit("/", 1)[-1]) == 1 else rid.replace("/", "-")))
            for rid, b, ba in plan]
    if interactive and not json_:
        confirm_plan(plan, found, remote, from_)
    wr, wb = max(len(p[0]) for p in plan), max(len(p[1]) for p in plan)
    results = []
    for rid, b, ba, path in plan:
        if not dry_run:
            tdir.mkdir(parents=True, exist_ok=True)
        with out.status(f"{rid}...") if not json_ else nullcontext():  # spinner only on a TTY, never in --json
            r = resolve_and_add(found[rid], path, b, ba, remote, not no_fetch, dry_run, task, from_)
        r["repo"] = rid
        extra = ""
        if r["status"] == "created" and not dry_run:  # only fresh worktrees: never touch an existing one
            if not no_include:
                r["copied"], errs = copy_include(found[rid], path)
                r["include_errors"] = errs
                extra += f"  +{len(r['copied'])} copied" + (f", {len(errs)} failed" if errs else "")
            hook = None if no_setup else run_setup(found[rid], path, task, rid)
            if hook:
                r["setup"] = "ok" if hook[0] else "failed"
                if not hook[0]:
                    r["error"] = hook[1]
                extra += f"  setup {r['setup']}"
        results.append(r)
        if not json_:
            status_line(r, f"{r['branch']:{wb}}  {rel(r['path'])}{extra}", wr)
    emit({"task": task, "path": str(tdir), "results": results}, json_, lambda: None)
    raise typer.Exit(1 if any(r["status"] == "failed" or r.get("setup") == "failed" for r in results) else 0)


def worktrees(tdir: Path, scan_root: Path):
    for d in sorted(tdir.iterdir()):
        if d.is_dir() and (d / ".git").is_file():
            rc, common = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=d)
            main = Path(common).resolve().parent if rc == 0 else None
            try:  # resolve both: Windows 8.3 short names (RUNNER~1) vs git's long paths
                rid = main.relative_to(scan_root.resolve()).as_posix() if main else d.name  # posix: ids match scan() on Windows
            except ValueError:
                rid = main.name
            yield d, main, rid


def merged(d, branch):
    """True when the branch's work is in its base (the ref copse cut it from, else <remote>/HEAD): ancestor, or squash-merged. False for
    an untouched branch (its reflog is only "Created from <base>"); None without a base or on a detached HEAD."""
    if branch == "HEAD":
        return None
    rc, base = git("config", "--get", f"branch.{branch}.copseBase", cwd=d)
    if rc:
        remote = git("config", "--get", f"branch.{branch}.remote", cwd=d)[1] or "origin"
        rc, base = git("symbolic-ref", f"refs/remotes/{remote}/HEAD", cwd=d)
    if rc or git("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", cwd=d)[0]:
        return None
    # ponytail: a fresh branch (at or behind its base) looks merged; its reflog tells it apart. With core.logAllRefUpdates off it reads true.
    # No "HEAD == base tip" shortcut: a fast-forward merge lands there too.
    if git("reflog", "show", "--format=%gs", f"refs/heads/{branch}", cwd=d)[1] == f"branch: Created from {base}":
        return False
    if git("merge-base", "--is-ancestor", "HEAD", base, cwd=d)[0] == 0:
        return True
    # squash: one synthetic commit of the whole branch diff (dangling object, as git-delete-squashed does), then `git cherry` it
    rc, mb = git("merge-base", base, "HEAD", cwd=d)
    if rc == 0:
        rc, mb = git("-c", "user.name=copse", "-c", "user.email=copse@localhost", "commit-tree", "HEAD^{tree}", "-p", mb, "-m", "copse", cwd=d)
    return rc == 0 and git("cherry", base, mb, cwd=d)[1].startswith("-")


def info(d, main, rid):
    w = {"repo": rid, "path": str(d), "branch": git("rev-parse", "--abbrev-ref", "HEAD", cwd=d)[1],
         "dirty": bool(git("status", "--porcelain", cwd=d)[1]), "ahead": None, "behind": None}
    w["merged"] = merged(d, w["branch"])
    # adapted from nesquena/hermes-webui (MIT): HEAD...@{u} left/right counts
    rc, out = git("rev-list", "--left-right", "--count", "HEAD...@{u}", cwd=d)
    if rc == 0:
        w["ahead"], w["behind"] = map(int, out.split())
    return w


@app.command()
@notifies
def ls(task: Optional[str] = typer.Argument(None), root: Root = None, json_: Json = False):
    """Show worktrees per task: branch, dirty, ahead/behind, merged."""
    troot = task_root(root)
    if task and not task_dir(root, task).is_dir():
        fail(f"no such task '{task}'")
    dirs = [troot / task] if task else (sorted(p for p in troot.iterdir() if p.is_dir()) if troot.is_dir() else [])
    tasks = []
    for t in dirs:
        ws = [info(*w) for w in worktrees(t, Path.cwd())]
        tasks.append({"task": t.name, "path": str(t), "worktrees": ws})
    def render():
        for t in tasks:
            tb = Table(title=t["task"], title_style="bold", title_justify="left", box=None, pad_edge=False, header_style="dim")
            for c in ("repo", "branch", "state", "sync"):
                tb.add_column(c)
            for w in t["worktrees"]:
                sync = " ".join(x for x in (w["ahead"] and f"↑{w['ahead']}", w["behind"] and f"↓{w['behind']}") if x) or "[dim]-[/]"
                sync += " [magenta]merged[/]" if w["merged"] else ""
                tb.add_row(escape(w["repo"]), f"[cyan]{escape(w['branch'])}[/]", "[yellow]●[/] dirty" if w["dirty"] else "[green]✔[/] clean", sync)
            out.print(tb)
    emit({"tasks": tasks}, json_, render)


@app.command()
@notifies
def rm(task: Task, repo: RmRepos = None,
       force: Annotated[bool, typer.Option("--force", help="Remove even with uncommitted changes")] = False,
       delete_branch: Annotated[bool, typer.Option("--delete-branch", help="Also delete the task branch")] = False,
       root: Root = None, json_: Json = False):
    """Remove worktrees of TASK (all, or only -r repos)."""
    tdir = task_dir(root, task)
    if not tdir.is_dir():
        fail(f"no such task '{task}'")
    results = []
    for d, main, rid in list(worktrees(tdir, Path.cwd())):
        if repo and rid not in repo and d.name not in repo:
            continue
        r = {"repo": rid, "path": str(d), "branch": git("rev-parse", "--abbrev-ref", "HEAD", cwd=d)[1], "status": "removed", "error": None}
        rc, out = git("worktree", "remove", *(["--force"] if force else []), str(d), cwd=main)
        if rc == 0 and delete_branch:
            owner = git("config", "--get", f"branch.{r['branch']}.copseTask", cwd=main)[1]
            if owner != task:  # not created by this task (attached existing branch): keep it
                r["branch_kept"] = "not created by copse for this task"
            else:
                rc, out = git("branch", "-D" if force else "-d", r["branch"], cwd=main)  # -d keeps unmerged commits unless --force
        if rc:
            r.update(status="failed", error=out)
        results.append(r)
    if not any(tdir.iterdir()):
        tdir.rmdir()
    def render():
        wr = max((len(r["repo"]) for r in results), default=0)
        for r in results:
            status_line(r, r["branch"] + (f"  (branch kept: {r['branch_kept']})" if r.get("branch_kept") else ""))
    emit({"task": task, "path": str(tdir), "results": results}, json_, render)
    raise typer.Exit(1 if any(r["status"] == "failed" for r in results) else 0)


if __name__ == "__main__":
    app()
