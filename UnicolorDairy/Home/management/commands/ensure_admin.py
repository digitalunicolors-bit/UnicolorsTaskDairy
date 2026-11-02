"""Create or repair the admin login from environment variables.

Runs on every deploy (see build.sh). Reads DJANGO_SUPERUSER_USERNAME,
DJANGO_SUPERUSER_EMAIL and DJANGO_SUPERUSER_PASSWORD.

A normal deploy only creates the owner if it is missing. It never touches an existing
owner's password, so a password changed inside the app stays changed.

Locked out? Set DJANGO_SUPERUSER_FORCE_RESET=1 on the host and redeploy: that sets the
password from the environment and turns off two-step login. Then remove the setting.
"""
import os

from django.core.management.base import BaseCommand

from Home.models import User


class Command(BaseCommand):
    help = "Create or update the admin user from DJANGO_SUPERUSER_* environment variables."

    def handle(self, *args, **options):
        username = os.environ.get("DJANGO_SUPERUSER_USERNAME", "").strip()
        email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "").strip()
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "").strip()
        if not username or not password:
            self.stdout.write("DJANGO_SUPERUSER_USERNAME / PASSWORD not set - skipping admin setup.")
            return

        user = User.objects.filter(username__iexact=username).first()
        created = user is None
        if created:
            user = User(username=username)
        if email:
            user.email = email
        force_reset = os.environ.get("DJANGO_SUPERUSER_FORCE_RESET", "").strip().lower() in ("1", "true", "yes", "on")
        user.is_staff = True
        user.is_superuser = True
        user.is_active = True
        if created or force_reset:
            user.set_password(password)
        if force_reset:
            user.totp_enabled = False
            user.totp_secret = ""
            user.must_change_password = True
        user.save()
        self.stdout.write(self.style.SUCCESS(
            f"Admin '{user.username}' ({user.email or 'no email'}) {'created' if created else 'updated'}."
        ))
