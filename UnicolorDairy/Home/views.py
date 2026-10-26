from datetime import date
from datetime import timedelta
from functools import wraps
from django.contrib.auth import login
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.core.files.base import ContentFile
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.views.decorators.http import require_POST
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from django.contrib.auth.tokens import default_token_generator
from . import notifications, recurring, reports
from .forms import (
    AssignTaskForm,
    SetPasswordForm,
    UserCreateForm,
    UserEditForm,
    AcceptInviteForm,
    RecurringTaskForm,
)
from .models import (
    Notification, RecurringTask, Task, TaskActivity, TaskAttachment, TaskComment, User,
    active_admins, find_mentioned_users,
)

S = Task.Status


def admin_required(view):
    """Admin-only pages. Team members are sent back to My Tasks with a friendly message
    (instead of a "403 Forbidden" page)."""
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not request.user.is_admin:
            if request.headers.get("X-Requested-With") == "fetch":
                raise PermissionDenied  # background requests just get "no access"
            messages.info(request, "That page is only for admins, so we brought you to your tasks.")
            return redirect("my_tasks")
        return view(request, *args, **kwargs)

    return wrapper


def _parse_date(value, default):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return default


def _sorted(tasks):
    return sorted(tasks, key=lambda t: (Task.PRIORITY_ORDER.get(t.priority, 1), t.due_date, t.created_at))


def _board(tasks):
    tasks = list(tasks)
    return [
        {"status": value, "label": label, "tasks": _sorted(t for t in tasks if t.status == value)}
        for value, label in S.choices
    ]


def _stats(tasks):
    tasks = list(tasks)
    total = len(tasks)
    done = sum(1 for t in tasks if t.status == S.DONE)
    return {
        "total": total,
        "progress": sum(1 for t in tasks if t.status == S.PROGRESS),
        "review": sum(1 for t in tasks if t.status == S.REVIEW),
        "change": sum(1 for t in tasks if t.status == S.CHANGE),
        "late": sum(1 for t in tasks if t.is_late),
        "pct": round(done * 100 / total) if total else 0,
    }


def _log(task, user, action, note=""):
    TaskActivity.objects.create(task=task, actor=user, action=action, note=note)


def _mention_people():
    """Everyone who can be tagged with @, for the autocomplete list."""
    return [
        {"u": u.username, "n": u.display_name, "i": u.initials, "d": u.designation}
        for u in User.objects.filter(is_active=True)
    ]


def _safe_next(request, default):
    """Only allow redirects back to a page on this site."""
    back = request.POST.get("next") or request.GET.get("next") or ""
    if not back.startswith("/") or back.startswith("//"):
        return default
    return back


# ---------------------------------------------------------------- home


@login_required
def home(request):
    return redirect("dashboard" if request.user.is_admin else "my_tasks")


# ---------------------------------------------------------------- admin dashboard


@admin_required
def dashboard(request):
    today = timezone.localdate()
    show = request.GET.get("show", "today")
    mode = request.GET.get("mode", "single")
    member_id = request.GET.get("member", "all")
    day = _parse_date(request.GET.get("date"), today)

    # Work out which dates to show
    if show == "week":
        start, end, mode = today - timedelta(days=6), today, "range"
    elif show == "month":
        first = today.replace(day=1)
        next_month = (first + timedelta(days=32)).replace(day=1)
        start, end, mode = first, next_month - timedelta(days=1), "range"
    elif show == "custom":
        if mode == "range":
            start = _parse_date(request.GET.get("start"), today.replace(day=1))
            end = _parse_date(request.GET.get("end"), today)
            if end < start:
                start, end = end, start
        else:
            mode = "single"
            start = end = day
    else:
        show, mode = "today", "single"
        start = end = today

    qs = (
        Task.objects.select_related("assignee").prefetch_related("attachments")
        .annotate(comment_count=Count("comments", distinct=True))
    )
    if member_id != "all":
        qs = qs.filter(assignee_id=member_id)

    if mode == "single":
        # One day: tasks due that day, plus anything waiting for approval.
        # For today, also include older tasks that are still not done.
        tasks = list(qs.exclude(status=S.DONE).filter(due_date__lt=start)) if start == today else []
        tasks += list(qs.filter(due_date=start))
        tasks += list(qs.filter(status=S.REVIEW))
        tasks = list({t.id: t for t in tasks}.values())
    else:
        tasks = list(qs.filter(due_date__range=(start, end)))

    form = AssignTaskForm()
    return render(request, "tasks/dashboard.html", {
        "form": form,
        "board": _board(tasks),
        "stats": _stats(tasks),
        "show": show,
        "mode": mode,
        "day": start,
        "start": start,
        "end": end,
        "member_id": member_id,
        "members": User.objects.filter(is_active=True),
        "mention_people": _mention_people(),
        "is_admin_view": True,
        "max_upload_mb": getattr(settings, "MAX_UPLOAD_MB", 20),
    })


