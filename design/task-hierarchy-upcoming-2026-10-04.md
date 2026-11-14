# Task Hierarchy — Upcoming Days

*Generated 2026-10-04 from batch review of deleg_22dc677b, deleg_90b2fac2, deleg_b149bfd2, deleg_5572d737, deleg_fd38098c*

---

## Priority 1 — Critical (fix first)

### 012-001: Wire inventory snapshot into player transaction
- **Problem**: 6 sites spend inventory inside `player_store.transaction()` with no snapshot/restore. Player loses goods on a request that reports failure.
- **Fix**: One small local edit in `server/player_store.py` around lines 94-153: snapshot inventory store alongside wallet on entry, restore in `except` arm.
- **Why**: `InventoryStore.snapshot()`/`.restore()` already exist at `inventory.py:487/503` but have zero callers.
- **Tool**: `patch` on `server/player_store.py`

### 012-002: Add test for Go authorization function
- **Problem**: `cmd/server/wiring.go` contains the only security-relevant code in the gateway protobuf change and its test file has zero references to it.
- **Fix**: Mint a real session, prove a matching caller is allowed and a different one refused.
- **Tool**: `cmd/server/wiring_test.go`

### 012-003: Fix C# `**/*.cs` glob trap
- **Problem**: `client/WayfarersCycle.csproj` compiles every `.cs` file under `client/`. A test file placed at `tests/` broke the game build with 162 errors.
- **Fix**: Add explicit `Compile Remove` for the tests folder in `client/WayfarersCycle.csproj`.
- **Tool**: `patch` on `client/WayfarersCycle.csproj`

### 012-004: Close seven repo-root rewrite scripts
- **Problem**: `phase1_*.py` scripts still do whole-file rewrites of `verify-violations.json`, bypassing the locked helper.
- **Fix**: Repoint each at `record_turn`.
- **Tool**: `grep -n "open(.*verify-violations" phase1_*.py` at repo root, then repoint each

### 012-005: Write test for `GET /config`
- **Problem**: `GET /config` in `main.py` is called by no test. It is live-ops remote config the client reads on boot.
- **Fix**: Write a test asserting it answers with its feature-flag shape.
- **Tool**: `server/tests/test_main_contract.py` (new file)

---

## Priority 2 — High (fix soon)

### 012-006: Fix multi-item escrow loop
- **Problem**: `trading_market.py:1196` takes items one at a time. When the second is refused, the first is already destroyed (10→6) and no mail row is created.
- **Fix**: Either take all lines with one `consume_items` call, or track removals and add them back when a later line fails.
- **Tool**: `patch` on `server/trading_market.py`

### 012-007: Flip ownership guard manifest row
- **Problem**: The `/sect/list` row in `server/ownership_guard.py` is still `discloses-player-content` but the route is now self-guarded (401).
- **Fix**: Change the tuple's first element from `discloses-player-content` to `self-guarded`.
- **Tool**: `patch` on `server/ownership_guard.py`

### 012-008: Fix PG double-strip
- **Problem**: The runner script deletes all five database variables before starting Python, and the test file deletes them again at import. No contract test can reach PostgreSQL.
- **Fix**: Remove or gate the environment-deletion block at `test_daily_boss_routes_contract.py` lines 70-73 behind an explicit opt-in.
- **Tool**: `patch` on `server/tests/test_daily_boss_routes_contract.py`

### 012-009: Resolve protoc contradiction — **RESOLVED 2026-10-05, do not re-dispatch**
- **Stated problem was false**: `task-objectives.md` never said "protoc is not installed"; it already credited the vendored `protoc` 29.3 at `tools/protoc/bin/protoc.exe`. It was fixed the same day it was written.
- **Real contradiction found by running the probe**: Rust was marked BLOCKED with `rust/zones/gen/` missing, but the script writes to `rust/gen/proto/`, which holds 9 generated `.rs` files, `--check --target rust` is clean, and `cargo check` in `rust/zones` exits 0. 011-024 and 011-025 closed.
- **Also true**: `--check --target elixir` exits 5 because `C:\Program Files\Elixir` ships no `escript`, so the plugin wrapper cannot run. Broken local Elixir install, not a contract defect.
- **Measurement to reuse**: `bash tools/gen_proto.sh --versions`, then `--check --target <one>` per target. Bare `--check` exits 4 when any single plugin is missing and is not a whole-toolchain verdict.

### 012-010: Reconcile two Go plans
- **Problem**: `go-migration-roadmap-2026-10-03.md` says "Full Go migration. No hybrid." The umbrella plan says Go is a facade and Python stays the source of truth.
- **Fix**: Write one decision note that names the winner and marks the loser superseded.
- **Tool**: vault write under `design/`

### 012-011: Fix two incompatible task-objectives.md copies
- **Problem**: Repo = 87 lines, only abstraction 011. Vault = 1123 lines, abstractions 001-010. Same filename, two incompatible files.
- **Fix**: Decide which is canonical, rename the other, give both a provenance line.
- **Tool**: compare against git history first

### 012-012: Regenerate 4 stale patches
- **Problem**: `AchievementPanel.mount.patch`, `fixes-server-paid-actions-and-es-pool.patch` (x2), `ELEMENT_MATRIX_WIRING.patch` are stale. Target files have grown or changed.
- **Fix**: Tell the authors to regenerate. Do not hand-patch.
- **Tool**: `git apply --check` to confirm staleness

### 012-013: Rename 2 prose patches
- **Problem**: `VIP_PANEL_MOUNT.patch` and `WALLET_PANEL_MOUNT.patch` are plain-language instruction sheets, not real patches. They will never apply.
- **Fix**: Rename to `.md` so nobody retries them.
- **Tool**: `git mv` or `mv`

### 012-014: Remove duplicate patch
- **Problem**: `FIXES_server_paid_actions_and_es_pool.patch` exists in two locations with identical content.
- **Fix**: Delete one copy.
- **Tool**: `rm`

### 012-101: Fix three stale PlayerStateCache alias tests
- **Problem**: `client/scripts/systems/tests/.testsrc/PlayerStateCacheTests.cs` has 3 tests that pin the old buggy behavior (alias `foundation` → `foundation_establishment`). After the alias fix, these tests fail because they assert the defect.
- **Fix**: Replace the three alias assertions with one that pins the correct contract: `foundation` survives verbatim and ranks 1, and an unknown realm stays verbatim with a null rank.
- **Tool**: `patch` on `client/scripts/systems/tests/.testsrc/PlayerStateCacheTests.cs`
- **Verified**: 37 passed, 3 failed before fix; expect 40/40 after

### 012-102: Confirm server sends short realm name
- **Problem**: The alias fix assumes server writes `foundation` not `foundation_establishment`. Checked 4 call sites but not all.
- **Fix**: Search `server/` for every write of `cultivation_realm` and confirm none emits the long spelling.
- **Tool**: `search_files` on `server/` for `cultivation_realm`

### 012-103: Build append-only ledger journal
- **Problem**: No append-only journal exists for the shared ledger; workers rewrite the whole file, risking data loss.
- **Fix**: Created `memory/ledger_journal.py` with `append_turn`, `read_journal`, `compact`, `seed_from_keyed`, `read_all`, `journal_status`. Journal seeded from live keyed file: 122 lines, 373,049 bytes, 0 unparseable.
- **Verified**: 15/15 tests pass, concurrency proof (400/400 lines survived from 2 threads and 2 OS processes), corruption proof (3 damaged lines in 10-line journal, reader still returned other 7).
- **Tool**: `memory/ledger_journal.py` (new file)

### 012-104: Add count-based clobber detection + high-water mark
- **Problem**: No way to detect if the shared ledger has been clobbered (keys dropped).
- **Fix**: Added `--detect-clobber` to `memory/append_violation.py`, created `memory/.ledger-highwater.json` with keys, bytes, sha256, timestamp. Detector is count-only (no name rule).
- **Verified**: 22 existing tests pass, 15 new tests pass, both directions proven by runtime sabotage.
- **Tool**: `patch` on `memory/append_violation.py`, new `memory/.ledger-highwater.json`

### 012-105: Build pre-tool hook to block direct ledger writes
- **Problem**: Workers can bypass the guarded helper and write directly to the shared ledger, causing data loss.
- **Fix**: Created `plugins/ledger-write-guard/` with `__init__.py` + `plugin.yaml`. Refuses `write_file`, `patch`, and shell redirection at `memory/verify-violations.json` or `memory/*.ndjson`.
- **Verified**: 7/7 end-to-end cases blocked, 4/4 sanctioned-path checks pass, 15/15 assertions green.
- **NOT YET ACTIVATED**: Plugin is inert until added to `plugins.enabled` in `config.yaml`.
- **Tool**: `plugins/ledger-write-guard/` (new directory)

### 012-106: Convert disk-sabotaging test fixtures to in-memory sabotage
- **Problem**: Two test fixtures (`test_battle_pass_season_clock_control.py`, `test_battle_pass_money_path_control.py`) sabotage production files on disk, risking corruption if tests fail mid-run.
- **Fix**: Converted both to in-memory sabotage using `exec`-into-module-dict technique. Zero write calls remain.
- **Verified**: All 4 sabotages bite (season local-time start 23→8 red, 30-day window→7 red, money-path guard 12→3 red, receipt never written→1 red). Live tree untouched (SHA-256 verified).
- **Tool**: `patch` on both test files

### 012-107: Add GD parse gate as permanent repeatable check
- **Problem**: No permanent gate exists for GDScript parse errors; regressions can slip through undetected.
- **Fix**: Created `tools/gd_parse_gate.sh` (251 lines) — runs `godot --headless --path client --check-only --script res://<file>` 6 files at a time across all 126 `.gd` files.
- **Verified**: Full run prints `126 files, 87 clean, 39 autoload-artifact, 0 errors`, exit 0. Sabotage control (planted unclosed-bracket file) gives 1 error exit 1. Abort-stub control gives 1 error exit 1.
- **Tool**: `tools/gd_parse_gate.sh` (new file)

### 012-108: Reconcile realm-name disagreement (5 ladders, 8 sites)
- **Problem**: Client has 6 realm names, server has 8. Five distinct ladders exist across 8 sites. `immortal` is reachable but has no multiplier row (fights at 1.0x). Foundation/Core/Dao Seeking/Immortal players see empty zone maps.
- **Fix**: Owner decision needed: pick 8-name list (matches all zone/dungeon content) or collapse content to 6. If 8-name wins, write SQL migration moving stored rows.
- **Tool**: owner decision, then `patch` on `server/breakthrough.py`, `server/cultivation_config.py`, `server/dungeons.py`, `server/trading_market.py`, `client/scenes/game/TowerPanel.gd`

### 012-109: Fix stale key in PG money test
- **Problem**: `server/tests/test_once_only_pg.py` asks for prices by tier (`monthly`) while the route now stores them by card (`monthly_card`). Helper raises `KeyError` and dies before any money is checked.
- **Fix**: Change lookup at line 342 from `_route_costs()[tier_key]` to `_route_costs()[series_id]`, update line 684 same way.
- **Tool**: `patch` on `server/tests/test_once_only_pg.py`
- **Verified**: 3 failed, 2 passed before fix; expect 5/5 after

### 012-110: Fix monthly_event quest verification
- **Problem**: `server/monthly_event.py:324` grants daily reward as quest completion bonus with no check the player did the quest. Free rewards reachable by any caller.
- **Fix**: Add quest-criteria check before `claimed_quests.append`. Use existing quest-criteria helpers.
- **Tool**: `patch` on `server/monthly_event.py`

### 012-111: Fix dungeons template loading
- **Problem**: `server/dungeons.py:239` has `# TODO: Load from file` + `pass`. Function silently ignores `dungeons_path` argument. No caller passes it today, but it's a loaded gun.
- **Fix**: Either implement JSON read or delete the parameter. Check for dungeons JSON under `server/config/` first.
- **Tool**: `patch` on `server/dungeons.py`

### 012-112: Correct stale test_tower.py marker
- **Problem**: `server/test_tower.py:550` says "30-day automatic reset remains a known-unimplemented promise" — wrong. `tower.py:787` enforces `TOWER_SEASON_DAYS` with a 429 today.
- **Fix**: Rewrite the paragraph; no behaviour change.
- **Tool**: `patch` on `server/test_tower.py`

### 012-113: Delete leftover test_zz_sb_probe.py
- **Problem**: `server/tests/test_zz_sb_probe.py` is a leftover scratch file shipping in the test tree.
- **Fix**: Verify nothing imports it, then delete.
- **Tool**: `search_files` scoped to `server/`, then `rm`

