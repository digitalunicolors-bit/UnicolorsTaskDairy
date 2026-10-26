"""Due-date reminders: 7 days before, 1 day before, and on the due date.

Each reminder is recorded in TaskReminder before it is sent, so even if this runs
several times a day (or on several servers) nobody gets the same reminder twice.
"""
import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import notifications
from .models import Notification, Task, TaskReminder, active_admins

log = logging.getLogger(__name__)

# (kind, days before the due date, wording for the bell notification)
SCHEDULE = [
    (TaskReminder.Kind.WEEK, 7, "in 7 days"),
    (TaskReminder.Kind.DAY, 1, "tomorrow"),
    (TaskReminder.Kind.DUE, 0, "today"),
]

# Tasks that still need work. Done tasks and ones waiting for approval don't get reminders.
NEEDS_WORK = [Task.Status.TODO, Task.Status.PROGRESS, Task.Status.CHANGE]


def send_due_reminders(today=None):
    """Send every reminder that is due today. Returns how many were sent."""
    today = today or timezone.localdate()
    sent = 0
    for kind, days, when in SCHEDULE:
        due = today + timedelta(days=days)
        tasks = (
            Task.objects.filter(due_date=due, status__in=NEEDS_WORK, assignee__is_active=True)
            .select_related("assignee", "assigned_by")
        )
        for task in tasks:
            try:
                with transaction.atomic():
                    TaskReminder.objects.create(task=task, kind=kind, due_date=task.due_date)
            except IntegrityError:
                continue  # already reminded
            Notification.objects.create(
                recipient=task.assignee,
                task=task,
                verb=Notification.Verb.REMINDER,
                message=f"Reminder: “{task.title}” is due {when}"[:300],
            )
            if task.assignee.email:
                notifications.task_reminder(task, kind)
            sent += 1
            log.info("Sent %s reminder for task %s to %s", kind, task.pk, task.assignee)
    return sent


def send_overdue_alerts(today=None):
    """Tell admins about tasks that passed their due date without being submitted.

    Each task is reported once (per due date), in one summary email per admin.
    Returns how many tasks were reported.
    """
    today = today or timezone.localdate()
    tasks = (
        Task.objects.filter(due_date__lt=today, status__in=NEEDS_WORK, assignee__is_active=True)
        .select_related("assignee", "assigned_by")
        .order_by("due_date")
    )
    newly_overdue = []
    for task in tasks:
        try:
            with transaction.atomic():
                TaskReminder.objects.create(task=task, kind=TaskReminder.Kind.OVERDUE, due_date=task.due_date)
        except IntegrityError:
            continue  # admins already told about this one
        newly_overdue.append(task)
    if not newly_overdue:
        return 0

    admins = list(active_admins())
    Notification.objects.bulk_create([
        Notification(
            recipient=admin,
            task=task,
            verb=Notification.Verb.OVERDUE,
            message=f"Overdue: “{task.title}” ({task.assignee.display_name}) was due {task.due_date:%d %b}"[:300],
        )
        for task in newly_overdue
        for admin in admins
    ])
    notifications.overdue_digest(newly_overdue, today)
    log.info("Overdue alert sent for %s task(s)", len(newly_overdue))
    return len(newly_overdue)