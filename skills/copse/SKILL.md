---
name: copse
description: Creates and manages git worktrees for one task across several repos with the copse CLI. Use for "create worktrees for a task", "work on issue 200 across api and web", "set up worktrees in multiple repos", "clean up task worktrees", "which worktrees are dirty", or "is anything unpushed in the task folders". Covers repo discovery, per-repo branches and bases, existing remote branches, status checks, and safe removal. Not for single-repo branching, plain `git worktree` use, or editing copse's own source.
license: MIT
compatibility: Requires git and the copse CLI (uv tool install copse).
metadata:
  version: "1.0.0"
---

# copse

copse puts one task's worktrees for several repos in one folder: `tasks/<task>/<repo>`. Run it from the folder that contains your repos.

## When to use this skill

- The user wants a branch for the same task in two or more sibling repos ("work on issue 200 across api and web").
- The user asks which task worktrees are dirty, unpushed, or behind.
- The user wants a task's worktrees removed and its branches cleaned up.
- Not for: one repo (use `git worktree` or `git switch`), or changing copse's code.

Every flag, JSON shape and exit code is in `references/copse-cli.md`. Open it before parsing output you have not seen.

## Install check

```bash
copse --version || uv tool install copse
```

To upgrade, a human runs `copse update`. Agents do not need to: the daily update notice only prints on an interactive terminal, never with `--json` or piped output, so you will not see it and it costs you no network call.

## Rules for agents

1. Always pass `-r` and `--json`. Without `-r` and without a TTY, `copse new` exits 2 and creates nothing. Never run bare `copse new TASK`.
2. Run from the parent folder of the repos. copse scans 1-2 levels below the current directory. Use `copse repos --json` to see what it found.
3. When unsure, run `new` with `--dry-run --json` first, read the plan, then rerun without `--dry-run`.
4. Read the exit code. 0 means all repos worked. 1 means at least one repo has `status: "failed"`, the others still ran. 2 means usage error and nothing was created.
5. On exit 1, read `.results[].error` per repo. Do not assume the whole run failed.
6. Never pass `--force` on `rm` unless the user agreed to lose uncommitted work.

## Recipes

### Discover repos

```bash
copse repos --json | jq -r '.[].repo'
```

Each entry is `{"repo": "libs/auth-service", "path": "..."}`. A repo can be named by its id or by its folder name when that is unique.

### One task, many repos, same branch

The branch defaults to the task name.

```bash
copse new 200 -r api -r web --json | jq '.results[] | {repo, branch, status, error}'
```

Use `-b feat/200` to name the branch for every repo at once.

### Per-repo branch and base

A SPEC is `repo[:branch[:base]]`.

```bash
copse new 200 -r api:feat/200:develop -r web --base main --json
```

`api` gets branch `feat/200` cut from `develop`. `web` gets branch `200` cut from `main`. With no base, copse uses what `origin/HEAD` points at, else the repo's current branch.

### Start from remote or local

New branches start from `origin/<base>` by default, after a `git fetch`. Pass `--from local` to start from the local `<base>` instead. Pass `--no-fetch` to skip the fetch (offline, or fetch already done). `--remote upstream` swaps the remote.

```bash
copse new 200 --from local -r api -r web:200:origin/main --json
```

A base written `origin/x` always uses the remote ref, even under `--from local`. Here `api` starts from local base and `web` from `origin/main`.

### Pick up an existing remote branch

If `origin/<branch>` exists and no local branch does, copse creates a local branch that tracks it. If a local branch exists, copse reuses it. Read the tracked ref from the dry run:

```bash
copse new 200 -b feat/200 -r api --dry-run --json | jq '.results[] | {repo, start}'
```

### Add a repo to an existing task

Rerun `new` with the extra repo. Worktrees that already exist come back as `exists`, which is fine and not a failure.

```bash
copse new 200 -r api -r mobile --json | jq -r '.results[] | "\(.repo) \(.status)"'
```

### Custom root

Worktrees go in `./tasks` by default. Set `--root DIR` or `COPSE_ROOT=DIR`. Use the same root for `new`, `ls` and `rm`, or they will not see each other's tasks.

```bash
COPSE_ROOT=~/work/tasks copse ls --json
```

### Check status

```bash
copse ls 200 --json | jq -r '.tasks[].worktrees[] | "\(.repo)\t\(.branch)\tdirty=\(.dirty)\tahead=\(.ahead)\tbehind=\(.behind)"'
copse ls --json | jq '[.tasks[].worktrees[] | select(.dirty)] | map(.repo)'
```

`ahead` and `behind` count against the branch's upstream, and are `null` when it has none or the remote branch is not pushed yet. A branch copse creates gets the upstream `<remote>/<branch>` (the same name, not the base), so both are `null` until the first push. After that they count unpushed and unpulled commits. A bare `git push` publishes the branch. A repo without the remote gets no upstream. Omit the task to list every task under the root.

### Remove one repo or the whole task

```bash
copse rm 200 -r web --json      # only web
copse rm 200 --json             # every worktree of task 200
```

Git refuses to remove a dirty worktree. That shows up as `status: "failed"` with git's message in `error`. Commit or stash, or ask the user before using `--force`.

### Delete branches safely

```bash
copse rm 200 --delete-branch --json | jq '.results[] | {repo, branch, status, branch_kept}'
```

copse deletes a branch only if it created that branch for this task. That includes a local branch it created to track a remote-only branch. The remote branch stays. An existing local branch it reused stays, and the result has `branch_kept`. Deletion uses `git branch -d`, so a branch with unmerged commits stays and the entry is `failed`. `--force` also removes dirty worktrees and switches to `-D`. Use it only with user consent.

### Work inside the worktrees

Paths come from the JSON. Do not rebuild them.

```bash
wt=$(copse new 200 -r api --json | jq -r '.results[0].path')
cd "$wt"
```

The layout is `tasks/<task>/<repo>`. A nested repo uses its last path part (`auth-service`), or `libs-auth-service` if another repo shares that folder name. Commit, push and open PRs from inside that folder as in any git checkout.

### Interactive mode (humans only)

`copse new TASK` with no `-r` on a terminal opens a repo picker, prompts for branch, base and start point, shows a plan, and asks to confirm. Agents must not use it. Ctrl-C or declining exits 2 with nothing created.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `error` says the branch is "already checked out" or "used by worktree" | Another worktree holds that branch. Run `git worktree list` in the repo, remove that worktree, or pick another branch with `-r repo:other-branch`. |
| `missing but already registered worktree` | A worktree folder was deleted by hand. Run `git worktree prune` in that repo, then rerun `new`. |
| `unknown or ambiguous repo 'x'` (exit 2) | Wrong name or wrong directory. Run `copse repos --json` from the repos' parent and use an id from it. |
| `start ref 'origin/x' does not exist` | The base is wrong or not fetched. Drop `--no-fetch`, check the base name, or use `--from local`. |
| `no such task 'x'` (exit 2) | Task name or `--root` differs from the one used at creation. |
| `no -r given and stdin is not a TTY` (exit 2) | Add `-r repo`. |
| `'<path>' already exists` | A plain, non-empty folder sits at `tasks/<task>/<repo>`. Move it, then rerun. |
| `<path> exists and is not a worktree of this repo` | A checkout of some other repo sits there. Move it, then rerun. |
| `cannot remove a locked working tree` | `--force` does not override a lock. Run `git worktree unlock <path>` in the repo, then rerun `rm`. |
