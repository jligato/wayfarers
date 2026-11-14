# Delegation Batch Mining — Worker Brief

Standing workflow: task 012-193 in `design/task-hierarchy-upcoming-2026-10-04.md`.

## Why this exists

The orchestrator cannot read delegation batches one at a time. Each batch costs a full
turn and produces roughly fifteen new tasks, so the backlog grows faster than it drains.
Subagents do the reading; the orchestrator only reviews the *findings*.

## Your input — read these exact paths, nothing else

One batch = one folder:

```
C:/Users/Boss/AppData/Local/hermes/profiles/gamedev/cache/delegation/live/<deleg_id>/
```

Inside it:

```
manifest.json          the batch's own index and per-task status
task-<N>.log           one file per worker; N starts at 0
```

Re-measured 2026-10-05: **661** batch folders exist under `delegation/live/`. You are given
one `deleg_id` and you read that one folder. Never enumerate the parent directory to
"get oriented" — listing 661 folders is wasted budget.

`manifest.json` carries `delegation_id`, `started`, `task_count`, and a `tasks[]` array
with each task's `index`, `goal`, `log` (absolute path), `status`, `exit_reason`, and
`error`. Read it first; it tells you how many logs exist and which workers failed.

## Never scan the repository

Your job is reading text files in the delegation cache. It is **not** a repository task.

- Do **not** run `find`, `grep -r`, `du`, or any Python `os.walk` over
  `G:/Projects/wayfarers-cycle` or over the delegation cache parent.
- Do **not** open a repo file to "check whether the worker's claim is true". The worker
  already measured it. Your job is to report what they measured, with its proof.
- Do **not** edit, create, or delete anything under `G:/Projects/wayfarers-cycle`.
- The only repo file you may read is
  `design/task-hierarchy-upcoming-2026-10-04.md`, for the dedup step below.

A repo-wide scan on this tree is the single most expensive mistake available to you: the
root holds roughly 280,000 files, mostly Android APK test fixtures.

## How to actually find the closeout (measured, do not guess)

Each `task-<N>.log` is an **append-only timestamped trace**, not a markdown document.
Every line is prefixed with a clock time and a stream label:

```
10:02:56 assistant| <text>
10:02:56 final    | status=completed duration=1441.65s summary: <the closeout>
12:45:26 final    | end status=completed exit_reason=completed
```

Because of that prefix, a heading is never at the start of a line. Measured 2026-10-05
over 12 batch logs: `grep -c '^#'` matched **0** lines in total, and
`grep -c '^## Objectives'` matched **0** — while the unanchored `grep -c '## Objectives'`
matched **16**. So an anchored grep silently reports "nothing here" on a log that
plainly contains the closeout.

**Use an unanchored pattern, or skip grep and read the tail.** Both work:

```
grep -c '## Objectives' "<log>"        # unanchored - finds it
tail -40 "<log>"                       # simplest, no escaping worries
```

Prefer the tail. The closeout is the `assistant|` and `final|` lines near the end.
Measured over the same 12 logs: task logs run 17 to 324 lines. Read only the tail of each
log, and open an earlier part **only** when the closeout names a finding you cannot
interpret without its context.

## Step 0 — Dedup FIRST (do not skip)

Read `design/task-hierarchy-upcoming-2026-10-04.md` and build the set of findings it
already records. A finding that names a file or symbol already covered by an existing
row is **not a new task** — it is a confirmation. Report confirmations separately in a
short list; do not create duplicate rows.

Measured 2026-10-05: the plan holds **214** task headings, numbered `### 012-001` through
`### 012-308`. (An earlier draft of this brief claimed 121 rows; that number was wrong.)
Count them yourself before you report, so your closeout carries a measured figure rather
than a remembered one.

A finding that names a file or symbol already covered by an existing row is **not a new
task** — it is a confirmation. Report confirmations in a short separate list; do not
re-number them. Re-adding known defects under new numbers is the single biggest failure
mode of this workflow.

