"""Email notifications. Sending never blocks or breaks the app: failures are logged and skipped.

Every email is sent as a designed HTML card (templates/tasks/email_task.html)
with a plain-text version for email apps that don't show HTML.
"""
import logging
import threading
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .models import User

log = logging.getLogger(__name__)

# Banner colours for each kind of email: (badge colour, light background, icon)
STYLES = {
    "assigned": ("#2f6fde", "#eef3fd", "📌", "New task"),
    "submitted": ("#b7791f", "#fdf6e9", "📤", "Needs approval"),
    "approved": ("#2a9d5c", "#ebf7f0", "✅", "Approved"),
    "changes": ("#d6409f", "#fcedf6", "✏️", "Changes requested"),
    "reopened": ("#d6409f", "#fcedf6", "↩️", "Reopened"),
    "welcome": ("#e5484d", "#fdeeee", "👋", "Invitation"),
    "reminder_week": ("#2f6fde", "#eef3fd", "⏰", "Due in 7 days"),
    "reminder_day": ("#b7791f", "#fdf6e9", "⏰", "Due tomorrow"),
    "reminder_due": ("#d93d42", "#fdeeee", "⏰", "Due today"),
    "overdue": ("#d93d42", "#fdeeee", "⚠️", "Overdue"),
}
PRIORITY_COLORS = {"high": "#d93d42", "medium": "#b7791f", "low": "#2a9d5c"}


# ---------------------------------------------------------------- helpers


def _site():
    return getattr(settings, "SITE_URL", "http://127.0.0.1:8000").rstrip("/")


def _url(name):
    return _site() + reverse(name)


def _task_link(task, for_admin):
    """Link that opens the board with this task's pop-up."""
    if for_admin:
        return f"{_url('dashboard')}?show=custom&mode=single&date={task.due_date:%Y-%m-%d}#task-{task.pk}"
    return f"{_url('my_tasks')}?show=all#task-{task.pk}"


def _task_rows(task):
    rows = [
        {"label": "Task", "value": task.title, "bold": True},
        {"label": "Priority", "value": task.get_priority_display(), "color": PRIORITY_COLORS.get(task.priority)},
        {
            "label": "Due date",
            "value": f"{task.due_date:%a, %d %b %Y}" + ("  ·  Overdue" if task.is_late else ""),
            "color": "#d93d42" if task.is_late else None,
        },
        {"label": "Assigned to", "value": task.assignee.display_name},
    ]
    if task.assigned_by:
        rows.append({"label": "Assigned by", "value": task.assigned_by.display_name})
    rows.append({"label": "Status", "value": task.get_status_display()})
    return rows


def _context(kind, subject, heading, greeting_name, intro, rows=(), notes=(), button=None, footer="", preheader="",
             task_list=()):
    color, bg, icon, label = STYLES[kind]
    if button:
        button.setdefault("color", color if kind != "welcome" else "#e5484d")
    return {
        "subject": subject,
        "preheader": preheader or intro,
        "badge": {"color": color, "bg": bg, "icon": icon, "label": label},
        "heading": heading,
        "greeting_name": greeting_name,
        "intro": intro,
        "rows": list(rows),
        "task_list": list(task_list),
        "notes": [n for n in notes if n.get("text")],
        "button": button,
        "footer": footer,
        "site_url": _site(),
        "site_host": urlparse(_site()).netloc or _site(),
    }


