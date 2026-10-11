import datetime
import logging
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.core import signing
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.views.decorators.debug import sensitive_post_parameters

from apps.portal.permissions import has_journal_access
from apps.curriculum.models import AcademicYear, StudentClass, Subject, TeacherAssignment
from apps.journals.models import Journal, NZAccount, NZSyncLog
from apps.journals.services.nz_service import NZService, NZServiceError, NZAuthError
from apps.accounts.personal import journal_nz_account

logger = logging.getLogger(__name__)


@login_required
def nz_account_status_view(request):
    """Повертає поточний статус підключення облікового запису nz.ua."""
    account = getattr(request.user, 'nz_account', None)
    if not account or not account.is_active:
        return JsonResponse({
            'connected': False,
            'message': 'Акаунт nz.ua не підключено'
        })

    return JsonResponse({
        'connected': True,
        'login': account.login,
        'last_synced_at': account.last_synced_at.strftime('%d.%m.%Y %H:%M') if account.last_synced_at else None,
        'has_session_cookie': bool(account.phpsessid),
    })


@login_required
@require_POST
@sensitive_post_parameters('password', 'phpsessid')
def nz_account_connect_view(request):
    """
    Підключення або оновлення сесії nz.ua:
    Підтримує 2 режими:
    1. Через сесійну куку PHPSESSID (найбезпечніше, коли користувач вже увійшов у браузері)
    2. Через логін та пароль на nz.ua
    """
    auth_method = request.POST.get('auth_method', 'cookie')
    next_url = request.POST.get('next', 'portal:teacher_dashboard')

    account, _ = NZAccount.objects.get_or_create(user=request.user)

    if auth_method == 'cookie':
        phpsessid = request.POST.get('phpsessid', '').strip()
        if not phpsessid:
            messages.error(request, "Будь ласка, вкажіть значення PHPSESSID.")
            return redirect(next_url)

        service = NZService(phpsessid=phpsessid)
        is_valid, info = service.verify_session()

        if not is_valid:
            messages.error(request, f"Помилка перевірки сесії nz.ua: {info}")
            return redirect(next_url)

        account.phpsessid = phpsessid
        account.cookies = service.get_cookies_dict()
        account.is_active = True
        account.last_synced_at = timezone.now()
        account.save()

        messages.success(request, f"Успішно підключено до nz.ua! Користувач: {info}")
        return redirect(next_url)

    elif auth_method == 'credentials':
        login_str = request.POST.get('login', '').strip()
        password_str = request.POST.get('password', '')

        if not login_str or not password_str:
            messages.error(request, "Логін та пароль є обов'язковими.")
            return redirect(next_url)

        service = NZService()
        success, msg = service.login(login_str, password_str)

        if not success:
            messages.error(request, f"Помилка авторизації на nz.ua: {msg}")
            return redirect(next_url)

        valid, _ = service.verify_session()
        if not valid:
            messages.error(request, 'Не вдалося підтвердити сесію nz.ua після входу.')
            return redirect(next_url)

        cookies = service.get_cookies_dict()
        account.login = login_str
        account.phpsessid = cookies.get('PHPSESSID', '')
        account.cookies = cookies
        account.is_active = True
        account.last_synced_at = timezone.now()
        account.save()

        messages.success(request, "Успішно авторизовано на порталі nz.ua!")
        return redirect(next_url)

    messages.error(request, "Невідомий метод автентифікації.")
    return redirect(next_url)


@login_required
@require_POST
def nz_account_disconnect_view(request):
    """Відключення облікового запису nz.ua."""
    account = getattr(request.user, 'nz_account', None)
    if account:
        account.is_active = False
        account.phpsessid = ""
        account.cookies = {}
        account.save()
        messages.info(request, "Підключення до nz.ua деактивовано.")
    return redirect(request.POST.get('next', 'portal:teacher_dashboard'))


