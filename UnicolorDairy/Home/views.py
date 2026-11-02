from datetime import date
from datetime import timedelta
from decimal import Decimal, InvalidOperation
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
    TaskEditForm,
)
from .models import (
    AdminNote, Client, EmailFailure, Leave, LoginAttempt, leaves_managed_by, ExtensionRequest, Notification, RecurringTask, Task, TaskActivity, TaskAttachment,
    TaskComment, TaskRequest, User, active_admins, find_mentioned_users, is_office_closed, on_leave_ids,
    people_managed_by,
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


def manager_required(view):
    """Pages for admins and team leads."""
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not request.user.is_lead:
            if request.headers.get("X-Requested-With") == "fetch":
                raise PermissionDenied
            messages.info(request, "That page is only for team leads and admins, so we brought you to your tasks.")
            return redirect("my_tasks")
        return view(request, *args, **kwargs)

    return wrapper


def tasks_managed_by(user):
    """The tasks a team lead or admin is responsible for."""
    return Task.objects.all() if user.is_admin else Task.objects.filter(assignee__manager=user)


def _with_card_data(qs):
    """Everything a task card needs, in as few queries as possible."""
    return (
        qs.select_related("assignee", "assignee__manager", "client", "assigned_by")
        .prefetch_related("attachments", "collaborators", "subtasks")
        .annotate(comment_count=Count("comments", filter=Q(comments__deleted_at__isnull=True), distinct=True))
    )


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
        "hold": sum(1 for t in tasks if t.status == S.HOLD),
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
    return redirect("dashboard" if request.user.is_lead else "my_tasks")


# ---------------------------------------------------------------- admin dashboard


DASHBOARD_LISTS = {
    "open": "all open tasks",
    "review": "all tasks waiting for approval",
    "overdue": "all overdue tasks",
    "changes": "all tasks with changes requested",
    "hold": "all tasks on hold",
}


@manager_required
def dashboard(request):
    today = timezone.localdate()
    show = request.GET.get("show", "today")
    mode = request.GET.get("mode", "single")
    member_id = request.GET.get("member", "all")
    if not member_id.isdigit():
        member_id = "all"
    client_id = request.GET.get("client", "all")
    if not client_id.isdigit():
        client_id = "all"
    day = _parse_date(request.GET.get("date"), today)
    scope = tasks_managed_by(request.user)

    # Work out which dates (or which status list) to show
    if show in DASHBOARD_LISTS:
        mode = "list"
        start = end = today
    elif show == "week":
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

    qs = _with_card_data(scope)
    if member_id != "all":
        qs = qs.filter(assignee_id=member_id)
    if client_id != "all":
        qs = qs.filter(client_id=client_id)

    if show == "open":
        tasks = list(qs.exclude(status=S.DONE))
    elif show == "review":
        tasks = list(qs.filter(status=S.REVIEW))
    elif show == "overdue":
        tasks = list(qs.filter(status__in=NEEDS_WORK, due_date__lt=today))
    elif show == "changes":
        tasks = list(qs.filter(status=S.CHANGE))
    elif show == "hold":
        tasks = list(qs.filter(status=S.HOLD))
    elif mode == "single":
        # One day: tasks due that day, plus anything waiting for approval.
        # For today, also include older tasks that are still not done.
        tasks = list(qs.exclude(status=S.DONE).filter(due_date__lt=start)) if start == today else []
        tasks += list(qs.filter(due_date=start))
        tasks += list(qs.filter(status=S.REVIEW))
        tasks = list({t.id: t for t in tasks}.values())
    else:
        tasks = list(qs.filter(due_date__range=(start, end)))

    # ---- "Needs your attention" (whole team, not affected by the filters)
    open_all = list(scope.exclude(status=S.DONE).only("status", "due_date", "assignee_id", "estimated_hours"))
    focus = {
        "review": sum(1 for t in open_all if t.status == S.REVIEW),
        "overdue": sum(1 for t in open_all if t.status in NEEDS_WORK and t.due_date < today),
        "today": sum(1 for t in open_all if t.status in NEEDS_WORK and t.due_date == today),
        "changes": sum(1 for t in open_all if t.status == S.CHANGE),
        "hold": sum(1 for t in open_all if t.status == S.HOLD),
        "open": len(open_all),
    }
    queue = list(
        scope.filter(status=S.REVIEW).select_related("assignee", "assignee__manager")
        .prefetch_related("collaborators").order_by("submitted_at", "due_date")[:6]
    )
    members = list(people_managed_by(request.user))
    away = on_leave_ids(today)
    workload = []
    owners = {t.assignee_id for t in open_all}
    gone = list(User.objects.filter(pk__in=owners, is_active=False))
    for u in [*members, *gone]:
        mine = [t for t in open_all if t.assignee_id == u.id]
        if mine or not u.is_admin:
            workload.append({
                "user": u,
                "open": len(mine),
                "hours": sum((t.estimated_hours or 0) for t in mine),
                "overdue": sum(1 for t in mine if t.status in NEEDS_WORK and t.due_date < today),
                "away": u.pk in away,
                "gone": not u.is_active,
            })
    orphaned = [
        {"user": u, "open": sum(1 for t in open_all if t.assignee_id == u.id)} for u in gone
    ] if request.user.is_admin else []
    pending_extensions = list(
        ExtensionRequest.objects.filter(status=ExtensionRequest.Status.PENDING, task__in=scope)
        .select_related("task", "task__assignee", "requested_by")
    )
    request_scope = TaskRequest.objects.filter(status=TaskRequest.Status.PENDING).select_related("requested_by", "client")
    if not request.user.is_admin:
        request_scope = request_scope.filter(requested_by__manager=request.user)
    workload.sort(key=lambda w: (-w["open"], w["user"].display_name.lower()))
    now = timezone.localtime()
    greeting = "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 17 else "Good evening"

    form = AssignTaskForm(user=request.user)
    return render(request, "tasks/dashboard.html", {
        "client_id": client_id,
        "clients": Client.objects.filter(is_active=True),
        "orphaned": orphaned,
        "pending_extensions": pending_extensions,
        "task_requests": list(request_scope),
        "pending_leave_count": leaves_managed_by(request.user).filter(status=Leave.Status.PENDING).count(),
        "form": form,
        "board": _board(tasks),
        "stats": _stats(tasks),
        "show": show,
        "mode": mode,
        "list_label": DASHBOARD_LISTS.get(show, ""),
        "day": start,
        "start": start,
        "end": end,
        "today": today,
        "member_id": member_id,
        "members": members,
        "focus": focus,
        "queue": queue,
        "workload": workload,
        "greeting": greeting,
        "mention_people": _mention_people(),
        "is_admin_view": True,
        "max_upload_mb": getattr(settings, "MAX_UPLOAD_MB", 20),
    })


@manager_required
@require_POST
def assign_task(request):
    form = AssignTaskForm(request.POST, request.FILES, user=request.user)
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
    people = list(data["assignees"])
    collaborators = []
    if data["shared"] and len(people) > 1:
        lead = data["lead"] if data["lead"] and data["lead"] in people else people[0]
        collaborators = [p for p in people if p.pk != lead.pk]
        people = [lead]
    with transaction.atomic():
        for user in people:
            task = Task.objects.create(
                title=data["title"], details=data["details"], priority=data["priority"],
                due_date=data["due_date"], assignee=user, assigned_by=request.user,
                client=data["client"], estimated_hours=data["estimated_hours"],
                is_confidential=data["is_confidential"],
            )
            if collaborators:
                task.collaborators.set(collaborators)
            for name, content, kind in blobs:
                att = TaskAttachment(task=task, kind=kind, original_name=name[:255], size=len(content), uploaded_by=request.user)
                att.file.save(name, ContentFile(content), save=True)
            _log(task, request.user, TaskActivity.Action.ASSIGNED)
            Notification.send(task, request.user, "assigned", recipients=[user, *collaborators])
            tagged = [u for u in find_mentioned_users(task.details) if u.pk not in (user.pk, request.user.pk)]
            tagged = task.grant_tagged_access(request.user, tagged)
            if tagged:
                Notification.send(task, request.user, "mentioned", recipients=tagged)
                transaction.on_commit(lambda t=task, p=tagged: notifications.task_mentioned(t, request.user, p, t.details))
            transaction.on_commit(lambda t=task: notifications.task_assigned(t, request.user))
    everyone = [*people, *collaborators]
    names = ", ".join(u.display_name for u in everyone)
    if collaborators:
        messages.success(request, f'"{data["title"]}" is one shared task for {names}, led by {people[0].display_name}.')
    else:
        messages.success(request, f'"{data["title"]}" assigned to {names}.')
    if is_office_closed(data["due_date"]):
        messages.warning(request, f'{data["due_date"]:%a, %d %b} is a day the office is closed. Check the due date.')
    away = on_leave_ids(data["due_date"])
    on_leave = [u.display_name for u in everyone if u.pk in away]
    if on_leave:
        messages.warning(request, f'On leave on the due date: {", ".join(on_leave)}.')
    return redirect("dashboard")


# ---------------------------------------------------------------- task actions


@login_required
@require_POST
def task_action(request, pk, action):
    task = get_object_or_404(Task.objects.select_related("assignee", "assignee__manager"), pk=pk)
    user = request.user
    note = request.POST.get("note", "").strip()[:1000]
    now = timezone.now()
    back = request.POST.get("next", "")
    if not back.startswith("/") or back.startswith("//"):
        back = "home"  # only allow same-site redirects

    member_actions = {"start", "submit", "hold", "resume"}
    manager_actions = {"approve", "changes", "reopen", "delete"}

    if action in member_actions and not task.can_work(user):
        raise PermissionDenied
    if action in manager_actions and not task.can_manage(user):
        raise PermissionDenied

    if action == "start" and task.status == S.TODO:
        task.status, task.started_at = S.PROGRESS, now
        task.save()
        _log(task, user, TaskActivity.Action.STARTED)
        Notification.send(task, user, "started")
    elif action == "submit" and task.status in (S.TODO, S.PROGRESS, S.CHANGE):
        task.status, task.submitted_at, task.member_note = S.REVIEW, now, note
        task.started_at = task.started_at or now
        task.actual_hours = _parse_hours(request.POST.get("actual_hours"), task.actual_hours)
        task.save()
        _log(task, user, TaskActivity.Action.SUBMITTED, note)
        Notification.send(task, user, "submitted", note)
        notifications.task_submitted(task, user)
        messages.success(request, "Submitted for approval.")
    elif action == "approve" and task.status == S.REVIEW and not task.can_approve(user):
        messages.error(request, "You can't approve your own work. Another admin or your team lead needs to approve it.")
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
    elif action == "hold" and task.status in NEEDS_WORK:
        kind = request.POST.get("hold_kind")
        if kind not in Task.HoldKind.values or not note:
            messages.error(request, "Please choose why it's on hold and add a short note.")
            return redirect(back)
        task.status, task.hold_kind, task.hold_note, task.hold_since = S.HOLD, kind, note[:500], now
        task.save()
        summary = f"{task.get_hold_kind_display()}: {note}"
        _log(task, user, TaskActivity.Action.HOLD, summary)
        Notification.send(task, user, "hold", summary)
        messages.success(request, "Put on hold. It won't count as overdue until it's resumed.")
    elif action == "resume" and task.status == S.HOLD:
        waited = (timezone.localdate() - timezone.localtime(task.hold_since).date()).days if task.hold_since else 0
        task.status, task.hold_kind, task.hold_note, task.hold_since = S.PROGRESS, "", "", None
        task.started_at = task.started_at or now
        task.save()
        _log(task, user, TaskActivity.Action.RESUMED, f"After {waited} day{'s' if waited != 1 else ''} on hold")
        Notification.send(task, user, "resumed")
        if task.is_late:
            messages.warning(request, "Resumed, but the due date has passed. Ask for more time if the delay wasn't your team's.")
        else:
            messages.success(request, "Resumed.")
    elif action == "delete":
        title = task.title
        task.archived_at, task.archived_by = now, user
        task.save(update_fields=["archived_at", "archived_by"])
        _log(task, user, TaskActivity.Action.ARCHIVED, note)
        Notification.send(task, user, "deleted", note)
        messages.success(request, f'Archived "{title}". You can restore it from the Archive.')
    else:
        messages.error(request, "That action isn't possible for this task right now.")
    return redirect(back)


def _parse_hours(value, default=None):
    try:
        hours = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return default
    return hours.quantize(Decimal("0.1")) if 0 <= hours <= 999 else default


@login_required
def task_detail(request, pk):
    """Full details of one task, shown in the pop-up on the board."""
    task = get_object_or_404(
        Task.objects.select_related("assignee", "assignee__manager", "assigned_by", "approved_by", "client")
        .prefetch_related("attachments", "collaborators", "subtasks", "watchers"), pk=pk
    )
    if not task.can_view(request.user):
        raise PermissionDenied
    back = request.GET.get("next", "")
    if not back.startswith("/") or back.startswith("//"):
        back = reverse("home")
    return render(request, "tasks/_task_detail.html", {
        "t": task,
        "activity": list(task.activity.select_related("actor")),
        "comments": list(task.comments.select_related("author", "deleted_by")),
        "people": _mention_people(),
        "next": back,
        "reopen_next": f"{back.split('#')[0]}#task-{task.pk}",
        "admin_view": task.can_manage(request.user),
        "can_work": task.can_work(request.user),
        "is_worker": task.is_worker(request.user),
        "can_approve": task.can_approve(request.user),
        "can_extend": task.status != S.DONE and task.can_work(request.user),
        "pending_extension": next(iter(task.extensions.filter(status=ExtensionRequest.Status.PENDING)), None),
        "extensions": list(task.extensions.select_related("requested_by", "decided_by")),
        "hold_kinds": Task.HoldKind.choices,
        "max_upload_mb": getattr(settings, "MAX_UPLOAD_MB", 20),
        "min_extension_date": timezone.localdate().isoformat(),
        "needs_work": task.status in NEEDS_WORK,
        "away_on_due": task.assignee_id in on_leave_ids(task.due_date),
    })


@manager_required
def task_edit(request, pk):
    """Edit a task after it was assigned (shown inside the pop-up)."""
    task = get_object_or_404(Task.objects.select_related("assignee", "assignee__manager"), pk=pk)
    if not task.can_manage(request.user):
        raise PermissionDenied
    before = Task.objects.select_related("assignee", "client").get(pk=pk)  # untouched copy, to describe what changed
    before_people = set(before.collaborators.values_list("pk", flat=True))
    back = _safe_next(request, reverse("dashboard"))
    form = TaskEditForm(request.POST or None, instance=task, user=request.user)

    if request.method == "POST":
        if not form.is_valid():
            for errors in form.errors.values():
                for e in errors:
                    messages.error(request, e)
            return redirect(back)
        if not form.has_changed():
            messages.info(request, "Nothing was changed.")
            return redirect(back)

        task = form.save()
        task.collaborators.remove(task.assignee)
        today = timezone.localdate()
        if "due_date" in form.changed_data and before.status != S.DONE and before.scoring_due_date >= today:
            # Rescheduling before the deadline passed is a real new plan.
            # After it has passed, only an approved extension changes how the task is scored.
            task.score_due_date = task.due_date
            task.save(update_fields=["score_due_date"])
        if task.is_confidential and not before.is_confidential:
            task.watchers.clear()
        changes = []
        for f in form.changed_data:
            if f == "title":
                changes.append(f"Title “{before.title}” → “{task.title}”")
            elif f == "priority":
                changes.append(f"Priority {before.get_priority_display()} → {task.get_priority_display()}")
            elif f == "due_date":
                changes.append(f"Due date {before.due_date:%d %b} → {task.due_date:%d %b}")
            elif f == "assignee":
                changes.append(f"Reassigned {before.assignee.display_name} → {task.assignee.display_name}")
            elif f == "details":
                changes.append("Brief updated")
            elif f == "client":
                changes.append(f"Client {before.client.name if before.client else 'none'} → {task.client.name if task.client else 'none'}")
            elif f == "estimated_hours":
                changes.append(f"Estimate {before.estimated_hours or '—'}h → {task.estimated_hours or '—'}h")
            elif f == "is_confidential":
                changes.append("Marked confidential" if task.is_confidential else "No longer confidential")
            elif f == "collaborators":
                names = ", ".join(p.display_name for p in task.collaborators.all()) or "nobody"
                changes.append(f"Now working with {names}")
        note = "; ".join(changes)
        _log(task, request.user, TaskActivity.Action.EDITED, note)

        reassigned = before.assignee_id != task.assignee_id
        if reassigned:
            Notification.send(task, request.user, "assigned", recipients=[task.assignee])
            transaction.on_commit(lambda: notifications.task_assigned(task, request.user))
            others = [before.assignee, *task.overseers()]
        else:
            others = [task.assignee, *task.overseers()]
        Notification.send(task, request.user, "edited", note, recipients=[*others, *task.collaborators.all()])
        new_people = [p for p in task.collaborators.all() if p.pk not in before_people]
        if new_people:
            Notification.send(task, request.user, "assigned", recipients=new_people)

        if "details" in form.changed_data:
            already = {u.pk for u in task.watchers.all()}
            tagged = [u for u in find_mentioned_users(task.details)
                      if u.pk not in already and u.pk not in (task.assignee_id, request.user.pk)]
            tagged = task.grant_tagged_access(request.user, tagged)
            if tagged:
                Notification.send(task, request.user, "mentioned", recipients=tagged)
                notifications.task_mentioned(task, request.user, tagged, task.details)

        messages.success(request, f'Updated "{task.title}": {note}.')
        return redirect(back)

    return render(request, "tasks/_task_edit.html", {
        "task": task,
        "form": form,
        "next": back,
        "people": _mention_people(),
    })


def _comments_response(request, task):
    """Send back the refreshed comments list (used by the pop-up)."""
    if request.headers.get("X-Requested-With") != "fetch":
        return redirect(_safe_next(request, "home"))
    return render(request, "tasks/_comments.html", {
        "t": task,
        "comments": list(task.comments.select_related("author", "deleted_by")),
    })


@login_required
@require_POST
def task_comment(request, pk):
    task = get_object_or_404(Task.objects.select_related("assignee", "assignee__manager"), pk=pk)
    if not task.can_view(request.user):
        raise PermissionDenied
    body = request.POST.get("body", "").strip()[:2000]
    if body:
        comment = TaskComment.objects.create(task=task, author=request.user, body=body)
        tagged = [u for u in find_mentioned_users(body) if u.pk != request.user.pk]
        tagged = task.grant_tagged_access(request.user, tagged)
        if tagged:
            comment.mentions.set(tagged)
            Notification.send(task, request.user, "mentioned", body, recipients=tagged)
            notifications.task_mentioned(task, request.user, tagged, body)
        # Everyone else involved hears about the new comment.
        tagged_ids = {u.pk for u in tagged}
        commenters = User.objects.filter(pk__in=task.comments.values("author"))
        involved = [task.assignee, *task.collaborators.all(), *task.overseers(), *task.watchers.all(), *commenters]
        involved = [p for p in involved if task.can_view(p)]
        Notification.send(task, request.user, "commented", body,
                          recipients=[p for p in involved if p.pk not in tagged_ids])
    return _comments_response(request, task)


@login_required
@require_POST
def task_comment_delete(request, pk):
    comment = get_object_or_404(TaskComment.objects.select_related("task", "task__assignee"), pk=pk)
    task = comment.task
    if not (task.can_manage(request.user) or comment.author_id == request.user.id):
        raise PermissionDenied
    if not comment.deleted_at:
        comment.deleted_at, comment.deleted_by = timezone.now(), request.user
        comment.save(update_fields=["deleted_at", "deleted_by"])
        who = comment.author.display_name if comment.author else "someone"
        _log(task, request.user, TaskActivity.Action.COMMENT_DEL, f"{who}'s comment from {timezone.localtime(comment.created_at):%d %b, %I:%M %p}: {comment.body[:500]}")
    return _comments_response(request, task)


@login_required
def attachment(request, pk):
    """Serve a task file only to the person it's assigned to, and to admins."""
    att = get_object_or_404(TaskAttachment.objects.select_related("task", "task__assignee"), pk=pk)
    if not att.task.can_view(request.user) or (att.task.archived_at and not request.user.is_admin):
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


NEEDS_WORK = (S.TODO, S.PROGRESS, S.CHANGE)

MY_FILTERS = [
    ("open", "Open tasks"),
    ("today", "Due today"),
    ("week", "Due this week"),
    ("overdue", "Overdue"),
    ("changes", "Changes requested"),
    ("hold", "On hold"),
    ("waiting", "Waiting for approval"),
    ("tagged", "Tagged on"),
    ("month", "This month"),
    ("all", "Everything"),
]


def _due_label(t, today):
    if t.status == S.DONE:
        return "Done"
    days = (t.due_date - today).days
    if days < 0:
        return f"Overdue · {-days} day{'s' if days != -1 else ''}"
    if days == 0:
        return "Due today"
    if days == 1:
        return "Due tomorrow"
    if days < 7:
        return f"Due {t.due_date:%a, %d %b}"
    return f"Due {t.due_date:%d %b}"


def _agenda(tasks, today):
    """Group tasks by deadline for the list view."""
    week_end = today + timedelta(days=6 - today.weekday())
    groups = [
        ("overdue", "Overdue", []), ("today", "Today", []), ("tomorrow", "Tomorrow", []),
        ("week", "Later this week", []), ("later", "Later", []), ("done", "Done", []),
    ]
    by_key = {k: items for k, _, items in groups}
    for t in tasks:
        t.due_label = _due_label(t, today)
        if t.status == S.DONE:
            by_key["done"].append(t)
        elif t.due_date < today:
            by_key["overdue"].append(t)
        elif t.due_date == today:
            by_key["today"].append(t)
        elif t.due_date == today + timedelta(days=1):
            by_key["tomorrow"].append(t)
        elif t.due_date <= week_end:
            by_key["week"].append(t)
        else:
            by_key["later"].append(t)
    return [{"key": k, "label": label, "tasks": _sorted(items)} for k, label, items in groups if items]


@login_required
def my_tasks(request):
    user = request.user
    if user.is_admin:
        # Admins get their personal checklist here instead of task counts.
        note, _ = AdminNote.objects.get_or_create(user=user)
        return render(request, "tasks/admin_checklist.html", {"note": note})
    show = request.GET.get("show", "open")
    now = timezone.localtime()
    today = now.date()
    week_end = today + timedelta(days=6 - today.weekday())

    base = _with_card_data(Task.objects.all())
    mine_ids = Task.objects.filter(Q(assignee=user) | Q(collaborators=user)).values("pk")
    mine = base.filter(pk__in=mine_ids)

    if show == "today":
        qs = mine.filter(due_date=today)
    elif show == "week":
        qs = mine.filter(due_date__range=(today, week_end))
    elif show == "overdue":
        qs = mine.filter(status__in=NEEDS_WORK, due_date__lt=today)
    elif show == "changes":
        qs = mine.filter(status=S.CHANGE)
    elif show == "waiting":
        qs = mine.filter(status=S.REVIEW)
    elif show == "hold":
        qs = mine.filter(status=S.HOLD)
    elif show == "tagged":
        qs = base.filter(watchers=user).exclude(assignee=user).distinct()
    elif show == "month":
        qs = mine.filter(due_date__year=today.year, due_date__month=today.month)
    elif show == "all":
        qs = mine
    else:
        show = "open"
        qs = [t for t in mine if t.status != S.DONE or t.done_date == today]
    tasks = list(qs)

    # "Needs your attention" numbers – always about your own open work.
    open_mine = list(Task.objects.filter(pk__in=mine_ids).exclude(status=S.DONE).only("status", "due_date"))
    focus = {
        "overdue": sum(1 for t in open_mine if t.status in NEEDS_WORK and t.due_date < today),
        "today": sum(1 for t in open_mine if t.status in NEEDS_WORK and t.due_date == today),
        "week": sum(1 for t in open_mine if t.status in NEEDS_WORK and today <= t.due_date <= week_end),
        "changes": sum(1 for t in open_mine if t.status == S.CHANGE),
        "waiting": sum(1 for t in open_mine if t.status == S.REVIEW),
        "hold": sum(1 for t in open_mine if t.status == S.HOLD),
        "tagged": Task.objects.filter(watchers=user).exclude(assignee=user).exclude(status=S.DONE).distinct().count(),
    }
    month_summary = reports.summarize(
        Task.objects.filter(assignee=user, due_date__year=today.year, due_date__month=today.month)
    )
    greeting = "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 17 else "Good evening"

    return render(request, "tasks/my_tasks.html", {
        "board": _board(tasks),
        "stats": _stats(tasks),
        "agenda": _agenda(tasks, today),
        "show": show,
        "filters": MY_FILTERS,
        "focus": focus,
        "month_summary": month_summary,
        "greeting": greeting,
        "today": today,
        "is_tagged_view": show == "tagged",
    })


@admin_required
@require_POST
def save_admin_note(request):
    """Autosave for the admin's checklist (called by the page's JavaScript)."""
    content = request.POST.get("content", "")
    if len(content) > 500_000:
        return JsonResponse({"error": "Checklist is too long"}, status=400)
    note, _ = AdminNote.objects.get_or_create(user=request.user)
    note.content = content
    note.save()
    saved_at = timezone.localtime(note.updated_at).strftime("%d %b %Y, %I:%M %p")
    return JsonResponse({"ok": True, "saved_at": saved_at})


# ---------------------------------------------------------------- reports


def _report_scope(request):
    """The period, member and tasks the report (and its CSV) covers."""
    period = reports.parse_period(request.GET)
    user = request.user
    member_id = request.GET.get("member", "all")
    if member_id != "all" and not member_id.isdigit():
        member_id = "all"
    qs = Task.objects.select_related("assignee", "approved_by", "client").prefetch_related("collaborators")
    if not user.is_lead:
        member_id = str(user.id)
    elif not user.is_admin:
        allowed = {str(pk) for pk in user.reports.values_list("pk", flat=True)} | {str(user.pk)}
        if member_id not in allowed:
            member_id = "all"
        qs = qs.filter(Q(assignee__manager=user) | Q(assignee=user))
    if member_id != "all":
        qs = qs.filter(assignee_id=member_id)
    client_id = request.GET.get("client", "all")
    if client_id.isdigit():
        qs = qs.filter(client_id=client_id)
    tasks = list(qs.filter(due_date__range=(period["start"], period["end"])))
    prev = list(qs.filter(due_date__range=(period["prev_start"], period["prev_end"])))
    return period, member_id, tasks, prev


@login_required
def report(request):
    period, member_id, tasks, prev_tasks = _report_scope(request)
    all_days = reports.days_between(period["start"], period["end"])
    grid_days = all_days if len(all_days) <= reports.MAX_GRID_DAYS else []
    members = User.objects.all() if request.user.is_admin else User.objects.filter(Q(manager=request.user) | Q(pk=request.user.pk))
    if member_id == "all":
        ids = {t.assignee_id for t in tasks}
        users = [u for u in members if u.is_active or u.id in ids]
    else:
        users = [get_object_or_404(User, pk=member_id)]
    summary = reports.summarize(tasks)
    prev = reports.summarize(prev_tasks)
    return render(request, "tasks/report.html", {
        "period": period,
        "month": period["month"],
        "member_id": member_id,
        "members": members,
        "clients": Client.objects.all(),
        "client_id": request.GET.get("client", "all"),
        "days": grid_days,
        "summary": summary,
        "prev": prev,
        "deltas": {
            "pct": reports.delta(summary.pct if summary.assigned else None, prev.pct if prev.assigned else None),
            "on_time": reports.delta(summary.on_time_pct, prev.on_time_pct),
            "first_pass": reports.delta(summary.first_pass_pct, prev.first_pass_pct),
        },
        "breakdown": reports.status_breakdown(tasks),
        "daily": reports.daily_counts(tasks, grid_days) if grid_days else [],
        "rows": reports.build_grid(users, tasks, grid_days),
        "log": reports.day_log(tasks, all_days),
        "single": member_id != "all",
        "selected_user": users[0] if member_id != "all" else None,
        "query": request.GET.urlencode(),
        "max_grid_days": reports.MAX_GRID_DAYS,
    })


@login_required
def report_csv(request):
    period, member_id, tasks, _ = _report_scope(request)
    tasks.sort(key=lambda t: (t.due_date, t.assignee.display_name))
    suffix = "" if member_id == "all" else f"-{tasks[0].assignee.username}" if tasks else ""
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    name = f"unicolors-report-{period['start']:%Y-%m-%d}-to-{period['end']:%Y-%m-%d}{suffix}.csv"
    response["Content-Disposition"] = f'attachment; filename="{name}"'
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
    with_tasks = set(Task.all_objects.values_list("assignee_id", flat=True).distinct())
    away = on_leave_ids(today)

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
            "overdue": sum(1 for t in mine if t.is_late),
            "has_tasks": u.id in with_tasks,
            "can_manage": request.user.can_manage_person(u),
            "away": u.pk in away,
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
        "email_failures": list(EmailFailure.objects.all()[:5]),
        "email_failure_count": EmailFailure.objects.count(),
    })


