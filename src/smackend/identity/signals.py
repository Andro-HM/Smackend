from allauth.account.signals import email_changed, email_removed
from django.dispatch import receiver

from .sessions import revoke_sessions


@receiver(email_changed, dispatch_uid="smackend.revoke_on_email_change")
@receiver(email_removed, dispatch_uid="smackend.revoke_on_email_remove")
def end_other_sessions(sender, request, user, **kwargs):
    revoke_sessions(user, exclude=request.session.session_key)