@admin_required
@require_POST
def assign_task(request):
    form = AssignTaskForm(request.POST, request.FILES)
    if not form.is_valid():
        for errors in form.errors.values():
            for e in errors:
                messages.error(request, e)
        return redirect("dashboard")
    data = form.cleaned_data
    uploads = [(f, TaskAttachment.Kind.FILE) for f in data["attachments"]]
    if data["voice_note"]:
        uploads.append((data["voice_note"], TaskAttachment.Kind.VOICE))
    # Read each upload once so the same file can be attached to every assignee's task.
    blobs = [(f.name, f.read(), kind) for f, kind in uploads]
    with transaction.atomic():
        for user in data["assignees"]:
            task = Task.objects.create(
                title=data["title"], details=data["details"], priority=data["priority"],
                due_date=data["due_date"], assignee=user, assigned_by=request.user,
            )
            for name, content, kind in blobs:
                att = TaskAttachment(task=task, kind=kind, original_name=name[:255], size=len(content), uploaded_by=request.user)
                att.file.save(name, ContentFile(content), save=True)
            _log(task, request.user, TaskActivity.Action.ASSIGNED)
            Notification.send(task, request.user, "assigned", recipients=[user])
            tagged = [u for u in find_mentioned_users(task.details) if u.pk not in (user.pk, request.user.pk)]
            if tagged:
                task.watchers.add(*tagged)
                Notification.send(task, request.user, "mentioned", recipients=tagged)
            transaction.on_commit(lambda t=task: notifications.task_assigned(t, request.user))
    names = ", ".join(u.display_name for u in data["assignees"])
    messages.success(request, f'"{data["title"]}" assigned to {names}.')
    return redirect("dashboard")


# ---------------------------------------------------------------- task actions


@login_required
@require_POST
def task_action(request, pk, action):
    task = get_object_or_404(Task, pk=pk)
    user = request.user
    note = request.POST.get("note", "").strip()[:1000]
    now = timezone.now()
    is_owner = task.assignee_id == user.id
    back = request.POST.get("next", "")
    if not back.startswith("/") or back.startswith("//"):
        back = "home"  # only allow same-site redirects

    member_actions = {"start", "submit"}
    admin_actions = {"approve", "changes", "reopen", "delete"}

    if action in member_actions and not (is_owner or user.is_admin):
        raise PermissionDenied
    if action in admin_actions and not user.is_admin:
        raise PermissionDenied

    if action == "start" and task.status == S.TODO:
        task.status, task.started_at = S.PROGRESS, now
        task.save()
        _log(task, user, TaskActivity.Action.STARTED)
        Notification.send(task, user, "started")
    elif action == "submit" and task.status in (S.TODO, S.PROGRESS, S.CHANGE):
        task.status, task.submitted_at, task.member_note = S.REVIEW, now, note
        task.started_at = task.started_at or now
        task.save()
        _log(task, user, TaskActivity.Action.SUBMITTED, note)
        Notification.send(task, user, "submitted", note)
        notifications.task_submitted(task, user)
        messages.success(request, "Submitted for approval.")
    elif action == "approve" and task.status == S.REVIEW:
        task.status, task.done_at, task.approved_by, task.admin_note = S.DONE, now, user, ""
        task.save()
        _log(task, user, TaskActivity.Action.APPROVED)
        Notification.send(task, user, "approved")
        notifications.task_approved(task, user)
        messages.success(request, f'Approved "{task.title}".')
    elif action in ("changes", "reopen") and task.status in (S.REVIEW, S.DONE):
        if not note:
            messages.error(request, "Please write what needs to change.")
            return redirect(back)
        reopened = task.status == S.DONE
        task.status, task.admin_note, task.done_at, task.approved_by = S.CHANGE, note, None, None
        task.change_count += 1
        task.save()
        _log(task, user, TaskActivity.Action.REOPENED if reopened else TaskActivity.Action.CHANGES, note)
        Notification.send(task, user, "reopened" if reopened else "changes", note)
        notifications.task_changes(task, user)
        messages.success(request, "Change request sent.")
    elif action == "delete":
        title = task.title
        Notification.send(task, user, "deleted")
        task.delete()
        messages.success(request, f'Deleted "{title}".')
    else:
        messages.error(request, "That action isn't possible for this task right now.")
    return redirect(back)


