# Trading and Market System — Design

Date: 2026-09-30. Repo: `G:/Projects/wayfarers-cycle`. Base commit `4f4bed1`.
Status: **design only, nothing built, nothing committed.** No server, no migration,
no client file is changed by this document.

## 1. Current state, measured on the tree today

| Question | Answer | How it was checked |
|---|---|---|
| Is there player-to-player trading? | No | `grep -rln -i 'auction\|market board\|trade request' docs design *.md` → 0 files; no `/trade`, `/market`, `/auction` route in `server/main.py` |
| Is there an auction house? | No | no route, no `server/auction*.py`, no migration |
| Is there a market board? | No | no route, no `server/market*.py` |
| Are trade logs written anywhere? | No | no `trade_log` table in `server/migrations/*.sql` (only 001–009 exist) |
| Is there an anti-RMT mechanism? | No | `shared/items/items.json` has no `tradable` key on any of its 22 items |

## 2. What already exists to build on

Measured, not assumed. Every one of these is a real file/function in the tree.

- **Item catalogue and prices.** `shared/items/items.json`, 22 items, each with
  `type`, `rarity`, `stack_size` and a `price` block, e.g.
  `cultivation_pill_med` = `{"currency": "spirit_stone", "amount": 500}`,
  `breakthrough_pill` = `{"currency": "fateum", "amount": 500}`.
  `server/inventory.py:181` `load_item_definitions()` loads it into
  `ITEM_DEFINITIONS`.
- **Item transfer.** `InventoryStore.add_item` / `remove_item`
  (`server/inventory.py:249` / `:317`). `add_item` is **partial-fill safe** and
  says so: every success reports `requested`, `granted`, `discarded`
  (`_grant_receipt`, `server/inventory.py:156`). A market that ignores
  `discarded` and still reports "sold" is exactly the paid-no-op defect this
  receipt exists to prevent.
- **Bag capacity.** `BASE_INVENTORY_CAPACITY = 50` (`server/inventory.py:34`),
  raised by `expansion_scroll`. A buyer with a full bag must be refused or
  partially served **with the shortfall reported**, never silently.
- **Idempotency.** `require_idempotency_key(request, route=...)`
  (`server/idem_gate.py:47`) refuses a missing/blank/oversized key (128 chars).
  It **namespaces by route**, so a key minted for `/shop/buy` cannot dedupe a
  market buy. Every new money route must call it.
- **Money movement.** `wallet_dispatch.spend` / `earn`
  (`server/wallet_dispatch.py:46` / `:56`), both idempotency-keyed, both routed
  to the PostgreSQL store when one exists.
- **Currencies.** `spirit_stone` (soft, earnable), `fateum`, `destium`,
  `souldust` (`server/multi_currency.py:34`).
- **Sects.** `SectStore` (`server/sect.py:399`) with `create_sect`,
  `join_sect`, `get_player_sect`. **It is in-memory**: `grep -cE 'INSERT|UPDATE |
  SELECT |CREATE TABLE' server/sect.py` → **0**. Sects do not survive a restart.
  Same for `server/friends.py` → **0**.
- **Blocks and friends.** `FriendsStore.block_user` (`server/friends.py:252`),
  `are_friends`, `is_online`, `last_seen`.
- **Presence.** `is_online` / `last_seen` (`server/friends.py:313`/`:333`).
- **Auth.** `Depends(require_player)` (`server/auth.py:889`) returns a
  `PlayerIdentity` that is the authority; `enforce_ownership` gives 403 on a
  mismatch.
- **Prestige.** `SURVIVES_FIELDS` (`server/prestige_reset.py:147`) keeps
  `inventory`, `wallet`, `spirit_stones`, `sect_id`, `sect_rank`. A prestige
  therefore does **not** confiscate traded goods or traded currency.
- **Migrations.** Next free number is `010_*.sql` (`server/migrations/` holds
  001–009). Migration 008 is the reference style: server-derived key, row
  inserted *before* the credit in the same transaction, `ON CONFLICT ... DO
  NOTHING RETURNING` decides who pays.
- **Client tab budget is spent.** `client/scenes/game/MainHub.gd` maps 25 views
  and 25 tab buttons; the file's own comments state a 25th button pushes the
  phone nav grid onto a 7th row, and that a previous feature was mounted
  *inside* an existing tab for that reason.

## 3. Trade methods — three, deliberately distinct

