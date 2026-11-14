extends GamePanel
## VipPanel — the player's VIP tier, their progress toward the next one, the
## privileges the tier grants, and the once-a-day privilege grant claim.
##
## A SECTION, not a tab. The MainHub nav grid is sized from `buttons.size()` and
## the live tree holds 23 TabBar buttons (measured: `grep -c 'parent="TabBar"'`
## on client/scenes/game/MainHub.tscn returns 23). At 4 columns that is already 6
## rows, so a 24th button would start a 7th and steal body height on a phone.
## This panel rides inside the SHOP view, which is the screen a player is already
## on when they ask what a paid tier is worth — the ladder's benefits are
## discounts, bag slots, cosmetics and extra daily attempts, all of which the
## shop is where they act. Mount via VIP_PANEL_MOUNT.patch.md; this file owns no
## wiring into MainHub.gd.
##
## ---------------------------------------------------------------------------
## MIGRATED ONTO GamePanel (client/scenes/game/GamePanel.gd) — what moved
## ---------------------------------------------------------------------------
## What this file no longer declares, because GamePanel now owns it:
##   `_api`, `_busy`, `_generation`  — the client handle and the ONE in-flight
##                                     request guard. Redeclaring any of the
##                                     three is a FATAL parse error ("The
##                                     member ... already exists in parent
##                                     class GamePanel"), not a warning, so
##                                     they are deleted here, not renamed.
##   `_ready()` -> `_on_ready()`     — Godot calls ONLY the most-derived
##                                     `_ready()`. Left named `_ready()`, this
##                                     panel's copy shadows GamePanel's and
##                                     the base's setup never runs.
##   `get_node_or_null(..) as X`    — now `label_at()` / `button_at()` /
##                                     `progress_bar_at()` / `vbox_at()`, so a
##                                     path that stops resolving is NAMED in
##                                     `missing_bindings()` instead of
##                                     silently leaving a null behind.
##   the remove_child+queue_free loops — now `clear_container()`.
##
## What stayed, deliberately: this panel's OWN status line and its OWN
## `_set_buttons_enabled()`. GamePanel's header says `_status` is not shadowed
## and should be handed to bind_status() rather than deleted, so `_status`
## survives and is bound explicitly in _on_ready(). Likewise
## `auto_toggle_buttons = false` below: this panel re-enables buttons itself in
## _render(), and the claim row disables ClaimButton on `claimed_today`, so a
## base-class end_call() would re-enable a button the server already closed.
##
## The request path is NOT converted to call_api(). This panel exists for its
## measured handling of a refusal that wears a 200, and its wording for the
## three claim outcomes is deliberate copy. It still opens its own requests —
## but with the base's begin_call() / is_current() / end_call(), which also
## fixes the panel's own supersede path: the pre-migration code set
## `_busy = false` on a stale reply, reporting "idle" while the newer request
## was still open. That is the bug GamePanel.end_call()'s header describes.
##
## ---------------------------------------------------------------------------
## ROUTES — exactly two exist, both mounted at server/main.py:738-739 via
## `register_vip_routes(app, lambda: players_db)`. Measured by booting the app
## and driving them (the live route table holds 355 paths; exactly these two
## match "vip"):
##
##   GET  /vip/{player_id}/status        server/vip.py:702
##        -> 200 {ok, vip:{...}}           (see the 15 vip keys below)
##        -> 403 when the session's player is not {player_id}
##   POST /vip/{player_id}/claim-daily    server/vip.py:709
##        -> 200 {ok, already_claimed, empty_grant, reason, reward,
##                 tier_id, utc_day}   -- NO body, NO query params
##
## MEASURED vip keys: benefits, claimed_today, display_name, is_max_tier,
## last_claim_utc_day, next_tier_cost_spirit_stones, next_tier_id, points,
## points_to_next, progress_pct, server_utc_day, tier_id, tiers, total_claims,
## total_spirit_stones_claimed.
## MEASURED benefit keys: bag_slots, cosmetics, daily_claim,
## shop_discount_pct. (extra_daily_attempts was REMOVED server-side on
## 2026-10-05; this panel used to render it as a permanent "+0" row.)
## MEASURED tiers: 4 rows, each {cost_spirit_stones, display_name, id,
## points_required} — traveler(0), wanderer(500), practitioner(2000),
## immortal(8000).
##
## *** THE TRAP THIS PANEL EXISTS TO NOT REPEAT: A REFUSAL WEARING A 200. ***
## A fresh player is on the free `traveler` tier, whose folded daily grant is
## empty. Measured: `POST /vip/{id}/claim-daily` on a brand-new player answers
##
##     200 {"already_claimed": false, "empty_grant": true, "ok": true,
##          "reason": "tier_grants_nothing_daily",
##          "reward": {"items": {}, "spirit_stones": 0}, ...}
##
## — and pressing it a second time answers the IDENTICAL body. So a panel that
## branches on the status code, or on `ok` alone, tells a player who received
## nothing that their claim "succeeded", twice. The decision must read
## `empty_grant` and `already_claimed` from the body. That is why both flags get
## their own line of player-facing copy below.
##
## Two other measured facts worth not rediscovering:
##
## 1. THE TIER LADDER IS DATA, NOT A UI CONSTANT. The panel renders
##    `vip.tiers` — the four rows the server sent — rather than a hardcoded
##    ladder, so a fifth tier is a server edit and needs no client change.
## 2. THE CLAIM IS ONCE PER CALENDAR DAY, NOT PER PRESS. The server's key is
##    the integer UTC day index (`server_utc_day`, measured 20729 on the probe
##    run), not a timestamp. The panel therefore greys the button out from
##    `vip.claimed_today` rather than trying to compare clocks.