def _plain_text(ctx):
    """Neatly laid-out text version of the same email."""
    lines = [f"{ctx['badge']['label'].upper()}: {ctx['heading']}", "", f"Hi {ctx['greeting_name']},", "", ctx["intro"], ""]
    if ctx["rows"]:
        width = max(len(r["label"]) for r in ctx["rows"])
        lines += [f"  {r['label']:<{width}} : {r['value']}" for r in ctx["rows"]]
        lines.append("")
    for t in ctx.get("task_list", []):
        lines += [f"  • {t['title']} — {t['who']} · due {t['due']} · {t['late']} · {t['status']}", f"    {t['url']}"]
    if ctx.get("task_list"):
        lines.append("")
    for n in ctx["notes"]:
        lines += [f"{n['label']}:", *[f"  {line}" for line in str(n["text"]).splitlines()], ""]
    if ctx["button"]:
        lines += [f"{ctx['button']['label']}: {ctx['button']['url']}", ""]
    if ctx["footer"]:
        lines += [ctx["footer"], ""]
    lines.append("— Unicolors Daily Tasks")
    return "\n".join(lines)


def _send(ctx, recipients, background=None):
    recipients = sorted({r for r in recipients if r})
    if not recipients or not getattr(settings, "EMAIL_NOTIFICATIONS", True):
        return
    subject = f"[Unicolors Tasks] {ctx['subject']}"
    text = _plain_text(ctx)
    html = render_to_string("tasks/email_task.html", ctx)

    def run():
        try:
            for to in recipients:  # one email each, so people don't see each other's addresses
                msg = EmailMultiAlternatives(subject, text, None, [to])
                msg.attach_alternative(html, "text/html")
                msg.send()
        except Exception:  # noqa: BLE001 - email problems must never break the app
            log.exception("Could not send email to %s", recipients)

    if background is None:
        background = getattr(settings, "EMAIL_SEND_IN_BACKGROUND", True)
    if background:
        threading.Thread(target=run, daemon=True).start()
    else:
        run()


def _admin_emails(exclude=None):
    qs = User.objects.filter(is_active=True, role=User.Role.ADMIN).exclude(email="")
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    return list(qs.values_list("email", flat=True))


# ---------------------------------------------------------------- emails


def task_assigned(task, by):
    if task.recurring_id:
        intro = f"Your recurring task for {task.occurrence_date:%A, %d %b} is ready ({task.recurring.schedule_label})."
    else:
        intro = f"{by.display_name if by else 'Unicolors Tasks'} assigned you a new task. Here are the details:"
    ctx = _context(
        "assigned",
        subject=f"New task: {task.title}",
        heading=task.title,
        greeting_name=task.assignee.first_name or task.assignee.display_name,
        intro=intro,
        rows=_task_rows(task),
        notes=[{"label": "Brief", "text": task.details, "color": "#2f6fde"}],
        button={"label": "Open task", "url": _task_link(task, for_admin=False)},
        footer="Start the task when you begin, and mark it complete when it's done.",
    )
    _send(ctx, [task.assignee.email])


def task_submitted(task, by):
    ctx = _context(
        "submitted",
        subject=f"Needs approval: {task.title}",
        heading=task.title,
        greeting_name="team",
        intro=f"{task.assignee.display_name} finished this task and submitted it for your approval.",
        rows=_task_rows(task),
        notes=[{"label": f"{task.assignee.display_name}'s note", "text": task.member_note, "color": "#b7791f"}],
        button={"label": "Review task", "url": _task_link(task, for_admin=True)},
        footer="Approve it, or request changes with a short note.",
    )
    _send(ctx, _admin_emails(exclude=by))


def task_approved(task, by):
    ctx = _context(
        "approved",
        subject=f"Approved: {task.title}",
        heading=task.title,
        greeting_name=task.assignee.first_name or task.assignee.display_name,
        intro=f"Great work! {by.display_name} approved your task.",
        rows=_task_rows(task) + [{"label": "Approved by", "value": by.display_name}],
        button={"label": "View task", "url": _task_link(task, for_admin=False)},
    )
    _send(ctx, [task.assignee.email])