### 012-114: Mount ElementMatrixPanel
- **Problem**: ElementMatrixPanel (721 lines, finished) is never mounted. Patch exists at `client/scenes/game/ELEMENT_MATRIX_MOUNT.patch`. Adds zero tab buttons.
- **Fix**: `git apply --check` then `git apply` the patch. Re-run `verify_panel_mounts.py` to confirm 3→2 orphans.
- **Tool**: `git apply` on patch file

### 012-115: Resolve WorldEventPanel duplicate
- **Problem**: WorldEventPanel (704 lines, 405 uncommitted lines) is never mounted. A twin named WorldEventsPanel (plural) is already mounted at `EventView.tscn:66` with the same routes.
- **Fix**: Owner decision: if abandoned, delete `.gd` and `.tscn`. If active, merge into mounted WorldEventsPanel. Do not mount it.
- **Tool**: owner decision, then `patch` or `rm`

### 012-116: Delete TowerPanel stub
- **Problem**: TowerPanel (319 lines, zero server calls, "implementation pending") is never mounted. A 2026-10-02 audit decided to leave it unmounted on purpose.
- **Fix**: Confirm zero references, then delete both files.
- **Tool**: `search_files` scoped to `client/`, then `rm`

### 012-117: Regenerate stale mount_manifest.json
- **Problem**: `hub/mount_manifest.json` claims 14 orphaned panels. True number is 3. Written 2026-10-04 but measured an older tree.
- **Fix**: Delete or rewrite from `verify_panel_mounts.py --json` output.
- **Tool**: `patch` or `rm` on `hub/mount_manifest.json`

### 012-118: Fix append_violation.py IndentationError
- **Problem**: `memory/append_violation.py:440` has IndentationError, blocks every future turn's ledger write.
- **Fix**: Align the argument line with its neighbours, prove with `python -m py_compile`.
- **Tool**: `patch` on `memory/append_violation.py`

### 012-119: Relabel ownership guard rows
- **Problem**: Two rows claim "not-serving-anonymous" but serve 200 to anonymous callers: `/analytics/batch` and `/cosmetics/preview`. Both strip or never accept player identity, so mislabelled not breached.
- **Fix**: Relabel to "public-static" with reasons describing the real 200. Re-run suite after.
- **Tool**: `patch` on `server/ownership_guard.py`

### 012-120: Commit sect-war job wiring
- **Problem**: Sect-war job wiring patch is applied to working tree but uncommitted. `server/main.py` shows as modified. If anyone resets, the settlement clock disappears silently.
- **Fix**: `git add -p` on `server/main.py`, select only sect-war hunks, commit.
- **Tool**: `git add -p` + `git commit`

### 012-121: Test Go→Python proto round-trip
- **Problem**: Python→Go proto round-trip proven (38/38 fields match). Go→Python direction untested. Asymmetric field-drop would not show up.
- **Fix**: Invert the two scratch scripts, reuse `protosec2_compare.py` unchanged.
- **Tool**: scratch scripts in `cache/scratch/`

### 012-122: Reconcile stale route counts
- **Problem**: Old counts of 130/246/357 are still cited in notes. Re-measured 2026-10-05: **388** mounted route objects = **383 API routes** + 4 FastAPI docs pages (`/openapi.json`, `/docs`, `/docs/oauth2-redirect`, `/redoc`) + 1 websocket route (`/comm/ws`). 387 HTTP operations (140 GET, 245 POST, 2 DELETE), 0 duplicate path+method pairs. Counting from a plain import without starting the app gives 384 and misses the 4 routes added during startup.
- **Fix**: DONE 2026-10-05  corrected the stale 130/246/357 figures in place, stamped with the measurement date and the method.
- **Tool**: boot the app inside `TestClient` so startup code runs, then count `len(app.routes)` with **no filter** and cross-check against `app.openapi()`. Script: `route_census_FINAL.py` in the gamedev scratch dir. `search_files` scoped to `design/` and `docs/`, then `patch`.
- **Status**: DONE  corrected `docs/stack-transition-plan.md` (357  388). `design/task-objectives.md` already said 388 and was **correct**  it was briefly and wrongly lowered to 387 before this measurement was corrected. The 130 and 246 figures no longer appear anywhere in `design/` or `docs/`. Backups in scratch/bak-routecount/.
- **Trap that cost a wrong number**: filtering routes on `hasattr(r, "methods")` silently drops `APIWebSocketRoute`, because a websocket route has no `.methods` attribute. That reported 387 instead of 388. A peer agent's independent script caught it. **Never filter the route list by `.methods` to get a total**  count the objects and break them down by class instead.

### 012-123: Fix stale vault citations
- **Problem**: 2,439 citations in vault notes point at wrong line, wrong file, or past end of file. 252 of 297 notes have at least one bad citation.
- **Fix**: Fix 17 past-end-of-file citations first (starting with `cosmetics-client-wiring-design.md`), then repair worst notes in priority order.
- **Tool**: `patch` on vault notes

### 012-124: Decide duplicate test pairs
- **Problem**: 27 test file groups carry duplicate risk. Clearest pairs: `test_sect_war_contract.py` vs `test_sect_war_routes_contract.py` (22 tests duplicated).
- **Fix**: Run both files, keep the one covering more routes, delete the other.
- **Tool**: `tools/wc_pytest.sh` on both paths, then `rm`

### 012-125: RESOLVE the 5 tests broken by the deleted-player session guard
- **Problem**: `server/auth.py` now refuses a session whose player row is absent (fix works — auth suite 20/20). But it cannot tell "deleted" from "never created", so 5 tests that mint a session for a never-created player now get 401 where they want 404/422. Confirmed red by re-running: `test_battle_pass_claim_route.py::test_claim_route_missing_player_is_404` (1 failed), `test_gacha_contract.py::test_pull_is_422_when_the_body_omits_player_id` + `::test_pull_is_422_when_there_is_no_body_at_all` (2 failed).
- **Fix**: Product decision, not a quiet patch. Either update the 5 tests to expect 401, or exempt a never-created id from the liveness check (one-line branch in `auth._require_live_player`). Do NOT add xfail markers — that hides the decision.
- **Tool**: `patch` on `server/auth.py` OR on the 3 test files
- **Note**: full `server/tests/` run timed out at 420s, so 5 is a LOWER BOUND not the total.

### 012-126: Measure the true blast radius of the auth liveness guard
- **Problem**: The guard runs in `authenticate`, so it affects every guarded route. Only 3 failures were reachable in the timeout window.
- **Fix**: Run `bash tools/wc_pytest.sh server/tests/ -q --tb=line` in background with a generous timeout, or split by subdirectory. Grep output for `401 == 404` and `401 == 422` to enumerate every instance of the same failure mode.
- **Tool**: `tools/wc_pytest.sh` (background)

### 012-127: Attach the player store explicitly at server startup
- **Problem**: `auth.attach_player_directory(app, store)` exists but the real server still depends on a `sys.modules` fallback to find `main.players_db`. If no store is attached, the guard keeps its OLD behaviour and the deleted-player hole stays open.
- **Fix**: Add `auth.attach_player_directory(app, main.players_db)` in `main.py` near the existing auth wiring. Alternative: make a missing store a 503 so the gap is visible instead of silent.
- **Tool**: `patch` on `server/main.py`
- **RESOLVED 2026-10-05** — a peer had already landed it in the uncommitted tree at `server/main.py:638`, as `auth_module.attach_player_directory(app, _auth_player_exists)` (absent from HEAD). NOTE the brief's literal `main.players_db` was deliberately NOT used: a callable is resolved at call time, so the ~30 tests that swap `main.players_db` still work; the object form pinned the import-time store and broke them.
- **Verified by running**: live probe on the booted app gave 200 for a live player's token and 401 for the same token after the row was deleted. Removing the line from source, with the fallback also unable to resolve, reopened the hole at 200.
- **Regression check**: 137 tests pass (auth + sect). 6 failures in gacha/ownership-guard suites are PRE-EXISTING — proven by running them both with and without the line and getting the identical 6 failures.

### 012-128: Add config-level guard in npc_roster_config.py
- **Problem**: The write-path guard in `npc_state.py` protects one caller. `favour_deadline_hours` itself is still unguarded, so any other caller can compute a non-finite deadline.
- **Fix**: Reject a non-finite `deadline_hours` inside `favour_deadline_hours`.
- **Tool**: `patch` on `server/npc_roster_config.py`

### 012-129: Add JWT_SECRET to gateway service in root docker-compose.yml
- **Problem**: The root `docker-compose.yml` does not pass `JWT_SECRET` to the gateway. Anyone running `docker compose up` gets a gateway container that exits on startup (the gateway correctly refuses to boot without a key).
- **Fix**: Add `JWT_SECRET=${JWT_SECRET:?set JWT_SECRET in .env; no default exists}` under the gateway service's `environment`.
- **Tool**: `patch` on root `docker-compose.yml`

### 012-130: Fix lifecycle guard ordering (400 instead of 401)
- **Problem**: `test_auth_route_coverage_live_table.py::test_control_the_lifecycle_pre_auth_refusal_is_not_mistaken_for_open` fails standalone, answering 400 where 401 is expected. A game-rule guard runs before the sign-in check in `server/lifecycle.py`.
- **Fix**: Reorder so the auth dependency runs before the game-rule guard. Alternative: correct the test's expectation if the ordering is deliberate — needs the file owner's approval.
- **Tool**: `patch` on `server/lifecycle.py`

### 012-131: Close the mount-filter hole in mint-authority tests
- **Problem**: `test_mint_authority_contract.py`'s mount test picks routes only from its own expected list, so a route mounted that the list does not name is invisible. Proved by re-attaching the unmounted route — all 119 tests still passed.
- **Fix**: Change `_mint_routes` to scan every route under the mint namespace rather than filtering by the expected list. Validate with the same mutation that currently survives.
- **Tool**: `patch` on `server/tests/test_mint_authority_contract.py`

### 012-132: Restore or accept the lost audit-ledger entries
- **Problem**: A worker truncated 46 entries in `audit/memory/verify-violations.json` by obeying "clear when resolved" literally. 23 entries under a sibling key survived. No backup held that exact state.
- **Fix**: Check whether any agent transcript holds a pre-write dump. If none, leave the recorded violation in place rather than fabricating provenance.
- **Tool**: `search_files` in `C:/Users/Boss/AppData/Local/hermes/profiles/gamedev/cache/delegation/live/`

### 012-133: Prove the reward-claim closure on the database path
- **Problem**: The closing test exercises the in-memory store only; the database branch reads the cycle stamp separately. "Gap is closed" holds for the in-memory path alone.
- **Fix**: Stand up a test database, point `WC_PG_HOST` at it, run the same freeze-clock re-arm scenario.
- **Tool**: `docker start wcpg-hermes` + `pytest` with DB env vars

### 012-134: Fix 4 confirmed raising non-finite conversions
- **Problem**: `qi_rewards.py:74` `_finite`, `return_receipt.py:38` `_as_number`, `return_receipt.py:50` `_as_delta`, `element_matrix_routes.py:315` `_coerce_count` all raise on a 1e400 integer. The receipt pair also leak infinity/NaN into written receipts. `_coerce_count` serves 5 routes.
- **Fix**: Add `OverflowError` to the handler AND a `math.isfinite` check — the infinity case returns instead of raising.
- **Tool**: `patch` on each file
- **Verified**: reproduced by AST sweep + execution probe

### 012-135: Fix 3 daily-boss non-finite passthrough sites
- **Problem**: `daily_boss_routes.py:96`, `daily_boss.py:367`, `daily_boss_tables.py:661` all compute `float(player.get("legacy_bonus", 0.0)) + amount`. Infinity/NaN survive silently with no exception — 8 of 10 checks are genuine write-throughs.
- **Fix**: Add one shared finite-check where the balance is read, matching the `_as_count` pattern at `daily_boss.py:192`.
- **Tool**: `patch` on `daily_boss.py`
- **Note**: reachability unproven — no live row holds a non-finite today

### 012-136: Sweep the 4 sibling float() passthrough sites
- **Problem**: `monthly_event.py:221` and `:327`, `tribulation.py:580` and `:657` share the identical unguarded `float()` line as the daily-boss trio.
- **Fix**: Grep `float(` scoped to `server/` so one guard lands everywhere instead of two edit rounds.
- **Tool**: `search_files` scoped to `server/`, then `patch`