signal state_loaded(data: Dictionary)
signal action_completed(outcome: Dictionary)

const STATUS_PATH := "/vip/%s/status"
const CLAIM_PATH := "/vip/%s/claim-daily"

## True when a caller supplied its own client, which suppresses the
## self-refresh: a harness that injects a double and then calls refresh()
## would otherwise have its own result discarded as stale.
##
## "Was one injected?" can NO LONGER be answered by testing `_api` for null —
## GamePanel._ready() runs _resolve_api() before _on_ready(), so by the time
## this panel runs, _api is already the autoload. It is answered the way
## DailyStreakPanel answers it: by asking whether the client we hold is still
## the autoload node.
var _injected_api: bool = false

## Plain property, assigned by the mount as `panel.set("player_id", ...)`.
## NOT set through a setup(id): _ready schedules exactly one deferred refresh
## and a second one would supersede the first, leaving the panel blank.
var player_id: String = ""

var _data: Dictionary = {}
## The outcome of the last claim, held across the reload that follows it.
## Without this the reload overwrites "Collected 150 spirit stones" with
## "Server state current." and the player is told nothing at all.
var _action_message: String = ""

## Node handles. Plain `var`, NOT `@onready`: GamePanel._ready() calls
## bind_status() and then _on_ready(), and an @onready assignment would make
## the bind depend on script-instance notification order that is not worth
## relying on. bind_status() is handed the handle explicitly below.
var _title: Label = null
var _status: Label = null
var _refresh_button: Button = null
var _tier_label: Label = null
var _points_label: Label = null
var _progress: ProgressBar = null
var _next_label: Label = null
var _claim_button: Button = null
var _claim_info: Label = null
var _benefits_box: VBoxContainer = null
var _tier_box: VBoxContainer = null


