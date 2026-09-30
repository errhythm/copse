"""copse: task worktrees across git repos."""
import json
import os
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

for _s in (sys.stdout, sys.stderr):  # a cp1252 pipe (Windows CI, redirects) must print "?" for ✔●◇, not raise UnicodeEncodeError
    if _s and "utf" not in (getattr(_s, "encoding", "") or "").lower() and hasattr(_s, "reconfigure"):
        _s.reconfigure(errors="replace")

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)
Root = Annotated[Optional[Path], typer.Option("--root", envvar="COPSE_ROOT", help="Task root (default ./tasks)")]
Json = Annotated[bool, typer.Option("--json", help="Machine-readable output")]
Repos = Annotated[Optional[list[str]], typer.Option("-r", "--repo", help="repo[:branch[:base]] (repeatable)")]


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


out, err = Console(highlight=False), Console(stderr=True, highlight=False)  # no force_terminal: rich drops ANSI when piped / NO_COLOR
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
        cmd = ["worktree", "add", "-b", branch, str(path), ref]
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
    return r


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


@app.command()
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
def new(task: str, repo: Repos = None, branch: Annotated[Optional[str], typer.Option("-b", "--branch")] = None,
        base: Annotated[Optional[str], typer.Option("--base")] = None,
        from_: Annotated[str, typer.Option("--from", help="Start new branches from remote|local base")] = "remote",
        remote: str = "origin", no_fetch: bool = False, dry_run: bool = False, root: Root = None, json_: Json = False):
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
        results.append(r)
        if not json_:
            status_line(r, f"{r['branch']:{wb}}  {rel(r['path'])}", wr)
    emit({"task": task, "path": str(tdir), "results": results}, json_, lambda: None)
    raise typer.Exit(1 if any(r["status"] == "failed" for r in results) else 0)


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


def info(d, main, rid):
    w = {"repo": rid, "path": str(d), "branch": git("rev-parse", "--abbrev-ref", "HEAD", cwd=d)[1],
         "dirty": bool(git("status", "--porcelain", cwd=d)[1]), "ahead": None, "behind": None}
    # adapted from nesquena/hermes-webui (MIT): HEAD...@{u} left/right counts
    rc, out = git("rev-list", "--left-right", "--count", "HEAD...@{u}", cwd=d)
    if rc == 0:
        w["ahead"], w["behind"] = map(int, out.split())
    return w


@app.command()
def ls(task: Optional[str] = typer.Argument(None), root: Root = None, json_: Json = False):
    """Show worktrees per task: branch, dirty, ahead/behind."""
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
                tb.add_row(escape(w["repo"]), f"[cyan]{escape(w['branch'])}[/]", "[yellow]●[/] dirty" if w["dirty"] else "[green]✔[/] clean", sync)
            out.print(tb)
    emit({"tasks": tasks}, json_, render)


@app.command()
def rm(task: str, repo: Repos = None, force: bool = False, delete_branch: bool = False, root: Root = None, json_: Json = False):
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
