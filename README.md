# copse

[![CI](https://github.com/errhythm/copse/actions/workflows/ci.yml/badge.svg)](https://github.com/errhythm/copse/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/copse?cacheSeconds=3600)](https://pypi.org/project/copse/)
[![Python versions](https://img.shields.io/pypi/pyversions/copse?cacheSeconds=3600)](https://pypi.org/project/copse/)
[![License](https://img.shields.io/pypi/l/copse?cacheSeconds=3600)](LICENSE)

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

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Commands](#commands)
- [Setup for new worktrees](#setup-for-new-worktrees)
- [Configuration](#configuration)
- [Agents and scripts](#agents-and-scripts)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [License](#license)

## Install

```bash
uv tool install copse          # or: pipx install copse
uv tool install -e .           # from a clone
```

Upgrade with `copse update`. It runs `uv tool upgrade copse` or `pipx upgrade copse`, and on Windows it prints the command instead. On a terminal, copse checks PyPI once a day and prints a one-line notice on stderr when a newer version exists. Set `COPSE_NO_UPDATE_CHECK=1` to turn that off. The check never runs with `--json` or when output is piped.

## Quick start

```bash
cd ~/code                      # a folder that contains your repos
copse repos                    # see what it found
copse new 200 -r api -r web    # tasks/200/api and tasks/200/web
copse ls 200                   # branch, clean or dirty, ahead/behind, merged
copse rm 200 --delete-branch   # clean up when the task is done
```

Run `copse new 200` on a terminal with no `-r` to pick repos interactively.

## How it works

Worktrees go in `./tasks/<task>/` next to your repos:

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

- **Repo discovery.** copse scans 1-2 levels below the current directory for folders with a `.git` directory, skipping dot-folders, `node_modules` and existing worktrees. A repo's id is the path below the current directory (`libs/auth-service`). Its worktree folder is the last path part (`auth-service`), or `libs-auth-service` if another repo has the same folder name.
- **Per-repo branch and base.** Use `-r repo[:branch[:base]]`. Branch defaults to the task name. `repo` is the id from `copse repos`, or the folder name if that is unique.
- **Branch choice.** For each repo, the first rule that matches wins:
  1. A local branch with that name exists. It is reused.
  2. `<remote>/<branch>` exists. A local branch is created that tracks it.
  3. Otherwise a new branch is cut from the base (see below).
- **Base.** With no base given, copse uses what `<remote>/HEAD` points at, or the repo's current branch if there is none. A base written as `origin/x` always uses that remote ref, even with `--from local`. Otherwise `--from remote` (the default) uses `origin/<base>` when it exists, and `--from local` uses `<base>`. A sha or tag also works. copse fetches first unless you pass `--no-fetch`.
- **Upstream.** A new branch's upstream is `<remote>/<branch>` (the same name, not the base), even before that branch exists on the remote, so a bare `git push` publishes it. Until the first push, `copse ls` shows no ahead/behind. A repo without the remote gets no upstream.
- **Reruns.** Rerunning `new` marks worktrees that already exist as `exists`.
- **Include files.** `copse new` copies git-ignored files such as `.env` into each new worktree (see [Setup for new worktrees](#setup-for-new-worktrees)).
- **Removal.** `copse rm` removes worktrees. With `--delete-branch` it deletes a branch only if copse created it for that task. Those branches carry a `branch.<name>.copseTask` git config entry. Branches you attached are kept and reported as `branch_kept` in JSON. Deletion uses `git branch -d`, so a branch with unmerged commits survives unless you add `--force`, which also forces worktree removal and uses `-D`.

## Commands

| Command | What it does |
| --- | --- |
| `copse repos` | List detected repos with their current branch. |
| `copse new TASK` | Create one worktree per repo under the task folder. |
| `copse ls [TASK]` | Show worktrees per task: branch, dirty, ahead/behind, merged. |
| `copse rm TASK` | Remove a task's worktrees, all or only the `-r` repos. |
| `copse update` | Upgrade copse with uv or pipx. |

### `copse new`

| Flag | Default | Meaning |
| --- | --- | --- |
| `-r`, `--repo SPEC` | interactive pick | Repo to include. Repeat for more. SPEC is `repo[:branch[:base]]`. |
| `-b`, `--branch` | task name | Branch for every repo without its own. |
| `--base` | `<remote>/HEAD`, else current branch | Base for new branches. |
| `--from remote\|local` | `remote` | Cut new branches from `<remote>/<base>` or from local `<base>`. |
| `--remote` | `origin` | Remote to fetch and track. |
| `--no-fetch` | off | Skip `git fetch`. |
| `--no-include` | off | Don't copy `.worktreeinclude` files. |
| `--no-setup` | off | Don't run the setup hook. |
| `--dry-run` | off | Plan only, create nothing. |
| `--root` | `./tasks` (`$COPSE_ROOT`) | Where task folders live. |
| `--json` | off | Machine-readable output. |

```bash
copse new 200 -r api                       # branch 200, default base
copse new 200 -r api:feat/200:develop      # own branch and base
copse new 200 --from local -r web:x:origin/main   # local start, but this repo uses origin/main
```

### `copse ls`

`copse ls [TASK]` shows each worktree's branch, dirty state and ahead/behind counts. It also reports `merged`: `true` once the branch's commits are in the base it was cut from (or `<remote>/HEAD` for branches copse did not cut), including squash and fast-forward merges; `false` if not, or if the branch has no commits yet; `null` if there is no base to compare. Options: `--root`, `--json`.

### `copse rm`

| Flag | Meaning |
| --- | --- |
| `-r`, `--repo` | Repo id or folder name to remove. Repeat for more. Default is all of the task's repos. |
| `--delete-branch` | Also delete the task branch, if copse created it. |
| `--force` | Remove even with uncommitted changes. Also uses `git branch -D`. |
| `--root`, `--json` | As above. |

## Setup for new worktrees

Both steps run in `copse new`, after each worktree is created, and both read from the main checkout.

**Include files.** List patterns in `<repo>/.worktreeinclude`, one per line, in gitignore syntax. copse copies matching files from the main checkout into the new worktree, but only files that git ignores, such as `.env`. It skips symlinks and never overwrites an existing file. JSON results list them under `copied`. Pass `--no-include` to skip.

```text
# .worktreeinclude
.env
.env.local
```

**Setup hook.** Set a command with `git config copse.setup "<cmd>"`, or add an executable `<repo>/.copse/setup`. copse runs it in each new worktree with these environment variables:

| Variable | Value |
| --- | --- |
| `COPSE_TASK` | Task name. |
| `COPSE_REPO` | Repo id. |
| `COPSE_MAIN` | Path of the repo's main checkout. |

If the hook fails, the result gets `"setup": "failed"` in JSON and `copse new` exits 1. Pass `--no-setup` to skip.

```bash
git config copse.setup "pnpm install"
```

## Configuration

| Setting | Effect |
| --- | --- |
| `COPSE_ROOT` | Task root, same as `--root`. Default `./tasks`. |
| `COPSE_NO_UPDATE_CHECK=1` | Turn off the daily update notice. |
| `NO_COLOR` | Plain output. Color is only used on a TTY. |
| `git config copse.setup` | Setup hook command. |
| `<repo>/.worktreeinclude` | Files to copy into new worktrees. |
| `<repo>/.copse/setup` | Executable setup hook. |

## Agents and scripts

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

`status` is `created`, `exists`, `planned` (dry run), `removed` or `failed`. Dry runs also include `start`, the ref the new branch would be cut from. `new` also reports `copied` and `include_errors` for new worktrees and, when a hook ran, `setup`.

| Code | Meaning |
| --- | --- |
| 0 | Everything worked. |
| 1 | At least one repo failed, or a setup hook failed. The others still ran. |
| 2 | Usage error, no `-r` without a TTY, or a cancelled prompt. Nothing was created. |

Without `-r` and without a TTY, copse exits 2 instead of prompting, and git never asks for credentials. Errors go to stderr as `error: ...`.

```bash
copse new 200 -r api -r web:feat/200:develop --json | jq '.results[] | {repo,status,error}'
```

To teach a coding agent to drive copse safely (always `-r` and `--json`, dry-run first, read exit codes, remove branches only when copse created them), install the skill:

```bash
npx skills add errhythm/copse
```

## Troubleshooting

- **`copse new` exits 2 in a script.** Pass `-r` for every repo. Without a TTY it will not prompt.
- **A repo is missing from `copse repos`.** It must have a `.git` directory within 2 levels of the current directory. Dot-folders, `node_modules` and existing worktrees are skipped.
- **Two repos share a folder name.** Use the id from `copse repos` (for example `libs/auth-service`).
- **`git push` has no upstream.** Only repos that have the remote get one. Check `--remote`.
- **A branch survived `copse rm --delete-branch`.** copse did not create it, or it has unmerged commits. Add `--force` for the second case.

## Development

```bash
uv sync
uv run pytest
```

copse was called `twt` for about a day. A copse is a small group of trees, which is what a task is here: a handful of worktrees standing together in one folder, one per repo.

## License

MIT, see [LICENSE](LICENSE). [NOTICE](NOTICE) credits the projects some code was adapted from.
