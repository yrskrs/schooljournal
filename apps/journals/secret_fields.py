"""Authenticated encryption for recoverable source credentials at rest.

Back up SECRET_KEY with the private configuration. SECRET_KEY_FALLBACKS permits
key rotation. Fernet: https://cryptography.io/en/stable/fernet/
"""
import base64

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

PREFIX = 'sj-token:v1:'


def cipher():
    secrets = [settings.SECRET_KEY, *settings.SECRET_KEY_FALLBACKS]
    return MultiFernet([Fernet(base64.urlsafe_b64encode(HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None,
        info=b'schooljournal/source-token/v1').derive(secret.encode()))) for secret in secrets])


def reveal(value):
    if not value or not value.startswith(PREFIX):
        # Read compatibility while the explicit data migration encrypts old rows.
        return value
    try:
        return cipher().decrypt(value[len(PREFIX):].encode()).decode()
    except (InvalidToken, UnicodeError, ValueError) as error:
        raise ValidationError('Не вдалося відкрити збережений ключ. Перевірте приватну конфігурацію та резервну копію.') from error


class EncryptedTokenField(models.TextField):
    """Values are plaintext in application memory and ciphertext in storage.

    Randomized ciphertext cannot be queried by equality; use token_fingerprint
    for grouping or explicitly compare scoped application values.
    """
    def from_db_value(self, value, expression, connection):
        return reveal(value)

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if not value:
            return value
        if value.startswith(PREFIX):
            reveal(value)  # Reject corrupt ciphertext rather than silently wrap it.
            return value
        return PREFIX + cipher().encrypt(value.encode()).decode()
