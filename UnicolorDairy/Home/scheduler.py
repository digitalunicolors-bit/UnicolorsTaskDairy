"""Daily background jobs while the website is running:

  • RECURRING_HOUR (default 8 AM): create today's recurring tasks
  • REMINDER_HOUR:REMINDER_MINUTE (default 10:00 AM): due-date reminders + overdue alerts to admins

Change the time in settings.py (24-hour clock): REMINDER_HOUR = 16 and REMINDER_MINUTE = 35 means 4:35 PM.
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
CHECK_EVERY_SECONDS = 60  # look every minute
_started = False


def start():
    global _started
    if _started or not getattr(settings, "REMINDERS_AUTO", True):
        return
    _started = True
    threading.Thread(target=_loop, name="task-reminders", daemon=True).start()
    log.info("Due-date reminders scheduled daily at %02d:%02d",
             getattr(settings, "REMINDER_HOUR", 10), getattr(settings, "REMINDER_MINUTE", 0))


def _loop():
    from .recurring import create_recurring_tasks
    from .reminders import send_due_reminders, send_overdue_alerts

    last_run = None
    last_created = None
    time.sleep(10)  # let the server finish starting
    while True:
        try:
            now = timezone.localtime()
            # 1) Recurring tasks – once a day from RECURRING_HOUR (catches up if the server starts later).
            if now.hour >= getattr(settings, "RECURRING_HOUR", 8) and last_created != now.date():
                close_old_connections()
                made = create_recurring_tasks(now.date())
                last_created = now.date()
                log.info("Recurring tasks for %s: %s created", now.date(), made)

            # 2) Reminders + overdue alerts
            run_at = (getattr(settings, "REMINDER_HOUR", 10), getattr(settings, "REMINDER_MINUTE", 0))
            # Once a day, at or after the reminder time (if the server was off then, it catches up when it starts).
            if (now.hour, now.minute) >= run_at and last_run != now.date():
                close_old_connections()
                count = send_due_reminders(now.date())
                overdue = send_overdue_alerts(now.date())
                last_run = now.date()
                log.info("Reminders for %s: %s sent, %s overdue reported", now.date(), count, overdue)
        except Exception:  # noqa: BLE001 - never let the checker die
            log.exception("Reminder check failed; will try again")
        finally:
            close_old_connections()
        time.sleep(CHECK_EVERY_SECONDS)