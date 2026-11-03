"""One-time clean-up of the test data on the live site, ahead of real use.

Keeps every user account and login. Removes tasks and everything attached to them, clients,
holidays, leave, recurring tasks, notifications, admin checklists and email records.

Runs only where DATABASE_URL is set (the live site), so a copy running on someone's own
computer keeps its data. Django runs a migration once per database, so this never repeats.
"""
import os

from django.db import migrations

# Children before parents, so the counts in the deploy log are meaningful.
MODELS = [
    "TaskReminder", "TaskComment", "TaskAttachment", "TaskActivity", "Subtask", "ExtensionRequest",
    "TaskRequest", "Task", "RecurringTask", "Leave", "Holiday", "Client", "Notification",
    "AdminNote", "DailySummary", "EmailFailure",
]


def clear_test_data(apps, schema_editor):
    if not os.environ.get("DATABASE_URL"):
        return
    for name in MODELS:
        model = apps.get_model("Home", name)
        count = model.objects.using(schema_editor.connection.alias).count()
        model.objects.using(schema_editor.connection.alias).all().delete()
        print(f"  cleared {name}: {count}")


class Migration(migrations.Migration):
    dependencies = [
        ("Home", "0018_leave_day_part_remove_cover"),
    ]

    operations = [
        migrations.RunPython(clear_test_data, migrations.RunPython.noop),
    ]
