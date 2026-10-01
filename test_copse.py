import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

import copse
from copse import app

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "init.defaultBranch", "GIT_CONFIG_VALUE_0": "main"}


@pytest.fixture(autouse=True)
def no_update_check(monkeypatch, tmp_path):  # the suite must never reach PyPI or write the real ~/.cache
    monkeypatch.setenv("COPSE_NO_UPDATE_CHECK", "1")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(copse.urllib.request, "urlopen", lambda *a, **k: pytest.fail("PyPI call in tests"))


def sh(*a, cwd):
    subprocess.run(a, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    root = tmp_path / "ws"
    root.mkdir()
    for name, where in [("api", "api"), ("web", "web"), ("auth", "libs/auth")]:
        bare = tmp_path / f"{name}.git"
        seed = tmp_path / f"seed-{name}"
        sh("git", "init", "-q", "--bare", str(bare), cwd=tmp_path)
        sh("git", "init", "-q", str(seed), cwd=tmp_path)
        sh("git", "commit", "-q", "--allow-empty", "-m", "init", cwd=seed)
        sh("git", "remote", "add", "origin", str(bare), cwd=seed)
        sh("git", "push", "-q", "origin", "main", cwd=seed)
        sh("git", "branch", "develop", cwd=seed)
        sh("git", "push", "-q", "origin", "develop", cwd=seed)
        sh("git", "branch", "team/x", cwd=seed)
        sh("git", "push", "-q", "origin", "team/x", cwd=seed)
        (root / where).parent.mkdir(exist_ok=True)
        sh("git", "clone", "-q", str(bare), str(root / where), cwd=tmp_path)
    (root / "web" / "node_modules" / "junk").mkdir(parents=True)
    sh("git", "init", "-q", str(root / "web" / "node_modules" / "junk"), cwd=root)
    monkeypatch.chdir(root)
    return root


def run(*args):
    r = CliRunner().invoke(app, list(args))
    return r


def js(*args):
    r = run(*args, "--json")
    return r.exit_code, json.loads(r.stdout)


def branches(repo):
    return subprocess.run(["git", "branch", "--format=%(refname:short)"], cwd=repo, capture_output=True, text=True).stdout.split()


def test_scan_depth_and_worktree_skip(ws):
    code, out = js("repos")
    assert sorted(r["repo"] for r in out) == ["api", "libs/auth", "web"]
    assert run("new", "1", "-r", "api").exit_code == 0
    assert sorted(r["repo"] for r in js("repos")[1]) == ["api", "libs/auth", "web"]  # tasks/ skipped


def test_new_from_remote_base_and_json_shape(ws):
    code, out = js("new", "200", "-r", "api", "-r", "web:feat/w:develop")
    assert code == 0 and set(out) == {"task", "path", "results"}
    for r in out["results"]:
        assert {"repo", "path", "branch", "base", "status", "error"} <= set(r) and r["status"] == "created"
    assert out["results"][0]["branch"] == "200" and out["results"][0]["base"] == "main"
    assert out["results"][1]["branch"] == "feat/w" and out["results"][1]["base"] == "develop"
    assert (ws / "tasks/200/api/.git").is_file()


def test_local_branch_exists(ws):
    sh("git", "branch", "mine", cwd=ws / "api")
    code, out = js("new", "1", "-r", "api:mine")
    assert code == 0 and out["results"][0]["status"] == "created"
    head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ws / "tasks/1/api", capture_output=True, text=True).stdout.strip()
    assert head == "mine"


def test_remote_only_branch_tracked(ws):
    code, out = js("new", "1", "-r", "api:team/x")
    assert code == 0
    wt = ws / "tasks/1/api"
    up = subprocess.run(["git", "rev-parse", "--abbrev-ref", "@{u}"], cwd=wt, capture_output=True, text=True).stdout.strip()
    assert up == "origin/team/x"
    assert cfg(ws / "api", "branch.team/x.merge") == "refs/heads/team/x"


def test_global_flags_and_spec_override(ws):
    code, out = js("new", "1", "-b", "gb", "--base", "develop", "-r", "api", "-r", "web:sb:main")
    a, w = out["results"]
    assert (a["branch"], a["base"]) == ("gb", "develop") and (w["branch"], w["base"]) == ("sb", "main")


