extends GamePanel
## SectWarPanel.gd — sect standing, war, and settlement screen.
##
## WHY /sect-v2/ + /sect-war/, NOT the legacy /sect/ routes
## --------------------------------------------------------
## Three overlapping sect route sets ship on the server. This panel wires the
## two that are actually reachable for a player today:
##
##   * /sect-v2/*  (13 routes, all mounted) -- standing: create, join, leave,
##     list, hierarchy, contribution, meditation, activity, beast hunt, manage.
##     This is the set the server itself treats as current.
##   * /sect-war/* (6 routes, all mounted) -- the war and settlement half:
##     current, read, declare, contribute, battle, preview.
##
## It deliberately does NOT call the legacy /sect/* war trio
## (POST /sect/war/declare, /sect/war/contribute, /sect/war/{war_id}/settle).
## Measured: those read a DIFFERENT store, so /sect-war/* answers
## {"detail": "Not in a sect"} for a player who demonstrably is in one, while
## the legacy trio works. Wiring the panel to the legacy war trio would give a
## player a screen that works today and silently breaks the moment the store
## split is fixed. Standing comes from /sect-v2/, which agrees with both.
##
## TWO SERVER BEHAVIOURS THE UI MUST HANDLE (measured, not guessed)
## ----------------------------------------------------------
## 1. Refusals come back as HTTP status codes with a `detail` string, NOT as
##    a 200 with an `outcome` field. This panel branches on HTTP + detail.
## 2. POST /sect-war/{war_id}/battle with {"preview": true} returns HTTP 500.
##    Measured live. `sect_war.py:911` passes a bare `players_db` that is not
##    in scope. So this panel NEVER asks battle for a preview; it calls
##    POST /sect-war/{war_id}/preview, which returns 200 with the full payout
##    table. Same data, no crash.

## MIGRATED ONTO GamePanel (2026-10-03)
## --------------------------------------------------------
## What actually changed, and what was measured rather than assumed:
##   * `extends PanelContainer` -> `extends GamePanel`, and `_ready()` ->
##     `_on_ready()` (Godot calls only the most-derived `_ready()`, so keeping
##     the panel's own would have silently skipped the base's setup).
##   * `_api` and `_busy` were DELETED, not renamed. The base declares both, and
##     re-declaring either is a FATAL parse error, not a shadow warning.
##   * `bind_status(_status)` after the nodes resolve, so the base's display
##     helpers reach the same label this panel always wrote to.
##   * `sect_war_action` is now written by `tag_action()`, which emits the same
##     meta plus the generic `panel_action`. `action_family` is pinned in
##     `_ensure_sections()` because the base's derived default would have been
##     "sect_war_panel_action" -- a different name the hub does not match.
##
## WHAT DID NOT CHANGE, and why that was checked rather than hoped for:
## The sibling InnerSkillPanel migration LOST ITS LAYOUT, because that panel is
## built with `Script.new()` and therefore took its node type from the script
## (a bare Control does not arrange its children). This panel is instantiated
## from SectWarPanel.tscn, whose ROOT NODE is a PanelContainer, and
## GamePanel extends Control precisely so a Control-based script can sit on
## one. Measured before and after at a 400x600 parent, identical children:
##     before: node_class=PanelContainer  scroll=400x600  body=400x157
##     after:  node_class=PanelContainer  scroll=400x600  body=400x157
## No root-VBox-and-anchor fix was needed and none was added.
##
## The panel KEEPS its own `_api.request_get` / `_api.post` calls and its own
## `_ok()` / `_detail()`. Adopting the shared `call_api()` would change
## behaviour (a fresh idempotency key per call, and a different success test),
## which is out of scope for a migration.

const SECT_V2 := "/sect-v2"
const SECT_WAR := "/sect-war"

## Set as a PROPERTY, never via a setup() call that also defers a refresh.
## Two requests in flight -> the second supersedes the first and the panel
## renders nothing. This exact failure is recorded on TribulationPanel.
@export var player_id: String = ""

