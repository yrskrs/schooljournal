"""Isolated synthetic-data tests; never load the working .env or database."""
from unittest.mock import patch

with patch('dotenv.load_dotenv'):
    from .settings import *  # noqa: F403

DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
SECRET_KEY = 'isolated-synthetic-journal-tests-only'
ALLOWED_HOSTS = ['testserver', 'localhost', '127.0.0.1']
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
MEDIA_ROOT = '/tmp/schooljournal-test-media'
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
