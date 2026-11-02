import calendar
import re
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    """A Unicolors employee. Role decides what they can see and do."""

    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        MANAGER = "manager", "Team lead"
        MEMBER = "member", "Team member"

    role = models.CharField(max_length=10, choices=Role.choices, default=Role.MEMBER)
    designation = models.CharField(max_length=60, blank=True, help_text="e.g. Designer, Editor")
    invite_sent_at = models.DateTimeField(null=True, blank=True)
    invite_accepted_at = models.DateTimeField(null=True, blank=True)
    manager = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="reports",
        verbose_name="Reports to", help_text="The team lead who assigns and approves this person's work",
    )
    must_change_password = models.BooleanField(default=False)

    class Meta:
        ordering = ["first_name", "last_name", "username"]

    @property
    def is_admin(self):
        return self.is_superuser or self.role == self.Role.ADMIN

    @property
    def is_lead(self):
        """Admins and team leads: can open the dashboard and manage work."""
        return self.is_admin or self.role == self.Role.MANAGER

    @property
    def role_label(self):
        return "Admin" if self.is_superuser else self.get_role_display()

    def can_manage_person(self, other):
        """The owner manages everyone. Admins manage themselves and non-admins."""
        if self.is_superuser:
            return True
        if not self.is_admin:
            return False
        return other.pk == self.pk or not other.is_admin

    @property
    def display_name(self):
        return self.get_full_name() or self.username

    @property
    def initials(self):
        parts = self.display_name.split()
        return "".join(p[0] for p in parts[:2]).upper() or "?"

    def save(self, *args, **kwargs):
        # Anyone created with createsuperuser is automatically an admin in the app.
        if self.is_superuser:
            self.role = self.Role.ADMIN
        super().save(*args, **kwargs)

    def __str__(self):
        return self.display_name


def active_admins():
    return User.objects.filter(is_active=True).filter(models.Q(role=User.Role.ADMIN) | models.Q(is_superuser=True))


def people_managed_by(user):
    """Active people this user can assign work to."""
    qs = User.objects.filter(is_active=True)
    return qs if user.is_admin else qs.filter(manager=user)


def weekly_off_days():
    return getattr(settings, "WEEKLY_OFF_DAYS", [6])


def is_holiday(day):
    return Holiday.objects.filter(date=day).exists()


def is_office_closed(day):
    return day.weekday() in weekly_off_days() or is_holiday(day)


def next_working_day(day):
    for _ in range(60):
        if not is_office_closed(day):
            return day
        day += timedelta(days=1)
    return day


def on_leave_ids(day):
    """People on approved leave that day. A request has no effect until it's approved."""
    return set(
        Leave.objects.filter(status=Leave.Status.APPROVED, start_date__lte=day, end_date__gte=day)
        .values_list("user_id", flat=True)
    )


def is_on_leave(user, day):
    return Leave.objects.filter(status=Leave.Status.APPROVED, user=user, start_date__lte=day, end_date__gte=day).exists()


def leaves_managed_by(user):
    """Leave this person can decide: everyone else's for admins, their own team's for team leads."""
    if user.is_admin:
        return Leave.objects.exclude(user=user)
    return Leave.objects.filter(models.Q(user__manager=user) | models.Q(approver=user)).exclude(user=user)