# _api is NOT declared here. GamePanel owns it and resolves it in its own
# _ready(), before _on_ready() runs. Redeclaring it is a fatal parse error.

# NOT @onready. Resolved lazily inside _ensure_sections() instead.
#
# Measured failure this fixes: a caller that does `add_child(panel)` and then
# `panel.bind(api)` in the SAME frame reaches _ready() only later, so an
# @onready _body was still null when _ensure_sections() first ran. It returned
# early, every _render_*() then hit its `if _box == null: return` guard, and the
# panel rendered NOTHING while every reply parsed correctly -- a silent all-clear
# with an empty screen. Resolving the node on each call makes the order moot.
var _body: VBoxContainer = null

# The three section containers live in the .tscn. `_ensure_sections` creates
# them if a caller mounts the script on a bare node instead of the scene, so a
# headless test can mount either shape.
var _standing_box: VBoxContainer = null
var _war_box: VBoxContainer = null
var _settle_box: VBoxContainer = null
var _status: Label = null

func _ensure_sections() -> void:
	# Pin the action-family tag here, on the path both bind() and _ready() take,
	# so it is correct before ANY button is built. The base's derived default
	# for this script would be "sect_war_panel_action"; the hub matches
	# "sect_war_action" and would grey the panel out.
	action_family = "sect_war"
	# Re-resolved on every call so the panel is order-independent: whether the
	# caller binds before or after the node enters the tree, the boxes resolve.
	if _body == null or not is_instance_valid(_body):
		_body = get_node_or_null("Scroll/Body") as VBoxContainer
	if _body == null:
		return
	if _status == null:
		_status = get_node_or_null("Scroll/Body/Status") as Label
	_standing_box = get_node_or_null("Scroll/Body/Standing") as VBoxContainer
	_war_box = get_node_or_null("Scroll/Body/War") as VBoxContainer
	_settle_box = get_node_or_null("Scroll/Body/Settle") as VBoxContainer
	# A title Label is not a container; build one if the scene lacked it.
	for pair in [["Standing", "standing"], ["War", "war"], ["Settle", "settle"]]:
		var node = get_node_or_null("Scroll/Body/" + str(pair[0]))
		if node is VBoxContainer:
			continue
		if node == null:
			var made := VBoxContainer.new()
			made.name = str(pair[0])
			_body.add_child(made)
			node = made
		if str(pair[1]) == "standing":
			_standing_box = node as VBoxContainer
		elif str(pair[1]) == "war":
			_war_box = node as VBoxContainer
		else:
			_settle_box = node as VBoxContainer

var _your_sect_id: String = ""
var _your_rank: String = ""
var _war_id: String = ""
# _busy is NOT declared here either; GamePanel owns it. The panel still
# sets it directly, exactly as before.

# Merged reply state. Three replies arrive in a chain (list -> hierarchy ->
# contribution) and each used to re-render the standing block from a PARTIAL
# dict, so the last render erased the rank and roster the first one had drawn.
# Measured: the standing block showed contribution counters but silently lost
# "Your rank" and "Members: n". Each reply is merged in here instead.
var _list_d: Dictionary = {}
var _hier_d: Dictionary = {}
var _contrib_d: Dictionary = {}
var _war_d: Dictionary = {}

# The reply from POST /sect-war/{war_id}/battle. Measured 3 keys:
# battle, ok, preview -- and NO war fields at all, so it cannot be fed to
# _render_war, which expects the flattened war_view. Kept separate, or the
# panel would read fields this route never sends.
var _battle_d: Dictionary = {}

# Built in code, not in the .tscn: it holds content only after the player
# presses Simulate Battle, so an empty box at hub load costs nothing.
var _battle_host: VBoxContainer = null

## Read a reply key as a String, treating JSON null and "" the same.
##
## NEVER write `str(d.get(k, "") or "")` here. Measured in Godot 4.7: the `or`
## operator on a Variant and a String does NOT return the left operand -- it
## returns the BOOL. `str(d.get("your_sect_id","") or "")` for a real sect id
## yields the string "true", which then compares unequal to every real sect id
## and the panel silently renders nothing. This helper is the only sanctioned
## way to pull a string out of a decoded reply.
func _s(d: Dictionary, key: String, def: String = "") -> String:
	if d == null:
		return def
	var v = d.get(key, null)
	if v == null:
		return def
	return str(v)


