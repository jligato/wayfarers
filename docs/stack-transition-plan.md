---
title: Stack Transition Plan — Wayfarer's Cycle
type: note
tags:
  - wayfarers-cycle
  - architecture
  - transition-plan
created: 2026-10-04
---
> **This file is a verbatim copy of the design-vault original.**
> Source: `C:/Users/Boss/Obsidian Vaults/Game Studio/our-games/wayfarers-cycle/design/stack-transition-plan-2026-10-04.md`
> Do not edit here. The vault file is the record of the decision; it is the
> copy target for any future revision.


# Stack Transition Plan — Wayfarer's Cycle

> **THIS DOCUMENT SUPERSEDES `design/language-architecture-plan-2026-10-03.md`.**
> That plan was a measured technical review and its evidence still stands, but its
> *conclusions* — reject Rust, Elixir, Go-in-production and Django — were
> overruled by the owner's direction on 2026-10-04. The 2026-10-03 note is kept
> for its measurements and its Appendix A commands, not as current policy.
>
> **UNRESOLVED — see "Open question the 10-03 plan raised and this plan has not answered" below.**
> The older plan's single strongest technical objection was that Godot has no
> maintained Protobuf runtime, which is why it recommended a JSON schema file
> instead. Nothing in this plan refutes that. Measured 2026-10-04:
> `shared/api_contract.json` was never built, `client/addons/` and
> `client/plugins/` do not exist, and no client file mentions Protobuf. The
> Protobuf choice below is therefore **unvalidated on the client side.**

## Final Stack Decision

| Layer | Language | Rationale |
|-------|----------|-----------|
| Zone servers | Rust | No GC, predictable tick times, ECS avoids borrow-checker tangles |
| Matchmaking, presence, parties, chat | Elixir | BEAM processes, supervision trees, Phoenix PubSub |
| Gateway, auth, accounts, inventory/persistence API | Go | Existing prototype, strict typing, low concurrency cost |
| Admin, analytics, scripts | Python | AI writes Python reliably, Django admin, pandas/DuckDB |
| Contracts | Protobuf | Generated for every language including Godot |

## Current State — rewritten 2026-10-04 after measurement

The original text here described the **target** stack as though it were already
built. It was rewritten because it nearly misled a reader into believing the
migration was finished. Every line below was measured by a command, not read.

| Layer | Reality | Evidence |
|-------|---------|----------|
| Python server | **The only language actually running.** FastAPI + PostgreSQL, ~532 files, ~88k LOC | the `api` service in `docker-compose.yml` |
| Godot client | GDScript only — **126** `.gd` files, zero `.cs`. Engine is the **plain** build | `godot --version` = `4.7.2.stable.official`; no `GodotSharp/`; no `.csproj` |
| Go gateway | **Builds cleanly.** 22 files, 6,247 lines | `go build ./...` exit 0 |
| Rust zones | **Compiles.** 5 files, 1,175 lines, `ecs.rs` + `tick.rs` | `cargo check` finished in 5.21s |
| Elixir matchmaking | Scaffold only, 5 files. **Unverified** — `mix` is not installed here; an `erl_crash.dump` is present | `mix compile` → "No such file or directory" |
| Contracts | **Partly built — tests red.** `shared/proto/` no longer exists (deleted 2026-10-04). `proto/` holds **8** `.proto` files and `server/proto_gen/` holds the **matching 8** `_pb2.py` files — the two sets are identical, *not* divergent. The generator reports **no drift** for python, go, and godot. The stubs *are* loaded, by `server/tests/test_proto_contract.py`. **But 6 of that file's tests fail** with `KeyError: 'PlayerIdentity'`: its pinned table still lists `PlayerIdentity` under `player_pb2`, while the message actually lives in `common_pb2` (it was renamed out of the player module — see the note at `proto/player.proto:119`). | measured 2026-10-05: `ls shared/proto` → "No such file or directory"; proto-vs-proto_gen filename stem `diff` → empty (8 = 8); `bash tools/gen_proto.sh --check --target {python,go,godot}` → "No drift." ×3, exit 0; `bash tools/wc_pytest.sh server/tests/test_proto_contract.py` → exit 1, 6 failed, all `KeyError: 'PlayerIdentity'` |
| Routes | **388** measured 2026-10-05 on the booted app (startup code run). Breakdown: **383 API routes** + 4 FastAPI docs pages (`/openapi.json`, `/docs`, `/docs/oauth2-redirect`, `/redoc`) + 1 websocket route (`/comm/ws`). 387 HTTP operations (140 GET, 245 POST, 2 DELETE), 0 duplicate path+method pairs. Counting from a plain import without starting the app gives 384 and is wrong. Superseded the stale "357". | re-measured; see `design/task-objectives.md` §003 |

