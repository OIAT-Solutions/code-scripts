from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.epos_qbo.services import workflows


class Command(BaseCommand):
    help = "Create Nora's Daily routine schedule if it is missing (paused). Never changes an existing schedule."

    def handle(self, *args, **options):
        schedule, created = workflows.ensure_daily_routine_schedule()
        state = "created (paused)" if created else "already exists"
        self.stdout.write(f"{schedule.name}: {state}; enabled={schedule.enabled}; cron '{schedule.cron_expr}' "
                          f"{schedule.timezone_name}; next due {schedule.next_fire_at}")
