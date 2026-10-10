from pathlib import Path
import shutil
import subprocess
import tempfile
from unittest.mock import patch

from django.conf import settings
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


class SetupScriptTests(TestCase):
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