### 012-137: Fix proof_clock nested-clock read
- **Problem**: `server/battle_pass_season_proof.py` `proof_clock` reads `body["season_id"]` at top level, but `GET /battle-pass/season` answers `{"ok": true, "season": {...}}` — measured live, top-level keys are exactly `['ok','season']`. Raises KeyError against the live app.
- **Fix**: Read `body["season"]["season_id"]`, then re-pin `test_proof_clock_raises_on_the_live_nested_payload`. Do NOT flatten the route — the shipped Godot panel reads the nested shape.
- **Tool**: `patch` on `server/battle_pass_season_proof.py`

### 012-138: Rule on recover-to-zero vs cap in validate_reward
- **Problem**: `progression_ladders.py:195` now recovers an infinite reward to `0.0`, matching `pay_reward`. A player with an infinite reward gets nothing rather than a lot. 6 tests now pin `0.0`.
- **Fix**: Owner decision. Capping needs a new module constant and touches the frozen return-shape contract.
- **Tool**: owner decision, then `patch` + test update

### 012-139: Fix cultivation_config negative-balance clamp
- **Problem**: `cultivation_config.py:322` multiplies the balance then clamps to 5%-95%. Measured: minus-infinity and NaN both produce a 5% roll — a permanent failure, not a clamp.
- **Fix**: Guard the input before the multiply.
- **Tool**: `patch` on `server/cultivation_config.py`

### 012-140: Add route-level test for the 4 non-finite guard helpers
- **Problem**: `test_nonfinite_guard_contract.py` (36 tests, all pass) pins the helpers only. No route calling them is exercised, so a caller that stopped using a helper would not turn anything red.
- **Fix**: Add one route-level test per helper, driving it with the helper's own trigger.
- **Tool**: `patch` on `server/tests/test_nonfinite_guard_contract.py`

### 012-141: Triage 1,671 no-try conversion sites
- **Problem**: AST sweep found 1,873 `int()`/`float()` calls in 156 files; 1,671 have no `try` at all. Ranked, not cleared — most are provably safe.
- **Fix**: Filter to sites whose argument is a subscript or a call (stored data), then hand-read what remains. Do NOT hand the raw count to a worker as a defect list.
- **Tool**: extend `astovf_sweep2.py` in scratch

### 012-142: Cap the ledger backup chain
- **Problem**: 64 `verify-violations.json.bak*` files in `memory/`, growing live (31 to 36 during one audit). `append_violation.py` has no cap.
- **Fix**: Keep newest N in `append_violation.py`. After the rescue lands, drop the 27 superseded append-chains and the duplicate `131938`.
- **Tool**: `patch` on `memory/append_violation.py`

### 012-143: Fix the shared-ledger clobber defect at its root
- **Problem**: The rescued `tree-consolidation-2026-09-28` narrative documents the shared ledger being rewritten wholesale by concurrent agents, losing entries, four times.
- **Fix**: Enable the ledger-write-guard plugin, or make `append_violation.py` refuse a whole-file overwrite that shrinks the key count.
- **Tool**: `hermes plugins enable ledger-write-guard`

### 012-144: Sweep test fixtures for the pristine-check pattern
- **Problem**: `test_battle_pass_money_path_control.py` called the copy helper with no verification, adopting whatever was on disk as "original". Fixed in that file; siblings may share the flaw.
- **Fix**: `search_files` for the copy helper name scoped to `server/tests/`, check each hit for a pre-backup pristine check.
- **Tool**: `search_files` scoped to `server/tests/`

### 012-145: Commit the uncommitted load-bearing test helper
- **Problem**: The helper update that makes `test_battle_pass_season_proof_contract.py` pass is uncommitted. A peer revert makes the test red again for the identical reason.
- **Fix**: Verify with `git diff -- server/tests/test_battle_pass_season_proof_contract.py`, then commit that exact path.
- **Tool**: `git add` explicit path

### 012-146: Delete stale mem0ry.jsonl.lock
- **Problem**: `mem0ry.jsonl.lock` sits 4.3 days old in the memory directory and will block the next exclusive-create lock.
- **Fix**: Delete only if no write is in flight.
- **Tool**: `rm` after checking for writers

### 012-147: Kill the 45-vs-36 backup count discrepancy
- **Problem**: The original audit said 45 ignored `.bak` files; the recount found 36 (`*.bak*`), 38 (`*bak*`), 53 (whole repo). Unexplained.
- **Fix**: `git status --ignored` scoped to `memory/`, compare. If files vanished mid-run, someone else is already cleaning up and steps must coordinate.
- **Tool**: `git status --ignored` scoped to `memory/`

### 012-148: Fix 21 false "route unmounted" claims across docs
- **Problem**: A live route-table audit (388 mounted route objects, measured 2026-10-05 by entering the app lifespan and counting `len(app.routes)` with no filter; 387 is the filtered-by-.methods count that silently drops the /comm/ws websocket) found 21 claims across 4 directories saying a route is unmounted when it is actually registered. Most dangerous: `docs/feature-roadmap.md:249` calls itself "the one surviving Phase 3 claim" and is now false — anyone treating it as the authoritative shortlist will skip real work.
- **Fix**: Patch each claim's text (not just line numbers). Start with `docs/feature-roadmap.md:249` and `:301`, then `design/signed-magnitude-route-audit.md:45-46`, `design/register-function-auth-audit-2026-10-04.md` §8, `design/dead-code-census-2026-10-03.md` §1a, `server/sect_routes.py` comments, `WalletPanel.tscn:37-40`, `WALLET_PANEL_MOUNT.patch:66-67`.
- **Tool**: `patch` on each file
- **Priority**: HIGH — these claims mislead planning

### 012-149: Re-measure the two /admin/* seed routes
- **Problem**: `design/signed-magnitude-route-audit.md:45-46` marks two `/admin/*` routes SAFE on the strength of "route not registered." The routes are now registered; their actual safety has never been re-measured.
- **Fix**: Authenticated probe against a booted app using a real operator token.
- **Tool**: boot app + authenticated request
- **Priority**: HIGH — security conclusion resting on a false premise

### 012-150: Test Elixir presence auto-disconnect path
- **Problem**: `elixir/presence/` has 21 passing tests, but none cover a tracked process dying without a clean goodbye. The design promises this works automatically; no test proves it.
- **Fix**: Add a test in `elixir/presence/test/presence_test.exs` where a tracked process is killed outright and the presence must vanish with no goodbye. Run 45 times with different seeds to prove it is not flaky.
- **Tool**: `patch` on `elixir/presence/test/presence_test.exs`
- **Priority**: MEDIUM — main behavior the design promises

### 012-151: Resolve the 51-vs-54 C# protobuf class gap
- **Problem**: 54 classes appear in generated source; 51 load from the DLL. The difference is almost certainly the `*Reflection` helper classes, but this was not verified.
- **Fix**: One reflection filter change, then rerun the probe. If the 3 are expected reflection helpers, document it; if not, a message type is not being generated.
- **Tool**: reflection probe in scratch
- **Priority**: MEDIUM — a wrong answer means a message type is missing

### 012-152: Execute the analytics SQL against a live database
- **Problem**: `analytics/README.md` sections 5A and 5B contain schema-verified SQL that was never executed against a live database. `date_trunc` returns `timestamptz` compared against `date` in the cohort query — a real coercion risk.
- **Fix**: `docker compose up postgres`, then `psql` as the `wayfarers` role. Watch for zero rows or type-coercion errors.
- **Tool**: `docker compose` + `psql`
- **Priority**: MEDIUM — unproven at runtime

### 012-153: Guard the empty-set max() in ownership guard observability test
- **Problem**: `server/tests/test_ownership_guard_skip_observability.py:434` computes the loudest "unreviewed" log level with `max()` over lines whose verdict is `untriaged`. Clearing the last unreviewed route empties that group, raising `ValueError: max() iterable argument is empty`.
- **Fix**: Materialise the untriaged-level list first and compare only when it is non-empty — mirroring how lines 459-467 already handle "nothing discloses today."
- **Tool**: `patch` on `server/tests/test_ownership_guard_skip_observability.py`
- **Priority**: HIGH — the test is red right now

### 012-154: Commit the untracked stack-transition plan copy
- **Problem**: `docs/stack-transition-plan.md` is a verified byte-identical copy of the vault note (md5 `fdeccef82736df93ec331e8bab77cb45`), but it is untracked in git. A fresh clone has neither the project file nor the contracts.
- **Fix**: `git add docs/stack-transition-plan.md` with the explicit path. Never `git add -A`.
- **Tool**: `git add` explicit path
- **Priority**: HIGH — untracked work can be lost

### 012-155: Commit the C# protobuf generation wiring
- **Problem**: `client/WayfarersCycle.csproj` and `proto/*.proto` are untracked. Until added, a fresh clone has neither the project file nor the contracts.
- **Fix**: `git add proto/ client/WayfarersCycle.csproj` with those exact paths.
- **Tool**: `git add` explicit paths
- **Priority**: HIGH — untracked work can be lost

### 012-156: Record the register_payment_verifier verdict in task-objectives.md
- **Problem**: `design/task-objectives.md` lines 84-86 say "Nobody has decided" whether `register_payment_verifier` is a deliberate placeholder or forgotten wiring. A live probe proved it is a deliberate placeholder (a setter, not a route registrar; the route refuses safely with 501).
- **Fix**: Replace "Nobody has decided" with the verdict plus one line of reasoning.
- **Tool**: `patch` on `design/task-objectives.md`
- **Priority**: MEDIUM — every other agent reads that file

### 012-157: Fix the invented-receipt payout leak
- **Problem**: The other purchase route accepts any receipt string a player invents and pays out each time. Measured: 25 made-up receipts, 25 accepted, balance went from 0 to 25. The once-only guard is real but keyed on the player's own words, so new words walk past it.
- **Fix**: Read `server/mint_authority.py` around `claim_purchase_receipt` (line 1257) and its reward table. The route must stop honouring a receipt string the caller made up — either refuse when the store cannot confirm it, or cap it.
- **Tool**: `patch` on `server/mint_authority.py`
- **Priority**: HIGH — pays real currency

### 012-158: Extend the protobuf round-trip proof to Elixir and Godot
- **Problem**: Python-Go round-trip is proven (29/29 fields, deterministic, 3 clean replays). Elixir and Godot are unproven. Eight schema files generate for four languages; only one pair has a cross-language byte proof.
- **Fix**: Keep `write_player.py` and `player.bin` untouched; write one reader per remaining language that decodes the same 237 bytes and emits the same key-and-value table, then run the existing `compare.py` unchanged.
- **Tool**: scratch files + `compare.py`
- **Priority**: MEDIUM — closes the stated gap

### 012-159: Write down the mix path quirk for Elixir
- **Problem**: The `mix` tool only runs when called by its full path, not from the shell's normal search path. This affects every Elixir command on this machine and is not yet written down anywhere in the repo.
- **Fix**: Add a short `README.md` under `elixir/presence/` or patch the repo's Elixir notes. Record the exact working command: add Erlang's bin folder to the path, then call mix by its full native path.
- **Tool**: `patch` or `write_file` under `elixir/presence/`
- **Priority**: MEDIUM — blocks every future Elixir task

### 012-160: Refresh stale comments in ownership guard contract test
- **Problem**: `test_ownership_guard_contract.py:1659` and `:1829` still say 96 rows and 23 public rows; measured now they are 97 and 24. Cosmetic, but it misleads the next reader.
- **Fix**: Update to 97 rows and 24 public rows, or better, delete the hard numbers and keep only the relationship the test actually asserts.
- **Tool**: `patch` on `server/tests/test_ownership_guard_contract.py`
- **Priority**: LOW — cosmetic

### 012-161: Fix the Go gateway blocking engine.Start
- **Problem**: `go-server/cmd/server/main.go:43` calls `engine.Start(ctx)` synchronously, blocking the web server from ever starting. The container stays up but serves nothing — `/health` returns status 000.
- **Fix**: Change to `go engine.Start(ctx)` so the engine runs in the background.
- **Tool**: `patch` on `go-server/cmd/server/main.go`
- **Priority**: HIGH — gateway cannot serve any request

### 012-162: Fix the total_qi_earned unguarded conversion
- **Problem**: `daily_streak.py` `claim()` and `status()` read `total_qi_earned` with a bare `float()`. A stored text value raises ValueError; a huge stored whole number raises OverflowError. Both escape to the caller. This is the same bug class the worker was hired to fix, one line away from the lines they fixed.
- **Fix**: Add a fourth sibling helper next to `_coerce_count` in daily_streak.py and route both `claim()` and `status()` through it. Use the same `(TypeError, ValueError, OverflowError)` tuple.
- **Tool**: `patch` on `server/daily_streak.py`
- **Priority**: HIGH — same bug class as the 33 reachable sites

