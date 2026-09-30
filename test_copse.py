import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from copse import app

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "init.defaultBranch", "GIT_CONFIG_VALUE_0": "main"}


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
