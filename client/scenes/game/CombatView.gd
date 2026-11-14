extends GamePanel
## CombatView.gd — Combat screen controller with PvP Arena
##
## MIGRATED ONTO GamePanel -- boilerplate only, no behaviour change.
##
##   1. `_ready()` was renamed `_on_ready()`. This one is load-bearing:
##      Godot calls ONLY the most-derived `_ready()`, so keeping our own
##      would have skipped the base's API client, player id and status
##      binding setup, and the screen would render nothing.
##   2. `auto_toggle_buttons = false`, because this panel owns its button
##      states. _show_tab() disables the ACTIVE tab on purpose, and
##      _on_battle_pressed() disables BattleBtn for the duration of a
##      battle. The base's guard re-enables the buttons IT turned off, and
##      it cannot tell a deliberate disable from its own.
##   3. `bind_status(combat_status)`. This panel's status line is named
##      "CombatStatus", not "Status", so the base's default lookup finds
##      nothing and all four display helpers would be silent no-ops.
##      The panel's own `combat_status.text = ...` writes are untouched.
##
## NOT DONE, deliberately:
##   * Nothing was deleted. This panel declares no member the base also
##     owns (`_busy`, `_generation`, `_api`, `_key_seq`, `_missing_nodes`,
##     `_status_hold_until_msec`, `_status_label`), so there was no
##     shadowed copy to remove and no `_status_label` rename to make.
##   * The typed-lookup helpers (`label_at`, `button_at`, ...) were NOT
##     adopted: this screen also looks up RichTextLabel and plain Control,
##     and GamePanel ships no helper for either.
##   * `action_family` / `tag_action()` were NOT used. Setting them makes
##     buttons count as LIVE to the hub's styler, which is a behaviour
##     change, not boilerplate.
##
## The base is Control, and so was this panel, so every call that compiled
## before still compiles -- no container-only call on `self` was added or
## removed.

@onready var tab_buttons: Array[Button] = [
	get_node_or_null("VBox/TabButtons/AutoBattleTab") as Button,
	get_node_or_null("VBox/TabButtons/PvPTab") as Button,
	get_node_or_null("VBox/TabButtons/RankingTab") as Button,
	get_node_or_null("VBox/TabButtons/HistoryTab") as Button,
	get_node_or_null("VBox/TabButtons/TournamentTab") as Button,
]

## INDEX CONTRACT: panels[i] must be the panel that tab_buttons[i] opens.
## _show_tab() walks range(tab_buttons.size()), so an entry parked past the
## last button index is never reached and its button shows the wrong panel.
## TournamentPanel therefore sits at index 4, matching TournamentTab, and
## ReplayPanel stays at index 5 -- deliberately outside the walk, because it
## is an overlay driven by _show_replay/_hide_replay by node path, not by a tab.
@onready var panels: Array[Control] = [
	get_node_or_null("VBox/AutoBattlePanel") as Control,
	get_node_or_null("VBox/PvPPanel") as Control,
	get_node_or_null("VBox/RankingPanel") as Control,
	get_node_or_null("VBox/HistoryPanel") as Control,
	get_node_or_null("VBox/TournamentPanel") as Control,
	get_node_or_null("VBox/ReplayPanel") as Control,
]

@onready var player_hp: Label = get_node_or_null("VBox/AutoBattlePanel/PlayerHP") as Label
@onready var enemy_hp: Label = get_node_or_null("VBox/AutoBattlePanel/EnemyHP") as Label
@onready var turn_label: Label = get_node_or_null("VBox/AutoBattlePanel/TurnLabel") as Label
@onready var log_box: RichTextLabel = get_node_or_null("VBox/AutoBattlePanel/AutoBattleLogBox") as RichTextLabel
@onready var battle_btn: Button = get_node_or_null("VBox/AutoBattlePanel/AutoBattleHBox/BattleBtn") as Button
@onready var auto_back_btn: Button = get_node_or_null("VBox/AutoBattlePanel/AutoBattleHBox/AutoBackBtn") as Button
@onready var battle_result: Label = get_node_or_null("VBox/AutoBattlePanel/AutoBattleResult") as Label

