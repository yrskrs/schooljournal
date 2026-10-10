import secrets
from django.db import transaction
from apps.accounts.models import User
from apps.schools.models import School
from apps.journals.models import NZAccount, Journal


@transaction.atomic
def ensure_personal_account(account):
    """Keep the NZ identity independent of an administration-created login."""
    account = NZAccount.objects.select_for_update().select_related('user').get(pk=account.pk)
    if account.user.is_nz_personal:
        return account
    local_user = account.user
    school = School.objects.create(name='Особистий простір журналів nz.ua')
    personal = User.objects.create_user(username='nz_' + secrets.token_hex(12), school=school,
        role=User.Role.TEACHER, is_nz_personal=True, first_name=local_user.first_name,
        last_name=local_user.last_name, patronymic=local_user.patronymic)
    # Existing journals remain in their original school and retain all records.
    Journal.objects.filter(teacher=local_user).exclude(nz_journal_id='').filter(
        nz_owner__isnull=True).update(nz_owner=account)
    Journal.objects.filter(nz_owner=account).update(teacher=personal, local_profile=local_user)
    account.user = personal
    account.save(update_fields=['user'])
    return account


def journal_nz_account(journal, user):
    return journal.nz_owner if journal.nz_owner_id else getattr(user, 'nz_account', None)