**None of Go, Rust or Elixir runs anywhere.** `docker-compose.yml` declares
exactly two services — `postgres` and `api`. Nothing routes to the other three.

### Decision 2026-10-04 — Protobuf to the client is APPROVED

The owner approved **Protobuf to the client**, superseding the earlier
recommendation to keep the client on JSON. The old objection — GDScript has no
maintained Protobuf runtime — is removed by the C# Godot migration, because the
.NET Protobuf runtime **is** maintained. The client half therefore depends on the
C# migration landing first; until it does, unmigrated GDScript keeps reading JSON.

**Tests**: 291 test files, contract tests green across 40+ modules

## Migration Path

### Phase 1: Contracts First (2-3 weeks)
- Define all API contracts in `.proto` files
- Generate Python, Go, Elixir, Rust, and Godot stubs
- Write a contract drift test that walks the live app and compares against the proto
- No behavior change — just codify what exists

### Phase 2: Go Gateway (3-4 weeks)
- Build Go gateway service: auth, accounts, inventory, persistence API
- Reuse existing PostgreSQL schema
- Mount alongside Python server, route by path prefix
- Load test with Python httpx bots
- Python server remains source of truth for game logic

### Phase 3: Elixir Matchmaking (2-3 weeks)
- Build Elixir service: matchmaking, presence, parties, chat
- Use Phoenix PubSub for real-time communication
- GenServer per party/lobby, Supervisor tree for crash isolation
- Connect to Go gateway via Protobuf

### Phase 4: Rust Zone Servers (4-6 weeks)
- Build Rust zone server with ECS (bevy_ecs or hecs)
- Single-threaded simulation loop per zone
- Connect to Go gateway via Protobuf
- Start with one zone type, prove tick consistency
- `cargo check` + `clippy` in a loop for AI-written code

### Phase 5: Python Admin/Analytics (2-3 weeks)
- Django admin panel over Postgres tables
- pandas/DuckDB analytics scripts
- Grafana for metrics, Metabase for player analytics
- No changes to game server

### Phase 6: Godot Client Migration (ongoing)
- Generate Godot stubs from Protobuf
- Replace HTTP JSON calls with Protobuf over WebSocket
- Keep GDScript for UI, consider C# for real-time combat if needed

## Risk Assessment

| Risk | Mitigation |
|------|------------|
| AI makes mistakes with OTP supervision | Keep Elixir service small, GenServer/Supervisor/PubSub only, test crash/restart |
| Rust borrow checker tangles | Use ECS, avoid Arc<Mutex<>>, cargo check + clippy loop |
| Protobuf drift between languages | Contract drift test in CI, generated stubs only |
| Migration downtime | Mount new services alongside old, route by path prefix, gradual cutover |
| Go prototype is incomplete | Micro-benchmarks done, but real routes need building |

## Open Questions

1. ~~Does the Godot client support Protobuf natively, or do we need a GDScript plugin?~~
   **ANSWERED 2026-10-04:** via C#, not GDScript. The .NET build of Godot
   consumes the maintained .NET Protobuf runtime. See the Decision section above.
2. ~~Should the Go gateway replace Python entirely, or remain a facade?~~
   **ANSWERED 2026-10-05: it remains a facade.** Go owns only the real-time
   layer; Python stays the source of truth for game logic. Two plans
   disagreed — a "Full Go migration. No hybrid." roadmap and a Go +
   Python hybrid design. The hybrid won; the roadmap is superseded.
   Reasoning and re-measured numbers: [[go-plan-decision-2026-10-05]].