class Client(models.Model):
    """A client or project that tasks belong to."""

    name = models.CharField(max_length=120, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Holiday(models.Model):
    """A day the office is closed."""

    date = models.DateField(unique=True)
    name = models.CharField(max_length=255)

    class Meta:
        ordering = ["date"]

    def __str__(self):
        return f"{self.name} ({self.date:%d %b %Y})"


class Leave(models.Model):
    """Dates a person is away. Applied for by the person, or added directly by an admin."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for approval"
        APPROVED = "approved", "Approved"
        DECLINED = "declined", "Declined"
        CANCELLED = "cancelled", "Cancelled"

    class Kind(models.TextChoices):
        CASUAL = "casual", "Casual leave"
        SICK = "sick", "Sick leave"
        OTHER = "other", "Other"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="leaves")
    start_date = models.DateField()
    end_date = models.DateField()
    note = models.CharField(max_length=200, blank=True)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.OTHER)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.APPROVED, db_index=True)
    cover = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="leaves_to_approve",
        help_text="The person this request was sent to",
    )
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=500, blank=True)
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-start_date"]

    def __str__(self):
        return f"{self.user} {self.start_date}–{self.end_date}"

    @property
    def days(self):
        return (self.end_date - self.start_date).days + 1

    @property
    def can_cancel(self):
        return self.status == self.Status.PENDING or (
            self.status == self.Status.APPROVED and self.end_date >= timezone.localdate()
        )

    def approvers(self):
        """Who is asked to decide: the person they picked and their reporting manager, never themselves.
        Requests with nobody picked go to all admins and the reporting manager."""
        picked = self.approver if self.approver_id and self.approver.is_active else None
        people = [picked] if picked else [p for p in active_admins() if p.pk != self.user_id]
        lead = self.user.manager
        if lead and lead.is_active and lead.pk != self.user_id and all(p.pk != lead.pk for p in people):
            people.append(lead)
        return people

    def can_decide(self, user):
        return user.pk != self.user_id and (
            user.is_admin or self.user.manager_id == user.pk or self.approver_id == user.pk
        )

    def tasks_due(self):
        return Task.objects.filter(
            assignee=self.user, status__in=Task.NEEDS_WORK, due_date__range=(self.start_date, self.end_date),
        ).select_related("client")


class LoginAttempt(models.Model):
    """A failed login, used to slow down password guessing."""

    key = models.CharField(max_length=190, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)


class DailySummary(models.Model):
    """Records each morning summary, so a person only ever gets one a day."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    date = models.DateField()
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "date"], name="unique_daily_summary")]


class EmailFailure(models.Model):
    """An email that could not be sent, shown to admins on the Team page."""

    subject = models.CharField(max_length=255)
    recipient = models.CharField(max_length=255)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class ActiveTaskManager(models.Manager):
    """Archived tasks are hidden everywhere unless asked for through Task.all_objects."""

    def get_queryset(self):
        return super().get_queryset().filter(archived_at__isnull=True)


# "@username" anywhere in text (but not inside an email address like a@b.com)
MENTION_RE = re.compile(r"(?<![\w@])@([\w.@+-]+)")
MENTION_TRIM = ".,!?;:"


def find_mentioned_users(text):
    """Active users tagged as @username in the text."""
    names = {m.group(1).rstrip(MENTION_TRIM).lower() for m in MENTION_RE.finditer(text or "")}
    names.discard("")
    if not names:
        return []
    q = models.Q()
    for n in names:
        q |= models.Q(username__iexact=n)
    return list(User.objects.filter(is_active=True).filter(q))


