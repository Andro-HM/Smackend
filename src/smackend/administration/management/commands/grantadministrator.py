from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from smackend.administration.models import record
from smackend.identity.sessions import revoke_sessions


class Command(BaseCommand):
    help = "Promote an existing active account using trusted operator access."

    def add_arguments(self, parser):
        parser.add_argument("user_id", help="Database primary key of the account")

    @transaction.atomic
    def handle(self, *args, **options):
        users = get_user_model().objects.select_for_update()
        try:
            user = users.get(pk=options["user_id"], is_active=True)
        except (users.model.DoesNotExist, ValidationError, ValueError, OverflowError):
            raise CommandError("The account ID does not identify an active account.") from None
        if user.is_staff and user.is_superuser:
            self.stdout.write("The account already has administrator access.")
            return
        user.is_staff = True
        user.is_superuser = True
        user.save(update_fields=["is_staff", "is_superuser"])
        revoke_sessions(user)
        record("administrator_granted", user)
        self.stdout.write("Granted administrator access. The account must sign in again.")
