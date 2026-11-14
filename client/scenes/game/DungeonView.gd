extends Control
## DungeonView.gd — the dungeon screen. The first client code in this repo that
## calls any /dungeons route.
##
## THIS PANEL DELIBERATELY DOES NOT USE THE SHARED BASE -- it `extends Control`
## and issues its 14 requests directly on _api, NOT through GamePanel.call_api().
## That is a decision, not unfinished migration debt: `_judge_claim` below reads
## the `rewards` ARRAY to tell a real payout from a refused replay, because the
## once-only guard answers a replay with HTTP 200 {"ok": true, "rewards": []}.
## Swapping in call_api() would replace that per-route branch with one generic
## answer and re-open the trap the rest of this header exists to document.
##
## Do NOT "fix" this by editing line 1 to `extends GamePanel`. MEASURED on the
## real tree 2026-10-05 with tools/gd_parse_gate.sh against a throwaway copy:
## that one-line change alone is a FATAL parse error, not a shadow warning --
## this file declares `_busy` (:144) and `_api` (:123), and GamePanel provides
## both (:116, :125), so Godot refuses to load the script at all:
##     Parse Error: The member "_api" already exists in parent class GamePanel.
##     Parse Error: The member "_busy" already exists in parent class GamePanel.
## A real migration is a multi-site change, not a header edit: drop those two
## declarations, rename `_ready()` (:176) to `_on_ready()`, set
## `auto_toggle_buttons = false` (the base defaults it TRUE, and this file
## drives its own button states through its own _busy), and re-point 14 call
## sites. Confirm zero SELF add_theme_constant_override first -- it is zero here
## (all 8 calls are on child nodes), which is why this panel is NOT one of the
## six files GamePanel.gd:66-68 lists as blocked on that call.
##
## WHY THIS FILE BRANCHES ON THE OUTCOME FIELD, NOT THE STATUS CODE
## ---------------------------------------------------------------
## Measured live on 2026-10-02 by driving the real app in-process (TestClient,
## memory store, real sessions). The trap is not hypothetical:
##
##   POST /dungeons/instance/claim-loot, called a second time by the same
##   player on the same instance -> HTTP 200, body {"ok": true, "rewards": []}
##
## The once-only guard at server/dungeon_routes.py:853 refuses the replay, but
## it refuses it with a 200 and a TRUE `ok`. A client that checks the status
## code -- or even checks `ok` -- tells the player they were just paid, and
## they were paid nothing. The only field separating a real payout from a
## refused replay is the `rewards` ARRAY: non-empty means paid, empty means the
## server kept its money. `_judge_claim` reads exactly that.
##
## Two more measured facts that shape the code below:
##
##   * GET /dungeons/instance/{id} has NO `ok` field at all. Its body is the
##     12-key dict built at server/dungeons.py:805-820 (measured: instance_id,
##     template_id, name, state, current_floor, total_floors, time_limit,
##     started_at, completed_at, loot_dropped, rewards_claimed,
##     current_floor_enemies). A blanket `if body.ok` would read a perfectly
##     good run as a failure. The outcome key for that route is `state`.
##
##   * GET /dungeons/templates answers 200 with `[]` when the realm/level filter
##     excludes everything (measured: realm=mortal&level=1 -> LIST len=0). An
##     empty catalogue is a FILTER RESULT, not an error, and must not be drawn
##     as a failure. The outcome key for that route is the array itself.
##
## THE ROUTE LIST, MEASURED NOT COUNTED
## -------------------------------------
## This panel was written against a belief of "14 dungeon routes". Measured on
## the booted app on 2026-10-03 by walking `app.routes` after a real import
## (scratch/dv_measure.py), the server has exactly 14 /dungeons routes -- so the
## count was right and the COVERAGE was not: the first version of this file
## called 9 of them and left 5 with no caller anywhere in the client. Those 5
## are wired below and the list now stands at 14 of 14.
##
## The line numbers here were re-read off the file on 2026-10-03. The previous
## list cited 141/248/426/467/516/562/617/695/763, every one of them wrong --
## a file:line citation is a claim about a shared tree that rots silently, so
## these were re-derived from the `@router` decorators rather than trusted.
##
##   ROUTE                                  DEFINED AT              CALLED BY
##   GET  /dungeons/templates               dungeon_routes.py:142    refresh()
##   GET  /dungeons/templates/{id}          dungeon_routes.py:219    read_template_detail()
##   POST /dungeons/party/create            dungeon_routes.py:265    create_party()
##   GET  /dungeons/party/{id}              dungeon_routes.py:312    read_party()
##   POST /dungeons/party/invite            dungeon_routes.py:353    invite_player()
##   POST /dungeons/party/leave             dungeon_routes.py:411    leave_party()
##   POST /dungeons/party/ready             dungeon_routes.py:443    set_ready()
##   POST /dungeons/instance/create         dungeon_routes.py:484    enter_dungeon()
##   GET  /dungeons/instance/{id}           dungeon_routes.py:533    _read_run()
##   POST /dungeons/instance/start          dungeon_routes.py:579    enter_dungeon()
##   POST /dungeons/instance/advance        dungeon_routes.py:634    advance_floor()
##   POST /dungeons/instance/fail           dungeon_routes.py:680    fail_run()
##   POST /dungeons/instance/abandon        dungeon_routes.py:712    abandon_run()
##   POST /dungeons/instance/claim-loot     dungeon_routes.py:780    claim_rewards()
##
## THE FIVE THAT HAD NO CALLER, AND WHY EACH WAS NOT OPTIONAL
## ----------------------------------------------------------
## "A solo screen has no second player's id" was the reason the first version
## gave for skipping these. That reason is real for `invite` alone and wrong for
## the other four, which is why five routes shipped with no client caller:
##
##   * templates/{id} needs no second player at all. It is the ONLY route that
##     carries `floors` and `loot_table`, so before this the player could enter
##     a dungeon but never see what was in it.
##   * party/{id} needs no second player either, and it is the ONLY route that
##     returns a roster. Both party WRITES answer `{"ok": true}` and nothing
##     else (measured), so a client that drew members from their own reply
##     renders nothing and never learns who joined.
##   * party/leave is a one-field POST naming the caller. It is the only way out
##     of a party this panel can form.
##   * instance/fail is the counterpart to abandon, and the two do NOT accept the
##     same states -- see the fail_run() note. Without it the player could only
##     ever quit a run, never lose one.

