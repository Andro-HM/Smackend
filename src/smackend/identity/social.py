"""Google identities must be explicitly connected to an authenticated local account."""

from allauth.account.internal.flows.reauthentication import did_recently_reauthenticate
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.http import HttpResponseRedirect


class SocialAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(self, request, sociallogin):
        return False

    def pre_social_login(self, request, sociallogin):
        if sociallogin.state.get("process") == "connect" and not did_recently_reauthenticate(request):
            raise ImmediateHttpResponse(HttpResponseRedirect("/account/?provider=error"))
        if not sociallogin.is_existing and sociallogin.state.get("process") != "connect":
            raise ImmediateHttpResponse(HttpResponseRedirect("/account/?provider=error"))

    def on_authentication_error(self, request, provider, **kwargs):
        raise ImmediateHttpResponse(HttpResponseRedirect("/account/?provider=error"))
