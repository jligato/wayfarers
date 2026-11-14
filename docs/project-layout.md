# Target Project Layout — Wayfarer's Cycle

Status: **proposal, not executed.** Nothing in this document has been applied. Every number
below was measured on `G:/Projects/wayfarers-cycle` during the session that wrote it and
carries the command that produced it. Re-measure before acting: the tree is a shared
worktree with live agents and the counts move.

**Scope note.** A separate agent owns the content-hash merge plan for
`C:` vs `G:`. This document deliberately does **not** duplicate that work. §6 covers only
what the *layout* must look like once the merge lands, and §7 flags where layout decisions
depend on that merge.

```wc-facts
# Machine-checked facts. Verify: python tools/check_doc_staleness.py
# Regenerate with --update, then READ THE DIFF before committing.
# `git.dirty_paths` is a live number: every edit to this file changes it, so it
# is compared with a tolerance rather than pinned exactly.
# Every count asserted in sections 2 and 3 is one of these keys.
artifacts.apk_size_on_disk = 52465118
artifacts.tracked_apks = 0
coding_attempts.dirs = 70
coding_attempts.subdirs_with_tracked = 48
coding_attempts.tracked = 85
fixtures.emulator_interruption_tracked = 162
fixtures.goal_receipt_android_tracked = 2564
git.commit_count = 9
git.dirty_paths = 323
git.has_github_workflows = false
git.head = d59f876
git.remotes = 0
git.tracked_files = 3375
gitignore.c_covers_buildpass = false
gitignore.c_covers_keys = false
gitignore.c_covers_keystore = false
gitignore.c_size_bytes = 564
gitignore.g_covers_buildpass = true
gitignore.g_covers_keys = true
gitignore.g_covers_keystore = true
gitignore.g_size_bytes = 4575
leak.executable_nonbuild = 8
leak.files = 56
leak.files_nonbuild = 26
mirror.branch = master
mirror.untracked = 7
project.project_location_json_exists = false
secrets.any_credential_files = 0
secrets.pg_password_files = 0
secrets.release_password_files = 0
secrets.release_password_files_nonbuild = 0
secrets.tracked_key_files = 0
```

**How the numbers in this document went wrong before, and how they stay honest now.**
The last revision of this document reported the secret exposure as "two committed
secrets" — a *filename* count — when a content scan finds **100** files carrying a live
credential. It also reported 86 path-leaking files (the root-commit number) as if it
described the current tree, when HEAD has 85. Both errors had the same cause: a number
was measured once, written into prose, and never re-checked. The block above is the
checkable form of the same measurements; `python tools/check_doc_staleness.py` fails the
moment one of them stops being true. **Prefer a key from that block over a bare number
in the prose below.**

---

## 0. The one fact that reframes everything

The canonical root is now `C:\Users\Boss\Projects\wayfarers-cycle` and `G:` is being
retired. Measured **during this session**, the `C:` tree is:

```
C:/Users/Boss/Projects/wayfarers-cycle
├── .git/                 ← real repo, but ZERO commits ("master" has no commits yet)
├── .gitignore            ← 39 lines, DIFFERENT rules from G:'s 60 lines
├── PROJECT_LOCATION.json
├── memory/               (1 file)
├── src/                  (904 files)
└── tests/                (1 dir: goal_receipt_android)
```

Evidence: `git log --oneline` in `C:` → `fatal: your current branch 'master' does not have
any commits yet`. `git ls-files | wc -l` → `0`. `du` of `C:` total files → `904`, all
under `src/` and `memory/`.

**The move has not happened.** `G:` is still the only tree with history: HEAD `93863c0`,
3,408 tracked files. This document therefore describes the `G:` layout (the real one) and
proposes a target that is also valid for `C:`.

### 0.1 `C:/src/` already contains three divergent copies of the Godot client

This is the single largest unknown in the migration and needs a decision before any
`MOVE` runs. Measured inside `C:.../src/`:

| Copy | `.gd` files | `.tscn` files | Hash of `scenes/game/MainHub.gd` | Hash of `scripts/autoload/GameState.gd` |
|---|---|---|---|---|
| `src/client_ui/` | 71 | 25 | `7b4ed52c` | `ca60489b` |
| `src/ui_panels/` | **270** | 23 | `61affcba` | `868b531d` |
| `src/ui_probe/` | 71 | 25 | `7b4ed52c` | `ca60489b` |
| `src/scenes/` + `src/scripts/` (loose, alongside) | 31 + 39 | — | — | — |