func _i(d: Dictionary, key: String, def: int = 0) -> int:
	if d == null:
		return def
	var v = d.get(key, null)
	if v == null:
		return def
	return int(v)


func _b(d: Dictionary, key: String, def: bool = false) -> bool:
	if d == null:
		return def
	var v = d.get(key, null)
	if v == null:
		return def
	return bool(v)

## A headless test may inject a fake API before _ready() runs. Without this
## guard the panel's own _ready() refresh clobbers the test's in-flight reply.
var _injected: bool = false


func _on_ready() -> void:
	# The base has already run _resolve_api(), so _api is populated by now.
	# Do NOT re-resolve it here: _resolve_api() returns early when _api is
	# already set, and that early return is what keeps an injected fake alive.
	_ensure_sections()
	# Bind AFTER the nodes resolve. The base's own bind_status() runs before
	# _on_ready(), which finds nothing on the script-built path.
	bind_status(_status)
	if _api != null and not _injected:
		# Only self-refresh when nobody injected a reply for a test to read.
		# _injected is an explicit flag written by bind(), NOT an `_api != null`
		# identity test, so the base resolving _api first cannot forge it.
		if player_id != "":
			refresh_all()


## Point the panel at an API node and load. Used by MainHub and by tests.
func bind(api: Node) -> void:
	_ensure_sections()
	_api = api
	_injected = true
	refresh_all()


# ============================ load ============================

func refresh_all() -> void:
	_load_standing()
	_load_war()


func _load_standing() -> void:
	if _api == null:
		_set_status("Not connected to the server.")
		return
	# /sect-v2/list carries both the roster of joinable sects AND which one the
	# caller already belongs to, so it is the single first call.
	_list_call()


func _list_call() -> void:
	var res = await _api.request_get(SECT_V2 + "/list")
	if not _ok(res):
		_set_status("Sect list unavailable.")
		return
	var d: Dictionary = res.get("data", {})
	_your_sect_id = _s(d, "your_sect_id")
	_list_d = d
	_render_standing()
	if _your_sect_id == "":
		_render_no_sect(d.get("sects", []))
		_load_war()
		return
	_hierarchy_call()


func _hierarchy_call() -> void:
	var res = await _api.request_get(SECT_V2 + "/hierarchy")
	if not _ok(res):
		_set_status("Sect hierarchy unavailable.")
		return
	var d: Dictionary = res.get("data", {})
	_your_rank = _s(d, "your_rank")
	_hier_d = d
	_render_standing()
	_contribution_call()


func _contribution_call() -> void:
	var res = await _api.request_get(SECT_V2 + "/contribution")
	if _ok(res):
		_contrib_d = res.get("data", {})
		_render_standing()
	_load_war()


# ============================ render ============================

func _clear(box: VBoxContainer) -> void:
	if box == null:
		return
	for c in box.get_children():
		c.queue_free()
		box.remove_child(c)


func _label(box: VBoxContainer, text: String, size: int = 14,
		color: Color = Color(1, 1, 1)) -> void:
	if box == null:
		return
	var l := Label.new()
	l.text = text
	l.add_theme_font_size_override("font_size", size)
	l.add_theme_color_override("font_color", color)
	l.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	box.add_child(l)


func _button(box: VBoxContainer, text: String, cb: Callable) -> void:
	if box == null:
		return
	var b := Button.new()
	b.text = text
	b.custom_minimum_size = Vector2(200, 40)
	# MainHub._style_content re-styles buttons via a deferred
	# child_entered_tree handler. A button created during render and never
	# marked here comes out GREYED OUT after hub load, with no error.
	# Same "sect_war_action" meta as before, now written by the base, which
	# also emits the generic "panel_action" the hub accepts.
	tag_action(b)
	b.pressed.connect(cb)
	box.add_child(b)


