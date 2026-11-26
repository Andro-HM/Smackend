from allauth.usersessions.models import UserSession
from django.contrib.sessions.models import Session
from django.db import transaction


def revoke_sessions(user, *, exclude=None):
    tracked = UserSession.objects.filter(user=user)
    if exclude:
        tracked = tracked.exclude(session_key=exclude)
    with transaction.atomic():
        keys = list(tracked.values_list("session_key", flat=True))
        Session.objects.filter(session_key__in=keys).delete()
        tracked.delete()
