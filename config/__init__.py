# Config package
import sys

# Патч для сумісності Python 3.14+ та Django 5.1 при копіюванні Context у тестах
try:
    import django.template.context as ctx
    def _patched_context_copy(self):
        cls = self.__class__
        duplicate = cls.__new__(cls)
        duplicate.__dict__.update(self.__dict__)
        duplicate.dicts = self.dicts[:]
        return duplicate
    ctx.BaseContext.__copy__ = _patched_context_copy
except Exception:
    pass
