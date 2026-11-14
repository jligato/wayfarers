class_name CultivationSystem
extends Node
## CultivationSystem.gd — Client-side cultivation controller
## Manages cultivation progress, breakthrough attempts, and offline progress display

signal progress_updated(progress: float, breakthrough_ready: bool)
signal breakthrough_attempted(success: bool, new_realm: String, new_stage: String)
signal offline_progress_earned(amount: float)

@onready var cultivation_rate: float = 2.0  # progress per hour
@onready var current_progress: float = 0.0
@onready var breakthrough_ready: bool = false
@onready var is_cultivating: bool = false
@onready var last_cultivation_time: float = 0.0

var timer: Timer

func _ready():
	timer = Timer.new()
	timer.wait_time = 1.0  # update every second
	timer.timeout.connect(_on_timer_timeout)
	add_child(timer)

func setup_player(data: Dictionary):
	current_progress = data.get("progress", 0.0)
	cultivation_rate = data.get("cultivation_rate", 2.0)
	breakthrough_ready = data.get("breakthrough_ready", false)
	last_cultivation_time = data.get("last_cultivation_time", 0.0)
	_update_display()

func start_cultivation():
	is_cultivating = true
	timer.start()

func stop_cultivation():
	is_cultivating = false
	timer.stop()

func _on_timer_timeout():
	current_progress += cultivation_rate / 3600.0  # per second
	if current_progress >= 100.0:
		current_progress = 100.0
		breakthrough_ready = true
		stop_cultivation()
	_update_display()

func attempt_breakthrough() -> Dictionary:
	if not breakthrough_ready:
		return {"ok": false, "error": "breakthrough not ready"}
	
	# Call server v2 endpoint — returns lifecycle-aware response with return_receipt.
	var result = await APIClient.post("/cultivation/breakthrough/v2", {
		"player_id": GameState.player_id,
		"current_realm": GameState.player_data.get("cultivation_realm", "mortal"),
		"current_stage": GameState.player_data.get("cultivation_stage", "early"),
		"client_version": GameState.wallet_version
	})

	if result.ok and result.data.get("ok", false):
		var new_realm = result.data.get("new_realm", "")
		var new_stage = result.data.get("new_stage", "")
		breakthrough_ready = false
		current_progress = 0.0
		# v2 endpoint includes lifecycle status and return_receipt in the response.
		if result.data.has("lifecycle"):
			GameState.player_data["lifecycle"] = result.data["lifecycle"]
		if result.data.has("return_receipt"):
			GameState.player_data["return_receipt"] = result.data["return_receipt"]
		emit_signal("breakthrough_attempted", true, new_realm, new_stage)
		_update_display()
		return {"ok": true, "success": true, "new_realm": new_realm, "new_stage": new_stage}
	else:
		emit_signal("breakthrough_attempted", false, "", "")
		return {"ok": true, "success": false}

func claim_offline_progress():
	# Use v2 endpoint which is server-timed and lifecycle-aware (rejects device clock skew).
	var result = await APIClient.post("/cultivation/claim-offline/v2", {
		"player_id": GameState.player_id,
		"device_now": Time.get_unix_time_from_system()
	})

	if result.ok:
		var progress_gained = result.data.get("progress_gained", 0.0)
		var new_realm = result.data.get("new_realm", "")
		var new_stage = result.data.get("new_stage", "")
		current_progress += progress_gained
		if current_progress >= 100.0:
			current_progress = 100.0
			breakthrough_ready = true
		# v2 endpoints reconcile lifecycle and include the return_receipt.
		if result.data.has("lifecycle"):
			GameState.player_data["lifecycle"] = result.data["lifecycle"]
		if result.data.has("return_receipt"):
			GameState.player_data["return_receipt"] = result.data["return_receipt"]
		emit_signal("offline_progress_earned", progress_gained)
		_update_display()
		return {"ok": true, "progress_gained": progress_gained, "new_realm": new_realm, "new_stage": new_stage}
	# Carry the server's own refusal text out of this function. APIClient
	# already resolved the server's "detail" (the 72-hour time-cap message,
	# a lifecycle 409, a transport failure) into response["error"]. Dropping
	# it here is what collapsed every distinct cause into one identical
	# status line in CultivationView._on_offline_pressed.
	return {"ok": false, "error": result.get("error", "Claim rejected")}

func _update_display():
	emit_signal("progress_updated", current_progress, breakthrough_ready)

func get_display_data() -> Dictionary:
	return {
		"progress": current_progress,
		"breakthrough_ready": breakthrough_ready,
		"cultivation_rate": cultivation_rate,
		"realm": GameState.player_data.get("cultivation_realm", "mortal"),
		"stage": GameState.player_data.get("cultivation_stage", "early"),
		"is_cultivating": is_cultivating
	}