### 012-163: Fix the 33 reachable non-finite conversion sites
- **Problem**: An AST scan of 148 non-test files found 33 reachable sites with no guard. 89 candidate sites total, 6 already correct, 13 unreachable. None of the 33 has a fix.
- **Fix**: Fix the caller-triggerable site first (`trading_market.py:822`), then the four write-back sites (`prestige_reset.py:192`, `spirit_beasts.py:548`, `return_receipt.py:37` and `:51`). Then batch the eleven tower/ladder normalizers.
- **Tool**: `patch` on each file
- **Priority**: HIGH — proven to break

### 012-164: Sweep other pre-auth refusals for the same ordering bug
- **Problem**: The lifecycle 400-vs-401 fix addressed one site. The same lifecycle guard runs on every lifecycle route, and the same bug class (pre-auth refusal masking 401) could survive elsewhere.
- **Fix**: `search_files` scoped to `server/` for `status_code=4` inside any route dependency, then check each for the same ordering shape.
- **Tool**: `search_files` scoped to `server/`
- **Priority**: MEDIUM — same bug class

### 012-165: Run the live-table auth sweep
- **Problem**: `test_auth_route_coverage_live_table.py` has docstrings describing the lifecycle 400 defect as a known red. The fix may turn those green and nobody has checked.
- **Fix**: `bash tools/wc_pytest.sh server/tests/test_auth_route_coverage_live_table.py`
- **Tool**: `tools/wc_pytest.sh`
- **Priority**: MEDIUM — may already be green

### 012-166: Decide the signed-in 400 vs 404 for unknown player id
- **Problem**: A signed-in caller naming an unknown id still gets 400 rather than 404. Two existing tests pin that as shipped behaviour. Changing it is a separate decision.
- **Fix**: Read `tests/test_lifecycle_contract.py::test_anonymous_player_id_is_answered_400_rather_than_404` and the matching return-receipt test, then choose 400 (current) or a proper 404.
- **Tool**: owner decision
- **Priority**: LOW — deliberate current behaviour

### 012-167: Close the unmeasured cross-player case for /events/list
- **Problem**: The `/events/list` auth fix proved anonymous callers are refused, but never signed in as player A and asked about player B. The cross-player (403) case is inherited from the guard's documentation, not measured.
- **Fix**: Sign in as player A, then request `/events/list` naming player B, and expect a refusal.
- **Tool**: boot app + authenticated request
- **Priority**: MEDIUM — converts weakest claim into measured one

### 012-168: Correct the stale comment in go-server/docker-compose.yml
- **Problem**: Lines 77-103 still say "The root file does NOT set this" about `JWT_SECRET`. That is now wrong — the root file does set it.
- **Fix**: Patch the comment to reflect reality.
- **Tool**: `patch` on `go-server/docker-compose.yml`
- **Priority**: LOW — cosmetic

### 012-169: Decide whether .env.example should be tracked
- **Problem**: `.env.example` is untracked in git. A reviewer looking only at a diff will see just one of the two JWT_SECRET files.
- **Fix**: Someone who owns git should add it in a separate change.
- **Tool**: `git add` explicit path
- **Priority**: MEDIUM — untracked work can be lost

### 012-170: Check the compile() gate into a repository as a real test
- **Problem**: The compile gate from the patch-tool-costs skill lives as a code block inside prose. No test file runs it, so a future edit to that snippet would go unnoticed.
- **Fix**: Add a small script that copies a fixture into a temporary location, runs the gate, and asserts both the verdict and the resulting bytes.
- **Tool**: new test file in a safe location
- **Priority**: MEDIUM — nothing guards it today

### 012-171: Reconcile the three adjacent non-finite skills
- **Problem**: Three neighbouring skills cover the same non-finite conversion ground: `overflowerror-guard-needs-a-huge-int-not-an-infinity`, `nonfinite-recovery-for-untrusted-numbers`, and `sign-only-guard-admits-nan-inf-and-overflow`. A second reconciliation pass could fold these in.
- **Fix**: Read each, merge the survivors into `nonfinite-stored-conversion-overflowerror`, keeping the shortest correct name.
- **Tool**: `skill_manage`
- **Priority**: LOW — deduplication

### 012-172: Fix the 0-byte ledger incident cleanup
- **Problem**: A worker's negative control ran old truncating code against the real ledger file, leaving it 0 bytes. It was restored to a verified 4-key pre-test state. The 0-byte damaged file is kept as `verify-violations.json.clobberscripts-0byte-incident`; six `.bak-append-20261004T164154*` files also remain.
- **Fix**: Confirm the ledger restore was correct by comparing the 6 leftover backups against the current file. Then decide on the leftover files.
- **Tool**: `python` printing each backup's keys and diffing
- **Priority**: HIGH — touched shared state

### 012-173: Decide whether the 7 patched clobbering scripts should keep running
- **Problem**: All 7 scripts crash on missing files (`client/scripts/GameAPI.gd`), so the patch may be cosmetic. The patched write blocks are proven only as isolated fragments.
- **Fix**: Run `python phase1_verify_v3.py` from the repo root and read the traceback. If the dead file references are the real problem, that is a separate fix.
- **Tool**: `terminal` + read traceback
- **Priority**: MEDIUM — patch may be cosmetic

### 012-174: Resolve the port 8080 conflict for the Go gateway
- **Problem**: Host port 8080 is already held by another project's container, so the briefed run command cannot work as written even after the blocking bug is fixed.
- **Fix**: Either stop the peer container holding 8080 or publish the gateway elsewhere. Check `docker ps` first; do not kill another project's container without asking.
- **Tool**: `docker ps` + decision
- **Priority**: MEDIUM — blocks gateway testing

### 012-175: Start the api service and probe a non-Go route
- **Problem**: The `api` service was never started during the gateway boot test, so the proxy half of the gateway has no coverage.
- **Fix**: `docker compose up -d api`, then probe a non-Go route.
- **Tool**: `docker compose` + HTTP request
- **Priority**: MEDIUM — proxy path untested

### 012-176: Decide the signing-key exemption security question
- **Problem**: `GET /wallet/signing-key` was added to `EXEMPT_PATHS` (ownership_guard.py:464) so a login check is no longer attached. The route is gated inside its handler by `_require_signing_key_operator_token` on an OPERATOR token. Measured live: operator token alone, no player session, `player_id: null` → **200 with the signing key**. That key is a player's permanent login credential, and the route accepts any player id. Anyone holding only the operator token can mint any account's credential.
- **Fix**: Owner decision. Either require the operator token AND proof of entitlement to that specific player, or restore the session gate for the owner's own key. Do NOT edit the tests to accept the new exemption — that deletes a working control to make the suite green.
- **Tool**: read `server/main.py` ~2102-2116 + `server/ownership_guard.py` 422-440, then decide
- **Priority**: CRITICAL — live credential-exfiltration path

### 012-177: Add a safety test pinning the signing-key gate
- **Problem**: Nothing fails if someone deletes the password check in `main.py`. The exemption in `ownership_guard.py` is then the only barrier to the login credential.
- **Fix**: New test in `test_ownership_guard_contract.py` driving the route three ways: 401 without a token, 401 with a wrong one, 200 with the right one. The probe already exists at `cache/scratch/operatorexempt_three_arm_probe.py` — adapt it.
- **Tool**: `patch` on `server/tests/test_ownership_guard_contract.py`
- **Priority**: HIGH — the missing control

### 012-178: Update the red tests pinning the old exemption list
- **Problem**: 5 tests are red because they pin the exempt list to exactly one entry: `test_signing_key_route_is_deliberately_not_exempt`, `test_no_exempt_path_exposes_player_state`, `test_the_ledger_did_not_change_the_matching_rule_or_the_exemptions`, `test_documented_exemptions_are_the_only_exemptions`, plus siblings in `test_ownership_guard_exemption_leak.py`, `test_ownership_guard_skip_observability.py`, `test_player_ownership_guard.py`.
- **Fix**: Only AFTER 012-176 confirms the exemption is intended. Change exact-set assertions to membership + a written justification per entry, matching the pattern the wallet test already uses.
- **Tool**: `patch` one file at a time
- **Priority**: HIGH but BLOCKED on 012-176

### 012-179: Name one canonical ledger and retire the other seven
- **Problem**: Eight ledger copies with mutually incompatible formats are written concurrently. The repo file has no top-level `violations` list; five other copies each have one with different lengths (0, 1, 28, 107, 121). This is the real cause of the four "clobbers", not any single file's shape.
- **Fix**: `search_files` scoped to the Hermes plugins folder to find what writes `audit/verify-violations.json` and `hermes/memory/verify-violations.json`; pick one and redirect readers.
- **Tool**: `search_files` + `read_file`
- **Priority**: HIGH — root cause of the ledger damage

### 012-180: Fix the mem0ry plugin reader/writer mismatch
- **Problem**: The memory plugin that feeds last-turn feedback to agents reads a top-level `violations` list. The repo ledger has none, so **that reader currently sees nothing at all** — a live silent failure.
- **Fix**: `read_file` on `plugins/mem0ry/__init__.py`, then patch its read to accept both shapes — a top-level list or per-turn objects.
- **Tool**: `patch` on `plugins/mem0ry/__init__.py`
- **Priority**: HIGH — silent dead feedback loop

### 012-181: Install the ledger write guard fleet-wide
- **Problem**: The write-blocking plugin exists only in the `gamedev` profile. It also misses `audit/verify-violations.json` because that path has no `memory/` folder segment. And it does not stop silent overwriting of an existing key's value, only key deletion.
- **Fix**: Copy `profiles/gamedev/plugins/ledger-write-guard/` to every profile that writes a ledger, or move it to the shared plugin directory. Widen `_MEMORY_DIR_SEGMENT` so a bare `verify-violations.json` anywhere is guarded. Verify with a real blocked tool call, not a direct function call.
- **Tool**: plugin install + `patch`
- **Priority**: HIGH

### 012-182: Remove the leaked PG password and rotate it
- **Problem**: The container password is hardcoded in a tracked repo file — `server/tests/test_daily_boss_restart_durability.py` line 96 — and in roughly 50 scratch files. A tracked file with a live credential is worse than a failing test.
- **Fix**: Change the hardcoded default to an empty string that forces the environment variable, then rotate the container password. Confirm with the user before touching either.
- **Tool**: `patch` + `docker exec`
- **Priority**: HIGH — secret in a tracked file

### 012-183: Commit the sect-war job wiring
- **Problem**: The job runs on disk but `git show HEAD:server/main.py | grep -c sect_war_job` returns **0**. A fresh clone gets no settlement clock and no error. The wiring at `main.py:331-342` is uncommitted.
- **Fix**: `git add server/main.py` scoped to that file only. Do NOT ship the bare-except fix in the same commit.
- **Tool**: `git add` explicit path
- **Priority**: HIGH — feature not shipped

### 012-184: Replace the bare except that hides a failed job start
- **Problem**: `main.py:336-337` wraps the sect-war job start in `except Exception: sect_war_job = None` — a broken import disables settlement with no log line. Nothing tests it.
- **Fix**: `patch` so a start failure calls `logger.exception("sect war job failed to start")`, matching the tournament job's shape plus a log line.
- **Tool**: `patch` on `server/main.py`
- **Priority**: HIGH — silent failure path

### 012-185: Fix the two non-finite sites hiding in "fixed" files
- **Problem**: `npc_state.py:490` and `main.py:3715` read a saved player row and convert with no guard, inside files the team already believes are clean — so they will be skipped again.
- **Fix**: Run `freezecensus_final.py` to confirm the handler is still there, then add `OverflowError` to the tuple and a finiteness check before the conversion. Two lines each.
- **Tool**: `patch` on each file
- **Priority**: HIGH

### 012-186: Reconcile the 19-vs-46-vs-55 non-finite count
- **Problem**: Three counts exist for the same thing: the original brief says 19, one census says 46, another says 55. It decides the scope of the fix work. Also, the earlier match rule silently changed once, so 46 and 55 may both be artifacts.
- **Fix**: Ask whoever wrote 19 what they counted, and pin the match rule before recounting.
- **Tool**: owner question + recount
- **Priority**: HIGH — decides scope