@login_required
def task_detail(request, pk):
    """Full details of one task, shown in the pop-up on the board."""
    task = get_object_or_404(
        Task.objects.select_related("assignee", "assigned_by", "approved_by").prefetch_related("attachments"), pk=pk
    )
    if not task.can_view(request.user):
        raise PermissionDenied
    back = request.GET.get("next", "")
    if not back.startswith("/") or back.startswith("//"):
        back = reverse("home")
    return render(request, "tasks/_task_detail.html", {
        "t": task,
        "activity": list(task.activity.select_related("actor")),
        "comments": list(task.comments.select_related("author")),
        "people": _mention_people(),
        "next": back,
        "admin_view": request.user.is_admin,
    })


def _comments_response(request, task):
    """Send back the refreshed comments list (used by the pop-up)."""
    if request.headers.get("X-Requested-With") != "fetch":
        return redirect(_safe_next(request, "home"))
    return render(request, "tasks/_comments.html", {
        "t": task,
        "comments": list(task.comments.select_related("author")),
    })


@login_required
@require_POST
def task_comment(request, pk):
    task = get_object_or_404(Task.objects.select_related("assignee"), pk=pk)
    if not task.can_view(request.user):
        raise PermissionDenied
    body = request.POST.get("body", "").strip()[:2000]
    if body:
        comment = TaskComment.objects.create(task=task, author=request.user, body=body)
        tagged = [u for u in find_mentioned_users(body) if u.pk != request.user.pk]
        if tagged:
            comment.mentions.set(tagged)
            task.watchers.add(*tagged)
            Notification.send(task, request.user, "mentioned", body, recipients=tagged)
        # Everyone else involved hears about the new comment.
        tagged_ids = {u.pk for u in tagged}
        commenters = User.objects.filter(pk__in=task.comments.values("author"))
        involved = [task.assignee, *active_admins(), *task.watchers.all(), *commenters]
        Notification.send(task, request.user, "commented", body,
                          recipients=[p for p in involved if p.pk not in tagged_ids])
    return _comments_response(request, task)


@login_required
@require_POST
def task_comment_delete(request, pk):
    comment = get_object_or_404(TaskComment.objects.select_related("task"), pk=pk)
    if not (request.user.is_admin or comment.author_id == request.user.id):
        raise PermissionDenied
    task = comment.task
    comment.delete()
    return _comments_response(request, task)


@login_required
def attachment(request, pk):
    """Serve a task file only to the person it's assigned to, and to admins."""
    att = get_object_or_404(TaskAttachment.objects.select_related("task"), pk=pk)
    if not att.task.can_view(request.user):
        raise PermissionDenied
    try:
        handle = att.file.open("rb")
    except FileNotFoundError:
        raise Http404("File is missing on the server.")
    ext = att.original_name.lower().rsplit(".", 1)[-1]
    # Only open safe types in the browser; everything else downloads.
    inline = ext in {"jpg", "jpeg", "png", "gif", "webp", "pdf", "webm", "ogg", "m4a", "mp3", "wav"}
    return FileResponse(handle, as_attachment=not inline, filename=att.original_name)


# ---------------------------------------------------------------- member view


@login_required
def my_tasks(request):
    show = request.GET.get("show", "open")
    today = timezone.localdate()
    qs = (
        Task.objects.filter(assignee=request.user).select_related("assignee").prefetch_related("attachments")
        .annotate(comment_count=Count("comments", distinct=True))
    )
    if show == "month":
        qs = qs.filter(due_date__year=today.year, due_date__month=today.month)
    elif show == "all":
        pass
    else:
        show = "open"
        tasks = [t for t in qs if t.status != S.DONE or t.done_date == today]
        qs = tasks
    tasks = list(qs)
    return render(request, "tasks/my_tasks.html", {"board": _board(tasks), "stats": _stats(tasks), "show": show})


