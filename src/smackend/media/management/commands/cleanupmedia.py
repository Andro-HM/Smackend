from django.core.management.base import BaseCommand, CommandError

from smackend.media.service import cleanup


class Command(BaseCommand):
    help = "Retry storage deletions in a bounded batch. Stop upload workers before using --abandoned."

    def add_arguments(self, parser):
        parser.add_argument("--abandoned", action="store_true", help="Also reclaim pending uploads older than 24 hours.")

    def handle(self, *args, **options):
        removed, failed = cleanup(abandoned=options["abandoned"])
        self.stdout.write(f"Removed {removed} unused file objects.")
        if failed:
            raise CommandError(f"{failed} deletions failed and remain queued.")
