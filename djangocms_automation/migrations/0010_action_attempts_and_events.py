import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("djangocms_automation", "0009_add_idempotency_key"),
    ]

    operations = [
        migrations.AddField(
            model_name="automationaction",
            name="attempt_count",
            field=models.PositiveIntegerField(default=0, verbose_name="Attempt count"),
        ),
        migrations.AddField(
            model_name="automationaction",
            name="error_detail",
            field=models.TextField(blank=True, verbose_name="Error detail"),
        ),
        migrations.AddField(
            model_name="automationaction",
            name="error_type",
            field=models.CharField(blank=True, max_length=256, verbose_name="Error type"),
        ),
        migrations.AddField(
            model_name="automationaction",
            name="heartbeat_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Last heartbeat"),
        ),
        migrations.AddField(
            model_name="automationaction",
            name="lease_id",
            field=models.UUIDField(blank=True, editable=False, null=True, verbose_name="Execution lease"),
        ),
        migrations.AddField(
            model_name="automationaction",
            name="max_attempts",
            field=models.PositiveIntegerField(default=1, verbose_name="Maximum attempts"),
        ),
        migrations.AddField(
            model_name="automationaction",
            name="next_attempt_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Next attempt at"),
        ),
        migrations.AddField(
            model_name="automationaction",
            name="started",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Started"),
        ),
        migrations.AddField(
            model_name="automationaction",
            name="timeout_seconds",
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name="Timeout in seconds"),
        ),
        migrations.CreateModel(
            name="AutomationActionEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "from_state",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("RUNNING", "Running"),
                            ("WAITING", "Waiting"),
                            ("COMPLETED", "Completed"),
                            ("FAILED", "Failed"),
                        ],
                        max_length=20,
                        verbose_name="Previous state",
                    ),
                ),
                (
                    "to_state",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("RUNNING", "Running"),
                            ("WAITING", "Waiting"),
                            ("COMPLETED", "Completed"),
                            ("FAILED", "Failed"),
                        ],
                        max_length=20,
                        verbose_name="New state",
                    ),
                ),
                ("attempt", models.PositiveIntegerField(default=0, verbose_name="Attempt")),
                (
                    "lease_id",
                    models.UUIDField(blank=True, editable=False, null=True, verbose_name="Execution lease"),
                ),
                ("metadata", models.JSONField(blank=True, default=dict, verbose_name="Metadata")),
                ("created", models.DateTimeField(auto_now_add=True)),
                (
                    "action",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="events",
                        to="djangocms_automation.automationaction",
                        verbose_name="Action",
                    ),
                ),
            ],
            options={
                "verbose_name": "Action event",
                "verbose_name_plural": "Action events",
                "ordering": ("created", "pk"),
            },
        ),
    ]