- `client_ui` and `ui_probe` are **byte-identical to each other** (same hashes on both
  sampled files). One is a redundant copy of the other.
- `ui_panels` is **divergent**: 49 files identical to `client_ui`, **16 files present at
  the same path with different content**, and **221 files that exist only in `ui_panels`**
  (measured by content-hash `comm`/`join` over all `.gd`).

`G:/client/` has 73 `.gd` files — the same order of magnitude as `client_ui`, *not* as
`ui_panels` (270). So `ui_panels` is very likely the most complete client, and
`client_ui`/`ui_probe` are two copies of an older or partial one.

> **UNSURE — needs an owner's decision, do not guess.** Which of the three is canonical?
> The evidence says `ui_panels` has 4× the scripts, but the 16 divergent shared-path files
> mean it is *not* a strict superset. A file-by-file diff against `G:/client/` is required
> before deleting either copy. I am not making that call.

---

## 1. Measured inventory of `G:/Projects/wayfarers-cycle`

`du -sh` + `find | wc -l` per top-level directory. **7.2 GB total**, of which **~1.4 GB is
generated and reproducible**.

| Directory | Files on disk | Size | Git-tracked | Purpose |
|---|---|---|---|---|
| `test-apks/` | **272,767** | **5.0 G** | **0** | Extracted third-party competitor APKs |
| `tests/` | 6,110 | 1.6 G | 2,911 | Godot/Flutter client + integration test suites |
| `server/` | 320 | 5.8 M | 117 | FastAPI backend |
| `client/` | 238 | 1.1 M | 159 | Godot 4 client (the shipped game) |
| `coding-attempts/` | 181 | 62 M | 85 | Per-task agent scratch/evidence dumps |
| `research/` | 67 | 5.3 M | 67 | Competitor wiki research |
| `build/` | 21 | 441 M | 0 | Signed APKs + build logs |
| `playtest/` | 22 | 139 K | 19 | Human playtest kit (briefs, forms, CSVs) |
| `balance/` | 17 | 194 K | 9 | Balance model + generated reports |
| `docs/` | 11 | ~180 K | 5 | Specs, audits, roadmap |
| `shared/` | 7 | 64 K | 6 | JSON data: realms, sects, items, NPCs, zones |
| `_wc_baseline/` | 12 | 293 K | 0 | Point-in-time backups + verbose logs |
| `memory/` | 7 | 141 K | 2 | Verify-violation ledgers + manual backups |
| `scripts/` | 2 | 5.0 K | 2 | `build_release.py`, `build_release.sh` |
| `cron/` | 1 | 4.0 K | 1 | `weekly_events.sh` — scheduler entrypoint |
| `cron-logs/` | 1 | 4.0 K | 1 | One weekly log |
| `_build_test/` | 2 | 56 M | 0 | Test APK + idsig |
| `__pycache__/` | 7 | 64 K | 0 | Python bytecode cache |
| `keys/` | 2 | 9.0 K | **2** | Release keystore + build password |
| `.pytest_cache/` | — | — | 0 | Pytest cache |

### 1.1 Root-level files (29 tracked + untracked)

| File | Tracked | Verdict |
|---|---|---|
| `phase1_verify.py`, `_v2`, `_v3`, `_final`, `_turn1`, `_turn2`, `_turn2_fixed`, `_turns3to7`, `phase1_v4.py`, `phase2_validate_turn1.py`, `verify_turn2.py` | **yes (11 files)** | **DELETE/MOVE** — confirmed present |
| `test_integration.py`, `test_offline.py`, `test_lifecycle_offline.py`, `test_wallet_integration.py` | yes (4) | **MOVE** — real tests, wrong location |
| `check_gd.py` | yes | **MOVE** — 9-line GDScript parse helper |
| `test_base64.gd` | **no** | **DELETE** — content differs from `client/test_base64.gd` |
| `tower_http_probe.log` | **yes** | **DELETE** — 7-line log |
| `game-dev-rounds.md` | yes | **MOVE** → `docs/` |
| `conftest.py` | **no** | **KEEP at root — required** (see §2) |
| `docker-compose.yml` | yes | KEEP at root — required |
| `requirements-test.txt` | yes | **MOVE** → `server/` (see §3, unsure) |
| `README.md` | yes | KEEP at root |
| `_diag.py`, `_probe_contract.py`, `_wc_verbose.sh` | no | **DELETE** — scratch |
| `_gap_client_paths.json`, `_gap_client_refs.json`, `_gap_diff.json`, `_gap_routes.json` | no | **DELETE** — 987/616/799/793-line one-shot audit dumps |