@onready var matchmaking_button: Button = get_node_or_null("VBox/PvPPanel/MatchmakingButton") as Button
@onready var candidates_container: VBoxContainer = get_node_or_null("VBox/PvPPanel/CandidatesContainer") as VBoxContainer
@onready var candidate_list: VBoxContainer = get_node_or_null("VBox/PvPPanel/CandidatesContainer/CandidateList") as VBoxContainer
@onready var pvp_log_box: RichTextLabel = get_node_or_null("VBox/PvPPanel/PvPLogBox") as RichTextLabel
@onready var pvp_back_btn: Button = get_node_or_null("VBox/PvPPanel/PvPHBox/PvPBackBtn") as Button

@onready var my_rank_label: Label = get_node_or_null("VBox/RankingPanel/MyRankLabel") as Label
@onready var leaderboard_list: VBoxContainer = get_node_or_null("VBox/RankingPanel/LeaderboardContainer/LeaderboardList") as VBoxContainer
@onready var ranking_back_btn: Button = get_node_or_null("VBox/RankingPanel/RankingBackBtn") as Button

@onready var history_list: VBoxContainer = get_node_or_null("VBox/HistoryPanel/HistoryList") as VBoxContainer
@onready var history_back_btn: Button = get_node_or_null("VBox/HistoryPanel/HistoryBackBtn") as Button

@onready var replay_log_box: RichTextLabel = get_node_or_null("VBox/ReplayPanel/ReplayLogBox") as RichTextLabel
@onready var replay_info: Label = get_node_or_null("VBox/ReplayPanel/ReplayInfo") as Label
@onready var replay_close_btn: Button = get_node_or_null("VBox/ReplayPanel/ReplayCloseBtn") as Button

@onready var combat_status: Label = get_node_or_null("VBox/CombatStatus") as Label

var combat_system: Node
var _combat_view_bound := false
var _current_tab: int = 0
var _selected_opponent: Dictionary = {}

func _on_ready() -> void:
	# See the migration note at the top of this file.
	auto_toggle_buttons = false
	bind_status(combat_status)
	add_to_group("combat_view")
	if _combat_view_bound and combat_system:
		combat_system = null
	if not combat_system:
		combat_system = preload("res://scripts/systems/CombatSystem.gd").new()
		# Own it in the tree so it is freed with this scene. Without add_child
		# the instance is an orphan: it outlives the scene and shows up in the
		# ObjectDB leak report at exit.
		add_child(combat_system)
	combat_system.battle_completed.connect(_on_battle_completed)
	combat_system.pvp_match_completed.connect(_on_pvp_completed)
	combat_system.ranking_loaded.connect(_on_ranking_loaded)
	combat_system.matchmaking_ready.connect(_on_matchmaking_ready)
	combat_system.history_loaded.connect(_on_history_loaded)
	combat_system.replay_loaded.connect(_on_replay_loaded)
	combat_system.error_occurred.connect(_on_error)
	
	if battle_btn:
		battle_btn.pressed.connect(_on_battle_pressed)
	if auto_back_btn:
		auto_back_btn.pressed.connect(func(): _show_tab(0))
	if matchmaking_button:
		matchmaking_button.pressed.connect(func(): combat_system.pvp_matchmaking())
	if pvp_back_btn:
		pvp_back_btn.pressed.connect(func(): _show_tab(0))
	if ranking_back_btn:
		ranking_back_btn.pressed.connect(func(): _show_tab(0))
	if history_back_btn:
		history_back_btn.pressed.connect(func(): _show_tab(0))
	if replay_close_btn:
		replay_close_btn.pressed.connect(_hide_replay)
	
	for i in range(tab_buttons.size()):
		if tab_buttons[i]:
			tab_buttons[i].pressed.connect(_on_tab_pressed.bind(i))
	
	_show_tab(0)
	
	# Load initial data
	combat_system.get_pvp_ranking()
	combat_system.get_pvp_history()
	if combat_status:
		combat_status.text = combat_system._cosmetic_status_text() if combat_system.has_method("_cosmetic_status_text") else combat_status.text