## Emitted after any call that changed the run, so a host can re-read a balance
## it caches. The panel never writes GameState.wallet itself.
signal dungeon_changed
## Emitted for every completed call with the human-readable verdict, so a host
## can surface a toast without parsing labels.
signal dungeon_message(text: String, accepted: bool)

const PATH_TEMPLATES := "/dungeons/templates"
const PATH_PARTY_CREATE := "/dungeons/party/create"
const PATH_PARTY_READY := "/dungeons/party/ready"
const PATH_PARTY_INVITE := "/dungeons/party/invite"
const PATH_PARTY_LEAVE := "/dungeons/party/leave"
const PATH_INSTANCE_CREATE := "/dungeons/instance/create"
const PATH_INSTANCE_START := "/dungeons/instance/start"
const PATH_INSTANCE_ADVANCE := "/dungeons/instance/advance"
const PATH_INSTANCE_FAIL := "/dungeons/instance/fail"
const PATH_INSTANCE_ABANDON := "/dungeons/instance/abandon"
const PATH_INSTANCE_CLAIM := "/dungeons/instance/claim-loot"

## The three reward buckets claim-loot accepts. Measured: a value outside this
## set is refused 400 BEFORE any ledger is touched (dungeon_routes.py:810).
const REWARD_TYPES := ["loot", "first_clear", "weekly"]

## Run states, from server/dungeons.py's InstanceState. `waiting` is what
## instance/create returns and the only state start_instance accepts;
## `completed` is terminal, as are `failed` and `abandoned`.
const STATE_WAITING := "waiting"
const STATE_ACTIVE := "active"
const STATE_COMPLETED := "completed"
const STATE_FAILED := "failed"
const STATE_ABANDONED := "abandoned"
const TERMINAL_STATES := [STATE_COMPLETED, STATE_FAILED, STATE_ABANDONED]

## The templates route filters on `player_level >= template.min_level`, and the
## player row carries no `level` column (server/dungeon_routes.py:169-170
## documents this and deliberately leaves the parameter caller-supplied). 50 is
## the value this repo's own server tests use to reach a non-empty catalogue.
## A real player level, when GameState carries one, wins.
const FALLBACK_LEVEL := 50

var _api: Node
var player_id: String = ""

var _templates: Array = []
var _party_id: String = ""
var _instance_id: String = ""
var _instance: Dictionary = {}
var _selected_template_id: String = ""

## The member roster, from GET /dungeons/party/{id} -- the only route that
## returns one. Both party WRITES answer {"ok": true} alone (measured), so this
## is re-read after invite and after leave rather than patched from their reply.
var _roster: Array = []

## The floors and loot table from GET /dungeons/templates/{id}, the only route
## that carries either. Kept whole so _render_detail can redraw without a
## second request when the player switches back from the roster.
var _template_detail: Dictionary = {}

## One in-flight call at a time. Every action button mutates the same run, and
## a double press on advance would burn two floors.
var _busy: bool = false

## Confirmation answers are held on a MEMBER, not a local. A GDScript lambda
## captures a local BY VALUE, so writing to a captured local inside
## `confirmed.connect(...)` would leave the outer variable false forever and
## every confirmation would silently answer "no". See _confirm().
var _confirm_answer_override = null
var _confirm_answer: bool = false

@onready var refresh_button: Button = get_node_or_null("VBox/Header/RefreshButton") as Button
@onready var catalogue: VBoxContainer = get_node_or_null("VBox/CatalogueScroll/Catalogue") as VBoxContainer
@onready var catalogue_note: Label = get_node_or_null("VBox/CatalogueNote") as Label
@onready var status_label: Label = get_node_or_null("VBox/RunPanel/StatusLabel") as Label
@onready var detail_label: Label = get_node_or_null("VBox/RunPanel/DetailLabel") as Label
@onready var party_button: Button = get_node_or_null("VBox/RunPanel/PartyButton") as Button
@onready var ready_button: Button = get_node_or_null("VBox/RunPanel/ReadyButton") as Button
@onready var enter_button: Button = get_node_or_null("VBox/RunPanel/EnterButton") as Button
@onready var advance_button: Button = get_node_or_null("VBox/RunPanel/AdvanceButton") as Button
@onready var claim_button: Button = get_node_or_null("VBox/RunPanel/ClaimButton") as Button
@onready var abandon_button: Button = get_node_or_null("VBox/RunPanel/AbandonButton") as Button
@onready var fail_button: Button = get_node_or_null("VBox/RunPanel/FailButton") as Button
@onready var party_label: Label = get_node_or_null("VBox/PartyPanel/PartyLabel") as Label
@onready var roster_box: VBoxContainer = get_node_or_null("VBox/PartyPanel/MemberScroll/MemberBox") as VBoxContainer
@onready var invitee_edit: LineEdit = get_node_or_null("VBox/PartyPanel/InviteRow/InviteeEdit") as LineEdit
@onready var invite_button: Button = get_node_or_null("VBox/PartyPanel/InviteRow/InviteButton") as Button
@onready var roster_button: Button = get_node_or_null("VBox/PartyPanel/RosterButton") as Button
@onready var leave_button: Button = get_node_or_null("VBox/PartyPanel/LeaveButton") as Button
@onready var detail_title: Label = get_node_or_null("VBox/DetailPanel/DetailTitle") as Label
@onready var floors_box: VBoxContainer = get_node_or_null("VBox/DetailPanel/FloorsScroll/FloorsBox") as VBoxContainer
@onready var loot_label: Label = get_node_or_null("VBox/DetailPanel/LootLabel") as Label


