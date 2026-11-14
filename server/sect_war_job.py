"""
Wayfarer's Cycle — the sect war settlement cron: a 01:00 UTC daemon thread.

WHAT THIS IS
------------
A settlement that runs on the SERVER CLOCK, not on a player's request. The
design's rule is that the job is the only writer of a war's phase: a player can
ASK whether a war is due, and can see what it paid, but cannot cause, accelerate
or influence any of it. This thread is what makes that true.

WHY A THREAD AND NOT A TICK OF THE EVENT SCHEDULER
---------------------------------------------------
`sect_war.run_settlement`'s docstring says it is called at the end of
`EventScheduler.tick()`. That is a real coupling, and it is a bad one for a
MONEY job: `tick()` runs under a blanket `except Exception` that logs and
continues, so a settlement failure would be indistinguishable from an event
state-transition failure, and it would be reported without the day it belongs
to. A separate thread has its own handler and can log the day.

It also has its own lifecycle. The event scheduler is started inside
`main.py`'s lifespan; this one is started and stopped the same way, in its own
`try`/`finally`, so a failure here cannot stop the event clock and vice versa.

THE 60-SECOND TICK IS NOT THE SCHEDULE
--------------------------------------
The thread wakes every 60 seconds and asks `run_settlement` whether the day is
due. It does NOT sleep until 01:00. That distinction is load-bearing: a server
that boots at 01:01 must still settle the day, and a thread that slept until the
next 01:00 would silently skip a whole day — with a clean run, no error, and no
log line. `_is_due` is a catch-up PREDICATE, not a moment, precisely so a server
that was down over the boundary still pays.

WIRING (landed)
---------------
`main.py` is off-limits for whoever writes this module, so it shipped with a
start/stop API and no caller. The wiring landed in commit 780ecb4 and now sits
in `main.py`'s lifespan, beside `TournamentJob`:

    global sect_war_job
    try:
        from sect_war_job import SectWarJob
        sect_war_job = SectWarJob(lambda: players_db, sect_store)
        sect_war_job.start()
    except Exception:
        sect_war_job = None
    ...
    finally:
        if sect_war_job:
            sect_war_job.stop()

Verified by driving the real lifespan: the thread starts named
`sect-war-settlement`, runs, and is joined on shutdown.

The design's "only the job writes a phase" rule is therefore NOT enforced by
the job's existence alone, and this module does not enforce it. The manual
route `POST /sect/war/{war_id}/settle` in `sect.py` gates on identity,
membership in a warring sect, and having PAID a contribution into this war --
but it never consults `sect_war.settle_due`, so a paying member can end an
active war at any hour. That is a separate gap in `sect.py`, not in this file.
"""

from __future__ import annotations

import logging
import threading
import time

import sect_war

logger = logging.getLogger("sect_war_job")

# How often the thread asks whether the day is due. Not the schedule — see the
# module docstring. 60s matches the event scheduler's own cadence.
TICK_SECONDS = 60


class SectWarJob:
    """The 01:00 UTC settlement, on a daemon thread.

    Args:
        players_db_provider: a callable returning the live players_db. Called
            fresh on every tick rather than captured once, so the job follows a
            store that is swapped at runtime instead of holding a dead handle.
        sect_store: the SectStore holding the in-memory wars.
        tick_seconds: wake interval. The due check is what decides when work
            happens; this only decides how quickly that is noticed.
    """

    def __init__(self, players_db_provider, sect_store,
                 tick_seconds: int = TICK_SECONDS):
        self._players_db_provider = players_db_provider
        self._sect_store = sect_store
        self._tick_seconds = max(1, int(tick_seconds))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # The last report, so a status route or a test can read what the job
        # actually did rather than inferring it from a balance.
        self.last_report: dict | None = None

    def start(self) -> None:
        """Start the thread. Idempotent — a second call is a no-op, not a leak."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="sect-war-settlement", daemon=True)
        self._thread.start()
        logger.info("sect war settlement job started (settles at %02d:00 UTC, "
                    "wakes every %ss)",
                    sect_war.SETTLE_HOUR_UTC, self._tick_seconds)

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the thread to finish and wait briefly for it.

        The wait is bounded because the thread may be mid-settlement, and a
        shutdown that blocks on a wallet write is worse than a shutdown that
        lets a daemon thread die — the receipt table is the once-only guard, so
        an interrupted settlement resumes rather than double-paying.
        """
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        logger.info("sect war settlement job stopped")

    def tick(self) -> dict:
        """One pass. Exposed so a test can drive it without a thread.

        Returns `run_settlement`'s report, which carries the day and whether the
        day was due — so a caller can tell "nothing to do" from "ran and paid
        nobody", which are very different and otherwise look the same.
        """
        players_db = self._players_db_provider()
        report = sect_war.run_settlement(players_db, self._sect_store, time.time())
        self.last_report = report
        if report.get("due"):
            if report.get("errors"):
                # The DAY goes in the message. A settlement failure that does
                # not say which day is a settlement failure nobody can act on.
                logger.error("sect war settlement day %s finished with %s "
                             "error(s): %s", report.get("day"),
                             len(report["errors"]), report["errors"])
            else:
                logger.info("sect war settlement day %s: %s war(s) settled",
                            report.get("day"), report.get("settled"))
        return report

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                # A bare except around the WHOLE body, so one broken tick cannot
                # kill the clock. Without it the thread dies silently and every
                # later day goes unsettled with no error anywhere.
                logger.error("sect war settlement tick failed: %s", exc,
                             exc_info=True)
            self._stop_event.wait(self._tick_seconds)