"""Create optional automatic addresses without changing any journal identity."""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from apps.journals.models import NetworkProfile, ServiceConnection, ServiceEndpoint


def unique_name(owner, name):
    suffix = 1
    candidate = name
    while NetworkProfile.objects.filter(owner=owner, name=candidate).exists():
        suffix += 1
        candidate = f'{name} · {suffix}'
    return candidate


@transaction.atomic
def ensure_automatic_profiles(owner):
    get_user_model().objects.select_for_update().only('pk').get(pk=owner.pk)
    profiles = []
    profile = NetworkProfile.objects.filter(owner=owner, use_journal_host=True).first()
    if profile is None:
        name = unique_name(owner, 'Поточна адреса журналу')
        profile = NetworkProfile.objects.create(owner=owner, name=name, use_journal_host=True,
            schooltest_port=settings.LOCAL_SCHOOLTEST_PORT, schoolwork_port=settings.LOCAL_SCHOOLWORK_PORT)
    profiles.append(profile)
    if settings.LOCAL_SERVER_PROFILE_ENABLED:
        profile = NetworkProfile.objects.filter(owner=owner, host='host.docker.internal', use_journal_host=False).first()
        if profile is None:
            profile = NetworkProfile.objects.create(owner=owner, host='host.docker.internal',
                name=unique_name(owner, 'Цей сервер'), schooltest_port=settings.LOCAL_SCHOOLTEST_PORT,
                schoolwork_port=settings.LOCAL_SCHOOLWORK_PORT)
        profiles.append(profile)
    for service in ServiceConnection.objects.filter(owner=owner):
        for profile in profiles:
            if not ServiceEndpoint.objects.filter(connection=service, profile=profile).exists():
                defaults = {}
                if profile.use_journal_host:
                    template = service.endpoints.filter(profile__use_journal_host=False).exclude(
                        profile__host='host.docker.internal').select_related('profile').first()
                    if template:
                        defaults = {'path_prefix': template.path_prefix,
                            'port_override': template.port_override or getattr(template.profile, service.source + '_port')}
                ServiceEndpoint.objects.create(connection=service, profile=profile, **defaults)
    return profiles