@admin_required
@require_POST
def user_toggle_active(request, pk):
    """Deactivate (block login) or reactivate a person."""
    person = get_object_or_404(User, pk=pk)
    if not request.user.can_manage_person(person):
        raise PermissionDenied
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
        messages.success(request, f"{person.display_name} can no longer log in.")
        if needs_handover(person):
            return redirect("handover", pk=person.pk)
    return redirect("team")


@admin_required
@require_POST
def user_cancel_invite(request, pk):
    """Remove someone who was invited but never joined (and has no tasks)."""
    person = get_object_or_404(User, pk=pk)
    if not request.user.can_manage_person(person):
        raise PermissionDenied
    if person == request.user or person.invite_accepted_at or Task.all_objects.filter(assignee=person).exists():
        messages.error(request, "This invitation can't be cancelled because the person has joined or has tasks. Deactivate them instead.")
        return redirect("team")
    name, email = person.display_name, person.email
    person.delete()
    messages.success(request, f"Cancelled the invitation for {name}{f' ({email})' if email else ''}.")
    return redirect("team")


def _invite_result(request, person, sent):
    """Tell the admin what really happened with the email."""
    if sent:
        messages.success(request, f"Invitation sent to {person.email}.")
    else:
        messages.error(request, f"Couldn't email the invitation to {person.email}. Check the address, then use Resend invite.")


