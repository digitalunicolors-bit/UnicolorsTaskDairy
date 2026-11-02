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
from django.db import connection
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .models import EmailFailure, User

log = logging.getLogger(__name__)

# Banner colours for each kind of email: (badge colour, light background, icon)
STYLES = {
    "assigned": ("#2f6fde", "#eef3fd", "📌", "New task"),
    "submitted": ("#b7791f", "#fdf6e9", "📤", "Needs approval"),
    "approved": ("#2a9d5c", "#ebf7f0", "✅", "Approved"),
    "changes": ("#d6409f", "#fcedf6", "✏️", "Changes requested"),
    "reopened": ("#d6409f", "#fcedf6", "↩️", "Reopened"),
    "welcome": ("#0B1220", "#FFF4CC", "👋", "Invitation"),
    "reminder_week": ("#2f6fde", "#eef3fd", "⏰", "Coming up"),
    "reminder_day": ("#b7791f", "#fdf6e9", "⏰", "Due tomorrow"),
    "reminder_due": ("#d93d42", "#fdeeee", "⏰", "Due today"),
    "overdue": ("#d93d42", "#fdeeee", "⚠️", "Overdue"),
    "mentioned": ("#2f6fde", "#eef3fd", "💬", "You were tagged"),
    "extension": ("#b7791f", "#fdf6e9", "📅", "Deadline"),
    "request": ("#2f6fde", "#eef3fd", "🙋", "Task request"),
    "leave": ("#2a9d5c", "#ebf7f0", "🌴", "Leave"),
    "summary": ("#0B1220", "#FFF4CC", "☀️", "Your day"),
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
        button.setdefault("color", color)
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
    lines.append("— Unicolors Workspace")
    return "\n".join(lines)


def _send(ctx, recipients, background=None):
    recipients = sorted({r for r in recipients if r})
    if not recipients or not getattr(settings, "EMAIL_NOTIFICATIONS", True):
        return
    subject = f"[Unicolors Workspace] {ctx['subject']}"
    text = _plain_text(ctx)
    html = render_to_string("tasks/email_task.html", ctx)

    def run():
        all_sent = True
        for to in recipients:  # one email each, so people don't see each other's addresses
            try:
                msg = EmailMultiAlternatives(subject, text, None, [to])
                msg.attach_alternative(html, "text/html")
                msg.send()
            except Exception as err:  # noqa: BLE001 - email problems must never break the app
                all_sent = False
                log.exception("Could not send email to %s", to)
                _record_failure(subject, to, err)
        return all_sent

    def run_in_thread():
        try:
            run()
        finally:
            connection.close()

    if background is None:
        background = getattr(settings, "EMAIL_SEND_IN_BACKGROUND", True)
    if background:
        threading.Thread(target=run_in_thread, daemon=True).start()
        return None
    return run()


def _record_failure(subject, recipient, err):
    """Keep failed emails where admins can see them. Never let this break anything."""
    try:
        EmailFailure.objects.create(subject=subject[:255], recipient=recipient[:255], error=str(err)[:2000])
    except Exception:  # noqa: BLE001
        log.exception("Could not record the email failure")


def _admin_emails(exclude=None):
    qs = User.objects.filter(is_active=True, role=User.Role.ADMIN).exclude(email="")
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    return list(qs.values_list("email", flat=True))


def _overseer_emails(task, exclude=None):
    return [p.email for p in task.overseers() if p.email and (exclude is None or p.pk != exclude.pk)]


def _worker_emails(task):
    return [task.assignee.email, *[p.email for p in task.collaborators.all()]]


# ---------------------------------------------------------------- emails


def task_assigned(task, by):
    if task.recurring_id:
        intro = f"Your recurring task for {task.occurrence_date:%A, %d %b} is ready ({task.recurring.schedule_label})."
    else:
        intro = f"{by.display_name if by else 'Unicolors Workspace'} assigned you a new task. Here are the details:"
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
    _send(ctx, _worker_emails(task))


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
    _send(ctx, _overseer_emails(task, exclude=by))


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
    _send(ctx, _worker_emails(task))


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
    _send(ctx, _worker_emails(task))


def task_mentioned(task, by, people, text):
    """Email the people tagged on a task."""
    ctx = _context(
        "mentioned",
        subject=f"{by.display_name} tagged you: {task.title}",
        heading=task.title,
        greeting_name="there",
        intro=f"{by.display_name} tagged you on this task.",
        rows=_task_rows(task),
        notes=[{"label": "What they wrote", "text": text, "color": "#2f6fde"}],
        button={"label": "Open task", "url": _task_link(task, for_admin=False)},
    )
    _send(ctx, [p.email for p in people])


def extension_requested(ext):
    task = ext.task
    who = ext.requested_by.display_name if ext.requested_by else "Someone"
    ctx = _context(
        "extension",
        subject=f"More time requested: {task.title}",
        heading=task.title,
        greeting_name="team",
        intro=f"{who} asked to move the deadline from {ext.previous_date:%d %b} to {ext.requested_date:%d %b %Y}.",
        rows=_task_rows(task),
        notes=[{"label": "Reason", "text": ext.reason, "color": "#b7791f"}],
        button={"label": "Review request", "url": _task_link(task, for_admin=True)},
    )
    _send(ctx, _overseer_emails(task, exclude=ext.requested_by))


def extension_decided(ext):
    task = ext.task
    approved = ext.status == ext.Status.APPROVED
    ctx = _context(
        "extension",
        subject=f"More time {'approved' if approved else 'declined'}: {task.title}",
        heading=task.title,
        greeting_name=task.assignee.first_name or task.assignee.display_name,
        intro=(f"The new deadline is {ext.requested_date:%d %b %Y}." if approved
               else f"The deadline stays {ext.previous_date:%d %b %Y}."),
        rows=_task_rows(task),
        notes=[{"label": "Note", "text": ext.decision_note, "color": "#b7791f"}],
        button={"label": "Open task", "url": _task_link(task, for_admin=False)},
    )
    _send(ctx, _worker_emails(task))


MAX_SUMMARY_TASKS = 15


def morning_summary(user, summary, today):
    """One email with everything needing this person's attention today."""
    items = []
    for t in summary["overdue"]:
        days = (today - t.due_date).days
        items.append((t, f"{days} day{'s' if days != 1 else ''} late", "#fdeeee", "#d93d42", False))
    for t in summary["today"]:
        items.append((t, "Due today", "#fdf6e9", "#b7791f", False))
    for t in summary["approvals"]:
        items.append((t, "To approve", "#eef3fd", "#2f6fde", True))
    rows = [
        {
            "title": t.title,
            "who": f"{t.client.name} · {t.assignee.display_name}" if t.client else t.assignee.display_name,
            "due": f"{t.due_date:%d %b}",
            "late": label,
            "pill_bg": bg,
            "pill_color": color,
            "status": t.get_status_display(),
            "url": _task_link(t, for_admin=for_admin),
        }
        for t, label, bg, color, for_admin in items[:MAX_SUMMARY_TASKS]
    ]
    parts = []
    if summary["overdue"]:
        parts.append(f"{len(summary['overdue'])} overdue")
    if summary["today"]:
        parts.append(f"{len(summary['today'])} due today")
    if summary["approvals"]:
        parts.append(f"{len(summary['approvals'])} to approve")
    if summary["leave_requests"]:
        parts.append(f"{summary['leave_requests']} leave request{'s' if summary['leave_requests'] != 1 else ''}")
    if summary["more_time"]:
        parts.append(f"{summary['more_time']} asking for more time")
    extra = len(items) - len(rows)
    ctx = _context(
        "summary",
        subject=f"Your day: {', '.join(parts)}",
        heading=f"{today:%A, %d %B}",
        greeting_name=user.first_name or user.display_name,
        intro="Here's what needs you today: " + ", ".join(parts) + ".",
        task_list=rows,
        button={"label": "Open Workspace", "url": _url("home")},
        footer=f"And {extra} more in the Workspace." if extra else "",
    )
    return _send(ctx, [user.email], background=False)


def _leave_rows(leave):
    return [
        {"label": "Person", "value": leave.user.display_name, "bold": True},
        {"label": "Type", "value": leave.get_kind_display()},
        {"label": "From", "value": f"{leave.start_date:%a, %d %b %Y}"},
        {"label": "To", "value": f"{leave.end_date:%a, %d %b %Y}"},
    ]


def leave_requested(leave, people):
    ctx = _context(
        "leave",
        subject=f"Leave request: {leave.user.display_name}, {leave.start_date:%d %b} to {leave.end_date:%d %b}",
        heading="New leave request",
        greeting_name="team",
        intro=f"{leave.user.display_name} applied for leave and needs a decision.",
        rows=_leave_rows(leave) + (
            [{"label": "Sent to", "value": leave.approver.display_name}] if leave.approver_id else []
        ),
        notes=[{"label": "Reason", "text": leave.note, "color": "#2a9d5c"}],
        button={"label": "Review request", "url": _url("leave_approvals")},
    )
    _send(ctx, [p.email for p in people])


def leave_decided(leave):
    approved = leave.status == leave.Status.APPROVED
    ctx = _context(
        "leave",
        subject=f"Your leave was {'approved' if approved else 'declined'}: {leave.start_date:%d %b} to {leave.end_date:%d %b}",
        heading=f"Leave {'approved' if approved else 'declined'}",
        greeting_name=leave.user.first_name or leave.user.display_name,
        intro=f"{leave.decided_by.display_name if leave.decided_by else 'Your manager'} {'approved' if approved else 'declined'} your leave request.",
        rows=_leave_rows(leave),
        notes=[{"label": "Note", "text": leave.decision_note, "color": "#2a9d5c"}],
        button={"label": "View my leave", "url": _url("my_leave")},
    )
    _send(ctx, [leave.user.email])


def leave_cancelled(leave, people):
    ctx = _context(
        "leave",
        subject=f"Leave cancelled: {leave.user.display_name}, {leave.start_date:%d %b} to {leave.end_date:%d %b}",
        heading="Leave cancelled",
        greeting_name="team",
        intro=f"{leave.user.display_name} cancelled this leave, so they're working on these dates after all.",
        rows=_leave_rows(leave),
        button={"label": "Open leave page", "url": _url("leave_approvals")},
    )
    _send(ctx, [p.email for p in people])


REMINDER_TEXT = {
    "week": ("in 7 days", "Plenty of time — just a heads-up so you can plan ahead."),
    "day": ("tomorrow", "Make sure you're on track to finish it by tomorrow."),
    "due": ("today", "Please finish it and mark it complete today."),
}


def task_reminder(task, kind, when=None):
    """Due-date reminder for the person the task is assigned to. Sent right away (not in background)."""
    default_when, tip = REMINDER_TEXT[kind]
    when = when or default_when
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
    """A summary email listing tasks that just became overdue: all of them for admins, their own team for team leads."""
    _overdue_email(tasks, today, _admin_emails())
    by_lead = {}
    for t in tasks:
        lead = t.assignee.manager
        if lead and lead.is_active and lead.email and not lead.is_admin:
            by_lead.setdefault(lead.email, []).append(t)
    for email, theirs in by_lead.items():
        _overdue_email(theirs, today, [email])


def _overdue_email(tasks, today, recipients):
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
    _send(ctx, recipients, background=False)


def welcome(user, by, background=False):
    """Send the invitation. Returns True if it went out, so the screen can tell the truth."""
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    invite_url = _site() + reverse("accept_invite", kwargs={"uidb64": uid, "token": token})
    hours = getattr(settings, "INVITE_EXPIRY_HOURS", 72)

    rows = [
        {"label": "Workspace", "value": "Unicolors Workspace", "bold": True},
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
        intro=f"{by.display_name} invited you to Unicolors Workspace, where the team assigns and tracks work and manages leave.",
        rows=rows,
        button={"label": "Join workspace", "url": invite_url},
        footer=f"This invitation link expires in {hours} hours. If it expires, ask your admin to resend it.",
    )
    return _send(ctx, [user.email], background=background)