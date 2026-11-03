"""Removes leave and office holidays from the app, along with everything stored for them.

Deletes every leave request, every office holiday and every leave notification. Sundays stay
closed: that comes from the WEEKLY_OFF_DAYS setting, not from this data.
"""
from django.db import migrations, models


def delete_leave_notifications(apps, schema_editor):
    Notification = apps.get_model("Home", "Notification")
    Notification.objects.using(schema_editor.connection.alias).filter(verb="leave").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("Home", "0019_clear_test_data"),
    ]

    operations = [
        migrations.RunPython(delete_leave_notifications, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="notification",
            name="verb",
            field=models.CharField(
                choices=[
                    ("assigned", "Assigned"),
                    ("started", "Started"),
                    ("submitted", "Submitted"),
                    ("approved", "Approved"),
                    ("changes", "Changes requested"),
                    ("reopened", "Reopened"),
                    ("deleted", "Deleted"),
                    ("commented", "Commented"),
                    ("mentioned", "Mentioned"),
                    ("reminder", "Reminder"),
                    ("overdue", "Overdue"),
                    ("edited", "Edited"),
                    ("restored", "Restored"),
                    ("hold", "On hold"),
                    ("resumed", "Resumed"),
                    ("extension", "Extension"),
                    ("request", "Task request"),
                    ("file", "File added"),
                ],
                max_length=12,
            ),
        ),
        migrations.DeleteModel(name="Leave"),
        migrations.DeleteModel(name="Holiday"),
    ]