def test_dry_run_touches_nothing(ws):
    code, out = js("new", "9", "-r", "api:f:develop", "--dry-run")
    r = out["results"][0]
    assert code == 0 and r["status"] == "planned" and r["start"] == "origin/develop"
    assert not (ws / "tasks").exists() and "f" not in branches(ws / "api")


def test_idempotent_rerun(ws):
    run("new", "1", "-r", "api")
    code, out = js("new", "1", "-r", "api", "-r", "web")
    assert code == 0 and [r["status"] for r in out["results"]] == ["exists", "created"]


def test_one_failure_others_created(ws):
    run("new", "1", "-r", "api:shared")
    code, out = js("new", "2", "-r", "api:shared", "-r", "web")  # branch already checked out elsewhere
    st = {r["repo"]: r["status"] for r in out["results"]}
    assert code == 1 and st == {"api": "failed", "web": "created"} and out["results"][0]["error"]


def test_non_tty_without_repo_exits_2(ws):
    r = run("new", "1")
    assert r.exit_code == 2 and not (ws / "tasks").exists()


def test_ls(ws):
    run("new", "1", "-r", "api", "-r", "libs/auth")
    (ws / "tasks/1/api/f.txt").write_text("x")
    code, out = js("ls", "1")
    ws_ = {w["repo"]: w for w in out["tasks"][0]["worktrees"]}
    assert set(ws_) == {"api", "libs/auth"} and ws_["api"]["dirty"] and not ws_["libs/auth"]["dirty"]
    assert ws_["api"]["branch"] == "1"


def test_rm_cleanup(ws):
    run("new", "1", "-r", "api", "-r", "web")
    assert run("rm", "1", "-r", "api", "--delete-branch").exit_code == 0
    assert "1" not in branches(ws / "api") and (ws / "tasks/1/web").exists()
    (ws / "tasks/1/web/f.txt").write_text("x")
    assert run("rm", "1").exit_code == 1  # untracked file blocks removal without --force
    assert run("rm", "1", "--force").exit_code == 0
    assert not (ws / "tasks/1").exists()


def test_exists_reports_real_branch(ws):
    run("new", "200", "-r", "api:feat/200:main")
    code, out = js("new", "200", "-r", "api")
    assert code == 0 and out["results"][0]["status"] == "exists" and out["results"][0]["branch"] == "feat/200"


def test_basename_collision_stable_and_no_remote(ws):
    other = ws / "other" / "auth"  # second "auth", local only: no remote, fetch fails
    sh("git", "init", "-q", str(other), cwd=ws)
    sh("git", "commit", "-q", "--allow-empty", "-m", "init", cwd=other)
    code, out = js("new", "1", "-r", "other/auth")
    assert code == 0 and out["results"][0]["path"].endswith("other-auth") and out["results"][0]["base"] == "main"
    code, out = js("new", "1", "-r", "libs/auth")  # must not land in (or report "exists" for) other/auth's dir
    assert code == 0 and out["results"][0]["status"] == "created" and out["results"][0]["path"].endswith("libs-auth")


def test_exists_but_other_repo_fails(ws):
    run("new", "1", "-r", "api")
    (ws / "tasks/1/api").rename(ws / "tasks/1/web")  # web's dir now holds api's worktree
    code, out = js("new", "1", "-r", "web")
    assert code == 1 and out["results"][0]["status"] == "failed"


def test_task_name_cannot_escape_root(ws):
    for bad in ("..", "/tmp", "a/b"):
        assert run("rm", bad).exit_code == 2 and run("new", bad, "-r", "api").exit_code == 2
    assert not (ws / "tasks").exists()


def test_bad_spec_exits_2(ws):
    assert run("new", "1", "-r", "api:a:b:c").exit_code == 2


def test_git_missing_is_failure_not_traceback(monkeypatch):
    import copse
    def no_git(*a, **k):  # PATH="" is not enough on Windows: CreateProcess also searches cwd and system dirs
        raise FileNotFoundError("git")
    monkeypatch.setattr(copse.subprocess, "run", no_git)
    assert copse.git("status")[0] == 127


def test_interactive_ctrl_c_exits(monkeypatch):
    import questionary
    import typer
    import copse
    ans = iter([["api"], None])
    monkeypatch.setattr(questionary, "checkbox", lambda *a, **k: type("Q", (), {"ask": lambda s: next(ans)})())
    monkeypatch.setattr(questionary, "text", lambda *a, **k: type("Q", (), {"ask": lambda s: next(ans, "")})())
    with pytest.raises(typer.Exit):
        copse.pick_interactive({"api": None}, "1", None, None)
    with pytest.raises(typer.Exit):
        copse.pick_interactive({}, "1", None, None)


