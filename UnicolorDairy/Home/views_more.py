"""Pages added for day-to-day corporate use: clients, leave and holidays, archive, extensions,
member uploads, checklists, task requests and handovers."""
import os
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import notifications, reports
from .forms import ClientForm, HolidayForm, LeaveForm, MemberTaskForm, MultipleFileField, _check_size
from .models import (
    Client, EmailFailure, ExtensionRequest, Holiday, Leave, Notification, Subtask, Task, TaskActivity,
    TaskAttachment, TaskRequest, User, active_admins, is_office_closed, people_managed_by,
)
from .views import S, _log, _safe_next, _parse_date, admin_required, manager_required, needs_handover

CARD_RELATED = ("assignee", "assignee__manager")


def _get_task(pk):
    return get_object_or_404(Task.objects.select_related(*CARD_RELATED), pk=pk)


def _back(request, task=None):
    """Return to the page, reopening the task's pop-up."""
    default = reverse("home")
    return _safe_next(request, f"{default}#task-{task.pk}" if task else default)


def healthz(request):
    """A simple check that the site is up, and which version is running."""
    return JsonResponse({"ok": True, "version": os.environ.get("RENDER_GIT_COMMIT", "")[:7]})


# ---------------------------------------------------------------- files from the person doing the work


@login_required
@require_POST
def task_upload(request, pk):
    task = _get_task(pk)
    if not task.can_work(request.user):
        raise PermissionDenied
    try:
        files = MultipleFileField(required=False).clean(request.FILES.getlist("files"))
        for f in files:
            _check_size(f)
    except Exception as err:  # a too-large file: show why, keep everything else
        messages.error(request, "; ".join(getattr(err, "messages", [str(err)])))
        return redirect(_back(request, task))
    if not files:
        messages.error(request, "Choose at least one file.")
        return redirect(_back(request, task))
    if len(files) > 10:
        messages.error(request, "You can add up to 10 files at a time.")
        return redirect(_back(request, task))
    kind = TaskAttachment.Kind.WORK if task.is_worker(request.user) else TaskAttachment.Kind.FILE
    for f in files:
        att = TaskAttachment(task=task, kind=kind, original_name=f.name[:255], size=f.size, uploaded_by=request.user)
        att.file.save(f.name, f, save=True)
    names = ", ".join(f.name for f in files)
    _log(task, request.user, TaskActivity.Action.FILE, names[:500])
    Notification.send(task, request.user, "file", names)
    messages.success(request, f"Added {len(files)} file{'s' if len(files) != 1 else ''}.")
    return redirect(_back(request, task))


# ---------------------------------------------------------------- checklist


@login_required
@require_POST
def subtask_add(request, pk):
    task = _get_task(pk)
    if not task.can_work(request.user):
        raise PermissionDenied
    title = request.POST.get("title", "").strip()[:200]
    if title:
        Subtask.objects.create(task=task, title=title)
    return redirect(_back(request, task))


@login_required
@require_POST
def subtask_toggle(request, pk):
    item = get_object_or_404(Subtask.objects.select_related("task", "task__assignee", "task__assignee__manager"), pk=pk)
    if not item.task.can_work(request.user) or item.task.archived_at:
        raise PermissionDenied
    item.is_done = not item.is_done
    item.done_by, item.done_at = (request.user, timezone.now()) if item.is_done else (None, None)
    item.save()
    return redirect(_back(request, item.task))


@login_required
@require_POST
def subtask_delete(request, pk):
    item = get_object_or_404(Subtask.objects.select_related("task", "task__assignee", "task__assignee__manager"), pk=pk)
    task = item.task
    if not task.can_work(request.user) or task.archived_at:
        raise PermissionDenied
    item.delete()
    return redirect(_back(request, task))


# ---------------------------------------------------------------- who can see a task


@login_required
@require_POST
def watcher_remove(request, pk, user_pk):
    task = _get_task(pk)
    if not task.can_manage(request.user):
        raise PermissionDenied
    person = get_object_or_404(User, pk=user_pk)
    task.watchers.remove(person)
    _log(task, request.user, TaskActivity.Action.EDITED, f"Removed {person.display_name}'s access")
    messages.success(request, f"{person.display_name} can no longer see this task.")
    return redirect(_back(request, task))


# ---------------------------------------------------------------- more time