class Task(models.Model):
    class Status(models.TextChoices):
        TODO = "todo", "To Do"
        PROGRESS = "progress", "In Progress"
        CHANGE = "change", "Changes Requested"
        HOLD = "hold", "On Hold"
        REVIEW = "review", "Needs Approval"
        DONE = "done", "Done"

    class HoldKind(models.TextChoices):
        CLIENT = "client", "Waiting on client"
        BLOCKED = "blocked", "Blocked"

    class Priority(models.TextChoices):
        HIGH = "high", "High"
        MEDIUM = "medium", "Medium"
        LOW = "low", "Low"

    PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
    NEEDS_WORK = ("todo", "progress", "change")

    objects = ActiveTaskManager()
    all_objects = models.Manager()

    title = models.CharField(max_length=200)
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="tasks")
    details = models.TextField(blank=True)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    due_date = models.DateField()
    original_due_date = models.DateField(null=True, blank=True, help_text="The first deadline this task was given")
    score_due_date = models.DateField(
        null=True, blank=True, help_text="The deadline used for on-time scoring. Moves only with an approved extension"
    )
    estimated_hours = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    actual_hours = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    is_confidential = models.BooleanField(default=False)
    hold_kind = models.CharField(max_length=10, choices=HoldKind.choices, blank=True)
    hold_note = models.CharField(max_length=500, blank=True)
    hold_since = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True, db_index=True)
    archived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="tasks")
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.TODO, db_index=True)

    member_note = models.TextField(blank=True, help_text="Latest note from the team member when submitting")
    admin_note = models.TextField(blank=True, help_text="Latest change request from the admin")
    change_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    done_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    watchers = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="watched_tasks", help_text="People tagged on this task"
    )
    collaborators = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="shared_tasks", help_text="People working on this task with the lead"
    )
    recurring = models.ForeignKey(
        "RecurringTask", on_delete=models.SET_NULL, null=True, blank=True, related_name="tasks",
        help_text="The repeating schedule that created this task",
    )
    occurrence_date = models.DateField(null=True, blank=True, help_text="Which day of the schedule this task is for")

    class Meta:
        ordering = ["due_date", "created_at"]
        indexes = [models.Index(fields=["assignee", "due_date"], name="task_assignee_due_idx")]
        constraints = [
            models.UniqueConstraint(
                fields=["recurring", "occurrence_date", "assignee"],
                condition=models.Q(recurring__isnull=False),
                name="unique_recurring_occurrence",
            )
        ]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.original_due_date:
            self.original_due_date = self.due_date
        if not self.score_due_date:
            self.score_due_date = self.due_date
        super().save(*args, **kwargs)

    # ---- who can do what

    def overseers(self):
        """Everyone responsible for this task: all admins, plus the assignee's team lead."""
        people = list(active_admins())
        lead = self.assignee.manager
        if lead and lead.is_active and all(p.pk != lead.pk for p in people):
            people.append(lead)
        return people

    def can_manage(self, user):
        return user.is_admin or (user.role == User.Role.MANAGER and self.assignee.manager_id == user.pk)

    def is_worker(self, user):
        return self.assignee_id == user.pk or any(p.pk == user.pk for p in self.collaborators.all())

    def can_work(self, user):
        return self.is_worker(user) or self.can_manage(user)

    def can_view(self, user):
        """Managers, the people doing the work, and anyone given access by a tag."""
        return self.can_work(user) or self.watchers.filter(pk=user.pk).exists()

    def can_approve(self, user):
        """Nobody approves their own work, unless nobody else is able to."""
        if not self.can_manage(user):
            return False
        if not self.is_worker(user):
            return True
        return not any(p.pk != user.pk and not self.is_worker(p) for p in self.overseers())

    def can_grant_access(self, user):
        """Tagging someone gives them access only if the person tagging is responsible for the task."""
        return self.can_manage(user) or (not self.is_confidential and self.is_worker(user))

    def grant_tagged_access(self, actor, people):
        """Add tagged people as watchers when allowed. Returns those who can actually see the task."""
        allowed = self.can_grant_access(actor)
        visible = []
        for person in people:
            if self.can_view(person):
                visible.append(person)
            elif allowed:
                self.watchers.add(person)
                visible.append(person)
        return visible

    # ---- deadlines

    @property
    def scoring_due_date(self):
        return self.score_due_date or self.due_date

    @property
    def is_late(self):
        """Still needs work and is past its due date. Tasks on hold or waiting for approval aren't overdue."""
        return self.status in self.NEEDS_WORK and self.due_date < timezone.localdate()

    @property
    def done_date(self):
        return timezone.localtime(self.done_at).date() if self.done_at else None

    @property
    def submitted_date(self):
        return timezone.localtime(self.submitted_at).date() if self.submitted_at else None

    @property
    def finish_date(self):
        """When the person finished: their last submission, not when it was approved."""
        return self.submitted_date or self.done_date

    @property
    def submitted_late(self):
        return bool(self.submitted_date and self.submitted_date > self.scoring_due_date)

    @property
    def finished_late(self):
        return self.status == self.Status.DONE and bool(self.finish_date) and self.finish_date > self.scoring_due_date

    @property
    def due_date_moved(self):
        return bool(self.original_due_date and self.original_due_date != self.due_date)

    @property
    def subtask_progress(self):
        items = list(self.subtasks.all())
        return {"done": sum(1 for s in items if s.is_done), "total": len(items)} if items else None

    @property
    def on_time(self):
        return self.status == self.Status.DONE and not self.finished_late