func _ready() -> void:
	_api = get_node_or_null("/root/APIClient")

	# CONNECT FIRST, GATE SECOND. Every handler is wired before any early
	# return, because the alternative is a panel whose buttons look pressable
	# and silently do nothing: this panel is instanced by WorldMapView.tscn at
	# scene load, which is before GameState has necessarily filled in a player
	# id, so the two guards below used to return with the buttons still
	# unconnected and they stayed dead for the rest of the session.
	#
	# Measured: the first version of this file returned early on `player_id == ""`
	# with no handler ever connected, and a headless harness that only counted
	# node paths reported a clean PASS while every press was a no-op. Each
	# handler re-checks the same conditions through _can_act(), so connecting
	# early costs nothing and recovers the whole screen.
	if refresh_button:
		refresh_button.pressed.connect(refresh)
	if party_button:
		party_button.pressed.connect(create_party)
	if ready_button:
		ready_button.pressed.connect(set_ready)
	if enter_button:
		enter_button.pressed.connect(enter_dungeon)
	if advance_button:
		advance_button.pressed.connect(advance_floor)
	if claim_button:
		claim_button.pressed.connect(func(): claim_rewards("loot"))
	if abandon_button:
		abandon_button.pressed.connect(abandon_run)
	if fail_button:
		fail_button.pressed.connect(fail_run)
	if leave_button:
		leave_button.pressed.connect(leave_party)
	if invite_button:
		invite_button.pressed.connect(invite_player)
	if roster_button:
		roster_button.pressed.connect(read_party)

	if _api == null:
		# Not a wiring bug to hide: with no client there is nothing to call, and
		# saying so beats an empty screen that looks like "no dungeons exist".
		_set_status("Not connected to a server.")
		_set_detail("This panel needs the API client, which is missing.")
		_set_buttons_enabled(false)
		return
	player_id = _read_player_id()
	if player_id == "":
		_set_status("No signed-in player.")
		_set_detail("Sign in, then reopen this screen.")
		_set_buttons_enabled(false)
		return
	_set_buttons_enabled(true)
	# Deliberately NOT calling refresh() here. A host that mounts this panel
	# inside a screen the player opens often would otherwise spend a request
	# every time that screen loads, whether or not the player ever looks at
	# the dungeons. The host calls open_dungeons() when it shows the panel.
	_refresh_run_line()


## Show the panel and (re)read the catalogue. The host calls this when it makes
## the panel visible; `force` is false so returning to a tab already loaded in
## this session does not re-request on every tap.
func open_dungeons(force: bool = false) -> void:
	visible = true
	if force or _templates.is_empty():
		refresh()
	elif catalogue != null and catalogue.get_child_count() != _templates.size():
		_rebuild_catalogue()


## The id every dungeon route is scoped to. Read the same way TowerSystem.gd
## reads it, so a change to the bootstrap shape breaks one reader, not two.
func _read_player_id() -> String:
	if GameState.player_id != "":
		return GameState.player_id
	if GameState.player_data is Dictionary and GameState.player_data.has("id"):
		return str(GameState.player_data["id"])
	return ""


## The templates filter needs a realm. A realm the gate cannot rank makes it
## RAISE rather than return an empty list (dungeons.py:410-412), so an
## unrecognised value would turn the whole screen into an error. Default to the
## bottom of the ladder, which is always present (zones.py:206-214).
func _player_realm() -> String:
	var realm := str(GameState.player_data.get("cultivation_realm", ""))
	if realm == "":
		return "mortal"
	return realm


func _player_level() -> int:
	var raw = GameState.player_data.get("level", null)
	if raw is int or raw is float:
		return int(raw)
	return FALLBACK_LEVEL


# ----------------------------------------------------------------------------
# CALLS
# ----------------------------------------------------------------------------

