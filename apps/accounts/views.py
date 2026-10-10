import logging
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.debug import sensitive_post_parameters
from .forms import CustomLoginForm, SchoolRegistrationForm, InitialSuperadminForm
from django.conf import settings
from django.db import transaction, IntegrityError
from django.http import Http404
from django.views.decorators.http import require_http_methods
from .models import User, InitialSetupState
from .bootstrap import initial_setup_required, global_admin_exists, setup_recovery_user
from django.views.decorators.cache import never_cache
from apps.schools.models import School
from apps.journals.models import NZAccount
from apps.journals.services.nz_service import NZService
from .nz_trust import COOKIE, trusted_account, remember_account
from .personal import ensure_personal_account

logger = logging.getLogger(__name__)


@never_cache
@sensitive_post_parameters('setup_token', 'password1', 'password2')
@require_http_methods(['GET', 'POST'])
def initial_setup(request):
    if not initial_setup_required():
        return redirect('login')
    if len(settings.INITIAL_SETUP_TOKEN) < 32:
        return render(request, 'accounts/initial_setup.html', {'setup_unconfigured': True}, status=503)

    recovery_user = setup_recovery_user()
    form = InitialSuperadminForm(request.POST if request.method == 'POST' else None, instance=recovery_user)
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                state = InitialSetupState.objects.select_for_update().get(pk=1)
                current_recovery = setup_recovery_user()
                if state.completed_at or (not recovery_user and global_admin_exists()):
                    return redirect('login')
                if recovery_user:
                    if current_recovery is None or current_recovery.pk != recovery_user.pk:
                        return redirect('login')
                    user = User.objects.select_for_update().get(pk=current_recovery.pk)
                    for field in ('username', 'email', 'first_name', 'last_name'):
                        setattr(user, field, form.cleaned_data[field])
                    user.set_password(form.cleaned_data['password1'])
                else:
                    user = form.save(commit=False)
                    user.role = User.Role.SUPERADMIN
                    user.is_superuser = True
                    user.school = None
                user.is_staff = True
                user.is_active = True
                user.save()
                state.completed_at = timezone.now()
                state.save(update_fields=['completed_at'])
                from apps.journals.models import AuditLog
                AuditLog.objects.create(user=user, action=AuditLog.Action.UPDATE if recovery_user else AuditLog.Action.CREATE,
                    target_model='User', target_id=str(user.pk),
                    description='Доступ суперадміністратора відновлено через браузер.' if recovery_user else
                        'Суперадміністратора створено під час початкового налаштування через браузер.')
        except IntegrityError:
            form.add_error('username', 'Цей логін уже зайнято. Виберіть інший.')
        else:
            login(request, user)
            messages.success(request, 'Доступ суперадміністратора відновлено.' if recovery_user else
                'Суперадміністратора створено. Початкове налаштування завершено.')
            return redirect('portal:superadmin_dashboard')
    return render(request, 'accounts/initial_setup.html', {'form': form, 'recovering_admin': bool(recovery_user)})


@sensitive_post_parameters('password1', 'password2')
@require_http_methods(['GET', 'POST'])
def register_school(request):
    if not settings.SCHOOL_REGISTRATION_ENABLED:
        raise Http404('Реєстрацію шкіл вимкнено адміністратором сервера.')
    if request.user.is_authenticated:
        return redirect('portal:dashboard')
    form = SchoolRegistrationForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                school = School.objects.create(name=form.cleaned_data['school_name'],
                    short_name=form.cleaned_data['school_short_name'], edrpou_code=form.cleaned_data['edrpou_code'],
                    email=form.cleaned_data['email'])
                user = form.save(commit=False)
                user.role = User.Role.SCHOOL_ADMIN
                user.school = school
                user.is_staff = False
                user.is_superuser = False
                user.save()
                from apps.journals.models import AuditLog
                AuditLog.objects.create(school=school, user=user, action=AuditLog.Action.CREATE,
                    target_model='School', target_id=str(school.pk), description='Школу зареєстровано через публічну форму.')
        except IntegrityError:
            form.add_error(None, 'Логін або ЄДРПОУ вже зайнято. Перевірте дані та повторіть реєстрацію.')
        else:
            login(request, user)
            messages.success(request, 'Школу зареєстровано. Додайте навчальний рік, предмети, класи та вчителів.')
            return redirect('portal:superadmin_dashboard')
    return render(request, 'accounts/register_school.html', {'form': form})