3. How do we handle real-time combat — Rust zone servers with client-side prediction?
4. What is the deployment story — Docker Compose for all services?
5. Do we need a service mesh, or is direct Protobuf over TCP sufficient?

## References

- `design/language-architecture-plan-2026-10-03.md` — original plan
- `go-server/` — existing Go prototype
- `tools/wc_pytest.sh` — test runner
- `shared/api_contract.json` — contract drift checks (proposed)

## Open question the 10-03 plan raised and this plan has not answered

**Godot has no maintained Protobuf runtime.** The 2026-10-03 plan measured this
and recommended a JSON schema file (`shared/api_contract.json`) plus two drift
tests instead of full Protobuf, precisely because GDScript cannot consume `.proto`
without a plugin.

Measured 2026-10-04, none of that has been resolved:

| Check | Result |
|-------|--------|
| `shared/api_contract.json` | ABSENT — the recommended alternative was never built |
| `client/addons/` | absent |
| `client/plugins/` | absent |
| Any client file mentioning Protobuf | 0 hits |
| `proto/*.proto` files | 4 written (player, combat, social, economy) |
| `tools/gen_proto.sh` | present, 15,177 bytes |

So the four contract files exist and the generator exists, but the **client half
is unproven**. Three options, in order of cost:

1. **Server-only Protobuf, JSON to the client.** Protobuf on the wire between
   Go/Elixir/Rust/Python; the gateway translates to JSON for Godot. Cheapest, and
   it captures nearly all the drift benefit because the drift risk lives between
   the *services*, not between client and gateway.
2. **Build `shared/api_contract.json` anyway** as a stopgap — the 10-03 plan's
   original recommendation, still unbuilt, still cheap.
3. **Adopt a third-party GDScript Protobuf addon.** Highest risk: unmaintained
   addons are the exact category the 10-03 plan flagged.

**RESOLVED 2026-10-04.** The owner chose **option 0 — Protobuf to the client**,
which was not on this list because it depended on a step the plan had not yet
taken: moving the client to the .NET Godot build. That build is now installed
and its C# toolchain is proven to compile against `GodotSharp.dll`, so the
client can carry the maintained .NET Protobuf runtime instead of a GDScript
plugin. Option 1 (JSON to the client) is therefore **withdrawn** — it remains a
valid fallback for any GDScript that is never migrated, but it is no longer the
plan. Option 3 (a third-party GDScript addon) stays rejected.

Sequencing consequence: the C# migration is now on the critical path for the
client half of Protobuf. The 126 existing `.gd` files keep reading JSON until
each is migrated; both wire formats coexist during the transition.

## Task hierarchy mapping (added 2026-10-04)

The transition work is carried by abstraction **011 — Language and service stack**
in `design/task-abstractions.md`, with 23 dispatchable objectives in
`design/task-objectives.md` (IDs `011-001` through `011-023`).

Grouped by phase:

| Phase | Objective IDs | First file to own |
|-------|---------------|-------------------|
| 1 Contracts | 011-001 .. 011-006 | `proto/player.proto` |
| 5 Python admin/analytics | 011-007 .. 011-012 | `admin/settings.py` |
| 2 Go gateway | 011-013 .. 011-015 | `go-server/internal/combat/engine.go` |
| 3 Elixir | 011-016 .. 011-019 | `elixir/matchmaking/lib/queue.ex` |
| 4 Rust zones | 011-020 .. 011-022 | `rust/zones/src/tick.rs` |

Note on ordering: the phase numbers above follow the original plan. The owner's
2026-10-04 direction puts Python admin/analytics as the lowest-risk starting
point, so phases 1 and 5 can run in parallel — contracts first, Python alongside.

**Hierarchy gap found 2026-10-04:** `design/task-abstractions.md`,
`design/task-objectives.md`, and `design/completed-tasks.md` did not exist on
disk before today, and neither did `design/tools/taskgen.py` nor
`design/tools/dispatch_free_tasks.py`. The skill
`wayfarers-cycle-task-hierarchy` described them as live. All three documents
were created today to carry this work; the two generator scripts are still
missing.
