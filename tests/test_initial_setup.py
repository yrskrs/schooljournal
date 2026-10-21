from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command, CommandError
from django.db import DatabaseError, IntegrityError
from django.db.migrations.executor import MigrationExecutor
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.bootstrap import initial_setup_required
from apps.accounts.models import InitialSetupState, User
from apps.journals.models import AuditLog


SETUP_TOKEN = 'synthetic-initial-setup-code-8327-abcdef'


@override_settings(INITIAL_SETUP_ENABLED=True, INITIAL_SETUP_TOKEN=SETUP_TOKEN)
class InitialSetupTests(TestCase):
    def setUp(self):
        InitialSetupState.objects.update_or_create(pk=1, defaults={'completed_at': None})

    def data(self, **overrides):
        data = {
            'setup_token': SETUP_TOKEN,
            'username': 'synthetic-first-admin',
            'email': 'admin@example.invalid',
            'first_name': 'Тестовий',
            'last_name': 'Адміністратор',
            'password1': 'Synthetic-Bootstrap-924!',
            'password2': 'Synthetic-Bootstrap-924!',
        }
        data.update(overrides)
        return data

    def test_first_login_and_registration_lead_to_browser_setup(self):
        for name in ('login', 'register_school', 'nz_login', 'nz_quick_login'):
            response = self.client.get(reverse(name))
            self.assertRedirects(response, reverse('initial_setup'))
        setup = self.client.get(reverse('initial_setup'))
        self.assertContains(setup, 'Створити суперадміністратора')
        self.assertContains(setup, 'INITIAL_SETUP_TOKEN')
        self.assertNotContains(setup, SETUP_TOKEN)
        self.assertIn('no-store', setup['Cache-Control'])
        self.assertEqual(User.objects.count(), 0)

    def test_setup_creates_admin_logs_in_and_seals_state(self):
        response = self.client.post(reverse('initial_setup'), self.data())
        self.assertRedirects(response, reverse('portal:superadmin_dashboard'))
        user = User.objects.get(username='synthetic-first-admin')
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_active)
        self.assertEqual(user.role, User.Role.SUPERADMIN)
        self.assertIsNone(user.school)
        self.assertTrue(user.check_password('Synthetic-Bootstrap-924!'))
        self.assertEqual(int(self.client.session['_auth_user_id']), user.pk)
        self.assertIsNotNone(InitialSetupState.objects.get(pk=1).completed_at)
        self.assertEqual(AuditLog.objects.get(target_model='User').target_id, str(user.pk))
        self.assertEqual(self.client.get('/admin/').status_code, 200)
        self.client.logout()
        self.assertEqual(self.client.get(reverse('login')).status_code, 200)

    def test_wrong_and_blank_codes_create_no_user_and_are_not_redisplayed(self):
        for code in ('', 'incorrect-secret-code-value-abcdefghijklmnopqrstuvwxyz'):
            response = self.client.post(reverse('initial_setup'), self.data(setup_token=code))
            self.assertEqual(response.status_code, 200)
            if code:
                self.assertNotContains(response, code)
            self.assertFalse(User.objects.exists())
            self.assertIsNone(InitialSetupState.objects.get(pk=1).completed_at)

    def test_weak_mismatched_password_and_invalid_email_do_not_seal_setup(self):
        for overrides in (
            {'password1': '12345678', 'password2': '12345678'},
            {'password2': 'different-password-913!'},
            {'email': 'invalid-email'},
            {'username': ''},
        ):
            response = self.client.post(reverse('initial_setup'), self.data(**overrides))
            self.assertEqual(response.status_code, 200)
            self.assertFalse(User.objects.exists())
            self.assertIsNone(InitialSetupState.objects.get(pk=1).completed_at)

    def test_setup_requires_csrf(self):
        response = Client(enforce_csrf_checks=True).post(reverse('initial_setup'), self.data())
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.exists())

    def test_repeated_submission_cannot_create_second_admin(self):
        self.client.post(reverse('initial_setup'), self.data())
        response = Client().post(reverse('initial_setup'), self.data(username='second-admin'))
        self.assertRedirects(response, reverse('login'))
        self.assertEqual(User.objects.filter(is_superuser=True).count(), 1)

    def test_deleted_first_admin_does_not_reopen_setup(self):
        self.client.post(reverse('initial_setup'), self.data())
        User.objects.all().delete()
        self.client.logout()
        self.assertFalse(initial_setup_required())
        self.assertRedirects(self.client.get(reverse('initial_setup')), reverse('login'))
        self.assertEqual(self.client.get(reverse('login')).status_code, 200)

    def test_existing_disabled_admin_closes_setup_without_changing_password(self):
        admin = User.objects.create_superuser(username='existing-disabled-admin', password='Original-Password-941!')
        admin.is_active = False
        admin.save()
        self.assertFalse(initial_setup_required())
        self.assertRedirects(self.client.post(reverse('initial_setup'), self.data()), reverse('login'))
        admin.refresh_from_db()
        self.assertFalse(admin.is_active)
        self.assertTrue(admin.check_password('Original-Password-941!'))
        self.assertEqual(User.objects.count(), 1)

    def test_existing_global_role_also_seals_setup(self):
        User.objects.create_user(username='existing-global-admin', role=User.Role.SUPERADMIN)
        self.assertIsNotNone(InitialSetupState.objects.get(pk=1).completed_at)
        self.assertFalse(initial_setup_required())

    def test_school_admin_does_not_close_global_setup(self):
        User.objects.create_user(username='school-only-admin', role=User.Role.SCHOOL_ADMIN)
        self.assertTrue(initial_setup_required())

    def test_audit_failure_rolls_back_admin_and_completion(self):
        with patch.object(AuditLog.objects, 'create', side_effect=DatabaseError('synthetic')):
            with self.assertRaises(DatabaseError):
                self.client.post(reverse('initial_setup'), self.data())
        self.assertFalse(User.objects.exists())
        self.assertIsNone(InitialSetupState.objects.get(pk=1).completed_at)

    def test_concurrent_completion_rechecked_inside_transaction(self):
        # Simulate another request completing between validation and the row lock.
        original = InitialSetupState.objects.select_for_update

        def completed_elsewhere(*args, **kwargs):
            InitialSetupState.objects.filter(pk=1).update(completed_at=timezone.now())
            return original(*args, **kwargs)

        with patch.object(InitialSetupState.objects, 'select_for_update', side_effect=completed_elsewhere):
            response = self.client.post(reverse('initial_setup'), self.data())
        self.assertRedirects(response, reverse('login'))
        self.assertFalse(User.objects.exists())

    def test_duplicate_username_and_save_race_are_reported(self):
        User.objects.create_user(username='already-used')
        response = self.client.post(reverse('initial_setup'), self.data(username='already-used'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.count(), 1)
        with patch.object(User, 'save', side_effect=IntegrityError('synthetic duplicate')):
            response = self.client.post(reverse('initial_setup'), self.data())
        self.assertContains(response, 'Цей логін уже зайнято')
        self.assertIsNone(InitialSetupState.objects.get(pk=1).completed_at)

    @override_settings(INITIAL_SETUP_TOKEN='')
    def test_missing_code_fails_closed_with_server_instructions(self):
        response = self.client.post(reverse('initial_setup'), self.data(setup_token=''))
        self.assertContains(response, 'Код початкового налаштування ще не задано', status_code=503)
        self.assertNotContains(response, '<form', status_code=503)
        self.assertFalse(User.objects.exists())

    @override_settings(INITIAL_SETUP_ENABLED=False)
    def test_explicitly_disabled_setup_keeps_regular_login(self):
        self.assertRedirects(self.client.get(reverse('initial_setup')), reverse('login'))
        self.assertEqual(self.client.get(reverse('login')).status_code, 200)

    def test_public_help_versions_and_health_available_during_setup(self):
        for name in ('portal:guide', 'portal:versions', 'portal:health'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)


class InitialSetupMigrationTests(TransactionTestCase):
    def test_migration_without_admin_initializes_pending_setup(self):
        before = ('accounts', '0003_alter_user_role')
        after = ('accounts', '0004_initialsetupstate')
        MigrationExecutor(connection).migrate([before])
        try:
            MigrationExecutor(connection).migrate([after])
            self.assertIsNone(InitialSetupState.objects.get(pk=1).completed_at)
        finally:
            MigrationExecutor(connection).migrate([after])

    def test_migration_seals_existing_global_admin(self):
        executor = MigrationExecutor(connection)
        before = ('accounts', '0003_alter_user_role')
        after = ('accounts', '0004_initialsetupstate')
        executor.migrate([before])
        try:
            old_user = executor.loader.project_state([before]).apps.get_model('accounts', 'User')
            old_user.objects.create(username='existing-at-upgrade', role='SUPERADMIN', is_active=False)
            executor = MigrationExecutor(connection)
            executor.migrate([after])
            self.assertIsNotNone(InitialSetupState.objects.get(pk=1).completed_at)
            self.assertTrue(User.objects.filter(username='existing-at-upgrade').exists())
        finally:
            MigrationExecutor(connection).migrate([after])


@unittest.skipUnless(shutil.which('bash'), 'bash is not available')
class SetupScriptTests(TestCase):
    def test_public_url_allows_lan_host_and_origin_without_rotating_existing_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            for name in ('setup.sh', 'scripts/setup.sh', '.env.example'):
                shutil.copy2(settings.BASE_DIR / name, root / name)
            env = root / '.env'
            env.write_text('SECRET_KEY=preserved\nINITIAL_SETUP_TOKEN=' + SETUP_TOKEN + '\nALLOWED_HOSTS=localhost,127.0.0.1\nCSRF_TRUSTED_ORIGINS=http://localhost:8085\nWEB_PORT=8085\n')
            args = ['bash', str(root / 'setup.sh'), '--public-url', 'http://192.168.1.13:8085/']
            subprocess.run(args, check=True, capture_output=True)
            text = env.read_text()
            self.assertIn('ALLOWED_HOSTS=localhost,127.0.0.1,192.168.1.13\n', text)
            self.assertIn('CSRF_TRUSTED_ORIGINS=http://localhost:8085,http://192.168.1.13:8085\n', text)
            self.assertIn('INITIAL_SETUP_TOKEN=' + SETUP_TOKEN + '\n', text)
            self.assertIn('SECRET_KEY=preserved\n', text)
            subprocess.run(args, check=True, capture_output=True)
            self.assertEqual(env.read_text(), text)
            for invalid in ('ftp://example.invalid', 'http://user:password@example.invalid', 'http://example.invalid/path', 'http://example.invalid:bad'):
                result = subprocess.run(args[:-1] + [invalid], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(env.read_text(), text)

    def test_setup_fills_blank_quoted_code_in_existing_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            for name in ('setup.sh', 'scripts/setup.sh', '.env.example'):
                shutil.copy2(settings.BASE_DIR / name, root / name)
            env = root / '.env'
            env.write_text('SECRET_KEY=preserved-test-key\nINITIAL_SETUP_TOKEN="" # not set yet\nWEB_PORT=8089\n')
            subprocess.run(['bash', str(root / 'setup.sh')], check=True, capture_output=True)
            text = env.read_text()
            self.assertIn('SECRET_KEY=preserved-test-key\n', text)
            self.assertIn('WEB_PORT=8089\n', text)
            self.assertRegex(text, r'INITIAL_SETUP_TOKEN=[0-9a-f]{64}\n')

    def test_setup_preserves_existing_database_secrets_and_generates_only_missing_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            for name in ('setup.sh', 'scripts/setup.sh', '.env.example'):
                shutil.copy2(settings.BASE_DIR / name, root / name)
            env = root / '.env'
            original = 'SECRET_KEY=existing-synthetic-key\nPOSTGRES_PASSWORD=existing-synthetic-password\nWEB_PORT=8089\n'
            env.write_text(original)
            subprocess.run(['bash', str(root / 'setup.sh')], check=True, capture_output=True)
            text = env.read_text()
            self.assertTrue(text.startswith(original))
            self.assertRegex(text, r'INITIAL_SETUP_TOKEN=[0-9a-f]{64}\n')
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)
            subprocess.run(['bash', str(root / 'setup.sh')], check=True, capture_output=True)
            self.assertEqual(env.read_text(), text)

    def test_setup_generates_unique_code_for_new_installations(self):
        values = []
        with tempfile.TemporaryDirectory() as directory:
            for i in range(2):
                root = Path(directory) / str(i)
                (root / 'scripts').mkdir(parents=True)
                for name in ('setup.sh', 'scripts/setup.sh', '.env.example'):
                    shutil.copy2(settings.BASE_DIR / name, root / name)
                subprocess.run(['bash', str(root / 'setup.sh')], check=True, capture_output=True)
                text = (root / '.env').read_text()
                values.append(next(line for line in text.splitlines() if line.startswith('INITIAL_SETUP_TOKEN=')))
                self.assertNotIn('SECRET_KEY=\n', text)
                self.assertNotIn('POSTGRES_PASSWORD=\n', text)
        self.assertNotEqual(*values)


@override_settings(INITIAL_SETUP_ENABLED=True, INITIAL_SETUP_TOKEN=SETUP_TOKEN)
class AdminRecoveryTests(TestCase):
    setUp = InitialSetupTests.setUp
    data = InitialSetupTests.data

    def prepare(self, admin):
        override = override_settings(INITIAL_SETUP_RECOVERY_USER_ID=admin.pk)
        override.enable()
        self.addCleanup(override.disable)
        call_command('prepare_admin_recovery', username=admin.username, stdout=StringIO())

    def test_recovery_is_not_opened_by_setting_alone_or_by_anonymous_request(self):
        admin = User.objects.create_superuser(username='old-admin', password='Original-Password-941!')
        with override_settings(INITIAL_SETUP_RECOVERY_USER_ID=admin.pk):
            self.assertFalse(initial_setup_required())
            self.assertRedirects(self.client.post(reverse('initial_setup'), self.data()), reverse('login'))
        with self.assertRaises(CommandError):
            call_command('prepare_admin_recovery', username=admin.username, stdout=StringIO())
        admin.refresh_from_db()
        self.assertTrue(admin.check_password('Original-Password-941!'))

    def test_recovery_preserves_admin_id_school_other_users_and_journal_history(self):
        from apps.schools.models import School
        school = School.objects.create(name='Synthetic recovery school')
        admin = User.objects.create_superuser(username='old-admin', password='Original-Password-941!', school=school)
        other = User.objects.create_user(username='other-teacher', school=school)
        history = AuditLog.objects.create(user=admin, action=AuditLog.Action.CREATE, target_model='Synthetic', target_id='1', description='Preserve this reference')
        self.prepare(admin)
        self.assertFalse(Client().login(username='old-admin', password='Original-Password-941!'))
        self.assertRedirects(self.client.get(reverse('login')), reverse('initial_setup'))
        self.assertContains(self.client.get(reverse('initial_setup')), 'Налаштувати доступ адміністратора')
        response = self.client.post(reverse('initial_setup'), self.data(username='my-admin'))
        self.assertRedirects(response, reverse('portal:superadmin_dashboard'))
        admin.refresh_from_db()
        self.assertEqual(admin.username, 'my-admin')
        self.assertEqual(admin.school_id, school.pk)
        self.assertTrue(admin.check_password('Synthetic-Bootstrap-924!'))
        self.assertFalse(admin.check_password('Original-Password-941!'))
        history.refresh_from_db()
        self.assertEqual(history.user_id, admin.pk)
        self.assertTrue(User.objects.filter(pk=other.pk).exists())
        self.assertEqual(User.objects.count(), 2)
        self.assertIsNotNone(InitialSetupState.objects.get(pk=1).completed_at)
        self.assertRedirects(Client().post(reverse('initial_setup'), self.data()), reverse('login'))

    def test_recovery_accepts_original_username_but_rejects_wrong_token(self):
        admin = User.objects.create_superuser(username='old-admin', password='Original-Password-941!')
        self.prepare(admin)
        response = self.client.post(reverse('initial_setup'), self.data(username=admin.username, setup_token='wrong'))
        self.assertContains(response, 'Невірний код')
        admin.refresh_from_db()
        self.assertTrue(admin.check_password('Original-Password-941!'))
        self.assertIsNone(InitialSetupState.objects.get(pk=1).completed_at)
        self.assertRedirects(self.client.post(reverse('initial_setup'), self.data(username=admin.username)), reverse('portal:superadmin_dashboard'))
        self.assertEqual(User.objects.count(), 1)

    def test_recovery_requires_strong_server_token_and_global_admin_target(self):
        user = User.objects.create_user(username='teacher')
        with override_settings(INITIAL_SETUP_RECOVERY_USER_ID=user.pk):
            with self.assertRaises(CommandError):
                call_command('prepare_admin_recovery', username=user.username, stdout=StringIO())
        admin = User.objects.create_superuser(username='old-admin', password='Original-Password-941!')
        with override_settings(INITIAL_SETUP_RECOVERY_USER_ID=admin.pk, INITIAL_SETUP_TOKEN=''):
            with self.assertRaises(CommandError):
                call_command('prepare_admin_recovery', username=admin.username, stdout=StringIO())
            self.assertIsNotNone(InitialSetupState.objects.get(pk=1).completed_at)

    def test_recovery_audit_failure_rolls_back_password_and_pending_state(self):
        admin = User.objects.create_superuser(username='old-admin', password='Original-Password-941!')
        self.prepare(admin)
        with patch.object(AuditLog.objects, 'create', side_effect=DatabaseError('synthetic')):
            with self.assertRaises(DatabaseError):
                self.client.post(reverse('initial_setup'), self.data(username=admin.username))
        admin.refresh_from_db()
        self.assertTrue(admin.check_password('Original-Password-941!'))
        self.assertIsNone(InitialSetupState.objects.get(pk=1).completed_at)


@override_settings(INITIAL_SETUP_ENABLED=True, INITIAL_SETUP_TOKEN=SETUP_TOKEN,
                   ALLOWED_HOSTS=['192.168.1.13'], CSRF_TRUSTED_ORIGINS=['http://192.168.1.13:8085'])
class LanFirstRunTests(TestCase):
    setUp = InitialSetupTests.setUp
    data = InitialSetupTests.data
    def test_lan_setup_csrf_and_login_work_with_the_browser_address(self):
        client = Client(enforce_csrf_checks=True, HTTP_HOST='192.168.1.13:8085')
        self.assertRedirects(client.get(reverse('login')), reverse('initial_setup'))
        response = client.get(reverse('initial_setup'))
        self.assertEqual(response.status_code, 200)
        data = self.data(csrfmiddlewaretoken=client.cookies['schooljournal_csrftoken'].value)
        self.assertRedirects(client.post(reverse('initial_setup'), data, HTTP_ORIGIN='http://192.168.1.13:8085'), reverse('portal:superadmin_dashboard'))
        self.assertEqual(client.get('/admin/').status_code, 200)


@override_settings(INITIAL_SETUP_ENABLED=True, INITIAL_SETUP_TOKEN=SETUP_TOKEN)
class DemoBootstrapTests(TestCase):
    def test_demo_seed_does_not_create_or_overwrite_global_administrators(self):
        InitialSetupState.objects.update_or_create(pk=1, defaults={'completed_at': None})
        with tempfile.TemporaryDirectory() as directory:
            with patch('apps.portal.management.commands.seed_demo_data.__file__', str(Path(directory) / 'a/b/c/d/e.py')):
                call_command('seed_demo_data', stdout=StringIO())
                self.assertFalse(User.objects.filter(is_superuser=True).exists())
                self.assertTrue(initial_setup_required())
                admin = User.objects.create_superuser(username='my-real-admin', password='Synthetic-Real-Admin-941!')
                call_command('seed_demo_data', stdout=StringIO())
                admin.refresh_from_db()
                self.assertTrue(admin.check_password('Synthetic-Real-Admin-941!'))
                self.assertEqual(User.objects.filter(is_superuser=True).count(), 1)