## Read the catalogue. An empty array is a filter result, not an error, so the
## outcome key here is the array itself and never `ok`.
func refresh() -> void:
	if _busy or _api == null or player_id == "":
		return
	_busy = true
	_set_buttons_enabled(false)
	var query := "player_id=" + player_id.uri_encode() \
		+ "&realm=" + _player_realm().uri_encode() \
		+ "&level=" + str(_player_level())
	var result: Dictionary = await _api.request_get(PATH_TEMPLATES + "?" + query)
	_busy = false

	if not result.get("ok", false):
		_set_status("Could not load dungeons.")
		_set_detail(_reason(result))
		_announce(_reason(result), false)
		_set_buttons_enabled(true)
		return

	var body = result.get("data", null)
	if not (body is Array):
		# A 200 that is not a list is a shape change, not an empty catalogue.
		_set_status("The server sent an unexpected catalogue.")
		_announce("Unexpected catalogue shape.", false)
		_set_buttons_enabled(true)
		return

	_templates = body
	_rebuild_catalogue()
	if _templates.is_empty():
		_set_status("No dungeons open to you yet.")
		_set_detail("Dungeons unlock by cultivation realm and level. Realm: %s."
			% _player_realm())
	else:
		_set_status("%d dungeon(s) available." % _templates.size())
		_set_detail("Pick one, form a party, and enter.")
	_set_buttons_enabled(true)


## Form a solo party. Refused 400 "Already in a party" if the player is already
## in one (dungeon_routes.py:283-284) -- a real refusal, and it says so.
func create_party() -> void:
	if not _can_act():
		return
	_busy = true
	var result: Dictionary = await _api.post(
		PATH_PARTY_CREATE, {"leader_id": player_id})
	var ok: bool = _judge(result, "ok")
	_busy = false
	if ok:
		_party_id = str(result.get("data", {}).get("party_id", ""))
		_set_detail("Party formed. Ready up, then enter.")
		_announce("Party formed.", true)
		# party/create DOES carry a members array, but read it from the roster
		# route anyway so the panel has one source of truth for the member list.
		await read_party()
	else:
		_set_detail(_reason(result))
		_announce(_reason(result), false)
	_refresh_run_line()
	_set_buttons_enabled(true)


## Every member must be ready before instance/create will form a run
## (dungeon_routes.py:501 calls all_ready). On a solo party that is just me.
func set_ready() -> void:
	if not _can_act():
		return
	_busy = true
	var result: Dictionary = await _api.post(
		PATH_PARTY_READY, {"player_id": player_id, "ready": true})
	var ok: bool = _judge(result, "ok")
	_busy = false
	if ok:
		_set_detail("Ready. Now enter the dungeon.")
		_announce("Ready.", true)
	else:
		_set_detail(_reason(result))
		_announce(_reason(result), false)
	_refresh_run_line()
	_set_buttons_enabled(true)


## Create the run, then start it. Two separate routes with separate state
## gates, and only the party leader may start (dungeon_routes.py:608).
func enter_dungeon() -> void:
	if not _can_act():
		return
	if _party_id == "":
		_set_detail("Form a party first.")
		return
	if _selected_template_id == "":
		_set_detail("Choose a dungeon first.")
		return

	_busy = true
	var created: Dictionary = await _api.post(PATH_INSTANCE_CREATE, {
		"party_id": _party_id, "template_id": _selected_template_id})
	if not _judge(created, "ok"):
		_busy = false
		# One 400 covers party size, readiness and template fit at once
		# (dungeon_routes.py:503), so the string cannot be trusted to name the
		# cause. Say that rather than repeat a message that misleads.
		_set_detail("The server refused to form this run: %s\n%s"
			% [_reason(created),
			   "Check party size and that every member is ready."])
		_announce("Could not form the run.", false)
		_set_buttons_enabled(true)
		return

	_instance_id = str(created.get("data", {}).get("instance_id", ""))
	_set_detail("Run formed. Starting it...")
	_set_buttons_enabled(false)

	var started: Dictionary = await _api.post(PATH_INSTANCE_START, {
		"instance_id": _instance_id, "player_id": player_id})
	_busy = false
	if _judge(started, "ok"):
		_announce("Dungeon started.", true)
	else:
		_announce(_reason(started), false)
	await _read_run()
	_set_buttons_enabled(true)
	dungeon_changed.emit()


## Move the party one floor deeper. The outcome field is `completed`: this call
## ends a run, and a completed run drops its loot (dungeon_routes.py:656-657).
func advance_floor() -> void:
	if not _can_act() or _instance_id == "":
		return
	_busy = true
	var result: Dictionary = await _api.post(PATH_INSTANCE_ADVANCE, {
		"instance_id": _instance_id, "player_id": player_id})
	var ok: bool = _judge(result, "ok")
	var completed: bool = bool(result.get("data", {}).get("completed", false))
	_busy = false
	if not ok:
		_set_detail(_reason(result))
		_announce(_reason(result), false)
	elif completed:
		_set_detail("Final floor cleared. Claim the rewards.")
		_announce("Dungeon cleared.", true)
	else:
		_set_detail("Deeper. The next floor is open.")
		_announce("Advanced a floor.", true)
	await _read_run()
	_set_buttons_enabled(true)
	dungeon_changed.emit()


## THE ROUTE THAT NEEDS THE OUTCOME FIELD.
##
## Measured: a repeat claim returns HTTP 200 with {"ok": true, "rewards": []}.
## The status code and `ok` both say success; the empty array says the server
## kept the items. So the verdict is read off the ARRAY, and a refused claim is
## reported as refused rather than as a successful, empty payout.
func claim_rewards(reward_type: String) -> void:
	if not _can_act() or _instance_id == "":
		return
	if not REWARD_TYPES.has(reward_type):
		_set_detail("Unknown reward type: %s" % reward_type)
		return
	_busy = true
	var result: Dictionary = await _api.post(PATH_INSTANCE_CLAIM, {
		"instance_id": _instance_id,
		"player_id": player_id,
		"reward_type": reward_type})
	_busy = false
	var verdict := _judge_claim(result)
	_set_detail("%s: %s" % [reward_type, str(verdict["text"])])
	_announce(str(verdict["text"]), bool(verdict["accepted"]))
	await _read_run()
	_set_buttons_enabled(true)
	dungeon_changed.emit()