def test_rm_delete_branch_keeps_unmerged(ws):
    run("new", "1", "-r", "api")
    sh("git", "commit", "-q", "--allow-empty", "-m", "work", cwd=ws / "tasks/1/api")
    assert run("rm", "1", "--delete-branch").exit_code == 1
    assert "1" in branches(ws / "api") and not (ws / "tasks/1").exists()


def rev(repo, ref):
    return subprocess.run(["git", "rev-parse", ref], cwd=repo, capture_output=True, text=True).stdout.strip()


def local_ahead(ws):
    sh("git", "commit", "-q", "--allow-empty", "-m", "local", cwd=ws / "api")  # local main ahead of origin/main


def test_rm_delete_branch_only_task_created(ws):
    sh("git", "branch", "mine", cwd=ws / "web")
    run("new", "1", "-r", "api", "-r", "web:mine")
    code, out = js("rm", "1", "--delete-branch")
    assert code == 0 and "1" not in branches(ws / "api")
    assert "mine" in branches(ws / "web")
    kept = {r["repo"]: r.get("branch_kept") for r in out["results"]}
    assert kept["api"] is None and kept["web"]


def test_from_local_vs_remote_default(ws):
    local_ahead(ws)
    code, out = js("new", "1", "-r", "api", "--from", "local")
    assert code == 0
    assert rev(ws / "api", "1") == rev(ws / "api", "main") != rev(ws / "api", "origin/main")
    assert run("new", "2", "-r", "api").exit_code == 0
    assert rev(ws / "api", "2") == rev(ws / "api", "origin/main")


def test_from_local_diverged_dry_run_start(ws):
    local_ahead(ws)
    assert js("new", "1", "-r", "api", "--from", "local", "--dry-run")[1]["results"][0]["start"] == "main"
    assert js("new", "1", "-r", "api", "--dry-run")[1]["results"][0]["start"] == "origin/main"


def test_from_local_per_repo_origin_override(ws):
    local_ahead(ws)
    sh("git", "commit", "-q", "--allow-empty", "-m", "local", cwd=ws / "web")
    code, out = js("new", "1", "--from", "local", "-r", "api:x:origin/main", "-r", "web")
    assert code == 0
    assert rev(ws / "api", "x") == rev(ws / "api", "origin/main")
    assert rev(ws / "web", "1") == rev(ws / "web", "main") != rev(ws / "web", "origin/main")


def test_missing_start_ref_fails_that_repo_only(ws):
    code, out = js("new", "1", "--from", "local", "-r", "api:x:nope", "-r", "web")
    st = {r["repo"]: r["status"] for r in out["results"]}
    assert code == 1 and st == {"api": "failed", "web": "created"} and "nope" in out["results"][0]["error"]
    assert "x" not in branches(ws / "api")


def test_start_ref_not_shadowed_by_local_branch_or_tag(ws):
    local_ahead(ws)
    sh("git", "branch", "origin/main", "main", cwd=ws / "api")  # local branch literally named origin/main
    sh("git", "tag", "main", "origin/main", cwd=ws / "api")  # tag shadowing local main
    code, out = js("new", "1", "-r", "api")
    assert code == 0 and out["results"][0]["base"] == "main"
    assert rev(ws / "api", "1") == rev(ws / "api", "refs/remotes/origin/main")
    assert run("new", "2", "-r", "api:y:origin/main").exit_code == 0
    assert rev(ws / "api", "y") == rev(ws / "api", "refs/remotes/origin/main")
    assert run("new", "3", "-r", "api", "--from", "local").exit_code == 0
    assert rev(ws / "api", "3") == rev(ws / "api", "refs/heads/main")
    sh("git", "branch", "origin/team/x", "refs/heads/main", cwd=ws / "api")
    assert run("new", "4", "-r", "api:team/x").exit_code == 0  # remote-only branch still tracked
    assert rev(ws / "api", "team/x") == rev(ws / "api", "refs/remotes/origin/team/x")


