import re

from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import JsonResponse

from .models import USERNAME_PATTERN
from .rate_limit import admit_login


class AccountAdapter(DefaultAccountAdapter):
    def clean_username(self, username, shallow=False):
        username = username.lower()
        if not re.fullmatch(USERNAME_PATTERN, username):
            raise ValidationError("Use 3 to 30 ASCII letters, digits or underscores.")
        return super().clean_username(username, shallow=shallow)

    def pre_authenticate(self, request, **credentials):
        username = credentials.get("username", "").lower()
        if not re.fullmatch(USERNAME_PATTERN, username):
            raise self.validation_error("invalid_login")
        if settings.PHASE0_STRICT_RATE:
            retry_after = admit_login(username)
            if retry_after:
                raise ImmediateHttpResponse(
                    JsonResponse({"code": "login_rate_limited"}, status=429, headers={"Retry-After": str(retry_after)})
                )
        super().pre_authenticate(request, **credentials)
