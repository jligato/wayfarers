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
- **Deliverable must be a named file.** If a later reader is meant to use your
  work, you must name the exact file you wrote it into. A chat-only answer counts
  as NOT DELIVERED.
- **Skill loading:** You MAY call `skill_view(name)` to load procedural knowledge. If it stalls more than 30 seconds, abandon it and proceed without the skill. Never let a skill load block your work. (A lookup is fast now — ~0.6 s warm; if you need the file text directly, `read_file` on the skill's `SKILL.md` also works.)

## PREFLIGHT GATE — the dispatcher runs this BEFORE sending any brief

Measured 2026-10-04: four worker tasks were dispatched on false premises and
each burned a full worker run: two named files that did not exist, one named a
compile error in a file shorter than the cited line, one named a module nobody
wrote. Do not repeat them.

**Rule: every path a brief names must exist, and every line number it cites must
be inside that file, before you dispatch.**

Every path, and every cited line number (the count must be >= the highest line
cited), in one call each:

```
cd G:/Projects/wayfarers-cycle
for f in <path1> <path2> ...; do [ -f "$f" ] && echo "OK   $f" || echo "MISS $f"; done
wc -l <path>
```

A `MISS`, or a cited line past the count, means the brief is wrong. Fix it before dispatching.

## BACKUP GATE — run this before any destructive or bulk operation

**Rule: before any destructive or bulk operation, run the backup gate; it must print `VERIFY: PASS`.**
If the gate prints `BACKUP GATE: FAIL`, stop and report — do not proceed with the
operation.

```
PY=C:/Users/Boss/AppData/Local/hermes/installs/3556b0859749568e/environments/7bb77ee6a9b64c07b94c41b54e08e540/venv/Scripts/python.exe
"$PY" C:/Users/Boss/AppData/Local/hermes/profiles/_shared/scripts/backup_gate.py
```

"Any destructive or bulk operation" means anything that deletes or rewrites
files at scale: `git clean`, `git checkout .`, `git reset`, a `rm`, a recursive
copy, a mass search-and-replace, a migration, or a prune of scratch.

**What counts as a pass.** `VERIFY: PASS` on its own line, from
`repo_backup.py`, with the gate's own exit code of 0. The gate exits 0 only if
the backup script both exited 0 and printed that verdict. A missing verdict is
treated as a failure — silence is not a pass.

**Cheap to call often.** If a verified backup already exists and is younger than
N minutes, the gate skips the work and prints `SKIPPED`, exiting 0:

```
"$PY" .../backup_gate.py --if-stale-minutes 30
```

Measured 2026-10-04: a full backup of the Wayfarer's Cycle repo produced
1,106 files / 6.9 MB and printed `VERIFY: PASS`. The same command run seconds
later with `--if-stale-minutes 30` measured the existing archive at 0.1 minutes
old, printed `SKIPPED`, and created nothing — after re-opening and re-checking
that archive.

Why this exists: on 2026-10-04 a single `git clean -fdx` destroyed 330 untracked
files in a repository with no remote, and about half of `server/` was untracked.
Recovery worked only because a backup happened to exist at the time. Backups
must be automatic, not remembered.

## CLIENT PARSE GATE — run this before accepting any client change

**Rule: no change under `client/` is accepted until `tools/gd_parse_gate.sh` exits
0.** It hands every `.gd` file to Godot's own parser in headless check-only mode,
and it refuses to call a file clean unless Godot really parsed it.

```
cd G:/Projects/wayfarers-cycle
bash tools/gd_parse_gate.sh; echo "GD PARSE GATE EXIT=$?"
```

**What counts as a pass.** Exit code 0, plus a summary line of the form
`PARSE GATE: N files, C clean, A autoload-artifact, E errors` with `E errors` = 0.
Exit 1 means at least one genuine parse error. Exit 2 means the harness could not
run at all (Godot missing, no files found) and that is NOT a pass — fix the harness
first. Silence is not a pass.

**Narrow the loop while you work, widen it before you hand back.**
`bash tools/gd_parse_gate.sh scripts/ui/` audits one subtree. Run the full gate with
no argument before you report the work done.

**The `A autoload-artifact` number is expected, not a defect.** In `--script` mode
Godot does not register the project's `[autoload]` singletons, so every reference to
GameState, APIClient, Analytics or SessionPersistence reports `Identifier not found`.
The gate reads the autoload names out of `client/project.godot`, so a newly added
autoload is recognised without editing the gate.

**If Godot is not on PATH**, point the gate at it with `GD_GATE_GODOT=/path/to/godot`.
Never skip the gate because the tool was missing — an unmeasured client is not a
verified one.

Why this exists: the parse audit that established the client state lived in a scratch
directory that is pruned after 24 hours, so nothing on the repo repeated it and every
later client edit re-ran the question from scratch. The gate is that audit, versioned
and re-runnable. Task 012-288 in `design/task-hierarchy/07-client-ui.md`.

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

- **Set TEMP/TMP explicitly before any scratch work.** A native interpreter
  reads `TEMP`/`TMP`, NOT the shell's `TMPDIR`. Measured 2026-10-05: briefs
  said "use the profile scratch folder", `TMPDIR` was unset, so every worker's
  scratch tree landed in the system temp folder instead. 101 GB accumulated and
  outlived the processes, the delegations and the session.
  ```
  export TMPDIR="$SCRATCH" TEMP="$SCRATCH" TMP="$SCRATCH"
  ```
  Use Windows-style forward-slash paths for `TEMP`/`TMP` so a native tool can
  read them.

- **NEVER duplicate the repo to prove a test can fail.** The `fleet-io-guard`
  plugin refuses it, and it is the single biggest cause of the temp bloat.
  Measured: one such tree was 20 GB across 44,632 files. Use the zero-copy
  proof instead: patch the module object in `sys.modules` in memory, run the
  test, then restore. Nothing is written to disk.

- **Run any long test through the memory cap.**
  ```
  python tools/run_test_capped.py --mem-gb 4 --timeout 300 -- <your pytest cmd>
  ```
  Measured 2026-10-05: an uncapped sweep reached 30 GB of memory and had to be
  killed by hand. The cap turns that into a clean stop.

- **NEVER run a whole-tree test sweep.** Measured 2026-10-05: two workers ran
  `pytest server/tests/` and a ~300-file sweep at the same time; one python
  process reached **10.2 GB** working memory, 15.7 GB was held across 39
  processes, and the host thrashed on disk IO. Every test module loads the whole
  app, so the cost is the size of the tree, not the size of the test.

  Run **one test file at a time**:
  ```
  env -u WC_PG_DB -u WC_PG_HOST -u WC_PG_PORT -u WC_PG_USER -u WC_PG_PASSWORD \
    "$PY" -m pytest server/tests/test_<module>.py -q --no-header -p no:randomly
  ```
  A handful of explicit files is fine. A bare directory (`server/tests/`), the
  repo root (`.`), or 25+ file paths in one command is refused by the
  `fleet-io-guard` plugin. To sweep the whole tree **on purpose**, use the capped
  wrapper, which enforces a hard memory cap and a wall-clock timeout:
  ```
  bash tools/wc_pytest.sh server/tests/
  ```
  A refused sweep is normal and expected - narrow the command, do not retry it.
- **Do NOT use the `patch` tool for small edits.** It costs about 100
  seconds per call on this host regardless of edit size. Do the edit inside
  `execute_code` with a byte-level read/replace/write instead, then re-read
  and assert the change landed. Use `patch` only when you genuinely need its
  fuzzy matching (whitespace drift, a block you cannot reproduce exactly) --
  then the 100 seconds buys something.
- **Report your disk footprint.** In your closeout, state the path and
  approximate size of anything you created outside the repo.

## DELIVERABLE DISCIPLINE - a report is a file, not a chat message

Measured 2026-10-05: several batches of workers reported success and produced
nothing durable. The brief asked them to "produce an inventory" without naming
a file, so each one answered in chat; the session log truncated most of the text,
and one batch lost 24 of its 26 findings. The work was done and then discarded.

**Rule: a deliverable intended for later consumption must name its file, and any
long report must be written to a file rather than returned as chat text.**

- Inventories, audits, tables of findings, test logs, and diffs go to a file
  under the repo or the scratch dir. Pick the path yourself if the brief did not
  give one, then state it.
- The closeout chat reply is a summary, not the deliverable. Keep it short and
  point at the file.
- Report the file's path and approximate size in your closeout. If you wrote
  nothing to a file, say so plainly instead of describing findings in prose.

## REPORTING

- If you run a command that may take over 60 seconds, print a one-line
  progress message BEFORE it starts and AFTER it returns. Do NOT spawn
  background ticker processes or `while` loops — they never exit and leak
  processes. Your ordinary tool calls keep the task alive.
- State measured numbers, never adjectives. "71 passed, 6 of 6 mutants killed",
  not "tests look good".
- A refuted premise is a valid result. If the brief names a file or defect that
  does not exist, say so with the evidence and change nothing.