### 012-187: Delete the 4 junk placeholder skills
- **Problem**: 4 skills under `cortex-closeout/` carry the placeholder text "(Not provided by the model in this response.)": `SKILL-3`, `SKILL-4`, `SKILL-5`, `not-provided-by-the-model-in-this-response`. They carry no content and pollute every listing.
- **Fix**: `skill_manage(action='delete')` on each. Do not hand-delete files.
- **Tool**: `skill_manage`
- **Priority**: MEDIUM — needs no judgement

### 012-188: Merge the duplicate skill families
- **Problem**: 893 folders, ~1,600 skill files, with large duplicate families: ast-parse gate (8), shared JSON ledger (11), mutation-control (22), peer syntax error (5), citation drift (5). Merging the HIGH groups would remove roughly 45-55 skills.
- **Fix**: Merge the 5 exact-duplicate pairs first (no judgement), then the HIGH families after reading every member's body — the overlap rating was word-based, not body-based.
- **Tool**: `skill_manage(action='patch')` then delete absorbed
- **Priority**: MEDIUM

### 012-189: Install the Godot .NET edition
- **Problem**: The C# code compiles (`dotnet build` exit 0) but cannot run: the installed Godot is the standard edition (`dotnet=false`, `c_sharp=false`). Godot errors on loading any `.cs`. And 8 generated protobuf classes have zero consumers — ~800 KB compiled every build and never called.
- **Fix**: Install the .NET edition, then add `"C#"` to `client/project.godot` line 11. Read `client/README-csharp-migration.md` first — it argues the migration cannot be justified by porting current code.
- **Tool**: `winget install GodotEngine.GodotEngine.Mono`
- **Priority**: MEDIUM — decide before committing the untracked C# work

### 012-190: Diagnose the gd_parse_gate.sh hang and commit it
- **Problem**: `tools/gd_parse_gate.sh` prints a verdict on 6 files then never exits; on 1 file it prints no verdict. It is untracked, so a fresh clone has no GDScript gate at all.
- **Fix**: Suspect the cleanup or parallel stage — a bare Godot call exits in 4.8s. Verify on `scripts/autoload`.
- **Tool**: `read_file` + `patch` on `tools/gd_parse_gate.sh`
- **Priority**: MEDIUM

### 012-191: Close the repo_backup silent-gap hole
- **Problem**: The backup script's include list is opt-in, so any new top-level directory is invisible until a human adds it — exactly what happened earlier today with `proto/`, `admin/`, `docs/`, `memory/`. Its own `VERIFY: PASS` hashes only 3 sampled files; a 2-file discrepancy was found that it missed.
- **Fix**: Hash every entry against the checksum list (not 3), and either re-run coverage after any `mkdir` or schedule the existing `backup_gate.py` with `--if-stale-minutes`. The script lives OUTSIDE the repo at `profiles/_shared/scripts/repo_backup.py`.
- **Tool**: `patch` on that script (no repo edit needed)
- **Priority**: MEDIUM

### 012-192: Widen the stale payout-test pin
- **Problem**: `test_leaderboard_payout_authz.py::test_claim_takes_no_rank_and_no_amount` pins the payout function to exactly two parameters, but it now takes a third named `settling` from the server side. Stale test, not a money hole — but the file reports 12 passed, 1 failed.
- **Fix**: Assert only that no rank or amount parameter exists — the actual property being protected — which survives future additions. Also trace `season_is_settling` in `leaderboard_settlement.py` to its lock.
- **Tool**: `patch` on `server/tests/test_leaderboard_payout_authz.py`
- **Priority**: MEDIUM

### 012-193: Subagent-driven delegation batch mining (STANDING WORKFLOW)
- **Status**: ADOPTED 2026-10-04. Batch-by-batch review by the orchestrator is STOPPED until 2026-10-05.
- **Problem**: 592 delegation batch folders exist on disk; 135 were dispatched on 2026-10-04 alone, holding ~946 worker tasks. Only 12 batches carry a review record. The orchestrator cannot read these one at a time — each review costs a full turn and produces ~15 new tasks, so the backlog grows faster than it drains.
- **Decision**: Delegate the mining to subagents. The orchestrator reviews *findings*, not raw batch transcripts.
- **Input**: `C:/Users/Boss/AppData/Local/hermes/profiles/gamedev/cache/delegation/live/<deleg_id>/task-<N>.log`
- **Output**: new rows appended to THIS file under the correct priority section, numbered from 012-194 upward.
- **Required dedup step (do this FIRST)**: read this hierarchy and build the set of already-known findings before adding anything. A finding that names a file already covered by an existing row is NOT a new task — it is a confirmation. Report confirmations separately; do not create duplicate rows.
- **What to extract (only these three classes)**:
  1. **Defects** — crashes, security holes, money leaks, broken guards, silent failures.
  2. **Landed work** — artifacts a worker wrote to disk that no task records yet (test files, migrations, design notes, patches).
  3. **Contradictions** — two sources disagreeing (e.g. a count of 19 vs 46 vs 55), or a claim measured false.
- **What to IGNORE**: worker self-assessment scores, phase-1/phase-2 gate narration, heartbeat shortfalls, "what worked" prose, and any task already present in this file.
- **Safety rules for the mining subagent**:
  - READ-ONLY on the repo. Do NOT edit any file under `G:/Projects/wayfarers-cycle`.
  - Do NOT call `skill_view` (50-100s stalls).
  - Do NOT run whole-repo recursive searches; scope every search to one explicit directory.
  - Scratch files go in `cache/scratch/` with a task-unique name.
  - Print `HEARTBEAT` every 60s so the 480s inactivity watchdog does not kill the run.
  - Return findings as text to the orchestrator; do NOT write to this hierarchy directly (one writer per file).
- **Batching**: one subagent per 10-15 batch folders, dispatched in batches of <=10. Prioritise the newest first (16:37 downward) since those are least likely to be stale.
- **Orchestrator duty**: read the returned findings, dedup against this file, append the surviving rows. One turn per ~10 subagent reports instead of one turn per batch.
- **Tool**: `delegate_task` (batches of <=10) + `read_file` on the hierarchy for dedup
- **Priority**: HIGH — this is the only path that drains the 123-batch backlog
- **Rejected alternative**: continuing batch-by-batch orchestrator review. Measured rate shows it never converges.

---

## Priority 3 — Medium (fix when convenient)

### 012-015: Write 6 missing design notes
- **Problem**: Objectives 011-007 through 011-012 have zero coverage: Django admin deps, Django settings, inventory tables, player tables, DuckDB queries, Grafana setup.
- **Fix**: Write notes under `design/`, following the front-matter shape of `zone-contract-python-rust-2026-10-04.md`.
- **Tool**: vault write under `design/`

### 012-016: Write Elixir note covering 011-016 through 011-019
- **Problem**: Four objectives, one service, one supervisor design. No note exists.
- **Fix**: Write one document covering all four.
- **Tool**: vault write under `design/`

### 012-017: Install Rust code generator
- **Problem**: `protoc-gen-rust` not installed. All-target drift check cannot run in one pass.
- **Fix**: `cargo install protoc-gen-rust` (correct crate is `protobuf-codegen`).
- **Tool**: `cargo install protobuf-codegen`

### 012-018: Fix stale doc referencing removed shared/proto/
- **Problem**: `docs/stack-transition-plan.md` line 55 still describes `shared/proto/` as a folder that exists.
- **Fix**: Remove or update the "Contracts | Broken" row.
- **Tool**: `patch` on `docs/stack-transition-plan.md`

### 012-019: Add contract test for no route spends inventory unguarded
- **Problem**: No test enforces that routes spending inventory inside a transaction are guarded.
- **Fix**: Copy the shape of `test_inner_skills_contract.py:2042`.
- **Tool**: `server/tests/test_inventory_transaction_rollback.py`

### 012-020: Give gift-send path a refund
- **Problem**: `social_routes.py:1042` takes the item then builds the gift record, with no refund after it.
- **Fix**: Add `_refund(sender_id, item_id, units)` after the record append.
- **Tool**: `patch` on `server/social_routes.py`

---

## Priority 4 — Low (fix if time permits)

### 012-021: Operator recovery lockout
- **Problem**: Worker running, awaiting report.
- **Fix**: Check worker status.

### 012-022: PG payout proof unrun
- **Problem**: Worker running, awaiting report.
- **Fix**: Check worker status.

### 012-023: Secret rotation
- **Problem**: Two secrets need rotation. A worker pasted a real password into their closeout.
- **Fix**: Rotate both secrets.
- **Needs**: User decision

### 012-024: Clock-skew bound
- **Problem**: Wallet clock skew window is 86,400s vs 300s.
- **Fix**: Decide: five minutes (recommended) or one day.
- **Needs**: User decision

### 012-025: JWT_SECRET compose gap
- **Problem**: JWT_SECRET not in compose file.
- **Fix**: Add to compose file.
- **Needs**: User decision

### 012-026: mint_authority syntax error
- **Problem**: Indentation error at line 1449/1551 prevents test collection.
- **Fix**: Repair indentation.
- **Tool**: `patch` on `server/mint_authority.py`

### 012-027: 7 clobbering scripts
- **Problem**: Seven scripts clobber the shared ledger.
- **Fix**: Repoint at locked helper.
- **Tool**: `grep` + `patch`

---

## Priority 0 — Root Cause (fix before anything else)

### 012-000: Fix concurrent file writes causing lost subagent work
- **Problem**: Multiple subagents editing the same file simultaneously. Worker A writes a fix, Worker B writes a different fix to the same file, Worker A's fix is overwritten and lost. The shared tree with ~30 live writers + no file locking = fixes get lost.
- **Root cause identified**: 2026-10-04. Investigation showed NO recent git resets (last reset was October 1, 3+ days ago). No git hooks causing resets. No scripts with `git reset`. No scheduled tasks. The real cause is concurrent file writes.
- **Evidence**: 
  - `git reflog` shows resets from Sep 28 and Oct 1 only — nothing recent
  - `ORIG_HEAD` is 81.6 hours old (last reset ~3.4 days ago)
  - `HEAD` is 204.3 hours old (~8.5 days)
  - No active git processes, no scheduled tasks, no hooks causing resets
  - `stale_write_blocked` errors encountered when trying to overwrite files — symptom of concurrent writes
- **Fix NOT applied**: We diagnosed the problem but never implemented a solution.
- **Proposed fix**: 
  1. Implement file-level locking mechanism (e.g., `.lock` files, `flock`, or a central lock server)
  2. Ensure workers check for locks before writing
  3. Use `git worktree` to give each worker its own isolated copy
  4. Or implement a central file-writer service that serializes writes
- **Tool**: `flock` (Linux), `LockFile` (Windows), or custom lock mechanism
- **Priority**: HIGHEST — this is causing loss of all subagent work

---

## Orchestrator Task List

*These are tasks the orchestrator (me) must do myself, not delegate to subagents.*

### Immediate (today)
1. ✅ Wire inventory snapshot into player transaction — DONE
2. ✅ Update task hierarchy with all batch findings — DONE
3. ✅ Create orchestrator task list — DONE
4. ⬜ **STOP GIT RESETS FROM DISCARDING WORK** — root cause identified but NOT fixed
5. ⬜ Verify inventory rollback fix with broader test run
6. ⬜ Commit inventory rollback fix (player_store.py, main.py, test file)

### This Week
7. ⬜ Triage 40+ test failures from full suite run
8. ⬜ Fix ghost_battle.py receipt-map crash
9. ⬜ Regenerate stale proto targets (Python, Go, Godot)
10. ⬜ Commit all uncommitted fixes from batch reviews
11. ⬜ Enable ledger write guard
12. ⬜ Correct append_violation.py docstring citations
13. ⬜ Move scratch tests into tracked suite

### Ongoing
14. ⬜ Review each batch completion and extract new tasks
15. ⬜ Update task hierarchy with new findings
16. ⬜ Maintain orchestrator task list
17. ⬜ Ensure fleet capacity stays saturated
18. ⬜ Verify fixes by running, not assuming

---

## Notes

- All tasks are derived from batch reviews of deleg_22dc677b, deleg_90b2fac2, deleg_b149bfd2, deleg_5572d737, deleg_fd38098c, deleg_9f0243bd, deleg_7eb776dd, deleg_259b0d73, deleg_55941d0c, deleg_efc0bbe4, deleg_20b40655.
- Priority 0 is the root cause of lost work and must be fixed before anything else.
- Priority 1 tasks are blocking and should be fixed before any new feature work.
- Priority 2 tasks are high-value and should be fixed soon.
- Priority 3 tasks are medium-value and should be fixed when convenient.
- Priority 4 tasks are low-value and should be fixed if time permits.
- Orchestrator task list is maintained separately and reviewed each turn.


