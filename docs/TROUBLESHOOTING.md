# Troubleshooting

Each entry gives the symptom, the cause and the fix.

## Quick reference for common install/setup problems

### 1. `Hooks use a matcher + hooks array` error in `.claude/settings.json`

**Cause.** Releases before v2.2.3 wrote an invalid hook schema: flat
`{matcher, command, timeout}` entries, timeouts in milliseconds, and a
`PreCommit` event that Claude Code does not have. v2.2.3 (PR #208) rewrote the
generator.

**Fix.**

```bash
pip install --upgrade gryphon
cd /path/to/your/project
gryphon install
```

`install` merges its entries into the existing `hooks` block and does not
delete entries it did not write. If the old flat entries are still there,
remove them from `.claude/settings.json` by hand (`install` writes a backup to
`.claude/settings.json.bak` first), then run `install` again.

The generated config uses two events: `PostToolUse` (runs
`gryphon update --skip-flows` after Edit/Write) and `SessionStart`
(runs `gryphon status`). Pre-commit checks live in a git hook, not in
Claude Code settings.

#### The git pre-commit hook

`install` writes a `pre-commit` hook to the directory reported by
`git rev-parse --git-path hooks`: usually `.git/hooks`, but `core.hooksPath`
(for example `.husky`) and submodules are covered too. An existing hook is
appended to, not overwritten. If husky or pre-commit manages your hooks, you
can add the two commands there instead:

```sh
gryphon update
gryphon detect-changes --brief
```

The hook skips linked worktrees, so a commit there cannot silently build a
second graph for another branch. It finds the git dir with
`git rev-parse --absolute-git-dir` (Git 2.13 or newer); a `commondir` file in
that directory marks a linked worktree. It then prints this on stderr and lets
the commit continue:

```
gryphon: skipping automatic checks in a linked worktree; set CRG_HOOK_WORKTREES=1 to keep a graph for this worktree too.
```

Set `CRG_HOOK_WORKTREES=1` to run the checks in a worktree. If Git cannot
report the git dir or the worktree root, the hook also skips the checks and the
commit proceeds.

Re-running `install` upgrades the exact hook block written by older releases.
A block you have edited is left alone; update it by hand.

### 2. `gryphon: command not found` after `pip install`

**Cause.** `pip` put the console script in a `bin/` directory that is not on
your `PATH`.

**Fix.** Pick one:

1. Install with `pipx`:

   ```bash
   pip uninstall gryphon
   pipx install git+https://github.com/jjgouveia/gryphon-standalone.git
   ```

   If the command is still not found, run `pipx ensurepath` and open a new
   shell.

2. Run it with `uvx` (no install):

   ```bash
   uvx gryphon install
   uvx gryphon build
   ```

3. Run it as a module with the interpreter you installed into:

   ```bash
   python -m gryphon install
   python -m gryphon build
   ```

4. Add the script directory to `PATH`. `pip show gryphon | grep Location`
   prints the `site-packages` directory; the scripts are in the sibling `bin/`
   (on macOS user installs, typically `~/Library/Python/3.X/bin`):

   ```bash
   echo 'export PATH="$HOME/Library/Python/3.12/bin:$PATH"' >> ~/.zshrc
   source ~/.zshrc
   ```

### 3. Is gryphon project-scoped or user-scoped?

Both. The pieces are scoped differently:

| Piece | Scope | Where |
|---|---|---|
| Python package | User | Installed once with `pip`, `pipx` or `uvx` |
| Graph database | Project | `.gryphon/graph.db` in each repository (or `--data-dir` / `CRG_DATA_DIR`) |
| MCP server config (`.mcp.json` and platform equivalents) | Project | One server per project; the entry carries `cwd=<project>` |
| Multi-repo registry | User | `~/.gryphon/registry.json` (or under `$CRG_HOME`) |

Install the package once, then run
`gryphon install && gryphon build` in each project.

### 4. Installed in a virtual environment? Re-run `install` from inside it

**Symptom.** The MCP server does not start, or hooks never update the graph,
after you moved the package into a venv.

**Cause.** `install` records the absolute path of the running interpreter
with `-m gryphon serve`, so an entry written outside the venv points at the
wrong interpreter. (Ephemeral `uvx` runs are the exception: they record
`uvx --from <dist> gryphon serve`, because an interpreter inside uv's cache
would not survive `uv cache clean`.) The Claude Code hooks store no path; they
run `gryphon` from `PATH` and exit silently when it is not found, so
they do nothing in a session where the venv is not activated.

**Fix.** Activate the venv and run `install` again:

```bash
source .venv/bin/activate
gryphon install
```

The fallback entry looks like this:

```json
{
  "mcpServers": {
    "gryphon": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "gryphon", "serve"],
      "cwd": "/path/to/your/project"
    }
  }
}
```

Start Claude Code from a shell with the venv activated if you want the hooks to
run. Quit and reopen Claude Code after changing the config.

### 5. "I built the graph but Claude Code doesn't see it in a new session"

Likely causes, most common first:

1. Claude Code was not restarted after `install`. It reads `.mcp.json` at
   startup.
2. The new session's working directory is different. The server runs with
   `cwd=<project>` and reads `.gryphon/graph.db` from there. A
   session opened in a parent folder or another project will not find your
   graph.
3. You ran `build` but not `install`. `build` writes `graph.db`; `install`
   registers the MCP server.
4. The server crashes on startup. Run `/mcp` in Claude Code to see the server
   status, and run the command from `.mcp.json` in a terminal to see the error.

Checklist:

```bash
cd /path/to/your/project
gryphon status    # prints Nodes, Edges and Files for the graph
ls .mcp.json                # must exist
cat .mcp.json               # must contain a gryphon entry ending in "serve"
# then quit Claude Code and reopen it in this directory
```

If `status` finds the graph but `/mcp` does not list `gryphon`,
`.mcp.json` is not in the session's working directory. Run
`gryphon install` from the project root.

---

## Database lock errors

The graph is SQLite in WAL mode. If you see `database is locked`:

- Run one `build`, `update` or `watch` at a time per repository.
- Retry; the lock is usually another process that has just finished.
- If the files are corrupt, stop every process that uses the graph, delete
  `.gryphon/graph.db`, `graph.db-wal` and `graph.db-shm`, and run
  `gryphon build`.

## Large repositories

- The first `build` parses every file. Later `update` runs parse only changed
  files and their dependents.
- Only files tracked by git are indexed, so anything in `.gitignore` is already
  skipped.
- Exclude generated code and vendored dependencies in `.gryphonignore`:

  ```
  generated/**
  vendor/**
  third_party/
  ```

  A directory name without a slash matches at any depth; a leading slash
  anchors the pattern to the repository root.

## Missing nodes after build

- Check the file's language is supported (see [FEATURES.md](FEATURES.md)) or
  added through `languages.toml` (see [CUSTOM_LANGUAGES.md](CUSTOM_LANGUAGES.md)).
- Check the file is tracked by git and not matched by `.gryphonignore`
  or the nested build-output detection described below.
- Look for a parse warning (next entry) after `update`.
- Run `gryphon build`, or the MCP tool `build_or_update_graph_tool`
  with `full_rebuild=True`, to re-parse everything.

## `Warning: N file(s) failed to parse and were not updated`

`update` prints this on stderr when a file could not be parsed. The file keeps
the rows from its last successful parse, and the rest of the update is still
recorded as current, so one bad file does not hold the graph back. `build`
prints `Errors: N` for the same case. Fix or ignore the file, then run `update`
again.

## Empty or incomplete graph (poisoned `graph.db`)

**Symptom.** `status` shows far fewer files than the repository has, or zero
nodes.

**Cause.** Older releases created an empty `graph.db` when `status`,
`detect-changes`, `visualize`, `wiki` or `watch` ran before the first `build`.
`update` then re-parsed only changed files, so the graph stayed incomplete.

Current behaviour:

- `status`, `detect-changes`, `visualize`, `wiki`, `watch`, `forget` and
  `dead-code` do not create a database. Without one they exit with:

  ```
  No graph found at <path>. Run `gryphon build` first.
  ```

- `update` on a missing or zero-node graph runs a full build and prints
  `Full rebuild (no usable incremental base): ...`.

**Fix.** Run `gryphon build`. It always re-parses the whole tree;
there is no `--force` flag. After that, `update`, hooks and `watch` stay
incremental.

## Legacy `.gryphon.db` at the repository root

Very old releases stored the database as `.gryphon.db` in the
repository root. Every command that opens the graph at its default location,
including `forget` and `dead-code`, moves it to `.gryphon/graph.db`
on first use. Nothing else is needed.

## Graph seems stale

- With hooks installed, `update --skip-flows` runs after each Edit/Write and
  the pre-commit hook runs `update` before each commit (not in linked
  worktrees; see above).
- Run `gryphon update`, or `/build-graph` in Claude
  Code, to catch up.
- Check `.claude/settings.json` still has the hooks; `gryphon install`
  rewrites them.

## Watcher is running but the graph stopped updating

`gryphon-daemon status` has a `Watcher` column next to the process `Status`, plus
the age of the last event each watcher processed:

```
  Alias     Status    Watcher   PID       Event   Path
  backend   alive     stalled   48213     3d      /work/backend
```

- `ok`: the filesystem observer is running and publishing a heartbeat.
- `stalled`: the process is up but its observer threads are not, so nothing is
  indexed. Check `gryphon-daemon logs --repo ALIAS`, then `gryphon-daemon restart`.
- `partial`: the watcher ran out of watch slots and fell back to one recursive
  watch. Still complete, but ignored trees are watched again. Raise
  `CRG_MAX_WATCH_SCHEDULES` to get the filtering back.
- `unknown`: the watcher has not published health yet (it just started, or it
  predates this feature).
- `dead`: the process exited. The daemon restarts it with exponential backoff,
  so a repository that cannot start does not repeat a full initial build every
  30 seconds.

A watcher whose observer dies logs an error and exits non-zero so the daemon
restarts it. Deleting a watched directory, or deleting and recreating one, is
ordinary work, not a dead watcher.

Watch mode registers OS watches only for directories that survive the ignore
patterns, so `node_modules/`, `.git/` and build output generate no events.
Optional settings:

- `CRG_MAX_WATCH_SCHEDULES` (default 24): cap on separate watches; a repository
  needing more falls back to one recursive watch on the root.
- `CRG_WATCH_PLAN_DEPTH` (default 3): how deep the planner may split.
- `CRG_WATCH_SPLIT_MIN_DIRS` (default 4): smallest ignored tree worth its own
  watch.
- `CRG_RESTART_BACKOFF` (default 30s), `CRG_RESTART_BACKOFF_MAX` (default 900s),
  `CRG_RESTART_HEALTHY_AFTER` (default 600s): daemon restart backoff.

## `Identity migration pending for ignored file`

Older builds recorded a C++ file that failed to parse as pending an identity
migration. If you then added the file to `.gryphonignore`, every
`update` reported this error and `watch` refused to start until a full rebuild.
Current releases drop the pending entry when the file has no rows in the graph.
If you still see the message, upgrade, or run `gryphon build` once.

## A directory disappeared from the graph

Nested `target/`, `build/`, `.next/` and `.nuxt/` directories are treated as
build output when a sibling manifest says so (`pom.xml`, `Cargo.toml`,
`build.sbt`, `build.gradle`, `build.gradle.kts`, `next.config.*`,
`nuxt.config.*`). Each build logs what it excluded:

```
Excluding 2 nested build-output directories (a sibling manifest marks them as
build output; keep one with '!<path>' in .gryphonignore):
moduleA/target, moduleB/target
```

If one of those is source, keep it with a `!` line in `.gryphonignore`:

```
!moduleA/target
```

`!` lines only opt a path out of this automatic detection; they do not negate
your explicit ignore patterns. `CRG_NESTED_OUTPUT_SCAN=0` turns the detection
off for the whole repository.

## Embeddings not working

- Install the local provider: `pip install "gryphon[embeddings] @ git+https://github.com/jjgouveia/gryphon-standalone.git"`.
- Run `gryphon embed`, or the `embed_graph_tool` MCP tool.
- The first local run downloads the `all-MiniLM-L6-v2` model.
- Cloud providers (`--provider openai|google|minimax|voyage`) read their key
  from `CRG_OPENAI_API_KEY`, `GOOGLE_API_KEY`, `MINIMAX_API_KEY` or
  `VOYAGE_API_KEY`, and print an egress warning until
  `CRG_ACCEPT_CLOUD_EMBEDDINGS=1` is set.

## MCP server won't start

- Run the command from your MCP config by hand, for example
  `<python> -m gryphon serve`, and read the error.
- `install` writes `<python> -m gryphon serve` using the absolute path of
  the interpreter that ran it, or `uvx --from <dist> gryphon serve` when
  `install` itself ran inside an ephemeral `uvx` environment. If the
  interpreter it recorded no longer exists, re-run `gryphon install` from
  the environment you want to use.

## Windows / WSL

- Upgrade to v2.3.6 or later if `daemon status` crashes with WinError 87 (#511)
  or CLI `detect-changes` maps 0 functions on Windows (#528).
- Use forward slashes in paths when passing `repo_root` to MCP tools.
- In WSL, install `uv` inside WSL, not the Windows build:
  `curl -LsSf https://astral.sh/uv/install.sh | sh`. If `uv` is not found
  afterwards, add the directory the installer reports to your `PATH`.
- File watching (`gryphon watch`) may lag on WSL1; use WSL2.
- On native Windows, long paths may need enabling:
  `git config --system core.longpaths true`.

## Community detection requires igraph

- Install with `pip install "gryphon[communities] @ git+https://github.com/jjgouveia/gryphon-standalone.git"`.
- Without igraph, community detection falls back to file-based grouping, which
  is coarser.

## Optional dependency groups

If a tool returns an ImportError, install the relevant group:

- `pip install "gryphon[embeddings] @ git+https://github.com/jjgouveia/gryphon-standalone.git"`: local semantic search
  (sentence-transformers).
- `pip install "gryphon[google-embeddings] @ git+https://github.com/jjgouveia/gryphon-standalone.git"`: Google Gemini embeddings.
  OpenAI-compatible, MiniMax and Voyage AI embeddings use the standard library
  HTTP client and need only their environment variables.
- `pip install "gryphon[communities] @ git+https://github.com/jjgouveia/gryphon-standalone.git"`: igraph-based community
  detection.
- `pip install "gryphon[enrichment] @ git+https://github.com/jjgouveia/gryphon-standalone.git"`: Python call-resolution
  enrichment through Jedi.
- `pip install "gryphon[eval] @ git+https://github.com/jjgouveia/gryphon-standalone.git"`: evaluation benchmarks (matplotlib,
  PyYAML).
- `pip install "gryphon[wiki] @ git+https://github.com/jjgouveia/gryphon-standalone.git"`: installs the `ollama` client. The
  current wiki generator is structural only and does not call it.
- `pip install "gryphon[all] @ git+https://github.com/jjgouveia/gryphon-standalone.git"`: everything above.
