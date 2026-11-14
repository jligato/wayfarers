# Multi-Language Migration Status — 2026-10-04

**Measured by:** orchestrator (gamedev), direct filesystem inspection.
**Method:** file existence + byte size via Python `os.stat`; directory listings.
**NOT yet verified:** compilation — with one exception. **Rust was run:**
`cargo check` and `cargo test` both pass in `rust/zones` (68 unit tests + 2
doc-tests, 0 failures, measured 2026-10-05). No `go build`, `mix test`, or
`dotnet build` result is in this note. The rest are listed under
"What is unproven".

---

## Summary table

| Layer | Language | Artifact on disk | Size | Compiles? |
|---|---|---|---|---|
| Persistence / admin | Python | `server/` (~200 modules) | large | Yes — suites run green |
| Gateway / auth | Go | `go-server/` + 8 `.pb.go` | 220 KB generated | **Unproven** |
| Zone simulation | Rust | `rust/zones/src/` (6 `.rs` files) | 150 KB source | Yes — `cargo check` + `cargo test` green (70 tests) |
| Matchmaking | Elixir | `elixir/matchmaking/lib/` (2 files) | 30 KB | **Unproven** |
| Client | C# (.NET) | `client/WayfarersCycle.csproj` + 1 script | 9 KB | **Unproven** |
| Contracts | Protobuf | `proto/` 8 `.proto` + README | 25 KB | Python stubs import |

---

## 1. Protobuf contracts — the single source of truth

**Location:** `proto/` — 8 files, plus `README.md` (10,579 B).

| Contract | Source | Python stub | Go stub |
|---|---|---|---|
| combat | `combat.proto` 3,256 B | `combat_pb2.py` 7,632 B | `combat.pb.go` 42,602 B |
| common | `common.proto` 1,698 B | `common_pb2.py` 1,950 B | `common.pb.go` 8,015 B |
| economy | `economy.proto` 3,197 B | `economy_pb2.py` 6,859 B | `economy.pb.go` 39,475 B |
| gateway | `gateway.proto` 2,199 B | `gateway_pb2.py` 2,366 B | `gateway.pb.go` 10,399 B |
| matchmaking | `matchmaking.proto` 2,244 B | `matchmaking_pb2.py` 2,952 B | `matchmaking.pb.go` 14,029 B |
| player | `player.proto` 2,821 B | `player_pb2.py` 6,487 B | `player.pb.go` 38,379 B |
| social | `social.proto` 4,073 B | `social_pb2.py` 8,513 B | `social.pb.go` 50,220 B |
| zone | `zone.proto` 2,679 B | `zone_pb2.py` 3,305 B | `zone.pb.go` 17,348 B |

**Godot descriptor:** `client/proto_gen/descriptor.bin` — 47,964 B.

**History note:** `player`, `combat`, `social`, `economy` sources were lost and
reconstructed from compiled stubs on 2026-10-04. Every message name, field name,
field number, field type and enum value was proven identical to the previous
descriptor before the reconstruction was accepted.

**Generator:** `tools/gen_proto.sh`, targets `python go rust elixir godot`.
Override the compiler with `WC_PROTOC`. The Rust target expects a plugin binary
named `protoc-gen-rust` (script line 227) and writes to `rust/gen/proto`.

---

## 2. Go — gateway and auth

**Module:** `github.com/wayfarers-cycle/go-server`, Go 1.27.

**Dependencies (measured from `go.mod`):**

| Dependency | Version | Direct? |
|---|---|---|
| `github.com/gorilla/websocket` | v1.5.3 | direct |
| `github.com/lib/pq` | v1.10.9 | direct |
| `go.uber.org/zap` | v1.27.0 | direct |
| `google.golang.org/protobuf` | v1.36.12 | **indirect** |

**Concern:** the protobuf runtime is marked `// indirect` but the generated
`.pb.go` files import it directly. It will need promoting to a direct dependency
the moment the generated packages are compiled — `go mod tidy` should do it.

**Present:** `go.sum`, `go-server/internal/gateway/`, `go-server/internal/auth/`,
and 8 generated packages under `go-server/gen/proto/`.

**Role:** WebSocket multiplexing, session-token validation, and a reverse proxy
that forwards unmigrated paths to the Python server.

---

## 3. Rust — zone simulation

**Crate:** `rust/zones/`, `Cargo.toml` 1,548 B.

| File | Size | Purpose |
|---|---|---|
| `src/lib.rs` | 2,566 B | crate root — declares the four modules, exports the zone-world types |
| `src/ecs.rs` | 19,673 B | entity/component world |
| `src/tick.rs` | 28,409 B | fixed-step tick loop |
| `src/json.rs` | 16,263 B | dependency-free strict JSON reader for the zone tables |
| `src/zone_data.rs` | 50,182 B | loader for the two shipped zone tables + their tests |
| `src/zone_data/tests.rs` | 36,890 B | drift tests: Rust must agree with the shipped JSON and Python |

