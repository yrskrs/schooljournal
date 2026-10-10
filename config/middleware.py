"""Keep LAN hosts and authentication independent of other apps on the same host."""
import ipaddress
from importlib import import_module

from django.conf import settings
from django.http.request import split_domain_port

PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7',
))


class PrivateNetworkHostMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.ALLOW_PRIVATE_NETWORK_HOSTS:
            raw = request._get_raw_host()
            domain, port = split_domain_port(raw)
            try:
                address = ipaddress.ip_address(domain.strip('[]'))
                valid_port = not port or 1 <= int(port) <= 65535
                if valid_port and any(address in network for network in PRIVATE_NETWORKS):
                    # Per-request validation; never mutate global ALLOWED_HOSTS.
                    request.get_host = lambda: raw
            except ValueError:
                pass
        return self.get_response(request)


class LegacySessionCookieMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        legacy = request.COOKIES.get('sessionid')
        if not request.COOKIES.get(settings.SESSION_COOKIE_NAME) and legacy:
            store = import_module(settings.SESSION_ENGINE).SessionStore(session_key=legacy)
            # Only this database's signed session is eligible. Django still
            # validates the user, backend and auth hash in AuthenticationMiddleware.
            if store.get('_auth_user_id'):
                request.session = store
                request.session.modified = True
        return self.get_response(request)
