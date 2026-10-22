from datetime import date
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.core.files.base import ContentFile
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import notifications, reports
from .forms import AssignTaskForm, SetPasswordForm, UserCreateForm, UserEditForm
from .models import Task, TaskActivity, TaskAttachment, User

S = Task.Status


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not request.user.is_admin:
            raise PermissionDenied
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


# ---------------------------------------------------------------- home


@login_required
def home(request):
    return redirect("dashboard" if request.user.is_admin else "my_tasks")


# ---------------------------------------------------------------- admin dashboard


@admin_required
def dashboard(request):
    today = timezone.localdate()
    day = _parse_date(request.GET.get("date"), today)
    show = request.GET.get("show", "day")
    member_id = request.GET.get("member", "all")

    qs = Task.objects.select_related("assignee").prefetch_related("attachments")
    if member_id != "all":
        qs = qs.filter(assignee_id=member_id)

    if show == "open":
        qs = qs.exclude(status=S.DONE)
    elif show == "month":
        qs = qs.filter(due_date__year=day.year, due_date__month=day.month)
    else:
        show = "day"
        tasks = list(qs.exclude(status=S.DONE).filter(due_date__lte=day)) if day == today else []
        tasks += list(qs.filter(due_date=day))
        tasks += list(qs.filter(status=S.REVIEW))
        qs = list({t.id: t for t in tasks}.values())

    tasks = list(qs)
    form = AssignTaskForm()
    return render(request, "tasks/dashboard.html", {
        "form": form,
        "board": _board(tasks),
        "stats": _stats(tasks),
        "day": day,
        "show": show,
        "member_id": member_id,
        "members": User.objects.filter(is_active=True),
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
    elif action == "submit" and task.status in (S.TODO, S.PROGRESS, S.CHANGE):
        task.status, task.submitted_at, task.member_note = S.REVIEW, now, note
        task.started_at = task.started_at or now
        task.save()
        _log(task, user, TaskActivity.Action.SUBMITTED, note)
        notifications.task_submitted(task, user)
        messages.success(request, "Submitted for approval.")
    elif action == "approve" and task.status == S.REVIEW:
        task.status, task.done_at, task.approved_by, task.admin_note = S.DONE, now, user, ""
        task.save()
        _log(task, user, TaskActivity.Action.APPROVED)
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
        notifications.task_changes(task, user)
        messages.success(request, "Change request sent.")
    elif action == "delete":
        title = task.title
        task.delete()
        messages.success(request, f'Deleted "{title}".')
    else:
        messages.error(request, "That action isn't possible for this task right now.")
    return redirect(back)


@login_required
def attachment(request, pk):
    """Serve a task file only to the person it's assigned to, and to admins."""
    att = get_object_or_404(TaskAttachment.objects.select_related("task"), pk=pk)
    if not (request.user.is_admin or att.task.assignee_id == request.user.id):
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
    qs = Task.objects.filter(assignee=request.user).select_related("assignee").prefetch_related("attachments")
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


@admin_required
def team(request):
    today = timezone.localdate()
    users = list(User.objects.all())
    month_tasks = list(Task.objects.filter(due_date__year=today.year, due_date__month=today.month))
    rows = [(u, reports.summarize([t for t in month_tasks if t.assignee_id == u.id])) for u in users]
    rows.sort(key=lambda r: (not r[0].is_active, not r[0].is_admin, r[0].display_name.lower()))
    return render(request, "tasks/team.html", {"rows": rows})


@admin_required
def user_add(request):
    form = UserCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        notifications.welcome(user, request.user)
        messages.success(request, f"{user.display_name} added as {user.get_role_display()}. Share their username and password with them.")
        return redirect("team")
    return render(request, "tasks/user_form.html", {"form": form, "title": "Add a person"})


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
