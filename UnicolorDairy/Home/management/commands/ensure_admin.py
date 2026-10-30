"""Create or repair the admin login from environment variables.

Runs on every deploy (see build.sh). Reads DJANGO_SUPERUSER_USERNAME,
DJANGO_SUPERUSER_EMAIL and DJANGO_SUPERUSER_PASSWORD, so changing those
values on the host and redeploying resets the admin's login.
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
        user.is_staff = True
        user.is_superuser = True
        user.is_active = True
        user.set_password(password)
        user.save()
        self.stdout.write(self.style.SUCCESS(
            f"Admin '{user.username}' ({user.email or 'no email'}) {'created' if created else 'updated'}."
        ))