**Confirmed: all 11 `phase1_verify*` scripts are committed.** They are turn-by-turn
verification scratch from a finished phase — 1,327 lines total across 9 files
(`phase1_verify*.py`) plus `phase2_validate_turn1.py` and `verify_turn2.py`.

`test_base64.gd` at root is a **stale duplicate**: `md5sum` gives
`e7cf272b…` for the root copy vs `50b62718…` for `client/test_base64.gd`. Different content,
same name, root copy untracked.

---

## 2. Problem inventory (with evidence)

### P1 — 272,767 files / 5.0 GB of competitor APK extraction, in the working tree
`test-apks/inputs/` holds 11 third-party APKs (951 MB `skyblade.xapk`, 376 MB
`battle_through_heavens_aarSubAsset.apk`, 97 MB `immortal-taoists-1-8-8.apk`, …).
`test-apks/outputs/` holds 8 `decoded-*` trees. `git ls-files test-apks/` → **0 files**;
`.gitignore:4` already excludes it.

**Verdict: IGNORE, never commit — but MOVE off the repo root.** It is correctly ignored
today, yet it dominates disk (5.0 G of 7.2 G) and sits inside the directory being retired.
It is also legally sensitive: extracted assets from other companies' games.

### P2 — 2,578 committed Godot build fixtures (76% of all tracked test files)
`tests/goal_receipt_android/` = 1.5 GB on disk, **2,594 tracked files**, of which
**2,578 live inside 31 `build-<timestamp>/` directories** (~51 MB each). The real test
harness is **16 tracked files, 1.2 MB** — every one of them is at the top level of
`tests/goal_receipt_android/`, not inside a `build-*` dir:

```
AcceptanceObserver.gd  build.py  clock.json  device.log  latest-build.json
serve.py  wd3.xml  window_dump.xml
logcat-after-cold-restart.txt  logcat-post-idle.txt
screen-after-cold-restart.png  screen-current.png  screen-goal.png
screen-name.png  screen-pinned.png  screen-start.png
```

Of those 16, only 4 are source or harness (`AcceptanceObserver.gd`, `build.py`, `serve.py`,
`clock.json`); the other 12 are captured evidence from one device run (logcat, screenshots,
UI dumps). See §6 item 6 — whether that evidence is worth keeping untracked is undecided.

`git check-ignore` on a path inside a `build-*` dir → **exit 1, not ignored.** The existing
`.gitignore` blocks `tests/*/fixture_project*/` and `tests/*/*_stale/`, but nothing matches
`build-*`. Every acceptance run writes a new 51 MB tree that becomes committable.

**This is the highest-value single fix in the repository.**

### P3 — 163 committed files under `tests/emulator_interruption/`
60 MB `current_build/` (a full Godot export tree) + `runs/` (1.6 MB) + `cleanup.log`, all
tracked. The real probe is **6 tracked files**: `run.py`, `probe.py`, `build_current.py`,
`latest.json`, `README.md`, and `memory/verify-violations.json`.

### P4 — Probe and scratch scripts left inside source folders
Confirmed present, all untracked except where noted:

| Path | Lines | Note |
|---|---|---|
| `server/_probe_inner.py` | 8 | **confirmed** — inline `TestClient` route dump, imports `server.main` |
| `server/_cf.bak` | — | backup of a config file |
| `server/_bak_reward_verify/` | 2 files | `battle_pass.py.bak`, `sign_in.py.bak` |
| `server/coding-attempts/` | 1 file | `lifecycle-v1.json` — scratch in a source tree |
| `tests/_probe_routes.py` | — | tracked |
| `tests/_audit_client_refs.py` | — | tracked |
| `tests/beast_integration_drive.tmp.gd` | — | tracked, `.tmp.` in the name |
| `tests/beast_integration_run.tmp.py` | — | tracked, `.tmp.` in the name |
| `memory/_probe_lb_payout.py` | 962 B | probe in a data dir |
| `memory/_bak_main.py_before_streak_routes` | 79 KB | manual backup of a source file |
| `memory/_bak_MainHub.gd_before_bootscene` | 24 KB | manual backup of a source file |
| `client/export_presets.cfg.bak` | — | Godot backup |
| `client/scenes/game/MainHub.tscn.bak-step4` | — | untracked backup |
| `client/scripts/autoload/_GameState.gd.bak` | — | untracked backup |

