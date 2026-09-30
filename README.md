# copse

[![CI](https://github.com/errhythm/copse/actions/workflows/ci.yml/badge.svg)](https://github.com/errhythm/copse/actions/workflows/ci.yml)

Task worktrees across git repos. Run it in a folder containing repos (1-2 levels deep); it groups one worktree per repo under a task folder (`./tasks/<task>/`, override with `--root` or `$COPSE_ROOT`).

## Why copse

It was called `twt` for about a day, and I didn't like it. A copse is a small group of trees, which is what a task is here: a handful of worktrees standing together in one folder, one per repo.

## Install

    uv tool install copse
    # or from a clone:
    uv tool install -e .

## Usage

    copse repos [--json]
    copse new 200 -r api:feat/200:develop -r web        # SPEC = repo[:branch[:base]]
    copse new 200 -r api -b fix/x --base main --dry-run --json
    copse new 200 -r api -r web                         # new branches start at origin/<base> (--from remote, default)
    copse new 200 --from local -r api -r web:x:origin/main   # start at local <base>; a spec base of origin/... overrides per repo
    copse ls [200] [--json]
    copse rm 200 [-r api] [--force] [--delete-branch]

Branch default: `-b`, else the task name. Base default: `<remote>/HEAD`, else the current branch.
Per repo: local branch is reused, else `<remote>/<branch>` is tracked, else a new branch is cut from the base.
Without `-r`: interactive checkbox on a TTY; exit 2 otherwise (safe for agents).
Interactive (`copse new 200` on a TTY): pick repos (space, type to filter), edit branch/base/from (defaults from `-b`, `--base`, `--from`), review the plan, confirm (default Yes). Ctrl-C or No exits 2 and creates nothing. Human output is colored on a TTY only; `--json`, pipes and `NO_COLOR` get plain text. Errors go to stderr as `error: ...`.
Exit codes: 0 ok, 1 a repo failed (others still done), 2 usage error. Rerunning `new` marks existing worktrees `exists`.
`rm --delete-branch` only deletes branches copse created for that task (marked `branch.<name>.copseTask`); attached existing branches are kept (`branch_kept` in JSON). It refuses to drop a branch with unmerged commits unless `--force`.

Agent example:

    copse new 200 -r api -r web:feat/200:develop --json | jq '.results[] | {repo,status,error}'

## Development

    uv sync
    uv run pytest
