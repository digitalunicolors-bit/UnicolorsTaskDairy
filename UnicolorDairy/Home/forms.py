from datetime import timedelta

from django import forms
from django.conf import settings
from django.db.models import Q
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone

from .models import Client, Holiday, Leave, RecurringTask, Task, User, people_managed_by


class AssigneeChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return obj.display_name


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, (list, tuple)):
            return [single(d, initial) for d in data if d]
        return [single(data, initial)] if data else []


def _check_size(f):
    mb = getattr(settings, "MAX_UPLOAD_MB", 20)
    if f.size > mb * 1024 * 1024:
        raise forms.ValidationError(f'"{f.name}" is larger than {mb} MB.')
    return f


def _hours_field():
    return forms.DecimalField(
        required=False, min_value=0, max_value=999, max_digits=5, decimal_places=1, label="Estimated hours",
        widget=forms.NumberInput(attrs={"step": "0.5", "min": "0", "placeholder": "e.g. 4"}),
    )


def _client_field():
    return forms.ModelChoiceField(
        queryset=Client.objects.filter(is_active=True), required=False, empty_label="No client / internal",
        label="Client / project",
    )


class AssignTaskForm(forms.Form):
    title = forms.CharField(max_length=200, widget=forms.TextInput(attrs={"placeholder": "e.g. Peanutji Diwali post – 3 options"}))
    priority = forms.ChoiceField(choices=Task.Priority.choices, initial=Task.Priority.MEDIUM)
    due_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    assignees = AssigneeChoiceField(
        queryset=User.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        error_messages={"required": "Pick at least one person."},
    )
    details = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Brief, references, file links…"}),
    )
    attachments = MultipleFileField(required=False)
    voice_note = forms.FileField(required=False, widget=forms.FileInput(attrs={"accept": "audio/*", "hidden": True}))
    client = _client_field()
    estimated_hours = _hours_field()
    is_confidential = forms.BooleanField(required=False, label="Confidential")
    shared = forms.BooleanField(required=False, label="One shared task for everyone picked")
    lead = forms.ModelChoiceField(queryset=User.objects.none(), required=False, empty_label="Lead: first person picked")

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        people = people_managed_by(user) if user else User.objects.filter(is_active=True)
        self.fields["assignees"].queryset = people
        self.fields["lead"].queryset = people
        self.fields["lead"].label_from_instance = lambda u: f"Lead: {u.display_name}"
        if not self.is_bound:
            self.initial.setdefault("due_date", timezone.localdate())

    def clean_attachments(self):
        files = self.cleaned_data.get("attachments") or []
        if len(files) > 10:
            raise forms.ValidationError("You can attach up to 10 files.")
        return [_check_size(f) for f in files]

    def clean_voice_note(self):
        f = self.cleaned_data.get("voice_note")
        return _check_size(f) if f else None


class NoteForm(forms.Form):
    note = forms.CharField(required=False, max_length=1000, widget=forms.Textarea(attrs={"rows": 2}))


def _limit_role_and_lead(form, actor, keep_admin=False):
    """Only the owner can make someone an admin."""
    if "role" in form.fields and actor is not None and not actor.is_superuser and not keep_admin:
        form.fields["role"].choices = [c for c in form.fields["role"].choices if c[0] != User.Role.ADMIN]
    leads = User.objects.filter(is_active=True, role__in=[User.Role.MANAGER, User.Role.ADMIN])
    if form.instance.pk:
        leads = leads.exclude(pk=form.instance.pk)
    form.fields["manager"].queryset = leads
    form.fields["manager"].required = False
    form.fields["manager"].empty_label = "Nobody (admins approve)"
    form.fields["manager"].label_from_instance = lambda u: u.display_name


class UserCreateForm(forms.ModelForm):
    class Meta:
        model = User
        fields = [
            "first_name",
            "last_name",
            "username",
            "email",
            "designation",
            "role",
            "manager",
        ]

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        _limit_role_and_lead(self, actor)

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()

        if not email:
            raise forms.ValidationError("Email is required for sending the invitation.")

        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("A user with this email already exists.")

        return email

    def save(self, commit=True):
        user = super().save(commit=False)

        # User cannot log in until invitation is accepted.
        user.is_active = False
        user.set_unusable_password()

        if commit:
            user.save()

        return user


class UserEditForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "designation", "role", "manager", "is_active"]
        labels = {"is_active": "Can log in (active)"}

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        keep_admin = self.instance.pk and actor and self.instance.pk == actor.pk
        _limit_role_and_lead(self, actor, keep_admin=keep_admin)


class SetPasswordForm(forms.Form):
    password = forms.CharField(widget=forms.PasswordInput, label="New password")

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_password(self):
        password = self.cleaned_data["password"]
        validate_password(password, self.user)
        return password


class AcceptInviteForm(forms.Form):
    password1 = forms.CharField(
        label="Create password",
        widget=forms.PasswordInput(
            attrs={"placeholder": "Create a password"}
        ),
    )

    password2 = forms.CharField(
        label="Confirm password",
        widget=forms.PasswordInput(
            attrs={"placeholder": "Confirm password"}
        ),
    )

    def clean(self):
        cleaned = super().clean()

        password1 = cleaned.get("password1")
        password2 = cleaned.get("password2")

        if password1 and password2:
            if password1 != password2:
                self.add_error("password2", "Passwords do not match.")
            else:
                validate_password(password1)

        return cleaned

class RecurringTaskForm(forms.ModelForm):
    assignees = AssigneeChoiceField(
        queryset=User.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        error_messages={"required": "Pick at least one person."},
    )
    weekday_choices = forms.MultipleChoiceField(
        choices=RecurringTask.WEEKDAY_CHOICES, required=False, widget=forms.CheckboxSelectMultiple, label="On these days"
    )

    class Meta:
        model = RecurringTask
        fields = ["title", "client", "estimated_hours", "priority", "frequency", "day_of_month", "due_in_days", "start_date", "end_date", "assignees", "details"]
        widgets = {
            "title": forms.TextInput(attrs={"placeholder": "e.g. Peanutji Instagram post"}),
            "details": forms.Textarea(attrs={"rows": 3, "placeholder": "Brief, references, file links…"}),
            "start_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "end_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "day_of_month": forms.NumberInput(attrs={"min": 1, "max": 31}),
            "due_in_days": forms.NumberInput(attrs={"min": 0, "max": 60}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assignees"].queryset = User.objects.filter(is_active=True)
        self.fields["end_date"].required = False
        self.fields["client"].queryset = Client.objects.filter(is_active=True)
        self.fields["client"].empty_label = "No client / internal"
        self.fields["estimated_hours"].widget.attrs.update({"step": "0.5", "min": "0"})
        if self.instance.pk:
            self.initial["weekday_choices"] = [str(d) for d in self.instance.weekday_list]
        elif not self.is_bound:
            self.initial.setdefault("start_date", timezone.localdate())

    def clean_day_of_month(self):
        d = self.cleaned_data.get("day_of_month") or 1
        if not 1 <= d <= 31:
            raise forms.ValidationError("Pick a day between 1 and 31.")
        return d

    def clean_due_in_days(self):
        d = self.cleaned_data.get("due_in_days") or 0
        if d > 60:
            raise forms.ValidationError("Due date can be at most 60 days after the task is created.")
        return d

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("frequency") == RecurringTask.Frequency.WEEKLY and not cleaned.get("weekday_choices"):
            self.add_error("weekday_choices", "Pick at least one day of the week.")
        start, end = cleaned.get("start_date"), cleaned.get("end_date")
        if start and end and end < start:
            self.add_error("end_date", "The end date can't be before the start date.")
        return cleaned

    def save(self, commit=True):
        rule = super().save(commit=False)
        days = self.cleaned_data.get("weekday_choices") or []
        rule.weekdays = ",".join(sorted(days)) if rule.frequency == RecurringTask.Frequency.WEEKLY else ""
        if commit:
            rule.save()
            self.save_m2m()
        return rule

class TaskEditForm(forms.ModelForm):
    """Admins change a task after assigning it: wording, priority, due date or who does it."""

    class Meta:
        model = Task
        fields = ["title", "client", "estimated_hours", "priority", "due_date", "assignee", "collaborators", "is_confidential", "details"]
        labels = {"assignee": "Assigned to", "title": "Task", "client": "Client / project",
                  "collaborators": "Working with", "is_confidential": "Confidential"}
        widgets = {
            "due_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "details": forms.Textarea(attrs={"rows": 4, "placeholder": "Brief, references, file links…"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        people = people_managed_by(user) if user else User.objects.filter(is_active=True)
        if self.instance.pk:
            people = people | User.objects.filter(pk=self.instance.assignee_id)
        self.fields["assignee"].queryset = people.distinct()
        self.fields["client"].queryset = Client.objects.filter(is_active=True) | Client.objects.filter(pk=self.instance.client_id)
        self.fields["client"].empty_label = "No client / internal"
        self.fields["collaborators"].required = False
        self.fields["collaborators"].queryset = User.objects.filter(is_active=True)
        self.fields["collaborators"].label_from_instance = lambda u: u.display_name
        self.fields["collaborators"].widget.attrs["size"] = 4
        self.fields["assignee"].label_from_instance = lambda u: u.display_name
        self.fields["assignee"].empty_label = None


class ClientForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = ["name"]
        widgets = {"name": forms.TextInput(attrs={"placeholder": "e.g. Peanutji"})}

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if Client.objects.filter(name__iexact=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("That client is already on the list.")
        return name


class HolidayForm(forms.ModelForm):
    class Meta:
        model = Holiday
        fields = ["date", "name"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "name": forms.TextInput(attrs={"placeholder": "e.g. Diwali"}),
        }


class LeaveRequestForm(forms.ModelForm):
    """A person applying for their own leave."""

    class Meta:
        model = Leave
        fields = ["kind", "start_date", "end_date", "approver", "note", "cover"]
        labels = {"kind": "Type of leave", "start_date": "From", "end_date": "To", "note": "Reason",
                  "cover": "Who can cover? (optional)", "approver": "Send for approval to"}
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
            "note": forms.TextInput(attrs={"placeholder": "A short reason"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["note"].required = True
        self.fields["kind"].initial = Leave.Kind.CASUAL
        people = User.objects.filter(is_active=True).filter(
            Q(role__in=[User.Role.ADMIN, User.Role.MANAGER]) | Q(is_superuser=True)
        ).exclude(pk=user.pk)
        self.fields["approver"].queryset = people
        self.fields["approver"].required = people.exists()  # the only admin has nobody to ask
        self.fields["approver"].empty_label = "Choose a person…"
        self.fields["approver"].label_from_instance = lambda u: f"{u.display_name} ({u.role_label})"
        self.fields["approver"].error_messages["required"] = "Choose who should approve this."
        if user.manager_id and people.filter(pk=user.manager_id).exists():
            self.fields["approver"].initial = user.manager_id
        self.fields["cover"].required = False
        self.fields["cover"].queryset = User.objects.filter(is_active=True).exclude(pk=user.pk)
        self.fields["cover"].empty_label = "Not sure"
        self.fields["cover"].label_from_instance = lambda u: u.display_name

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_date"), cleaned.get("end_date")
        if not start or not end:
            return cleaned
        if end < start:
            self.add_error("end_date", "The end date can't be before the start date.")
            return cleaned
        if start < timezone.localdate() - timedelta(days=30):
            self.add_error("start_date", "Leave can only be applied for up to 30 days back.")
        if (end - start).days > 90:
            self.add_error("end_date", "Please apply for up to 90 days at a time.")
        clash = Leave.objects.filter(
            user=self.user, status__in=[Leave.Status.PENDING, Leave.Status.APPROVED],
            start_date__lte=end, end_date__gte=start,
        ).exists()
        if clash:
            raise forms.ValidationError("You already have leave applied for on some of these dates.")
        return cleaned


class LeaveForm(forms.ModelForm):
    class Meta:
        model = Leave
        fields = ["user", "start_date", "end_date", "note"]
        labels = {"user": "Person", "start_date": "From", "end_date": "To"}
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
            "note": forms.TextInput(attrs={"placeholder": "Optional note"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["user"].queryset = User.objects.filter(is_active=True)
        self.fields["user"].label_from_instance = lambda u: u.display_name

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_date"), cleaned.get("end_date")
        if start and end and end < start:
            self.add_error("end_date", "The end date can't be before the start date.")
        return cleaned
