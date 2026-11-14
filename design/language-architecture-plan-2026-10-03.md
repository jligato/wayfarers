# Wayfarer's Cycle — Language and Architecture Plan

Date: 2026-10-03
Author: architecture review (AI-assisted)
Scope: which programming language runs which part of Wayfarer's Cycle, and what
to do about a list of twelve proposals from another AI.

Every number in this document was measured by running a command against the
repository. The original pass was 2026-10-03; every figure in section 2.1 was
re-measured on **2026-10-05** and now carries its own command. Commands are also
listed in Appendix A so a skeptical reader can repeat them.

---

## 1. Plain-language summary

The other AI's list assumes this game is a fast-paced action shooter with
thousands of players fighting each other in real time on a server. It is not.
Wayfarer's Cycle is an **idle cultivation RPG** — the genre where the game plays
while you sleep. Combat is turn-based, the server resolves it, and the phone
mostly draws numbers going up.

That mismatch drives most of the list. Three of the twelve proposals (a real-time
combat server in C#, a zone server in Rust, a matchmaking server in Elixir)
exist to solve problems this game does not have yet. Four more (Protobuf
contracts, load-test bots, database backups, an internal admin panel) are
genuinely good and cheap, and should be adopted.

The single most important finding: the working parts of this project are the
Python server and the GDScript (Godot's scripting language) client. Both work
today. Nothing in the list justifies replacing them.

---

## 2. Current stack assessment (measured)

### 2.1 What exists

Every figure below was measured on **2026-10-05** (the original figures in this
document were measured on 2026-10-03 and have since drifted). Each row names the
exact command that produced it, so any reader can re-run it and get a fresh
number. Run every command from the repository root (`G:\Projects\wayfarers-cycle`).

| Layer | What is actually there | Size measured 2026-10-05 | Command |
|---|---|---|---|
| Server language | Python, FastAPI web framework | 687 `.py` files, 461,327 lines | `find server -name "*.py" -type f \| wc -l` and `find server -name "*.py" -type f -print0 \| xargs -0 cat \| wc -l` |
| HTTP surface | FastAPI route decorators | 499 route handlers | `grep -rE "@(app\|router)\.(get\|post\|put\|delete\|patch)" server --include="*.py" \| wc -l` |
| Client engine | Godot, official standard build 4.7.2 | `godot.exe` on this machine | `godot --version` |
| Client language | GDScript (Godot's own language) | 125 `.gd` files, 37,520 lines | `find client -name "*.gd" -type f \| wc -l` and `find client -name "*.gd" -type f -print0 \| xargs -0 cat \| wc -l` |
| Database | PostgreSQL 16 in Docker | 31 SQL migration files | `find server/migrations -name "*.sql" -type f \| wc -l` |
| Go prototype | `go-server/` | 17,178 lines across 38 `.go` files | `find go-server -name "*.go" -type f -print0 \| xargs -0 cat \| wc -l` |
| Tests | pytest suites plus headless Godot harnesses | 1,001 `test_*.py` files | `find . -name "test_*.py" -type f -not -path "./.git/*" \| wc -l` |

Two counting caveats, so nobody is surprised by a different total:

- Client scripts whose name starts with an underscore are counted above. Excluding
  them gives **111 files / 35,600 lines**
  (`find client -name "*.gd" -not -name "_*" -type f`).
- Test files are spread across three places: 509 inside `server/`, 434 inside the
  `tests/` and `server/tests/` directories the old Appendix A command named, and
  4 at the repository root. The 1,001 figure is the whole repository, which is the
  honest "how much test code exists" number.

**These counts drift.** This repository is shared with other agents writing to it
right now. The original 2026-10-03 run gave 521 and then 532 Python files ten
minutes apart; the 2026-10-05 run gave 461,329 lines on the first pass and
461,327 on a repeat moments later, because peers are mid-edit. Treat every number
here as "measured at this moment, by the command shown", not as a fixed property.

### 2.2 The Python server is not a prototype — it is the product

- 499 route handlers (measured 2026-10-05) covering cultivation, breakthrough,
  combat, PvP, guilds,
  battle pass, trading, chat, achievements, towers, dungeons and beast waves.
- A background scheduler thread (`EventScheduler`) that runs on a 60-second
  cadence and opens and closes live events.
- A write path that moves real currency. This matters enormously: the money
  routes already had to be hardened against double-claim and forged requests
  (see `docs/payout-route-security-audit-2.md`,
  `docs/dupe-payout-auth-2026-10-03.md`).
- 1,001 Python test files repository-wide (measured 2026-10-05). One commit
  message records "recovering 249 tests" — the suite is load-bearing, not
  decorative.

### 2.3 The client is a real Godot project, and the AI's premise about it is wrong

- The client is **Godot 4.7.2, standard (non-C#) build**. Confirmed by running
  `godot --version`.
- `client/project.godot` declares `config/features=PackedStringArray("4.2", ...)`.
  This is a stale feature tag from when the project was created; the engine
  running the game is 4.7.2.
- **There is no C# toolchain installed and no `.csproj` anywhere in `client/`.**
  The .NET version of Godot is a *separate download* from the one on this
  machine. Adopting C# means reinstalling the engine.

### 2.4 The game is turn-based and server-authoritative

This is the load-bearing fact for the whole review. From `server/combat_config.py`:

```
AUTO_BATTLE_TICK_SECONDS = 2.0   # each turn every 2 seconds
AUTO_BATTLE_MAX_TURNS = 100       # auto-loss after 100 turns
```

A 2-second turn with a hard 100-turn cap is a turn-based auto-battler. PvP is
**asynchronous ghost battles**: the server stores a finished battle log and
re-simulates it deterministically from a seed. From `docs/top-priorities.md`,
the server "rebuilds the exact source seed, replays the battle, and rejects
(409) any stat line or rating that does not reproduce the stored log."

There is **no real-time netcode anywhere in this game today.** The only
WebSocket client code is chat (`client/scripts/network/CommClient.gd`,
`client/scenes/game/ChatPanel.gd`). One file, and it is text chat.

### 2.5 The Go prototype is a benchmark harness, not a server

Measured 2026-10-05, `go-server/` is **17,178 lines across 38 `.go` files** — it
has grown roughly twentyfold since the 859-line / 8-file figure recorded on
2026-10-03. Breaking it down:

- 7,067 lines (8 files) of generated Protobuf code under `go-server/gen/` — the
  economy, player, combat and social message stubs. Nobody hand-wrote these.
- 4,073 lines (17 files) of hand-written production code, excluding tests and
  generated code. A gateway (`internal/gateway/`) and an auth layer
  (`internal/auth/`) now exist alongside the original combat and WebSocket code.
- The rest is Go test files, several of them large (`session_racefix_test.go` at
  1,026 lines, `guard_test.go` at 875).

The growth is real but still unreferenced: `grep -rn "go-server" client --include=*.gd`
returns 0 hits, and `grep -rln "gateway" client --include=*.gd` returns 0 hits.

Its README states targets: 20 Hz tick, under 1 ms latency, 10,000+ concurrent
players. **No client code references port 8080 or `go-server` at all.** Measured:
a grep for `8080|go-server` across all `.gd` files returns zero hits.

So the Go prototype is disconnected from the shipped game. It is a hypothesis
written down, and it is 0.2% of the size of the Python server it would replace.

### 2.6 Nothing else in the list is present

Measured greps for `grafana`, `metabase`, `django`, `duckdb`, `pandas`, `.proto`,
`locust`, `k6`: **zero hits** outside `.venv` third-party packages. No
`requirements.txt` beyond a one-line `requirements-test.txt` containing only
`fastapi`. No `Dockerfile` at repo root despite `docker-compose.yml` referencing
`dockerfile: Dockerfile`. No backup tooling.

---

## 3. Evaluation of the twelve proposals

Each verdict is against five questions: does the current code support it, does it
fit a solo developer with AI writing the code, does it fit an idle cultivation
RPG, does it build on the existing Go prototype, and is the migration cost worth
the benefit.

### 3.1 Protobuf contracts first — **ADOPT, in a reduced form**

- Current state: no `.proto` files anywhere. The contract between client and
  server is implicit — the client reads JSON keys, and a renamed key breaks it
  silently.
- Solo-dev fit: good. Protobuf is generated code in three languages from one
  source file. An AI can edit a `.proto` file reliably; it cannot be trusted to
  hand-maintain three matching parsers.
- Genre fit: neutral-to-good. Contract drift is a real bug class regardless of
  genre.
- Go prototype: neutral.
- Migration cost vs benefit: **full Protobuf is too expensive right now.** See
  section 4.

### 3.2 C# instead of GDScript for real-time combat — **REJECT**

- Current state: actively blocked. The installed Godot is the standard build with
  no .NET runtime. There is no `.csproj` in the client. Switching means
  reinstalling the engine, adding the .NET SDK, and re-exporting for Android.
- Solo-dev fit: **poor.** A solo developer with AI assistance can write GDScript
  and Python fluently. Adding a third language plus a build-toolchain change
  multiplies the ways a build can break, and AI assistance is weakest across
  unfamiliar toolchains.
- Genre fit: **poor.** Real-time combat prediction and rollback netcode solve
  input-latency artefacts in action games. This game resolves a turn every two
  seconds and the server is the authority. There is nothing to predict.
- Migration cost: rewriting 125 GDScript files / 37,520 lines (measured
  2026-10-05) plus every headless test harness. Weeks of work for zero gameplay
  benefit.

### 3.3 GDScript for scenes and UI only — **REJECT as stated, KEEP the current mixed approach**

The proposal implies moving all game logic out of GDScript. That is a large
refactor with no measured defect behind it. GDScript's current failure modes
(large scripts, duplicated logic) are real but are not currently costing
playable time. Keep GDScript for presentation and light client logic, and treat
the rule as a *preference for new code*, not a mandate to migrate old code.

### 3.4 Go for gateway, matchmaking, zone servers, persistence — **DEFER, do not adopt now**

- Current state: a 17,178-line disconnected prototype (measured 2026-10-05).
  Nothing in the client or tests points at it.
- Solo-dev fit: poor. Two server languages means two deployment pipelines, two
  sets of bugs, two test harnesses.
- Genre fit: a gateway and matchmaking matter at player counts this game does not
  have. It targets soft launch on Android.
- Verdict: the prototype is worth **keeping on disk** as a reference and is not
  worth wiring up. Revisit if measured concurrent players exceed what one Python
  process can serve (section 6 gives the test that decides this).

### 3.5 Rust for zone servers (tick consistency) — **REJECT**

- Tick consistency solves deterministic simulation in real-time multiplayer.
- This game **already has** deterministic simulation and it is written in Python:
  ghost battles use SHA-256-derived seeds precisely so a fight re-simulates
  byte-identically. The problem the proposal names is already solved, in a
  different way, and it works.
- Solo-dev fit: worst of all twelve. Rust's borrow checker fights AI-authored
  patches constantly.
- Migration cost: highest of any option. Reject.

### 3.6 Elixir for matchmaking (crash isolation) — **REJECT**

- Crash isolation matters when a single crash takes down matchmaking for everyone.
  With one Python process and no matchmaking system, there is no blast radius to
  isolate.
- Highest new-language cost, lowest current benefit. Reject.

### 3.7 Python for admin tools, analytics, scripts — **ADOPT, already true**

Python is already the server language, so admin scripts and analytics queries in
Python have zero new dependencies. This proposal costs nothing and is already
the de facto state. Adopt as policy, not as a change.

### 3.8 Load test bots — **ADOPT, this is the most valuable item in the list**

There is currently **no load testing at all**. For a game whose entire risk is
"will the server survive launch," this is the biggest gap. See section 7.

### 3.9 Django admin for the internal admin panel — **REJECT Django, ADOPT the idea**

- The idea is right: internal operators need a web panel to look at players and
  events without hand-writing SQL.
- Django specifically is wrong here. It would be a **second web framework**
  alongside FastAPI, pulling in its own ORM, settings, migrations and auth system.
  Django's admin works by reading Django's own models; Wayfarer's data lives in
  PostgreSQL tables written by hand-rolled stores.
- Recommendation: build the panel as FastAPI routes under `/admin/*`, reusing the
  existing admin-token guard already present in `server/auth.py`. Use
  FastAPI's automatic OpenAPI documentation page as a free internal console until
  a real UI exists.

### 3.10 pandas/DuckDB for analytics — **ADOPT, deferred**

- Fits Python, no new language, no new service.
- Not needed at zero players. Adopt when there are enough real rows to be worth
  querying. DuckDB reads PostgreSQL directly and is a single binary with no
  server to run — lower operational cost than pandas alone.

### 3.11 Grafana + Metabase for dashboards — **PARTIAL**

- Metabase: reasonable. It is a single Docker container pointed at the existing
  PostgreSQL, read-only. Low cost, no new code.
- Grafana: it is for time-series metrics. This game's server does not emit
  metrics. Running Grafana with nothing to graph is a maintenance cost with no
  payoff.
- Recommendation: Metabase only, when there is data worth seeing. Skip Grafana
  until the load-test work in section 7 produces numbers worth graphing.

### 3.12 Automatic PostgreSQL backups with point-in-time recovery — **ADOPT, highest priority after load tests**

- Current state: **no backups of any kind.** Nothing in `scripts/`, `cron/`,
  `tools/` or `docs/` runs `pg_dump`.
- A currency-paying game with no backups is the single largest operational risk in
  this project. See section 8.

### 3.13 Scorecard

| # | Proposal | Verdict |
|---|---|---|
| 1 | Protobuf contracts | Adopt, reduced scope |
| 2 | C# for real-time combat | Reject |
| 3 | GDScript for UI only | Reject as mandate, prefer for new code |
| 4 | Go for gateway/matchmaking/zones | Defer |
| 5 | Rust for zone servers | Reject |
| 6 | Elixir for matchmaking | Reject |
| 7 | Python for admin/analytics | Adopt (already true) |
| 8 | Load test bots | Adopt |
| 9 | Django admin | Reject Django, build in FastAPI |
| 10 | pandas/DuckDB | Adopt, deferred |
| 11 | Grafana + Metabase | Metabase only, deferred |
| 12 | Postgres backups + PITR | Adopt, high priority |

---

## 4. Recommended stack

**Keep exactly what is running. Add instrumentation and safety. Add no new
programming languages this year.**

| Layer | Language | Change |
|---|---|---|
| Game server | Python 3 + FastAPI | No change |
| Client | Godot 4.7.2 + GDScript | No change |
| Database | PostgreSQL 16 | Add backups |
| Admin tooling | Python | New scripts |
| Analytics | DuckDB (SQL) | New, deferred |
| Load testing | Python | New |
| Admin panel | FastAPI routes | New, deferred |
| `go-server/` | Go | Parked, not deployed |

The governing principle: **a solo developer with AI assistance should have one
server language and one client language until a measurement says otherwise.**
Every additional language is a permanent tax on every future change, paid by a
person who is the only reviewer.

### 4.1 Protobuf contract design (reduced scope)

Full Protobuf means generated parsers in Python and GDScript plus a build step in
both. GDScript has no maintained Protobuf runtime, so this is a real cost.

**Recommended: a generated-contract-free approach instead.** Write the contract
once as JSON Schema, and add two cheap checks:

1. **A single source-of-truth file** at `shared/api_contract.json` listing every
   route path, its method, its required fields and the fields the server promises
   to return.
2. **A test that walks the live FastAPI app** and asserts every path in the
   contract file exists with the declared method, and that each declared response
   field appears in the server's actual response. This is the drift check, and it
   runs in the existing pytest suite with no new tooling.
3. **A client-side static scan** that reads every `.gd` file's `get("key")` calls
   and asserts each key exists in the contract. A repo tool for this already
   exists in spirit: `tools/wc_citation_checker.py`.

Why not Protobuf: the drift this catches is field-level, and the drift that
actually bites this project (documented in section 2.2's audit references) is
**semantic** — a route that returns success while refusing to pay. A schema file
catches the first kind. A schema file will never catch the second; a test that
proves the money lands will.

**Revisit Protobuf only if** the Go server is ever wired in, because at that
point two server languages share one wire format and hand-maintained structs in
two languages will genuinely drift.

---

## 5. Migration path

### Keep unchanged (the vast majority of the work)

- All 687 Python files, 499 routes, the background scheduler, the store layer
  (counts measured 2026-10-05; re-run the commands in 2.1 before quoting them).
- All 125 GDScript files and every headless test harness.
- The `go-server/` directory stays on disk, unmodified, as a reference. It is not
  deleted (it is the cheapest possible answer to "what if we need real-time
  later") and not deployed.

### Change (small, in priority order)

1. Add `scripts/backup_postgres.sh` — daily `pg_dump` plus continuous WAL
   archiving. Section 8.
2. Add `tests/load/` — a Python load-test bot. Section 7.
3. Add `shared/api_contract.json` and the two drift tests. Section 4.1.
4. Fix the two loose ends found during this review, which are not language issues
   but block any deployment: `docker-compose.yml` references `dockerfile:
   Dockerfile` and **no Dockerfile exists at the repo root**.

### Explicitly not doing

- No C# rewrite of the client.
- No Rust or Elixir services.
- No Go service in production.
- No Django.
- No new framework in the server process.

---

## 6. Load testing strategy

This is the adopted work that matters most, because it replaces guesswork with
measurement.

### 6.1 What it must measure

The question that decides the architecture is narrow and answerable: **how many
simultaneous active players can one Python server process handle before requests
stop meeting a response-time target?**

Pick the target first: 95th-percentile response under 500 ms for reads, under
1 s for currency writes.

### 6.2 The bot

- **Language: Python**, using `httpx` (async) or `aiohttp`, run with `asyncio`.
  Rationale: it must speak the same auth flow as the real client, and Python is
  already the language of the server's own test suite (`tools/wc_pytest.sh`).
- **Where: `tests/load/`** in the repo, or the scratch directory for throwaway
  runs. It must never point at production by accident: require an explicit
  `--target` flag and refuse to run if the host looks like a production name.
- **A realistic player is not a loop of GETs.** The bot should replay a scripted
  session: log in, claim offline cultivation progress, attempt a breakthrough,
  open a panel, and — most importantly — attempt a *paid* action, because that is
  the path where a database lock will show up first.
- **Login minting:** measure a real login flow rather than reusing one token.
  A bot with a pre-minted token measures nothing about session handling.

### 6.3 The three runs that matter

1. **Read-only ramp.** Ramp concurrent players, plot 95th-percentile latency.
   This finds the point where the Python event loop saturates.
2. **Write ramp.** Same ramp with currency-writing actions. Expect this to break
   much earlier than the read test if there are row-lock contention issues.
3. **Thundering herd.** 200 players all claiming offline progress at the same
   instant. This is the real launch scenario — an idle game is a game where
   everyone logs in after waking up.

### 6.4 The decision rule

Write the rule down **before** running the tests, so the answer is not chosen
after the fact:

- If one process handles the target with headroom → stay single-process Python.
  Do not touch the architecture.
- If it is close → add process workers behind one load balancer. Still Python.
- Only if the ceiling is a per-process limit that more workers cannot fix (for
  example, a single-process database connection bottleneck) does the Go
  prototype get reconsidered — and at that point it is a gateway, not a rewrite.

### 6.5 Tooling choice

Hand-rolled Python beats k6 or Locust here for one reason: the bot must replay
this game's specific auth and currency flows. k6 is excellent for generic HTTP
load and cannot express a signed-in player claiming offline progress. Revisit if
load profiles become simple enough for a generic tool.

---

## 7. Admin and analytics tooling

### 7.1 Admin panel — FastAPI, not Django

- Build read-and-act routes under `/admin/*` in FastAPI, behind the existing
  admin-token guard in `server/auth.py`. That guard already exists and already
  documents the `/admin/*` surface.
- Until that exists, the **automatic API documentation page FastAPI generates is
  the admin console.** It is free, already running, and lets an operator read any
  route's shape and try it.
- Do not add Django. It would be a second framework, its own ORM, its own
  migrations and its own auth, all to display tables another system owns.

### 7.2 Analytics — DuckDB first, pandas only if needed

- DuckDB reads PostgreSQL directly and is one binary. Zero server to run.
- Start with plain SQL questions the solo developer actually asks: how many
  players logged in today, what is the median time to first breakthrough, what
  fraction of players have claimed offline progress.
- Add pandas only if the questions need reshaping that SQL handles badly.

### 7.3 Dashboards — Metabase only, later

- Metabase is one Docker container with a read-only database user. It costs one
  container and no code.
- **Skip Grafana.** The server emits no metrics, so there is nothing to graph.
  Do not run a dashboard tool over an empty data source.

---

## 8. Backup strategy

**This is the highest-severity gap found in the review.** The game pays real
currency, stores it in PostgreSQL, and has **no backups of any kind.** Measured:
no `pg_dump` reference exists in `scripts/`, `cron/`, `tools/` or `docs/`.

### 8.1 What to set up

**Daily logical backup, plus continuous recovery.**

- **Daily `pg_dump`** (compressed custom format) to a directory outside the repo.
  Cheap, restorable in minutes, catches application-level corruption.
- **Continuous archiving (point-in-time recovery)** by shipping the write-ahead
  log to durable storage. This is the difference between losing an hour and losing
  a day. PostgreSQL's `archive_mode = on` plus a shipping script does it.
- **Retention:** keep 14 daily dumps and 7 days of logs. Disk is cheap; a
  restoration drill that has not been run is not a backup.
- **Offsite copy.** A backup on the same machine as the database protects against
  nothing. Ship to a second provider.

### 8.2 The part everyone skips

**Restore drill, on a schedule.** Once a month, restore the most recent dump into
a scratch database and run the test suite against it. An untested backup is a
hypothesis. This project already has the tooling to make the drill cheap:
`tools/wc_pytest.sh` runs the suite against a chosen database.

### 8.3 Do not back up the signing keys this way

`docs/android-signing-key-exposure-and-rotation-plan.md` covers the Android
signing key. That key must **not** be in the database dump path and must not share
a backup lifecycle with player data. Read that document before touching it — the
Android build depends on it.

---

## 9. Risk assessment

### 9.1 Risks of the current stack

| Risk | Severity | Mitigation |
|---|---|---|
| No backups of a currency-paying database | **Critical** | Section 8, first thing |
| Single developer is a single point of failure | High | Keep the language surface small; AI assistance helps with volume, not with judgement |
| 4,019-line `server/main.py` | Medium | Extract as it changes, not as a project |
| Stale Godot feature tag (`"4.2"` in project.godot while running 4.7.2) | Low | Correct the tag; verify the project still opens |
| `docker-compose.yml` references a missing `Dockerfile` | Medium | Write it, or the container path does not work |
| No metrics emitted | Medium | Instrument during load testing |

### 9.2 Risks of adopting the other AI's list

| Risk | Severity | Why |
|---|---|---|
| Client rewrite in C# | **Critical** | Reinstall the engine, rewrite 37,520 lines of GDScript (measured 2026-10-05), add a toolchain, for a latency problem this genre does not have |
| Three new server languages | **Critical** | Four deployment pipelines for one developer, each a permanent tax |
| Full Protobuf with GDScript runtime | High | No maintained GDScript runtime; new build step in both languages |
| Django alongside FastAPI | High | Second framework, second ORM, second auth system |
| Grafana over no metrics | Low | Operational cost, zero benefit |

### 9.3 The single assumption a skeptic should attack

Every rejection above rests on one claim: **this game is turn-based and
server-authoritative, and will stay that way.** The evidence is the 2-second turn
tick in `server/combat_config.py` and the deterministic ghost-battle replay in
`docs/top-priorities.md`. If the design changes to a live PvP arena where players
fight each other in real time with movement and abilities, then recommendations
2, 4 and 5 become live questions and the Go prototype stops being a curiosity.

That is a design decision, not a technical one, and it should be recorded
explicitly in the design notes so the next reviewer does not have to re-derive it.

---

## Appendix A — Commands used to produce this document

Run from the repository root on 2026-10-03.

```
godot --version
find server -name "*.py" ... | wc -l
find server -name "*.py" ... -print0 | xargs -0 wc -l | tail -3
grep -rEo "@(app|router)\.(get|post|put|delete|patch)\(" server --include=*.py | wc -l
find client -name "*.gd" -not -name "_*" -print0 | xargs -0 wc -l | tail -3
find go-server -name "*.go" -print0 | xargs -0 wc -l | tail -12  # -> 17178 total, 38 files (2026-10-05)
grep -rl "websocket" client --include=*.gd          # -> 1 file (chat only)
grep -rn "8080|go-server" client --include=*.gd      # -> 0 hits
grep -rli "grafana|metabase|django|duckdb|pandas" . # -> 0 hits outside .venv
grep -rn "AUTO_BATTLE_TICK_SECONDS" server/combat_config.py
ls server/migrations | tail -5                      # -> 031
find . -name "test_*.py" -type f -not -path "./.git/*" | wc -l   # -> 1001 (2026-10-05)
find client -name "*.csproj" -o -name "*.sln"       # -> 0 results
ls Dockerfile                                      # -> no such file
```

## Appendix B — Documents consulted

- `README.md` — project status and tech stack table
- `docs/top-priorities.md` — ghost-battle determinism design
- `docs/offline-rate-design-decision.md` — offline credit model
- `docs/feature-roadmap.md` — feature status
- `docs/postgres.md`, `docs/POSTGRES-TEST-DATABASE.md` — database setup
- `docs/android-signing-key-exposure-and-rotation-plan.md` — key handling
- `go-server/README.md` — Go prototype targets
- `design/dupe-payout-auth-2026-10-03.md`,
  `design/payout-route-security-audit-2.md` — currency-route hardening
- `tools/wc_pytest.sh` — the batch test runner every change must use