func refresh_cosmetic_status() -> void:
	if combat_status and combat_system and combat_system.has_method("_cosmetic_status_text"):
		combat_status.text = combat_system._cosmetic_status_text()

func bind_combat_system(system: Node) -> void:
	combat_system = system
	if combat_status and combat_system and combat_system.has_method("_cosmetic_status_text"):
		combat_status.text = combat_system._cosmetic_status_text()
	if not _combat_view_bound and system:
		if not system.battle_completed.is_connected(_on_battle_completed): system.battle_completed.connect(_on_battle_completed)
		if not system.pvp_match_completed.is_connected(_on_pvp_completed): system.pvp_match_completed.connect(_on_pvp_completed)
		if not system.ranking_loaded.is_connected(_on_ranking_loaded): system.ranking_loaded.connect(_on_ranking_loaded)
		if not system.matchmaking_ready.is_connected(_on_matchmaking_ready): system.matchmaking_ready.connect(_on_matchmaking_ready)
		if not system.history_loaded.is_connected(_on_history_loaded): system.history_loaded.connect(_on_history_loaded)
		if not system.replay_loaded.is_connected(_on_replay_loaded): system.replay_loaded.connect(_on_replay_loaded)
		if not system.error_occurred.is_connected(_on_error): system.error_occurred.connect(_on_error)
		_combat_view_bound = true
		system.get_pvp_ranking()
		system.get_pvp_history()

func _on_tab_pressed(tab_index: int) -> void:
	_show_tab(tab_index)

func _show_tab(index: int) -> void:
	_current_tab = index
	for i in range(tab_buttons.size()):
		if tab_buttons[i]:
			tab_buttons[i].button_pressed = (i == index)
			tab_buttons[i].disabled = (i == index)
		if panels[i]:
			panels[i].visible = (i == index)
	
	if index == 1 and not candidates_container.visible:
		# Refresh matchmaking when entering PvP tab
		combat_system.pvp_matchmaking()
	elif index == 2:
		combat_system.get_pvp_ranking()
	elif index == 3:
		combat_system.get_pvp_history()

func _on_battle_pressed() -> void:
	log_box.text = ""
	battle_result.visible = false
	battle_btn.disabled = true
	
	# Generate enemy based on player realm
	var realm = GameState.player_data.get("cultivation_realm", "mortal")
	var stage = GameState.player_data.get("cultivation_stage", "early")
	var enemy = combat_system.generate_enemy_for_realm(realm, stage)
	
	# Define rewards
	var rewards = {"spirit_stone": 100}
	
	var result = await combat_system.perform_auto_battle(enemy, rewards)
	battle_btn.disabled = false

func _on_battle_completed(winner: String, rewards: String) -> void:
	if winner == "player":
		battle_result.text = "Victory! Gained rewards."
		battle_result.visible = true
	else:
		battle_result.text = "Defeated..."
		battle_result.visible = true

func _on_pvp_completed(winner: String, rating_change: int) -> void:
	if pvp_log_box == null:
		return
	if winner == "player":
		pvp_log_box.text = "Victory! Rating +%d\n" % rating_change
	else:
		pvp_log_box.text = "Defeated... Rating %d\n" % rating_change
	# RichTextLabel has no scroll_to_end(); scroll_to_line() is the real
	# equivalent. Guarded so a line index of -1 can never reach it. Measured
	# on 4.7.2: get_line_count() returns 1 even for an empty document, and
	# scroll_to_line(-1) does not raise, so the guard is defensive, not a
	# fix for an observed crash.
	var line_count: int = pvp_log_box.get_line_count()
	if line_count > 0:
		pvp_log_box.scroll_to_line(line_count - 1)

