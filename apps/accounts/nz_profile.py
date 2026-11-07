"""Conservative name updates from the unstructured NZ display label."""


def nz_name_parts(info):
    # A two-word label can use either name order; do not guess structured fields.
    parts = info.split() if info and info != 'Авторизовано' else []
    if len(parts) == 3 and all(all(c.isalpha() or c in "'-’ʼ" for c in part) for part in parts):
        return tuple(parts)
    return None


def sync_nz_user_name(user, info):
    """Preserve local names; replace only a missing name or the legacy placeholder."""
    if user.first_name and user.first_name != 'nz.ua' and user.last_name and user.last_name != 'Вчитель':
        return
    names = nz_name_parts(info)
    if names:
        user.last_name, user.first_name, user.patronymic = names
        user.save(update_fields=['last_name', 'first_name', 'patronymic'])
    elif user.first_name == 'nz.ua':
        user.first_name = ''
        user.save(update_fields=['first_name'])
