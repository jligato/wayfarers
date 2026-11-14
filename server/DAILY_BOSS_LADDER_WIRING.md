# Daily Boss LADDER — wiring instructions and build estimate

Prepared 2026-10-01 for the ladder feature: a day is a SEQUENCE of three bosses,
reset at 05:00 UTC, with the shared health and the once-only payout both
enforced by the database.

## 1. What is in this change

| Piece | Path | Status |
|---|---|---|
| Migration, four tables | `server/migrations/019_daily_boss_ladder.sql` | new, applied and verified |
| Engine | `server/daily_boss_ladder.py` | new |
| Routes | `server/daily_boss_routes.py` | new, **mounted** (measured 2026-10-05) |
| Tests | `server/tests/test_boss_ladder_routes.py` | new, 13 passing |

The mount below has since landed in `server/main.py` and is committed (measured
2026-10-05: `git show HEAD:server/main.py` contains both lines, and the three
paths appear in `server.main.app.routes`). **Do not re-apply it** — a second
copy would register the same method+path twice, where the first silently wins.

## 2. The mount — two lines, position matters

These two lines sit ABOVE the line where `install_ownership_guard(app)` runs,
next to the existing `register_daily_boss_routes` call:

```python
from daily_boss_routes import register_boss_ladder_routes
register_boss_ladder_routes(app, lambda: players_db)
```

Why the position is load-bearing:

1. The shape-based ownership guard only looks at routes registered **above** it.
   Registered below, these routes are never examined.
2. It does not matter for path shadowing here: the paths are `/boss-ladder/...`,
   a prefix nothing else uses. That was a deliberate choice so this feature can
   never be swallowed by the older `/daily-boss/{player_id}` routes.

The routes carry `Depends(require_player)` by hand. A path that names no player
is invisible to a shape-based guard, so hand-wiring is the whole protection —
and `test_anonymous_is_refused_on_all_three` is the proof it stayed wired.

### The test suite does NOT prove the mount

`test_boss_ladder_routes.py`'s `client` fixture builds a **fresh** `FastAPI`
and calls `register_boss_ladder_routes` itself (lines 274-281). Its 13 green
tests prove the MODULE works; they say nothing about whether `server/main.py`
mounts it. To answer "are these routes mounted?", enumerate
`server.main.app.routes` — not this suite. Measured 2026-10-05: 8 declared
daily-boss paths across `daily_boss_routes.py`, `daily_boss_tables.py` and
`main.py`, 8 mounted, 0 unmounted; 0 duplicate (method,path) pairs app-wide.

## 3. The four tables, and what stops what

| Table | Stops |
|---|---|
| `boss_ladder` | a restart or a second server handing players a fresh boss; two servers both spending the same last point of health |
| `player_strikes` | a retried press dealing damage twice; a refused press being free |
| `boss_rewards` | the same payout twice, including across two server processes |
| `strike_log` | "why did this player get nothing" being unanswerable |

The health bar moves in one statement:

```sql
UPDATE boss_ladder SET hp_remaining = GREATEST(0, hp_remaining - %s)
WHERE day_index = %s AND ladder_slot = %s AND state = 'active'
RETURNING hp_remaining
```

A "read the pool, subtract in Python, write it back" shape loses a press from
another server. That is the exact bug this table exists to remove.

## 4. Anti-abuse, and where each rule lives

| Rule | Where | How it was checked |
|---|---|---|
| Sign-in on all three routes | `Depends(require_player)` | anonymous call measured 401 |
| Player is never a caller input | the session only | a body naming another player measured 403 |
| 10 presses per player per day | `STRIKES_PER_DAY`, charged first | press 11+ measured refused |
| A refused press still costs a press | the strike row is written before any decision | counter measured past the cap |
| Damage is the rung's own number | `damage_for()`, by realm | a body carrying 10,000,000 paid the table's figure |
| One press per idempotency key | `UNIQUE (player_id, idempotency_key)` | two interpreters, one key, one row |
| One payout per reward per day | `boss_rewards` PRIMARY KEY | replay paid nothing |
| Nothing to forge | no damage, reward or boss field in any request model | read from the models |

