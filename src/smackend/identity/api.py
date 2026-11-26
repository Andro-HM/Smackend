from allauth.account.adapter import get_adapter
from allauth.account.internal.flows.reauthentication import did_recently_reauthenticate
from django.conf import settings
from django.contrib.auth import get_user_model, logout
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from ninja import Field, Router, Schema

from smackend.errors import APIError

from .permissions import session_auth
from .sessions import revoke_sessions


class RegistrationField(Schema):
    name: str
    required: bool


class Capabilities(Schema):
    signup: list[RegistrationField]
    allow_signup: bool
    password_login: list[str]
    otp: list[str]
    email_verification: str
    require_phone: bool
    reset: bool
    google: bool


class Profile(Schema):
    id: str
    username: str
    first_name: str
    last_name: str


class ProfileUpdate(Schema):
    first_name: str = Field(max_length=150)
    last_name: str = Field(max_length=150)


class UsernameUpdate(Schema):
    username: str = Field(min_length=3, max_length=30)


def profile_payload(user):
    return Profile(id=str(user.pk), username=user.get_username(), first_name=user.first_name, last_name=user.last_name)


def ensure_recent_proof(request):
    if not did_recently_reauthenticate(request):
        raise APIError(403, "reauthentication_required", "Confirm your identity first.")


def identity_router():
    router = Router(tags=["Account"], auth=session_auth)

    @router.get("/capabilities", auth=None, response=Capabilities)
    def capabilities(request):
        profile = settings.SMACKEND_IDENTITY_PROFILE
        return Capabilities(
            **{key: getattr(profile, key) for key in
               ("allow_signup", "password_login", "otp", "email_verification", "require_phone", "reset", "google")},
            signup=[RegistrationField(name=value.removesuffix("!"), required=value.endswith("!"))
                    for value in profile.signup],
        )

    @router.get("/profile", response=Profile)
    def profile(request):
        return profile_payload(request.user)

    @router.patch("/profile", response=Profile)
    def update_profile(request, data: ProfileUpdate):
        request.user.first_name = data.first_name
        request.user.last_name = data.last_name
        request.user.save(update_fields=["first_name", "last_name"])
        return profile_payload(request.user)

    @router.put("/username", response=Profile)
    def change_username(request, data: UsernameUpdate):
        if "username" not in settings.SMACKEND_IDENTITY_PROFILE.fields:
            raise APIError(404, "not_found", "Not found.")
        ensure_recent_proof(request)
        try:
            with transaction.atomic():
                user = get_user_model().objects.select_for_update().get(pk=request.user.pk, is_active=True)
                candidate = get_adapter(request).clean_username(data.username, shallow=True)
                setattr(user, user.USERNAME_FIELD, candidate)
                user.save(update_fields=[user.USERNAME_FIELD])
                revoke_sessions(user, exclude=request.session.session_key)
        except ValidationError:
            raise APIError(400, "invalid_username", "Use 3 to 30 ASCII letters, digits or underscores.") from None
        except IntegrityError:
            raise APIError(409, "account_conflict", "The account change could not be completed.") from None
        return profile_payload(user)

    @router.post("/deactivate", response={204: None})
    def deactivate(request):
        ensure_recent_proof(request)
        with transaction.atomic():
            user = get_user_model().objects.select_for_update().get(pk=request.user.pk)
            user.is_active = False
            user.save(update_fields=["is_active"])
            revoke_sessions(user)
            logout(request)
        return 204, None

    return router
