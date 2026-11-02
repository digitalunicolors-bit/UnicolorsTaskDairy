"""Login protection: limits on wrong attempts and forced password changes."""
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import views as auth_views
from django.utils import timezone

from .models import LoginAttempt, User

# ---------------------------------------------------------------- wrong-attempt limits


def _client_ip(request):
    # Our host adds the real address as the last entry; earlier entries can be faked by the visitor.
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (forwarded.split(",")[-1].strip() if forwarded else request.META.get("REMOTE_ADDR", ""))[:64]


def _keys(request, username):
    return f"u:{(username or '').strip().lower()[:180]}", f"ip:{_client_ip(request)}"


def _window_start():
    return timezone.now() - timedelta(minutes=settings.LOGIN_LOCK_MINUTES)


def is_locked(request, username):
    user_key, ip_key = _keys(request, username)
    recent = LoginAttempt.objects.filter(created_at__gte=_window_start())
    return (
        recent.filter(key=user_key).count() >= settings.LOGIN_FAIL_LIMIT
        or recent.filter(key=ip_key).count() >= settings.LOGIN_FAIL_IP_LIMIT
    )


def record_failure(request, username):
    LoginAttempt.objects.bulk_create([LoginAttempt(key=k) for k in _keys(request, username)])
    LoginAttempt.objects.filter(created_at__lt=timezone.now() - timedelta(days=1)).delete()


def clear_failures(username):
    LoginAttempt.objects.filter(key=f"u:{(username or '').strip().lower()[:180]}").delete()


class ProtectedLoginView(auth_views.LoginView):
    """The normal login, with a limit on wrong attempts."""

    def post(self, request, *args, **kwargs):
        if is_locked(request, request.POST.get("username")):
            form = self.get_form_class()(request)
            context = self.get_context_data(form=form, locked=True, lock_minutes=settings.LOGIN_LOCK_MINUTES)
            return self.render_to_response(context, status=429)
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        record_failure(self.request, self.request.POST.get("username"))
        return super().form_invalid(form)

    def form_valid(self, form):
        clear_failures(self.request.POST.get("username"))
        return super().form_valid(form)


# ---------------------------------------------------------------- passwords


class PasswordChangeView(auth_views.PasswordChangeView):
    def form_valid(self, form):
        response = super().form_valid(form)
        User.objects.filter(pk=self.request.user.pk).update(must_change_password=False)
        return response


class PasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    """Setting a new password through the email link also lifts any login pause."""

    def form_valid(self, form):
        response = super().form_valid(form)
        User.objects.filter(pk=self.user.pk).update(must_change_password=False)
        clear_failures(self.user.username)
        if self.user.email:
            clear_failures(self.user.email)
        return response
