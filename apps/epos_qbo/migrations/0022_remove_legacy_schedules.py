"""Remove the legacy schedules (5 Oct 2026: one scheduler, nothing legacy).

* the system-managed env-fallback row ("Legacy Env Fallback") and the ``is_system_managed`` field it
  needed: the schedule worker no longer has an env fallback;
* inventory sync schedules: the legacy inventory tools can no longer be scheduled.

Their events and jobs stay (foreign keys are SET_NULL). The row deletion is not reversed.
"""
from django.db import migrations


def remove_legacy(apps, schema_editor):
    RunSchedule = apps.get_model("epos_qbo", "RunSchedule")
    RunSchedule.objects.filter(is_system_managed=True).delete()
    RunSchedule.objects.filter(scope__in=["inventory_pipeline", "inventory_sync"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('epos_qbo', '0021_workflow_scheduling'),
    ]

    operations = [
        migrations.RunPython(remove_legacy, migrations.RunPython.noop),
        migrations.RemoveIndex(
            model_name='runschedule',
            name='epos_qbo_rs_system_enabled_idx',
        ),
        migrations.RemoveField(
            model_name='runschedule',
            name='is_system_managed',
        ),
    ]