### 3.1 Direct trade request (sender proposes, receiver accepts)

- `POST /trade/request` — sender offers `[{item_id, quantity}]` plus optional
  `offer_spirit_stones`. Receiver must be a **friend** or in the same sect.
- `GET /trade/requests` — pending inbound and outbound.
- `POST /trade/accept`, `POST /trade/decline`, `POST /trade/cancel`.
- Lifetime **72 h**, then the offer expires and the escrow releases. This is the
  same shape as the friend request store (`FriendsStore.create_request`), which
  is a proven pattern in this codebase.
- Escrow: at **request** time the items are removed from the sender's bag into a
  `trade_holds` row. Not at accept time. A hold at accept time loses the race.
- **Both** sides re-verify at accept: sender still not blocked, receiver has bag
  room, sender's held quantity matches the hold row, neither side is in prestige
  transition. Failure at accept releases escrow and returns the offer to
  `declined`, never a partial transfer.

### 3.2 Mail trade (parcel — asynchronous, no acceptance needed)

- `POST /trade/mail` — sender packs items, names a recipient, picks a delivery
  fee from the table in §5. Items move to escrow immediately. Recipient claims
  with `POST /trade/mail/claim`.
- Unclaimed parcels return to the sender after **7 days** (fateum is never
  involved — mail only carries items and spirit stones).
- Mail is the method the anti-RMT rules in §7 police hardest, because it is the
  only one with no counterparty consent.

### 3.3 Face-to-face trade (both players online, same room)

- `POST /trade/room/open` → `trade_room_id`, valid **10 minutes**, both players
  must be `is_online` per `FriendsStore.is_online`.
- `POST /trade/room/offer`, `POST /trade/room/confirm` (each side), then
  `POST /trade/room/execute` when both confirmations are present and both
  offered sets are byte-identical to what was confirmed.
- A changed offer invalidates both confirmations. This is the classic trade-swap
  defect: confirm, then mutate, then execute.
- If either player is blocked, disconnects, or the room expires, the room
  closes and nothing moves.

### 3.4 Auction house (asynchronous, server-priced)

- `POST /auction/list` — seller lists `{item_id, quantity, start_price,
  buyout_price?, duration_hours}` against an **escrow** row. Items are held at
  list time.
- `POST /auction/bid` — bid must be `>= max(start_price, current_high +
  min_increment)`. Only `spirit_stone`. Refunds the previous high bidder's hold
  on the same transaction that places the new one.
- `POST /auction/buyout` — instant purchase at the list price, or at the end of
  the bid window against the standing high bid.
- `POST /auction/cancel` — only before the first bid.
- Durations: 6 h / 12 h / 24 h / 48 h. Server picks the end time from
  `time.time()`; the client never names it.
- Winner pays, seller receives less the sale tax (§5). Escrow pays out inside
  one transaction.

### 3.5 Market board (sect-only posting board)

- **Sect-scoped.** A board row carries a `sect_id` and is only visible to
  members of that sect. Because `SectStore` is in-memory and dies on restart,
  the board must read the player's sect from the **authoritative** source, not
  from `SectStore.player_sect`. Otherwise a restart silently re-scopes or drops
  listings.
- `POST /market/list`, `GET /market/listings?sect_id=`, `POST /market/buy`,
  `POST /market/remove`.
- A market listing is a **fixed price**, single unit or a stack, buyer takes the
  whole thing or nothing. No partial fills.
- Listing lifetime **72 h**, then it auto-expires and escrow returns.

### 3.6 Cross-server market

The cross-server infrastructure design is still in progress and **no shard or
region concept exists in the tree today** (`grep -rln 'shard\|server_id\|
cross-server' docs design server --include='*.md'` → nothing). So this design
does not assume one.

Shape that works regardless of which cross-server design lands:

- **Escrow is home-server.** A listing's goods live in the seller's own
  PostgreSQL rows. A cross-server market is therefore an **index**, not a
  transfer: each server publishes a read-only projection of its own live
  listings.
- **Settlement is a home-server route call.** "Buy this" is executed on the
  seller's server by an internal server-to-server call using the same internal
  token path `require_player_or_internal_mint` (`server/auth.py:933`) already
  protects. The buyer's server never writes to the seller's inventory.
- **Postgres `LISTEN/NOTIFY`** on `market_listings` is the notification
  mechanism, because it needs no new infrastructure and dies correctly when a
  server does.
