"""Bounded, nonredirecting requests to administrator-configured peers."""
import ipaddress
import json
import socket
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from .core import SyncError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(peer):
    try:
        url, token = peer['url'], peer['token']
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise SyncError('Некоректна адреса API списків.')
        for result in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM):
            address = ipaddress.ip_address(result[4][0])
            address = address.ipv4_mapped or address if isinstance(address, ipaddress.IPv6Address) else address
            if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
                raise SyncError('Для обміну потрібна мережева адреса іншого сервісу.')
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(Request(url, headers={'Accept': 'application/json', 'Authorization': 'Bearer ' + token}), timeout=6) as response:
            if response.status != 200:
                raise SyncError(f'API списків повернув HTTP {response.status}.')
            raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise SyncError('Відповідь списків перевищує 8 МБ.')
        return json.loads(raw)
    except HTTPError as error:
        raise SyncError(f'API списків повернув HTTP {error.code}. Перевірте адресу, ключ і версію.') from error
    except (URLError, OSError, ValueError, KeyError, TypeError) as error:
        raise SyncError('Сервіс списків недоступний або повернув некоректну відповідь. Локальні дані збережено.') from error