`server/` also carries **18 `.log` files at its top level** — more scratch than source
folders. Two are committed (`legacy-pg-clock-skew-verification.log`,
`legacy-pg-repeat-verification.log`); the other 16 are untracked and are mostly one-shot
probe output: `route_probe{,2,3,4}.log`, `tree_probe{,2}.log`, `confirm{,2,3}.log`,
`inc_probe.log`, `live_goal.log`, `reconcile.log`, `restart-test-server.log`,
`diffwalk.log`, `final_count.log`, `triangulate.log`. None is read by any test
(`grep` for their names returns no importer) — they are transcript debris from debugging
sessions that lived in the source tree.

### P5 — A 51 MB APK committed as "evidence"
`coding-attempts/release-audit/installed.apk` — 52,465,118 bytes on disk. Measured
`git ls-files | grep -c '\.apk$'` → **0**: it is **not tracked**. `.gitignore:8` has
`*.apk` and it landed before first commit, so unlike the `keys/` files it was never
committed. It still consumes 50 MB of disk and belongs in neither the repo nor the
archive — but "tracked" was wrong, and only `git ls-files` settles that.

### P6 — Committed release secrets ✅ UNTRACKED — rotation still required
`keys/wayfarers-release.keystore` and `keys/.buildpass` were **tracked from commit `93863c0`**
onward. **Both are now untracked** (`git ls-files keys/` → empty; neither is in HEAD). The
files remain on disk, which is what untracking means: nothing was deleted, git simply stops
carrying them. Commit `0ef873c` (2026-09-28) did it, together with the `*.bak` family rule
that makes the removal stick for `client/export_presets.cfg.bak` — a path the
`tests/<runner>/` build-tree rules never matched, so untracking it alone would have been
undone by the next broad `git add`.

**The name-based count of 2 was wrong by 50x.** Scanning contents rather than filenames found
86 tracked files carrying the release password, not 2: the rest were `config_notes.txt`-shaped
harness files, captured emulator logs, and the `.bak` preset copies embedding
`keystore/release_password`. **That count is now 0.** `secrets.any_credential_files` fell
100 → 14, and the 14 are all `pg_password_files`, not the signing key.

`.gitignore` **does** cover `keys/`, `*.keystore`, `*.jks`, `*.p12`, `*.buildpass` — added in
`a99426b`. That rule fixed nothing on its own, because an ignore rule never untracks an
already-tracked file; the work was `git rm --cached`, which is now done.

**What is NOT fixed: the password is still in git history.** `93863c0:keys/.buildpass`
resolves. Untracking only stops the *next* commit carrying it forward. **Rotate both
credentials before the first push to any remote** — that is the step that actually closes this.
Independently documented at `docs/git-workflow.md` §1.1.

### P7 — 25 tracked files hardcode `G:/Projects/wayfarers-cycle` (7 are executable)
Measured over tracked files excluding `build-*` fixtures (`git grep -l -F -I
"G:/Projects/wayfarers-cycle" HEAD | grep -v '/build-' | wc -l` → **25**). Of those,
**7 are executable** (`.py` / `.sh` / `.gd`) and 18 are docs, logs, or JSON evidence.
The 7 are the real breakage:

```
cron/weekly_events.sh                       ← a scheduled job
server/check_ghost_battle_persistence.py
server/integration_script.py
server/check_pvp_persistence.py
test_integration.py                         ← os.chdir(r'G:/Projects/wayfarers-cycle')
tests/ghost_battle_integration/run.py
tests/offline_recap/trace.py
+ 18 more (docs, playtest, client/export_presets.cfg.bak, coding-attempts logs,
  tests/*/README.md, tests/*/coding-attempts/.../attempts.json)
```

Total across the whole tracked tree: **85 files**, of which 60 are inside `build-*`
fixtures that P2 deletes anyway. **25 remain**, of which **7 are executable** and need
path parameterisation; the other 18 are documentation that merely records the old path
and needs a one-pass find-and-replace at most.

> Earlier revisions of this document said 86 total / 27 non-fixture / 8 executable.
> The correct per-commit figures are 87 at the root commit, 86 for the three commits
> after it, and **85 at HEAD** (measured `git grep -l -F -I "G:/Projects/wayfarers-cycle"
> <sha> | wc -l` per commit). The 87 was the root-commit number reported as if it
> described the current tree. Re-run the commands rather than trusting any of these
> numbers; the *shape* (a scheduled job and a `sys.path` shim are among them) is what
> matters.