---

## New Findings — mined from delegation closeouts, 2026-10-05

*Added by the orchestrator after verifying each claim against the live tree. Each row was re-measured by reading the named file, not taken from the worker's report.*

### 012-194: crafting.py charges its fee from the wrong wallet
- **Problem**: `server/crafting.py:462-483` reads the craft fee (souldust) from a per-player dict `player["wallet"]`, not the real wallet store. Verified: `grep` for `wallet_dispatch`/`wallet_store` in crafting.py returns zero hits. A player holding 100,000 souldust in the wallet the game shows them is told "Need 14 souldust, have 0" and every craft is refused. Same wrong-ledger bug the cosmetics shop already had, in a second module.
- **Fix**: Route the fee read and the fee debit through `wallet_dispatch` / the wallet store, matching the pattern cosmetics.py now uses.
- **Tool**: `patch` on `server/crafting.py`
- **Priority**: HIGH — refuses a core feature for every player

### 012-195: tower.py floor-clear reward reports money it did not verify was paid
- **Problem**: `server/tower.py:686-687` calls `earn(...)` then sets `granted[currency] = amount` unconditionally. The sibling `apply_path_rewards` path at `tower.py:337-340` guards the same call. A replay or a refused `earn` still reports the money as granted.
- **Fix**: Check the `earn` result (or the `_replayed` flag) before writing `granted[currency]`.
- **Tool**: `patch` on `server/tower.py`
- **Priority**: HIGH — a payout receipt that lies

### 012-196: battle_pass.py free tiers pay a legacy currency name
- **Problem**: `server/battle_pass.py:180-181` writes `REWARD_TABLES[FREE][level]["spirit_stones"]`, and `_pay_reward` -> `grant_stones` at `battle_pass.py:955-966` writes `player["spirit_stones"]` — the legacy dict — not the wallet store. Claiming all free tiers fills an off-wallet balance the player's real wallet never sees. Same class as 012-195 and the auto-battle legacy write.
- **Fix**: Pay battle-pass currency through `wallet_dispatch` using the `wallet_store_provider` the route registrar already accepts (currently unused).
- **Tool**: `patch` on `server/battle_pass.py`
- **Priority**: HIGH — the retention promise pays into a dead balance

### 012-197: /combat/auto-battle credits a legacy spirit_stones dict
- **Problem**: `server/main.py:3633-3635` writes `player["spirit_stones"]` (a legacy dict copy) instead of the wallet store. Measured by a worker: wallet delta 0 while `spirit_stones` delta was 3,000,000. The player's real balance never moves.
- **Fix**: Credit the wallet store; keep the legacy dict only as a mirror if a reader still needs it.
- **Tool**: `patch` on `server/main.py` (contended file — smallest possible edit)
- **Priority**: HIGH — combat payout vanishes

### 012-198: inner_skills.py realm ladder is 3 names against the live 6
- **Problem**: `server/inner_skills.py:60` sets `REALM_ORDER = ["mortal", "qi_refining", "foundation_establishment"]`. The live ladder in `server/cultivation_config.py:27+` uses `mortal`, `foundation`, `core`, `nascent_soul`, `soul_transformation`, `dao_seeking`. `_resolve_realm` (line 63) returns `"mortal"` for every name it cannot match, so all five higher realms collapse to index 0 and 2 of the 3 inner skills can never be learned by anyone.
- **Fix**: Drive the gate off `cultivation_config.REALMS` so the two ladders cannot drift again; land the literal name fix first.
- **Tool**: `patch` on `server/inner_skills.py`
- **Priority**: HIGH — feature unreachable for all players

### 012-199: currency_exchange has no rate into destium — verify it is intentional
- **Problem**: `server/currency_exchange.py:50-54` documents that no rate *into* `destium` exists, and a test locks the invariant. But a worker's probe showed every `/wallet/convert` into destium returning 400. Confirm whether destium is meant to be a one-way sink (then close this) or whether a earn path is missing.
- **Fix**: Owner decision — accept as designed, or add an earn path.
- **Tool**: read `server/currency_exchange.py` + `test_no_rate_creates_destium`
- **Priority**: MEDIUM — likely working as intended, needs a written verdict

### 012-200: no CI runs the 877-test suite
- **Problem**: No `.github/`, `.gitlab-ci.yml`, `Jenkinsfile`, `azure-pipelines.yml`, or `.circleci/` exists at the repo root, and no coverage config in any tracked file. The suite runs only when a person remembers. A launcher (`Temp/wcpytest.sh`) already exists and works.
- **Fix**: Add `.github/workflows/tests.yml` running the server suite.
- **Tool**: new workflow file
- **Priority**: HIGH — highest value per unit of effort

### 012-201: nine test_*.py files define zero tests
- **Problem**: `server/test_artifacts_persistence.py`, `server/test_integration.py`, `server/test_legacy_persistence.py` and six siblings are `__main__` durability scripts named `test_*`. They inflate every "how many test files" count while collecting nothing.
- **Fix**: Rename to `check_*_persistence.py`.
- **Tool**: `patch` / rename per file
- **Priority**: MEDIUM — makes test counts honest

### 012-202: three scratch Godot projects left inside the repo test folder — DONE 2026-10-05
- **Problem**: `tests/api_base_url/_e2e_project/`, `_sabotage_project/`, `_scratch_project/` were left behind after a worker's task finished, while the worker's own final gate claimed no scratch artifacts remained.
- **Fix**: DONE. Confirmed unreferenced, then removed. All three directories deleted (249 files); the parent `tests/api_base_url/` is now empty. Nothing was tracked by git, so the tree is unchanged in git terms.
- **Evidence**: Zero references outside this note. A repo-wide search for the three names returns only lines in this note; the Python harness that built them no longer exists, so `tests/api_base_url/` held nothing but the scratch copies. Copies were stale — `client/scripts/network/APIClient.gd` has newer login/auth code absent from all three. Backup archive kept at the profile scratch dir (`wc-012202-scratch-projects-backup.zip`, 83 files per directory, hashes verified on write and on read-back).
- **Related, NOT in scope**: `tests/auth_wiring/_e2e_project/` (83 files, real `project.godot`) and `tests/session_crypto_verify/_scratch_project/` (1 file) are separate leftovers for a separate item.
- **Tool**: `search_files` then `rm`
- **Priority**: LOW — tree hygiene

### 012-203: economy-decision-brief row contradicts the measured leaderboard payout
- **Problem**: `design/economy-decision-brief.md` section 5 still says the leaderboard payout was "already on the guarded ledger — no effect", while the route was minting 10 billion spirit stones per unauthenticated request until it was fixed mid-batch.
- **Fix**: Correct the row to record the fix and the measured before/after.
- **Tool**: `patch` on `design/economy-decision-brief.md`
- **Priority**: MEDIUM — a false "no effect" row

### 012-204: time-per-level-model.md understates progression time
- **Problem**: Two of seventeen checked rows are wrong; the rest run 1.06x-2.48x faster than the document claims. `foundation/middle` doc 150h vs measured 372h (2.48x).
- **Fix**: Re-measure and correct the table, or stamp the rows as unverified.
- **Tool**: `patch` on `design/time-per-level-model.md`
- **Priority**: MEDIUM — drives pacing decisions

### 012-205: competitor-features doc's own "correction" block is wrong
- **Problem**: The prereqStrings correction at `competitor-features-bth-and-shadowrun.md:598,602,797,975` replaces "92 items" with "672 files / 824 occurrences" and attributes it to recursing `items/`. Measured: `items/` alone is 504 files / 604 occurrences; 672/824 is the whole `data/` tree. The correction widened scope and called it recursion. Also: sign-in tables contradict themselves (7 at :358 vs 11 at :387 vs 12 measured), and :590 says 48 folders when `items/` holds 29 subfolders plus 38 root files.
- **Fix**: Re-label the counts with their real populations; reconcile the sign-in table counts.
- **Tool**: `patch` on `competitor-features-bth-and-shadowrun.md`
- **Priority**: MEDIUM — the doc promotes this figure as its most reusable idea

### 012-206: three ledger copies written by three workers in one batch
- **Problem**: In one batch, task-0 and task-3 wrote `G:/Projects/wayfarers-cycle/memory/verify-violations.json`; task-1 appended to the profile copy; task-4 created `C:/Users/Boss/Projects/memory/verify-violations.json` fresh. No single file holds the batch result. Related to 012-179.
- **Fix**: Fold into 012-179 — pick one canonical path and redirect every writer.
- **Tool**: `patch` on the writers
- **Priority**: MEDIUM

### 012-207: zone access ladder is 8 names against the live 6
- **Problem**: `shared/zones/zones.json` + `server/zones.py` rank access against an 8-name ladder while players hold the 6-name ladder, so `foundation`, `core`, `soul_transformation`, `dao_seeking` all rank at -1 and reach 0 zones. Note: plan row 012-108 names the fourth zero-zone realm as `immortal`; the measured fourth is `soul_transformation`.
- **Fix**: Fold into 012-108 (the global realm-name decision) and correct its fourth-realm name.
- **Tool**: `patch` on `design/task-hierarchy-upcoming-2026-10-04.md:127` first (one-word fix)
- **Priority**: HIGH — empty zone maps for four realms

### 012-208: Godot compressed-format doc states a format that does not exist
- **Problem**: `library/cortex-library/.cortex/docs/cortex-1/godot-ai-texture-import-measured.md:83` lists "ASTC 2x2 16bpp". No ASTC 2x2 profile exists in the Vulkan registry (verified against vk.xml: `ASTC_2x2 any: 0`; all 14 ASTC profiles use a 16-byte block). The companion evidence note declares that whole line correct.
- **Fix**: Delete the entry or mark it unsourced; scope the evidence note's "correct" claim to the entries actually checked.
- **Tool**: `patch` on both docs
- **Priority**: LOW — docs only, not game code

### 012-209: competitor capability baseline was never written to disk
- **Problem**: Batch `deleg_8bf4483a` built a 90-capability deduplicated baseline, printed it into chat, and never saved a file. The consuming audit (`design/feature-completeness-audit.md:44`) fell back to the coarse 32-row matrix the worker was told not to anchor on.
- **Fix**: Owner decision — accept the coarse matrix as baseline (cheapest), or re-run with an explicit output path in the brief.
- **Tool**: read `design/feature-completeness-audit.md` first
- **Priority**: LOW — the coarse matrix may be sufficient

### 012-210: delegation briefs must name the output file
- **Problem**: Multiple batches failed to persist their deliverable because the brief said "produce an inventory" without naming a file. The worker satisfied it with a chat reply, which the log then truncated to ~635 chars per line. One batch lost 24 of 26 findings this way.
- **Fix**: Add a standing line to the delegation brief template: a deliverable intended for later consumption must name its file, and long reports must be written to a file, not returned as chat text.
- **Tool**: `patch` on the brief template under `design/tools/`
- **Priority**: HIGH — root cause of repeated mining losses


---

## New Findings — mined from delegation closeouts, 2026-10-05 (batch 2)

*Added by the orchestrator after verifying each claim against the live tree. Each row was re-measured by reading the named file, not taken from the worker's report.*

### 012-231: Go combat engine syntax error
- **Problem**: `go-server/internal/combat/engine.go:153` has a syntax error: `non-declaration statement outside function body`. Prevents compilation of `go-server.exe`.
- **Fix**: Read the file around line 153, identify the syntax error, and fix it.
- **Tool**: `patch` on `go-server/internal/combat/engine.go`
- **Priority**: HIGH — blocks Go compilation

### 012-232: dispatch_from_hierarchy.py KeyError
- **Problem**: `design/tools/dispatch_from_hierarchy.py` crashes with `KeyError: 'description'` when processing task dictionaries that use varied keys.
- **Fix**: Update to safely resolve: `task.get('description') or task.get('goal') or task.get('title')`.
- **Tool**: `patch` on `design/tools/dispatch_from_hierarchy.py`
- **Priority**: MEDIUM — tooling defect

### 012-233: Sect war dual-store split
- **Problem**: Two distinct, unlinked sect store instances in `server/main.py`. Sect creation succeeds but war declarations fail with `400 "Not in a sect"`.
- **Fix**: Merge the two store instances into one.
- **Tool**: `patch` on `server/main.py` (contended file)
- **Priority**: HIGH — sect war feature broken

