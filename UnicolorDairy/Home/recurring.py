"""Creates tasks from recurring schedules (e.g. "Peanutji Instagram post every Monday").

Runs once a day from scheduler.py and catches up on days missed while the site was asleep.
Each person gets at most one task per schedule per day, enforced by the database,
so running it twice never makes duplicates.
"""
import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import notifications
from .models import (
    Notification, RecurringTask, Task, TaskActivity, active_admins, find_mentioned_users, next_working_day,
)

log = logging.getLogger(__name__)


CATCH_UP_DAYS = 14


def _days_to_run(rule, today, only_today):
    """Today, plus any scheduled days missed while the site was asleep (up to two weeks back)."""
    if only_today:
        return [today]
    first = max(rule.start_date, timezone.localtime(rule.created_at).date(), today - timedelta(days=CATCH_UP_DAYS))
    if rule.last_run_on:
        first = max(first, rule.last_run_on + timedelta(days=1))
    first = min(first, today)
    return [first + timedelta(days=i) for i in range((today - first).days + 1)]


def _due_date(rule, day, today):
    due = day + timedelta(days=rule.due_in_days)
    if day < today:
        due = max(due, today)  # a task we created late can't be overdue from the moment it appears
    return next_working_day(due)


def _tell_admins(message):
    Notification.objects.bulk_create([
        Notification(recipient=admin, verb=Notification.Verb.REMINDER, message=message[:300])
        for admin in active_admins()
    ])


def create_recurring_tasks(today=None, rules=None, only_today=False):
    """Create the tasks each active schedule owes (or just `rules`). Returns how many were created."""
    today = today or timezone.localdate()
    if rules is None:
        rules = RecurringTask.objects.filter(is_active=True, start_date__lte=today)
    created = 0
    for rule in rules:
        if not rule.is_active:
            continue
        first_check_today = rule.last_run_on != today
        for day in _days_to_run(rule, today, only_today):
            if rule.runs_on(day):
                created += _create_for_day(rule, day, today, tell_admins=first_check_today)
        RecurringTask.objects.filter(pk=rule.pk).update(last_run_on=today)
    return created


def _create_for_day(rule, day, today, tell_admins):
    created = 0
    by = rule.created_by
    people = list(rule.assignees.filter(is_active=True))
    if not people and tell_admins:
        _tell_admins(f"“{rule.title}” wasn't created for {day:%d %b}: nobody active is assigned to this schedule.")
    for person in people:
        try:
            with transaction.atomic():
                task = Task.objects.create(
                    title=rule.title,
                    details=rule.details,
                    client=rule.client,
                    estimated_hours=rule.estimated_hours,
                    priority=rule.priority,
                    due_date=_due_date(rule, day, today),
                    assignee=person,
                    assigned_by=by,
                    recurring=rule,
                    occurrence_date=day,
                )
        except IntegrityError:
            continue  # already created for that day
        note = f"Created automatically · {rule.schedule_label}"
        if day < today:
            note += f" · for {day:%d %b}, which was missed while the site was asleep"
        TaskActivity.objects.create(task=task, actor=by, action=TaskActivity.Action.ASSIGNED, note=note)
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
        log.info("Recurring task %s created for %s (%s)", task.pk, person, day)
    return created