`test_integration.py:11` is the worst case: `os.chdir(r'G:/Projects/wayfarers-cycle')` — the
test suite cannot pass on `C:` until this is fixed. `cron/weekly_events.sh` is worse: it
will silently stop firing after the move, with no error.

### P8 — Duplicated verification reports
`docs/verification-report.md` (2,032 B) and `docs/verification-report-2026-09-25.md`
(1,082 B) — different content (`md5` differs), both tracked. Dated copies of the same
artifact, both live.

### P9 — `coding-attempts/` mixes useful logs with binaries
181 files, 62 MB, 85 tracked. Tracked content includes `suite_clean.log`,
`suite_final.log`, `suite_isolate.log`, `suite_rerun.log`, `battle_pass.py.pristine`,
(`.pristine`), `test_missions.py.pristine`, `ledger-turn-2026-09-26-suite-green.json` — genuine
reproducible evidence. Mixed in: the 51 MB `installed.apk` (P5) and an 8.7 MB
`emulator-check/`. 70 subdirectories exist; **48 contain tracked files, so 22 are wholly
untracked** (measured `ls -1d coding-attempts/*/ | wc -l` → 70 vs
`git ls-files coding-attempts | cut -d/ -f2 | sort -u | wc -l` → 48).

### P10 — `test_integration.py` at root is a subprocess smoke test, not a pytest file
It starts a server with `subprocess` and probes it over `urllib` — no `def test_` functions.
It is a manual harness that `pytest` will import (module-level code runs at collection) and
that hardcodes the `G:` path.

---

## 3. Proposed target layout

```
wayfarers-cycle/
├── README.md
├── PROJECT_LOCATION.json          # DOES NOT EXIST YET — machine-readable
│                                 # root/interpreter/db location (proposed, P-kernel)
├── .gitignore                     # MERGE of the G: and C: rule sets
├── docker-compose.yml             # wc-postgres + wc-api
├── conftest.py                    # root-level: dual-import test isolation (REQUIRED)
│
├── server/                        # FastAPI backend — all application code
│   ├── main.py, *_routes.py, *_store*.py, ...   (117 tracked .py today)
│   ├── config/events/             # game event config
│   ├── data/economy_defaults.json
│   ├── migrations/                # 001..003 .sql
│   └── requirements.txt           # MOVED from requirements-test.txt
│
├── client/                        # Godot 4 project — exactly ONE copy
│   ├── project.godot
│   ├── export_presets.cfg
│   ├── scenes/  scripts/  resources/  assets/
│   └── .godot/                     # IGNORED — never committed
│
├── tests/                         # ALL tests, grouped by layer
│   ├── server/                    # MOVED from server/tests/
│   ├── client/                    # the ~40 existing suites, names unchanged
│   ├── fixtures/                  # shared fixture builders
│   └── conftest.py                # shared fixtures
│
├── shared/                        # game data both sides read (KEEP AS IS)
│   └── realms.json sects.json spirit_roots.json items/ lore/ npcs/ zones/
│
├── tools/                         # NEW: developer entrypoints
│   ├── build_release.py, build_release.sh   # MOVED from scripts/
│   ├── check_gd.py                          # MOVED from root
│   └── verify/                              # the 11 phase*_verify scripts, if kept
│
├── docs/                          # KEEP; add docs/README.md index
├── balance/                       # model + generated reports
├── playtest/                      # human playtest kit
├── research/                      # competitor research
│
├── ops/                           # NEW home for scheduled work
│   ├── cron/weekly_events.sh
│   └── deploy/release_notes.md
│
└── .artifacts/                    # ALL ignored, ALL gitignored
    ├── apks/                      # build/*.apk, _build_test/, installed.apk
    ├── competitor-apks/           # test-apks/ (5.0 G)
    ├── test-runs/                 # goal_receipt build-*, emulator current_build/runs
    ├── logs/                      # cron-logs/, *.log, verbose*.log
    └── scratch/                   # _gap_*.json, _diag.py, _probe_*.py
```

### Directory-by-directory rationale

**`server/`** — single home for backend code. Its four subdirectories are real and
load-bearing (`config/`, `data/`, `migrations/` all have committed content). Only its
*tests* move out, and its two `.bak` files and `_probe_inner.py` go.

**`client/`** — one Godot project, one root, because `project.godot` defines the import
graph. Three parallel copies (§0.1) is not a layout the engine supports: Godot resolves
`res://` per project root, so the copies are already drifting.