- If the cross-server design later lands on a shared database instead, only the
  projection layer changes. The escrow and settlement rules above do not.

## 4. Data model

All in PostgreSQL. No in-memory store — `sect.py` and `friends.py` prove that
an in-memory feature store is the defect, not a shortcut.

**`010_trading.sql`**

```
trade_holds          (hold_id PK, owner_id, kind, item_id, quantity,
                      origin_ref, created_at)          -- escrow for 3.1/3.2/3.5
trade_requests       (request_id PK, sender_id, recipient_id, status,
                      offered_json, offer_stones, expires_at, created_at)
trade_rooms          (room_id PK, host_id, guest_id, state,
                      offer_a_json, offer_b_json, confirm_a, confirm_b, expires_at)
trade_parcels        (parcel_id PK, sender_id, recipient_id, status,
                      content_json, expires_at, delivery_fee)
auction_listings     (listing_id PK, seller_id, item_id, quantity,
                      start_price, buyout_price, high_bidder, high_bid,
                      ends_at, status)
auction_bids         (listing_id, bidder_id, amount, placed_at,
                      PRIMARY KEY (listing_id, bidder_id, placed_at))
market_listings      (listing_id PK, seller_id, sect_id, item_id, quantity,
                      unit_price, ends_at, status)
trade_log            (log_id PK, ts, method, actor_id, counterparty_id,
                      item_id, quantity, gross, fees, tax, net_to_seller,
                      status, request_key)
```

`trade_log` is append-only. It is written in the **same transaction** as the
credit, so a log that exists proves the credit landed and a credit with no log
means a bug.

## 5. Fee and tax tables

All prices in the item catalogue are `spirit_stone` or `fateum`
(`shared/items/items.json`). The table below uses **spirit stones** and states
its rounding.

### 5.1 Listing fees (charged to the seller at listing time)

| Method | Fee | Basis | Minimum |
|---|---|---|---|
| Auction, 6 h | 5% of start price | list price | 10 |
| Auction, 12 h | 6% | list price | 10 |
| Auction, 24 h | 8% | list price | 10 |
| Auction, 48 h | 10% | list price | 10 |
| Market board, 72 h | 5% | unit price | 10 |
| Market board, stack | 5% | unit price × quantity | 10 |

Round **up** to whole spirit stones. A fee never exceeds 25% of the sale price
(hard clamp), so a mispriced listing can never eat the whole payout.

### 5.2 Sale tax (deducted from the seller's proceeds)

| Rarity | Tax | Note |
|---|---|---|
| common | 5% | materials: `beast_bone`, `spirit_herb`, `blood_vial`, `taming_bait` |
| uncommon | 8% | `insight_scroll`, `body_essence`, `cultivation_pill_low`/`med` |
| rare | 12% | `spirit_ink`, `cultivation_pill_high`, `breakthrough_pill` |

Tax is computed on the **sale price actually achieved**, rounded down.

### 5.3 Minimum increments (auction bids)

| Current high bid | Minimum next bid |
|---|---|
| below 500 | +50 |
| 500 – 4,999 | +5% |
| 5,000 – 24,999 | +2% |
| 25,000 and above | +1% |

These are derived from the measured price band in the catalogue: the cheapest
priced item is 50 spirit stones (`beast_food_basic`) and the most expensive
spirit-stone item is 1,000 (`talisman_escape`). Mission payouts run 30 – 2,500
(`server/missions.py:64-105`), so a 2,500-stone bid is roughly one large
mission — that is the size of trade this system is being built for.

### 5.4 Direct trade and mail

| Path | Fee | Tax |
|---|---|---|
| Direct trade (§3.1), spirit stones only | 0 | 0 |
| Direct trade, items only | 0 | 0 |
| Mail parcel (§3.2) | 25 flat | 0 |
| Mail parcel returning unclaimed after 7 days | 0 (refund in full) | 0 |

Direct trade is free on purpose: it is the loop players already want, and a fee
there suppresses the exact session-to-session trade that retention depends on.
Mail is charged because it is the abuse vector.

**Rounding rule, everywhere:** all fees and taxes are `int`, computed as
`(amount * rate) // 10000` where rate is in basis points, then clamped by the
table. Never a float. A float fee that rounds to 0.0 is a free trade.

## 6. Trade restrictions

1. **Account age.** No listing, parcel, or trade request within **24 h** of
   account creation. Server reads the player's own creation timestamp.
