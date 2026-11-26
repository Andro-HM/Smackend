"""Use allauth's flows while restricting proof to currently verified identifiers."""

from hashlib import sha256

from allauth.account.internal.flows.password_reset_by_code import PASSWORD_RESET_VERIFICATION_SESSION_KEY
from allauth.account.internal.flows.reauthentication import did_recently_reauthenticate
from allauth.account.internal.flows.login_by_code import LoginCodeVerificationProcess
from allauth.account.models import EmailAddress
from allauth.account.stages import LoginStageController, LoginStageKey
from allauth.headless.account.inputs import RequestLoginCodeInput, RequestPasswordResetInput
from allauth.headless.account.views import (
    ConfirmLoginCodeView, ManageEmailView, ManagePhoneView, RequestLoginCodeView,
    RequestPasswordResetView, ResendLoginCodeView, ResetPasswordView, VerifyEmailView,
)
from allauth.headless.base.response import APIResponse
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction

from .identifiers import canonical_email
from .middleware import identity_error
from .models import PhoneAddress


def verified_email(email):
    return EmailAddress.objects.filter(email=canonical_email(email), verified=True, user__is_active=True)


class VerifiedLoginCodeInput(RequestLoginCodeInput):
    def clean_email(self):
        email = super().clean_email()
        if email:
            address = verified_email(email).select_related("user").first()
            self._user = address.user if address else None
        return email


class RequestCode(RequestLoginCodeView):
    input_class = VerifiedLoginCodeInput


class ConfirmCode(ConfirmLoginCodeView):
    def post(self, request, *args, **kwargs):
        process = self.process
        user = process.user
        email = process.state.get("email")
        phone = process.state.get("phone")
        if user:
            # Keep credential ownership stable until the sign-in and session writes finish.
            active = get_user_model().objects.select_for_update().filter(pk=user.pk, is_active=True).exists()
            has_email = email and verified_email(email).filter(user=user).exists()
            has_phone = phone and PhoneAddress.objects.filter(user=user, number=phone, verified=True).exists()
            if not active or not (has_email or has_phone):
                process.abort()
                return identity_error(request, 409, "expired_flow", "Request a new code.")
        else:
            process.abort()
            return identity_error(request, 409, "expired_flow", "Request a new code.")
        return super().post(request, *args, **kwargs)


class ResendCode(ResendLoginCodeView):
    def post(self, request, *args, **kwargs):
        stage = LoginStageController.enter(request, LoginStageKey.LOGIN_BY_CODE)
        process = LoginCodeVerificationProcess.resume(stage) if stage else None
        if process and process.can_resend and process.user is None:
            process.record_resend()
            process.persist()
            return APIResponse(request)
        return super().post(request, *args, **kwargs)


class ManagePhone(ManagePhoneView):
    def get_input_kwargs(self):
        kwargs = super().get_input_kwargs()
        if self.request.user.is_authenticated and kwargs["phone"]:
            if not PhoneAddress.objects.filter(user=self.user, verified=True).exists():
                # An authenticated user can verify the existing pending number.
                kwargs["phone"] = None
        return kwargs


class ManageEmail(ManageEmailView):
    def dispatch(self, request, *args, **kwargs):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.user.is_authenticated:
            with transaction.atomic():
                active = get_user_model().objects.select_for_update().filter(pk=request.user.pk, is_active=True).exists()
                if not active:
                    return identity_error(request, 403, "unavailable", "The account is unavailable.")
                return super().dispatch(request, *args, **kwargs)
        return super().dispatch(request, *args, **kwargs)


class VerifyEmail(VerifyEmailView):
    def post(self, request, *args, **kwargs):
        address = self.process.email_address if self.process else self.input.verification.email_address
        with transaction.atomic():
            active = get_user_model().objects.select_for_update().filter(pk=address.user_id, is_active=True).exists()
            pending = not address.pk or EmailAddress.objects.filter(pk=address.pk, verified=False).exists()
            if not active or not pending:
                return identity_error(request, 409, "expired_flow", "Request a new verification message.")
            return super().post(request, *args, **kwargs)


class VerifiedResetInput(RequestPasswordResetInput):
    def clean_email(self):
        email = super().clean_email()
        self.users = [address.user for address in verified_email(email).select_related("user")[:1]]
        return email


def password_stamp(user):
    return sha256(user.password.encode()).hexdigest()


class RequestReset(RequestPasswordResetView):
    input_class = VerifiedResetInput

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if self.input.users and response.status_code != 429:
            state = request.session.get(PASSWORD_RESET_VERIFICATION_SESSION_KEY)
            if state:
                state["password_stamp"] = password_stamp(self.input.users[0])
                request.session[PASSWORD_RESET_VERIFICATION_SESSION_KEY] = state
        return response


class ResetPassword(ResetPasswordView):
    def post(self, request, *args, **kwargs):
        supplied_user = self.input.user
        if not supplied_user:
            return identity_error(request, 409, "expired_flow", "Request a new code.")
        user = get_user_model().objects.select_for_update().filter(pk=supplied_user.pk, is_active=True).first()
        state = self.process.state
        if not user or state.get("password_stamp") != password_stamp(user) or not verified_email(
            state["email"],
        ).filter(user=user).exists():
            self.process.abort()
            return identity_error(request, 409, "expired_flow", "Request a new code.")
        return super().post(request, *args, **kwargs)


def provider_redirect_view():
    from allauth.headless.constants import Client
    from allauth.headless.socialaccount.views import RedirectToProviderView

    class ProviderRedirect(RedirectToProviderView):
        def post(self, request, *args, **kwargs):
            if request.POST.get("provider") != "google" or not settings.SMACKEND_IDENTITY_PROFILE.google:
                return identity_error(request, 404, "not_found", "Not found.")
            if request.POST.get("process") == "connect" and not did_recently_reauthenticate(request):
                return identity_error(request, 403, "reauthentication_required", "Confirm your identity first.")
            if request.POST.get("callback_url") != "/account/":
                return identity_error(request, 400, "invalid_input", "Use the same-origin account callback.")
            return super().post(request, *args, **kwargs)

    return ProviderRedirect.as_api_view(client=Client.BROWSER)