**`tests/server/`** — moving `server/tests/` up-and-over breaks the `sys.path` juggling that
`conftest.py:1-18` documents as an existing hazard. Doing it in one step is the point.

**`tools/`** — gives a home to build scripts and one-shot verification harnesses so they
stop landing in the repo root. `tools/verify/` makes "these were throwaway" explicit.

**`.artifacts/`** — one ignored prefix for 6.6 GB of reproducible output, so `.gitignore`
needs 4 rules instead of 20 and the intent is legible.

**`ops/`** — a scheduled job is not a test and not application code. Its relocation is what
makes the `G:` hardcoding in `cron/weekly_events.sh` visible during review instead of
causing a silent post-move failure.

---

## 4. KEEP / MOVE / DELETE / IGNORE verdict — every current entry

### Directories

| Current | Verdict | Destination / action | Priority |
|---|---|---|---|
| `server/` | **KEEP** | — | — |
| `server/tests/` | **MOVE** | `tests/server/` | P2 |
| `server/config/` `server/data/` `server/migrations/` | **KEEP** | inside `server/` | — |
| `server/_bak_reward_verify/` | **DELETE** | 2 `.bak` files, superseded by git | P1 |
| `server/coding-attempts/` | **MOVE** | `coding-attempts/lifecycle-v1/` | P1 |
| `client/` | **KEEP** | exactly one copy — see §0.1 | P1 |
| `client/.godot/` | **IGNORE** | already ignored (`.gitignore:18`) | — |
| `tests/` | **KEEP** | becomes the single test root | — |
| `tests/goal_receipt_android/build-*/` | **DELETE + IGNORE** | 2,578 tracked files, 1.5 GB | **P0** |
| `tests/emulator_interruption/current_build/`, `runs/`, `cleanup.log` | **DELETE + IGNORE** | 163 tracked files, 62 MB | P1 |
| `tests/_probe_routes.py`, `_audit_client_refs.py`, `*.tmp.gd`, `*.tmp.py` | **DELETE or MOVE** | 4 tracked scratch files → `tools/` if still useful | P1 |
| `test-apks/` | **MOVE + IGNORE** | `.artifacts/competitor-apks/` — 5.0 GB | P1 |
| `build/` | **MOVE + IGNORE** | `.artifacts/apks/` — 441 MB | P1 |
| `_build_test/` | **DELETE** | 56 MB test APK, no value | P1 |
| `coding-attempts/` | **KEEP (logs) / PURGE (binaries)** | drop `installed.apk` (51 MB, tracked) | P1 |
| `coding-attempts/release-audit/installed.apk` | **DELETE** | 51 MB tracked binary | **P0** |
| `research/` | **KEEP** | — | — |
| `docs/` | **KEEP** | merge duplicate verification reports | P2 |
| `docs/verification-report*.md` (2) | **MERGE** | one dated, one current | P2 |
| `shared/` | **KEEP** | client+server both read it | — |
| `balance/` | **KEEP** | — | — |
| `playtest/` | **KEEP** | human process kit, not code | — |
| `scripts/` | **MOVE** | → `tools/` | P2 |
| `cron/` | **MOVE** | → `ops/cron/` | P2 |
| `cron-logs/` | **MOVE + IGNORE** | → `.artifacts/logs/` | P1 |
| `memory/` | **KEEP (json) / DELETE (bak+probe)** | drop 3 non-JSON files | P1 |
| `_wc_baseline/` | **DELETE** (after confirming) | 12 untracked files, incl. `main.py.bak-before-vip-currency` | P1 |
| `keys/` | **KEEP LOCALLY, UNTRACK, ROTATE** | add to `.gitignore`; `git rm --cached` | **P0** |
| `__pycache__/`, `.pytest_cache/` | **IGNORE** | already ignored | — |
| `src/` (C: only) | **RESOLVE FIRST** | 3 divergent Godot copies — §0.1 | **BLOCKER** |

### Root files