@sensitive_post_parameters('password')
def login_view(request):
    """Головна сторінка входу (пароль або через nz.ua)."""
    if request.user.is_authenticated:
        if request.user.is_school_admin_user:
            return redirect('portal:superadmin_dashboard')
        return redirect('portal:teacher_dashboard')

    if request.method == 'POST':
        form = CustomLoginForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            messages.success(request, f"Вітаємо, {user.get_full_name_ukr()}! Ви успішно увійшли до системи.")
            next_url = request.GET.get('next')
            if next_url and url_has_allowed_host_and_scheme(next_url, {request.get_host()}, require_https=request.is_secure()):
                return redirect(next_url)
            if user.is_school_admin_user:
                return redirect('portal:superadmin_dashboard')
            return redirect('portal:teacher_dashboard')
        else:
            messages.error(request, "Невірний логін або пароль. Перевірте правильність введених даних.")
    else:
        form = CustomLoginForm()

    # Знаходимо збережені активні сесії nz.ua
    saved_account = trusted_account(request)
    saved_nz_accounts = [saved_account] if saved_account else []

    context = {
        'form': form,
        'saved_nz_accounts': saved_nz_accounts,
    }
    return render(request, 'accounts/login.html', context)


def logout_view(request):
    """Вихід із системи."""
    logout(request)
    messages.info(request, "Ви успішно вийшли із системи.")
    response = redirect('login')
    response.delete_cookie(COOKIE, samesite='Lax')
    return response


@require_POST
def nz_quick_login_view(request):
    """Швидкий вхід в один клік через раніше збережену сесію nz.ua."""
    account_id = request.POST.get('account_id')
    account = trusted_account(request)
    if not account or str(account.pk) != account_id:
        messages.error(request, 'Для входу підтвердьте свій акаунт nz.ua або введіть локальний пароль.')
        return redirect('login')

    login(request, account.user)

    messages.success(request, f"Вітаємо, {account.user.get_full_name_ukr()}! Успішний вхід через акаунт nz.ua.")
    return redirect('portal:teacher_dashboard')


@require_POST
@sensitive_post_parameters('password', 'phpsessid')
def nz_login_view(request):
    """
    Вхід через nz.ua (за паролем або через сесійну куку PHPSESSID).
    Якщо користувач ще не існує локально, він автоматично створюється.
    """
    auth_method = request.POST.get('auth_method', 'cookie')
    phpsessid = request.POST.get('phpsessid', '').strip()
    login_str = request.POST.get('login', '').strip()
    password_str = request.POST.get('password', '')

    if auth_method == 'cookie':
        if not phpsessid:
            messages.error(request, "Вкажіть значення PHPSESSID для входу.")
            return redirect('login')

        service = NZService(phpsessid=phpsessid)
        is_valid, info = service.verify_session()
        if not is_valid:
            messages.error(request, f"Помилка входу через nz.ua: {info}")
            return redirect('login')

    elif auth_method == 'credentials':
        if not login_str or not password_str:
            messages.error(request, "Вкажіть логін та пароль nz.ua.")
            return redirect('login')

        service = NZService()
        success, msg = service.login(login_str, password_str)
        if not success:
            messages.error(request, f"Помилка авторизації nz.ua: {msg}")
            return redirect('login')

        is_valid, info = service.verify_session()
        if not is_valid:
            messages.error(request, 'Не вдалося підтвердити сесію nz.ua після входу.')
            return redirect('login')
    else:
        messages.error(request, "Невідомий метод автентифікації.")
        return redirect('login')

    # Знаходимо або створюємо користувача
    account = None
    if auth_method == 'credentials' and login_str:
        account = NZAccount.objects.filter(login=login_str).first()
    if not account and phpsessid:
        account = NZAccount.objects.filter(phpsessid=phpsessid).first()

    user = None
    if account:
        user = account.user
        if not user.is_active:
            messages.error(request, 'Локальний обліковий запис вимкнено.')
            return redirect('login')
        account = ensure_personal_account(account)
        user = account.user
    else:
        if not user:
            # A matching name/username never proves local identity. New NZ users
            # receive a separate space, not access to an existing school's data.
            school = School.objects.create(name='Особистий простір журналів nz.ua')
            parts = info.split() if info and info != "Авторизовано" else []
            last_name = parts[0] if len(parts) > 0 else "Вчитель"
            first_name = parts[1] if len(parts) > 1 else "nz.ua"
            patronymic = parts[2] if len(parts) > 2 else ""

            base_username = (login_str.split('@')[0] if login_str else f"nz_{last_name.lower()}").replace(' ', '_')
            username = base_username
            counter = 1
            while User.objects.filter(username=username).exists():
                username = f"{base_username}_{counter}"
                counter += 1

            user = User.objects.create_user(
                username=username,
                role=User.Role.TEACHER,
                school=school,
                last_name=last_name,
                first_name=first_name,
                patronymic=patronymic,
                is_nz_personal=True
            )

        account, _ = NZAccount.objects.get_or_create(user=user)

    account.login = login_str or account.login
    account.phpsessid = service.session.cookies.get('PHPSESSID', '') or phpsessid
    account.cookies = service.get_cookies_dict()
    account.is_active = True
    account.last_synced_at = timezone.now()
    account.save()

    login(request, user)
    messages.success(request, f"Вітаємо, {user.get_full_name_ukr()}! Успішний вхід через портал nz.ua.")
    return remember_account(request, redirect('portal:nz_journals_list'), account)