func _render_standing() -> void:
	_ensure_sections()
	if _standing_box == null:
		return
	_clear(_standing_box)
	var h: Dictionary = _hier_d
	var c: Dictionary = _contrib_d

	_label(_standing_box, "Your Sect Standing", 20, Color(1, 0.9, 0.5))
	if _your_sect_id == "":
		_label(_standing_box, "You are not in a sect.", 15, Color(1, 0.7, 0.7))
		return
	_label(_standing_box, "Sect: %s" % _your_sect_id, 15)
	if _s(h, "sect_id") != "":
		_label(_standing_box, "Sect id: %s" % _s(h, "sect_id"), 13, Color(0.8, 0.8, 0.8))
	# `your_rank` is an INT from the server, never a String. Comparing it to ""
	# throws at runtime in Godot 4 ("Invalid operands 'int' and 'String'"), and
	# the throw aborted the whole render -- the block drew its title and nothing
	# else. Only a null check is safe on a Variant of unknown type.
	var rank = h.get("your_rank", null)
	if rank != null:
		_label(_standing_box, "Your rank: %s" % str(rank), 15)
	var roster = h.get("roster", [])
	if roster is Array and (roster as Array).size() > 0:
		_label(_standing_box, "Members: %d" % (roster as Array).size(), 13, Color(0.8, 0.8, 0.8))
		_label(_standing_box, "Member cap: %s" % _s(h, "member_cap", "-"), 13, Color(0.8, 0.8, 0.8))
	if not c.is_empty():
		_label(_standing_box, "Contribution today: %s / %s" % [
			str(_i(c, "day_points")), str(_i(c, "day_cap"))], 14)
		_label(_standing_box, "Contribution this week: %s / %s" % [
			str(_i(c, "week_points")), str(_i(c, "week_cap"))], 14)
	_button(_standing_box, "Leave Sect", func(): _leave())


func _render_no_sect(sects: Array) -> void:
	_ensure_sections()
	if _standing_box == null:
		return
	_clear(_standing_box)
	_label(_standing_box, "Your Sect Standing", 20, Color(1, 0.9, 0.5))
	_label(_standing_box, "You are not in a sect. Join one, or found your own.",
		15, Color(1, 0.7, 0.7))
	var ed := LineEdit.new()
	ed.placeholder_text = "New sect name"
	ed.custom_minimum_size = Vector2(200, 40)
	_standing_box.add_child(ed)
	_button(_standing_box, "Found Sect", func(): _create(ed.text))
	if sects.is_empty():
		_label(_standing_box, "No sects exist yet.", 13, Color(0.8, 0.8, 0.8))
		return
	_label(_standing_box, "Joinable sects:", 15)
	for s in sects:
		if not (s is Dictionary):
			continue
		var d: Dictionary = s
		var sid := str(d.get("id", ""))
		_label(_standing_box, "%s  (lv %s, %s/%s members)" % [
			str(d.get("name", "?")), str(d.get("level", 1)),
			str(d.get("member_count", 0)), str(d.get("member_cap", 0))], 14)
		_button(_standing_box, "Join %s" % str(d.get("name", "?")), func(): _join(sid))