### 012-234: PG purchase route binding failure
- **Problem**: `patches/purchase-receipt-guard-patch` added PostgreSQL backing methods to `server/wallet_store_pg.py`, but the route handler still references in-memory wallet guards.
- **Fix**: Rebind handler references to the PG driver.
- **Tool**: `patch` on `server/wallet_store_pg.py`
- **Priority**: HIGH — 6 of 8 contract tests fail

### 012-235: Social vocabulary drift
- **Problem**: 10 parameter mismatches between `social_routes.py` and `friends.py`.
- **Fix**: Reconcile the parameter names.
- **Tool**: `patch` on both files
- **Priority**: MEDIUM

### 012-236: Split currency store
- **Problem**: `cosmetics.py` deducts directly from player records while other paths use `wallet_dispatch`.
- **Fix**: Route cosmetics through `wallet_dispatch`.
- **Tool**: `patch` on `server/cosmetics.py`
- **Priority**: HIGH — economy integrity

### 012-237: Inventory rollback deficiency
- **Problem**: Inventory store lacks real transactional rollback; compensating writes are in-memory only.
- **Fix**: Add transactional rollback to the inventory store.
- **Tool**: `patch` on `server/inventory.py`
- **Priority**: HIGH — data integrity

### 012-238: Receipt leak in wallet_defense.py
- **Problem**: `wallet_defense.py:626` logs raw `purchase_key`. Needs `receipt_fingerprint(purchase_key)`.
- **Fix**: Replace raw key with fingerprint.
- **Tool**: `patch` on `server/wallet_defense.py`
- **Priority**: HIGH — security vulnerability

### 012-239: Rebindable singletons
- **Problem**: `currency_exchange.py`, `dungeon_routes.py`, and `zone_routes.py` use module-level globals that can be rebound.
- **Fix**: Migrate to per-request `app.state`.
- **Tool**: `patch` on all three files
- **Priority**: HIGH — session isolation

### 012-240: Wallet clock skew
- **Problem**: `wallet_store_pg.py` skew window is 86,400s but should be 300s.
- **Fix**: Adjust the skew window constant.
- **Tool**: `patch` on `server/wallet_store_pg.py`
- **Priority**: MEDIUM

### 012-241: Sect war job wiring
- **Problem**: `sect_war_job` never starts. Patch at `sect-war-job-wiring.patch` not yet applied.
- **Fix**: Apply the patch.
- **Tool**: `git apply -p1 sect-war-job-wiring.patch`
- **Priority**: HIGH — scheduled job broken

### 012-242: legacy_bonus dead parameter
- **Problem**: `calculate_breakthrough_chance()` accepts a `legacy_bonus` parameter but no caller ever passes it. The legacy/prestige currency is collected and multiplied but has zero effect on breakthrough odds.
- **Fix**: Wire `legacy_bonus` into the calculation, or remove the parameter.
- **Tool**: `patch` on `server/cultivation_config.py`
- **Priority**: HIGH — feature unreachable

### 012-243: beast_config upgrade_blood no-op
- **Problem**: `upgrade_blood()` spends materials and raises the level but never applies the affix bonus. `_apply_blood_affix` does not exist. `affix_value` is stored but never read.
- **Fix**: Implement `_apply_blood_affix` and call it from `upgrade_blood`.
- **Tool**: `patch` on `server/beast_config.py`
- **Priority**: HIGH — paid feature does nothing

### 012-244: VIP extra_daily_attempts never enforced
- **Problem**: VIP tiers grant `extra_daily_attempts` (0-3 extra daily dungeon and tower runs) but nothing in the game ever reads it.
- **Fix**: Wire into attempt counters, or remove the field.
- **Tool**: `patch` on `server/vip.py`
- **Priority**: HIGH — paid feature does nothing

### 012-245: request_signing hardcoded fallback
- **Problem**: When `WC_SIGNING_KEY` is not set, the signing key silently falls back to a hardcoded placeholder string in the source.
- **Fix**: Make the fallback fail loudly.
- **Tool**: `patch` on `server/request_signing.py`
- **Priority**: HIGH — security vulnerability

### 012-246: SlotQueue.gd SlotManager parse error
- **Problem**: `SlotQueue.gd` calls `SlotManager` but it is neither a registered autoload nor a `class_name`, so the script cannot parse.
- **Fix**: Register as autoload or add `class_name`.
- **Tool**: `patch` on `client/project.godot` or `client/scripts/systems/SlotManager.gd`
- **Priority**: HIGH — screen dead

### 012-247: conftest.py untracked
- **Problem**: `server/tests/conftest.py` is not tracked by git. Without it, about twenty test files cannot import the sign-in helper.
- **Fix**: `git add server/tests/conftest.py`
- **Tool**: `git add` (orchestrator only)
- **Priority**: HIGH — fresh clone loses tests

### 012-248: identity.py assert_owns never called
- **Problem**: `identity.py` defines `assert_owns()` but no product code calls it.
- **Fix**: Wire into cross-player routes, or remove.
- **Tool**: `patch` on `server/identity.py`
- **Priority**: MEDIUM

### 012-249: Weekly event configs expired
- **Problem**: Only two weekly event configs exist and both are expired. The event rail runs on a timer with nothing valid to serve.
- **Fix**: Generate a new config for the current week.
- **Tool**: Create `server/config/events/week_2026-W40.json`
- **Priority**: MEDIUM

### 012-250: monthly_event grant_event_currency never called
- **Problem**: `grant_event_currency()` is the only thing that creates the monthly-event shop currency, and no gameplay code ever calls it.
- **Fix**: Wire into the monthly event reward path.
- **Tool**: `patch` on `server/monthly_event.py`
- **Priority**: HIGH — shop unusable

### 012-251: vip add_vip_points never called
- **Problem**: `add_vip_points()` is the only way to earn VIP status, and no gameplay code calls it.
- **Fix**: Wire into the gameplay reward path.
- **Tool**: `patch` on `server/vip.py`
- **Priority**: HIGH — VIP unreachable

### 012-252: Android signing password in git history
- **Problem**: The Android signing password was committed to git history (commit `93863c0`) and is still readable.
- **Fix**: User must rotate the password. Do NOT rewrite history.
- **Tool**: User action required
- **Priority**: HIGH — security vulnerability

### 012-253: /wallet/signing-key exemption
- **Problem**: The `/wallet/signing-key` route is listed as exempt but should not be. Four tests are failing.
- **Fix**: Remove from exempt list.
- **Tool**: `patch` on `server/ownership_guard.py`
- **Priority**: MEDIUM

### 012-254: Stale route ownership test
- **Problem**: `test_duplicate_route_shadowing.py` expects `POST /admin/events/create` in event_routes.py, but it was moved to `mint_authority.py`.
- **Fix**: Update the test.
- **Tool**: `patch` on `server/tests/test_duplicate_route_shadowing.py`
- **Priority**: LOW

### 012-255: Stale auto-battle daily cap test
- **Problem**: `test_auto_battle_daily_cap.py` reads `GET /player/{id}` without a session, so the guard answers 401.
- **Fix**: Add a session to the test request.
- **Tool**: `patch` on `server/tests/test_auto_battle_daily_cap.py`
- **Priority**: LOW

### 012-256: Stale ownership guard exemption test
- **Problem**: `test_player_ownership_guard.py` pins the exempt list to a fixed set and the list has since grown.
- **Fix**: Update the test.
- **Tool**: `patch` on `server/tests/test_player_ownership_guard.py`
- **Priority**: LOW

### 012-257: Stale proto contract test
- **Problem**: `test_proto_contract.py` expects `PlayerIdentity` but the message was renamed to `SessionIdentity`.
- **Fix**: Update the test.
- **Tool**: `patch` on `server/tests/test_proto_contract.py`
- **Priority**: LOW

### 012-258: Stale tower test marker
- **Problem**: `test_tower.py:550` has a stale marker about `TOWER_SEASON_DAYS`.
- **Fix**: Update or remove the marker.
- **Tool**: `patch` on `server/tests/test_tower.py`
- **Priority**: LOW

### 012-259: Stale once-only price lookup
- **Problem**: `test_once_only_pg.py` lines 342 and 684 use `_route_costs()[tier_key]` but should use `_route_costs()[series_id]`.
- **Fix**: Correct the key lookup.
- **Tool**: `patch` on `server/tests/test_once_only_pg.py`
- **Priority**: LOW

### 012-260: Stale leaderboard payout authz test
- **Problem**: `test_leaderboard_payout_authz.py` pins a parameter check that is too strict.
- **Fix**: Relax the parameter check.
- **Tool**: `patch` on `server/tests/test_leaderboard_payout_authz.py`
- **Priority**: LOW

### 012-261: Stale inner skills realm ladder test
- **Problem**: `test_inner_skills_contract.py` pins a realm ladder that has since changed.
- **Fix**: Update the test.
- **Tool**: `patch` on `server/tests/test_inner_skills_contract.py`
- **Priority**: LOW

### 012-262: Stale lifecycle contract test
- **Problem**: `test_lifecycle_contract.py` expects `rejected_rewards` but the route now returns a different shape.
- **Fix**: Update the test.
- **Tool**: `patch` on `server/tests/test_lifecycle_contract.py`
- **Priority**: LOW

### 012-263: Stale return receipt contract test
- **Problem**: `test_return_receipt_contract.py` expects `available: True` but the route now returns a different shape.
- **Fix**: Update the test.
- **Tool**: `patch` on `server/tests/test_return_receipt_contract.py`
- **Priority**: LOW

### 012-264: Stale npcs xfail marker
- **Problem**: `test_npcs.py` has a stale xfail marker — the test now passes.
- **Fix**: Remove the xfail marker.
- **Tool**: `patch` on `server/tests/test_npcs.py`
- **Priority**: LOW

### 012-265: Stale mint authority xfail marker
- **Problem**: `test_mint_authority_contract.py` has a stale xfail marker — the test now passes.
- **Fix**: Remove the xfail marker.
- **Tool**: `patch` on `server/tests/test_mint_authority_contract.py`
- **Priority**: LOW

### 012-266: Stale tournament clock xfail marker
- **Problem**: `test_tournament_clock_defects.py` has a stale xfail marker — the test now passes.
- **Fix**: Remove the xfail marker.
- **Tool**: `patch` on `server/tests/test_tournament_clock_defects.py`
- **Priority**: LOW

### 012-267: Stale tower ladder xfail marker
- **Problem**: `test_tower_ladder_contract.py` has a stale xfail marker — the test now passes.
- **Fix**: Remove the xfail marker.
- **Tool**: `patch` on `server/tests/test_tower_ladder_contract.py`
- **Priority**: LOW

### 012-268: Stale tower routes xfail marker
- **Problem**: `test_tower_routes_contract.py` has a stale xfail marker — the test now passes.
- **Fix**: Remove the xfail marker.
- **Tool**: `patch` on `server/tests/test_tower_routes_contract.py`
- **Priority**: LOW

### 012-269: Stale tower spire xfail marker
- **Problem**: `test_tower_spire_contract.py` has a stale xfail marker — the test now passes.
- **Fix**: Remove the xfail marker.
- **Tool**: `patch` on `server/tests/test_tower_spire_contract.py`
- **Priority**: LOW

### 012-270: Stale wallet defense xfail marker
- **Problem**: `test_wallet_defense_contract.py` has a stale xfail marker — the test now passes.
- **Fix**: Remove the xfail marker.
- **Tool**: `patch` on `server/tests/test_wallet_defense_contract.py`
- **Priority**: LOW

### 012-271: Stale sect war xfail markers
- **Problem**: `test_sect_war_routes_contract.py` has two stale xfail markers — the tests now pass.
- **Fix**: Remove the xfail markers.
- **Tool**: `patch` on `server/tests/test_sect_war_routes_contract.py`
- **Priority**: LOW

### 012-272: Stale trading market xfail marker
- **Problem**: `test_trading_market_contract.py` has a stale xfail marker — the test now passes.
- **Fix**: Remove the xfail marker.
- **Tool**: `patch` on `server/tests/test_trading_market_contract.py`
- **Priority**: LOW


---

## Not-Done Items — from user's 69-item list, 2026-10-05

*Added by the orchestrator. These are items from the user's list that are NOT covered by running workers and NOT done.*

### 012-273: element_matrix_routes.py non-finite guard
- **Problem**: Non-finite conversion sites lack guards.
- **Fix**: Add math.isfinite check and OverflowError to the handler.
- **Tool**: `patch` on `server/element_matrix_routes.py`
- **Priority**: HIGH