## Runs under GamePanel, which has already resolved the API client by the time
## this panel runs.
func _on_ready() -> void:
	# An injected client is not the autoload node. Checked here rather than by
	# `_api == null`, because GamePanel._ready() has already filled _api in.
	_injected_api = _injected_api or (
		_api != null and _api != get_node_or_null(API_AUTOLOAD_PATH))
	# This panel drives its own button states in _set_buttons_enabled(), and
	# the claim row disables ClaimButton on `claimed_today` — a base-class
	# end_call() would re-enable a button the server already closed.
	auto_toggle_buttons = false
	# Keeps the meta this panel has always shipped (`vip_action`) so the hub's
	# _is_interactive_button() keeps treating these buttons as LIVE. Left to
	# the base's _default_action_family() it would derive "vip_panel", a name
	# the hub does not look for, and every button here would grey out.
	action_family = "vip"

	# Typed lookups: each NAMES the path it could not find rather than leaving
	# a silent null behind. All eleven paths are authored in VipPanel.tscn,
	# which the node names there declare as the contract.
	_title = label_at("VipRoot/Header")
	_status = label_at("VipRoot/Status")
	_refresh_button = button_at("VipRoot/RefreshButton")
	_tier_label = label_at("VipRoot/TierBlock/TierLabel")
	_points_label = label_at("VipRoot/TierBlock/PointsLabel")
	_progress = progress_bar_at("VipRoot/TierBlock/ProgressBar")
	_next_label = label_at("VipRoot/TierBlock/NextLabel")
	_claim_button = button_at("VipRoot/ClaimBlock/ClaimButton")
	_claim_info = label_at("VipRoot/ClaimBlock/ClaimInfo")
	_benefits_box = vbox_at("VipRoot/BenefitsBox")
	_tier_box = vbox_at("VipRoot/TierListBox")

	# Hand this panel's own status line to the base, so status_text() and the
	# base's display helpers read the same node _set_status() writes to.
	bind_status(_status)

	if _refresh_button and not _refresh_button.pressed.is_connected(refresh):
		_refresh_button.pressed.connect(refresh)
	if _claim_button and not _claim_button.pressed.is_connected(claim_daily):
		_claim_button.pressed.connect(claim_daily)
	_mark_live_buttons()
	if _status:
		_status.text = "Reading your VIP standing…"
	if _injected_api:
		# A test double is driving; it decides when to call refresh().
		return
	call_deferred("refresh")


## This panel keeps the session id as its own plain property, set by the mount.
func resolve_player_id() -> String:
	return player_id


## Injection point for a harness. Guarded rather than a bare `if _api == null`
## so a test's client is never discarded — GamePanel._resolve_api() also
## returns early on a non-null _api, so a double handed over BEFORE this node
## entered the tree survives the base's own resolution. Recording that the
## client was injected turns off the self-refresh.
func set_api(node: Node) -> void:
	if node == null:
		return
	_api = node
	_injected_api = true


## The hub's live-button rule: `_is_interactive_button()` accepts ANY meta whose
## name ends in "_action" and whose value is truthy, and `_style_content()`
## disables every button without one — with no error, no tooltip worth reading
## and exit code 0. Routed through the base's tag_action(), which sets
## `vip_action` (this screen's original tag, from action_family above) and
## `panel_action` together. Called again after each render, because the tier
## and benefit rows are new nodes each time.
func _mark_live_buttons() -> void:
	for node in find_children("*", "Button", true, false):
		tag_action(node)


## A refresh the PLAYER asked for: it also clears the last claim's message,
## because asking for fresh numbers is a request to forget the last outcome.
func refresh() -> void:
	_action_message = ""
	await _reload()


