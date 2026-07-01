import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# Build the HTTP ASGI app (and populate the app registry) BEFORE importing anything that
# touches models/consumers — the Channels docs pattern. Hence the E402 waivers below.
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

from jobs.routing import websocket_urlpatterns  # noqa: E402

# HTTP (DRF API + /healthz /readyz /metrics) stays on Django; WebSocket goes to Channels,
# origin-checked (CSWSH defence) to match the open REST API. See ADR 0004.
application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AllowedHostsOriginValidator(
            AuthMiddlewareStack(URLRouter(websocket_urlpatterns)),
        ),
    }
)