### 012-274: daily_boss.py non-finite guard
- **Problem**: Non-finite conversion sites lack guards.
- **Fix**: Add math.isfinite check and OverflowError to the handler.
- **Tool**: `patch` on `server/daily_boss.py`
- **Priority**: HIGH

### 012-275: battle_pass_season_proof.py proof_clock fix
- **Problem**: proof_clock has a nested read that needs fixing.
- **Fix**: Patch the nested read.
- **Tool**: `patch` on `server/battle_pass_season_proof.py`
- **Priority**: MEDIUM

### 012-276: pvp_routes.py non-finite guard
- **Problem**: Non-finite conversion sites lack guards.
- **Fix**: Add math.isfinite check and OverflowError to the handler.
- **Tool**: `patch` on `server/pvp_routes.py`
- **Priority**: HIGH

### 012-277: hall_slot_routes.py non-finite guard
- **Problem**: Non-finite conversion sites lack guards.
- **Fix**: Add math.isfinite check and OverflowError to the handler.
- **Tool**: `patch` on `server/hall_slot_routes.py`
- **Priority**: HIGH

### 012-278: Integrate file locking into worker briefs
- **Problem**: Worker briefs do not use file locking.
- **Fix**: Add `from file_lock import FileLock` and `with FileLock(filepath):` to every worker brief that writes files.
- **Tool**: Update worker brief template.
- **Priority**: HIGH

### 012-279: Test file locking under concurrent load
- **Problem**: File locking has not been tested under concurrent load.
- **Fix**: Spawn 3+ workers writing to the same file simultaneously, verify only one succeeds at a time.
- **Tool**: `delegate_task` with 3 workers.
- **Priority**: MEDIUM

### 012-280: Re-seed ledger journal
- **Problem**: The ledger journal is out of sync with the live keyed file.
- **Fix**: Run `python memory/ledger_journal.py --seed`.
- **Tool**: The module's own CLI.
- **Priority**: MEDIUM

### 012-281: Point writers at append_turn
- **Problem**: Callers use `record_turn` instead of `ledger_journal.append_turn`.
- **Fix**: Change callers or make `record_turn` delegate.
- **Tool**: `patch` each call site.
- **Priority**: MEDIUM

### 012-282: Prove bait refund database path
- **Problem**: The bait refund database path has not been tested.
- **Fix**: Run the two refund tests against real PostgreSQL.
- **Tool**: `docker start wcpg-hermes` + `pytest` with DB env vars.
- **Priority**: HIGH

### 012-283: Triage 40+ test failures
- **Problem**: 40+ test failures need triage.
- **Fix**: Read failure details for npc_state_contract.py (30), mission_board_item_credit.py (4).
- **Tool**: `read_file` on failure output, then `pytest` per file.
- **Priority**: HIGH

### 012-284: Fix ghost_battle.py receipt-map crash
- **Problem**: ghost_battle.py has a receipt-map crash.
- **Fix**: Add isinstance check plus value-sweep loop.
- **Tool**: `patch` on `server/ghost_battle.py`.
- **Priority**: HIGH

### 012-285: Regenerate stale proto targets
- **Problem**: Proto targets are stale.
- **Fix**: Run `tools/gen_proto.sh --target python`, then `--target go`, then `--target godot`.
- **Tool**: `bash tools/gen_proto.sh --target <lang>`.
- **Priority**: MEDIUM

### 012-286: Regenerate mount_manifest.json
- **Problem**: mount_manifest.json is stale.
- **Fix**: Delete or rewrite from `verify_panel_mounts.py --json` output.
- **Tool**: `patch` or `rm` on `hub/mount_manifest.json`.
- **Priority**: LOW

### 012-287: Fix stale vault citations
- **Problem**: 17 past-end-of-file citations need fixing.
- **Fix**: Fix the worst notes in priority order.
- **Tool**: `patch` on vault notes.
- **Priority**: MEDIUM

### 012-288: Wire GD parse gate into review
- **Problem**: GD parse gate is not wired into review.
- **Fix**: Use `tools/gd_parse_gate.sh` as a required check before accepting client changes.
- **Tool**: No code change needed.
- **Priority**: MEDIUM

### 012-289: Copy narratives from .bak files
- **Problem**: The .bak files contain narratives that need to be copied out before cleanup.
- **Fix**: Read the three named backups, write complete blocks to scratch, then re-append.
- **Tool**: `read_file` on the backups.
- **Priority**: MEDIUM

### 012-290: Fix 4 raising non-finite conversions
- **Problem**: 4 raising non-finite conversions need fixing.
- **Fix**: Add OverflowError to the handler and a math.isfinite check.
- **Tool**: `patch` on `qi_rewards.py`, `return_receipt.py`, `element_matrix_routes.py`.
- **Priority**: HIGH

### 012-291: Fix proof_clock nested read
- **Problem**: proof_clock has a nested read that needs fixing.
- **Fix**: Patch the nested read.
- **Tool**: `patch` on `server/battle_pass_season_proof.py`.
- **Priority**: MEDIUM

### 012-292: Decide recover-to-zero question
- **Problem**: The recover-to-zero question needs an owner decision.
- **Fix**: Decide whether zero destroys a reward or capping preserves it.
- **Tool**: Owner decision.
- **Priority**: MEDIUM

### 012-293: Confirm 0-byte ledger restore
- **Problem**: The 0-byte ledger restore needs confirmation.
- **Fix**: Compare the 6 leftover backups against the current file.
- **Tool**: Python printing each backup's keys and diffing.
- **Priority**: HIGH

### 012-294: Sweep pre-auth refusals
- **Problem**: Pre-auth refusals need sweeping for the same ordering bug.
- **Fix**: Search for `status_code=4` inside any route dependency.
- **Tool**: `search_files` scoped to `server/`.
- **Priority**: MEDIUM

### 012-295: Run live-table auth sweep
- **Problem**: The live-table auth sweep needs to be run.
- **Fix**: Run `bash tools/wc_pytest.sh server/tests/test_auth_route_coverage_live_table.py`.
- **Tool**: `bash tools/wc_pytest.sh`.
- **Priority**: MEDIUM

### 012-296: Close cross-player /events/list case
- **Problem**: The cross-player /events/list case is unmeasured.
- **Fix**: Sign in as player A, request player B, expect refusal.
- **Tool**: Boot app + authenticated request.
- **Priority**: MEDIUM

### 012-297: Check compile() gate into repo
- **Problem**: The compile() gate is not in the repository as a real test.
- **Fix**: Add a small script that runs the gate and asserts both verdict and bytes.
- **Tool**: Create a new test file.
- **Priority**: MEDIUM

### 012-298: Reconcile non-finite skills
- **Problem**: Three adjacent non-finite skills need reconciliation.
- **Fix**: Read each, merge into `nonfinite-stored-conversion-overflowerror`.
- **Tool**: `skill_manage`.
- **Priority**: LOW

### 012-299: Write mining brief as file
- **Problem**: The mining brief is not a file.
- **Fix**: Write `design/tools/MINING_BRIEF.md`.
- **Tool**: `write_file`.
- **Priority**: HIGH

### 012-300: Fix security hole 012-176
- **Problem**: A security hole exists at `server/main.py` around lines 2102-2116.
- **Fix**: Require the operator token and proof of entitlement to the specific player.
- **Tool**: `patch` on `server/main.py`.
- **Priority**: CRITICAL

### 012-301: Fix hidden conversion sites 012-185
- **Problem**: Two hidden conversion sites at `npc_state.py:490` and `main.py:3715` need fixing.
- **Fix**: Patch, two lines each.
- **Tool**: `patch` on both files.
- **Priority**: HIGH

### 012-302: Rotate leaked database password
- **Problem**: The database password was leaked.
- **Fix**: Patch plus docker exec; confirm with user before touching either.
- **Tool**: User action required.
- **Priority**: HIGH


---

## Unlanded work from deleg_b67a72d3 (6 unknown tasks), 2026-10-05

*Added by the orchestrator after disk audit. Tasks 3, 4, 6, 7, 9, 10 exited before recording. Tasks 6 and 10 were verified landed on disk. Tasks 3, 4, 7, 9 did not land.*

### 012-305: Write 6 missing design notes (011-007 through 011-012)
- **Problem**: Objectives 011-007 through 011-012 have zero design-note coverage. They cover Django admin dependencies, settings, inventory admin, players admin, DuckDB queries, and Grafana/Metabase setup.
- **Fix**: Write 6 design notes covering those objectives.
- **Tool**: Dispatch worker to write notes to vault.
- **Priority**: MEDIUM
- **Status**: NOT DONE — batch exited, no notes on disk

### 012-306: Write Elixir note covering 011-016 through 011-019
- **Problem**: Objectives 011-016 through 011-019 have zero design-note coverage. They cover Elixir matchmaking queue, lobby supervision, restart test, and presence tracking.
- **Fix**: Write one design note covering all four objectives.
- **Tool**: Dispatch worker to write note to vault.
- **Priority**: MEDIUM
- **Status**: NOT DONE — batch exited, no note on disk

### 012-307: Reconcile stale route counts
- **Problem**: Old counts of 130/246/357/388 are still cited in notes. `design/task-objectives.md` still says "388 routes" and "384 and misses the 4".
- **Fix**: Correct to the measured live count, stamped with today's date.
- **Tool**: `patch` on `design/task-objectives.md` and any other notes citing old counts.
- **Priority**: MEDIUM
- **Status**: NOT DONE — batch exited. Note: `task-objectives.md` claims "388 routes" is correct, which contradicts the brief's "correct to 379/384". Needs a live measurement first.

### 012-308: Fix lifecycle guard ordering
- **Problem**: `server/lifecycle.py` should return 401 for unauthenticated callers, but may return 400 because a game-rule guard runs first.
- **Fix**: Reorder so the auth dependency runs before the game-rule guard.
- **Tool**: `patch` on `server/lifecycle.py`.
- **Priority**: MEDIUM
- **Status**: NOT DONE — batch exited. Note: the claim-offline route already has `view: dict = Depends(guard)` as a parameter, so FastAPI runs it before the body. Needs a live probe to confirm whether the defect still exists.

### 012-128b: DECISION - npc_state overflow policy (measured, q-520)
- **Question**: a finite config value that overflows on `* 3600.0` still charges the player. Keep the charge, or refuse before charging?
- **DECISION: refuse before charging.** Do not widen to `None`.
- **Measured, both arms, live code** (`server/npc_state.py:accept_favour`, config `deadline_hours = 1e308`):
  - Arm A (shipped, widen to `None`): accept debits 1 standing point; the next sweep debits a SECOND point, marks the favour `failed`, and the row is terminal -- a second accept is refused with 'already resolved'. Net: 2 points lost, nothing delivered, reply looks like success.
  - Arm B (refuse): standing unchanged, NO row written, favour stays retryable; accepts cleanly once the config is healed. Net: 0 cost.
- **Why widen is wrong HERE**: widening is correct for a corrupt stored row (the player cannot fix it). A config constant is different -- an operator fixes it in seconds, and the write is a real debit. Arm B surfaces as 409 via the existing `except ValueError` in `npc_routes.py`.
- **Root cause**: `_readable_deadline` returns a marker, it never raises, so the line above the charge is a marker factory, not a guard. Ordering (already fixed) governs WHEN it runs, not WHAT it decides.
- **Fix shape**: compute `hours * 3600.0` FIRST, then `math.isfinite()` on the computed seconds. A guard on the input cannot see `1e308` overflow.
- **Cost of flipping (corrected after checking a second suite)**: 50 cases across **7** test functions, in TWO files -- 48/6 in `server/tests/test_npc_state_contract.py` plus `test_accept_stores_no_non_finite_deadline_when_config_is_hostile` in the UNTRACKED peer file `server/tests/test_npc_state_nonfinite.py` (34 passed + 11 strict xfailed, 486 lines). That second file states 'the call RETURNS, never raises' as its module-level contract, so it pins the widen harder than the first. Both suites: ARM=off 323 passed/11 xfailed, ARM=on 50 failed. REWRITE the assertions to 'raises + standing unchanged'; do not delete -- and the peer file needs its owner to agree the module contract changes.
- **Verified by running**: 289 collected / 289 passed baseline; scoped mutation arm turns exactly those 48 red with an ARM=off control proving the harness loaded. Broad (unscoped) mutation inflates this to 81 by breaking legitimate `_hours_left`/`favour_view` reads of the same marker.
- **NOT IMPLEMENTED** -- decision only, no source file edited this turn.
- **Skill**: `a-widening-guard-still-charges-the-player`.
