# copse

[![CI](https://github.com/errhythm/copse/actions/workflows/ci.yml/badge.svg)](https://github.com/errhythm/copse/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/copse.svg)](https://pypi.org/project/copse/)
[![Python versions](https://img.shields.io/pypi/pyversions/copse.svg)](https://pypi.org/project/copse/)
[![License](https://img.shields.io/pypi/l/copse.svg)](LICENSE)

One folder per task, one git worktree per repo. Run it from any directory with repos 1-2 levels deep.

```text
$ copse new 301
◇ Repos api, libs/auth-service-with-long-name
◇ Branch 301
◇ Start new branches from remote
╭─ Plan ───────────────────────────────────────────────╮
│ api                               301  ← origin/main │
│ libs/auth-service-with-long-name  301  ← main        │
╰──────────────────────────────────────────────────────╯
◆ Create 2 worktrees? Yes
✔ api                               301  tasks/301/api
✔ libs/auth-service-with-long-name  301  tasks/301/auth-service-with-long-name
```

## What it does

- Finds repos on its own. It scans 1-2 levels below the current directory for folders with a `.git` directory, skipping dot-folders, `node_modules` and existing worktrees.
- Sets branch and base per repo with `-r repo[:branch[:base]]`. Branch defaults to the task name.
- Starts new branches from `origin/<base>` (the default) or from your local `<base>` with `--from local`, and fetches first unless you pass `--no-fetch`.
- Reuses a local branch if it exists, and tracks a remote-only branch instead of cutting a new one.
- Shows every worktree's branch, dirty state and ahead/behind counts with `copse ls`.
- Removes worktrees with `copse rm`, and deletes a branch only if copse created it for that task.
- Speaks JSON and exit codes for scripts. Without `-r` and without a TTY it exits 2 instead of prompting, and git never asks for credentials.

## Quick start

```bash
uv tool install copse          # or, from a clone: uv tool install -e .

cd ~/code                      # a folder that contains your repos
copse repos                    # see what it found
copse new 200 -r api -r web    # tasks/200/api and tasks/200/web
copse ls 200                   # branch, clean or dirty, ahead/behind
copse rm 200 --delete-branch   # clean up when the task is done
```

Run `copse new 200` on a terminal with no `-r` to pick repos interactively.

## Layout

```text
~/code/
├── api/                # repos you already have
├── web/
├── libs/
│   └── auth-service/
└── tasks/
    └── 200/
        ├── api/        # worktree on branch 200
        └── web/
```

Worktrees go in `./tasks/<task>/`. Change that with `--root` or the `COPSE_ROOT` environment variable. A nested repo is named by its last path part (`auth-service`), or `libs-auth-service` if another repo has the same folder name.

## Commands

| Command | What it does |
| --- | --- |
| `copse repos` | List detected repos with their current branch. |
| `copse new TASK` | Create one worktree per repo under the task folder. |
| `copse ls [TASK]` | Show worktrees per task: branch, dirty, ahead/behind. |
| `copse rm TASK` | Remove a task's worktrees, all or only the `-r` repos. |

`copse new` options:

| Flag | Default | Meaning |
| --- | --- | --- |
| `-r`, `--repo SPEC` | interactive pick | Repo to include. Repeat for more. |
| `-b`, `--branch` | task name | Branch for every repo without its own. |
| `--base` | `<remote>/HEAD`, else current branch | Base for new branches. |
| `--from remote\|local` | `remote` | Cut new branches from `<remote>/<base>` or from local `<base>`. |
| `--remote` | `origin` | Remote to fetch and track. |
| `--no-fetch` | off | Skip `git fetch`. |
| `--dry-run` | off | Plan only, create nothing. |
| `--root` | `./tasks` (`$COPSE_ROOT`) | Where task folders live. |
| `--json` | off | Machine-readable output. |

A SPEC is `repo[:branch[:base]]`. `repo` is the id from `copse repos` or just the folder name if that is unique.

```bash
copse new 200 -r api                       # branch 200, default base
copse new 200 -r api:feat/200:develop      # own branch and base
copse new 200 --from local -r web:x:origin/main   # local start, but this repo uses origin/main
```

## How branches are picked

For each repo, the first rule that matches wins.

1. A local branch with that name exists. It is reused.
2. `<remote>/<branch>` exists. A local branch is created that tracks it.
3. Otherwise a new branch is cut from the base. A base written as `origin/x` always uses that remote ref, even with `--from local`. Otherwise `--from remote` uses `origin/<base>` when it exists and `--from local` uses `<base>`. A sha or tag also works.

With no base given, copse uses what `<remote>/HEAD` points at, or the repo's current branch if there is none. Rerunning `new` marks worktrees that already exist as `exists`.

## For agents and scripts

Add `--json` to `repos`, `new`, `ls` or `rm`. `new` and `rm` print:

```json
{
  "task": "200",
  "path": "/home/me/code/tasks/200",
  "results": [
    {"repo": "api", "path": ".../tasks/200/api", "branch": "200", "base": "main",
     "status": "created", "error": null}
  ]
}
```

`status` is `created`, `exists`, `planned` (dry run), `removed` or `failed`. Dry runs also include `start`, the ref the new branch would be cut from.

| Code | Meaning |
| --- | --- |
| 0 | Everything worked. |
| 1 | At least one repo failed. The others still ran. |
| 2 | Usage error, no `-r` without a TTY, or a cancelled prompt. Nothing was created. |

Output is colored only on a TTY. Pipes, `--json` and `NO_COLOR` get plain text, and errors go to stderr as `error: ...`.

```bash
copse new 200 -r api -r web:feat/200:develop --json | jq '.results[] | {repo,status,error}'
```

## Removing tasks safely

`copse rm 200 --delete-branch` removes the worktrees, then deletes each branch only if copse created it for that task. Branches copse created carry a `branch.<name>.copseTask` git config entry. Branches you attached are kept and reported as `branch_kept` in JSON. Deletion uses `git branch -d`, so a branch with unmerged commits survives unless you add `--force`, which also forces worktree removal and uses `-D`.

## Agent skill

```bash
npx skills add errhythm/copse
```

Teaches coding agents to drive copse safely: always `-r` and `--json`, dry-run first, read exit codes, and remove branches only when copse created them.

## Why copse

It was called `twt` for about a day, and I didn't like it. A copse is a small group of trees, which is what a task is here: a handful of worktrees standing together in one folder, one per repo.

## Development

```bash
uv sync
uv run pytest
```

## License

MIT, see [LICENSE](LICENSE). [NOTICE](NOTICE) credits the projects some code was adapted from.