## End the run for the party. Permanent: nothing returns a run to a live state.
func abandon_run() -> void:
	if not _can_act() or _instance_id == "":
		return
	_busy = true
	var result: Dictionary = await _api.post(PATH_INSTANCE_ABANDON, {
		"instance_id": _instance_id, "player_id": player_id})
	var ok: bool = _judge(result, "ok")
	_busy = false
	if ok:
		_instance_id = ""
		_instance = {}
		_set_detail("Run abandoned.")
		_announce("Run abandoned.", true)
	else:
		_set_detail(_reason(result))
		_announce(_reason(result), false)
	_refresh_run_line()
	_set_buttons_enabled(true)
	dungeon_changed.emit()


## GET /dungeons/party/{party_id} -- ROUTE 4 OF 5, and the only one that returns
## a roster.
##
## Both party WRITES answer `{"ok": true}` and nothing else (measured), so a
## client that drew the member list off invite or leave renders nothing at all
## and never learns who joined. That is why this is re-read after both.
##
## Refusals are all real and all distinguishable (measured):
##   403 "Not a member of this party"  the caller is not on this roster
##   401                              no session
##   404                              the guard passes but the party is gone
func read_party() -> void:
	if _busy or _api == null or player_id == "":
		return
	if _party_id == "":
		_roster = []
		_set_party_note("No party yet.")
		_render_roster()
		return
	_busy = true
	_set_buttons_enabled(false)
	var query := "player_id=" + player_id.uri_encode()
	var result: Dictionary = await _api.request_get(
		"/dungeons/party/" + _party_id.uri_encode() + "?" + query)
	_busy = false

	if not result.get("ok", false):
		# A refused roster read is a fact about this party, not about the
		# transport: keep the cached roster and say why it is not refreshing.
		_set_party_note(_reason(result))
		_set_buttons_enabled(true)
		return

	var body = result.get("data", null)
	if not (body is Dictionary):
		_set_party_note("The server sent an unexpected party reply.")
		_set_buttons_enabled(true)
		return

	var members = body.get("members", null)
	_roster = members if members is Array else []
	_set_party_note(_party_summary(body))
	_render_roster()
	_set_buttons_enabled(true)


## POST /dungeons/party/invite -- ROUTE 5 OF 5.
##
## THE FIELD IS `invitee_id`, NOT `player_id`. Measured: posting `player_id`
## here answers 403 "player_id does not match the authenticated session",
## because the central ownership guard reads a field by that name as the ACTING
## player -- and on this route that field names a THIRD PARTY. Every other party
## route here uses `player_id` as the actor, so the one call that must not is
## the one most likely to be copied from its neighbour.
##
## Measured refusals, all distinct from a success:
##   403 "Not a member of this party"  self-invite, or the caller is not in it
##   400                              party full, or invitee already in a party
func invite_player() -> void:
	if not _can_act():
		return
	if _party_id == "":
		_set_party_note("Form a party first.")
		return
	var invitee := invitee_edit.text.strip_edges() if invitee_edit != null else ""
	if invitee == "":
		_set_party_note("Type a player id to invite.")
		return
	_busy = true
	_set_buttons_enabled(false)
	var result: Dictionary = await _api.post(PATH_PARTY_INVITE, {
		"party_id": _party_id,
		"invitee_id": invitee,
	})
	var ok: bool = _judge(result, "ok")
	_busy = false
	if ok:
		_set_party_note("Invited %s." % invitee)
		_announce("Invited %s" % invitee, true)
		if invitee_edit != null:
			invitee_edit.text = ""
	else:
		_set_party_note(_reason(result))
		_announce(_reason(result), false)
	# The invite reply carries no roster, so re-read it either way -- a refusal
	# can also be a state the roster explains (party already full).
	await read_party()
	_set_buttons_enabled(true)
	dungeon_changed.emit()


## POST /dungeons/party/leave -- ROUTE 3 OF 5.
##
## The body names the CALLER, so it is `player_id` here (measured 403 on
## naming anyone else). Measured:
##   400 "Not in a party"  already out
## and by a SOLO LEADER this DISBANDS the party outright: nothing on the server
## returns a disbanded party, so a confirmation is the point rather than
## decoration. Leaving also cannot be undone, which is why the dialog is not
## skippable by accident -- the answer lives on a member for exactly the reason
## in _confirm().
func leave_party() -> void:
	if not _can_act():
		return
	if _party_id == "":
		_set_party_note("You are not in a party.")
		return
	var solo := _roster.size() <= 1
	if solo and not await _confirm(
			"Leaving as the only member disbands the party. Continue?"):
		return
	_busy = true
	_set_buttons_enabled(false)
	var result: Dictionary = await _api.post(PATH_PARTY_LEAVE, {
		"player_id": player_id,
	})
	var ok: bool = _judge(result, "ok")
	_busy = false
	if ok:
		# The party id is dead on the server now. Clearing it stops every later
		# button from aiming a request at an id that can only 403.
		_roster = []
		_party_id = ""
		_set_party_note("Left the party.")
		_announce("Left the party.", true)
		_render_roster()
		_refresh_run_line()
	else:
		_set_party_note(_reason(result))
		_announce(_reason(result), false)
	_set_buttons_enabled(true)
	dungeon_changed.emit()


