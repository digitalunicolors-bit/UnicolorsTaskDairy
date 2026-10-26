"""Due-date reminders: 7 days before, 1 day before, and on the due date.

Each reminder is recorded in TaskReminder before it is sent, so even if this runs
several times a day (or on several servers) nobody gets the same reminder twice.
"""
import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import notifications
from .models import Notification, Task, TaskReminder

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