def _apply_extension(ext, user, note=""):
    task = ext.task
    ext.status, ext.decided_by, ext.decided_at, ext.decision_note = ExtensionRequest.Status.APPROVED, user, timezone.now(), note[:500]
    ext.save()
    task.due_date = task.score_due_date = ext.requested_date
    task.save(update_fields=["due_date", "score_due_date", "updated_at"])
    text = f"{ext.previous_date:%d %b} → {ext.requested_date:%d %b %Y}: {ext.reason[:300]}"
    _log(task, user, TaskActivity.Action.EXT_OK, text)
    Notification.send(task, user, "extension", f"New deadline {ext.requested_date:%d %b}", recipients=[task.assignee])
    notifications.extension_decided(ext)


@login_required
@require_POST
def extension_request(request, pk):
    task = _get_task(pk)
    user = request.user
    if not task.can_work(user):
        raise PermissionDenied
    back = _back(request, task)
    new_date = _parse_date(request.POST.get("requested_date"), None)
    reason = request.POST.get("reason", "").strip()[:1000]
    if task.status == S.DONE:
        messages.error(request, "This task is already done.")
    elif not new_date or not reason:
        messages.error(request, "Please pick the new date and give a reason.")
    elif new_date < timezone.localdate() or new_date == task.due_date:
        messages.error(request, "Pick a new date from today onwards that's different from the current one.")
    elif task.extensions.filter(status=ExtensionRequest.Status.PENDING).exists():
        messages.error(request, "There's already a request waiting for a decision.")
    else:
        ext = ExtensionRequest.objects.create(
            task=task, requested_by=user, previous_date=task.due_date, requested_date=new_date, reason=reason,
        )
        if task.can_approve(user):
            _apply_extension(ext, user)
            messages.success(request, f"Deadline moved to {new_date:%d %b}.")
        else:
            _log(task, user, TaskActivity.Action.EXT_REQUEST, f"{task.due_date:%d %b} → {new_date:%d %b %Y}: {reason}")
            Notification.send(task, user, "extension", f"to {new_date:%d %b}: {reason}", recipients=task.overseers())
            notifications.extension_requested(ext)
            messages.success(request, "Request sent. The deadline stays the same until it's approved.")
    return redirect(back)


@login_required
@require_POST
def extension_decide(request, pk, decision):
    ext = get_object_or_404(
        ExtensionRequest.objects.select_related("task", "task__assignee", "task__assignee__manager"), pk=pk,
    )
    task = ext.task
    if not task.can_approve(request.user):
        raise PermissionDenied
    back = _back(request, task)
    if ext.status != ExtensionRequest.Status.PENDING:
        messages.info(request, "That request was already decided.")
    elif decision == "approve":
        _apply_extension(ext, request.user, request.POST.get("note", "").strip())
        messages.success(request, f"Deadline moved to {ext.requested_date:%d %b}.")
    elif decision == "decline":
        note = request.POST.get("note", "").strip()[:500]
        ext.status, ext.decided_by, ext.decided_at, ext.decision_note = ExtensionRequest.Status.DECLINED, request.user, timezone.now(), note
        ext.save()
        _log(task, request.user, TaskActivity.Action.EXT_NO, note)
        Notification.send(task, request.user, "extension", f"Declined. {note}".strip(), recipients=[task.assignee])
        notifications.extension_decided(ext)
        messages.success(request, "Request declined.")
    return redirect(back)


# ---------------------------------------------------------------- archive


@admin_required
def archive(request):
    tasks = (
        Task.all_objects.filter(archived_at__isnull=False)
        .select_related("assignee", "archived_by", "client").order_by("-archived_at")[:200]
    )
    return render(request, "tasks/archive.html", {"tasks": tasks})


@admin_required
@require_POST
def archive_restore(request, pk):
    task = get_object_or_404(Task.all_objects.select_related(*CARD_RELATED), pk=pk, archived_at__isnull=False)
    task.archived_at, task.archived_by = None, None
    task.save(update_fields=["archived_at", "archived_by"])
    _log(task, request.user, TaskActivity.Action.RESTORED)
    Notification.send(task, request.user, "restored")
    messages.success(request, f'Restored "{task.title}".')
    return redirect("archive")


# ---------------------------------------------------------------- clients


