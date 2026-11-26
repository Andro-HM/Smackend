import os

from django.core.asgi import get_asgi_application
from django.conf import settings

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
application = get_asgi_application()
if settings.SMACKEND_REALTIME_ENABLED:
    from smackend.realtime.routing import with_realtime

    application = with_realtime(application)
