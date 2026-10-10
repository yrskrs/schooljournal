import json
from functools import lru_cache

from django.conf import settings


@lru_cache(maxsize=1)
def release_info():
    version = (settings.BASE_DIR / 'VERSION').read_text().strip()
    releases = json.loads((settings.BASE_DIR / 'RELEASES.json').read_text())
    return version, releases


def site_context(request):
    return {'site_version': release_info()[0], 'school_registration_enabled': settings.SCHOOL_REGISTRATION_ENABLED}