The idempotency key is the one caller-supplied field, and it grants nothing. It
collapses a retried request onto its original outcome. A caller who invents
keys gets one press per key and is stopped by the daily cap, because the cap
counts ROWS in `player_strikes`, not keys.

## 5. The 05:00 UTC reset

`RESET_HOUR_UTC = 5` is defined once, in `daily_boss_ladder.py`. The day index
is `floor((now - 5h) / 86400)`. Nothing else in the feature repeats the number
— two copies of a reset hour drift.

`GET /boss-ladder/state` returns `reset_hour_utc` and `next_reset_at`, so the
client counts down to the **server's** boundary rather than to its own phone's
midnight. A countdown to the phone's midnight is a countdown to a different
event.

Checked by asserting a relationship, not a frozen number: 04:59 and 05:00 UTC
on the same date are different days, and 03:00 and 01:00 UTC on consecutive
dates are the same day.

## 6. Measured, not asserted

**Verified by running, on 2026-10-01:**

- 13 tests pass, exit code 0 (`server/tests/test_boss_ladder_routes.py`).
- The migration applies twice on a disposable database with no error, and the
  second run is a clean skip.
- The three composite constraints bite: a duplicate `(player_id, day_index,
  strike_seq)` insert is refused; a duplicate `(day_index, ladder_slot)` is
  refused; a template row carrying a `day_index` is refused by the shape
  constraint.
- Nine ladder templates seed correctly.
- The idempotency guard holds across two Python processes on one database: the
  second insert with the same key loses the unique index.
- The negative control bites in both directions: with the press cap reverted in a
  throwaway copy, press 2 deals damage instead of being refused; on the shipping
  tree it is refused.
- One player cannot clear the shared ladder in a day. This is the anti-grief
  property, and it is asserted rather than hoped for.

**Not checked:**

- The routes are not mounted in `main.py`, so nothing has exercised them on the
  live app yet. Section 2 is the gate.
- Two server processes racing the shared health pool through HTTP. The single
  statement is proved; the cross-process race is not.
- Any human playtest of the reward numbers, the 10-press cap, or the 3-day
  rotation.
- The client panel. No Godot work was in scope.

## 7. Build estimate, in server days

One engineer who already knows this codebase. A day is a focused day. This is
the work that remains, not the work done.

| Task | Days | Note |
|---|---|---|
| Land the two-line mount in `main.py` | 0.1 | Section 2. Position matters; it is above the guard |
| Anonymous and cross-player tests on the live app | 0.25 | The in-file tests use their own app; the live app has the real guard mounted |
| Two-process race on the shared pool over HTTP | 0.5 | The single statement is proved; the race is not |
| Client panel against the three routes | 1.0 | Separate owner; the reply key set is final |
| Balance pass on rewards and the 3-press-per-rung feel | 0.5 | No human has played it |
| Close the press-sequence race (row lock or a sequence) | 0.25 | Section 8 item 2 |
| **Total** | **2.6** | |

Optional, not in the total:

| Task | Days | Priority |
|---|---|---|
| Rotation longer than three days | 0.5 | Low. The catalogue is data, so this is a seed edit |
| A public contribution board | 0.5 | **Do not.** Strike strength scales with realm, so a board ranks progress, not effort |

## 8. What a reviewer should attack first

1. **The number 019.** Another agent landed `018_beast_wave.sql` while this was
   being written. Two files on one version number means one of them is skipped
   forever, because `schema_migrations.version` is the primary key. Re-run
   `migrations_runner.discover_migrations()` before shipping.
2. **The daily press counter is per-player-persistent, not database-enforced.**
   The press cap counts rows, and the sequence number is read-then-written. Two
   servers could hand the same player two presses with the same sequence
   number; one insert would lose. A `SELECT ... FOR UPDATE` or a sequence would
   close it. The payout ledger does not have this hole.
3. **The reset hour exists in one place**, which is correct, but it is still a
   Python constant rather than a row. If the rotation length ever changes, the
   modulo and the templates must change together.