func _render_war(d: Dictionary) -> void:
	_ensure_sections()
	if _war_box == null:
		return
	_clear(_war_box)
	_label(_war_box, "Sect War", 20, Color(1, 0.6, 0.4))

	# THE TWO SHAPES, measured live:
	#   no war at all   -> {"ok": true, "war": null, "server_now": ...}
	#   a war exists    -> war_view FLATTENED into the top level, i.e. the
	#                      reply has "war_id" and NO "war" key at all.
	# Keying the branch on `war` alone therefore reported "not at war" for every
	# ACTIVE war. The discriminator is the presence of war_id.
	var nested = d.get("war")
	var has_war = _s(d, "war_id") != ""
	if not has_war and (nested is Dictionary) and (nested as Dictionary).has("war_id"):
		has_war = true
	if not has_war:
		_war_id = ""
		_label(_war_box, "Your sect is not at war right now.", 15, Color(0.85, 0.85, 0.85))
		if _your_sect_id != "":
			_declare_row()
		_render_settle({})
		return

	var w: Dictionary = (nested as Dictionary) if (nested is Dictionary) else d
	# /sect-war/current and /sect-war/{war_id} FLATTEN war_view into the top
	# level -- there is no nested "war" key when a war exists. Only the
	# no-war reply carries {"war": null}. Both shapes are handled.
	if w.is_empty():
		w = d
	_war_id = _s(w, "war_id")

	var side := _s(w, "your_side")
	var mine := _s(w, "your_sect_id")
	var theirs := _s(w, "defender_sect_id")
	if side == "attacker":
		theirs = _s(w, "defender_sect_id")
	elif side == "defender":
		theirs = _s(w, "attacker_sect_id")

	_label(_war_box, "War %s" % _war_id, 16)
	_label(_war_box, "Phase: %s" % str(w.get("phase", "?")), 15)
	_label(_war_box, "Your side: %s" % (side if side != "" else "spectator"), 15)
	_label(_war_box, "You fight for: %s" % (mine if mine != "" else "-"), 13,
		Color(0.8, 0.8, 0.8))
	_label(_war_box, "Opponent: %s" % (theirs if theirs != "" else "-"), 13,
		Color(0.8, 0.8, 0.8))
	var atk := int(w.get("attacker_score", 0))
	var dfn := int(w.get("defender_score", 0))
	_label(_war_box, "Score  %d  :  %d" % [atk, dfn], 18)
	_label(_war_box, "Your stake: %d spirit stones" % int(w.get("your_paid", 0)), 15,
		Color(0.7, 0.95, 0.7))

	var battle = d.get("battle")
	if battle is Dictionary:
		var b: Dictionary = battle
		_label(_war_box, "Predicted winner: %s" % str(b.get("predicted_winner", "?")), 14,
			Color(1, 0.9, 0.5))
		_label(_war_box, "Rounds: %s   lead changes: %s%s" % [
			str(b.get("rounds", "?")), str(b.get("lead_changes", 0)),
			"   CONTESTED" if bool(b.get("contested", false)) else ""], 13,
			Color(0.9, 0.75, 0.5))

	if _s(w, "winner") != "":
		_label(_war_box, "Winner: %s" % str(w.get("winner")), 17, Color(1, 0.85, 0.4))

	var settled := str(w.get("phase", "")) == "settled"
	if not settled:
		_contribute_row()
		_preview_button()
		_battle_button()
	_refresh_button()
	_read_war_row()
	# Redraw the battle block last. _load_war clears it, but a refresh that
	# did not change the war would otherwise leave a stale simulation up.
	if not _battle_d.is_empty():
		_render_battle()


func _render_settle(pv: Dictionary) -> void:
	_ensure_sections()
	if _settle_box == null:
		return
	_clear(_settle_box)
	_label(_settle_box, "Settlement Preview", 20, Color(0.7, 0.8, 1))
	if pv.is_empty():
		_label(_settle_box, "Ask for a preview to see what settlement would pay.",
			13, Color(0.8, 0.8, 0.8))
		return
	if bool(pv.get("settled", false)):
		_label(_settle_box, "This war is already settled.", 16, Color(0.9, 0.9, 0.9))
		_label(_settle_box, "Winner: %s" % str(pv.get("winner", "?")), 16,
			Color(1, 0.85, 0.4))
		return
	_label(_settle_box, "This is a PREVIEW. It pays nothing and changes nothing.",
		13, Color(1, 0.85, 0.5))
	_label(_settle_box, "Phase: %s" % str(pv.get("phase", "?")), 14)
	_label(_settle_box, "You would rank: %s" % str(pv.get("predicted_role", "?")), 15)
	_label(_settle_box, "Total payout across all members: %d" %
		int(pv.get("total_payout", 0)), 15)
	_label(_settle_box, "Members paid: %s" % str(pv.get("payout_count", 0)), 13,
		Color(0.8, 0.8, 0.8))
	_label(_settle_box, "Stakes: %s needed for a bonus, %s daily cap, %s season cap" % [
		str(pv.get("min_stake_for_bonus", "-")), str(pv.get("daily_cap", "-")),
		str(pv.get("season_cap", "-"))], 12, Color(0.75, 0.75, 0.75))
	var payouts = pv.get("payouts", [])
	if payouts is Array:
		for p in (payouts as Array):
			if p is Dictionary:
				var pd: Dictionary = p
				_label(_settle_box, "  %s  ->  %s" % [
					str(pd.get("player_id", "?")), str(pd.get("amount", 0))], 12,
					Color(0.85, 0.85, 0.95))


