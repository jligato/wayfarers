defmodule Wayfarers.Matchmaking.Lobby do
  @moduledoc """
  One lobby: a waiting room with a roster and a fixed number of seats.

  WHAT THIS IS
  ------------
  A `GenServer` for a single lobby. It answers three questions from its own
  state rather than by re-reading the queue every time:

    * who is sitting in this waiting room
    * is the room full yet
    * is the room in a state where a new match may be started

  A lobby keeps its own roster because `Wayfarers.Matchmaking.Queue` holds
  *volatile* state. If the queue dies and restarts, the waiting line comes back
  empty, and the lobby is the only thing left that still knows a player was
  waiting. That is what makes `resync/1` possible.

  THE CALLER PICKS NOBODY
  -----------------------
  `add_player/3` takes a rating, never an opponent. Matching is resolved by
  `Wayfarers.Matchmaking.Queue.pop/2`, which walks the line in arrival order
  and refuses to hand back the caller. There is deliberately no
  "add this player with that opponent" path, because a caller that can name
  its own opponent is a caller that can forge one.

  OPERATIONS
  ----------
      start_link(opts)               start one lobby
      add_player(player_id, opts)    seat a player and queue them
      remove_player(player_id)       unseat, and take them out of the queue
      roster()                       who is here, in seating order
      status()                       :open, :full, or :started
      seats() / taken() / available()
      try_start(player_id)           attempt to start a match in this lobby
      resync()                       re-push waiting players into the queue

  OPTIONS
  -------
    * `:id`    - required, the lobby name; becomes the process name
    * `:mode`  - a label for the game mode, passed through untouched
    * `:size`  - seats in the room, default `2`
    * `:queue` - queue name or pid to use, default `Wayfarers.Matchmaking.Queue`
    * `:range` - rating window to ask the queue for, default `200`
    * `:ttl`   - milliseconds an unwatched queue entry may wait before the
      queue drops it, default `60_000`. Without it an entry nobody is watching
      sits in the line forever and blocks every real player behind it.
    * `:pid`   - the caller's live process, watched by this lobby

  A QUEUE THAT IS DOWN MUST NOT TAKE THIS LOBBY WITH IT
  ------------------------------------------------------
  Every call into the queue is wrapped, and a dead queue produces
  `{:error, :queue_down}` instead of an exit. A lobby is a room players sit
  in; losing the queue is a matchmaking problem, not a reason to evict
  everyone waiting. The seat stands, and `resync/1` pushes the player back in
  once the queue is back.

  WHAT HAPPENS ON A CRASH
  -----------------------
  This process holds volatile state. A restarted lobby comes back with an
  empty roster, because inventing a roster would be worse than admitting
  there is none. Clients re-join.

  The *queue* is the part that must not be lost, and it is not: the lobby never
  asks the queue to watch the lobby. It passes the caller's own process, or
  none at all, so a lobby dying cannot empty the waiting line. The lobby comes
  back empty and the queue still holds the players who were waiting for it.

  That leaves a queue entry whose lobby no longer knows the player. This is
  safe because `Queue.join/2` is idempotent per player: a re-joining client
  replaces its old entry rather than taking a second place in line.

  The lobby does not watch the queue and the queue does not watch the lobby.
  They sit side by side in `Wayfarers.Matchmaking.LobbySupervisor`, so one
  dying does not restart the other.
  """

  use GenServer

  require Logger

  alias Wayfarers.Matchmaking.Queue

  @default_size 2
  @default_range 200

  # How long an UNWATCHED queue entry may sit in the line before the queue
  # drops it. Taken from `QUEUE_WINDOW_SECONDS` (60.0) in
  # `server/pvp_routes.py:206`, the window this service mirrors and the only
  # queue-lifetime constant the repo already carries. Reusing it keeps the
  # Elixir queue and the Python arena giving a player the same amount of
  # patience instead of two numbers that drift apart.
  #
  # This is a BACKSTOP, not a turn timer. A caller that passes a `:pid` is
  # removed by its monitor far sooner; this only matters for a caller that
  # seats a player and hands over no process to watch, where the entry would
  # otherwise sit at the front of the rating window forever and block every
  # real player behind it.
  @queue_entry_ttl_ms 60_000

  # ---------------------------------------------------------------------------
  # Client API
  # ---------------------------------------------------------------------------

  @doc """
  Start one lobby. `:id` is required and becomes the registered name.
  """
  def start_link(opts \\ []) do
    {id, opts} = Keyword.pop(opts, :id)
    GenServer.start_link(__MODULE__, {id, opts}, name: via(id))
  end

  @doc """
  Supervisor child spec for one lobby.

  `restart: :permanent` is deliberate: a lobby is a room players are waiting
  in, so a crashed lobby must come back. The supervisor's `:one_for_one`
  strategy is what stops one lobby's restart loop from disturbing the others.
  """
  def child_spec(opts) do
    %{
      id: Keyword.fetch!(opts, :id),
      start: {__MODULE__, :start_link, [opts]},
      restart: :permanent,
      shutdown: 5_000,
      type: :worker
    }
  end

  @doc """
  The registered name for a lobby id, so a caller can address one directly.
  """
  def via(nil), do: __MODULE__
  def via(id), do: {:global, {__MODULE__, id}}

  @doc """
  Seat `player_id` and put them in the matchmaking queue.

  Options:

    * `:rating` - rating to match on, default `0`
    * `:pid`    - the caller's live process; its death frees the seat

  Returns `{:ok, summary}` where summary is `%{roster: list, status: atom}`, or
  one of `{:error, :full}`, `{:error, :already_seated}`,
  `{:error, :already_started}`, `{:error, :queue_down}`.

  A queue that is down refuses the seat rather than accepting one the lobby
  cannot honour.
  """
  def add_player(player_id, opts \\ []) do
    GenServer.call(via(Keyword.get(opts, :id)), {:add_player, player_id, opts})
  end

  @doc """
  Unseat `player_id` and take them out of the matchmaking queue.
  """
  def remove_player(player_id, opts \\ []) do
    GenServer.call(via(Keyword.get(opts, :id)), {:remove_player, player_id})
  end

  @doc "Who is here, in seating order."
  def roster(opts \\ []) do
    GenServer.call(via(Keyword.get(opts, :id)), :roster)
  end

  @doc "The room state: `:open`, `:full`, or `:started`."
  def status(opts \\ []) do
    GenServer.call(via(Keyword.get(opts, :id)), :status)
  end

  @doc "How many seats the room has."
  def seats(opts \\ []) do
    GenServer.call(via(Keyword.get(opts, :id)), :seats)
  end

  @doc "How many seats are taken."
  def taken(opts \\ []) do
    GenServer.call(via(Keyword.get(opts, :id)), :taken)
  end

  @doc "How many seats are free."
  def available(opts \\ []) do
    GenServer.call(via(Keyword.get(opts, :id)), :available)
  end

  @doc """
  Try to start a match with `player_id` as the initiator.

  The lobby asks the queue for an opponent inside the rating window and
  refuses to start a match that would pair the initiator with themselves.

  Returns `{:ok, %{players: [player_id, opponent_id]}}`, or `{:error, reason}`
  where reason is one of `:not_seated`, `:already_started`, `:queue_down`,
  or `:no_match`.
  """
  def try_start(player_id, opts \\ []) do
    GenServer.call(via(Keyword.get(opts, :id)), {:try_start, player_id})
  end

  @doc """
  Re-push every waiting player into the matchmaking queue.

  Use after the queue restarted: the lobby survived and still knows who was
  waiting, but the queue's volatile line came back empty. Returns the number
  of players pushed back in.

  Returns `{:error, :queue_down}` when the queue is still not reachable, rather
  than claiming a resync that did not happen.
  """
  def resync(opts \\ []) do
    GenServer.call(via(Keyword.get(opts, :id)), :resync)
  end

  # ---------------------------------------------------------------------------
  # Server callbacks
  # ---------------------------------------------------------------------------

  @impl true
  def init({id, opts}) do
    state = %{
      id: id,
      mode: Keyword.get(opts, :mode, :default),
      size: Keyword.get(opts, :size, @default_size),
      queue: Keyword.get(opts, :queue, Queue),
      range: Keyword.get(opts, :range, @default_range),
      ttl: Keyword.get(opts, :ttl, @queue_entry_ttl_ms),
      players: %{},
      order: [],
      status: :open,
      matches: [],
      monitors: %{}
    }

    if queue_pid(state) do
      {:ok, state}
    else
      # Refuse to start a lobby with no queue to talk to. Starting anyway
      # would hand every caller a lobby that can never match anyone.
      {:stop, {:queue_down, state.queue}}
    end
  end

  @impl true
  def handle_call({:add_player, player_id, opts}, _from, state) do
    cond do
      state.status == :started ->
        {:reply, {:error, :already_started}, state}

      Map.has_key?(state.players, player_id) ->
        {:reply, {:error, :already_seated}, state}

      map_size(state.players) >= state.size ->
        {:reply, {:error, :full}, state}

      true ->
        seat(state, player_id, opts)
    end
  end

  def handle_call({:remove_player, player_id}, _from, state) do
    state = state |> leave_queue(player_id) |> unseat(player_id)
    {:reply, summary(state), state}
  end

  def handle_call(:roster, _from, state) do
    {:reply, state.order, state}
  end

  def handle_call(:status, _from, state) do
    {:reply, state.status, state}
  end

  def handle_call(:seats, _from, state) do
    {:reply, state.size, state}
  end

  def handle_call(:taken, _from, state) do
    {:reply, map_size(state.players), state}
  end

  def handle_call(:available, _from, state) do
    {:reply, state.size - map_size(state.players), state}
  end

  def handle_call({:try_start, player_id}, _from, state) do
    cond do
      state.status == :started ->
        {:reply, {:error, :already_started}, state}

      not Map.has_key?(state.players, player_id) ->
        {:reply, {:error, :not_seated}, state}

      true ->
        case find_opponent(state, player_id) do
          {:ok, opponent} ->
            Logger.info(
              "Lobby #{inspect(state.id)} started #{inspect(state.mode)} with " <>
                "#{inspect(player_id)} and #{inspect(opponent.player_id)}"
            )

            players = [player_id, opponent.player_id]
            {:reply, {:ok, %{players: players}}, %{state | status: :started, matches: players}}

          {:error, reason} ->
            {:reply, {:error, reason}, state}
        end
    end
  end

  def handle_call(:resync, _from, state) do
    if queue_pid(state) do
      pushed =
        Enum.count(state.order, fn player_id ->
          case Map.fetch(state.players, player_id) do
            {:ok, %{rating: rating}} ->
              enqueue(state, player_id, rating)
              true

            :error ->
              false
          end
        end)

      {:reply, {:ok, pushed}, state}
    else
      {:reply, {:error, :queue_down}, state}
    end
  end

  @impl true
  def handle_cast(message, state) do
    Logger.warning("Matchmaking.Lobby #{inspect(state.id)} got an unexpected cast: #{inspect(message)}")
    {:noreply, state}
  end

  @impl true
  def handle_info({:DOWN, ref, :process, _pid, _reason}, state) do
    # The lobby watches each caller that seats a player. When that process
    # dies the seats it holds are freed and those players leave the queue, so a
    # disconnected player never keeps waiting. Matching is on the pid, not the
    # player id, because one caller may be handling requests for several
    # players.
    state =
      case pid_for_ref(state.monitors, ref) do
        nil ->
          state

        dead_pid ->
          state
          |> leave_all_queue(dead_pid)
          |> unseat_pid(dead_pid)
          |> drop_monitor(dead_pid)
      end

    {:noreply, state}
  end

  def handle_info(message, state) do
    Logger.warning("Matchmaking.Lobby #{inspect(state.id)} got an unexpected message: #{inspect(message)}")
    {:noreply, state}
  end

  @impl true
  def terminate(_reason, state) do
    Logger.info(
      "Matchmaking.Lobby #{inspect(state.id)} stopping with #{map_size(state.players)} seated"
    )

    :ok
  end

  # ---------------------------------------------------------------------------
  # Pure helpers
  # ---------------------------------------------------------------------------

  @doc """
  The room's state word. `:started` beats `:full` beats `:open`.
  """
  def room_status(%{status: :started}), do: :started

  def room_status(%{size: size, players: players}) when map_size(players) >= size,
    do: :full

  def room_status(_state), do: :open

  defp summary(state), do: %{roster: state.order, status: state.status}

  # --- seating ---------------------------------------------------------------

  # Seat one player and queue them. The queue call is allowed to fail loudly
  # on the queue being down; that refusal is the whole point, because a seat
  # the lobby cannot honour is worse than no seat.
  defp seat(state, player_id, opts) do
    rating = Keyword.get(opts, :rating, 0)
    pid = Keyword.get(opts, :pid)

    case enqueue(state, player_id, rating, pid, opts) do
      {:error, :queue_down} ->
        {:reply, {:error, :queue_down}, state}

      _queued ->
        state = %{
          state
          | players: Map.put(state.players, player_id, %{rating: rating, pid: pid}),
            order: state.order ++ [player_id],
            status: room_status(%{state | players: Map.put(state.players, player_id, %{rating: rating, pid: pid})})
        }

        state = if pid, do: watch(state, pid), else: state

        {:reply, {:ok, summary(state)}, state}
    end
  end

  defp unseat(state, player_id) do
    players = Map.delete(state.players, player_id)

    %{
      state
      | players: players,
        order: List.delete(state.order, player_id),
        status: room_status(%{state | players: players})
    }
  end

  # Free every seat held by one caller process.
  defp unseat_pid(state, pid) do
    Enum.reduce(state.order, state, fn player_id, acc ->
      case Map.fetch(acc.players, player_id) do
        {:ok, %{pid: ^pid}} -> unseat(acc, player_id)
        _otherwise -> acc
      end
    end)
  end

  defp pid_for_ref(monitors, ref) do
    Enum.find_value(monitors, fn {pid, watched} -> if watched == ref, do: pid end)
  end

  defp drop_monitor(state, pid) do
    %{state | monitors: Map.delete(state.monitors, pid)}
  end

  # Watching a caller a second time must not orphan the first monitor: an
  # orphaned reference never matches in the :DOWN handler, so the seats it
  # guards would never be freed. Drop the old reference first.
  defp watch(state, pid) when is_pid(pid) do
    state = drop_monitor(state, pid)
    %{state | monitors: Map.put(state.monitors, pid, Process.monitor(pid))}
  end

  # A `:pid` option that is not a live process cannot be watched. Ignore it
  # rather than raising: a caller that names a process it does not hold is a
  # caller mistake, not a reason to take the whole waiting room down.
  defp watch(state, _not_a_pid), do: state

  # --- talking to the queue --------------------------------------------------

  # Resolve the configured queue to a live pid. `nil` means "no queue right
  # now", which every caller below turns into an error tuple rather than an
  # exit, so a lobby outlives the queue it was told to use.
  #
  # Only a plain atom or a pid is accepted. Anything else, including an
  # unregistered name, resolves to `nil` instead of raising inside a callback.
  defp queue_pid(%{queue: queue}) when is_pid(queue) do
    if Process.alive?(queue), do: queue
  end

  defp queue_pid(%{queue: queue}) when is_atom(queue) do
    Process.whereis(queue)
  end

  defp queue_pid(_state), do: nil

  defp enqueue(state, player_id, rating, pid \\ nil, opts \\ []) do
    case queue_pid(state) do
      nil ->
        {:error, :queue_down}

      server ->
        # The watched process is the *caller*, never this lobby.
        #
        # Handing the queue `self()` here would make the lobby's own death drop
        # every player it seated, so a supervised restart would come back to an
        # empty waiting line: the room would return and the queue it exists to
        # feed would have forgotten it. That is the failure this lobby is
        # supervised to prevent.
        #
        # Only a caller-supplied pid is passed on. When the caller gave none,
        # the entry is simply unwatched: a player stays in line until the player
        # asks to leave, until the queue pops them for a match, or until a
        # caller that *was* watched dies. See `seat/3`.
        #
        # An unwatched entry is also the one case with nothing else to clear it,
        # so it always carries a `:ttl`. Without this line the lobby seats a
        # player who can never be swept, and `pop/2` keeps finding that stale
        # entry at the front of the rating window, blocking every real player
        # behind it for as long as the process lives.
        Queue.join(player_id,
          rating: rating,
          range: state.range,
          pid: pid,
          ttl: Keyword.get(opts, :ttl, state.ttl),
          server: server
        )
    end
  end

  defp leave_queue(state, player_id) do
    case queue_pid(state) do
      nil ->
        # The queue is gone, so there is nothing to remove this player from.
        # The seat is still freed: the queue is volatile and cannot be holding
        # a stale entry for a process that no longer exists.
        state

      server ->
        Queue.leave(player_id, server: server)
        state
    end
  end

  defp leave_all_queue(state, pid) do
    Enum.reduce(state.order, state, fn player_id, acc ->
      case Map.fetch(acc.players, player_id) do
        {:ok, %{pid: ^pid}} -> leave_queue(acc, player_id)
        _otherwise -> acc
      end
    end)
  end

  defp find_opponent(state, player_id) do
    rating = Map.get(state.players, player_id, %{rating: 0}).rating

    case queue_pid(state) do
      nil ->
        {:error, :queue_down}

      server ->
        case Queue.pop(rating, range: state.range, exclude: player_id, server: server) do
          {:ok, opponent} -> {:ok, opponent}
          :empty -> {:error, :no_match}
        end
    end
  end