func _on_matchmaking_ready(candidates: Array) -> void:
	candidates_container.visible = true
	# VBoxContainer has no clear_children(); remove and free each child by hand.
	for child in candidate_list.get_children():
		candidate_list.remove_child(child)
		child.queue_free()
	
	if candidates.size() == 0:
		var empty = Label.new()
		empty.text = "No opponents found in your rating range"
		candidate_list.add_child(empty)
		return
	
	for candidate in candidates:
		var hbox = HBoxContainer.new()
		hbox.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		
		var info_text = "%s (Rating: %d, %s %s)" % [
			candidate.get("name", "Unknown"),
			candidate.get("rating", 1000),
			candidate.get("realm", "mortal"),
			candidate.get("stage", "early"),
		]
		var info_label = Label.new()
		info_label.text = info_text
		info_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		hbox.add_child(info_label)
		
		var diff = candidate.get("rating_diff", 0)
		var diff_text = "%+d" % diff
		var diff_label = Label.new()
		diff_label.text = diff_text
		diff_label.add_theme_color_override("font_color", Color(0.2, 1, 0.2) if diff >= 0 else Color(1, 0.3, 0.3))
		hbox.add_child(diff_label)
		
		var fight_btn = Button.new()
		fight_btn.text = "Fight"
		fight_btn.custom_minimum_size = Vector2(80, 36)
		fight_btn.pressed.connect(func(c=candidate): _start_pvp_match(c))
		hbox.add_child(fight_btn)
		
		candidate_list.add_child(hbox)

func _start_pvp_match(opponent: Dictionary) -> void:
	candidates_container.visible = false
	pvp_log_box.text = "Starting match against %s...\n" % opponent.get("name", "?")
	
	var realm = GameState.player_data.get("cultivation_realm", "mortal")
	var stage = GameState.player_data.get("cultivation_stage", "early")
	var enemy = combat_system.generate_enemy_for_realm(realm, stage)
	# Override enemy stats with opponent's data
	enemy = {
		"realm": opponent.get("realm", "mortal"),
		"stage": opponent.get("stage", "early"),
		"element": ["fire", "water", "earth", "wood", "metal"].pick_random(),
		"hp": enemy.get("hp", 1000),
		"max_hp": enemy.get("hp", 1000),
		"atk": enemy.get("atk", 100),
		"def": enemy.get("def", 50),
	}
	
	var result = await combat_system.perform_pvp_match(
		opponent.get("player_id", ""),
		opponent.get("rating", 1000),
		enemy
	)
	
	# Show replay option
	if result.ok and result.data.get("log"):
		_show_replay(result.data.log, result.data.get("winner", "enemy"), result.data.get("turns", 0))
	else:
		# A refused or logless match used to leave only the "Starting match"
		# line on screen, which reads as a hang. Read the reason from the same
		# result shape APIClient._request returns: "error" carries the server
		# refusal detail (its "detail" field, a transport message, or an
		# invalid-body message) and is blank on a clean 2xx. A clean reply
		# with no log is a different failure, so it gets its own wording.
		var reason = str(result.get("error", "")).strip_edges()
		if reason.is_empty():
			reason = "The server accepted the match but returned no battle log."
		pvp_log_box.text += "Match failed: %s\n" % reason

func _on_ranking_loaded(rank: int, nearby: Array) -> void:
	my_rank_label.text = "Your Rank: %d | Rating: %d" % [rank, GameState.player_data.get("pvp_rating", 1000)]
	for child in leaderboard_list.get_children():
		leaderboard_list.remove_child(child)
		child.queue_free()
	
	if nearby.size() == 0:
		var empty = Label.new()
		empty.text = "No other players ranked yet"
		leaderboard_list.add_child(empty)
		return
	
	for i in range(nearby.size()):
		var opp = nearby[i]
		var hbox = HBoxContainer.new()
		
		var rank_label = Label.new()
		rank_label.text = "#%d" % (i + 1)
		rank_label.custom_minimum_size = Vector2(50, 0)
		hbox.add_child(rank_label)
		
		var name_label = Label.new()
		name_label.text = opp.get("name", "Unknown")
		name_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		hbox.add_child(name_label)
		
		var rating_label = Label.new()
		rating_label.text = "Rating: %d" % opp.get("rating", 1000)
		rating_label.custom_minimum_size = Vector2(120, 0)
		hbox.add_child(rating_label)
		
		var realm_label = Label.new()
		realm_label.text = "%s %s" % [opp.get("realm", "mortal"), opp.get("stage", "early")]
		realm_label.custom_minimum_size = Vector2(150, 0)
		hbox.add_child(realm_label)
		
		var fight_btn = Button.new()
		fight_btn.text = "Fight"
		fight_btn.custom_minimum_size = Vector2(80, 32)
		fight_btn.pressed.connect(func(o=opp): _start_pvp_match(o))
		hbox.add_child(fight_btn)
		
		leaderboard_list.add_child(hbox)

