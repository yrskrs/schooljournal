from django.db import models

class School(models.Model):
    name = models.CharField(max_length=255, verbose_name="Повна назва закладу")
    short_name = models.CharField(max_length=100, blank=True, verbose_name="Коротка назва")
    edrpou_code = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        unique=True,
        verbose_name="Код ЄДРПОУ"
    )
    address = models.CharField(max_length=255, blank=True, verbose_name="Адреса")
    phone = models.CharField(max_length=50, blank=True, verbose_name="Телефон")
    email = models.EmailField(blank=True, verbose_name="Електронна пошта")
    is_active = models.BooleanField(default=True, verbose_name="Активна")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Оновлено")

    class Meta:
        verbose_name = "Школа"
        verbose_name_plural = "Школи"
        ordering = ['name']

    def __str__(self):
        return self.short_name or self.name
