"""Optional device shortcut, issued only after authentication; contains no NZ secrets."""
from django.core import signing
from django.utils.crypto import salted_hmac, constant_time_compare

from apps.journals.models import NZAccount

COOKIE = 'journal_nz_device'
SALT = 'journal-nz-device-v1'
MAX_AGE = 30 * 24 * 60 * 60


def account_fingerprint(account):
    state = f'{account.pk}:{account.user_id}:{account.phpsessid}:{account.last_synced_at}:{account.user.get_session_auth_hash()}'
    return salted_hmac(SALT, state).hexdigest()


def trusted_account(request):
    try:
        token = request.get_signed_cookie(COOKIE, salt=SALT, max_age=MAX_AGE)
        account_id, fingerprint = token.split(':', 1)
        if not account_id.isdigit():
            return None
    except (KeyError, ValueError, signing.BadSignature):
        return None
    account = NZAccount.objects.filter(pk=account_id, is_active=True, user__is_active=True).select_related('user', 'user__school').first()
    return account if account and constant_time_compare(fingerprint, account_fingerprint(account)) else None


def remember_account(request, response, account):
    if request.POST.get('remember_device') == '1':
        response.set_signed_cookie(COOKIE, f'{account.pk}:{account_fingerprint(account)}',
                                   salt=SALT, max_age=MAX_AGE, httponly=True,
                                   secure=request.is_secure(), samesite='Lax')
    else:
        response.delete_cookie(COOKIE, samesite='Lax')
    return response
