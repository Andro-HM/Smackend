from allauth.headless.constants import Client
from django.conf import settings
from django.urls import include, path

from .headless import (
    ConfirmCode, ManageEmail, ManagePhone, RequestCode, RequestReset, ResendCode, ResetPassword,
    VerifyEmail, provider_redirect_view,
)


browser = {"client": Client.BROWSER}
urlpatterns = [
    path("browser/v1/auth/code/request", RequestCode.as_api_view(**browser)),
    path("browser/v1/auth/code/confirm", ConfirmCode.as_api_view(**browser)),
    path("browser/v1/auth/code/resend", ResendCode.as_api_view(**browser)),
    path("browser/v1/auth/password/request", RequestReset.as_api_view(**browser)),
    path("browser/v1/auth/password/reset", ResetPassword.as_api_view(**browser)),
    path("browser/v1/auth/email/verify", VerifyEmail.as_api_view(**browser)),
    path("browser/v1/account/email", ManageEmail.as_api_view(**browser)),
    path("browser/v1/account/phone", ManagePhone.as_api_view(**browser)),
]
if settings.SMACKEND_IDENTITY_PROFILE.google:
    urlpatterns += [path("browser/v1/auth/provider/redirect", provider_redirect_view())]
urlpatterns += [path("", include("allauth.headless.urls")), path("", include("allauth.urls"))]