| File | Verdict |
|---|---|
| `conftest.py` | **KEEP at root** — it must load before `server/` is importable |
| `docker-compose.yml`, `README.md` | **KEEP at root** |
| `PROJECT_LOCATION.json` | **Does not exist.** Referenced throughout this doc as if it did. Either create it (it would be the right machine-readable root) or stop citing it. `docker-compose.yml` is the actual source of the db coordinates. |
| `phase1_verify*.py` (9), `phase2_validate_turn1.py`, `verify_turn2.py` | **DELETE** (or `tools/verify/` if the agent wants an audit trail) |
| `test_integration.py`, `test_offline.py`, `test_lifecycle_offline.py`, `test_wallet_integration.py` | **MOVE** → `tests/server/` |
| `check_gd.py` | **MOVE** → `tools/` |
| `test_base64.gd` | **DELETE** — stale duplicate of `client/test_base64.gd` (md5 differs) |
| `tower_http_probe.log` | **DELETE** |
| `game-dev-rounds.md` | **MOVE** → `docs/` |
| `requirements-test.txt` | **MOVE** → `server/` (see §7 — unsure) |
| `_diag.py`, `_probe_contract.py`, `_wc_verbose.sh`, `_gap_*.json` (4) | **DELETE** |
| `client/export_presets.cfg.bak`, `client/**/*.bak*` | **DELETE** (7 files) |
| `server/*.log`, `server/_cf.bak` | **DELETE** |

### `.gitignore` — required new rules

```gitignore
# Secrets — P6. These are TRACKED; ignore alone will not untrack.
keys/
*.keystore
*.buildpass

# Generated test trees — P2, P3
tests/goal_receipt_android/build-*/
tests/emulator_interruption/current_build/
tests/emulator_interruption/runs/
*.tmp.gd
*.tmp.py

# One artifact root — P1
.artifacts/

# Scratch — P4
_gap_*.json
_diag.py
_probe_*.py
_wc_verbose.sh
```

---

## 5. Staged migration order (safe for a shared working tree)

**Hard constraint: 8 concurrent agents in this worktree. `git checkout`, `git reset`,
`git clean`, and `git stash` are forbidden — each destroys other agents' uncommitted work.**
Every stage below is either a pure filesystem move or an additive `.gitignore` change. No
stage rewrites history.

The stages are ordered by **blast radius × irreversibility**, cheapest and most reversible
first. Stages 0–2 touch nothing that a running agent needs.

| # | Stage | Why here | Reversible? |
|---|---|---|---|
| **0** | Get an owner decision on `C:/src/` (§0.1) and on `C:` vs `G:` merge ordering | Every later stage's destination depends on it | n/a (decision) |
| **1** | **Rotate the keystore and build password.** `git rm --cached keys/*` + `.gitignore` | Security first, independent of layout, history rewrite deferred | yes |
| **2** | **Add `.gitignore` rules** for P2/P3/P6/P4 — no file moves at all | Stops the tree getting *worse* while stages 3+ run | yes |
| **3** | **Delete the 11 root `phase*_verify*` scripts** + 4 `_gap_*.json` + `_diag.py` + `tower_http_probe.log` | Zero-risk: verified one-shot scratch, nothing imports them | yes (git-tracked) |
| **4** | **Move generated dirs out of the repo**: `test-apks/`, `build/`, `_build_test/`, `cron-logs/`, `coding-attempts/release-audit/installed.apk` → `.artifacts/` | Frees 5.5 GB; nothing tracked is lost (APKs are untracked) | yes |
| **5** | **Untrack the committed fixtures**: `tests/goal_receipt_android/build-*/` and `tests/emulator_interruption/{current_build,runs}/` | Biggest single win: 2,741 tracked files, 1.6 GB. Do this as its own commit so it is trivially revertible | yes, until history rewrite |
| **6** | **Parameterise the 25 hardcoded `G:` paths** (§P7), starting with `cron/weekly_events.sh` and `test_integration.py:11` | Must precede the C: move or tests and cron break | yes |
| **7** | **Relocate test directories**: `server/tests/` → `tests/server/`; 4 root `test_*.py` → `tests/server/`; `check_gd.py` → `tools/` | Structural; needs a full suite run to verify | yes, but noisy |
| **8** | **Purge in-source scratch**: `server/_probe_inner.py`, `server/_cf.bak`, `server/_bak_reward_verify/`, `memory/_bak_*`, `memory/_probe_lb_payout.py`, all `*.bak` / `*.bak-*` under `client/` | Cosmetic; safe after stage 7 | yes |
| **9** | **Merge duplicate docs**, `docs/verification-report*.md` | Trivial | yes |
| **10** | **Adopt the `C:` root.** Move `C:/src/{client_ui,ui_panels,ui_probe}` down to exactly one `client/`, delete the losers, move `server/`, `tests/`, `shared/`, `docs/` across | The big one. Only after stage 1's rotation, because the keystore must not exist in two histories | partially — needs stage 0 |
| **11** | **First commit on `C:`, then consider history rewrite** to purge the keystore from `93863c0` | `git log` on `C:` is empty today; the secret only enters history if `G:`'s is carried over | destructive — needs explicit sign-off |

