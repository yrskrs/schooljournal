"""LAN addresses only. Optional unprivileged host-network worker has no secrets."""
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import tempfile
import threading
import time
from urllib.parse import urlsplit, urlunsplit


def valid_address(value):
    try:
        address = ipaddress.ip_address(value.split('%', 1)[0])
        if not any((address.is_loopback, address.is_link_local, address.is_unspecified, address.is_multicast)):
            return str(address)
    except (ValueError, AttributeError):
        pass
    return None


def native_addresses():
    try:
        import psutil
        # Restricted containers can expose addresses but deny ioctl for link stats.
        try: stats = psutil.net_if_stats()
        except OSError: stats = {}
        found = []
        for name, rows in psutil.net_if_addrs().items():
            if name.startswith(('lo', 'docker', 'br-', 'veth', 'virbr', 'tun', 'tap', 'wg', 'tailscale')):
                continue
            if name in stats and not stats[name].isup:
                continue
            found.extend(row.address for row in rows if row.family in (socket.AF_INET, socket.AF_INET6))
    except (ImportError, OSError):
        try: found = socket.gethostbyname_ex(socket.gethostname())[2]
        except OSError: found = []
    return sorted({address for value in found if (address := valid_address(value))},
        key=lambda value: (':' in value, value))


def advertised_addresses():
    values = os.getenv('SCHOOL_LAN_ADDRESSES', '').split(',')
    path = Path(os.getenv('SCHOOL_LAN_INFO_FILE', '/run/school-lan/addresses.json'))
    try:
        if path.stat().st_size <= 8192:
            data = json.loads(path.read_text())
            if isinstance(data, dict) and isinstance(data.get('addresses'), list):
                values.extend(data['addresses'][:64])
    except (OSError, ValueError, TypeError):
        pass
    if not Path('/.dockerenv').exists():
        values.extend(native_addresses())
    return sorted({address for value in values if (address := valid_address(value))},
        key=lambda value: (':' in value, value))


def connection_addresses(current_url, default_port):
    current = urlsplit(current_url)
    rows = [{'url': current_url.rstrip('/'), 'label': 'Поточна адреса', 'current': True}]
    # A private-address request already reveals the actual published LAN port.
    port = current.port if valid_address(current.hostname) and current.port else int(default_port)
    for address in advertised_addresses():
        host = f'[{address}]' if ':' in address else address
        url = urlunsplit(('http', f'{host}:{port}', '', '', '')).rstrip('/')
        if url not in {row['url'] for row in rows}:
            rows.append({'url': url, 'label': address, 'current': False})
    return rows


def publish(path):
    """Atomic, bounded public metadata; never imports an application or database."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({'addresses': native_addresses()[:64], 'updated_at': time.time()})
    fd, temporary = tempfile.mkstemp(prefix='.lan-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(data)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def main():
    stopped = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *args: stopped.set())
    path = os.getenv('SCHOOL_LAN_INFO_FILE', '/inventory/addresses.json')
    while not stopped.is_set():
        try: publish(path)
        except OSError: pass
        stopped.wait(5)


if __name__ == '__main__':
    main()