func _declare_row() -> void:
	if _war_box == null or _busy:
		return
	var ed := LineEdit.new()
	ed.placeholder_text = "Defender sect id"
	ed.custom_minimum_size = Vector2(200, 40)
	_war_box.add_child(ed)
	_button(_war_box, "Declare War", func(): _declare(ed.text))


func _contribute_row() -> void:
	if _war_box == null or _busy:
		return
	var ed := LineEdit.new()
	ed.placeholder_text = "Spirit stones to stake (1-10000)"
	ed.custom_minimum_size = Vector2(200, 40)
	_war_box.add_child(ed)
	# Only a PAID stake earns a payout. The server refuses anything else with
	# 400 "Only a funds contribution is paid out", so the UI does not offer it.
	_button(_war_box, "Fund the War", func(): _contribute(ed.text))


func _preview_button() -> void:
	_button(_war_box, "Preview Settlement", func(): _preview())


## Route 6. preview:false, so this cannot reach the measured 500.
func _battle_button() -> void:
	_button(_war_box, "Simulate Battle", func(): _simulate_battle())


func _refresh_button() -> void:
	_button(_war_box, "Refresh War", func(): _load_war())


## Route 5. /sect-war/current returns only the war the server picks, so
## this is how a player reads a war id that view did not surface.
func _read_war_row() -> void:
	if _war_box == null or _busy:
		return
	var ed := LineEdit.new()
	ed.placeholder_text = "War id to read"
	ed.custom_minimum_size = Vector2(200, 40)
	_war_box.add_child(ed)
	_button(_war_box, "Read War", func(): _read_war(ed.text))


# ============================ actions ============================

func _idem(tag: String) -> PackedStringArray:
	# Minted per press, reused across retries of THAT press. The server
	# refuses a paid call with no key (400) and a retried one must not charge
	# the player twice.
	return PackedStringArray(["Idempotency-Key: sectwar-%s-%s-%d" %
		[player_id, tag, Time.get_ticks_msec()]])


func _ok(res) -> bool:
	# APIClient returns {ok, code, data, error, result}. `ok` is the transport
	# AND status verdict; the server's refusals are non-2xx with a `detail`.
	if res == null:
		return false
	var okv = res.get("ok", false)
	if okv is bool and not bool(okv):
		return false
	var d: Dictionary = res.get("data", {})
	# A refusal can still arrive 200 with ok:false in the payload.
	if d.has("ok") and not bool(d.get("ok", true)):
		return false
	return true


func _detail(res) -> String:
	if res == null:
		return "no response"
	var e = res.get("error", "")
	if e != null and str(e) != "":
		return str(e)
	var d: Dictionary = res.get("data", {})
	if d.has("detail"):
		return str(d.get("detail"))
	return "request failed"


func _finish(msg: String, ok: bool) -> void:
	_busy = false
	_set_status(msg if ok else "Refused: " + msg)
	_load_war()


func _set_status(msg: String) -> void:
	# Routed through the base so the outcome is recorded as a meta on BOTH the
	# label and the panel; the text the player reads is unchanged. bind_status()
	# in _on_ready() points _write_status at this panel's own _status label.
	_write_status(msg, "panel")


