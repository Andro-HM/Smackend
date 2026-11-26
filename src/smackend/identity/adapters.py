from dataclasses import dataclass
from datetime import timedelta
import re

from allauth.account.adapter import DefaultAccountAdapter
from allauth.account.auth_backends import AuthenticationBackend
from allauth.account.models import EmailAddress
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.headless.adapter import DefaultHeadlessAdapter
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sites.shortcuts import get_current_site
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .delivery import send_after_commit, send_sms
from .identifiers import canonical_email, canonical_username, is_e164
from .models import PhoneAddress


class VerifiedIdentifierBackend(AuthenticationBackend):
    def _authenticate(self, request, **credentials):
        username = credentials.get("username")
        if username:
            # Upstream accepts email as a username fallback. Keep identifier admission
            # and lookup in the same namespace so one identifier gets one budget.
            return self._authenticate_by_username(username, credentials["password"])
        return super()._authenticate(request, **credentials)

    def _authenticate_by_email(self, email, password):
        if "email" not in settings.SMACKEND_IDENTITY_PROFILE.password_login:
            return None
        verified = EmailAddress.objects.filter(email=canonical_email(email), verified=True).select_related("user").first()
        return self._check_password(verified.user if verified else None, password)

    def _authenticate_by_phone(self, phone, password):
        if "phone" not in settings.SMACKEND_IDENTITY_PROFILE.password_login:
            return None
        return super()._authenticate_by_phone(phone, password)

    def _authenticate_by_username(self, username, password):
        if "username" not in settings.SMACKEND_IDENTITY_PROFILE.password_login:
            return None
        return super()._authenticate_by_username(canonical_username(username), password)


class AccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        return settings.SMACKEND_IDENTITY_PROFILE.allow_signup and super().is_open_for_signup(request)

    def clean_username(self, username, shallow=False):
        username = canonical_username(username)
        if not re.fullmatch(r"[a-z0-9_]{3,30}", username):
            raise ValidationError("Use 3 to 30 ASCII letters, digits or underscores.", code="invalid_username")
        return super().clean_username(username, shallow=shallow)

    def clean_email(self, email):
        return super().clean_email(canonical_email(email))

    def clean_phone(self, phone):
        if not is_e164(phone):
            raise ValidationError("Enter a phone number in E.164 format.", code="invalid_phone")
        return phone

    def save_user(self, request, user, form, commit=True):
        # Hashing and the native allauth save precede any provider call.
        with transaction.atomic():
            return super().save_user(request, user, form, commit=commit)

    def send_mail(self, template_prefix, email, context):
        # Rendering needs the live allauth request context. Only delivery is deferred.
        message = self.render_mail(template_prefix, email, {
            "request": self.request, "email": email, "current_site": get_current_site(self.request), **context,
        })
        send_after_commit("email", email, message.send)

    def send_verification_code_sms(self, user, phone, code, **kwargs):
        send_sms(phone, f"Your verification code is {code}.")

    def send_unknown_account_sms(self, phone, **kwargs):
        send_sms(phone, "A code was requested. If you were expecting one, check your account details.")

    def send_account_already_exists_sms(self, phone):
        self.send_unknown_account_sms(phone)

    def get_phone(self, user):
        return PhoneAddress.objects.filter(user=user).values_list("number", "verified").first()

    def get_user_by_phone(self, phone):
        address = PhoneAddress.objects.filter(number=phone, verified=True).select_related("user").first()
        return address.user if address and address.user.is_active else None

    def set_phone(self, user, phone, verified):
        self.clean_phone(phone)
        PhoneAddress.objects.update_or_create(user=user, defaults={"number": phone, "verified": verified})

    def set_phone_verified(self, user, phone):
        from .sessions import revoke_sessions

        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=user.pk, is_active=True)
            previous = self.get_phone(user)
            if previous == (phone, True):
                return
            changing = previous is not None and previous[0] != phone
            if changing and (not self.request or self.request.user.pk != user.pk):
                raise PermissionDenied()
            self.set_phone(user, phone, True)
            if changing:
                revoke_sessions(user, exclude=self.request.session.session_key)

    def get_client_ip(self, request):
        from .rate_limit import client_address

        ip = client_address(request)
        return ip if ip != "unknown" else "0.0.0.0"

    def pre_login(self, request, user, **kwargs):
        if not user.is_active:
            from .middleware import identity_error

            raise ImmediateHttpResponse(identity_error(request, 403, "unavailable", "Sign-in could not be completed."))
        return super().pre_login(request, user, **kwargs)

    def login(self, request, user):
        super().login(request, user)
        request.session.set_expiry(timezone.now() + timedelta(seconds=settings.SESSION_COOKIE_AGE))


@dataclass
class PublicUser:
    id: str | None
    username: str | None
    display: str
    has_usable_password: bool


class HeadlessAdapter(DefaultHeadlessAdapter):
    def get_user_dataclass(self):
        return PublicUser

    def user_as_dataclass(self, user):
        actor = getattr(self.request, "user", None)
        if not actor or not actor.is_authenticated or actor.pk != user.pk:
            return PublicUser(None, None, "", False)
        username = user.get_username()
        return PublicUser(str(user.pk), username, username, user.has_usable_password())