def needs_handover(person):
    return (
        Task.objects.filter(assignee=person).exclude(status=S.DONE).exists()
        or person.recurring_tasks.exists()
        or person.reports.exists()
    )


@admin_required
def user_add(request):
    form = UserCreateForm(request.POST or None, actor=request.user)

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

        _invite_result(request, user, notifications.welcome(user, request.user))

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
    if not request.user.can_manage_person(person):
        raise PermissionDenied
    form = UserEditForm(request.POST or None, instance=person, actor=request.user)
    if request.method == "POST" and form.is_valid():
        if person == request.user and (not form.cleaned_data["is_active"] or form.cleaned_data["role"] != User.Role.ADMIN):
            messages.error(request, "You can't remove your own admin access. Ask another admin to do it.")
        else:
            form.save()
            messages.success(request, f"Saved {person.display_name}.")
            if not person.is_active and needs_handover(person):
                return redirect("handover", pk=person.pk)
            return redirect("team")
    return render(request, "tasks/user_form.html", {"form": form, "title": f"Edit {person.display_name}", "person": person})


@admin_required
def user_password(request, pk):
    person = get_object_or_404(User, pk=pk)
    if not request.user.can_manage_person(person):
        raise PermissionDenied
    form = SetPasswordForm(person, request.POST or None)
    if request.method == "POST" and form.is_valid():
        person.set_password(form.cleaned_data["password"])
        person.must_change_password = person != request.user
        person.save()
        LoginAttempt.objects.filter(key=f"u:{person.username.lower()}").delete()
        if person.email:
            LoginAttempt.objects.filter(key=f"u:{person.email.lower()}").delete()
        messages.success(request, f"New password set for {person.display_name}. They'll be asked to choose their own when they log in.")
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

    if not request.user.can_manage_person(person):
        raise PermissionDenied

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

    _invite_result(request, person, notifications.welcome(person, request.user))

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
    failed = []

    for person in people:
        if not request.user.can_manage_person(person):
            continue

        person.invite_sent_at = timezone.now()

        person.save(
            update_fields=[
                "invite_sent_at"
            ]
        )

        if notifications.welcome(person, request.user):
            count += 1
        else:
            failed.append(person.display_name)

    if count:
        messages.success(request, f"{count} invitation(s) resent.")
    if failed:
        messages.error(request, f"Couldn't email: {', '.join(failed)}. See the email problems on this page.")

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
    if n.verb == Notification.Verb.LEAVE:
        mine = n.message.startswith("Your leave") or not request.user.is_lead
        return redirect("my_leave" if mine else "leave_approvals")
    t = n.task
    if t is None or (t.archived_at and not request.user.is_admin):  # task was removed
        return redirect("notification_list")
    if t.archived_at:
        return redirect("archive")
    if t.can_manage(request.user):
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
            allowed = allowed.filter(
                Q(assignee=request.user) | Q(watchers=request.user) | Q(collaborators=request.user)
                | Q(assignee__manager=request.user)
            )
        match = (
            Q(title__icontains=q) | Q(details__icontains=q) | Q(member_note__icontains=q)
            | Q(admin_note__icontains=q) | Q(client__name__icontains=q)
            | Q(comments__body__icontains=q, comments__deleted_at__isnull=True)
        )
        ids = allowed.filter(match).values("id")
        qs = (
            Task.objects.filter(id__in=ids)
            .select_related("assignee", "client")
            .annotate(comment_count=Count("comments", filter=Q(comments__deleted_at__isnull=True), distinct=True))
            .prefetch_related(Prefetch(
                "comments",
                queryset=TaskComment.objects.filter(body__icontains=q, deleted_at__isnull=True).select_related("author"),
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
            made = recurring.create_recurring_tasks(now.date(), rules=[rule], only_today=True)
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
    if rule.is_active:
        # Don't create the tasks for the days it was deliberately paused.
        rule.last_run_on = timezone.localdate() - timedelta(days=1)
    rule.save(update_fields=["is_active", "last_run_on"])
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