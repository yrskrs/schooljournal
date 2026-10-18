import json
import hashlib
from functools import lru_cache

from django.conf import settings


@lru_cache(maxsize=1)
def release_info():
    version = (settings.BASE_DIR / 'VERSION').read_text().strip()
    releases = json.loads((settings.BASE_DIR / 'RELEASES.json').read_text())
    return version, releases


@lru_cache(maxsize=1)
def site_script_version():
    return hashlib.sha256((settings.BASE_DIR / 'static/js/site.js').read_bytes()).hexdigest()[:12]


def site_context(request):
    return {'site_version': release_info()[0], 'site_script_version': site_script_version(),
            'school_registration_enabled': settings.SCHOOL_REGISTRATION_ENABLED}