@manager_required
def clients(request):
    form = ClientForm(request.POST or None)
    if request.method == "POST":
        if not request.user.is_admin:
            raise PermissionDenied
        if form.is_valid():
            client = form.save()
            messages.success(request, f"Added {client.name}.")
            return redirect("clients")
    today = timezone.localdate()
    tasks = list(
        Task.objects.filter(client__isnull=False, due_date__year=today.year, due_date__month=today.month)
    )
    open_counts, overdue_counts = {}, {}
    for t in Task.objects.filter(client__isnull=False).exclude(status=S.DONE).only("client_id", "status", "due_date"):
        open_counts[t.client_id] = open_counts.get(t.client_id, 0) + 1
        if t.is_late:
            overdue_counts[t.client_id] = overdue_counts.get(t.client_id, 0) + 1
    rows = [
        {
            "client": c,
            "summary": reports.summarize([t for t in tasks if t.client_id == c.pk]),
            "open": open_counts.get(c.pk, 0),
            "overdue": overdue_counts.get(c.pk, 0),
        }
        for c in Client.objects.all().order_by("-is_active", "name")
    ]
    return render(request, "tasks/clients.html", {"form": form, "rows": rows, "month": today})


@admin_required
@require_POST
def client_update(request, pk):
    client = get_object_or_404(Client, pk=pk)
    if "toggle" in request.POST:
        client.is_active = not client.is_active
        client.save(update_fields=["is_active"])
        messages.success(request, f"{client.name} is now {'active' if client.is_active else 'hidden from new tasks'}.")
        return redirect("clients")
    form = ClientForm(request.POST, instance=client)
    if form.is_valid():
        form.save()
        messages.success(request, f"Renamed to {client.name}.")
    else:
        for errors in form.errors.values():
            for e in errors:
                messages.error(request, e)
    return redirect("clients")


# ---------------------------------------------------------------- leave & holidays


@admin_required
def calendar_page(request):
    today = timezone.localdate()
    holiday_form = HolidayForm(request.POST if request.POST.get("kind") == "holiday" else None)
    leave_form = LeaveForm(request.POST if request.POST.get("kind") == "leave" else None)
    if request.method == "POST":
        if request.POST.get("kind") == "holiday" and holiday_form.is_valid():
            holiday = holiday_form.save()
            messages.success(request, f"Added {holiday.name}.")
            return redirect("calendar")
        if request.POST.get("kind") == "leave" and leave_form.is_valid():
            leave = leave_form.save(commit=False)
            leave.added_by = request.user
            leave.save()
            count = Task.objects.filter(
                assignee=leave.user, status__in=Task.NEEDS_WORK, due_date__range=(leave.start_date, leave.end_date),
            ).count()
            messages.success(request, f"Added leave for {leave.user.display_name}.")
            if count:
                messages.warning(request, f"{leave.user.display_name} has {count} task{'s' if count != 1 else ''} due during this leave. Reassign them or move the dates.")
            return redirect("calendar")
    return render(request, "tasks/calendar.html", {
        "holiday_form": holiday_form,
        "leave_form": leave_form,
        "holidays": Holiday.objects.filter(date__gte=today - timedelta(days=30)),
        "leaves": Leave.objects.filter(end_date__gte=today - timedelta(days=30)).select_related("user"),
        "today": today,
    })


@admin_required
@require_POST
def holiday_delete(request, pk):
    get_object_or_404(Holiday, pk=pk).delete()
    return redirect("calendar")


@admin_required
@require_POST
def leave_delete(request, pk):
    get_object_or_404(Leave, pk=pk).delete()
    return redirect("calendar")


# ---------------------------------------------------------------- tasks from team members


@login_required
@require_POST
def member_task_add(request):
    form = MemberTaskForm(request.POST)
    if not form.is_valid():
        for errors in form.errors.values():
            for e in errors:
                messages.error(request, e)
        return redirect("my_tasks")
    data = form.cleaned_data
    user = request.user
    if data["kind"] == "self":
        task = Task.objects.create(
            title=data["title"], details=data["details"], priority=data["priority"], due_date=data["due_date"],
            assignee=user, assigned_by=user, client=data["client"], estimated_hours=data["estimated_hours"],
        )
        _log(task, user, TaskActivity.Action.ASSIGNED, "Added by the person doing it")
        Notification.send(task, user, "assigned", recipients=task.overseers())
        messages.success(request, f'Added "{task.title}" to your tasks. It still goes for approval when it\'s done.')
        if is_office_closed(task.due_date):
            messages.warning(request, f"{task.due_date:%a, %d %b} is a day the office is closed.")
    else:
        req = TaskRequest.objects.create(
            title=data["title"], details=data["details"], priority=data["priority"], wanted_by=data["due_date"],
            client=data["client"], requested_by=user,
        )
        people = list(active_admins())
        if user.manager and user.manager.is_active and all(p.pk != user.manager_id for p in people):
            people.append(user.manager)
        Notification.objects.bulk_create([
            Notification(recipient=p, actor=user, verb=Notification.Verb.REQUEST,
                         message=f"{user.display_name} asked for a task: “{req.title}”"[:300])
            for p in people if p.pk != user.pk
        ])
        notifications.task_request_received(req, [p for p in people if p.pk != user.pk])
        messages.success(request, "Request sent. You'll be told when it's decided.")
    return redirect("my_tasks")