## The post-action reload. Identical to refresh() except it leaves the claim's
## own message on screen, so the outcome is still readable after the panel has
## re-read the server.
func _reload() -> void:
	# The cheap refusals come FIRST, before the guard is opened, so a panel
	# with nothing to ask about never opens a slot it would have to close.
	if _api == null:
		_set_status("VIP unavailable: no network client.")
		return
	if player_id.strip_edges() == "":
		_set_status("VIP unavailable: no player signed in.")
		return
	if not _signed_in():
		_set_status("VIP unavailable: you are not signed in.")
		return
	var ticket := begin_call()
	if ticket == BUSY_TICKET:
		# A request is already open and it, not this call, owns the flag.
		return
	var result: Dictionary = await _api.request_get(STATUS_PATH % player_id)
	if not is_current(ticket):
		# A newer request replaced this one. Do NOT close the slot: the newer
		# request owns it, and clearing it here would report "idle" while that
		# one is still in flight. (The pre-migration code set _busy = false
		# here — the bug GamePanel.end_call()'s header describes.)
		return
	end_call()
	if not result.get("ok", false):
		_set_status("Could not read your VIP standing. " + str(result.get("error", "")))
		return
	# The server key is read through `data`. APIClient's envelope is
	# {ok, result, code, headers, body, data, error} and `data` holds the whole
	# parsed body — `ok` and `error` are the only payload-bearing keys that live
	# on the envelope itself. Reading `result.get("vip")` off the envelope reads
	# a key that is never there, and the panel renders nothing with no error.
	var body: Dictionary = result.get("data", {})
	if typeof(body) != TYPE_DICTIONARY or body.is_empty():
		_set_status("The server sent a VIP standing this panel could not read.")
		return
	var vip: Variant = body.get("vip")
	if typeof(vip) != TYPE_DICTIONARY:
		_set_status("The server sent a VIP standing this panel could not read.")
		return
	_data = vip
	_render()
	if _action_message == "":
		_set_status("Server state current.")
	state_loaded.emit(_data)


## Claim today's privilege grant. The route takes NO body and NO query params:
## the player comes from the path and the once-per-day key is the server's.
func claim_daily() -> void:
	if _api == null:
		_set_status("VIP unavailable: no network client.")
		return
	if player_id.strip_edges() == "":
		_set_status("VIP unavailable: no player signed in.")
		return
	if not _signed_in():
		_set_status("VIP unavailable: you are not signed in.")
		return
	var ticket := begin_call()
	if ticket == BUSY_TICKET:
		return
	var result: Dictionary = await _api.post(CLAIM_PATH % player_id, {})
	if not is_current(ticket):
		return
	end_call()
	if not result.get("ok", false):
		_action_message = "Claim refused. " + str(result.get("error", ""))
		_set_status(_action_message)
		_reject("http")
		return
	var body: Dictionary = result.get("data", {})

	# THE 200-THAT-MEANS-NO BRANCH. Three outcomes, three different messages,
	# none of them a success. Read the flags from the body, never the status.
	if bool(body.get("already_claimed", false)):
		_action_message = "You already collected today's grant. It comes back tomorrow."
	elif bool(body.get("empty_grant", false)):
		_action_message = "Nothing to collect yet — your tier's daily grant is empty."
	else:
		_action_message = _payout_copy(body.get("reward", {}))

	_set_status(_action_message)
	# The result metas sit on BOTH the panel and the status label, so a caller
	# inspecting either node sees this claim's outcome rather than the previous
	# render's text.
	var outcome := str(body.get("reason", ""))
	set_meta("last_action", "claim_daily")
	set_meta("last_result", body)
	set_meta("last_reason", outcome)
	if _status:
		_status.set_meta("last_action", "claim_daily")
		_status.set_meta("last_result", body)
		_status.set_meta("last_reason", outcome)
	action_completed.emit(body)
	await _reload()


## Record a transport-level refusal on the metas too, so a harness can tell
## "the server said no" from "the server said yes and paid nothing".
func _reject(reason: String) -> void:
	set_meta("last_reason", reason)
	if _status:
		_status.set_meta("last_reason", reason)


func _payout_copy(reward: Variant) -> String:
	if typeof(reward) != TYPE_DICTIONARY:
		return "Collected today's grant."
	var grant: Dictionary = reward
	var stones := int(grant.get("spirit_stones", 0))
	var items: Variant = grant.get("items", {})
	var parts: Array[String] = []
	if stones > 0:
		parts.append("%s spirit stones" % _number(stones))
	if typeof(items) == TYPE_DICTIONARY:
		for item_id in (items as Dictionary):
			var qty := int((items as Dictionary)[item_id])
			if qty > 0:
				parts.append("%s x%d" % [_pretty(str(item_id)), qty])
	if parts.is_empty():
		return "Collected today's grant."
	return "Collected %s." % ", ".join(parts)