**Source totals:** 6 `.rs` files, 153,983 B in `src/`; plus `Cargo.toml` 1,548 B
and `Cargo.lock` 3,525 B = 159,056 B for the crate excluding build output.

**Build output is not source.** `rust/zones/target/` holds 1,067 files and
344 MB — that is the compiler's scratch directory, not the zone module. Any
size figure for this layer must exclude `target/`, or it is off by three orders
of magnitude.

**ECS library:** `hecs` (already a dependency — confirmed, not switched).
**Generated protobuf:** `rust/gen/proto/` exists.

**Design requirement:** the tick loop must be deterministic — two identical
worlds given the same tick must produce identical results.

---

## 4. Elixir — matchmaking

**Project:** `elixir/matchmaking/`, `mix.exs` 1,422 B, `mix.lock` 269 B.

| File | Size | Purpose |
|---|---|---|
| `lib/queue.ex` | 9,966 B | matchmaking queue GenServer |
| `lib/lobby.ex` | 20,250 B | per-match lobby GenServer |
| `test/restart_test.exs` | 25,799 B | supervision restart test |

**Dependency:** the `protobuf` hex package is installed (`deps/protobuf` present).
**Baseline:** 15 tests were passing before this work began.

---

## 5. C# Godot client

**Project file:** `client/WayfarersCycle.csproj` — 266 B.

```xml
<Project Sdk="Godot.NET.Sdk/4.7.2">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <EnableDynamicLoading>true</EnableDynamicLoading>
    <Nullable>enable</Nullable>
    <RootNamespace>WayfarersCycle</RootNamespace>
  </PropertyGroup>
</Project>
```

**Scripts:** `client/scripts/tools/ProtoDescriptorLister.cs` — 8,877 B. A tool
that reads the protobuf descriptor at startup.

**Engine:** Godot 4.7.2 .NET (Mono) installed at `C:/Users/Boss/tools/godot-mono`.
**.NET SDK:** 10.0.401.

**State:** the C# project exists and one tool script is written. There is **no
C# game logic yet** — the client's gameplay remains entirely GDScript.

---

## 6. Supporting subsystems

| Directory | Contents | Purpose |
|---|---|---|
| `admin/` | `manage.py`, `requirements.txt`, `wcadmin/`, `gameadmin/`, `check_structure.py`, `README.md` | Django admin over the existing PostgreSQL database, read-only first |
| `analytics/` | `duckdb_queries.py`, `README.md` | DuckDB retention queries attached to PostgreSQL |
| `docs/` | `stack-transition-plan.md` + 7 others | repo-side copy of the decision record |

---

## What is unproven (the honest gap)

Every row below is a claim that has **not** been verified by running anything.
Do not treat any of it as working until the command has been run and its output
read.

1. **Go does not compile yet** — `go build ./...` has never been run against the
   8 generated packages or the gateway/auth code.
2. ~~**Rust does not compile yet**~~ — **RESOLVED 2026-10-05.** `cargo check
   --offline` finishes clean and `cargo test --offline` reports 68 passed / 0
   failed plus 2 doc-tests passed. This was the only layer in the table whose
   "Unproven" cell has been replaced by a run. Cross-language agreement with
   the Python zone tables is still *not* proven by that run — see item 7.
3. **Elixir protobuf not generated** — no `.ex` modules exist for the 8 contracts
   yet; `mix compile` and `mix test` not run since the work began.
4. **C# does not build yet** — `dotnet build` not run against the real client.
5. **The Godot parse gate has not been re-run** since the `.csproj` was added.
6. **Rust protobuf generation is blocked** — the crate that provides the
   `protoc-gen-rust` binary is `protobuf-codegen`, not `protoc-gen-rust` (which
   does not exist in the registry). `rust/gen/proto/` exists but its provenance
   is unconfirmed.
7. **No cross-language contract test exists** — nothing yet proves the Python,
   Go, Rust, Elixir and C# implementations agree on the same wire format.

---

## Recommended verification order

1. `cd go-server && go build ./...` then `go test ./gen/...` — cheapest, largest
   generated surface.
2. ~~`cd rust/zones && cargo check && cargo test`~~ — **DONE 2026-10-05**, both
   green. Remaining Rust gap is item 6 below (protobuf provenance), not the crate.
3. `cd elixir/matchmaking && mix.bat test` — must stay at 15+ passing.
4. `dotnet build client/WayfarersCycle.csproj` then the Godot parse gate.
5. A cross-language round-trip test: encode a message in Python, decode it in Go.

---

*Provenance: measured 2026-10-04 by the gamedev orchestrator. Every file size in
this note came from `os.stat`. No compilation claim is made.*
