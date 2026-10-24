"""Small additive discovery messages; never include rosters, names or keys."""
from uuid import UUID

from .core import SyncError

SOURCES = {'schooljournal', 'schooltest', 'schoolwork'}


def ping_payload(source, instance_id, version):
    if source not in SOURCES:
        raise SyncError('Невідомий тип сервісу.')
    return {'source': source, 'instance_id': str(UUID(str(instance_id))), 'version': str(version)[:40]}


def validate_ping(payload, source, instance_id=None):
    if not isinstance(payload, dict) or payload.get('source') != source:
        raise SyncError('За цією адресою інший тип сайту. Перевірте порт сервісу в профілі мережі.')
    try:
        identity = str(UUID(payload['instance_id']))
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise SyncError('Сайт не підтвердив ідентичність. Оновіть сервіс і перевірте адресу.') from error
    if instance_id is not None and identity != str(instance_id):
        raise SyncError('За цією адресою інший екземпляр сайту. Ключ і дані не передано; перевірте профіль мережі.')
    version = payload.get('version')
    if not isinstance(version, str) or not version.strip() or len(version) > 40:
        raise SyncError('Сайт повернув некоректну версію. Перевірте адресу та оновлення сервісу.')
    return {'source': source, 'instance_id': identity, 'version': version}