def task_changes(task, by):
    ctx = _context(
        "changes",
        subject=f"Changes requested: {task.title}",
        heading=task.title,
        greeting_name=task.assignee.first_name or task.assignee.display_name,
        intro=f"{by.display_name} reviewed your task and asked for a few changes.",
        rows=_task_rows(task) + [{"label": "Change requests", "value": str(task.change_count)}],
        notes=[{"label": "What needs to change", "text": task.admin_note, "color": "#d6409f"}],
        button={"label": "Update task", "url": _task_link(task, for_admin=False)},
        footer="Make the changes, then mark the task complete again to send it back for approval.",
    )
    _send(ctx, [task.assignee.email])


REMINDER_TEXT = {
    "week": ("in 7 days", "Plenty of time — just a heads-up so you can plan ahead."),
    "day": ("tomorrow", "Make sure you're on track to finish it by tomorrow."),
    "due": ("today", "Please finish it and mark it complete today."),
}


def task_reminder(task, kind):
    """Due-date reminder for the person the task is assigned to. Sent right away (not in background)."""
    when, tip = REMINDER_TEXT[kind]
    started = task.status != task.Status.TODO
    ctx = _context(
        f"reminder_{kind}",
        subject=f"Reminder: “{task.title}” is due {when}",
        heading=task.title,
        greeting_name=task.assignee.first_name or task.assignee.display_name,
        intro=f"Friendly reminder: this task is due {when}, on {task.due_date:%A, %d %B}. {tip}",
        rows=_task_rows(task),
        notes=[
            {"label": "Brief", "text": task.details, "color": STYLES[f"reminder_{kind}"][0]},
            {"label": "Changes requested", "text": task.admin_note if task.status == task.Status.CHANGE else "", "color": "#d6409f"},
        ],
        button={"label": "Open task" if started else "Start task", "url": _task_link(task, for_admin=False)},
        footer="You're getting this because the task is assigned to you and isn't complete yet.",
    )
    _send(ctx, [task.assignee.email], background=False)


def overdue_digest(tasks, today):
    """One summary email to each admin listing tasks that just became overdue."""
    n = len(tasks)
    items = []
    for t in tasks:
        days = (today - t.due_date).days
        items.append({
            "title": t.title,
            "who": t.assignee.display_name,
            "due": f"{t.due_date:%d %b}",
            "late": "1 day late" if days == 1 else f"{days} days late",
            "status": t.get_status_display(),
            "url": _task_link(t, for_admin=True),
        })
    ctx = _context(
        "overdue",
        subject=f"{n} task{'s' if n != 1 else ''} overdue",
        heading=f"{n} task{'s have' if n != 1 else ' has'} passed the due date",
        greeting_name="team",
        intro="These tasks passed their due date and haven't been submitted for approval yet:",
        task_list=items,
        button={"label": "Open dashboard", "url": _url("dashboard")},
        footer="Each overdue task is reported once. Click a task to open it, or reach out to the person directly.",
    )
    _send(ctx, _admin_emails(), background=False)


def welcome(user, by):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    invite_url = _site() + reverse("accept_invite", kwargs={"uidb64": uid, "token": token})
    hours = getattr(settings, "INVITE_EXPIRY_HOURS", 72)

    rows = [
        {"label": "Workspace", "value": "Unicolors Daily Tasks", "bold": True},
        {"label": "Your role", "value": user.get_role_display()},
    ]
    if user.designation:
        rows.append({"label": "Designation", "value": user.designation})
    rows += [
        {"label": "Username", "value": user.username},
        {"label": "Invited by", "value": by.display_name},
    ]
    ctx = _context(
        "welcome",
        subject="You're invited to Unicolors Workspace",
        heading="You're invited to join the team",
        greeting_name=user.first_name or user.display_name,
        intro=f"{by.display_name} invited you to Unicolors Daily Tasks, where the team assigns, tracks and completes daily work.",
        rows=rows,
        button={"label": "Join workspace", "url": invite_url},
        footer=f"This invitation link expires in {hours} hours. If it expires, ask your admin to resend it.",
    )
    _send(ctx, [user.email])