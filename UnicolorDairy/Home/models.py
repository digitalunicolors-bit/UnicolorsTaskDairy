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

    class Meta:
        ordering = ["due_date", "created_at"]
        indexes = [models.Index(fields=["assignee", "due_date"], name="task_assignee_due_idx")]

    def __str__(self):
        return self.title

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
