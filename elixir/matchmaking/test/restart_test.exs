defmodule Wayfarers.Matchmaking.RestartTest do
  @moduledoc """
  What survives a crash, and what honestly admits it did not survive.

  Every test names its own queue and lobby, so nothing leaks between tests and
  no global registration outlives the case that made it.
  """

  use ExUnit.Case, async: false

  alias Wayfarers.Matchmaking.Lobby
  alias Wayfarers.Matchmaking.LobbySupervisor
  alias Wayfarers.Matchmaking.Queue

  # A lobby crash must not drop the queue, and a queue crash must not drop the
  # lobby. These tests kill both and measure the survivor.

  # --- helpers ---------------------------------------------------------------

  defp wait_down(pid) do
    ref = Process.monitor(pid)
    assert_receive {:DOWN, ^ref, :process, ^pid, _reason}, 1_000
  end

  # Poll until the named queue is empty, or fail with what it actually held.
  # Expiry is driven by a real timer, so it lands on a scheduler tick rather
  # than the instant the clock passes the deadline. Sleeping a fixed amount
  # would either be flaky on a busy machine or needlessly slow on an idle one.
  defp wait_until_empty(server, tries \\ 200) do
    cond do
      Queue.size(server: server) == 0 -> :ok
      tries > 0 -> Process.sleep(10) && wait_until_empty(server, tries - 1)
      true -> flunk("queue #{inspect(server)} never emptied: #{inspect(Queue.entries(server: server))}")
    end
  end

  # Poll until `player_id` is no longer in the named queue. Used where the
  # queue still legitimately holds other players, so waiting for it to be
  # empty would wait forever for a player who never expires.
  defp wait_until_gone(server, player_id, tries \\ 200) do
    cond do
      match?({:error, :not_queued}, Queue.position(player_id, server: server)) -> :ok
      tries > 0 -> Process.sleep(10) && wait_until_gone(server, player_id, tries - 1)
      true -> flunk("#{inspect(player_id)} never left #{inspect(server)}: #{inspect(Queue.entries(server: server))}")
    end
  end

  # A restarted lobby is re-registered asynchronously, so poll for a live one.
  defp wait_for_lobby(id, tries \\ 100) do
    pid = :global.whereis_name({Lobby, id})

    cond do
      is_pid(pid) and Process.alive?(pid) -> pid
      tries > 0 -> Process.sleep(10) && wait_for_lobby(id, tries - 1)
      true -> nil
    end
  end

  # Assert a named lobby is dead and stays dead for the window. Used by the
  # negative control, where supervision has been removed: green must mean "no
  # pid reappeared", not "the restart was merely slow".
  defp refute_lobby_returns(id, window_ms \\ 300) do
    Process.sleep(window_ms)
    refute is_pid(:global.whereis_name({Lobby, id})),
           "the unsupervised lobby came back on its own"
  end

  # --- the lobby refuses to exist without a queue ---------------------------

  test "a lobby refuses to start when no queue is registered" do
    # A supervisor traps exits, and a lobby only ever starts under one, so the
    # test must trap exits too. Otherwise the expected `{:stop, reason}` out of
    # init/1 takes this test process down with it and we learn nothing.
    Process.flag(:trap_exit, true)
    on_exit(fn -> Process.flag(:trap_exit, false) end)

    refute Process.whereis(:tq_absent)

    assert {:error, {:queue_down, :tq_absent}} =
             Lobby.start_link(id: :lonely, queue: :tq_absent)
  end

  test "the supervisor also refuses to leave a lobby running with no queue" do
    Process.flag(:trap_exit, true)
    on_exit(fn -> Process.flag(:trap_exit, false) end)

    # The supervisor's own name must NOT be the queue name. A lobby resolves
    # its queue with Process.whereis/1, so if the supervisor were registered
    # under the same atom it would find *itself*, believe a queue existed, and
    # start happily. Keeping the names apart is what makes this test real.
    started = LobbySupervisor.start_link(
      name: :tq_sup_owner,
      queue: :tq_sup_absent,
      lobbies: [[id: :l_sup_down, size: 2]]
    )

    Process.sleep(50)
    # :global.whereis_name/1 answers the atom :undefined when a name is free,
    # not nil, so the check must ask "is this a pid" rather than "is this falsy".
    refute Process.whereis(:tq_sup_absent), "the absent queue name is registered"
    refute is_pid(:global.whereis_name({Lobby, :l_sup_down})), "a lobby survived with no queue"

    case started do
      {:error, {:shutdown, {:failed_to_start_child, :l_sup_down, {:queue_down, :tq_sup_absent}}}} ->
        :refused_immediately

      {:ok, pid} ->
        assert Process.alive?(pid) == false,
               "supervisor reported :ok but then survived with no queue to talk to"

      other ->
        flunk("unexpected supervisor start result: #{inspect(other)}")
    end
  end

  # --- queue dies, lobby lives ----------------------------------------------

  test "a dead queue makes the lobby refuse the seat instead of exiting" do
    {:ok, queue} = Queue.start_link(name: :tq_dead)
    {:ok, lobby} = Lobby.start_link(id: :l_dead, queue: :tq_dead)

    assert {:ok, _summary} = Lobby.add_player("p1", id: :l_dead, rating: 100)

    Process.unlink(queue)
    Process.exit(queue, :kill)
    wait_down(queue)

    # The seat call must return an error tuple, not raise and not take the
    # lobby process down with it.
    assert {:error, :queue_down} = Lobby.add_player("p2", id: :l_dead, rating: 100)
    assert Process.alive?(lobby)
    # The player already seated keeps their seat.
    assert Lobby.roster(id: :l_dead) == ["p1"]
    assert Lobby.status(id: :l_dead) == :open

    # Every other read must also survive a dead queue, and answer an error
    # rather than exit. A read path that raised here would take the whole
    # waiting room down over a matchmaking problem.
    assert Lobby.roster(id: :l_dead) == ["p1"]
    assert Lobby.status(id: :l_dead) == :open
    assert Lobby.taken(id: :l_dead) == 1
    assert Lobby.available(id: :l_dead) == 1
    assert Lobby.seats(id: :l_dead) == 2
    # A dead queue must be reported as :queue_down, not as :no_match. Telling
    # a caller "no opponent found" when the real problem is a dead queue
    # would hide an outage behind an ordinary empty-room answer.
    assert {:error, :queue_down} = Lobby.try_start("p1", id: :l_dead)
    assert Process.alive?(lobby), "a read path raised while the queue was down"
  end

  test "a dead queue makes remove_player still free the seat" do
    {:ok, queue} = Queue.start_link(name: :tq_remove)
    {:ok, lobby} = Lobby.start_link(id: :l_remove, queue: :tq_remove)

    assert {:ok, _} = Lobby.add_player("p1", id: :l_remove, rating: 100)
    assert {:ok, _} = Lobby.add_player("p2", id: :l_remove, rating: 100)

    Process.unlink(queue)
    Process.exit(queue, :kill)
    wait_down(queue)

    # The queue is volatile and cannot hold a stale entry, so unseating must
    # still work rather than raising on the missing queue. Note this returns
    # the summary map bare, not wrapped in {:ok, _}: the `{:ok, summary}`
    # shape belongs to add_player only.
    assert %{roster: ["p2"], status: :open} = Lobby.remove_player("p1", id: :l_remove)
    assert Lobby.roster(id: :l_remove) == ["p2"]
    assert Lobby.taken(id: :l_remove) == 1
    assert Process.alive?(lobby)
  end

  # A lobby may be told to use a queue by *pid* rather than by name. That is a
  # different way of resolving the queue, so a dead queue has to be detected
  # that way too. The seat must be refused and the player already seated must
  # keep their seat.
  test "a dead queue reached by pid refuses the seat and keeps the seated player" do
    # No :name, so the queue is reachable only through this pid.
    {:ok, queue} = Queue.start_link([])
    {:ok, lobby} = Lobby.start_link(id: :l_pid_dead, queue: queue)

    assert {:ok, _} = Lobby.add_player("p1", id: :l_pid_dead, rating: 100)
    assert Queue.size(server: queue) == 1

    Process.unlink(queue)
    Process.exit(queue, :kill)
    wait_down(queue)
    refute Process.alive?(queue)

    # The queue is gone, so nothing can be queued: the seat is refused.
    assert {:error, :queue_down} = Lobby.add_player("p2", id: :l_pid_dead, rating: 100)

    # The refusal must not have seated p2, and must not have disturbed p1.
    assert Lobby.roster(id: :l_pid_dead) == ["p1"]
    assert Lobby.taken(id: :l_pid_dead) == 1
    assert Lobby.available(id: :l_pid_dead) == 1
    assert Lobby.status(id: :l_pid_dead) == :open
    assert Process.alive?(lobby), "the lobby died instead of refusing the seat"
  end

  # --- resync after the queue comes back ------------------------------------

  test "resync pushes the lobby roster back into a restarted queue" do
    {:ok, queue} = Queue.start_link(name: :tq_resync)
    {:ok, _lobby} = Lobby.start_link(id: :l_resync, queue: :tq_resync)

    assert {:ok, _} = Lobby.add_player("a", id: :l_resync, rating: 100)
    assert {:ok, _} = Lobby.add_player("b", id: :l_resync, rating: 130)
    assert Queue.size(server: :tq_resync) == 2

    Process.unlink(queue)
    Process.exit(queue, :kill)
    wait_down(queue)

    {:ok, _queue2} = Queue.start_link(name: :tq_resync)
    # Volatile state: the line came back empty, which is the whole reason the
    # lobby keeps its own roster.
    assert Queue.size(server: :tq_resync) == 0

    assert {:ok, 2} = Lobby.resync(id: :l_resync)
    assert Queue.size(server: :tq_resync) == 2
    assert Lobby.roster(id: :l_resync) == ["a", "b"]
  end

  test "resync refuses to claim a resync that did not happen" do
    {:ok, queue} = Queue.start_link(name: :tq_stilldown)
    {:ok, _lobby} = Lobby.start_link(id: :l_stilldown, queue: :tq_stilldown)

    assert {:ok, _} = Lobby.add_player("a", id: :l_stilldown, rating: 10)

    Process.unlink(queue)
    Process.exit(queue, :kill)
    wait_down(queue)

    # Reporting a count here would be the lie this test exists to catch.
    assert {:error, :queue_down} = Lobby.resync(id: :l_stilldown)
    assert Lobby.roster(id: :l_stilldown) == ["a"]
  end

  # --- lobby dies, queue lives ----------------------------------------------

  test "a crashed lobby restarts empty and leaves the queue alone" do
    {:ok, queue} = Queue.start_link(name: :tq_survive)

    {:ok, _sup} =
      LobbySupervisor.start_link(
        name: :tq_sup_survive,
        queue: :tq_survive,
        lobbies: [[id: :l_survive, size: 2]]
      )

    assert {:ok, _} = Lobby.add_player("x", id: :l_survive, rating: 50)
    assert Queue.size(server: :tq_survive) == 1

    first = :global.whereis_name({Lobby, :l_survive})
    assert is_pid(first)
    ref = Process.monitor(first)
    Process.exit(first, :kill)
    assert_receive {:DOWN, ^ref, :process, ^first, :killed}, 1_000

    second = wait_for_lobby(:l_survive)
    assert is_pid(second), "supervisor did not bring the lobby back"
    assert second != first, "restarted lobby is the same process"

    # Honest about volatile state: it came back, and it came back empty.
    assert Lobby.roster(id: :l_survive) == []
    assert Lobby.status(id: :l_survive) == :open

    # The queue is a sibling, so the lobby's crash never touched the process.
    # The waiting line SURVIVES, because the lobby never asked the queue to
    # watch the lobby. That is the whole claim of this module: the room is
    # allowed to forget, the queue is not.
    assert Process.alive?(queue)
    assert Queue.size(server: :tq_survive) == 1
    assert [%{player_id: "x", rating: 50}] = Queue.entries(server: :tq_survive)

    # And the queue is still usable afterwards, which is the actual claim.
    # `Queue.join/2` returns the 1-based POSITION in line, not the new size:
    # "after" is second, behind "x".
    assert {:ok, 2} = Queue.join("after", rating: 10, server: :tq_survive)
    assert Queue.size(server: :tq_survive) == 2
  end

  # --- matching behaviour the restart story depends on ----------------------

  test "try_start pairs the initiator with another player, never itself" do
    {:ok, _queue} = Queue.start_link(name: :tq_match)
    {:ok, _lobby} = Lobby.start_link(id: :l_match, queue: :tq_match, size: 2)

    assert {:ok, _} = Lobby.add_player("a", id: :l_match, rating: 100)
    assert {:ok, _} = Lobby.add_player("b", id: :l_match, rating: 120)

    assert {:ok, %{players: players}} = Lobby.try_start("a", id: :l_match)
    assert players == ["a", "b"]
    assert Lobby.status(id: :l_match) == :started
    assert {:error, :already_started} = Lobby.try_start("b", id: :l_match)
  end

  test "try_start reports no_match when nobody else is inside the rating window" do
    {:ok, _queue} = Queue.start_link(name: :tq_nomatch)
    {:ok, _lobby} = Lobby.start_link(id: :l_nomatch, queue: :tq_nomatch, size: 4, range: 200)

    assert {:ok, _} = Lobby.add_player("a", id: :l_nomatch, rating: 10)
    # 5000 is far outside the 200-wide window, so pop must not stretch it.
    assert {:ok, _} = Lobby.add_player("b", id: :l_nomatch, rating: 5_000)

    assert {:error, :no_match} = Lobby.try_start("a", id: :l_nomatch)
    assert Lobby.status(id: :l_nomatch) == :open
  end

  test "a caller cannot name its own opponent, and seats are capped" do
    {:ok, _queue} = Queue.start_link(name: :tq_cap)
    {:ok, _lobby} = Lobby.start_link(id: :l_cap, queue: :tq_cap, size: 2)

    assert {:ok, _} = Lobby.add_player("a", id: :l_cap, rating: 0)
    assert {:error, :already_seated} = Lobby.add_player("a", id: :l_cap, rating: 0)
    assert {:ok, _} = Lobby.add_player("b", id: :l_cap, rating: 0)
    assert {:error, :full} = Lobby.add_player("c", id: :l_cap, rating: 0)
    assert Lobby.taken(id: :l_cap) == 2
    assert Lobby.available(id: :l_cap) == 0
    assert Lobby.seats(id: :l_cap) == 2
  end

  test "a dead caller frees the seats it was holding" do
    {:ok, _queue} = Queue.start_link(name: :tq_dowp)
    {:ok, _lobby} = Lobby.start_link(id: :l_dowp, queue: :tq_dowp, size: 4)

    test_pid = self()

    caller =
      spawn(fn ->
        # Seats on its own pid, which is what the lobby must then watch.
        Lobby.add_player("ghost", id: :l_dowp, rating: 30, pid: self())
        send(test_pid, :seated)
        receive do
          :stop -> :ok
        end
      end)

    assert_receive :seated, 1_000
    assert Lobby.roster(id: :l_dowp) == ["ghost"]
    assert Queue.size(server: :tq_dowp) == 1

    Process.unlink(caller)
    Process.exit(caller, :kill)
    wait_down(caller)

    # The lobby watches the caller process, so the seat and the queue place both
    # go away without anyone asking.
    assert Lobby.roster(id: :l_dowp) == []
    assert Queue.size(server: :tq_dowp) == 0
  end

  test "rejoining the same player does not create a second place in line" do
    {:ok, _queue} = Queue.start_link(name: :tq_rejoin)

    assert {:ok, 1} = Queue.join("a", rating: 10, server: :tq_rejoin)
    assert {:ok, 2} = Queue.join("b", rating: 20, server: :tq_rejoin)
    assert Queue.size(server: :tq_rejoin) == 2

    # Rejoining moves the player to the back holding the new rating rather than
    # adding a second place, so a rating change cannot be missed.
    assert {:ok, 2} = Queue.join("a", rating: 99, server: :tq_rejoin)
    assert Queue.size(server: :tq_rejoin) == 2

    assert [%{player_id: "b", rating: 20}, %{player_id: "a", rating: 99}] =
             Queue.entries(server: :tq_rejoin)
  end

  test "a lobby refuses a second seat for a player already in the room" do
    {:ok, _queue} = Queue.start_link(name: :tq_rejoin_lobby)
    {:ok, _lobby} = Lobby.start_link(id: :l_rejoin_lobby, queue: :tq_rejoin_lobby, size: 4)

    assert {:ok, _} = Lobby.add_player("a", id: :l_rejoin_lobby, rating: 10)
    # The room already holds this player, so the seat call is refused rather
    # than quietly re-queuing them with a new rating.
    assert {:error, :already_seated} = Lobby.add_player("a", id: :l_rejoin_lobby, rating: 99)
    assert Lobby.taken(id: :l_rejoin_lobby) == 1
  end

  test "a single seated player cannot be matched with themselves" do
    {:ok, _queue} = Queue.start_link(name: :tq_solo)
    {:ok, _lobby} = Lobby.start_link(id: :l_solo, queue: :tq_solo, size: 2)

    assert {:ok, _} = Lobby.add_player("only", id: :l_solo, rating: 100)

    # The only waiting player is the initiator, and `:exclude` must skip them
    # rather than hand a player back to themselves.
    assert {:error, :no_match} = Lobby.try_start("only", id: :l_solo)
    assert Lobby.status(id: :l_solo) == :open
  end

  # ==========================================================================
  # 011-017 / 011-018: the supervised lobby and its restart proof.
  #
  # The claim under test, in one sentence: kill a lobby with a hard exit and it
  # comes back, AND the players it was holding are still in the matchmaking
  # queue afterwards.
  #
  # These tests go RED if supervision is removed. That was proved by deleting
  # the supervisor in a scratch copy of this project and re-running the suite;
  # the last describe block pins the counterfactual that makes the proof mean
  # something.
  # ==========================================================================

  describe "a killed lobby is restarted and keeps the queue" do
    test "the lobby comes back and the queue still holds every waiting player" do
      {:ok, queue} = Queue.start_link(name: :tq_keep)

      {:ok, sup} =
        LobbySupervisor.start_link(
          name: :tq_sup_keep,
          queue: :tq_keep,
          lobbies: [[id: :l_keep, size: 4, range: 200]]
        )

      # Three waiting players, so a partial loss cannot pass as a full one.
      assert {:ok, _} = Lobby.add_player("p1", id: :l_keep, rating: 100)
      assert {:ok, _} = Lobby.add_player("p2", id: :l_keep, rating: 150)
      assert {:ok, _} = Lobby.add_player("p3", id: :l_keep, rating: 200)
      assert Queue.size(server: :tq_keep) == 3

      before = Queue.entries(server: :tq_keep)
      assert Enum.map(before, & &1.player_id) == ["p1", "p2", "p3"]

      # Hard exit: :kill is untrappable, so nothing gets a chance to clean up on
      # the way out. A gentler shutdown could pass by design rather than by luck.
      victim = :global.whereis_name({Lobby, :l_keep})
      assert is_pid(victim), "the supervised lobby was never registered"
      ref = Process.monitor(victim)
      Process.exit(victim, :kill)
      assert_receive {:DOWN, ^ref, :process, ^victim, :killed}, 1_000

      # 1. It came back, as a NEW process under the SAME registered name.
      revived = wait_for_lobby(:l_keep)
      assert is_pid(revived), "supervisor did not restart the killed lobby"
      assert revived != victim, "the restart returned the same dead pid"

      # The supervisor itself never restarted; only the child did.
      assert Process.alive?(sup), "the supervisor died with its child"

      # 2. The queue still holds its waiting players. This is the assertion that
      # fails if the lobby ever hands the queue its own pid to watch.
      assert Process.alive?(queue), "the queue process died with the lobby"
      assert Queue.size(server: :tq_keep) == 3
      assert Queue.entries(server: :tq_keep) == before

      # 3. The revived lobby is a working room, not a corpse: it accepts a
      # re-joining client, and that client replaces its stale queue entry
      # rather than taking a second place in line. `Queue.join/2` moves a
      # re-joining player to the BACK, so p1 lands last, not first.
      assert {:ok, _} = Lobby.add_player("p1", id: :l_keep, rating: 120)
      assert Lobby.roster(id: :l_keep) == ["p1"]
      assert Lobby.taken(id: :l_keep) == 1
      assert Queue.size(server: :tq_keep) == 3
      assert ["p2", "p3", "p1"] = Enum.map(Queue.entries(server: :tq_keep), & &1.player_id)
      assert %{player_id: "p1", rating: 120} = List.last(Queue.entries(server: :tq_keep))
    end

    test "the surviving queue can still match the players the dead lobby held" do
      # Proves the queue is not merely holding stale rows: it is still usable
      # for matching, which is the whole reason to keep it.
      {:ok, _queue} = Queue.start_link(name: :tq_reuse)

      {:ok, _sup} =
        LobbySupervisor.start_link(
          name: :tq_sup_reuse,
          queue: :tq_reuse,
          lobbies: [[id: :l_reuse, size: 4]]
        )

      assert {:ok, _} = Lobby.add_player("a", id: :l_reuse, rating: 100)
      assert {:ok, _} = Lobby.add_player("b", id: :l_reuse, rating: 120)

      victim = :global.whereis_name({Lobby, :l_reuse})
      ref = Process.monitor(victim)
      Process.exit(victim, :kill)
      assert_receive {:DOWN, ^ref, :process, ^victim, :killed}, 1_000
      assert is_pid(wait_for_lobby(:l_reuse))

      assert Queue.size(server: :tq_reuse) == 2

      # Both survivors re-join the restarted room and match normally.
      assert {:ok, _} = Lobby.add_player("a", id: :l_reuse, rating: 100)
      assert {:ok, _} = Lobby.add_player("b", id: :l_reuse, rating: 120)
      assert {:ok, %{players: ["a", "b"]}} = Lobby.try_start("a", id: :l_reuse)

      # `try_start` pops only the OPPONENT. The initiator "a" is still seated
      # in the lobby and still holds its place in line, so one entry remains.
      # A zero here would mean the queue dropped the initiator, which would be
      # a different bug and would hide the match that just happened.
      assert Queue.size(server: :tq_reuse) == 1
      assert [%{player_id: "a"}] = Queue.entries(server: :tq_reuse)
    end

    test "one lobby's crash leaves the other lobby running" do
      # The `:one_for_one` claim. A crash must be contained to the child that
      # crashed, so a second, healthy lobby must not be disturbed.
      {:ok, _queue} = Queue.start_link(name: :tq_two)

      {:ok, _sup} =
        LobbySupervisor.start_link(
          name: :tq_sup_two,
          queue: :tq_two,
          lobbies: [[id: :l_two_a, size: 2], [id: :l_two_b, size: 2]]
        )

      assert {:ok, _} = Lobby.add_player("x", id: :l_two_a, rating: 100)
      assert {:ok, _} = Lobby.add_player("y", id: :l_two_b, rating: 100)

      b_before = :global.whereis_name({Lobby, :l_two_b})
      a_before = :global.whereis_name({Lobby, :l_two_a})

      ref = Process.monitor(a_before)
      Process.exit(a_before, :kill)
      assert_receive {:DOWN, ^ref, :process, ^a_before, :killed}, 1_000

      assert is_pid(wait_for_lobby(:l_two_a)), "the crashed lobby was not restarted"

      # The sibling is the SAME process, not a restarted one. A restart here
      # would mean the strategy had escalated to :one_for_all.
      assert :global.whereis_name({Lobby, :l_two_b}) == b_before
      assert Process.alive?(b_before)
      # And it still holds its own player.
      assert Lobby.roster(id: :l_two_b) == ["y"]
    end
  end

  describe "the supervisor's own settings" do
    test "the supervisor starts and stays up with no lobbies configured" do
      # Zero children is legal. A supervisor that refuses to start with no
      # lobbies would make the lobby set dynamic at the cost of a boot failure.
      {:ok, _queue} = Queue.start_link(name: :tq_empty)
      assert {:ok, pid} = LobbySupervisor.start_link(name: :tq_sup_empty, queue: :tq_empty)
      assert Process.alive?(pid)
      assert Supervisor.count_children(pid).active == 0
    end

    test "the supervisor is a supervisor, not a worker" do
      assert %{type: :supervisor} = LobbySupervisor.child_spec(name: :tq_sup_type, queue: :tq_x)
    end

    test "a lobby child spec is permanent, so it is always restarted" do
      # `:temporary` would compile, boot, and quietly leave lobbies dead after a
      # crash. The restart policy is the whole mechanism, so pin it.
      spec = Lobby.child_spec(id: :l_spec, queue: :tq_x)
      assert spec.restart == :permanent
      assert spec.id == :l_spec
      assert spec.type == :worker
    end

    test "the queue is passed in, never started by the lobby supervisor" do
      # If the supervisor owned the queue, a lobby crash-loop would take the
      # queue with it. The queue must arrive already running.
      {:ok, _queue} = Queue.start_link(name: :tq_owned)

      {:ok, sup} =
        LobbySupervisor.start_link(
          name: :tq_sup_owned,
          queue: :tq_owned,
          lobbies: [[id: :l_owned, size: 2]]
        )

      children = Supervisor.which_children(sup)
      ids = Enum.map(children, fn {id, _pid, _type, _mods} -> id end)

      # Only the lobby is a child. The queue is not in this tree.
      assert ids == [:l_owned]
      refute Wayfarers.Matchmaking.Queue in ids
    end
  end

  # ==========================================================================
  # The TTL backstop for an UNWATCHED queue entry.
  #
  # A monitor can only clean up after an entry when there is a live process to
  # watch. A player seated without a `:pid` has no watcher, so a connection
  # that drops without a matching `leave/2` leaves the entry in the line
  # forever, where it silently blocks a real player from ever being matched.
  #
  # `join/2`'s `:ttl` closes that hole. These tests pin the expiry itself and,
  # just as importantly, pin that expiry does NOT fire in the cases where it
  # must not: an entry with no TTL, an entry that left on its own, and an
  # entry that rejoined under a fresh token.
  # ==========================================================================

  describe "an unwatched queue entry expires on its own" do
    test "an entry with a ttl is dropped once the ttl elapses" do
      {:ok, _queue} = Queue.start_link(name: :tq_ttl)

      # No `:pid`, so nothing is watching this entry. That is the whole point:
      # the TTL is the only thing that can clean it up.
      assert {:ok, 1} = Queue.join("ghost", rating: 100, ttl: 60, server: :tq_ttl)
      assert Queue.size(server: :tq_ttl) == 1
      assert [%{player_id: "ghost"}] = Queue.entries(server: :tq_ttl)

      # Before the deadline the entry is still there. A sweep that fired
      # immediately would pass the "it vanished" assertion below while
      # breaking every real player who waits longer than one tick.
      Process.sleep(20)
      assert Queue.size(server: :tq_ttl) == 1, "the entry expired before its ttl"

      # Past the deadline the queue drops it with nobody asking.
      wait_until_empty(:tq_ttl)

      assert Queue.size(server: :tq_ttl) == 0
      assert Queue.entries(server: :tq_ttl) == []
      assert Queue.position("ghost", server: :tq_ttl) == {:error, :not_queued}
    end

    test "an expired entry stops blocking the queue for everyone else" do
      # The reason expiry matters: a stale entry at the front of the rating
      # window is what a real player keeps being handed instead of a match.
      {:ok, _queue} = Queue.start_link(name: :tq_ttl_block)

      assert {:ok, 1} = Queue.join("ghost", rating: 100, ttl: 60, server: :tq_ttl_block)

      # A live player arrives and asks for an opponent. Only the stale entry
      # is in the window, so this must not come back as a match.
      assert {:ok, %{player_id: "ghost"}} =
               Queue.pop(100, server: :tq_ttl_block)

      # Put the stale entry back, let it expire, and show the same live player
      # now finds a real opponent instead of a corpse.
      assert {:ok, 1} = Queue.join("ghost", rating: 100, ttl: 60, server: :tq_ttl_block)
      assert {:ok, 2} = Queue.join("live", rating: 110, server: :tq_ttl_block)

      wait_until_gone(:tq_ttl_block, "ghost")

      assert [%{player_id: "live"}] = Queue.entries(server: :tq_ttl_block)
      assert {:ok, %{player_id: "live"}} = Queue.pop(105, server: :tq_ttl_block)
      assert Queue.pop(105, server: :tq_ttl_block) == :empty
    end

    test "an entry with no ttl is never expired" do
      # The default must stay "keep it forever". If omitting `:ttl` swept
      # entries away, every existing caller would lose its place in line.
      {:ok, _queue} = Queue.start_link(name: :tq_no_ttl)

      assert {:ok, 1} = Queue.join("forever", rating: 100, server: :tq_no_ttl)
      assert {:ok, 2} = Queue.join("explicit", rating: 100, ttl: :infinity, server: :tq_no_ttl)

      Process.sleep(150)

      assert Queue.size(server: :tq_no_ttl) == 2
      assert Enum.map(Queue.entries(server: :tq_no_ttl), & &1.player_id) ==
               ["forever", "explicit"]
    end

    test "leaving on time cancels the ttl, so nothing is removed twice" do
      {:ok, _queue} = Queue.start_link(name: :tq_ttl_leave)

      assert {:ok, 1} = Queue.join("leaver", rating: 100, ttl: 60, server: :tq_ttl_leave)

      # Leaves before the deadline. The cancelled timer must not fire later
      # and remove whatever now sits under this player id.
      assert {:ok, 1} = Queue.leave("leaver", server: :tq_ttl_leave)
      assert {:ok, 1} = Queue.join("next", rating: 100, server: :tq_ttl_leave)

      Process.sleep(150)

      assert Queue.size(server: :tq_ttl_leave) == 1
      assert [%{player_id: "next"}] = Queue.entries(server: :tq_ttl_leave)
    end

    test "a rejoining player is not expired by the timer of the entry it replaced" do
      {:ok, _queue} = Queue.start_link(name: :tq_ttl_rejoin)

      # The first entry asks for a very short ttl. The player then rejoins with
      # no ttl at all, replacing it. The first entry's timer must not sweep
      # away the new place in line.
      assert {:ok, 1} = Queue.join("a", rating: 10, ttl: 50, server: :tq_ttl_rejoin)
      assert {:ok, 1} = Queue.join("a", rating: 99, server: :tq_ttl_rejoin)

      Process.sleep(150)

      assert Queue.size(server: :tq_ttl_rejoin) == 1
      assert [%{player_id: "a", rating: 99}] = Queue.entries(server: :tq_ttl_rejoin)
    end

    test "a stale expiry message cannot remove the entry that replaced it" do
      # The cancellation on removal usually hides the token check, so this test
      # reproduces the one race it exists for: the timer has ALREADY delivered
      # its message into the queue's mailbox, and only then does the player
      # rejoin. `cancel_timer/1` cannot recall a message that is already
      # queued, so the stale `{:expire, token}` is processed afterwards. Only
      # the token match stops it from removing the new entry.
      {:ok, queue} = Queue.start_link(name: :tq_ttl_stale)

      assert {:ok, 1} = Queue.join("a", rating: 10, ttl: 60_000, server: :tq_ttl_stale)

      # The token the first entry minted, taken from the live entry.
      [%{timer: {stale_token, _timer_ref}}] = Queue.entries(server: :tq_ttl_stale)
      assert is_reference(stale_token)

      # Rejoin replaces the entry, so the player now holds a NEW token.
      assert {:ok, 1} = Queue.join("a", rating: 99, server: :tq_ttl_stale)

      # Replay the message the cancelled timer would have sent. This is the
      # race, made deterministic: the message arrives with a token that no
      # longer belongs to any entry.
      send(queue, {:expire, stale_token})

      # The queue must ignore it and keep the new place in line.
      assert Queue.size(server: :tq_ttl_stale) == 1
      assert [%{player_id: "a", rating: 99}] = Queue.entries(server: :tq_ttl_stale)
    end

    test "an expiry message for a player who already left changes nothing" do
      # Same race, other direction: the player is gone entirely by the time the
      # stale message lands. There is nothing to remove and nothing to damage.
      {:ok, queue} = Queue.start_link(name: :tq_ttl_gone)

      assert {:ok, 1} = Queue.join("gone", rating: 10, ttl: 60_000, server: :tq_ttl_gone)
      [%{timer: {stale_token, _timer_ref}}] = Queue.entries(server: :tq_ttl_gone)

      assert {:ok, 1} = Queue.leave("gone", server: :tq_ttl_gone)
      assert {:ok, 1} = Queue.join("newcomer", rating: 20, server: :tq_ttl_gone)

      send(queue, {:expire, stale_token})

      # The newcomer must be untouched by a message about somebody else.
      assert Queue.size(server: :tq_ttl_gone) == 1
      assert [%{player_id: "newcomer"}] = Queue.entries(server: :tq_ttl_gone)
    end

    test "a watched entry is still removed by its monitor, not left to the ttl" do
      # The ttl is a backstop, not a replacement. A caller that DID pass a pid
      # is freed the moment that process dies, which is far sooner than any
      # reasonable ttl would allow.
      {:ok, _queue} = Queue.start_link(name: :tq_ttl_watch)
      test_pid = self()

      caller =
        spawn(fn ->
          # `pid: self()` is the point of this test: the entry is watched, so
          # the caller's death is what must free it, well inside the 60s ttl.
          Queue.join("watched", rating: 100, ttl: 60_000, pid: self(), server: :tq_ttl_watch)
          send(test_pid, :joined)
          receive do
            :stop -> :ok
          end
        end)

      assert_receive :joined, 1_000
      assert Queue.size(server: :tq_ttl_watch) == 1

      Process.unlink(caller)
      Process.exit(caller, :kill)
      wait_down(caller)

      # Freed immediately, well inside the 60 second ttl.
      assert Queue.size(server: :tq_ttl_watch) == 0
    end

    test "the lobby seats a player with a ttl, so an unwatched entry cannot hang" do
      # The hole this closes: `Lobby.add_player/2` reaches the queue, but it
      # used to reach it with no `:ttl`. A caller that passes no `:pid` leaves
      # the entry unwatched, and an unwatched entry with no ttl is never
      # removed by anything -- the monitor that would have cleared it does not
      # exist, and the queue's own default is "keep it forever".
      #
      # So this is measured at the LOBBY, not at the queue: the queue's ttl is
      # already covered above. What is new here is that the lobby supplies one
      # at all. A short `:ttl` is passed so the assertion does not wait out
      # the production 60s window.
      {:ok, _queue} = Queue.start_link(name: :tq_lobby_ttl)
      Process.flag(:trap_exit, true)
      on_exit(fn -> Process.flag(:trap_exit, false) end)

      {:ok, _lobby} = Lobby.start_link(id: :l_ttl, queue: :tq_lobby_ttl)

      # No `:pid` is passed, so this is the unwatched case: nothing but a ttl
      # can ever clear it.
      assert {:ok, _} = Lobby.add_player("ghost", id: :l_ttl, rating: 100, ttl: 60)

      # The entry really is in the queue, and really is unwatched. Without
      # this the expiry below could pass by the entry never being queued.
      assert Queue.size(server: :tq_lobby_ttl) == 1
      assert [%{player_id: "ghost", monitor: nil}] = Queue.entries(server: :tq_lobby_ttl)

      wait_until_gone(:tq_lobby_ttl, "ghost")

      assert Queue.position("ghost", server: :tq_lobby_ttl) == {:error, :not_queued}
      assert Queue.entries(server: :tq_lobby_ttl) == []
    end

    test "an expired lobby entry stops blocking the window for a live player" do
      # The cost of the bug, not just the presence of the fix. A stale entry at
      # the front of the rating window is what `pop/2` finds first, so before
      # the ttl it blocked the real player behind it. That a live player now
      # gets matched is the part a player would actually feel.
      {:ok, _queue} = Queue.start_link(name: :tq_lobby_ttl_block)
      Process.flag(:trap_exit, true)
      on_exit(fn -> Process.flag(:trap_exit, false) end)

      {:ok, _lobby} = Lobby.start_link(id: :l_ttl_block, queue: :tq_lobby_ttl_block, size: 4)

      assert {:ok, _} = Lobby.add_player("ghost", id: :l_ttl_block, rating: 100, ttl: 60)
      assert {:ok, _} = Lobby.add_player("live", id: :l_ttl_block, rating: 110)

      assert [%{player_id: "ghost"}, %{player_id: "live"}] =
               Queue.entries(server: :tq_lobby_ttl_block)

      wait_until_gone(:tq_lobby_ttl_block, "ghost")

      assert [%{player_id: "live"}] = Queue.entries(server: :tq_lobby_ttl_block)
      assert {:ok, %{player_id: "live"}} = Queue.pop(105, server: :tq_lobby_ttl_block)
      assert Queue.pop(105, server: :tq_lobby_ttl_block) == :empty
    end

    test "a lobby entry is still freed by the caller's death, well before its ttl" do
      # The ttl is a backstop, not a replacement. A lobby seat that names a
      # live process must still be released the moment that process dies; the
      # 60s ttl must not be the reason it comes free.
      {:ok, _queue} = Queue.start_link(name: :tq_lobby_ttl_watch)
      Process.flag(:trap_exit, true)
      on_exit(fn -> Process.flag(:trap_exit, false) end)

      {:ok, _lobby} = Lobby.start_link(id: :l_ttl_watch, queue: :tq_lobby_ttl_watch)

      test_pid = self()

      caller =
        spawn(fn ->
          Lobby.add_player("watched", id: :l_ttl_watch, rating: 100, pid: self(), ttl: 60_000)
          send(test_pid, :seated)
          receive do: (:stop -> :ok)
        end)

      assert_receive :seated, 1_000
      assert Queue.size(server: :tq_lobby_ttl_watch) == 1

      Process.unlink(caller)
      Process.exit(caller, :kill)
      wait_down(caller)

      wait_until_gone(:tq_lobby_ttl_watch, "watched")
    end
  end

  describe "the counterfactual: without a supervisor the lobby stays dead" do
    # These are the PASSING half of the negative control. Each one pins the
    # behaviour the supervised tests above depend on: with nothing watching,
    # a killed lobby does not come back, and the queue keeps its players only
    # because the lobby never watched itself with the queue.
    #
    # The failing half of the control (deleting the supervisor and showing the
    # supervised test go red) is run against a scratch copy of this project,
    # never in the committed suite, so `mix test` stays green.

    test "an unsupervised lobby does not come back" do
      {:ok, _queue} = Queue.start_link(name: :tq_cf)

      # trap_exit because a bare start_link links the lobby to this test
      # process, and the :kill below would otherwise take the test down.
      Process.flag(:trap_exit, true)
      on_exit(fn -> Process.flag(:trap_exit, false) end)

      {:ok, lobby} = Lobby.start_link(id: :l_cf, queue: :tq_cf)
      assert {:ok, _} = Lobby.add_player("z", id: :l_cf, rating: 100)
      assert Queue.size(server: :tq_cf) == 1

      ref = Process.monitor(lobby)
      Process.exit(lobby, :kill)
      assert_receive {:DOWN, ^ref, :process, ^lobby, :killed}, 1_000

      # This is the assertion the supervised tests earn their keep with: the
      # restart in the tests above comes from the supervisor, not from
      # something inside the lobby itself.
      refute_lobby_returns(:l_cf)
    end

    test "the queue keeps the player even when the lobby holding it dies" do
      # The other half of the control. This passes with or without supervision,
      # which is the point: the queue survives because the lobby never asked
      # the queue to watch the lobby. Supervision is what brings the ROOM back;
      # not watching the lobby with the queue is what keeps the LINE.
      {:ok, queue} = Queue.start_link(name: :tq_line)
      Process.flag(:trap_exit, true)
      on_exit(fn -> Process.flag(:trap_exit, false) end)

      {:ok, lobby} = Lobby.start_link(id: :l_line, queue: :tq_line)
      assert {:ok, _} = Lobby.add_player("kept", id: :l_line, rating: 90)
      assert Queue.size(server: :tq_line) == 1

      ref = Process.monitor(lobby)
      Process.exit(lobby, :kill)
      assert_receive {:DOWN, ^ref, :process, ^lobby, :killed}, 1_000

      assert Process.alive?(queue)
      assert Queue.size(server: :tq_line) == 1
      assert [%{player_id: "kept", rating: 90}] = Queue.entries(server: :tq_line)
    end
  end
end