## POST /dungeons/instance/fail -- ROUTE 2 OF 5, and NOT a synonym for abandon.
##
## MEASURED, and the difference is the whole point of wiring it separately:
##
##   state=waiting   fail -> 400 "Cannot fail instance"   abandon -> 200
##   state=active    fail -> 200                          abandon -> 200
##
## So a waiting run can be quit but not failed, and the button is gated on the
## state rather than left to spend a request discovering a 400. Terminal like
## abandon: measured the instance read afterwards reports state=failed and
## nothing returns a run to a live state.
func fail_run() -> void:
	if not _can_act():
		return
	if _instance_id == "":
		_set_detail("No run to fail.")
		return
	var state := str(_instance.get("state", ""))
	if state != STATE_ACTIVE:
		# Say the state rather than repeat a refusal that cannot name its cause.
		_set_detail("Only a running dungeon can fail. This one is %s."
			% ("unknown" if state == "" else state))
		return
	if not await _confirm("Fail this run for the whole party? This cannot be undone."):
		return
	_busy = true
	_set_buttons_enabled(false)
	var result: Dictionary = await _api.post(PATH_INSTANCE_FAIL, {
		"instance_id": _instance_id,
		"player_id": player_id,
	})
	var ok: bool = _judge(result, "ok")
	var now_state := str(result.get("data", {}).get("state", "")) if ok else ""
	_busy = false
	if ok:
		# The reply carries its own state, so update before re-reading rather
		# than waiting on a round trip to learn the run is already over.
		if now_state != "":
			_instance["state"] = now_state
		_set_detail("Run failed.")
		_announce("Run failed.", true)
	else:
		_set_detail(_reason(result))
		_announce(_reason(result), false)
	await _read_run()
	_set_buttons_enabled(true)
	dungeon_changed.emit()


## GET /dungeons/templates/{template_id} -- ROUTE 1 OF 5.
##
## The only route carrying `floors` and `loot_table`. Measured, two shape facts
## a client gets wrong if it assumes symmetry with the catalogue:
##
##   * it carries NO `ok` field, so the verdict is transport + shape, not `ok`;
##   * it carries NO `total_floors`. The LIST route has that int; this one has
##     a `floors` ARRAY and no such key. Reading total_floors here yields 0 and
##     renders "floor 0 of 0" on a real three-floor dungeon.
##
## Measured refusals: 404 unknown id, 403 a session for someone else, 401 none.
func read_template_detail(template_id: String) -> void:
	if _busy or _api == null or player_id == "":
		return
	if template_id == "":
		return
	_busy = true
	_set_buttons_enabled(false)
	var query := "player_id=" + player_id.uri_encode()
	var result: Dictionary = await _api.request_get(
		"/dungeons/templates/" + template_id.uri_encode() + "?" + query)
	_busy = false

	if not result.get("ok", false):
		_set_detail_note(_reason(result))
		_announce(_reason(result), false)
		_set_buttons_enabled(true)
		return

	var body = result.get("data", null)
	if not (body is Dictionary):
		# A 200 that is not a dict is a shape change, not an empty dungeon.
		_set_detail_note("The server sent an unexpected detail reply.")
		_announce("Unexpected detail reply.", false)
		_set_buttons_enabled(true)
		return

	_template_detail = body
	_render_detail(body)
	_set_buttons_enabled(true)


## Read the live run. This route carries NO `ok` field (measured), so the
## outcome key is `state`.
func read_run() -> void:
	await _read_run()


func _read_run() -> void:
	if _instance_id == "" or _api == null:
		_refresh_run_line()
		return
	var query := "player_id=" + player_id.uri_encode()
	var result: Dictionary = await _api.request_get(
		"/dungeons/instance/" + _instance_id.uri_encode() + "?" + query)
	if not result.get("ok", false):
		_refresh_run_line()
		return
	var body = result.get("data", null)
	if not (body is Dictionary) or not body.has("state"):
		_refresh_run_line()
		return
	_instance = body
	_refresh_run_line()


# ----------------------------------------------------------------------------
# VERDICTS -- the outcome field, never the status code
# ----------------------------------------------------------------------------

## Generic verdict: did the server's OWN outcome field say this worked?
##
## `result.ok` is only the transport gate (APIClient sets it false for a non-2xx
## or an unparseable body -- client/scripts/network/APIClient.gd:117-127). A 200
## refusal reaches here with ok=true and is caught by the outcome key, except on
## the routes whose refusal carries no boolean at all, which is why every call
## site names its own key.
func _judge(result: Dictionary, outcome_key: String) -> bool:
	if not result.get("ok", false):
		return false
	var body = result.get("data", null)
	if not (body is Dictionary):
		# A list or a bare value: no outcome field to read, so a 2xx is all
		# the evidence there is. Callers that can receive a list do not use
		# this helper (see refresh()).
		return true
	return bool(body.get(outcome_key, false))