def test_json_has_no_ansi(ws):
    for args in (("repos",), ("new", "1", "-r", "api"), ("ls",), ("rm", "1")):
        r = run(*args, "--json")
        json.loads(r.stdout)
        assert "\x1b" not in r.stdout


def test_human_output_non_tty_has_no_ansi(ws):
    run("new", "1", "-r", "api")
    (ws / "tasks/1/api/f.txt").write_text("x")
    for args in (("repos",), ("ls",), ("new", "2", "-r", "web"), ("rm", "2")):
        r = run(*args)
        assert r.exit_code == 0 and "\x1b" not in r.output and r.output.strip()
    assert "●" in run("ls").output and "error:" in run("ls", "nope").output


def fake_prompts(monkeypatch, picks, confirm):
    import questionary
    seen = {}
    def q(name, ret):
        def f(*a, **k):
            seen[name] = k
            return type("Q", (), {"ask": lambda s: ret(k) if callable(ret) else ret})()
        monkeypatch.setattr(questionary, name, f)
    q("checkbox", picks)
    q("text", lambda k: k["default"])
    q("select", lambda k: k["default"])
    q("confirm", confirm)
    return seen


def test_interactive_yes_creates_and_from_default(ws, monkeypatch):
    import copse
    seen = fake_prompts(monkeypatch, ["api"], True)
    monkeypatch.setattr(copse, "is_tty", lambda: True)
    r = run("new", "5", "--from", "local")
    assert r.exit_code == 0 and (ws / "tasks/5/api/.git").is_file()
    assert seen["select"]["default"] == "local" and seen["text"]["default"] == "" and "Plan" in r.output


def test_interactive_no_exits_2_creates_nothing(ws, monkeypatch):
    import copse
    fake_prompts(monkeypatch, ["api"], False)
    monkeypatch.setattr(copse, "is_tty", lambda: True)
    assert run("new", "5").exit_code == 2 and not (ws / "tasks").exists()


def test_result_lines_aligned_across_batch(ws):
    lines = run("new", "1", "-r", "api:feat/200", "-r", "libs/auth:1").output.splitlines()
    assert len(lines) == 2 and len({l.index("tasks") for l in lines}) == 1
    assert lines[0].index("feat/200") == lines[1].index("1  ")


def test_repos_table_one_line_per_repo(ws, monkeypatch):
    import copse
    from rich.console import Console
    monkeypatch.setattr(copse, "out", Console(width=40, highlight=False))
    lines = run("repos").output.strip().splitlines()
    assert len(lines) == 4 and "libs/auth" in lines[2] and "main" in lines[2]  # header + 3 rows; branch column kept
    assert lines[2].rstrip().endswith(str(Path("libs", "auth"))) and "…" in lines[2]  # path shortened from the left, tail kept


def test_plan_shows_real_start_ref(ws, monkeypatch):
    import copse
    other = ws / "solo"  # no remote: new branch starts at local main, not origin/main
    sh("git", "init", "-q", str(other), cwd=ws)
    sh("git", "commit", "-q", "--allow-empty", "-m", "init", cwd=other)
    fake_prompts(monkeypatch, ["api", "solo"], False)
    monkeypatch.setattr(copse, "is_tty", lambda: True)
    out = run("new", "5").output
    assert "← origin/main" in [l for l in out.splitlines() if "api" in l][-1]
    solo = [l for l in out.splitlines() if "solo" in l][-1]
    assert "← main" in solo and "origin" not in solo


def test_ctrl_c_at_branch_stops_before_next_prompt(monkeypatch):
    import questionary
    import typer
    import copse
    asked = []
    monkeypatch.setattr(questionary, "checkbox", lambda *a, **k: type("Q", (), {"ask": lambda s: ["api"]})())
    monkeypatch.setattr(questionary, "text", lambda m, **k: asked.append(m) or type("Q", (), {"ask": lambda s: None})())
    with pytest.raises(typer.Exit):
        copse.pick_interactive({"api": None}, "1", None, None)
    assert asked == ["Branch"]


