from django import forms
from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone

from .models import Task, User


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