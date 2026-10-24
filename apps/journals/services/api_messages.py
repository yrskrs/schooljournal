"""Persisted reasons for an authorized teacher, with credentials removed."""


def log_error_rows(log):
    if log is None:
        return []
    rows = []
    for error in log.errors if isinstance(log.errors, list) else []:
        item = error if isinstance(error, dict) else {'message': error}
        message = str(item.get('message') or 'Джерело не підтвердило результат. Повторіть перевірку підключення.')
        if log.connection.api_key:
            message = message.replace(log.connection.api_key, '••••')
        row = item.get('row')
        rows.append({'message': message[:2000], 'row': row if type(row) is int else None})
    return rows
