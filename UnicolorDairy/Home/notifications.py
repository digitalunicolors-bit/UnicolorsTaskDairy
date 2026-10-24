"""Email notifications. Sending never blocks or breaks the app: failures are logged and skipped."""
import logging
import threading

from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse

from .models import User
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.core.mail import EmailMultiAlternatives

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
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)

    invite_url = (
        getattr(
            settings,
            "SITE_URL",
            "http://127.0.0.1:8000"
        ).rstrip("/")
        + reverse(
            "accept_invite",
            kwargs={
                "uidb64": uid,
                "token": token,
            },
        )
    )

    subject = "You're invited to Unicolors Workspace"

    text_body = (
        f"Hi {user.display_name},\n\n"
        f"{by.display_name} invited you to join "
        f"Unicolors Daily Tasks as {user.get_role_display()}.\n\n"
        f"Join Workspace:\n{invite_url}\n"
    )

    html_body = f"""
    <div style="
        font-family:Arial,sans-serif;
        max-width:560px;
        margin:30px auto;
        padding:35px;
        background:#ffffff;
        border-radius:14px;
    ">

        <h2 style="margin-bottom:20px;">
            You're invited to Unicolors
        </h2>

        <p>Hi {user.display_name},</p>

        <p style="line-height:1.6;color:#555;">
            <strong>{by.display_name}</strong> invited you to join
            <strong>Unicolors Daily Tasks</strong>
            as {user.get_role_display()}.
        </p>

        <div style="margin:30px 0;">

            <a href="{invite_url}"
               style="
                   background:#ed4650;
                   color:white;
                   padding:14px 28px;
                   border-radius:8px;
                   text-decoration:none;
                   font-weight:bold;
                   display:inline-block;
               ">
                Join Workspace
            </a>

        </div>

        <p style="font-size:13px;color:#888;">
            This invitation link will expire automatically.
        </p>

    </div>
    """

    recipients = [user.email]

    email = EmailMultiAlternatives(
        subject=f"[Unicolors Tasks] {subject}",
        body=text_body,
        from_email=None,
        to=recipients,
    )

    email.attach_alternative(html_body, "text/html")
    email.send()