**Per-stage verification** (run in this order; db coordinates from `docker-compose.yml`):

```bash
# baseline
git status --porcelain | wc -l
# server unit + route tests
env -u WC_PG_DB python -m pytest server/tests tests/server -q
# Godot parse gate
godot --headless --path client --check-only --script <path-relative-to-client/>
```

Per the test-harness convention: never pipe pytest through `tail`/`head` and never chain a
command after pytest — you will read the wrong exit code.

**Two windows require coordination, not just ordering:**
- **Stage 5** (untracking 2,741 files) will show a huge `git status`. Announce it; other
  agents must not run `git add -A` during it.
- **Stage 10** must not overlap with any agent running a Godot export or an emulator test —
  those hold file handles inside `client/` and `.artifacts/`.

---

## 6. UNSURE — flagged, not guessed

1. **Which `C:/src/` Godot copy is canonical.** §0.1. `ui_panels` has 4× the scripts but 16
   divergent shared-path files, so it is not a superset. Needs a file-by-file diff vs
   `G:/client/` by whoever owns the merge plan. **Blocks stage 10.**
2. **Whether the 11 `phase1_verify*` scripts are wanted as an audit trail.** I measured that
   they are committed one-shot scratch from a completed phase. If any agent values them as a
   record, they belong in `tools/verify/`, not deleted. Cheap to check with the agents that
   wrote them.
3. **`requirements-test.txt` → `server/requirements.txt` or repo root?** I moved it to
   `server/` on the grounds that it is server-only (contents: `fastapi>=0.115.0,<0.142`).
   If a client or shared tool also needs it, it belongs at the root.
4. **Is `memory/_bak_main.py_before_streak_routes` (79 KB) a still-relevant reference?** It
   is untracked, so deleting it loses it permanently. It looks like a pre-streak-routes
   snapshot. I did not diff it against current `server/main.py` — that is a
   behaviour question, not a layout one.
5. **`_wc_baseline/feature-roadmap.md.bak-2026-09-27` and `roadmap-factcheck-2026-09-27.json`.**
   Dated one day before the current `docs/feature-roadmap.md`. The factcheck JSON may be the
   evidence behind it and worth keeping in `docs/`. All 12 files are untracked, so this is
   delete-permanently-or-lose.
6. **Whether `tests/goal_receipt_android/build-*` holds any evidence not reproducible from
   `build.py`.** The `.gitignore` comment on lines 38-46 states a *different* runner
   (`tests/hub_views/fixture_setup.py`, `tests/session_integration/fixture_setup_local.py`)
   regenerates its own `fixture_project*` dirs — so that case is already proven
   reproducible. The `goal_receipt_android` builders are a separate mechanism and I did not
   run the reverse-control test. The claim rests on the uniform 51 MB size and timestamp
   naming, not on a proven rebuild.
7. **`test-apks/` retention policy.** I am confident it should not be in the repo. Whether
   the 5.0 GB should be *moved* to `.artifacts/` or *purged* and re-extracted on demand
   depends on whether the reverse-engineering work is ongoing. That is the owning agent's
   call.
8. **`coding-attempts/` — 51 untracked subdirectories.** I did not inspect their contents.
   Some may hold the only copy of an abandoned attempt worth keeping.
9. **The `C:` `.gitignore` is materially different from `G:`'s** (39 vs 60 lines; it adds
   `*.pem`/`*.key`/`*.sqlite`/`scratch/`, and omits every rule for P2/P3/P6). Which set
   becomes canonical is undecided, and §4's rule block is a merge of the two rather than a
   recommendation of either.

---

## 7. Re-measurement commands

Every claim above is reproducible from the repo root:

```bash
du -sh */ | sort -h                                   # sizes
for d in */; do echo "$(find "$d" -type f | wc -l) $d"; done | sort -rn
git ls-files | wc -l                                   # 3408
git ls-files <path> | wc -l                            # tracked-in-path
git ls-tree -r HEAD --name-only | grep -i keys/        # P6 secrets in HEAD
git check-ignore -v tests/goal_receipt_android/build-*/project.godot   # P2 not ignored
git grep -l -F -I "G:/Projects/wayfarers-cycle" HEAD | wc -l               # 85
md5sum test_base64.gd client/test_base64.gd             # stale duplicate
```