def test_help_clean_and_version(monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    for cmd in ("new", "rm"):
        out = CliRunner().invoke(app, [cmd, "--help"]).output
        assert out and not any(b in out for b in ("--no-no-fetch", "--no-dry-run", "--no-json", "{task}"))
    r = CliRunner().invoke(app, ["--version"])
    assert r.exit_code == 0 and r.output.startswith("copse ")


def test_error_text_not_emojified(tmp_path, monkeypatch):
    # rich would turn ":b:" into an emoji without emoji=False
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(app, ["new", "1", "-r", "a:b:c:d"])
    assert r.exit_code == 2 and "a:b:c:d" in r.output


def cfg(repo, key):
    return subprocess.run(["git", "config", "--get", key], cwd=repo, capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize("frm", ["remote", "local"])
def test_new_branch_upstream_is_same_name(ws, frm):
    sh("git", "config", "push.default", "simple", cwd=ws / "api")
    code, out = js("new", "1", "-b", "feature/pro-1", "--from", frm, "-r", "api")
    wt = ws / "tasks/1/api"
    assert code == 0 and out["results"][0]["status"] == "created"
    assert cfg(ws / "api", "branch.feature/pro-1.merge") == "refs/heads/feature/pro-1"
    assert cfg(ws / "api", "branch.feature/pro-1.remote") == "origin"
    w = js("ls", "1")[1]["tasks"][0]["worktrees"][0]
    assert (w["ahead"], w["behind"]) == (None, None)  # not pushed yet
    sh("git", "push", cwd=wt)
    sh("git", "rev-parse", "--verify", "origin/feature/pro-1", cwd=ws / "api")
    w = js("ls", "1")[1]["tasks"][0]["worktrees"][0]
    assert (w["ahead"], w["behind"]) == (0, 0)


def test_no_remote_no_upstream(ws):
    sh("git", "remote", "remove", "origin", cwd=ws / "api")
    code, out = js("new", "1", "-b", "nr", "--base", "main", "--from", "local", "-r", "api")
    assert code == 0 and out["results"][0]["status"] == "created"
    assert cfg(ws / "api", "branch.nr.merge") == "" and cfg(ws / "api", "branch.nr.remote") == ""


def test_attached_branch_config_untouched(ws):
    sh("git", "branch", "mine", cwd=ws / "api")
    js("new", "1", "-r", "api:mine")
    assert cfg(ws / "api", "branch.mine.merge") == "" and cfg(ws / "api", "branch.mine.remote") == ""


# ---- self-update ----

@pytest.fixture
def upd(tmp_path, monkeypatch):
    """Update-check sandbox: temp cache, fake TTY, stubbed PyPI. Returns a dict with the PyPI version and call count."""
    st = {"latest": "9.9.9", "calls": 0}
    monkeypatch.delenv("COPSE_NO_UPDATE_CHECK")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(copse, "__version__", "0.4.0")
    monkeypatch.setattr(copse, "_interactive", lambda: True)
    monkeypatch.setattr(copse, "_detect_install_method", lambda: "uv")
    monkeypatch.setattr(sys, "platform", "linux")

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return json.dumps({"info": {"version": st["latest"]}}).encode()

    def fake_urlopen(req, timeout=None):
        st["calls"] += 1
        assert timeout == 2
        if st["latest"] is None:
            raise OSError("offline")
        return Resp()

    monkeypatch.setattr(copse.urllib.request, "urlopen", fake_urlopen)
    return st


def set_prefix(monkeypatch, prefix):
    monkeypatch.setattr(sys, "prefix", prefix)
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    monkeypatch.delenv("PIPX_HOME", raising=False)


def test_detect_uv_pipx_unknown(monkeypatch):
    monkeypatch.setattr(copse, "_editable", lambda: False)
    set_prefix(monkeypatch, "/home/u/.local/share/uv/tools/copse")
    assert copse._detect_install_method() == "uv"
    set_prefix(monkeypatch, "/home/u/.local/pipx/venvs/copse")
    assert copse._detect_install_method() == "pipx"
    set_prefix(monkeypatch, "/home/u/proj/.venv")
    assert copse._detect_install_method() is None
    monkeypatch.setenv("UV_TOOL_DIR", "/opt/tools")
    monkeypatch.setattr(sys, "prefix", "/opt/tools/copse")
    assert copse._detect_install_method() == "uv"
    monkeypatch.delenv("UV_TOOL_DIR")
    monkeypatch.setenv("PIPX_HOME", "/opt/pipx")
    monkeypatch.setattr(sys, "prefix", "/opt/pipx/venvs/x")
    assert copse._detect_install_method() == "pipx"
    monkeypatch.setattr(sys, "prefix", "/elsewhere")
    assert copse._detect_install_method() is None


@pytest.mark.parametrize("method,cmd", [("uv", ["uv", "tool", "upgrade", "copse"]), ("pipx", ["pipx", "upgrade", "copse"])])
def test_update_runs_cmd_and_returns_rc(monkeypatch, method, cmd):
    calls = []
    monkeypatch.setattr(copse, "_detect_install_method", lambda: method)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(copse.subprocess, "run", lambda c, **kw: calls.append(c) or subprocess.CompletedProcess(c, 3))
    r = run("update")
    assert r.exit_code == 3
    assert calls == [cmd]  # rc != 0: no --version follow-up


def test_update_success_prints_new_version_and_upgrade_alias(monkeypatch):
    calls = []
    monkeypatch.setattr(copse, "_detect_install_method", lambda: "uv")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(copse.subprocess, "run", lambda c, **kw: calls.append(c) or subprocess.CompletedProcess(c, 0))
    assert run("upgrade").exit_code == 0
    assert calls == [["uv", "tool", "upgrade", "copse"], [sys.executable, "-m", "copse", "--version"]]  # not a PATH lookup
    assert "upgrade" not in run("--help").stdout


def test_update_windows_prints_only(monkeypatch):
    monkeypatch.setattr(copse, "_detect_install_method", lambda: "pipx")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(copse.subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    r = run("update")
    assert r.exit_code == 1
    assert "pipx upgrade copse" in r.output


def test_update_unknown_method(monkeypatch):
    monkeypatch.setattr(copse, "_detect_install_method", lambda: None)
    monkeypatch.setattr(copse.subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    r = run("update")
    assert r.exit_code == 1
    for s in ("uv tool upgrade copse", "pipx upgrade copse", "pip install --upgrade copse", "git pull"):
        assert s in r.output


def test_update_tool_not_on_path(monkeypatch):
    monkeypatch.setattr(copse, "_detect_install_method", lambda: "uv")
    monkeypatch.setattr(sys, "platform", "linux")
    def boom(*a, **k): raise FileNotFoundError
    monkeypatch.setattr(copse.subprocess, "run", boom)
    r = run("update")
    assert r.exit_code == 1 and "not on PATH" in r.output


def test_notice_shown_when_newer_on_tty(upd, ws):
    r = run("repos")
    assert r.exit_code == 0
    assert "copse 9.9.9 is available (you have 0.4.0). Run `copse update`." in r.stderr


def test_notice_hints(upd, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert "Run `uv tool upgrade copse`." in copse.update_notice()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(copse, "_detect_install_method", lambda: None)
    assert "for upgrade instructions" in copse.update_notice()


def test_no_notice_json_nontty_env(upd, ws, monkeypatch):
    assert run("repos", "--json").stderr == ""
    monkeypatch.setattr(copse, "_interactive", lambda: False)
    assert "available" not in run("repos").stderr
    monkeypatch.setattr(copse, "_interactive", lambda: True)
    monkeypatch.setenv("COPSE_NO_UPDATE_CHECK", "1")
    assert "available" not in run("repos").stderr
    assert upd["calls"] == 0  # none of those paid for the network call


@pytest.mark.parametrize("latest", ["0.4.0", "0.3.9", "0.4"])
def test_no_notice_equal_or_older(upd, latest):
    upd["latest"] = latest
    assert copse.update_notice() is None


def test_cache_honoured_within_24h_then_ignored(upd, monkeypatch):
    assert copse.update_notice() and upd["calls"] == 1
    assert copse.update_notice() and upd["calls"] == 1  # served from cache
    p = copse._cache_path()
    assert p.parent.name == "copse" and json.loads(p.read_text())["version"] == "9.9.9"
    monkeypatch.setattr(copse.time, "time", lambda: time.time() + 25 * 3600)
    copse.update_notice()
    assert upd["calls"] == 2


def test_network_error_no_notice_no_crash(upd, ws):
    upd["latest"] = None
    r = run("repos")
    assert r.exit_code == 0 and "available" not in r.stderr


@pytest.mark.parametrize("bad", ["garbage", "1.x.0", "", "1..2", "9.9.9rc1", "9.9.9.dev0"])
def test_garbage_version_no_crash(upd, bad):
    upd["latest"] = bad
    assert copse.update_notice() is None
    upd["latest"] = "9.9.9"
    assert copse.update_notice("not-a-version") is None
    assert copse.update_notice("0.4.0.dev0") is None


def test_editable_install_is_unknown_method(monkeypatch):  # `uv tool upgrade` on `uv tool install -e .` is "Nothing to upgrade"
    set_prefix(monkeypatch, "/home/u/.local/share/uv/tools/copse")
    monkeypatch.setattr(copse, "_editable", lambda: True)
    assert copse._detect_install_method() is None
    monkeypatch.setattr(copse, "_editable", lambda: False)
    assert copse._detect_install_method() == "uv"


@pytest.mark.parametrize("body", ["{not json", "[]", '{"ts": "x", "version": "9.9.9"}', '{"version": "9.9.9"}'])
def test_corrupt_cache_refetches(upd, body):
    p = copse._cache_path()
    p.parent.mkdir(parents=True)
    p.write_text(body)
    assert copse.update_notice() and upd["calls"] == 1
    assert json.loads(p.read_text())["version"] == "9.9.9"


def test_future_cache_ts_is_stale(upd):
    p = copse._cache_path()
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"ts": time.time() + 10 * 365 * 86400, "version": "0.1.0"}))
    assert copse.update_notice() and upd["calls"] == 1


def test_unwritable_cache_no_crash_no_tmp_left(upd, monkeypatch):
    def deny(*a): raise PermissionError
    monkeypatch.setattr(copse.os, "replace", deny)
    assert copse.update_notice() and upd["calls"] == 1
    assert list(copse._cache_path().parent.iterdir()) == []
    monkeypatch.setenv("XDG_CACHE_HOME", "/dev/null/nope")  # mkdir fails
    assert copse.update_notice()


def test_notice_is_one_line_on_narrow_terminal(upd, monkeypatch, capsys):
    monkeypatch.setattr(copse, "err", copse.Console(stderr=True, width=30))
    copse.maybe_notice()
    assert capsys.readouterr().err.count("\n") == 1


@pytest.mark.parametrize("code,shown", [(0, True), (1, True), (2, False)])
def test_notifies_keeps_exit_code(upd, code, shown, capsys):
    @copse.notifies
    def cmd(json_=False):
        raise copse.typer.Exit(code)
    with pytest.raises(copse.typer.Exit) as e:
        cmd(json_=False)
    assert e.value.exit_code == code and ("available" in capsys.readouterr().err) == shown


# --- .worktreeinclude, setup hook, merged ---
def write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def inc_repo(ws):
    api = ws / "api"
    write(api / ".gitignore", ".env\n*.log\n")
    write(api / ".worktreeinclude", ".env\nconf/*.log\nkeep.txt\ntracked.txt\n")
    write(api / ".env", "secret")
    write(api / "conf" / "a.log", "log")
    write(api / "keep.txt", "not ignored")
    write(api / "tracked.txt", "t")
    sh("git", "add", "-f", "tracked.txt", ".gitignore", cwd=api)
    sh("git", "commit", "-q", "-m", "c", cwd=api)
    sh("git", "push", "-q", "origin", "main", cwd=api)
    return api


def test_include_copies_only_ignored(ws):
    api = inc_repo(ws)
    code, out = js("new", "1", "-r", "api")
    wt = ws / "tasks" / "1" / "api"
    assert code == 0 and sorted(out["results"][0]["copied"]) == [".env", "conf/a.log"]
    assert (wt / ".env").read_text() == "secret" and (wt / "conf" / "a.log").is_file()
    assert not (wt / "keep.txt").exists()  # matches the file but is not ignored
    assert (wt / "tracked.txt").read_text() == "t"  # tracked: from checkout, not copied
    assert "tracked.txt" not in out["results"][0]["copied"]


def test_include_no_overwrite_and_skips_symlink(ws):
    api = inc_repo(ws)
    (api / "link.log").symlink_to(api / ".env")
    write(api / ".worktreeinclude", ".env\nlink.log\n")
    assert run("new", "1", "-r", "api", "--no-include").exit_code == 0
    wt = ws / "tasks" / "1" / "api"
    assert not (wt / ".env").exists()
    # second run: worktree exists, nothing touched even without the flag
    write(wt / ".env", "mine")
    code, out = js("new", "1", "-r", "api")
    assert out["results"][0]["status"] == "exists" and "copied" not in out["results"][0] and (wt / ".env").read_text() == "mine"
    r = copse.copy_include(api, wt)  # direct: existing dest kept, symlink skipped
    assert r == ([], []) and (wt / ".env").read_text() == "mine" and not (wt / "link.log").exists()


def hook(ws, cmd):
    sh("git", "config", "copse.setup", cmd, cwd=ws / "api")


def test_setup_hook_env_and_ok(ws):
    hook(ws, 'printf "$COPSE_TASK|$COPSE_REPO|$COPSE_MAIN|$PWD" > hook.out')
    code, out = js("new", "7", "-r", "api")
    wt = ws / "tasks" / "7" / "api"
    assert code == 0 and out["results"][0]["setup"] == "ok"
    t, rid, main, cwd = (wt / "hook.out").read_text().split("|")
    assert (t, rid, main) == ("7", "api", str(ws / "api")) and Path(cwd).resolve() == wt.resolve()


def test_setup_failure_exit1_and_no_setup(ws):
    hook(ws, "echo boom >&2; exit 3")
    code, out = js("new", "7", "-r", "api", "-r", "web")
    api, web = out["results"]
    assert code == 1 and api["setup"] == "failed" and "boom" in api["error"] and "setup" not in web
    code, out = js("new", "8", "-r", "api", "--no-setup")
    assert code == 0 and "setup" not in out["results"][0]


def test_setup_script_file_and_dry_run(ws):
    s = ws / "api" / ".copse" / "setup"
    write(s, "#!/bin/sh\ntouch ran\n")
    s.chmod(0o755)
    assert js("new", "9", "-r", "api", "--dry-run")[1]["results"][0].get("setup") is None
    code, out = js("new", "9", "-r", "api")
    assert out["results"][0]["setup"] == "ok" and (ws / "tasks" / "9" / "api" / "ran").exists()


def ls_merged():
    return js("ls", "5")[1]["tasks"][0]["worktrees"][0]["merged"]


def commit_in(wt, name):
    write(wt / name, name)
    sh("git", "add", name, cwd=wt)
    sh("git", "commit", "-q", "-m", name, cwd=wt)


def test_merged_states(ws):
    api = ws / "api"
    assert js("new", "5", "-r", "api")[0] == 0
    wt = ws / "tasks" / "5" / "api"
    assert ls_merged() is False  # fresh
    commit_in(wt, "f")
    assert ls_merged() is False  # unmerged commit
    sh("git", "merge", "-q", "--no-ff", "-m", "m", "5", cwd=api)
    sh("git", "push", "-q", "origin", "main", cwd=api)
    assert ls_merged() is True


def test_merged_squash_and_fresh_behind(ws):
    api = ws / "api"
    assert js("new", "5", "-r", "api")[0] == 0
    wt = ws / "tasks" / "5" / "api"
    sh("git", "commit", "-q", "--allow-empty", "-m", "x", cwd=api)
    sh("git", "push", "-q", "origin", "main", cwd=api)
    assert ls_merged() is False  # fresh branch, base moved on
    commit_in(wt, "f")
    commit_in(wt, "g")  # two commits: a squash only matches their combined diff
    sh("git", "merge", "-q", "--squash", "5", cwd=api)
    sh("git", "commit", "-q", "-m", "sq", cwd=api)
    sh("git", "push", "-q", "origin", "main", cwd=api)
    assert ls_merged() is True


def test_merged_null_without_origin_head(ws):
    assert js("new", "5", "-r", "api")[0] == 0
    sh("git", "remote", "set-head", "origin", "-d", cwd=ws / "api")
    assert ls_merged() is False  # still has its recorded base
    sh("git", "config", "--unset", "branch.5.copseBase", cwd=ws / "api")
    assert ls_merged() is None


def test_merged_uses_own_base(ws):
    api = ws / "api"
    assert js("new", "5", "-r", "api:5:develop")[0] == 0
    wt = ws / "tasks" / "5" / "api"
    commit_in(wt, "f")
    sh("git", "push", "-q", "origin", "5:develop", cwd=wt)
    sh("git", "fetch", "-q", "origin", cwd=api)
    assert ls_merged() is True  # merged into develop, not main


def test_setup_background_child_does_not_hang(ws):
    hook(ws, "sleep 30 & echo started")
    t = time.monotonic()
    assert js("new", "7", "-r", "api")[1]["results"][0]["setup"] == "ok" and time.monotonic() - t < 20
