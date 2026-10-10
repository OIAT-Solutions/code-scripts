from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.epos_qbo.services import housekeeping


class Command(BaseCommand):
    help = "Delete working files older than a month (Akponora daily-run evidence: three months)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="List what would be removed; remove nothing.")

    def handle(self, *args, **options):
        res = housekeeping.prune(dry_run=options["dry_run"])
        for path in res.removed:
            self.stdout.write(path)
        verb = "Would remove" if options["dry_run"] else "Removed"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {res.removed_dirs} folder(s) and {res.removed_files} file(s), {res.freed_bytes / 1e6:.1f} MB."))