end

defmodule Wayfarers.Matchmaking.LobbySupervisor do
  @moduledoc """
  Supervises the lobbies, with the matchmaking queue as their sibling.

  WHY THIS SHAPE
  --------------
  The requirement is that a lobby crash must not drop the queue. Two decisions
  make that true, and both are about where the queue sits:

    * The queue is started *beside* the lobbies, in the caller's own
      supervision tree, not underneath this supervisor. A lobby restarting in
      a loop therefore cannot touch it.
    * The strategy is `:one_for_one`, so a lobby that crashes and restarts is
      restarted on its own, leaving every other lobby alone. `:one_for_all`
      would take the whole room down with the worst one; `:rest_for_one` would
      not restart the failed lobby at all.

  Lobbies are `:permanent` children. A lobby is a room players are waiting in,
  so it must come back. Its state is volatile, so it comes back empty and
  clients re-join; that is documented in `Wayfarers.Matchmaking.Lobby` rather
  than papered over here.

  One restart budget per child is the OTP default (3 restarts in 5 seconds).
  A lobby that crash-loops past that budget takes this supervisor down with
  it. That is intended: a room that cannot hold a process is broken, and the
  queue, being a sibling, is untouched. Raise the budget only for a lobby
  that has a proven reason to fail this often.

  STARTING IT
  -----------
  The queue is passed in as an already-running name or pid, so this module
  never starts one and never becomes the thing that owns it:

      children = [
        Wayfarers.Matchmaking.LobbySupervisor.start_link(
          name: Wayfarers.Matchmaking.LobbySupervisor,
          queue: Wayfarers.Matchmaking.Queue,
          lobbies: [%{id: :ranked, size: 2}, %{id: :casual, size: 4}]
        )
      ]
  """

  use Supervisor

  alias Wayfarers.Matchmaking.Lobby

  @doc """
  Start the lobby supervisor.

  Options:

    * `:name`    - registered name, default this module
    * `:queue`   - running queue name or pid, default `Wayfarers.Matchmaking.Queue`
    * `:lobbies` - list of lobby option lists, each needing an `:id`
    * `:restarts`/`:seconds` - restart budget, default `3` in `5`
  """
  def start_link(opts \\ []) do
    {name, opts} = Keyword.pop(opts, :name, __MODULE__)
    Supervisor.start_link(__MODULE__, opts, name: name)
  end

  @doc """
  Child spec for the supervisor itself.
  """
  def child_spec(opts) do
    %{
      id: Keyword.get(opts, :name, __MODULE__),
      start: {__MODULE__, :start_link, [opts]},
      type: :supervisor
    }
  end

  @impl true
  def init(opts) do
    queue = Keyword.get(opts, :queue, Wayfarers.Matchmaking.Queue)
    lobbies = Keyword.get(opts, :lobbies, [])

    children =
      Enum.map(lobbies, fn lobby_opts ->
        lobby_opts
        |> Keyword.put_new(:queue, queue)
        |> Lobby.child_spec()
      end)

    Supervisor.init(children,
      strategy: :one_for_one,
      max_restarts: Keyword.get(opts, :restarts, 3),
      max_seconds: Keyword.get(opts, :seconds, 5)
    )
  end
end
