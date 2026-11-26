import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "lab.settings")
application = get_asgi_application()

if os.environ.get("PHASE0_PROFILE") == "1":
    from .profile_asgi import instrument

    application = instrument(application)
