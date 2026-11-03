from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("Home", "0017_morning_summary"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="leave",
            name="cover",
        ),
        migrations.AddField(
            model_name="leave",
            name="day_part",
            field=models.CharField(
                choices=[
                    ("full", "Full day"),
                    ("first_half", "First half"),
                    ("second_half", "Second half"),
                ],
                default="full",
                max_length=12,
            ),
        ),
    ]