# ---------------------------------------------------------------- reports


def _report_scope(request):
    first = reports.parse_month(request.GET.get("month"))
    member_id = request.GET.get("member", "all")
    if not request.user.is_admin:
        member_id = str(request.user.id)
    elif member_id != "all" and not member_id.isdigit():
        member_id = "all"
    qs = Task.objects.filter(due_date__year=first.year, due_date__month=first.month).select_related(
        "assignee", "approved_by"
    )
    if member_id != "all":
        qs = qs.filter(assignee_id=member_id)
    return first, member_id, list(qs)


@login_required
def report(request):
    first, member_id, tasks = _report_scope(request)
    days = reports.month_days(first)
    if member_id == "all":
        ids = {t.assignee_id for t in tasks}
        users = [u for u in User.objects.all() if u.is_active or u.id in ids]
    else:
        users = [get_object_or_404(User, pk=member_id)]
    return render(request, "tasks/report.html", {
        "month": first,
        "member_id": member_id,
        "members": User.objects.all(),
        "days": days,
        "summary": reports.summarize(tasks),
        "rows": reports.build_grid(users, tasks, days),
        "log": reports.day_log(tasks, days),
        "single": member_id != "all",
        "selected_user": users[0] if member_id != "all" else None,
    })


@login_required
def report_csv(request):
    first, member_id, tasks = _report_scope(request)
    tasks.sort(key=lambda t: (t.due_date, t.assignee.display_name))
    suffix = "" if member_id == "all" else f"-{tasks[0].assignee.username}" if tasks else ""
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="unicolors-report-{first:%Y-%m}{suffix}.csv"'
    return reports.write_csv(response, tasks)


# ---------------------------------------------------------------- team management


def _invite_status(user, now, expiry_hours):
    if user.invite_accepted_at:
        return "joined"
    if not user.invite_sent_at:
        return "none"
    if now > user.invite_sent_at + timedelta(hours=expiry_hours):
        return "expired"
    return "pending"


@admin_required
def team(request):
    today = timezone.localdate()
    now = timezone.now()
    expiry_hours = getattr(settings, "INVITE_EXPIRY_HOURS", 72)

    users = list(User.objects.all())
    month_tasks = list(Task.objects.filter(due_date__year=today.year, due_date__month=today.month))
    open_tasks = list(Task.objects.exclude(status=S.DONE).only("assignee_id", "status", "due_date"))
    with_tasks = set(Task.objects.values_list("assignee_id", flat=True).distinct())

    rows = []
    counts = {"all": 0, "admin": 0, "member": 0, "pending": 0, "inactive": 0}
    for u in users:
        invite_status = _invite_status(u, now, expiry_hours)
        if invite_status in ("pending", "expired"):
            state = invite_status
        elif not u.is_active:
            state = "inactive"
        else:
            state = "active"
        mine = [t for t in open_tasks if t.assignee_id == u.id]
        rows.append({
            "user": u,
            "summary": reports.summarize([t for t in month_tasks if t.assignee_id == u.id]),
            "invite_status": invite_status,
            "state": state,
            "open": len(mine),
            "review": sum(1 for t in mine if t.status == S.REVIEW),
            "overdue": sum(1 for t in mine if t.due_date < today),
            "has_tasks": u.id in with_tasks,
        })
        counts["all"] += 1
        counts["admin" if u.is_admin else "member"] += 1
        if state in ("pending", "expired"):
            counts["pending"] += 1
        elif state == "inactive":
            counts["inactive"] += 1

    rows.sort(key=lambda r: (r["state"] != "active", not r["user"].is_admin, r["user"].display_name.lower()))
    return render(request, "tasks/team.html", {
        "rows": rows,
        "counts": counts,
        "total_open": len(open_tasks),
        "month": today,
    })


