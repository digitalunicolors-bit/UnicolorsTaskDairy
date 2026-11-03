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

# Tasks that still need work. Done tasks, tasks on hold and ones waiting for approval don't get reminders.
NEEDS_WORK = list(Task.NEEDS_WORK)


def _reminders_due(today):
    """Every (task, kind, wording) that should have a reminder by today.

    The 7-day reminder has a window, so it still goes out if the site was asleep on the exact day.
    It is only for tasks that already existed 7 days before their deadline.
    """
    base = Task.objects.filter(status__in=NEEDS_WORK, assignee__is_active=True).select_related("assignee", "assigned_by")
    for task in base.filter(due_date__range=(today + timedelta(days=2), today + timedelta(days=7))):
        days = (task.due_date - today).days
        if timezone.localtime(task.created_at).date() <= task.due_date - timedelta(days=7):
            yield task, TaskReminder.Kind.WEEK, f"in {days} days"
    for task in base.filter(due_date=today + timedelta(days=1)):
        yield task, TaskReminder.Kind.DAY, "tomorrow"
    for task in base.filter(due_date=today):
        yield task, TaskReminder.Kind.DUE, "today"


def send_due_reminders(today=None):
    """Send every reminder that is due today. Returns how many were sent."""
    today = today or timezone.localdate()
    sent = 0
    for task, kind, when in _reminders_due(today):
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
            notifications.task_reminder(task, kind, when)
        sent += 1
        log.info("Sent %s reminder for task %s to %s", kind, task.pk, task.assignee)
    return sent


def send_overdue_alerts(today=None):
    """Tell admins about tasks that passed their due date without being submitted.

    Each task is reported once (per due date). This includes tasks of people who have left,
    so their work can't go quiet. Admins see everything; team leads see their own team.
    Returns how many tasks were reported.
    """
    today = today or timezone.localdate()
    tasks = (
        Task.objects.filter(due_date__lt=today, status__in=NEEDS_WORK)
        .select_related("assignee", "assignee__manager", "assigned_by")
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

    Notification.objects.bulk_create([
        Notification(
            recipient=person,
            task=task,
            verb=Notification.Verb.OVERDUE,
            message=f"Overdue: “{task.title}” ({task.assignee.display_name}) was due {task.due_date:%d %b}"[:300],
        )
        for task in newly_overdue
        for person in task.overseers()
    ])
    notifications.overdue_digest(newly_overdue, today)
    log.info("Overdue alert sent for %s task(s)", len(newly_overdue))
    return len(newly_overdue)