@login_required
def nz_journals_list_view(request):
    """
    Відображає сторінку/список усіх журналів з порталу nz.ua для поточного вчителя.
    Дозволяє в 1 клік створити локальний журнал та завантажити дані.
    """
    account = getattr(request.user, 'nz_account', None)
    if not account or not account.is_active:
        messages.warning(request, "Спочатку підключіть ваш акаунт nz.ua.")
        return redirect('portal:teacher_dashboard')

    service = NZService.from_account(account)
    try:
        remote_journals = service.fetch_journal_list()
    except NZAuthError:
        account.is_active = False
        account.save(update_fields=['is_active'])
        messages.error(request, "Сесія nz.ua закінчилась. Будь ласка, увійдіть повторно.")
        return redirect('portal:teacher_dashboard')
    except Exception as e:
        messages.error(request, f"Не вдалося отримати список журналів з nz.ua: {str(e)}")
        remote_journals = []

    # Локальні журнали для зіставлення
    local_journals = Journal.objects.filter(Q(nz_owner=account) | Q(teacher=request.user)).select_related('student_class', 'subject')

    local_by_nz_id = {j.nz_journal_id: j for j in local_journals if j.nz_journal_id}

    for rj in remote_journals:
        rj['local_journal'] = local_by_nz_id.get(rj['nz_journal_id'])

    context = {
        'account': account,
        'remote_journals': remote_journals,
        'local_journals': local_journals,
    }
    return render(request, 'portal/nz/journals_list.html', context)


