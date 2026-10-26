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
        MEMBER = "member", "Team member"

    role = models.CharField(max_length=10, choices=Role.choices, default=Role.MEMBER)
    designation = models.CharField(max_length=60, blank=True, help_text="e.g. Designer, Editor")
    invite_sent_at = models.DateTimeField(null=True, blank=True)
    invite_accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["first_name", "last_name", "username"]

    @property
    def is_admin(self):
        return self.is_superuser or self.role == self.Role.ADMIN

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
        REVIEW = "review", "Needs Approval"
        DONE = "done", "Done"

    class Priority(models.TextChoices):
        HIGH = "high", "High"
        MEDIUM = "medium", "Medium"
        LOW = "low", "Low"

    PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}

    title = models.CharField(max_length=200)
    details = models.TextField(blank=True)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    due_date = models.DateField()
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

    def can_view(self, user):
        """Admins, the person it's assigned to, and anyone tagged on it."""
        return user.is_admin or self.assignee_id == user.id or self.watchers.filter(pk=user.pk).exists()

    @property
    def is_late(self):
        """Not done and past its due date."""
        return self.status != self.Status.DONE and self.due_date < timezone.localdate()

    @property
    def done_date(self):
        return timezone.localtime(self.done_at).date() if self.done_at else None

    @property
    def finished_late(self):
        return self.status == self.Status.DONE and self.done_date and self.done_date > self.due_date

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

    ICONS = {
        "assigned": "📌", "started": "▶️", "submitted": "📤", "approved": "✅",
        "changes": "✏️", "reopened": "↩️", "deleted": "🗑️",
        "commented": "💬", "mentioned": "@", "reminder": "⏰", "overdue": "⚠️",
    }

    # (message for the person the task belongs to, message for everyone else)
    TEXTS = {
        "assigned": ("{actor} assigned you “{title}”", "{actor} assigned “{title}” to {owner}"),
        "started": ("{actor} started your task “{title}”", "{actor} started “{title}”{for_owner}"),
        "submitted": ("{actor} submitted your task “{title}” for approval", "{actor} submitted “{title}”{for_owner} for approval"),
        "approved": ("{actor} approved “{title}”", "{actor} approved “{title}”{for_owner}"),
        "changes": ("{actor} requested changes on “{title}”", "{actor} requested changes on “{title}”{for_owner}"),
        "reopened": ("{actor} reopened “{title}”", "{actor} reopened “{title}”{for_owner}"),
        "deleted": ("{actor} deleted your task “{title}”", "{actor} deleted “{title}”{for_owner}"),
        "commented": ("{actor} commented on your task “{title}”", "{actor} commented on “{title}”{for_owner}"),
        "mentioned": ("{actor} mentioned you on “{title}”", "{actor} mentioned you on “{title}”{for_owner}"),
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

        By default: the person the task belongs to and every admin, except whoever made the change.
        """
        owner = task.assignee
        if recipients is None:
            recipients = [owner, *active_admins()]
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

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.author}: {self.body[:40]}"

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
        DAILY = "daily", "Every day"
        WEEKDAYS = "weekdays", "Every weekday (Mon–Fri)"
        WEEKLY = "weekly", "Every week, on chosen days"
        MONTHLY = "monthly", "Every month"

    WEEKDAY_CHOICES = [("0", "Mon"), ("1", "Tue"), ("2", "Wed"), ("3", "Thu"), ("4", "Fri"), ("5", "Sat"), ("6", "Sun")]

    title = models.CharField(max_length=200)
    details = models.TextField(blank=True)
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
            return True
        if self.frequency == self.Frequency.WEEKDAYS:
            return day.weekday() < 5
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