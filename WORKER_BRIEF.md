# Worker brief — Wayfarer's Cycle (gamedev fleet)

Copy the block below into every `delegate_task` `context` field. Add task-specific
lines on top; never remove a line from this block.

## IO CONSTRAINTS (do not deviate)

- **Repo:** `G:/Projects/wayfarers-cycle`. Never edit outside your owned file(s).
- **Runner:** `tools/wc_pytest.sh` for every test run. Never bare `pytest`.
- **Env:** `PYTHONDONTWRITEBYTECODE=1`, `--import-mode=importlib`.
- **Probe python:** `C:/Users/Boss/AppData/Local/hermes/installs/3556b0859749568e/environments/7bb77ee6a9b64c07b94c41b54e08e540/venv/Scripts/python.exe`
- **Scratch:** `C:/Users/Boss/AppData/Local/hermes/profiles/gamedev/cache/scratch/`. Probes and copies live HERE, never in the repo.
- **Budget:** 50 tool calls maximum.
- **Forbidden:** `delegate_task` (no nested subagents), any `git commit`, `git add -A`, `git clean`, `git checkout`, `git stash`, `git reset`.
- **Skill loading:** You MAY call `skill_view(name)` to load procedural knowledge. If it stalls more than 30 seconds, abandon it and proceed without the skill. Never let a skill load block your work.

## COPY DISCIPLINE — read this before making any copy

A full repository copy costs ~280,000 files, most of them Android APK test
fixtures under `test-apks/`. Measured 2026-10-04: several workers copied the whole
tree for tasks that needed one module, and the copies alone were the largest
resource cost of the session.

**Copy only what your task needs. Never copy the repository root.**

| Your task | What to copy |
|-----------|--------------|
| Edit and test one module | The module plus `server/tests/` and the one test file you run |
| Mutation control on one module | That module alone, into its own scratch subdir |
| Anything else | Ask yourself whether you need a copy at all — often you do not |

**Never copy `test-apks/`, `builds/`, `rust/*/target/`, `__pycache__/`, or `.venv/`.**

**Use the scoped-copy tool instead of `cp -r`.** It refuses the copies that
caused the storm, so you do not have to remember the rules:

```
PY=C:/Users/Boss/AppData/Local/hermes/installs/3556b0859749568e/environments/7bb77ee6a9b64c07b94c41b54e08e540/venv/Scripts/python.exe
"$PY" C:/Users/Boss/AppData/Local/hermes/profiles/_shared/scripts/scoped_copy.py check <src> <dest>
"$PY" C:/Users/Boss/AppData/Local/hermes/profiles/_shared/scripts/scoped_copy.py copy  <src> <dest>
```

It refuses, with a reason: the repository root, anything containing the repo
root, any excluded directory, a destination outside the scratch root, and any
copy over 5,000 files. Use `budget <dest>` to report what you wrote.

**Prefer in-memory substitution to a copy.** Patching a module object from a
pytest plugin (`sys.modules`) proves a test bites without writing any file. Use it
first; fall back to a copy only when the module cannot be substituted.

**Name your scratch subdir uniquely** — `cache/scratch/<task>-<your-id>/`. Several
workers share this directory and collisions have silently clobbered probe files.

**Delete your scratch copy when you finish**, or state in your closeout that you
left it and where.

## IO DISCIPLINE — this machine has a shared disk and shared CPU

Measured 2026-10-04: many concurrent workers doing bulk file work saturated disk
read/write and the owner had to restart the machine. These rules are not
suggestions.

- **Never run a recursive scan over a large tree.** No `find`, `du`, `grep -r` or
  a Python `os.walk` across the repo root or the scratch root. They are slow,
  they touch directory mtimes, and they defeat the scratch auto-prune. Measure
  one directory, or use a targeted `search_files` call.
- **Never `rm -rf` a tree as part of a task.** Report what should be deleted
  instead; the orchestrator deletes, paced, one item at a time.
- **Deletions use the fast tool, never `rm -rf` or `shutil.rmtree`:**

  ```
  PY=C:/Users/Boss/AppData/Local/hermes/installs/3556b0859749568e/environments/7bb77ee6a9b64c07b94c41b54e08e540/venv/Scripts/python.exe
  "$PY" C:/Users/Boss/AppData/Local/hermes/profiles/_shared/scripts/scratch_rm.py list
  "$PY" C:/Users/Boss/AppData/Local/hermes/profiles/_shared/scripts/scratch_rm.py rm <name>
  ```

  Measured 2026-10-04: `rm -rf` over a 280,000-file directory takes minutes and
  saturates the disk; the same delete through this tool takes under a second,
  because it uses Windows extended-length paths. `list` never walks the tree.
  One entry per call — never loop it in a single command.
- **Keep total disk writes small.** If a step would write more than a few
  megabytes, stop and say so in your closeout rather than doing it.
- **Do not run more than one long test invocation at a time** within your task.
  Serialise them; a parallel storm from one worker is the same problem as a storm
  from many.
- **Report your disk footprint.** In your closeout, state the path and
  approximate size of anything you created outside the repo.

## REPORTING

- If you run a command that may take over 60 seconds, print a one-line
  progress message BEFORE it starts and AFTER it returns. Do NOT spawn
  background ticker processes or `while` loops — they never exit and leak
  processes. Your ordinary tool calls keep the task alive.
- State measured numbers, never adjectives. "71 passed, 6 of 6 mutants killed",
  not "tests look good".
- A refuted premise is a valid result. If the brief names a file or defect that
  does not exist, say so with the evidence and change nothing.