## The claim verdict, read off the rewards ARRAY.
##
## Measured, same instance, same player:
##   first claim -> 200 {"ok": true, "rewards": [{...2 items...}]}
##   replay      -> 200 {"ok": true, "rewards": []}
## The replay is a refusal a status-code check calls a success. A non-empty
## array is the only evidence the items actually moved.
func _judge_claim(result: Dictionary) -> Dictionary:
	if not result.get("ok", false):
		return {"accepted": false, "text": _reason(result)}
	var body = result.get("data", null)
	if not (body is Dictionary):
		return {"accepted": false,
			"text": "The server sent an unexpected claim reply."}
	var rewards = body.get("rewards", null)
	if not (rewards is Array):
		return {"accepted": false,
			"text": "The server sent an unexpected claim reply."}
	if rewards.is_empty():
		# 200, ok:true, nothing paid. Say what that is.
		return {"accepted": false,
			"text": "Nothing to take. Already claimed, or this run dropped no loot."}
	var parts: Array[String] = []
	for item in rewards:
		if item is Dictionary:
			parts.append("%s x%d" % [item.get("item_id", "?"),
				int(item.get("count", 0))])
	return {"accepted": true, "text": "Received " + ", ".join(parts)}


## The human-readable reason. APIClient already lifted `detail` out of a
## refusal body into `error` (APIClient.gd:126-127), so this is the server's
## own wording, never a guess.
func _reason(result: Dictionary) -> String:
	var text := str(result.get("error", "")).strip_edges()
	if text == "":
		return "The server refused the request."
	return text


func _can_act() -> bool:
	if _busy:
		return false
	if _api == null:
		_set_detail("Not connected to a server.")
		return false
	if player_id == "":
		_set_detail("No signed-in player.")
		return false
	return true


# ----------------------------------------------------------------------------
# DRAWING
# ----------------------------------------------------------------------------

func _rebuild_catalogue() -> void:
	if catalogue == null:
		return
	for child in catalogue.get_children():
		catalogue.remove_child(child)
		child.queue_free()
	for template in _templates:
		if template is Dictionary:
			catalogue.add_child(_make_template_row(template))
	if catalogue_note:
		catalogue_note.text = "" if not _templates.is_empty() else "Nothing to show."


func _make_template_row(template: Dictionary) -> Control:
	var row := VBoxContainer.new()
	row.add_theme_constant_override("separation", 2)

	var name_label := Label.new()
	name_label.text = str(template.get("name", "?"))
	name_label.add_theme_constant_override("font_size", 16)
	row.add_child(name_label)

	var meta := Label.new()
	meta.text = "%s\n%s realm  |  floors %d  |  party %d-%d  |  lv %d-%d" % [
		str(template.get("description", "")).left(70),
		str(template.get("recommended_realm", "?")),
		int(template.get("total_floors", 0)),
		int(template.get("min_party_size", 1)),
		int(template.get("max_party_size", 1)),
		int(template.get("min_level", 0)),
		int(template.get("max_level", 0)),
	]
	meta.add_theme_constant_override("font_size", 12)
	meta.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	row.add_child(meta)

	var pick := Button.new()
	pick.text = "Choose"
	pick.custom_minimum_size = Vector2(0, 30)
	var template_id := str(template.get("id", ""))
	pick.pressed.connect(func(): select_template(template_id))
	row.add_child(pick)

	return row


## Selecting a template also drops a stale run: the old instance belongs to the
## previous dungeon, and keeping its id on screen would invite a claim against
## the wrong run.
func select_template(template_id: String) -> void:
	_selected_template_id = template_id
	_instance_id = ""
	_instance = {}
	_refresh_run_line()
	_set_detail("Chosen: %s. Form a party, then enter." % template_id)
	# Pull the floors and the loot table now, while the player is still deciding.
	# This is the only route that carries either, so without this the choice is
	# made blind. Deliberately NOT awaited: selecting must return immediately so
	# the catalogue stays responsive while the read is in flight.
	read_template_detail(template_id)


## One line describing the live run, or the absence of one. Driven by `state`,
## because that is the field the instance read actually carries.
func _refresh_run_line() -> void:
	if _instance.is_empty():
		if status_label and status_label.text.find("dungeon(s)") >= 0:
			pass
		return
	var state := str(_instance.get("state", "?"))
	var floor := int(_instance.get("current_floor", 0))
	var total := int(_instance.get("total_floors", 0))
	var name_text := str(_instance.get("name", "?"))
	var enemies = _instance.get("current_floor_enemies", [])
	if status_label:
		status_label.text = "%s -- %s -- floor %d of %d -- %d on this floor" % [
			name_text, state, floor, total, enemies.size() if enemies is Array else 0]


## Buttons are disabled while a call is in flight, so a second press cannot
## advance the same run twice.
##
## `fail` is the one exception that is gated on STATE as well: measured, a run
## that is still `waiting` answers 400 "Cannot fail instance", so the button is
## held off until the run is actually live rather than left to spend a request
## discovering the refusal.
func _set_buttons_enabled(enabled: bool) -> void:
	for button in [refresh_button, party_button, ready_button, enter_button,
			advance_button, claim_button, abandon_button, fail_button,
			leave_button, invite_button, roster_button]:
		if button:
			button.disabled = not enabled
	if fail_button:
		# Re-derived from _instance every call, so a failed run cannot be failed
		# again and a fresh one becomes failable the moment it starts.
		fail_button.disabled = not enabled or str(_instance.get("state", "")) != STATE_ACTIVE
	if leave_button:
		leave_button.disabled = not enabled or _party_id == ""
	if invite_button:
		invite_button.disabled = not enabled or _party_id == ""


func _set_status(text: String) -> void:
	if status_label:
		status_label.text = text


func _set_detail(text: String) -> void:
	if detail_label:
		detail_label.text = text