func _create(name_text: String) -> void:
	var n := name_text.strip_edges()
	if n == "":
		_set_status("Type a sect name first.")
		return
	_busy = true
	var res = await _api.post(SECT_V2 + "/create", {"name": n, "description": ""},
		_idem("create"))
	if _ok(res):
		_set_status("Sect founded.")
		_busy = false
		_list_call()
	else:
		_finish(_detail(res), false)


func _join(sect_id: String) -> void:
	_busy = true
	var res = await _api.post(SECT_V2 + "/join", {"sect_id": sect_id}, _idem("join"))
	if _ok(res):
		_set_status("Joined.")
		_busy = false
		_list_call()
	else:
		_finish(_detail(res), false)


func _leave() -> void:
	_busy = true
	var res = await _api.post(SECT_V2 + "/leave", {}, _idem("leave"))
	if _ok(res):
		_set_status("Left the sect.")
		_busy = false
		_your_sect_id = ""
		_your_rank = ""
		_list_call()
	else:
		_finish(_detail(res), false)


func _declare(defender: String) -> void:
	var d := defender.strip_edges()
	if d == "":
		_set_status("Type the defender sect id first.")
		return
	_busy = true
	var res = await _api.post(SECT_WAR + "/declare", {"defender_sect_id": d},
		_idem("declare"))
	if _ok(res):
		_set_status("War declared.")
		_busy = false
		_war_id = _s(res.get("data", {}), "war_id")
		_load_war()
	else:
		_finish(_detail(res), false)


func _contribute(value_text: String) -> void:
	var v := int(value_text.strip_edges())
	if v < 1 or v > 10000:
		_set_status("Stake must be between 1 and 10000 stones.")
		return
	if _war_id == "":
		_set_status("No war to fund.")
		return
	_busy = true
	var res = await _api.post(SECT_WAR + "/" + _war_id + "/contribute", {
		"war_id": _war_id,
		# "funds" is the ONLY type that pays out. The server refuses the
		# others with 400, so the client does not send them.
		"contribution_type": "funds",
		"value": v,
	}, _idem("contrib"))
	if _ok(res):
		var d: Dictionary = res.get("data", {})
		_set_status("Staked %d stones. Your total: %s" % [
			_i(d, "stones_spent"), _s(d, "your_paid", "0")])
		_busy = false
		_load_war()
	else:
		_finish(_detail(res), false)


func _preview() -> void:
	if _war_id == "":
		_set_status("No war to preview.")
		return
	_busy = true
	# The dedicated preview route, NOT battle with preview:true. Measured:
	# battle?preview=true returns HTTP 500 (sect_war.py:911 passes a `players_db`
	# that is not in scope). This route returns 200 with the same payout table.
	var res = await _api.post(SECT_WAR + "/" + _war_id + "/preview", {}, _idem("preview"))
	if _ok(res):
		_set_status("Preview loaded. Nothing was paid.")
		_busy = false
		_render_settle(res.get("data", {}))
	else:
		_finish(_detail(res), false)


## ROUTE 5 of 6: GET /sect-war/{war_id}.
##
## /sect-war/current returns only the ONE war the server picks (sorted by
## started_at, newest wins), so a player in two warring sects cannot read the
## other one. Measured 200 with the SAME flattened shape as /current -- 12
## keys, war_view spliced into the top level -- so it feeds _render_war as is.
##
## Refusals are 403 "Not a member of either warring sect". The server answers
## 403 for a made-up id too, on purpose, so this route is not an oracle.
func _read_war(war_id: String) -> void:
	if _api == null:
		return
	var w := war_id.strip_edges()
	if w == "":
		_set_status("Type a war id to read.")
		return
	_busy = true
	var res = await _api.request_get(SECT_WAR + "/" + w)
	if not _ok(res):
		_finish(_detail(res), false)
		return
	_battle_d = {}
	_war_d = res.get("data", {})
	_busy = false
	_set_status("Read war %s." % w)
	_render_war(_war_d)


