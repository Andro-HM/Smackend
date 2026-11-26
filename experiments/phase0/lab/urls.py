from allauth.headless.account.views import LoginView, SessionView, SignupView
from allauth.headless.constants import Client
from django.urls import path
from ninja import NinjaAPI
from ninja.security import django_auth

from habits.api import router as habits_router
from notes.api import router as notes_router

from . import provider, views

api = NinjaAPI(auth=django_auth, docs_url=None, openapi_url=None)
api.add_router("/notes", notes_router)
api.add_router("/habits", habits_router)
api.add_router("/provider", provider.router)
for error in views.DATABASE_ERRORS:
    api.add_exception_handler(error, lambda request, exc: views.unavailable())

urlpatterns = [
    path("", views.page, {"template": "login.html"}),
    path("notes/", views.page, {"template": "notes.html"}),
    path("assets/browser.js", views.asset, {"filename": "browser.js"}),
    path("assets/login.js", views.asset, {"filename": "login.js"}),
    path("assets/notes.js", views.asset, {"filename": "notes.js"}),
    path("assets/lab.css", views.asset, {"filename": "lab.css"}),
    path("live", views.live),
    path("ready", views.ready),
    path("_allauth/browser/v1/auth/login", LoginView.as_api_view(client=Client.BROWSER)),
    path("_allauth/browser/v1/auth/signup", SignupView.as_api_view(client=Client.BROWSER)),
    path("_allauth/browser/v1/auth/session", SessionView.as_api_view(client=Client.BROWSER)),
    path("api/", api.urls),
]