@login_required
@require_POST
@transaction.atomic
def nz_journal_import_view(request, journal_id=None):
    """
    Імпорт або зв'язування журналу з nz.ua:
    Якщо journal_id передано в URL — імпортує в цей локальний журнал.
    Якщо ні — журнал створюється АВТОМАТИЧНО для вчителя (разом з класом та предметом)!
    """
    target_journal_id = journal_id or request.POST.get('local_journal_id')
    nz_journal_id = request.POST.get('nz_journal_id', '').strip()
    nz_subgroup_id = request.POST.get('nz_subgroup_id', '').strip()
    if not nz_journal_id and not target_journal_id:
        return HttpResponse('Вкажіть ID журналу nz.ua.', status=400)

    account = getattr(request.user, 'nz_account', None)
    if not target_journal_id and (not account or not account.is_active):
        messages.error(request, "Акаунт nz.ua не підключено або сесія недійсна.")
        return redirect('portal:nz_journals_list')

    if not target_journal_id:
        existing = Journal.objects.filter(nz_owner=account, nz_journal_id=nz_journal_id,
            nz_subgroup_id=nz_subgroup_id).first()
        if existing:
            target_journal_id = existing.pk

    if target_journal_id:
        local_journal = get_object_or_404(Journal, id=target_journal_id)
        if not has_journal_access(request.user, local_journal, write=True):
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.POST.get('format') == 'json':
                return JsonResponse({'success': False, 'error': 'У вас немає прав для зміни цього журналу.'}, status=403)
            messages.error(request, "У вас немає прав для зміни цього журналу.")
            return redirect('portal:teacher_dashboard')

        account = journal_nz_account(local_journal, request.user)
        if not account or not account.is_active:
            return HttpResponse('Особистий акаунт nz.ua потребує повторного входу.', status=400)

        if not nz_journal_id and local_journal.nz_journal_id:
            nz_journal_id = local_journal.nz_journal_id
        if not nz_subgroup_id and local_journal.nz_subgroup_id:
            nz_subgroup_id = local_journal.nz_subgroup_id
    else:
        # Автоматичне створення класу, предмета та журналу без вибору зі списку
        school = request.user.school
        if not school:
            messages.error(request, "У вашого користувача не вказана школа. Зверніться до адміністратора.")
            return redirect('portal:nz_journals_list')

        class_name = request.POST.get('class_name', '').strip() or "Клас"
        subject_name = request.POST.get('subject_name', '').strip() or "Предмет"

        academic_year = AcademicYear.objects.filter(school=school, is_current=True).first()
        if not academic_year:
            academic_year = AcademicYear.objects.filter(school=school).order_by('-start_date').first()
        if not academic_year:
            today = timezone.localdate()
            now_y = today.year if today.month >= 9 else today.year - 1
            academic_year = AcademicYear.objects.create(
                school=school,
                name=f"{now_y}/{now_y+1}",
                start_date=datetime.date(now_y, 9, 1),
                end_date=datetime.date(now_y+1, 6, 30),
                is_current=True
            )

        student_class, _ = StudentClass.objects.get_or_create(
            school=school,
            academic_year=academic_year,
            name=class_name
        )

        subject, _ = Subject.objects.get_or_create(
            school=school,
            name=subject_name
        )

        TeacherAssignment.objects.get_or_create(
            school=school,
            teacher=request.user,
            subject=subject,
            student_class=student_class,
            academic_year=academic_year,
            defaults={'is_active': True}
        )

        local_journal, created = Journal.objects.get_or_create(
            academic_year=academic_year,
            student_class=student_class,
            subject=subject,
            defaults={
                'school': school,
                'teacher': request.user,
                'nz_journal_id': nz_journal_id,
                'nz_subgroup_id': nz_subgroup_id,
                'nz_owner': account,
            }
        )
        if not created:
            if local_journal.teacher_id != request.user.pk:
                return HttpResponse('Цей журнал належить іншому вчителю. Оберіть власний журнал для прив’язки.', status=403)
            if local_journal.nz_journal_id and (local_journal.nz_journal_id, local_journal.nz_subgroup_id) != (nz_journal_id, nz_subgroup_id):
                return HttpResponse('Для цього класу й предмета вже є інша онлайн-прив’язка. Наявний журнал не змінено.', status=409)
            local_journal.nz_journal_id = nz_journal_id
            local_journal.nz_subgroup_id = nz_subgroup_id
            local_journal.teacher = request.user
            local_journal.save(update_fields=['nz_journal_id', 'nz_subgroup_id', 'teacher'])

    if not local_journal.nz_owner_id:
        local_journal.nz_owner = account
        local_journal.save(update_fields=['nz_owner'])

    is_ajax = (request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.POST.get('format') == 'json')

    if not nz_journal_id:
        err_msg = "Не вказано ID журналу nz.ua для імпорту."
        if is_ajax:
            return JsonResponse({'success': False, 'error': err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect('portal:journal_detail', journal_id=local_journal.id)

    html_file = request.FILES.get('html_file')
    html_content = None
    if html_file:
        try:
            html_content = html_file.read().decode('utf-8', errors='replace')
        except Exception:
            pass

    service = NZService.from_account(account) if account else NZService()
    try:
        stats = service.import_journal_to_local(
            nz_journal_id=nz_journal_id,
            local_journal=local_journal,
            nz_subgroup_id=nz_subgroup_id,
            user=request.user,
            html_content=html_content
        )
        msg = (
            f"Журнал «{local_journal.student_class.name} — {local_journal.subject.name}» успішно завантажено з nz.ua! "
            f"Учнів: {stats['students_matched'] + stats['students_created']}, "
            f"уроків: {stats['lessons_synced']}, "
            f"оцінок: {stats['grades_synced']}, "
            f"відвідувань: {stats['attendances_synced']}."
        )
        if stats.get('lesson_times_synced'):
            msg += f" Розклад дзвінків: {stats['lesson_times_synced']}."
        if stats.get('local_changes_preserved'):
            msg += f" Локальні зміни збережено без перезапису: {stats['local_changes_preserved']}."
        if is_ajax:
            return JsonResponse({
                'success': True,
                'message': msg,
                'stats': stats,
            })
        messages.success(request, msg)
    except NZAuthError:
        err_msg = "Сесія nz.ua закінчилась. Оновіть кукі або увійдіть знову."
        if is_ajax:
            return JsonResponse({'success': False, 'error': err_msg}, status=401)
        messages.error(request, err_msg)
    except Exception as e:
        logger.exception("NZ import error")
        err_msg = f"Помилка імпорту з nz.ua: {str(e)}"
        if is_ajax:
            return JsonResponse({'success': False, 'error': err_msg}, status=500)
        messages.error(request, err_msg)

    return redirect('portal:journal_detail', journal_id=local_journal.id)


@login_required
def nz_sync_preview_view(request, journal_id):
    """
    Попередній перегляд (Preview) несинхронізованих змін для конкретного журналу.
    Повертає JSON або модальний фрагмент.
    """
    journal = get_object_or_404(Journal, id=journal_id)
    if not has_journal_access(request.user, journal, write=False):
        return JsonResponse({'error': 'Доступ заборонено'}, status=403)

    account = journal_nz_account(journal, request.user)
    service = NZService()
    preview = service.get_unsynced_changes(journal)
    preview['preview_token'] = signing.dumps({
        'journal': journal.pk, 'user': request.user.pk,
        'fingerprint': service.pending_changes_fingerprint(journal),
    }, salt='nz-sync-preview')

    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
        return JsonResponse(preview)

    context = {
        'journal': journal,
        'account': account,
        'preview': preview,
    }
    return render(request, 'portal/nz/partials/sync_modal.html', context)


@login_required
@require_POST
def nz_sync_execute_view(request, journal_id):
    """
    Вивантаження змінених записів (дельта) журналу в nz.ua:
    - Виконується ТІЛЬКИ коли вчитель натискає відповідну кнопку.
    - Вивантажуються ТІЛЬКИ ті записи, які вчитель змінив локально (is_synced_to_nz=False).
    - Підтримує dry_run (тестовий прогін).
    - Повертає детальний звіт про виконання.
    """
    journal = get_object_or_404(Journal, id=journal_id)
    if not has_journal_access(request.user, journal, write=True):
        return JsonResponse({'status': 'error', 'message': 'У вас немає прав редагування цього журналу.'}, status=403)

    def failed(message, status=400):
        log = NZSyncLog.objects.create(journal=journal, user=request.user, status=NZSyncLog.Status.FAILED,
            error_count=1, error_message=message, details={'items': [{'status': 'error', 'item': 'Вивантаження журналу', 'error': message}]})
        return JsonResponse({'status': 'error', 'message': message, 'log_id': log.pk,
            'history_url': reverse('portal:nz_sync_history', kwargs={'journal_id': journal.pk}) + '#log-' + str(log.pk)}, status=status)

    account = journal_nz_account(journal, request.user)
    if not account or not account.is_active:
        return failed('Акаунт nz.ua не підключено або термін дії сесії вичерпано. Перейдіть до підключення nz.ua.')

    if not journal.nz_journal_id:
        return failed("Цей журнал ще не прив'язано до журналу на nz.ua. Спочатку виконайте прив'язку.")

    dry_run = request.POST.get('dry_run') in ['1', 'true', 'True', True]

    service = NZService.from_account(account)

    try:
        approved = signing.loads(request.POST.get('preview_token', ''), salt='nz-sync-preview', max_age=1800)
    except signing.BadSignature:
        return failed('Спочатку відкрийте перегляд змін для вивантаження.', 409)
    if (approved.get('journal') != journal.pk or approved.get('user') != request.user.pk or
            approved.get('fingerprint') != service.pending_changes_fingerprint(journal)):
        return failed('Зміни оновлено після перегляду. Закрийте вікно й перегляньте їх ще раз.', 409)

    try:
        sync_log = service.push_journal_changes(
            journal=journal,
            user=request.user,
            dry_run=dry_run
        )

        return JsonResponse({
            'status': 'success',
            'log_id': sync_log.id,
            'history_url': reverse('portal:nz_sync_history', kwargs={'journal_id': journal.pk}) + '#log-' + str(sync_log.pk),
            'sync_status': sync_log.status,
            'dry_run': dry_run,
            'items_count': sync_log.items_count,
            'success_count': sync_log.success_count,
            'error_count': sync_log.error_count,
            'message': (
                f"Синхронізацію успішно завершено! "
                f"Оновлено записів: {sync_log.success_count} з {sync_log.items_count}."
                if sync_log.error_count == 0 else
                f"Синхронізація завершена з зауваженнями: успішно {sync_log.success_count}, помилок {sync_log.error_count}."
            ),
            'details': sync_log.details.get('items', []),
        })

    except NZAuthError as e:
        account.is_active = False
        account.save(update_fields=['is_active'])
        return failed(f"Помилка авторизації nz.ua: {str(e)}. Будь ласка, оновіть сесію.", 401)
    except NZServiceError as e:
        return failed(f"Помилка nz.ua: {str(e)}")
    except Exception as e:
        logger.exception("NZ sync execute error")
        return failed('Не вдалося завершити вивантаження. Місцеві дані збережено; деталі для адміністратора є в журналі сервера.', 500)


@login_required
def nz_sync_history_view(request, journal_id):
    """Історія синхронізацій з nz.ua для конкретного журналу."""
    journal = get_object_or_404(Journal, id=journal_id)
    if not has_journal_access(request.user, journal, write=False):
        return HttpResponse("Доступ заборонено", status=403)

    logs = NZSyncLog.objects.filter(journal=journal).order_by('-created_at')[:20]

    return render(request, 'portal/nz/sync_history.html', {
        'journal': journal,
        'logs': logs,
    })