## ROUTE 6 of 6: POST /sect-war/{war_id}/battle.
##
## NEVER send preview:true here. Measured again this turn: it answers HTTP 500
## from sect_war.py:912, `NameError: name 'players_db' is not defined` -- the
## handler passes a bare `players_db` that is not in its scope. With
## preview:false the same route answers 200 with the battle simulation, which
## is exactly what a Simulate Battle button should ask for.
##
## The reply carries no war fields, so the war view is left alone and only the
## battle block is redrawn.
func _simulate_battle() -> void:
	if _api == null:
		return
	if _war_id == "":
		_set_status("No war to simulate.")
		return
	_busy = true
	var res = await _api.post(SECT_WAR + "/" + _war_id + "/battle", {
		"war_id": _war_id,
		# Explicit false. Omitting it also defaults to false server-side, but
		# stating it keeps the 500 branch unreachable by accident.
		"preview": false,
	}, _idem("battle"))
	if not _ok(res):
		_finish(_detail(res), false)
		return
	_battle_d = res.get("data", {})
	_busy = false
	_set_status("Battle simulated. Nothing was paid.")
	_render_battle()


## Draw the battle reply. Kept apart from _render_war because the two replies
## have disjoint key sets.
func _render_battle() -> void:
	_ensure_sections()
	if _war_box == null or _battle_d.is_empty():
		return
	var b = _battle_d.get("battle")
	if not (b is Dictionary):
		return
	var d: Dictionary = b
	if _battle_host == null:
		_battle_host = VBoxContainer.new()
		_battle_host.name = "BattleResult"
		_war_box.add_child(_battle_host)
	_clear(_battle_host)
	_label(_battle_host, "Battle Simulation", 16, Color(1, 0.6, 0.4))
	_label(_battle_host, "Attacker %s   vs   Defender %s" % [
		str(d.get("attacker_score", "?")), str(d.get("defender_score", "?"))], 15)
	_label(_battle_host, "Attacker share: %s" % str(d.get("attacker_share", "?")),
		14)
	_label(_battle_host, "Predicted winner: %s" %
		str(d.get("predicted_winner", "?")), 15, Color(1, 0.9, 0.5))
	var rounds = d.get("rounds", [])
	if rounds is Array:
		var arr: Array = rounds
		_label(_battle_host, "Rounds simulated: %d" % arr.size(), 14)
		if arr.size() > 0 and arr[arr.size() - 1] is Dictionary:
			var last: Dictionary = arr[arr.size() - 1]
			_label(_battle_host, "Final round leader: %s" %
				str(last.get("leader", "none")), 14)
	_label(_battle_host, "Lead changes: %s%s" % [
		str(d.get("lead_changes", 0)),
		"   CONTESTED" if bool(d.get("contested", false)) else ""], 13,
		Color(0.9, 0.75, 0.5))
	if bool(d.get("decided_early", false)):
		_label(_battle_host, "Decided before the final round.", 13,
			Color(0.85, 0.85, 0.85))


func _load_war() -> void:
	if _api == null:
		return
	# The read the battle view polls. Exists only on /sect-war/*; the legacy
	# /sect/* set has no war read at all.
	var res = await _api.request_get(SECT_WAR + "/current")
	if not _ok(res):
		_render_war({})
		_set_status("War view unavailable.")
		return
	_war_d = res.get("data", {})
	_battle_d = {}
	if _battle_host != null:
		_clear(_battle_host)
	_render_war(_war_d)


# ============================ test seam ============================

## Expose the last rendered state so a headless test can assert on it without
## scraping pixels.
func debug_state() -> Dictionary:
	return {
		"your_sect_id": _your_sect_id,
		"your_rank": _your_rank,
		"war_id": _war_id,
		"standing_children": (_standing_box.get_child_count() if _standing_box else -1),
		"standing_text": _text_of(_standing_box),
		"war_text": _text_of(_war_box),
		"settle_text": _text_of(_settle_box),
		"battle_text": _text_of(_battle_host),
		"battle_children": (_battle_host.get_child_count() if _battle_host else -1),
	}


func _text_of(box: Node) -> String:
	if box == null:
		return ""
	var out := ""
	for c in box.get_children():
		if c is Label:
			out += (c as Label).text + "\n"
	return out