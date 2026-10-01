# copse CLI reference

Run every command from the folder that holds your repos. copse scans 1-2 levels below the current directory for folders with a `.git` directory. It skips dot-folders, `node_modules`, the task root and existing worktrees.

Errors go to stderr as `error: ...`. `--json` output goes to stdout as plain JSON. git never prompts for credentials.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Everything worked. |
| 1 | At least one repo has `status: "failed"`. The other repos still ran. `new` and `rm` only. |
| 2 | Usage error (unknown repo, bad spec, bad `--from`, no such task, invalid task name, no `-r` without a TTY) or a cancelled prompt. Nothing was created. |

## Global

| Flag | Meaning |
|---|---|
| `--version` | Print the version. |
| `--help` | Help for copse or any command. |

## copse update

Upgrade copse. Runs `uv tool upgrade copse` or `pipx upgrade copse`, picked from where copse is installed, and exits with that command's code. On success it then prints `copse --version`. `copse upgrade` is a hidden alias.

Exit 1 when the install method is unknown (it prints manual steps), when `uv` or `pipx` is not on PATH, and always on Windows, where it prints the command instead of running it.

Environment: `COPSE_NO_UPDATE_CHECK=1` turns off the daily update notice. The notice is one dim line on stderr after `repos`, `new`, `ls` and `rm`. It never appears with `--json` or when stdout or stderr is not a TTY, so scripts and agents never see it.

## copse repos

List detected repos.

| Flag | Meaning |
|---|---|
| `--json` | Machine-readable output. |

```json
[{"repo": "api", "path": "/home/me/code/api"},
 {"repo": "libs/auth-service", "path": "/home/me/code/libs/auth-service"}]
```

## copse new TASK

Create one worktree per repo at `<root>/<TASK>/<repo>`. TASK must be a plain name (no `/`, `.`, `..`).

| Flag | Default | Meaning |
|---|---|---|
| `-r`, `--repo SPEC` | interactive pick (TTY only) | `repo[:branch[:base]]`. Repeatable. `repo` is an id from `copse repos` or a unique folder name. |
| `-b`, `--branch` | the task name | Branch for every repo that has no branch in its SPEC. |
| `--base` | `<remote>/HEAD`, else the repo's current branch | Base for new branches. Overridden by a SPEC base. |
| `--from remote\|local` | `remote` | Cut new branches from `<remote>/<base>` or from local `<base>`. Anything else exits 2. |
| `--remote` | `origin` | Remote to fetch and track. |
| `--no-fetch` | off | Skip `git fetch <remote>`. |
| `--no-include` | off | Skip copying `.worktreeinclude` files. |
| `--no-setup` | off | Skip the setup hook. |
| `--dry-run` | off | Resolve and report the plan. Creates nothing, does not fetch, copies nothing, runs no hook. |
| `--root` | `./tasks` (env `COPSE_ROOT`) | Where task folders live. |
| `--json` | off | Machine-readable output. |

Only newly created worktrees get the next two steps (never `exists`):

- `.worktreeinclude`: if the main checkout has one (gitignore syntax), files that are gitignored and match it are copied into the new worktree. Tracked, non-ignored and symlinked files are skipped, existing files are never overwritten, mode is kept. Result: `copied` (relative paths) and `include_errors` (per-file messages; they do not change the exit code).
- Setup hook, run after the copy with the worktree as cwd and stdin closed: `git config copse.setup` (a shell string, repo or global), else an executable `.copse/setup` in the main checkout. Env: `COPSE_TASK`, `COPSE_REPO`, `COPSE_MAIN`. Result: `setup` is `"ok"` or `"failed"` (with the output tail in `error`, and exit 1); the key is absent when there is no hook.

Branch resolution per repo, first match wins:

1. Local branch exists: reused (`git worktree add <path> <branch>`).
2. `<remote>/<branch>` exists: local branch created with `--track`.
3. Otherwise a new branch is cut from the base with `--no-track`, then `branch.<name>.remote` and `branch.<name>.merge` are set so its upstream is `<remote>/<branch>` (same name, works before the remote branch exists). If the repo has no such remote, no upstream is set. A base `origin/x` always uses that remote ref. Else `--from remote` prefers `origin/<base>` then local `<base>`, and `--from local` uses local `<base>` first. A sha or tag also works.

