from django import forms
from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone

from .models import RecurringTask, Task, User


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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assignees"].queryset = User.objects.filter(is_active=True)
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
        ]

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
        fields = ["first_name", "last_name", "email", "designation", "role", "is_active"]
        labels = {"is_active": "Can log in (active)"}


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
        fields = ["title", "priority", "frequency", "day_of_month", "due_in_days", "start_date", "end_date", "assignees", "details"]
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