# ----------------------------------------------------------------------------
# PARTY ROSTER AND TEMPLATE DETAIL
# ----------------------------------------------------------------------------

## One line naming the party, its leader and whether it holds a run.
func _party_summary(body: Dictionary) -> String:
	var leader := str(body.get("leader_id", ""))
	var is_leader: bool = (player_id != "" and leader == player_id)
	var run_id = body.get("instance_id", null)
	var run_text := "" if run_id == null or str(run_id) == "" else " | run %s" % str(run_id)
	return "Party %s | you are the %s%s" % [
		str(body.get("party_id", "?")),
		"leader" if is_leader else "a member",
		run_text,
	]


func _render_roster() -> void:
	if roster_box == null:
		return
	for child in roster_box.get_children():
		roster_box.remove_child(child)
		child.queue_free()
	for member in _roster:
		if member is Dictionary:
			roster_box.add_child(_make_member_row(member))


## The roster carries identifiers only -- measured: no name, no realm, no level --
## so a short id is the most a row can honestly show.
func _make_member_row(member: Dictionary) -> Control:
	var row := HBoxContainer.new()
	row.add_theme_constant_override("separation", 8)

	var who := Label.new()
	who.text = str(member.get("player_id", "?"))
	if str(member.get("player_id", "")) == player_id:
		who.text += "  (you)"
	who.add_theme_constant_override("font_size", 13)
	who.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(who)

	var role := Label.new()
	role.text = str(member.get("role", "?"))
	role.add_theme_constant_override("font_size", 12)
	row.add_child(role)

	var is_ready: bool = bool(member.get("ready", false))
	var ready_label := Label.new()
	ready_label.text = "ready" if is_ready else "not ready"
	ready_label.add_theme_constant_override("font_size", 12)
	ready_label.add_theme_color_override("font_color",
		Color(0.6, 0.95, 0.6) if is_ready else Color(0.9, 0.75, 0.6))
	row.add_child(ready_label)

	return row


## The floors and the loot table. Neither is on the catalogue row, so this is
## the only place a player can see either before committing to a run.
func _render_detail(body: Dictionary) -> void:
	if detail_title:
		detail_title.text = "%s -- route detail" % str(body.get("name", "?"))
	if floors_box:
		for child in floors_box.get_children():
			floors_box.remove_child(child)
			child.queue_free()
		var floors = body.get("floors", null)
		if floors is Array:
			for entry in floors:
				if entry is Dictionary:
					floors_box.add_child(_make_floor_row(entry))
	if loot_label == null:
		return
	var loot = body.get("loot_table", null)
	if not (loot is Array) or loot.is_empty():
		loot_label.text = "No loot table published."
		return
	var parts: Array[String] = []
	for item in loot:
		if item is Dictionary:
			parts.append("%s %d-%d at %d%%" % [
				str(item.get("item_id", "?")),
				int(item.get("min_count", 0)),
				int(item.get("max_count", 0)),
				roundi(float(item.get("chance", 0.0)) * 100.0),
			])
	loot_label.text = "Loot: " + ", ".join(parts)


func _make_floor_row(entry: Dictionary) -> Control:
	var row := Label.new()
	# `boss` is measured as an id STRING on the floor that has a boss and as the
	# BOOLEAN false on floors that do not, so it must be tested by TYPE. In
	# GDScript `"spirit_beast_king" == false` is a runtime error on String-vs-bool
	# operands, not a quiet false.
	var boss = entry.get("boss", false)
	var boss_text := ""
	if boss is String and not (boss as String).is_empty():
		boss_text = "  BOSS: %s" % boss
	elif boss is bool and bool(boss):
		boss_text = "  BOSS"
	var enemies = entry.get("enemies", [])
	var names := ""
	if enemies is Array:
		var collected: Array[String] = []
		for enemy in enemies:
			collected.append(str(enemy))
		names = ", ".join(collected)
	row.text = "Floor %d -- %s%s%s" % [
		int(entry.get("floor", 0)),
		str(entry.get("name", "?")),
		("  enemies: " + names) if names != "" else "  no enemies",
		boss_text,
	]
	row.add_theme_constant_override("font_size", 12)
	row.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	return row


## Confirmation prompt, overridable so a headless harness can answer without a
## window. The default is a real modal, because both confirmable actions here
## are permanent for other people as well as for the player.
##
## The answer is carried on a MEMBER, not a local: a GDScript lambda captures a
## local by value at creation, so assigning to a captured local inside
## `confirmed.connect(...)` would leave the outer variable false forever and
## every confirmation would silently answer "no". This function is a coroutine
## (`await get_tree().process_frame`), so every caller must `await` it.
func _confirm(text: String) -> bool:
	if _confirm_answer_override != null:
		return bool(_confirm_answer_override)
	_confirm_answer = false
	var dialog := ConfirmationDialog.new()
	dialog.title = "Are you sure?"
	dialog.dialog_text = text
	add_child(dialog)
	dialog.confirmed.connect(func() -> void: _confirm_answer = true)
	dialog.popup_centered()
	while dialog.visible:
		await get_tree().process_frame
	dialog.queue_free()
	return _confirm_answer


func _set_party_note(text: String) -> void:
	if party_label:
		party_label.text = text


func _set_detail_note(text: String) -> void:
	if loot_label:
		loot_label.text = text


func _announce(text: String, accepted: bool) -> void:
	dungeon_message.emit(text, accepted)