Branches copse creates in steps 2 and 3 get `branch.<name>.copseTask = <task>` in git config. `rm --delete-branch` uses it.

### new JSON

```json
{
  "task": "200",
  "path": "/home/me/code/tasks/200",
  "results": [
    {"repo": "api", "path": "/home/me/code/tasks/200/api", "branch": "200",
     "base": "main", "status": "created", "error": null}
  ]
}
```

| status | Meaning |
|---|---|
| `created` | Worktree added. |
| `planned` | Dry run. The entry also has `start`, the ref the branch would come from. |
| `exists` | A worktree of this repo is already at that path. `branch` is its current branch and `base` is `null`. Not a failure. |
| `failed` | `error` holds git's message or copse's. Missing start ref entries also carry `start`. |

## copse ls [TASK]

Show worktrees per task. Without TASK, lists every task folder under the root.

| Flag | Meaning |
|---|---|
| `--root` | Task root (env `COPSE_ROOT`). |
| `--json` | Machine-readable output. |

Unknown TASK exits 2. A missing root gives `{"tasks": []}`.

```json
{"tasks": [
  {"task": "200", "path": "/home/me/code/tasks/200",
   "worktrees": [
     {"repo": "api", "path": "/home/me/code/tasks/200/api", "branch": "200",
      "dirty": false, "ahead": 1, "behind": 0, "merged": false}
   ]}
]}
```

`dirty` is true when `git status --porcelain` prints anything. `ahead` and `behind` count against the upstream (`HEAD...@{u}`) and are `null` when `@{u}` does not resolve: no upstream, or the remote branch is not pushed yet. After the first `git push` they count unpushed and unpulled commits.

`merged` compares HEAD with its base: the ref copse cut the branch from (`branch.<name>.copseBase`), else `<remote>/HEAD` (remote from the branch's upstream, default `origin`). `true` if HEAD is an ancestor of the base, or the branch's whole diff was squash-merged (`git cherry` on a synthetic squash commit); `false` if not, or if the branch is untouched (its reflog is only "Created from <base>"); `null` without a base or on a detached HEAD. The table shows a `merged` tag in the sync column.

## copse rm TASK

Remove worktrees of TASK. The task folder is deleted once empty. Unknown TASK exits 2.

| Flag | Default | Meaning |
|---|---|---|
| `-r`, `--repo` | all repos of the task | Only these repos. Matches the repo id or the worktree folder name. Repeatable. |
| `--force` | off | Remove even if dirty, and delete branches with `git branch -D`. A locked worktree still fails: run `git worktree unlock` first. |
| `--delete-branch` | off | After removal, delete the branch if copse created it for this task. Uses `git branch -d`. |
| `--root` | `./tasks` (env `COPSE_ROOT`) | Task root. |
| `--json` | off | Machine-readable output. |

### rm JSON

```json
{
  "task": "200",
  "path": "/home/me/code/tasks/200",
  "results": [
    {"repo": "api", "path": "...", "branch": "200", "status": "removed", "error": null},
    {"repo": "web", "path": "...", "branch": "feat/x", "status": "removed", "error": null,
     "branch_kept": "not created by copse for this task"}
  ]
}
```

| status | Meaning |
|---|---|
| `removed` | Worktree removed. Any `branch_kept` explains why `--delete-branch` spared the branch. |
| `failed` | Git refused (dirty worktree without `--force`) or the branch could not be deleted (unmerged commits without `--force`). `error` has the message. |

## Gotchas

- `--root` and `COPSE_ROOT` must match across `new`, `ls` and `rm`.
- Repo scanning depends on the current directory. `ls` and `rm` also resolve repo ids relative to it.
- `--dry-run` skips the fetch, so a branch that only exists on a fresh remote may plan as a new branch.
- Interactive mode (`new` with no `-r` on a TTY) is for humans. Do not script it.
