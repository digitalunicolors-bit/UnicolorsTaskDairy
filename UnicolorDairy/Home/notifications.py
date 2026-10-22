"""Email notifications. Sending never blocks or breaks the app: failures are logged and skipped."""
import logging
import threading

from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse

from .models import User

log = logging.getLogger(__name__)


def _url(name):
    return getattr(settings, "SITE_URL", "http://127.0.0.1:8000").rstrip("/") + reverse(name)


def _send(subject, body, recipients):
    recipients = sorted({r for r in recipients if r})
    if not recipients or not getattr(settings, "EMAIL_NOTIFICATIONS", True):
        return

    def run():
        try:
            send_mail(f"[Unicolors Tasks] {subject}", body, None, recipients)
        except Exception:  # noqa: BLE001 - email problems must never break the app
            log.exception("Could not send email to %s", recipients)

    if getattr(settings, "EMAIL_SEND_IN_BACKGROUND", True):
        threading.Thread(target=run, daemon=True).start()
    else:
        run()


def _admin_emails(exclude=None):
    qs = User.objects.filter(is_active=True, role=User.Role.ADMIN).exclude(email="")
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    return list(qs.values_list("email", flat=True))


def task_assigned(task, by):
    body = (
        f"Hi {task.assignee.display_name},\n\n"
        f"{by.display_name} assigned you a new task:\n\n"
        f"  {task.title}\n"
        f"  Priority: {task.get_priority_display()}\n"
        f"  Due: {task.due_date:%d %b %Y}\n"
        + (f"\n{task.details}\n" if task.details else "")
        + f"\nOpen your tasks: {_url('my_tasks')}\n"
    )
    _send(f"New task: {task.title}", body, [task.assignee.email])


def task_submitted(task, by):
    body = (
        f"{task.assignee.display_name} submitted a task for approval:\n\n"
        f"  {task.title} (due {task.due_date:%d %b})\n"
        + (f"\nTheir note: {task.member_note}\n" if task.member_note else "")
        + f"\nReview it: {_url('dashboard')}?show=open\n"
    )
    _send(f"Needs approval: {task.title}", body, _admin_emails(exclude=by))


def task_approved(task, by):
    body = (
        f"Hi {task.assignee.display_name},\n\n"
        f"{by.display_name} approved your task \"{task.title}\". Well done!\n\n"
        f"{_url('my_tasks')}\n"
    )
    _send(f"Approved: {task.title}", body, [task.assignee.email])


def task_changes(task, by):
    body = (
        f"Hi {task.assignee.display_name},\n\n"
        f"{by.display_name} requested changes on \"{task.title}\":\n\n"
        f"  {task.admin_note}\n\n"
        f"Update it and submit again: {_url('my_tasks')}\n"
    )
    _send(f"Changes requested: {task.title}", body, [task.assignee.email])


def welcome(user, by):
    body = (
        f"Hi {user.display_name},\n\n"
        f"{by.display_name} added you to Unicolors Daily Tasks as {user.get_role_display()}.\n\n"
        f"Log in here: {_url('login')}\n"
        f"Username: {user.username}\n"
        f"Password: your admin will share it with you. You can change it after logging in.\n"
    )
    _send("You've been added to Unicolors Daily Tasks", body, [user.email])