@manager_required
@require_POST
def request_decide(request, pk, decision):
    req = get_object_or_404(TaskRequest.objects.select_related("requested_by"), pk=pk)
    user = request.user
    if not (user.is_admin or req.requested_by.manager_id == user.pk):
        raise PermissionDenied
    if req.status != TaskRequest.Status.PENDING:
        messages.info(request, "That request was already decided.")
        return redirect("dashboard")
    note = request.POST.get("note", "").strip()[:500]
    if decision == "accept":
        assignee = people_managed_by(user).filter(pk=request.POST.get("assignee")).first()
        due = _parse_date(request.POST.get("due_date"), None)
        if not assignee or not due:
            messages.error(request, "Pick who will do it and a due date.")
            return redirect("dashboard")
        with transaction.atomic():
            task = Task.objects.create(
                title=req.title, details=req.details, priority=req.priority, due_date=due, assignee=assignee,
                assigned_by=user, client=req.client,
            )
            req.status, req.decided_by, req.decided_at, req.decision_note, req.task = TaskRequest.Status.ACCEPTED, user, timezone.now(), note, task
            req.save()
            _log(task, user, TaskActivity.Action.ASSIGNED, f"From a request by {req.requested_by.display_name}")
            Notification.send(task, user, "assigned", recipients=[assignee])
        notifications.task_assigned(task, user)
        message = f"Your request “{req.title}” was accepted and assigned to {assignee.display_name}"
        messages.success(request, f'Created "{task.title}" for {assignee.display_name}.')
    else:
        req.status, req.decided_by, req.decided_at, req.decision_note = TaskRequest.Status.DECLINED, user, timezone.now(), note
        req.save()
        message = f"Your request “{req.title}” was declined" + (f": {note}" if note else "")
        messages.success(request, "Request declined.")
    if req.requested_by_id != user.pk:
        Notification.objects.create(recipient=req.requested_by, actor=user, verb=Notification.Verb.REQUEST, message=message[:300])
    return redirect("dashboard")


# ---------------------------------------------------------------- when someone leaves


@admin_required
def handover(request, pk):
    person = get_object_or_404(User, pk=pk)
    if not request.user.can_manage_person(person):
        raise PermissionDenied
    open_tasks = list(Task.objects.filter(assignee=person).exclude(status=S.DONE).select_related("client"))
    rules = list(person.recurring_tasks.all())
    reports_to_them = list(person.reports.all())
    candidates = User.objects.filter(is_active=True).exclude(pk=person.pk)
    if request.method == "POST":
        new_owner = candidates.filter(pk=request.POST.get("to")).first()
        if new_owner is None:
            messages.error(request, "Pick who should take over.")
            return redirect("handover", pk=person.pk)
        moved, skipped = 0, 0
        for task in open_tasks:
            task.assignee = new_owner
            try:
                with transaction.atomic():
                    task.save()
            except IntegrityError:
                skipped += 1  # they already have this day's task from the same schedule
                continue
            task.collaborators.remove(new_owner)
            _log(task, request.user, TaskActivity.Action.EDITED, f"Handed over from {person.display_name} to {new_owner.display_name}")
            Notification.send(task, request.user, "assigned", recipients=[new_owner])
            notifications.task_assigned(task, request.user)
            moved += 1
        for rule in rules:
            rule.assignees.remove(person)
            rule.assignees.add(new_owner)
        for task in Task.objects.filter(collaborators=person).exclude(status=S.DONE):
            task.collaborators.remove(person)
        lead = new_owner if new_owner.is_lead else None
        person.reports.update(manager=lead)
        text = f"Handed {person.display_name}'s work to {new_owner.display_name}: {moved} task{'s' if moved != 1 else ''}, {len(rules)} recurring schedule{'s' if len(rules) != 1 else ''}."
        if skipped:
            text += f" {skipped} couldn't move because {new_owner.display_name} already has that day's task."
        messages.success(request, text)
        return redirect("team")
    return render(request, "tasks/handover.html", {
        "person": person, "open_tasks": open_tasks, "rules": rules, "reports_to_them": reports_to_them,
        "candidates": candidates, "needs_it": needs_handover(person),
    })


@admin_required
@require_POST
def email_failures_clear(request):
    EmailFailure.objects.all().delete()
    messages.success(request, "Cleared the email problems list.")
    return redirect("team")


