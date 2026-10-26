"""Runs the due-date reminders every day at 10:00 AM while the website is running.

Change the time with REMINDER_HOUR in settings.py (24-hour clock, e.g. 9 for 9 AM),
or switch this off with REMINDERS_AUTO = False if you schedule
`python manage.py send_reminders` yourself (Windows Task Scheduler / cron).
"""
import logging
import threading
import time

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

log = logging.getLogger(__name__)
CHECK_EVERY_SECONDS = 300  # look every 5 minutes
_started = False


def start():
    global _started
    if _started or not getattr(settings, "REMINDERS_AUTO", True):
        return
    _started = True
    threading.Thread(target=_loop, name="task-reminders", daemon=True).start()
    log.info("Due-date reminders scheduled daily at %02d:00", getattr(settings, "REMINDER_HOUR", 10))


def _loop():
    from .reminders import send_due_reminders

    last_run = None
    time.sleep(20)  # let the server finish starting
    while True:
        try:
            now = timezone.localtime()
            hour = getattr(settings, "REMINDER_HOUR", 10)
            # Once a day, at or after the reminder hour (if the server was off at 10, it catches up when it starts).
            if now.hour >= hour and last_run != now.date():
                close_old_connections()
                count = send_due_reminders(now.date())
                last_run = now.date()
                log.info("Reminders for %s: %s sent", now.date(), count)
        except Exception:  # noqa: BLE001 - never let the checker die
            log.exception("Reminder check failed; will try again")
        finally:
            close_old_connections()
        time.sleep(CHECK_EVERY_SECONDS)