@admin_required
@require_POST
def user_toggle_active(request, pk):
    """Deactivate (block login) or reactivate a person."""
    person = get_object_or_404(User, pk=pk)
    if person == request.user:
        messages.error(request, "You can't deactivate your own account.")
        return redirect("team")
    if person.invite_sent_at and not person.invite_accepted_at:
        messages.error(request, f"{person.display_name} hasn't joined yet. Resend or cancel the invitation instead.")
        return redirect("team")
    person.is_active = not person.is_active
    person.save(update_fields=["is_active"])
    if person.is_active:
        messages.success(request, f"{person.display_name} can log in again.")
    else:
        n = Task.objects.filter(assignee=person).exclude(status=S.DONE).count()
        msg = f"{person.display_name} can no longer log in."
        if n:
            msg += f" They still have {n} open task{'s' if n != 1 else ''} — reassign or delete them."
        messages.success(request, msg)
    return redirect("team")


@admin_required
@require_POST
def user_cancel_invite(request, pk):
    """Remove someone who was invited but never joined (and has no tasks)."""
    person = get_object_or_404(User, pk=pk)
    if person == request.user or person.invite_accepted_at or Task.objects.filter(assignee=person).exists():
        messages.error(request, "This invitation can't be cancelled because the person has joined or has tasks. Deactivate them instead.")
        return redirect("team")
    name, email = person.display_name, person.email
    person.delete()
    messages.success(request, f"Cancelled the invitation for {name}{f' ({email})' if email else ''}.")
    return redirect("team")


@admin_required
def user_add(request):
    form = UserCreateForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        user = form.save()

        user.invite_sent_at = timezone.now()
        user.invite_accepted_at = None
        user.save(
            update_fields=[
                "invite_sent_at",
                "invite_accepted_at"
            ]
        )

        notifications.welcome(
            user,
            request.user
        )

        messages.success(
            request,
            f"Invitation sent to {user.email}."
        )

        return redirect("team")

    return render(
        request,
        "tasks/user_form.html",
        {
            "form": form,
            "title": "Add a person"
        },
    )


@admin_required
def user_edit(request, pk):
    person = get_object_or_404(User, pk=pk)
    form = UserEditForm(request.POST or None, instance=person)
    if request.method == "POST" and form.is_valid():
        if person == request.user and (not form.cleaned_data["is_active"] or form.cleaned_data["role"] != User.Role.ADMIN):
            messages.error(request, "You can't remove your own admin access. Ask another admin to do it.")
        else:
            form.save()
            messages.success(request, f"Saved {person.display_name}.")
            return redirect("team")
    return render(request, "tasks/user_form.html", {"form": form, "title": f"Edit {person.display_name}", "person": person})


@admin_required
def user_password(request, pk):
    person = get_object_or_404(User, pk=pk)
    form = SetPasswordForm(person, request.POST or None)
    if request.method == "POST" and form.is_valid():
        person.set_password(form.cleaned_data["password"])
        person.save()
        messages.success(request, f"New password set for {person.display_name}.")
        return redirect("team")
    return render(request, "tasks/user_form.html", {"form": form, "title": f"Reset password – {person.display_name}"})


def accept_invite(request, uidb64, token):
    try:
        user_id = force_str(urlsafe_base64_decode(uidb64))
        person = User.objects.get(pk=user_id)

    except (
        TypeError,
        ValueError,
        OverflowError,
        User.DoesNotExist,
    ):
        person = None

    if person is None:
        messages.error(request, "This invitation link is invalid.")
        return redirect("login")

    # If already accepted, send user to login
    if person.invite_accepted_at:
        messages.info(
            request,
            "This invitation has already been accepted. Please log in."
        )
        return redirect("login")

    # Check invitation expiry
    expiry_hours = getattr(
        settings,
        "INVITE_EXPIRY_HOURS",
        72
    )

    if person.invite_sent_at:
        expiry_time = (
            person.invite_sent_at
            + timedelta(hours=expiry_hours)
        )

        if timezone.now() > expiry_time:
            messages.error(
                request,
                "This invitation has expired. "
                "Please ask your administrator to resend it."
            )
            return redirect("login")

    # Check security token
    if not default_token_generator.check_token(person, token):
        messages.error(
            request,
            "This invitation link is invalid or expired."
        )
        return redirect("login")

    # Show password form
    form = AcceptInviteForm(request.POST or None)

    if request.method == "POST" and form.is_valid():

        # Set employee's chosen password
        person.set_password(
            form.cleaned_data["password1"]
        )

        # Activate employee
        person.is_active = True

        # Mark invitation accepted
        person.invite_accepted_at = timezone.now()

        person.save(
            update_fields=[
                "password",
                "is_active",
                "invite_accepted_at",
            ]
        )

        # Automatically login employee
        login(
            request,
            person,
            backend="Home.backends.EmailOrUsernameBackend",
        )

        messages.success(
            request,
            "Welcome! Your account is ready."
        )

        # Directly open employee's own dashboard
        return redirect("my_tasks")

    return render(
        request,
        "tasks/accept_invite.html",
        {
            "form": form,
            "invited_user": person,
        },
    )