## What to extract — only these three classes

1. **Defects** — crashes, security holes, money leaks, broken guards, silent failures,
   unreachable code, wrong status codes, missing guards.
2. **Landed work** — artifacts a worker wrote to disk that no task records yet:
   test files, SQL migrations, design notes, patches, skills. Name the path.
3. **Contradictions** — two sources disagreeing (e.g. a count of 19 vs 46 vs 55), or a
   claim that measured false.

## What to IGNORE

- Worker self-assessment scores (phase-1 / phase-2 grades).
- Heartbeat shortfalls and process narration.
- "What worked" prose and tool-usage commentary.
- Anything already present in the hierarchy.
- Cosmetic observations with no defect behind them.

## Output format — every finding needs a file and a line

Report plain text to the orchestrator. One block per finding:

```
CLASS: defect | landed-work | contradiction
FILE: <path>:<line>
WHAT: <one or two sentences, plain words, what is wrong>
EVIDENCE: <how the worker proved it — command run, test name, measured value>
SEVERITY: critical | high | medium | low
```

**The `FILE:` line is mandatory and must name one exact path and one exact line number
that proves the finding.** A finding without a proving line is not a finding; drop it, or
put it under `CONFIRMATIONS:` and say why you could not cite one. Do not write
`FILE: <path>:<line>` back as a literal template, and do not write "several places in
`player_store.py`" — the orchestrator cannot act on that.

If the worker cited no line for a defect it reported, say so in `WHAT:` and lower the
severity. An uncited defect quoted from a closeout is a claim, not a measurement.

Then a short `CONFIRMATIONS:` list of findings already in the plan.
Then `BATCHES_READ:` with the deleg ids you covered.
Then `PLAN_ROWS_COUNTED:` with the row count you measured in step 0.

Do **not** write to the hierarchy. The orchestrator is its single writer.

## Safety rules

- **NEVER spawn subagents.** Do not call `delegate_task` in any form. You are a leaf
  worker. Fanning out drives the fleet to its concurrency cap and the orchestrator loses
  control of the count. Do the reading yourself, in this session.
- Do **not** call `skill_view`. It stalls 50-100+ seconds per call and you do not need it
  — the tail of the log already says what the worker did.
- Scratch files go in `C:/Users/Boss/AppData/Local/hermes/profiles/gamedev/cache/scratch/`
  with a name unique to your batch id. Several workers share that directory, and
  collisions have silently clobbered files. You will not normally need a scratch file
  at all — the logs are the deliverable.
- Never run `git add`, `git commit`, `git clean`, `git checkout`, `git reset`, `git stash`.
  Roughly half of `server/` is untracked and there is no remote.
- If you run a command that may take over 60 seconds, print a one-line progress
  message BEFORE it starts and AFTER it returns. Do NOT spawn background ticker
  processes or `while` loops to "stay alive" — they never exit and leak processes.
  Your ordinary tool calls keep the task alive.

## Volume — ONE batch per worker

One subagent covers **exactly one** batch folder. Do not accept a range of batches.

Read the batch's **closeout output**, not every byte of the folder:
1. Read `manifest.json` for the batch's index and per-task status.
2. Read the **tail** of each `task-N.log` — the worker's final report. That is where the
   defects, landed work, and contradictions live. Use `read_file` with an offset near the
   end, or `tail -40`. If you grep for a heading such as `## Objectives`, drop the `^`
   anchor: measured 2026-10-05, the anchored form matches 0 lines on 12 of 12 logs while
   the unanchored form matches 16.
3. Read earlier parts of a log **only** when the closeout references something you must
   verify.

Reading whole logs for 8-15 batches is what made the first attempt blow past the fleet
cap. One batch, closeout first.

## What makes a good finding

A finding is worth a row only if someone could act on it. "Worker reported a heartbeat
shortfall" is not actionable. "`server/daily_streak.py:claim()` reads `total_qi_earned`
with a bare `float()`, so a stored text value raises and escapes to the caller" is.