func _on_history_loaded(matches: Array) -> void:
	for child in history_list.get_children():
		history_list.remove_child(child)
		child.queue_free()
	
	if matches.size() == 0:
		var empty = Label.new()
		empty.text = "No matches yet"
		empty.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
		history_list.add_child(empty)
		return
	
	for match in matches:
		var hbox = HBoxContainer.new()
		
		var result_text = "✓" if match.get("winner") == "player" else "✗"
		var result_label = Label.new()
		result_label.text = result_text
		result_label.add_theme_color_override("font_color", Color(0.2, 1, 0.2) if match.get("winner") == "player" else Color(1, 0.3, 0.3))
		result_label.custom_minimum_size = Vector2(40, 0)
		hbox.add_child(result_label)
		
		var opp_label = Label.new()
		opp_label.text = "vs %s" % match.get("opponent_name", "Unknown")
		opp_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		hbox.add_child(opp_label)
		
		var rating_change = match.get("rating_change", 0)
		var rc_text = "%+d" % rating_change
		var rc_label = Label.new()
		rc_label.text = rc_text
		rc_label.add_theme_color_override("font_color", Color(0.2, 1, 0.2) if rating_change >= 0 else Color(1, 0.3, 0.3))
		rc_label.custom_minimum_size = Vector2(80, 0)
		hbox.add_child(rc_label)
		
		var replay_btn = Button.new()
		replay_btn.text = "Replay"
		replay_btn.custom_minimum_size = Vector2(80, 32)
		replay_btn.pressed.connect(func(m=match): _load_replay(m))
		hbox.add_child(replay_btn)
		
		history_list.add_child(hbox)

func _load_replay(match: Dictionary) -> void:
	combat_system.get_pvp_replay(
		match.get("opponent_id", ""),
		match.get("opponent_rating", 1000),
		match.get("enemy_stats", {})
	)

func _on_replay_loaded(log: Array, winner: String, turns: int) -> void:
	_show_replay(log, winner, turns)

func _show_replay(log: Array, winner: String, turns: int) -> void:
	$VBox/ReplayPanel.visible = true
	replay_log_box.clear()
	
	var result_text = "VICTORY" if winner == "player" else "DEFEAT"
	var color = Color(0.2, 1, 0.2) if winner == "player" else Color(1, 0.3, 0.3)
	replay_info.text = "%s in %d turns" % [result_text, turns]
	replay_info.add_theme_color_override("font_color", color)
	replay_info.add_theme_constant_override("font_size", 18)
	
	for entry in log:
		var turn = entry.get("turn", 0)
		var attacker = entry.get("attacker", "")
		var damage = entry.get("damage", 0)
		var is_crit = entry.get("crit", false)
		var crit_text = " CRIT!" if is_crit else ""
		
		if attacker == "player":
			var text = "[color=#33ff33]Turn %d: You dealt %d damage%s[/color]\n" % [turn, damage, crit_text]
			replay_log_box.append_text(text)
		else:
			var text = "[color=#ff3333]Turn %d: Enemy dealt %d damage%s[/color]\n" % [turn, damage, crit_text]
			replay_log_box.append_text(text)
		
		var hp_remaining = entry.get("player_hp_remaining") or entry.get("enemy_hp_remaining", 0)
		if hp_remaining > 0:
			replay_log_box.append_text("  HP remaining: %d\n" % hp_remaining)
	# No scroll_to_end() on RichTextLabel; scroll_following = true on ReplayLogBox
	# already keeps the newest line in view as text is appended.

func _hide_replay() -> void:
	$VBox/ReplayPanel.visible = false

func _on_error(message: String) -> void:
	combat_status.text = "Error: %s" % message
	combat_status.add_theme_color_override("font_color", Color(1, 0.3, 0.3))