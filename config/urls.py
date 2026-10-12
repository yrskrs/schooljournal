from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

from django.views.generic.base import RedirectView

# Налаштування заголовків стандартної адмінки Django
admin.site.site_header = "Електронний шкільний журнал — Технічне адміністрування"
admin.site.site_title = "Шкільний журнал"
admin.site.index_title = "Панель технічного керування"

urlpatterns = [
    path('favicon.ico', RedirectView.as_view(url='/static/favicon.ico', permanent=True)),
    path('admin/', admin.site.urls),
    path('', include('apps.portal.urls')),
    path('accounts/', include('apps.accounts.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
