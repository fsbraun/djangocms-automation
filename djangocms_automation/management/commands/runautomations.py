"""Scheduler entry point for the automation engine.

Run periodically (e.g. every minute)::

    * * * * * cd /path/to/project && python manage.py runautomations

Where a per-minute cron is not available, run it as a long-lived process
instead::

    python manage.py runautomations --loop 60

One tick performs, in order:

1. **Recovery** — actions whose worker died or which ran past their timeout are
   rescheduled or failed, so nothing stays ``RUNNING`` forever, and join points
   whose wake-up was lost to a crash are resumed.
2. **Timers** — due timer triggers fire, with bounded catch-up after an outage.
3. **Revival** — due retries and paused actions are enqueued.
4. **Retention** — optionally, old history is redacted or deleted.

The whole tick is guarded by a database-backed lock, so the command can be
installed on several hosts for availability without firing timers twice or
double-enqueueing a recovered action. The same lock makes a second ``--loop``
process harmless.
"""

import logging
import signal
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from djangocms_automation import engine
from djangocms_automation.instances import AutomationInstance

logger = logging.getLogger(__name__)

# Upper bound on one sleep while waiting for the next tick, so a stop signal
# is acted on within this long even though the sleep itself is not interrupted.
SLEEP_STEP = 0.5


class Command(BaseCommand):
    help = "Recover, fire timers, revive due actions, and apply retention policy."

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-recover",
            action="store_true",
            help="Skip recovery of expired leases and timed-out actions.",
        )
        parser.add_argument(
            "--no-timers",
            action="store_true",
            help="Skip firing due timer triggers.",
        )
        parser.add_argument(
            "--catch-up",
            type=int,
            default=None,
            help="Missed timer occurrences to fire in this tick (default: AUTOMATION_TIMER_CATCHUP).",
        )
        parser.add_argument(
            "--cleanup",
            type=int,
            nargs="?",
            const=30,
            default=None,
            metavar="DAYS",
            help="Delete finished executions older than DAYS (default 30 when given without a value).",
        )
        parser.add_argument(
            "--redact",
            type=int,
            default=None,
            metavar="DAYS",
            help="Strip payloads from finished executions older than DAYS, keeping their metadata.",
        )
        parser.add_argument(
            "--lock-name",
            default="runautomations",
            help="Scheduler lock name; use distinct names to run independent schedulers.",
        )
        parser.add_argument(
            "--lock-seconds",
            type=int,
            default=300,
            help="How long the scheduler lock is held before it is considered abandoned.",
        )
        parser.add_argument(
            "--no-lock",
            action="store_true",
            help="Run without taking the scheduler lock. Only safe for a single scheduler.",
        )

        parser.add_argument(
            "--loop",
            type=float,
            default=None,
            metavar="SECONDS",
            help="Keep running, starting a tick every SECONDS, until SIGTERM or SIGINT. "
            "For hosts that cannot schedule a per-minute cron.",
        )

    def handle(self, *args, **options):
        interval = options["loop"]
        if interval is None:
            self._run_once(**options)
            return
        if interval <= 0:
            raise CommandError("--loop needs a positive number of seconds.")
        self._loop(interval, **options)

    def _loop(self, interval, **options):
        self._stop = False
        previous = self._install_stop_handlers()
        self.stdout.write(f"Scheduler loop started: one tick every {interval:g} second(s).")
        try:
            while not self._stop:
                # Measured from the start of the tick, so a slow tick shortens
                # the wait that follows instead of pushing every later one back.
                next_tick = time.monotonic() + interval
                # A long-lived process outlives its database connections; drop
                # the ones the server closed or that passed CONN_MAX_AGE.
                close_old_connections()
                try:
                    self._run_once(**options)
                except Exception as exc:
                    # One bad tick must not end the scheduler: timers and
                    # retries would silently stop until someone restarted it.
                    logger.exception("Automation tick failed")
                    self.stderr.write(f"Automation tick failed: {exc!r}; continuing.")
                while not self._stop:
                    remaining = next_tick - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(remaining, SLEEP_STEP))
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
        self.stdout.write("Scheduler loop stopped.")

    def _install_stop_handlers(self):
        """Make SIGTERM and SIGINT end the loop after the tick in progress."""

        def request_stop(signum, frame):
            self._stop = True

        previous = {}
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                previous[signum] = signal.signal(signum, request_stop)
            except ValueError:
                # Not the main thread, so signals cannot be handled here; the
                # caller that started the thread is responsible for stopping it.
                pass
        return previous

    def _run_once(self, **options):
        if options["no_lock"]:
            self._tick(**options)
            return
        with engine.scheduler_lock(options["lock_name"], options["lock_seconds"]) as lock:
            if not lock:
                self.stdout.write("Another scheduler holds the lock; skipping this tick.")
                return
            self._tick(**options)

    def _tick(self, **options):
        recovered = reconciled = 0
        if not options["no_recover"]:
            recovered = engine.recover_expired_leases()
            reconciled = engine.reconcile_waiting_joins()
            reconciled += engine.reconcile_stalled_instances()
        fired = 0 if options["no_timers"] else engine.fire_due_timers(catch_up=options["catch_up"])
        revived = engine.revive_pending()

        parts = [
            f"recovered {recovered} action(s)",
            f"reconciled {reconciled} join(s)",
            f"fired {fired} timer trigger(s)",
            f"revived {revived} action(s)",
        ]

        if options["redact"] is not None:
            redacted = AutomationInstance.redact_payloads(days=options["redact"])
            parts.append(f"redacted {redacted} execution(s)")

        if options["cleanup"] is not None:
            deleted, _details = AutomationInstance.delete_history(days=options["cleanup"])
            parts.append(f"deleted {deleted} row(s)")

        self.stdout.write(self.style.SUCCESS("Automation tick: " + ", ".join(parts) + "."))
