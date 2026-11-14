extends GamePanel
## GachaView.gd — Summon screen controller.
## Connects to the live Lottery API (5 per-rarity pools with pity).
## Has a GachaSystem child node for signal-based communication.
##
## -----------------------------------------------------------------------
## MIGRATED ONTO GamePanel -- boilerplate only, no behaviour change.
## -----------------------------------------------------------------------
##   * `extends Control` -> `extends GamePanel`. GamePanel itself extends
##     Control, so the GachaView.tscn root node type is unchanged and the scene
##     still loads without a .tscn edit.
##   * `_ready()` -> `_on_ready()`. Godot calls ONLY the most-derived `_ready()`,
##     so keeping this panel's own would skip GamePanel's setup entirely and the
##     panel would silently render nothing.
##   * this panel's own `var _api: Node` is GONE. Redeclaring a member the base
##     already owns (`_api`) is a FATAL parse error in GDScript, not a shadow
##     warning. GamePanel._resolve_api() -- run by the base before _on_ready() --
##     fills the base's `_api` from the same "/root/APIClient" path this panel
##     looked up by hand, with the same null guard.
##   * `@onready var ... = $VBox/X` -> `label_at("VBox/X")` / `button_at("VBox/X")`,
##     so a missing child is reported by name instead of aborting the panel.
##
## DELIBERATELY NOT CHANGED:
##   * NOT moved onto call_api()/describe(). This panel reads the raw
##     `_api.request_get` / `_api.post` envelopes directly and calls no display
##     helper. call_api() would turn each POST into a post_signed() behind the
##     in-flight guard and change the reply's shape -- a behaviour change, not
##     boilerplate.
##   * NOT tagged with tag_action(). MainHub.gd:1375 lists gacha as preview-only,
##     so tagging these two buttons would flip them from greyed-out to live in
##     the hub. Leaving them untagged preserves today's behaviour.
##   * the player-id read stays on this panel's own `player_data.id` rather than
##     the base's resolve_player_id(), which prefers GameState.player_id. The two
##     can disagree, so swapping them would change which player this panel loads.

var pity_label: Label
var pool_label: Label
var result_label: Label
var single_button: Button
var multi_button: Button
var _gacha_system: Node

var _player_id: String = ""
var _current_pool: String = "common_satchel"


## Renamed from _ready() -- see the note at the top of this file. By the time
## this runs, GamePanel has already resolved `_api` and the player id.
func _on_ready() -> void:
	pity_label = label_at("VBox/PityLabel")
	pool_label = label_at("VBox/PoolBanner")
	result_label = label_at("VBox/ResultLabel")
	single_button = button_at("VBox/ButtonRow/SinglePull")
	multi_button = button_at("VBox/ButtonRow/MultiPull")
	_gacha_system = get_node_or_null("GachaSystem")
	var state: Node = get_node_or_null(GAME_STATE_AUTOLOAD_PATH)
	if state is Node and state.has_method("get"):
		var data: Variant = state.get("player_data")
		if data is Dictionary:
			_player_id = str(data.get("id", ""))
	if single_button != null:
		single_button.pressed.connect(_on_single_pull)
	if multi_button != null:
		multi_button.pressed.connect(_on_multi_pull)
	if pool_label != null:
		pool_label.text = "Standard Banner"
	if not _player_id.is_empty():
		_load_state()

func _load_state() -> void:
	if _api == null or pool_label == null or pity_label == null:
		return
	var response: Dictionary = await _api.request_get("/lottery/" + _player_id + "/state")
	if response.get("ok") and response.has("data") and response.data is Dictionary:
		var pools: Array = response.data.get("pools", [])
		if pools is Array and pools.size() > 0:
			var pool: Variant = pools[0]
			if pool is Dictionary:
				pool_label.text = pool.get("name", "Standard Banner")
				pity_label.text = "Pity: %d/%d" % [
					int(pool.get("pity", 0)),
					int(pool.get("pity_max", 90)),
				]

## One key per INTENDED pull, minted when the button is pressed and reused for
## any retry of that same press. The server REQUIRES this header now
## (server/lottery.py `idempotency_key_for`): it refuses a pull with no
## Idempotency-Key rather than minting a per-attempt uuid, because a per-attempt
## key can never match a retry, so the retry is charged a second time.
##
## Time.get_ticks_msec() is milliseconds since engine start, so two presses
## cannot collide inside one millisecond, and the pool/batch are in the string
## so a key can never name two different purchases. This is the same shape
## GachaSystem.pull() already uses for POST /gacha/pull.
func _mint_pull_key(count: int) -> String:
	return "lottery-pull:%s:%d:%d" % [_player_id, count, Time.get_ticks_msec()]


func _on_single_pull() -> void:
	if _api == null or _player_id.is_empty():
		if result_label != null:
			result_label.text = "No connected session."
		return
	var response: Dictionary = await _api.post_signed("/lottery/" + _player_id + "/pull", {
		"pool": _current_pool, "count": 1,
	}, _mint_pull_key(1))
	_apply_result(response)

func _on_multi_pull() -> void:
	if _api == null or _player_id.is_empty():
		if result_label != null:
			result_label.text = "No connected session."
		return
	var response: Dictionary = await _api.post_signed("/lottery/" + _player_id + "/pull", {
		"pool": _current_pool, "count": 10,
	}, _mint_pull_key(10))
	_apply_result(response)

func _apply_result(response: Dictionary) -> void:
	if result_label == null:
		return
	if not response.get("ok", false):
		result_label.text = "Pull failed: " + str(response.get("error", "unknown"))
		return
	var results: Array = response.get("results", [])
	var lines: PackedStringArray = []
	for r in results:
		if r is Dictionary:
			lines.append(str(r.get("rarity", "?")) + ": " + str(r.get("reward", "?")))
	result_label.text = ", ".join(lines) if lines else "No rewards."