func _signed_in() -> bool:
	if _api == null:
		return false
	if _api.has_method("is_authenticated"):
		return bool(_api.call("is_authenticated"))
	# A double with no is_authenticated() is trusted; a refusal then comes back
	# as 403 through the normal path.
	return true


func _set_status(message: String) -> void:
	if _status:
		_status.text = message


func _render() -> void:
	var tier_id := str(_data.get("tier_id", ""))
	var display_name := str(_data.get("display_name", tier_id))
	var points := int(_data.get("points", 0))
	var is_max := bool(_data.get("is_max_tier", false))
	var claimed_today := bool(_data.get("claimed_today", false))
	var benefits: Dictionary = _data.get("benefits", {}) if typeof(_data.get("benefits", {})) == TYPE_DICTIONARY else {}

	if _title:
		_title.text = "VIP — %s" % display_name
	if _tier_label:
		_tier_label.text = "Tier: %s (%s)" % [display_name, tier_id]
	if _points_label:
		_points_label.text = "Lifetime VIP points: %s" % _number(points)

	# progress_pct arrives as a JSON number. A whole number with no decimal
	# point comes back from Godot's JSON parser as a FLOAT, so a
	# TYPE_INT-shaped check would misread it. float() accepts both.
	var pct := clampf(float(_data.get("progress_pct", 0.0)), 0.0, 100.0)
	if _progress:
		_progress.min_value = 0.0
		_progress.max_value = 100.0
		_progress.value = pct
	if _next_label:
		if is_max:
			_next_label.text = "Highest tier reached."
		else:
			_next_label.text = "%s more points to %s (%s stones)." % [
				_number(int(_data.get("points_to_next", 0))),
				str(_data.get("next_tier_id", "")),
				_number(int(_data.get("next_tier_cost_spirit_stones", 0))),
			]

	_render_claim(claimed_today, benefits)
	_render_benefits(benefits)
	_render_tiers()
	_mark_live_buttons()
	_set_buttons_enabled(not _busy)


## The claim row. The button is disabled — not hidden, not merely greyed by a
## later sweep — when the server has already said today is done, because
## pressing it again buys the player a second identical "nothing to collect"
## line and teaches them the button lies.
func _render_claim(claimed_today: bool, benefits: Dictionary) -> void:
	var grant: Dictionary = {}
	if typeof(benefits.get("daily_claim", {})) == TYPE_DICTIONARY:
		grant = benefits.get("daily_claim", {})
	var stones := int(grant.get("spirit_stones", 0))
	var has_items := false
	if typeof(grant.get("items", {})) == TYPE_DICTIONARY:
		for item_id in (grant.get("items", {}) as Dictionary):
			if int((grant.get("items", {}) as Dictionary)[item_id]) > 0:
				has_items = true
				break
	var pays_nothing := stones <= 0 and not has_items

	if _claim_button:
		_claim_button.disabled = _busy or claimed_today
		_claim_button.text = "Collect Daily Grant"
	if _claim_info:
		if claimed_today:
			_claim_info.text = "Collected today. Resets at the next UTC day."
		elif pays_nothing:
			_claim_info.text = "Your tier grants no daily gift yet — reach Wanderer for 150 stones."
		else:
			_claim_info.text = "Today's grant: %s." % _payout_copy(grant)


