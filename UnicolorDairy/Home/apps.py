import os
import sys

from django.apps import AppConfig


class HomeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = 'Home'
    verbose_name = "Unicolors Daily Tasks"

    def ready(self):
        # Start the daily reminder checker when the website runs –
        # but not for commands like migrate, makemigrations or test.
        args = sys.argv
        if args and os.path.basename(args[0]) in ("manage.py", "django-admin"):
            if len(args) < 2 or args[1] != "runserver":
                return
            # runserver starts twice (a watcher + the real server); only start in the real one.
            if os.environ.get("RUN_MAIN") != "true" and "--noreload" not in args:
                return
        from . import scheduler
        scheduler.start()