@admin_required
@require_POST
def resend_invite(request, pk):
    person = get_object_or_404(
        User,
        pk=pk
    )

    if person.invite_accepted_at:
        messages.info(
            request,
            f"{person.display_name} has already joined."
        )

        return redirect("team")

    if not person.email:
        messages.error(
            request,
            f"{person.display_name} does not have an email address."
        )

        return redirect("team")

    # Updating this creates a fresh expiry window.
    person.invite_sent_at = timezone.now()

    person.save(
        update_fields=[
            "invite_sent_at"
        ]
    )

    notifications.welcome(
        person,
        request.user
    )

    messages.success(
        request,
        f"Invitation resent to {person.email}."
    )

    return redirect("team")


@admin_required
@require_POST
def resend_all_invites(request):
    people = User.objects.filter(
        invite_accepted_at__isnull=True
    ).exclude(
        email=""
    ).exclude(
        pk=request.user.pk
    )

    count = 0

    for person in people:
        person.invite_sent_at = timezone.now()

        person.save(
            update_fields=[
                "invite_sent_at"
            ]
        )

        notifications.welcome(
            person,
            request.user
        )

        count += 1

    messages.success(
        request,
        f"{count} invitation(s) resent."
    )

    return redirect("team")


# ---------------------------------------------------------------- notifications


@login_required
def notification_list(request):
    show = request.GET.get("filter", "all")
    base = request.user.notifications.select_related("actor")
    total = base.count()
    unread = base.filter(is_read=False).count()
    counts = {"all": total, "unread": unread, "read": total - unread}
    if show == "unread":
        qs = base.filter(is_read=False)
    elif show == "read":
        qs = base.filter(is_read=True)
    else:
        show, qs = "all", base
    page = Paginator(qs, 30).get_page(request.GET.get("page"))
    return render(request, "tasks/notifications.html", {"page": page, "filter": show, "counts": counts})


@login_required
def notification_open(request, pk):
    """Mark as read, then jump to the task on the board."""
    n = get_object_or_404(Notification.objects.select_related("task"), pk=pk, recipient=request.user)
    if not n.is_read:
        n.is_read = True
        n.save(update_fields=["is_read"])
    t = n.task
    if t is None:  # task was deleted
        return redirect("notification_list")
    if request.user.is_admin:
        url = reverse("dashboard") + f"?show=custom&mode=single&date={t.due_date:%Y-%m-%d}"
    else:
        url = reverse("my_tasks") + "?show=all"
    return redirect(f"{url}#task-{t.pk}")


@login_required
@require_POST
def notification_toggle(request, pk):
    n = get_object_or_404(Notification, pk=pk, recipient=request.user)
    n.is_read = not n.is_read
    n.save(update_fields=["is_read"])
    return redirect(_safe_next(request, "notification_list"))


@login_required
@require_POST
def notifications_read_all(request):
    count = request.user.notifications.filter(is_read=False).update(is_read=True)
    if count:
        messages.success(request, f"Marked {count} notification{'s' if count != 1 else ''} as read.")
    return redirect(_safe_next(request, "notification_list"))


@login_required
def notifications_count(request):
    return JsonResponse({"unread": request.user.notifications.filter(is_read=False).count()})


# ---------------------------------------------------------------- search


