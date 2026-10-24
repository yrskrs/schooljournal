"""Owner-scoped services and network addresses; journal bindings keep their IDs."""
import hashlib
from urllib.parse import urlunsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from .secret_fields import EncryptedTokenField

PORT_VALIDATORS = [MinValueValidator(1), MaxValueValidator(65535)]
SOURCE_CHOICES = [('schooltest', 'SchoolTest · Тести'), ('schoolwork', 'SchoolNet · Завдання')]


class NetworkProfile(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='network_profiles')
    name = models.CharField(max_length=100)
    host = models.CharField(max_length=253, blank=True)
    scheme = models.CharField(max_length=5, choices=[('http', 'HTTP'), ('https', 'HTTPS')], default='http')
    schooltest_port = models.PositiveIntegerField(default=8080, validators=PORT_VALIDATORS)
    schoolwork_port = models.PositiveIntegerField(default=8000, validators=PORT_VALIDATORS)
    use_journal_host = models.BooleanField(default=False)
    enabled = models.BooleanField(default=True)

    class Meta:
        ordering = ['pk']
        constraints = [models.UniqueConstraint(fields=['owner', 'name'], name='network_owner_name_unique')]

    def clean(self):
        from .services.external_grades import validate_api_url
        if self.use_journal_host:
            return
        if not self.host or any(c in self.host for c in '/@?#'):
            raise ValidationError({'host': 'Вкажіть лише IP або hostname, без порту, ключа та шляху.'})
        host = f'[{self.host}]' if ':' in self.host else self.host
        validate_api_url(f'{self.scheme}://{host}:8000/')

    def __str__(self):
        return self.name


class NetworkSelection(models.Model):
    owner = models.OneToOneField(settings.AUTH_USER_MODEL, primary_key=True, on_delete=models.CASCADE,
        related_name='network_selection')
    selected_profile = models.ForeignKey(NetworkProfile, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+')
    last_profile = models.ForeignKey(NetworkProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')

    def clean(self):
        for profile in (self.selected_profile, self.last_profile):
            if profile and profile.owner_id != self.owner_id:
                raise ValidationError('Профіль мережі належить іншому вчителю.')


class ServiceConnection(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='service_connections')
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES)
    name = models.CharField(max_length=100)
    instance_id = models.UUIDField(null=True, blank=True)
    identity_key = models.CharField(max_length=180)
    token = EncryptedTokenField(blank=True)
    token_fingerprint = models.CharField(max_length=64, blank=True)
    enabled = models.BooleanField(default=True)
    version = models.CharField(max_length=40, blank=True)
    token_status = models.CharField(max_length=16, default='unknown')
    permissions = models.JSONField(default=dict, blank=True)
    roster_cache = models.JSONField(default=dict, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    last_endpoint = models.ForeignKey('ServiceEndpoint', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['source', 'pk']
        constraints = [models.UniqueConstraint(fields=['owner', 'source', 'identity_key'], name='service_identity_unique')]

    def set_token(self, token):
        self.token = token.strip()
        self.token_fingerprint = hashlib.sha256(self.token.encode()).hexdigest() if self.token else ''

    def __str__(self):
        return self.name


class ServiceEndpoint(models.Model):
    connection = models.ForeignKey(ServiceConnection, on_delete=models.CASCADE, related_name='endpoints')
    profile = models.ForeignKey(NetworkProfile, on_delete=models.CASCADE, related_name='endpoints')
    port_override = models.PositiveIntegerField(null=True, blank=True, validators=PORT_VALIDATORS)
    path_prefix = models.CharField(max_length=300, blank=True)
    enabled = models.BooleanField(default=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(max_length=24, default='unknown')
    last_error = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ['pk']
        constraints = [models.UniqueConstraint(fields=['connection', 'profile', 'port_override', 'path_prefix'],
            name='service_endpoint_unique')]

    def clean(self):
        if self.connection.owner_id != self.profile.owner_id:
            raise ValidationError('Адреса й підключення мають належати одному вчителю.')
        if self.path_prefix and (not self.path_prefix.startswith('/') or any(c in self.path_prefix for c in '?#\\')):
            raise ValidationError({'path_prefix': 'Префікс шляху має починатися з / і не містити параметрів.'})

    def site_url(self, journal_host=None):
        host = journal_host if self.profile.use_journal_host else self.profile.host
        if not host:
            raise ValidationError('Адреса цієї мережі ще не вказана. Відкрийте «Підключення».')
        host = f'[{host}]' if ':' in host and not host.startswith('[') else host
        port = self.port_override or getattr(self.profile, self.connection.source + '_port')
        return urlunsplit((self.profile.scheme, f'{host}:{port}', self.path_prefix.rstrip('/'), '', '')).rstrip('/')

    def api_url(self, kind, journal_host=None):
        path = '/api/v2/ping' if kind == 'ping' else '/api/v2/journal/' + kind + '/'
        return self.site_url(journal_host) + path