func _render_benefits(benefits: Dictionary) -> void:
	if _benefits_box == null:
		return
	clear_container(_benefits_box)

	_benefits_box.add_child(_headline("What this tier grants"))

	var grant: Dictionary = {}
	if typeof(benefits.get("daily_claim", {})) == TYPE_DICTIONARY:
		grant = benefits.get("daily_claim", {})
	_benefits_box.add_child(_row("Daily gift", _grant_summary(grant)))
	_benefits_box.add_child(_row("Bag slots", "+%d" % int(benefits.get("bag_slots", 0))))
	_benefits_box.add_child(_row("Shop discount", "%d%%" % int(benefits.get("shop_discount_pct", 0))))

	var cosmetics: Variant = benefits.get("cosmetics", [])
	var names: Array[String] = []
	if typeof(cosmetics) == TYPE_ARRAY:
		for cosmetic_id in (cosmetics as Array):
			names.append(_pretty(str(cosmetic_id)))
	_benefits_box.add_child(_row(
		"Cosmetics",
		"" if names.is_empty() else ", ".join(names),
	))


## The four ladder rows come from the SERVER (`vip.tiers`), never from a
## hardcoded table, so a new tier is a server edit and needs no client change.
func _render_tiers() -> void:
	if _tier_box == null:
		return
	clear_container(_tier_box)

	var tiers: Variant = _data.get("tiers", [])
	if typeof(tiers) != TYPE_ARRAY or (tiers as Array).is_empty():
		_tier_box.add_child(_row("The server reported no tier ladder.", ""))
		return
	var current_id := str(_data.get("tier_id", ""))
	for tier in (tiers as Array):
		if typeof(tier) != TYPE_DICTIONARY:
			continue
		var row: Dictionary = tier
		var name_label := str(row.get("display_name", row.get("id", "")))
		var detail := "%s points" % _number(int(row.get("points_required", 0)))
		var cost := int(row.get("cost_spirit_stones", 0))
		if cost > 0:
			detail += "  ·  unlocks at %s stones" % _number(cost)
		if str(row.get("id", "")) == current_id:
			name_label = "▸ %s  (your tier)" % name_label
		_tier_box.add_child(_row(name_label, detail))


func _headline(text: String) -> Control:
	var label := Label.new()
	label.text = text
	label.add_theme_font_size_override("font_size", 16)
	label.add_theme_color_override("font_color", Color(1.0, 0.82, 0.45))
	return label


func _row(name_text: String, detail_text: String) -> Control:
	var row := HBoxContainer.new()
	row.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	var name_label := Label.new()
	name_label.text = name_text
	name_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(name_label)
	var detail_label := Label.new()
	detail_label.text = detail_text
	detail_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	row.add_child(detail_label)
	return row


func _grant_summary(grant: Dictionary) -> String:
	var stones := int(grant.get("spirit_stones", 0))
	var parts: Array[String] = []
	if stones > 0:
		parts.append("%s stones" % _number(stones))
	if typeof(grant.get("items", {})) == TYPE_DICTIONARY:
		for item_id in (grant.get("items", {}) as Dictionary):
			var qty := int((grant.get("items", {}) as Dictionary)[item_id])
			if qty > 0:
				parts.append("%s x%d" % [_pretty(str(item_id)), qty])
	return "" if parts.is_empty() else ", ".join(parts)


## Grouped thousands. `String.num` on a value that arrived as a float would
## print "1500.0" for a whole number, so this coerces to int first.
func _number(value: int) -> String:
	return String.num_int64(int(value))


## "spirit_herb" -> "Spirit Herb". Cosmetic and item ids arrive as snake_case
## from the server and this tree has no client-side catalogue, so the id is
## prettified rather than resolved.
func _pretty(identifier: String) -> String:
	if identifier.strip_edges() == "":
		return ""
	var parts := identifier.split("_", false)
	var words: Array[String] = []
	for word in parts:
		var text := str(word)
		words.append(text.substr(0, 1).to_upper() + text.substr(1))
	return " ".join(words)


func _set_buttons_enabled(enabled: bool) -> void:
	if _refresh_button:
		_refresh_button.disabled = not enabled
	if _claim_button:
		# Only the busy state may re-enable a button the claim row chose to
		# disable: an already-claimed grant must stay unpressable after the
		# reload that follows the claim.
		if not enabled:
			_claim_button.disabled = true
		elif not bool(_data.get("claimed_today", false)):
			_claim_button.disabled = false