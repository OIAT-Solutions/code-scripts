from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.epos_qbo.services import workflows


class Command(BaseCommand):
    help = ("Create the portal 'Nora daily routine' schedule if it is missing (paused). It only queues work once "
            f"{workflows.OWNER_ENV}=portal and it is enabled (cutover, docs/SCHEDULING_AUTHORITY.md). "
            "Never changes an existing schedule.")

    def handle(self, *args, **options):
        schedule, created = workflows.ensure_daily_routine_schedule()
        state = "created (paused)" if created else "already exists"
        self.stdout.write(f"{schedule.name}: {state}; enabled={schedule.enabled}; cron '{schedule.cron_expr}' "
                          f"{schedule.timezone_name}; next due {schedule.next_fire_at}; "
                          f"owner now: {workflows.company_a_daily_owner()}")
