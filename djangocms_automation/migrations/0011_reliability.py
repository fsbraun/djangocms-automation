# Phase 0 reliability: retry accounting, dead letter, cancellation, scheduler lock.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('djangocms_automation', '0010_action_attempts_and_events'),
    ]

    operations = [
        migrations.CreateModel(
            name='SchedulerLock',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=64, unique=True, verbose_name='Lock name')),
                ('holder', models.UUIDField(blank=True, null=True, verbose_name='Holder')),
                ('locked_until', models.DateTimeField(blank=True, null=True, verbose_name='Locked until')),
            ],
            options={
                'verbose_name': 'Scheduler lock',
                'verbose_name_plural': 'Scheduler locks',
            },
        ),
        migrations.AddField(
            model_name='automationaction',
            name='dead_lettered',
            field=models.BooleanField(default=False, help_text='Set when the action exhausted its attempts and awaits inspection or replay.', verbose_name='Dead lettered'),
        ),
        migrations.AddField(
            model_name='automationaction',
            name='dead_lettered_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Dead lettered at'),
        ),
        migrations.AddField(
            model_name='automationaction',
            name='input_data',
            field=models.JSONField(blank=True, help_text='The rows this action was given, recorded at claim time so it can be replayed.', null=True, verbose_name='Input data'),
        ),
        migrations.AddField(
            model_name='automationaction',
            name='re_entry_count',
            field=models.PositiveIntegerField(default=0, help_text='How often a waiting node resumed after its children finished. Counted separately from retry attempts: a split or agent that re-enters many times has not failed even once.', verbose_name='Re-entry count'),
        ),
        migrations.AddField(
            model_name='automationaction',
            name='replayed_from',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='replays', to='djangocms_automation.automationaction', verbose_name='Replayed from'),
        ),
        migrations.AddField(
            model_name='automationaction',
            name='resumed',
            field=models.BooleanField(default=False, editable=False, help_text='Marks the next claim as a continuation rather than a new attempt. Set when a waiting node is woken or a paused action is revived; cleared when the action is claimed.', verbose_name='Resumed'),
        ),
        migrations.AlterField(
            model_name='automationaction',
            name='state',
            field=models.CharField(choices=[('PENDING', 'Pending'), ('RUNNING', 'Running'), ('WAITING', 'Waiting'), ('COMPLETED', 'Completed'), ('FAILED', 'Failed'), ('CANCELED', 'Canceled')], default='PENDING', max_length=20, verbose_name='State'),
        ),
        migrations.AlterField(
            model_name='automationactionevent',
            name='from_state',
            field=models.CharField(choices=[('PENDING', 'Pending'), ('RUNNING', 'Running'), ('WAITING', 'Waiting'), ('COMPLETED', 'Completed'), ('FAILED', 'Failed'), ('CANCELED', 'Canceled')], max_length=20, verbose_name='Previous state'),
        ),
        migrations.AlterField(
            model_name='automationactionevent',
            name='to_state',
            field=models.CharField(choices=[('PENDING', 'Pending'), ('RUNNING', 'Running'), ('WAITING', 'Waiting'), ('COMPLETED', 'Completed'), ('FAILED', 'Failed'), ('CANCELED', 'Canceled')], max_length=20, verbose_name='New state'),
        ),
        migrations.AlterField(
            model_name='automationinstance',
            name='status',
            field=models.CharField(choices=[('PENDING', 'Pending'), ('RUNNING', 'Running'), ('WAITING', 'Waiting'), ('COMPLETED', 'Completed'), ('FAILED', 'Failed'), ('CANCELED', 'Canceled')], default='RUNNING', max_length=20, verbose_name='Status'),
        ),
    ]