def attachment_path(instance, filename):
    return f"task_files/{timezone.now():%Y/%m}/{filename}"


class TaskAttachment(models.Model):
    """A file or voice note attached to a task's brief. Served only to the assignee and admins."""

    class Kind(models.TextChoices):
        FILE = "file", "File"
        VOICE = "voice", "Voice note"
        WORK = "work", "Finished work"

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=attachment_path, max_length=300)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.FILE)
    original_name = models.CharField(max_length=255)
    size = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["kind", "created_at"]

    def __str__(self):
        return self.original_name

    @property
    def is_image(self):
        return self.original_name.lower().rsplit(".", 1)[-1] in {"jpg", "jpeg", "png", "gif", "webp"}

    @property
    def size_label(self):
        kb = self.size / 1024
        return f"{kb / 1024:.1f} MB" if kb >= 1024 else f"{max(kb, 1):.0f} KB"


class TaskActivity(models.Model):
    """Audit trail: who did what on a task, and when."""

    class Action(models.TextChoices):
        ASSIGNED = "assigned", "Assigned"
        STARTED = "started", "Started"
        SUBMITTED = "submitted", "Submitted for approval"
        APPROVED = "approved", "Approved"
        CHANGES = "changes", "Requested changes"
        REOPENED = "reopened", "Reopened"
        EDITED = "edited", "Edited"
        ARCHIVED = "archived", "Archived"
        RESTORED = "restored", "Restored"
        HOLD = "hold", "Put on hold"
        RESUMED = "resumed", "Resumed"
        EXT_REQUEST = "ext_request", "Asked for more time"
        EXT_OK = "ext_ok", "Approved more time"
        EXT_NO = "ext_no", "Declined more time"
        FILE = "file", "Added a file"
        COMMENT_DEL = "comment_del", "Deleted a comment"

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="activity")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    action = models.CharField(max_length=12, choices=Action.choices)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.actor} {self.get_action_display()} {self.task}"