2. **Per-day value cap.** A player's total *outgoing* traded value (list price
   basis) is capped at **20,000 spirit stones per UTC day** across all methods.
   Exceeding it is a 429 naming the cap and the remaining amount. This is the
   single most important anti-RMT number in the document: it bounds how fast a
   purchased account can be drained.
3. **Per-day count cap.** 40 outgoing transfers per day. Cheaper to check than
   the value cap and catches many-to-one item splitting.
4. **Cooldown.** 90 s between two accepted trades by the same account. Both
   legs check it.
5. **Blocks.** A block by either party cancels pending requests, expires rooms,
   removes listings between them, and returns unclaimed parcels to sender. Same
   behaviour `FriendsStore.block_user` already has for friends and requests.
6. **Prestige transition.** No trading while the player is mid-prestige.
7. **Bound items.** A new `tradable: bool` key in `shared/items/items.json`,
   defaulting to **false**. Without an explicit `true`, an item is not
   tradeable — a missing key must not read as "allowed".
8. **Currency restriction.** Only `spirit_stone` may move between players.
   `fateum`, `destium` and `souldust` are never tradable. `spirit_stone` is the
   documented free-to-play currency (`server/multi_currency.py:28`), so it is
   the one currency whose movement between players is defensible.
9. **No self-trading and no alt-chain trading.** `actor != counterparty`, and
   no more than **3 counterparties** per day may be accounts that registered
   within 30 days of each other (a cheap new-account-cluster heuristic).
10. **Bag room.** A buyer with no free slot is refused with the same
    `inventory_full` detail `/shop/buy` already returns
    (`server/main.py`), or partially served **with `discarded` reported and the
    shortfall refunded**.

## 7. Anti-RMT measures

- **Price floors.** The server refuses a listing below **40% of the catalogue
  price** for that item. Below that, the listing is either a mistake or a
  transfer, and a transfer has no business going through a market. Derived from
  the catalogue, not invented: the priced items in `shared/items/items.json`
  already carry their reference price.
- **Price ceilings.** 300% of catalogue price, same reasoning in the other
  direction, so a listing cannot launder currency into "an item".
- **Velocity scoring.** `trade_log` is the evidence. Rolling 24 h counters per
  account: transfer count, distinct counterparties, median price vs catalogue
  ratio, and repeat-counterparty rate. Over threshold, listings go to
  `held` status and a moderator sees them in `GET /trade/flagged`.
- **New-account clustering.** New accounts cannot trade with each other past
  3 counterparties in 24 h (restriction 9). This is the cheapest structural
  defence against a freshly bought farm.
- **No currency laundering.** A listing must be for a `tradable` item with
  quantity ≥ 1. There is no "price" field a player can use to move stones
  sideways, because stones only move on a *completed* transfer.
- **Every payout is once-only.** Each credit runs through a
  `trade_settlements` ledger row inserted **before** the credit in the same
  transaction with `ON CONFLICT DO NOTHING RETURNING`, exactly as
  `server/migrations/008_dungeon_claim_ledger.sql` does it. The row count
  alone decides who pays.
- **The item definition is the allowlist.** A future item that forgets
  `tradable: true` is simply not tradeable. Fail-closed by construction.
- **No anonymous surfaces.** Every one of the ~20 new routes carries
  `Depends(require_player)` written by hand, because
  `install_ownership_guard` matches on a bare `player_id`/`pid` parameter shape
  (`server/ownership_guard.py:163`) and would not see a route taking only a
  `listing_id`.

## 8. Trade logs

`trade_log` one row per **attempt**, written for successes and failures alike,
with `status` = `held | completed | failed | expired | cancelled`.

Every row carries: method, both player ids, item, quantity, gross, fees, tax,
net to seller, and the **server-derived** request key. A client never names a
log key. Retention: **180 days**, then aggregated into daily counts and dropped.

Two read surfaces:

- `GET /trade/history` — the player's own rows (this is also the player's
  receipt screen).
- `GET /trade/flagged` — moderator only, returns accounts over the §7 velocity
  thresholds. Not player-facing; must fail closed with 403 for a non-moderator.

## 9. Server layout and route ordering

New modules: `server/trading.py` (holds, requests, rooms, parcels),
`server/auction.py`, `server/market.py`, `server/trade_log.py`.
Registration follows the `register_*(app, ...)` shape already used by
`sect.py:637` and `friends.py:457`, with Pydantic request models at **module
scope** (inside a register function they break OpenAPI for the whole app).

