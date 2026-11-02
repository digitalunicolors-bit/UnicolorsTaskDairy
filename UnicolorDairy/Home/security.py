"""Login protection: limits on wrong attempts, two-step login, forced password changes."""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from datetime import timedelta
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

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
    """The normal login, with a limit on wrong attempts and a second step for people who turned it on."""

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
        user = form.get_user()
        clear_failures(self.request.POST.get("username"))
        if user.totp_enabled:
            self.request.session["2fa_user"] = user.pk
            self.request.session["2fa_at"] = time.time()
            self.request.session["2fa_next"] = self.get_success_url()
            return redirect("login_verify")
        return super().form_valid(form)


def login_verify(request):
    """Second step: the 6-digit code from the person's authenticator app."""
    pending = request.session.get("2fa_user")
    started = request.session.get("2fa_at", 0)
    if not pending or time.time() - started > 300:
        for key in ("2fa_user", "2fa_at", "2fa_next"):
            request.session.pop(key, None)
        return redirect("login")
    user = User.objects.filter(pk=pending, is_active=True).first()
    if user is None:
        return redirect("login")
    locked = is_locked(request, user.username)
    error = None
    if request.method == "POST" and not locked:
        if check_code(user, request.POST.get("code", "")):
            next_url = request.session.get("2fa_next") or settings.LOGIN_REDIRECT_URL
            if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                next_url = "home"
            clear_failures(user.username)
            login(request, user)
            return redirect(next_url)
        record_failure(request, user.username)
        locked = is_locked(request, user.username)
        error = "That code isn't right. Check your authenticator app and try again."
    return render(request, "registration/login_verify.html", {
        "error": error, "locked": locked, "lock_minutes": settings.LOGIN_LOCK_MINUTES,
    }, status=429 if locked else 200)


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


# ---------------------------------------------------------------- authenticator codes (TOTP, RFC 6238)

STEP_SECONDS = 30


def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode()


def _code_for_step(secret, step):
    key = base64.b32decode(secret)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{number % 1_000_000:06d}"


def _matching_step(secret, code):
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 6:
        return None
    now = int(time.time() // STEP_SECONDS)
    for step in (now - 1, now, now + 1):  # allow for a phone clock slightly off
        if hmac.compare_digest(_code_for_step(secret, step), code):
            return step
    return None


def check_code(user, code):
    """True if the code is right and hasn't been used before."""
    step = _matching_step(user.totp_secret, code) if user.totp_secret else None
    if step is None or step <= user.totp_last_step:
        return False
    User.objects.filter(pk=user.pk).update(totp_last_step=step)
    user.totp_last_step = step
    return True


def _otpauth_uri(user, secret):
    label = quote(f"Unicolors Workspace:{user.email or user.username}")
    return f"otpauth://totp/{label}?secret={secret}&issuer=Unicolors%20Tasks&digits=6&period={STEP_SECONDS}"


@login_required
def security_settings(request):
    """Turn two-step login on or off for your own account."""
    user = request.user
    setup_secret = None
    error = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "start" and not user.totp_enabled:
            request.session["2fa_setup"] = new_secret()
        elif action == "confirm" and not user.totp_enabled:
            secret = request.session.get("2fa_setup")
            if secret and _matching_step(secret, request.POST.get("code", "")) is not None:
                user.totp_secret, user.totp_enabled, user.totp_last_step = secret, True, 0
                user.save(update_fields=["totp_secret", "totp_enabled", "totp_last_step"])
                request.session.pop("2fa_setup", None)
                messages.success(request, "Two-step login is on. You'll need a code from your app each time you log in.")
                return redirect("security")
            error = "That code isn't right. Try the newest code in your app."
        elif action == "disable" and user.totp_enabled:
            if user.check_password(request.POST.get("password", "")):
                user.totp_enabled, user.totp_secret = False, ""
                user.save(update_fields=["totp_enabled", "totp_secret"])
                messages.success(request, "Two-step login is off.")
                return redirect("security")
            error = "That password isn't right."
    if not user.totp_enabled:
        setup_secret = request.session.get("2fa_setup")
    return render(request, "tasks/security.html", {
        "setup_secret": setup_secret,
        "setup_uri": _otpauth_uri(user, setup_secret) if setup_secret else "",
        "error": error,
    })


@require_POST
@login_required
def reset_two_step(request, pk):
    """Someone lost their phone: the person who manages them can turn two-step login off."""
    from django.core.exceptions import PermissionDenied

    person = User.objects.filter(pk=pk).first()
    if person is None or person.pk == request.user.pk or not request.user.can_manage_person(person):
        raise PermissionDenied
    person.totp_enabled, person.totp_secret = False, ""
    person.save(update_fields=["totp_enabled", "totp_secret"])
    messages.success(request, f"Two-step login is off for {person.display_name}.")
    return redirect("team")