class Notification(models.Model):
    """An in-app update shown under the bell icon."""

    class Verb(models.TextChoices):
        ASSIGNED = "assigned", "Assigned"
        STARTED = "started", "Started"
        SUBMITTED = "submitted", "Submitted"
        APPROVED = "approved", "Approved"
        CHANGES = "changes", "Changes requested"
        REOPENED = "reopened", "Reopened"
        DELETED = "deleted", "Deleted"
        COMMENTED = "commented", "Commented"
        MENTIONED = "mentioned", "Mentioned"
        REMINDER = "reminder", "Reminder"
        OVERDUE = "overdue", "Overdue"
        EDITED = "edited", "Edited"
        RESTORED = "restored", "Restored"
        HOLD = "hold", "On hold"
        RESUMED = "resumed", "Resumed"
        EXTENSION = "extension", "Extension"
        REQUEST = "request", "Task request"
        FILE = "file", "File added"
        LEAVE = "leave", "Leave"

    ICONS = {
        "assigned": "📌", "started": "▶️", "submitted": "📤", "approved": "✅",
        "changes": "✏️", "reopened": "↩️", "deleted": "🗑️",
        "commented": "💬", "mentioned": "@", "reminder": "⏰", "overdue": "⚠️", "edited": "📝",
        "restored": "♻️", "hold": "⏸️", "resumed": "▶️", "extension": "📅", "request": "🙋", "file": "📎", "leave": "🌴",
    }

    # (message for the person the task belongs to, message for everyone else)
    TEXTS = {
        "assigned": ("{actor} assigned you “{title}”", "{actor} assigned “{title}” to {owner}"),
        "started": ("{actor} started your task “{title}”", "{actor} started “{title}”{for_owner}"),
        "submitted": ("{actor} submitted your task “{title}” for approval", "{actor} submitted “{title}”{for_owner} for approval"),
        "approved": ("{actor} approved “{title}”", "{actor} approved “{title}”{for_owner}"),
        "changes": ("{actor} requested changes on “{title}”", "{actor} requested changes on “{title}”{for_owner}"),
        "reopened": ("{actor} reopened “{title}”", "{actor} reopened “{title}”{for_owner}"),
        "deleted": ("{actor} archived your task “{title}”", "{actor} archived “{title}”{for_owner}"),
        "restored": ("{actor} restored your task “{title}”", "{actor} restored “{title}”{for_owner}"),
        "hold": ("{actor} put your task “{title}” on hold", "{actor} put “{title}”{for_owner} on hold"),
        "resumed": ("{actor} resumed your task “{title}”", "{actor} resumed “{title}”{for_owner}"),
        "extension": ("{actor} updated the deadline request on “{title}”", "{actor} asked for more time on “{title}”{for_owner}"),
        "file": ("{actor} added a file to your task “{title}”", "{actor} added a file to “{title}”{for_owner}"),
        "commented": ("{actor} commented on your task “{title}”", "{actor} commented on “{title}”{for_owner}"),
        "mentioned": ("{actor} mentioned you on “{title}”", "{actor} mentioned you on “{title}”{for_owner}"),
        "edited": ("{actor} updated your task “{title}”", "{actor} updated “{title}”{for_owner}"),
    }

    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    task = models.ForeignKey(Task, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    verb = models.CharField(max_length=12, choices=Verb.choices)
    message = models.CharField(max_length=300)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["recipient", "is_read"], name="notif_recipient_read_idx")]

    def __str__(self):
        return self.message

    @property
    def icon(self):
        return self.ICONS.get(self.verb, "🔔")

    @classmethod
    def send(cls, task, actor, verb, note="", recipients=None):
        """Notify everyone who should know about a change.

        By default: the people working on the task and everyone overseeing it, except whoever made the change.
        """
        owner = task.assignee
        if recipients is None:
            recipients = [owner, *task.collaborators.all(), *task.overseers()]
        mine, others = cls.TEXTS[verb]
        values = {
            "actor": actor.display_name,
            "title": task.title,
            "owner": owner.display_name,
            "for_owner": "" if owner.pk == actor.pk else f" for {owner.display_name}",
        }
        note = (note or "").strip()
        seen, items = set(), []
        for person in recipients:
            if not person or person.pk == actor.pk or person.pk in seen or not person.is_active:
                continue
            seen.add(person.pk)
            text = (mine if person.pk == owner.pk else others).format(**values)
            if note:
                text += f": {note}"
            items.append(cls(recipient=person, actor=actor, task=task, verb=verb, message=text[:300]))
        cls.objects.bulk_create(items)


class TaskComment(models.Model):
    """A comment on a task. Type @username to tag someone."""

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    body = models.TextField()
    mentions = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.author}: {self.body[:40]}"


class Subtask(models.Model):
    """A checklist step inside a task."""

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="subtasks")
    title = models.CharField(max_length=200)
    is_done = models.BooleanField(default=False)
    done_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    done_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return self.title


class ExtensionRequest(models.Model):
    """A request for more time. Only an approved one changes how the task is scored."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for a decision"
        APPROVED = "approved", "Approved"
        DECLINED = "declined", "Declined"

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="extensions")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    previous_date = models.DateField()
    requested_date = models.DateField()
    reason = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.task}: {self.previous_date} → {self.requested_date}"


class TaskRequest(models.Model):
    """A team member asking for work to be added. Becomes a task when accepted."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for a decision"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"

    title = models.CharField(max_length=200)
    details = models.TextField(blank=True)
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    priority = models.CharField(max_length=10, choices=Task.Priority.choices, default=Task.Priority.MEDIUM)
    wanted_by = models.DateField(null=True, blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="task_requests")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=500, blank=True)
    task = models.ForeignKey(Task, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

class TaskReminder(models.Model):
    """Records each due-date reminder email, so it is only ever sent once."""

    class Kind(models.TextChoices):
        WEEK = "week", "7 days before"
        DAY = "day", "1 day before"
        DUE = "due", "On the due date"
        OVERDUE = "overdue", "Overdue alert to admins"

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="reminders")
    kind = models.CharField(max_length=8, choices=Kind.choices)
    due_date = models.DateField(help_text="The due date this reminder was for")
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-sent_at"]
        constraints = [models.UniqueConstraint(fields=["task", "kind", "due_date"], name="unique_task_reminder")]

    def __str__(self):
        return f"{self.task} – {self.get_kind_display()}"