**Route ordering is load-bearing.** FastAPI serves the first match, so any
`/auction/{listing_id}` must be registered after every literal
`/auction/list`, `/auction/bid`, `/auction/buyout`, `/auction/cancel`.

## 10. Client plan

**No new tab.** `client/scenes/game/MainHub.gd` already has 25 tab buttons and
its own comments record that a 26th pushes the phone nav grid onto a 7th row.
The Market panel mounts **inside the existing Shop tab** (auction + market
board) and **inside the existing Friends tab** (direct trade + parcels). The
trade request inbox mounts inside Inventory, next to the item list it consumes.

## 11. Build estimate

| Phase | Work | Days |
|---|---|---|
| 1 | `010_trading.sql` migration + `trade_holds`/`trade_log` stores + escrow with `discarded` reporting | 2.0 |
| 2 | Direct trade requests + rooms (3.1, 3.3), including the confirm-then-mutate guard | 2.5 |
| 3 | Mail parcels (3.2), expiry, return-to-sender | 1.0 |
| 4 | Auction house (3.4): bids, refund-the-previous-bidder, buyout, settlement ledger | 3.0 |
| 5 | Market board (3.5) with authoritative sect lookup, not `SectStore` | 1.5 |
| 6 | Anti-RMT: price floors/ceilings, value and count caps, cooldown, cluster rule, velocity scoring, flagged view | 2.0 |
| 7 | Cross-server index + `LISTEN/NOTIFY` projection + home-server settlement call (3.6) | 2.5 |
| 8 | Client: Market panel in Shop, trade inbox in Inventory, friends trade in Friends | 3.0 |
| 9 | Negative-control tests per §12 | 2.0 |

**Total: 19.5 days, rounded up to 20 for planning.** Single-server only (drop phase 7) is **17.0 days**.
Phases 1–2 are the floor worth shipping: direct trade is the loop players ask
for and it needs no new economy numbers. Phases 4–6 without phase 1 are not
worth shipping at all, because an escrow that is not once-only is a mint.

## 12. Test plan — every one has a negative control

A green test that cannot fail is worthless; each row states how it is proven to
bite (revert the guard, watch it go red).

| Claim | Test | Negative control |
|---|---|---|
| A transfer happens once | N concurrent accepts on one request → 1 credit | remove the settlement ledger insert → N credits |
| Bag overflow never loses units | buy 999 into a 50-slot bag → `discarded` reported, shortfall refunded | drop `discarded` from the receipt → refund is short |
| Escrow releases on failure | block mid-accept → items return, no credit | delete the release path → items vanish |
| Confirm-then-mutate is refused | confirm both, mutate A, execute → 409 | drop the equality check → items swap silently |
| Bids are once-only | 20 threads bid on one listing → one winner | remove `ON CONFLICT` → 20 winners |
| Previous bidder is refunded | outbid then finish → both refunds present | skip the refund branch → high bidder's stones vanish |
| Fees match the table | 10 listings at known prices → exact fee integers | pass a float rate → 0.0 fee |
| A missing `tradable` key refuses | sell an item with no key → 400 | default the lookup to `True` → it sells |
| Value cap holds | 2×15,000 outgoing → second 429 | remove the cap → both succeed |
| Prestige cannot race a trade | reset mid-request → escrow intact | drop the transition check → goods lost |
| No new-account cluster | 4 fresh accounts trade pairwise → refused past 3 | raise the counterparty cap → all pass |
| Server-derived end times | client posts `ends_at` in the past → ignored | honour the body field → instant expiry |

## 13. Open risks

1. **Cross-server design may land differently.** Phase 7 assumes an index +
   home-server settlement. If the cross-server design chooses a shared
   database, the projection layer is replaced but §3.6's escrow and settlement
   rules hold unchanged.
2. **`SectStore` is in-memory.** It must be migrated to PostgreSQL, or the
   market board reads sect membership from the wrong place. This is a
   pre-existing defect that this feature would amplify, not create.
3. **Price floors may block legitimate dumping.** A player who wants to sell
   300 spirit stones' worth of `beast_bone` (catalogued at 0) cannot. The floor
   is only applied to priced items; unpriced materials are priced by the server
   from a configured table.
4. **The 20,000/day cap is a guess** calibrated from the mission payout band
   (30–2,500). It should be re-measured against real transfer distributions
   after a month of live data.