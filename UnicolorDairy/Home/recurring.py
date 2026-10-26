"""Creates tasks from recurring schedules (e.g. "Peanutji Instagram post every Monday").

Runs once a day from scheduler.py. Each person gets at most one task per schedule per day,
enforced by the database, so running it twice never makes duplicates.
"""
import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import notifications
from .models import Notification, RecurringTask, Task, TaskActivity, find_mentioned_users

log = logging.getLogger(__name__)


def create_recurring_tasks(today=None, rules=None):
    """Create today's tasks for every active schedule (or just `rules`). Returns how many were created."""
    today = today or timezone.localdate()
    if rules is None:
        rules = RecurringTask.objects.filter(is_active=True, start_date__lte=today)
    created = 0
    for rule in rules:
        if not rule.is_active or not rule.runs_on(today):
            continue
        by = rule.created_by
        for person in rule.assignees.filter(is_active=True):
            try:
                with transaction.atomic():
                    task = Task.objects.create(
                        title=rule.title,
                        details=rule.details,
                        priority=rule.priority,
                        due_date=today + timedelta(days=rule.due_in_days),
                        assignee=person,
                        assigned_by=by,
                        recurring=rule,
                        occurrence_date=today,
                    )
            except IntegrityError:
                continue  # already created today
            TaskActivity.objects.create(
                task=task, actor=by, action=TaskActivity.Action.ASSIGNED,
                note=f"Created automatically · {rule.schedule_label}",
            )
            if by:
                Notification.send(task, by, "assigned", recipients=[person])
            else:
                Notification.objects.create(
                    recipient=person, task=task, verb=Notification.Verb.ASSIGNED,
                    message=f"New recurring task “{task.title}”"[:300],
                )
            tagged = [u for u in find_mentioned_users(task.details) if by is None or u.pk not in (person.pk, by.pk)]
            if tagged:
                task.watchers.add(*tagged)
                if by:
                    Notification.send(task, by, "mentioned", recipients=tagged)
            notifications.task_assigned(task, by)
            created += 1
            log.info("Recurring task %s created for %s", task.pk, person)
        RecurringTask.objects.filter(pk=rule.pk).update(last_run_on=today)
    return created