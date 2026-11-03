"""The morning summary email: what each person has due and overdue, and what's waiting on admins and team leads.

Each person gets at most one a day, recorded before sending, so a restart can't send it twice.
Nothing is sent on days the office is closed, or to anyone with nothing to report.
"""
import logging

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from . import notifications
from .models import DailySummary, ExtensionRequest, Task, User, is_office_closed

log = logging.getLogger(__name__)


def build_summary(user, today):
    """What this person needs to know today."""
    mine = Task.objects.filter(pk__in=Task.objects.filter(Q(assignee=user) | Q(collaborators=user)).values("pk"))
    mine = mine.filter(status__in=Task.NEEDS_WORK).select_related("assignee", "client").order_by("due_date", "created_at")
    summary = {
        "overdue": list(mine.filter(due_date__lt=today)),
        "today": list(mine.filter(due_date=today)),
        "approvals": [],
        "more_time": 0,
    }
    if user.is_lead:
        managed = Task.objects.all() if user.is_admin else Task.objects.filter(assignee__manager=user)
        waiting = managed.filter(status=Task.Status.REVIEW).exclude(assignee=user).exclude(collaborators=user)
        summary["approvals"] = list(waiting.select_related("assignee", "client").order_by("submitted_at", "due_date"))
        summary["more_time"] = ExtensionRequest.objects.filter(
            status=ExtensionRequest.Status.PENDING, task__in=managed.exclude(assignee=user),
        ).count()
    return summary


def has_news(summary):
    return any(summary[k] for k in ("overdue", "today", "approvals", "more_time"))


def send_morning_summaries(today=None):
    """Send today's summaries. Returns how many went out."""
    today = today or timezone.localdate()
    if is_office_closed(today):
        return 0
    sent = 0
    for user in User.objects.filter(is_active=True).exclude(email=""):
        summary = build_summary(user, today)
        if not has_news(summary):
            continue
        try:
            with transaction.atomic():
                DailySummary.objects.create(user=user, date=today)
        except IntegrityError:
            continue  # already sent today
        notifications.morning_summary(user, summary, today)
        sent += 1
    log.info("Morning summaries for %s: %s sent", today, sent)
    return sent
