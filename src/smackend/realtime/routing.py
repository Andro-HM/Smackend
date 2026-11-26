from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import OriginValidator
from django.conf import settings
from django.http.request import split_domain_port, validate_host
from django.urls import path

from .consumers import Socket, VoiceSocket


def strict_headers(inner):
    async def application(scope, receive, send):
        origins = [value for key, value in scope["headers"] if key.lower() == b"origin"]
        hosts = [value for key, value in scope["headers"] if key.lower() == b"host"]
        host = split_domain_port(hosts[0].decode("ascii", "replace"))[0] if len(hosts) == 1 else ""
        if len(origins) != 1 or not host or not validate_host(host, settings.ALLOWED_HOSTS):
            await receive()
            await send({"type": "websocket.close", "code": 4403})
            return
        await inner(scope, receive, send)

    return application


def with_realtime(http_application, extra_routes=()):
    routes = [
        *extra_routes,
        path("ws/realtime/", Socket.as_asgi()),
        path("ws/voice/", VoiceSocket.as_asgi()),
    ]
    return ProtocolTypeRouter({
        "http": http_application,
        "websocket": strict_headers(
            OriginValidator(AuthMiddlewareStack(URLRouter(routes)), settings.SMACKEND_REALTIME_ALLOWED_ORIGINS),
        ),
    })
