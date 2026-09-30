"""End-to-end: the real entry point (`python -m copse`) as a subprocess against throwaway repos with a bare origin."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(*args, cwd, out=False):
    r = subprocess.run(["git", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True, capture_output=True, text=True,
                       env={**os.environ, **GIT_ENV})
    return r.stdout.strip() if out else None


def copse(cwd, *args, env=None, stdin=""):
    base = {k: v for k, v in os.environ.items() if k != "COPSE_ROOT"}
    e = {**base, **GIT_ENV, "PYTHONPATH": str(HERE), "NO_COLOR": "1", **(env or {})}
    return subprocess.run([sys.executable, "-m", "copse", *args], cwd=cwd, input=stdin, capture_output=True, text=True, env=e)


def cj(cwd, *args, code=0, **kw):
    r = copse(cwd, *args, "--json", **kw)
    assert r.returncode == code, r.stderr
    assert "\x1b" not in r.stdout
    return json.loads(r.stdout)


def branches(repo):
    return git("branch", "--format=%(refname:short)", cwd=repo, out=True).split()


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / "libs").mkdir(parents=True)
    for name, where in [("api", "api"), ("web", "web"), ("auth", "libs/auth")]:
        bare, seed = tmp_path / f"{name}.git", tmp_path / f"seed-{name}"
        git("init", "-q", "--bare", str(bare), cwd=tmp_path)
        git("init", "-q", str(seed), cwd=tmp_path)
        git("commit", "-q", "--allow-empty", "-m", "init", cwd=seed)
        git("remote", "add", "origin", str(bare), cwd=seed)
        git("push", "-q", "origin", "main", cwd=seed)
        git("push", "-q", "origin", "main:team/x", cwd=seed)  # remote-only branch
        git("clone", "-q", str(bare), str(root / where), cwd=tmp_path)
    git("commit", "-q", "--allow-empty", "-m", "local", cwd=root / "api")  # local main ahead of origin/main
    git("branch", "mine", cwd=root / "web")  # pre-existing local branch to attach
    return root


def test_session(ws, tmp_path):
    repos = cj(ws, "repos")
    assert sorted(r["repo"] for r in repos) == ["api", "libs/auth", "web"]

    # api: new branch from local main; web: attach existing branch; libs/auth: remote-only branch gets tracked
    new = cj(ws, "new", "200", "--from", "local", "-r", "api:feat/200:main", "-r", "web:mine", "-r", "libs/auth:team/x")
    assert {r["repo"]: r["status"] for r in new["results"]} == {"api": "created", "web": "created", "libs/auth": "created"}
    tdir = Path(new["path"])
    assert tdir == (ws / "tasks" / "200").resolve()
    api, web, auth = tdir / "api", tdir / "web", tdir / "auth"
    assert git("rev-parse", "HEAD", cwd=api, out=True) == git("rev-parse", "main", cwd=ws / "api", out=True) != \
        git("rev-parse", "origin/main", cwd=ws / "api", out=True)
    assert git("rev-parse", "--abbrev-ref", "@{u}", cwd=auth, out=True) == "origin/team/x"

    ls = cj(ws, "ls", "200")
    w = {x["repo"]: x for x in ls["tasks"][0]["worktrees"]}
    assert set(w) == {"api", "web", "libs/auth"} and not any(x["dirty"] for x in w.values())
    assert w["api"]["branch"] == "feat/200" and w["web"]["branch"] == "mine"

    (api / "scratch.txt").write_text("x")
    assert {x["repo"]: x["dirty"] for x in cj(ws, "ls", "200")["tasks"][0]["worktrees"]}["api"] is True
    assert "dirty" in copse(ws, "ls", "200").stdout
    r = copse(ws, "ls", "200", env={"PYTHONIOENCODING": "cp1252"})  # Windows pipe encoding: ● must not raise
    assert r.returncode == 0 and "dirty" in r.stdout, r.stderr
    (api / "scratch.txt").unlink()

    # web: commit -> ahead 1 after push -u sets upstream; then someone else pushes to auth -> behind 1
    git("push", "-q", "-u", "origin", "mine", cwd=web)
    git("commit", "-q", "--allow-empty", "-m", "work", cwd=web)
    ws_ = {x["repo"]: x for x in cj(ws, "ls", "200")["tasks"][0]["worktrees"]}
    assert (ws_["web"]["ahead"], ws_["web"]["behind"]) == (1, 0)
    git("push", "-q", cwd=web)
    assert {x["repo"]: x for x in cj(ws, "ls", "200")["tasks"][0]["worktrees"]}["web"]["ahead"] == 0
    other = tmp_path / "other"
    git("clone", "-q", "-b", "team/x", str(tmp_path / "auth.git"), str(other), cwd=tmp_path)
    git("commit", "-q", "--allow-empty", "-m", "theirs", cwd=other)
    git("push", "-q", cwd=other)
    git("fetch", "-q", cwd=auth)
    ws_ = {x["repo"]: x for x in cj(ws, "ls", "200")["tasks"][0]["worktrees"]}
    assert (ws_["libs/auth"]["ahead"], ws_["libs/auth"]["behind"]) == (0, 1)

    again = cj(ws, "new", "200", "--from", "local", "-r", "api:feat/200:main", "-r", "web:mine", "-r", "libs/auth:team/x")
    assert [r["status"] for r in again["results"]] == ["exists"] * 3

    r = copse(ws, "new", "201")  # piped stdin, no -r
    assert r.returncode == 2 and "error:" in r.stderr and not (ws / "tasks" / "201").exists()

    rm = cj(ws, "rm", "200", "--delete-branch")
    assert {x["repo"]: x["status"] for x in rm["results"]} == {"api": "removed", "libs/auth": "removed", "web": "removed"}
    assert [x["repo"] for x in rm["results"] if x.get("branch_kept")] == ["web"]
    assert "feat/200" not in branches(ws / "api") and "team/x" not in branches(ws / "libs" / "auth")
    assert "mine" in branches(ws / "web")
    assert not tdir.exists()


def test_copse_root_env(ws, tmp_path):
    custom = tmp_path / "custom-root"
    env = {"COPSE_ROOT": str(custom)}
    new = cj(ws, "new", "300", "-r", "api", env=env)
    assert Path(new["path"]) == custom.resolve() / "300" and (custom / "300" / "api" / ".git").is_file()
    assert not (ws / "tasks").exists()
    assert [t["task"] for t in cj(ws, "ls", env=env)["tasks"]] == ["300"]
    assert cj(ws, "ls")["tasks"] == []  # default root untouched
    cj(ws, "rm", "300", env=env)
    assert not (custom / "300").exists()
