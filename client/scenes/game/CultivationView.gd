extends GamePanel
## CultivationView.gd — Cultivation screen controller
##
## Migrated onto the GamePanel base (boilerplate only, no behaviour change):
## the base now owns the APIClient lookup, the player-id resolution and the
## status-line plumbing, and this panel keeps every line of its own logic.
##
## The node paths below are resolved through the base's typed helpers
## (label_at / button_at / progress_bar_at) instead of `$VBox/...`, so a path
## that cannot be found is NAMED in the log and recorded in bindings_ok()
## instead of erroring as a bare missing-node. Every use below is guarded, so
## a failed lookup degrades this screen rather than taking it down.
##
## This panel still makes no request of its own: every call goes through
## cultivation_system (CultivationSystem), which owns the server round trip.
## It therefore does not adopt call_api() -- doing so would be a behaviour
## change, not boilerplate.

@onready var realm_label: Label = label_at("VBox/RealmLabel")
@onready var progress_bar: ProgressBar = progress_bar_at("VBox/ProgressBar")
@onready var progress_label: Label = label_at("VBox/ProgressLabel")
@onready var rate_label: Label = label_at("VBox/RateLabel")
@onready var cultivate_button: Button = button_at("VBox/CultivateButton")
@onready var breakthrough_button: Button = button_at("VBox/BreakthroughButton")
@onready var offline_button: Button = button_at("VBox/OfflineButton")
@onready var status_label: Label = label_at("VBox/StatusLabel")

var cultivation_system: CultivationSystem

func _on_ready():
	# The tag MainHub._is_interactive_button() looks for. Assigned, not
	# redeclared: it is a public member of the base class, and a script that
	# redeclares a base name fails to parse. Setting it here (rather than
	# leaving the base's script-name default) keeps the emitted meta name
	# exactly "cultivation_action" -- the name this panel always set -- with
	# the generic "panel_action" added alongside it.
	action_family = "cultivation"

	# The scene's own status line, handed to the base so the base's display
	# helpers write here instead of hunting for a node named "Status" (this
	# scene calls it "StatusLabel").
	bind_status(status_label)

	cultivation_system = CultivationSystem.new()
	add_child(cultivation_system)

	# Opt these three into the interactive-button rule. They are server-backed and
	# fully wired below, but set no meta, so MainHub._style_content was disabling
	# the entire core loop. tag_action() is the base's spelling of this and also
	# applies the generic panel_action tag.
	for b in [cultivate_button, breakthrough_button, offline_button]:
		tag_action(b)

	if cultivate_button:
		cultivate_button.pressed.connect(_on_cultivate_pressed)
	if breakthrough_button:
		breakthrough_button.pressed.connect(_on_breakthrough_pressed)
	if offline_button:
		offline_button.pressed.connect(_on_offline_pressed)

	cultivation_system.progress_updated.connect(_on_progress_updated)
	cultivation_system.breakthrough_attempted.connect(_on_breakthrough_attempted)
	cultivation_system.offline_progress_earned.connect(_on_offline_progress_earned)

	# Setup with player data
	cultivation_system.setup_player(GameState.player_data)
	update_display()

func update_display():
	var data = cultivation_system.get_display_data()
	if realm_label:
		realm_label.text = data.realm + " Realm (" + data.stage + ")"
	if progress_bar:
		progress_bar.value = data.progress
	if progress_label:
		progress_label.text = "%.1f / 100.0" % data.progress
	if rate_label:
		rate_label.text = "Cultivation Rate: %.2f/hour" % data.cultivation_rate
	if breakthrough_button:
		breakthrough_button.visible = data.breakthrough_ready
	if cultivate_button:
		cultivate_button.visible = not data.is_cultivating and not data.breakthrough_ready

	if status_label:
		if data.is_cultivating:
			status_label.text = "Cultivating..."
		elif data.breakthrough_ready:
			status_label.text = "Breakthrough Ready!"
		else:
			status_label.text = ""

func _on_cultivate_pressed():
	cultivation_system.start_cultivation()
	update_display()

func _on_breakthrough_pressed():
	if status_label:
		status_label.text = "Attempting breakthrough..."
	var result = await cultivation_system.attempt_breakthrough()
	if status_label:
		if result.ok and result.success:
			status_label.text = "Breakthrough successful!"
		elif result.ok:
			status_label.text = "Breakthrough failed. Try again."
		else:
			status_label.text = "Error: " + result.get("error", "unknown")
	update_display()

func _on_offline_pressed():
	if status_label:
		status_label.text = "Claiming offline progress..."
	var result = await cultivation_system.claim_offline_progress()
	if status_label:
		if result.ok:
			status_label.text = "Gained %.1f progress!" % result.get("progress_gained", 0.0)
		else:
			# Same key the breakthrough arm above reads: the server's own
			# refusal text, so a time-cap refusal reads differently from an
			# attempts or lifecycle refusal.
			status_label.text = "Failed to claim: " + result.get("error", "unknown")
	update_display()

func _on_progress_updated(progress: float, ready: bool):
	update_display()

func _on_breakthrough_attempted(success: bool, new_realm: String, new_stage: String):
	if success:
		update_display()

func _on_offline_progress_earned(amount: float):
	update_display()