def _highlight(text, q, width=70):
    """A short piece of `text` around the first match of `q`, with the match marked. None if no match."""
    text = text or ""
    i = text.lower().find(q.lower())
    if i < 0:
        return None
    start, end = max(0, i - width), min(len(text), i + len(q) + width)
    before = ("…" if start else "") + text[start:i]
    after = text[i + len(q):end] + ("…" if end < len(text) else "")
    return format_html("{}<mark>{}</mark>{}", before, text[i:i + len(q)], after)


@login_required
def search(request):
    q = request.GET.get("q", "").strip()[:100]
    status = request.GET.get("status", "all")
    results = []
    if q:
        allowed = Task.objects.all()
        if not request.user.is_admin:
            allowed = allowed.filter(Q(assignee=request.user) | Q(watchers=request.user))
        match = (
            Q(title__icontains=q) | Q(details__icontains=q) | Q(member_note__icontains=q)
            | Q(admin_note__icontains=q) | Q(comments__body__icontains=q)
        )
        ids = allowed.filter(match).values("id")
        qs = (
            Task.objects.filter(id__in=ids)
            .select_related("assignee")
            .annotate(comment_count=Count("comments", distinct=True))
            .prefetch_related(Prefetch(
                "comments",
                queryset=TaskComment.objects.filter(body__icontains=q).select_related("author"),
                to_attr="matching_comments",
            ))
            .order_by("-due_date", "-created_at")
        )
        if status == "open":
            qs = qs.exclude(status=S.DONE)
        elif status == "done":
            qs = qs.filter(status=S.DONE)
        else:
            status = "all"
        for t in qs[:100]:
            where, snippet = None, None
            for label, text in (("Brief", t.details), ("Their note", t.member_note), ("Change request", t.admin_note)):
                snippet = _highlight(text, q)
                if snippet:
                    where = label
                    break
            if not snippet and t.matching_comments:
                c = t.matching_comments[0]
                where = f"Comment by {c.author.display_name if c.author else 'someone'}"
                snippet = _highlight(c.body, q)
            results.append({
                "task": t,
                "title": _highlight(t.title, q, width=200) or t.title,
                "where": where,
                "snippet": snippet,
            })
    return render(request, "tasks/search.html", {"q": q, "status": status, "results": results})


# ---------------------------------------------------------------- recurring tasks


@admin_required
def recurring_list(request, pk=None):
    """List of repeating tasks, with the form to add one (or edit one when pk is given)."""
    editing = get_object_or_404(RecurringTask, pk=pk) if pk else None
    form = RecurringTaskForm(request.POST or None, instance=editing)
    if request.method == "POST" and form.is_valid():
        rule = form.save(commit=False)
        if not rule.created_by_id:
            rule.created_by = request.user
        rule.save()
        form.save_m2m()
        # If today is one of its days and today's batch already ran, create today's task now.
        now = timezone.localtime()
        made = 0
        if now.hour >= getattr(settings, "RECURRING_HOUR", 8):
            made = recurring.create_recurring_tasks(now.date(), rules=[rule])
        msg = f'Saved "{rule.title}" · {rule.schedule_label}.'
        if made:
            msg += f" Today's task was created for {made} person{'s' if made != 1 else ''}."
        messages.success(request, msg)
        return redirect("recurring")

    today = timezone.localdate()
    rules = (
        RecurringTask.objects.prefetch_related("assignees").select_related("created_by")
        .annotate(task_count=Count("tasks", distinct=True))
    )
    rows = []
    for r in rules:
        start = today + timedelta(days=1) if r.last_run_on == today else today
        rows.append({"rule": r, "next_run": r.next_run(start) if r.is_active else None})
    return render(request, "tasks/recurring.html", {
        "form": form,
        "rows": rows,
        "editing": editing,
        "today": today,
        "mention_people": _mention_people(),
    })


@admin_required
@require_POST
def recurring_toggle(request, pk):
    rule = get_object_or_404(RecurringTask, pk=pk)
    rule.is_active = not rule.is_active
    rule.save(update_fields=["is_active"])
    messages.success(request, f'{"Resumed" if rule.is_active else "Paused"} "{rule.title}".')
    return redirect("recurring")


@admin_required
@require_POST
def recurring_delete(request, pk):
    rule = get_object_or_404(RecurringTask, pk=pk)
    title = rule.title
    rule.delete()
    messages.success(request, f'Deleted the schedule "{title}". Tasks it already created are kept.')
    return redirect("recurring")