from django.urls import path
from . import views

urlpatterns = [
    path('register-school/', views.register_school, name='register_school'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('nz-login/', views.nz_login_view, name='nz_login'),
    path('nz-quick-login/', views.nz_quick_login_view, name='nz_quick_login'),
]