def _ordinal(n):
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


class RecurringTask(models.Model):
    """A task that is created automatically on a schedule, e.g. "Instagram post every Monday"."""

    class Frequency(models.TextChoices):
        DAILY = "daily", "Every working day"
        WEEKDAYS = "weekdays", "Every weekday (Mon–Fri)"
        WEEKLY = "weekly", "Every week, on chosen days"
        MONTHLY = "monthly", "Every month"

    WEEKDAY_CHOICES = [("0", "Mon"), ("1", "Tue"), ("2", "Wed"), ("3", "Thu"), ("4", "Fri"), ("5", "Sat"), ("6", "Sun")]

    title = models.CharField(max_length=200)
    details = models.TextField(blank=True)
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    estimated_hours = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    priority = models.CharField(max_length=10, choices=Task.Priority.choices, default=Task.Priority.MEDIUM)
    assignees = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="recurring_tasks")
    frequency = models.CharField(max_length=10, choices=Frequency.choices, default=Frequency.WEEKLY)
    weekdays = models.CharField(max_length=20, blank=True, help_text="For weekly: days as numbers, Mon=0 … Sun=6")
    day_of_month = models.PositiveSmallIntegerField(default=1, help_text="For monthly: 1–31")
    due_in_days = models.PositiveSmallIntegerField(default=0, help_text="0 = due the same day it is created")
    start_date = models.DateField(default=timezone.localdate)
    end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    last_run_on = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-is_active", "title"]

    def __str__(self):
        return f"{self.title} ({self.schedule_label})"

    @property
    def weekday_list(self):
        return [int(x) for x in self.weekdays.split(",") if x.strip().isdigit()]

    def runs_on(self, day):
        if day < self.start_date or (self.end_date and day > self.end_date):
            return False
        if self.frequency == self.Frequency.DAILY:
            return not is_office_closed(day)
        if self.frequency == self.Frequency.WEEKDAYS:
            return day.weekday() < 5 and not is_holiday(day)
        if self.frequency == self.Frequency.WEEKLY:
            return day.weekday() in self.weekday_list
        if self.frequency == self.Frequency.MONTHLY:
            last = calendar.monthrange(day.year, day.month)[1]
            return day.day == min(self.day_of_month, last)
        return False

    def next_run(self, after=None):
        """The next day (from `after`, inclusive) a task will be created, or None."""
        day = max(after or timezone.localdate(), self.start_date)
        for _ in range(400):
            if self.end_date and day > self.end_date:
                return None
            if self.runs_on(day):
                return day
            day += timedelta(days=1)
        return None

    @property
    def schedule_label(self):
        if self.frequency == self.Frequency.WEEKLY:
            names = dict(self.WEEKDAY_CHOICES)
            days = [names[str(d)] for d in sorted(self.weekday_list)]
            return "Every " + ", ".join(days) if days else "Weekly (no days picked)"
        if self.frequency == self.Frequency.MONTHLY:
            return f"Every month on the {_ordinal(self.day_of_month)}"
        return self.get_frequency_display()

    @property
    def due_label(self):
        if self.due_in_days == 0:
            return "Due the same day"
        if self.due_in_days == 1:
            return "Due the next day"
        return f"Due {self.due_in_days} days later"

class AdminNote(models.Model):
    """An admin's personal checklist on the My Tasks page (stored as JSON text)."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="admin_note_pad")
    content = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Checklist of {self.user}"
