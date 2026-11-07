"""Regressions for legacy names and successful-status NZ error pages."""
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.journals.models import NZAccount
from apps.journals.services.nz_service import NZService
from apps.schools.models import School


class NZNameStabilityTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name='Synthetic name regression school')
        self.user = User.objects.create_user(
            username='name-regression', school=self.school, is_nz_personal=True,
            first_name='Оксана', last_name='Коваленко', patronymic='Іванівна')
        self.account = NZAccount.objects.create(user=self.user, login='synthetic-nz-login')

    @patch.object(NZService, 'verify_session', return_value=(True, 'Авторизовано'))
    @patch.object(NZService, 'login', return_value=(True, 'ok'))
    def test_legacy_placeholder_without_remote_name_connects_without_copying_foreign_user(self, *_):
        other_school = School.objects.create(name='Other synthetic school')
        User.objects.create_user(username=self.account.login, school=other_school,
                                 first_name='Чуже', last_name='Прізвище')
        self.user.first_name, self.user.last_name, self.user.patronymic = 'nz.ua', 'Вчитель', ''
        self.user.save()
        self.client.force_login(self.user)
        response = self.client.post(reverse('portal:nz_connect'), {
            'auth_method': 'credentials', 'login': self.account.login, 'password': 'synthetic'})
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.account.refresh_from_db()
        self.assertTrue(self.account.is_active)
        self.assertEqual((self.user.first_name, self.user.last_name), ('', 'Вчитель'))

    @patch.object(NZService, 'verify_session')
    @patch.object(NZService, 'login', return_value=(True, 'ok'))
    def test_connect_and_login_preserve_existing_structured_names(self, login, verify):
        for route in ('portal:nz_connect', 'nz_login'):
            for label in ('Оксана Коваленко', 'Коваленко Оксана Петрівна'):
                with self.subTest(route=route, label=label):
                    verify.return_value = True, label
                    self.client.force_login(self.user)
                    response = self.client.post(reverse(route), {
                        'auth_method': 'credentials', 'login': self.account.login, 'password': 'synthetic'})
                    self.assertEqual(response.status_code, 302)
                    self.user.refresh_from_db()
                    self.assertEqual((self.user.last_name, self.user.first_name, self.user.patronymic),
                                     ('Коваленко', 'Оксана', 'Іванівна'))

    @patch.object(NZService, 'verify_session', return_value=(True, 'Оксана Коваленко'))
    @patch.object(NZService, 'login', return_value=(True, 'ok'))
    def test_new_profile_does_not_guess_order_of_two_word_label(self, *_):
        response = self.client.post(reverse('nz_login'), {
            'auth_method': 'credentials', 'login': 'new-synthetic-account', 'password': 'synthetic'})
        self.assertEqual(response.status_code, 302)
        new_user = NZAccount.objects.get(login='new-synthetic-account').user
        self.assertEqual((new_user.last_name, new_user.first_name), ('Вчитель', ''))


class NZAvailabilityStabilityTests(TestCase):
    ERROR_PAGES = (
        (200, '<html>Ведуться роботи з оновлення</html>', 'недоступний'),
        (200, '<html>Web server is down</html>', 'недоступний'),
        (503, '<html>Service unavailable</html>', 'недоступний'),
        (200, '<script>var _cf_chl_opt = {};</script>', 'Cloudflare'),
        (403, '<html>Forbidden</html>', 'Cloudflare'),
    )

    @patch('apps.journals.services.nz_service.requests.Session.get')
    def test_verify_session_reports_error_pages_before_authentication(self, get):
        for status, body, message in self.ERROR_PAGES:
            with self.subTest(status=status, body=body):
                get.return_value = MagicMock(status_code=status, text=body, url='https://nz.ua/journal/list')
                valid, info = NZService().verify_session()
                self.assertFalse(valid)
                self.assertIn(message, info)

    @patch('apps.journals.services.nz_service.requests.Session.post')
    @patch('apps.journals.services.nz_service.requests.Session.get')
    def test_login_reports_error_pages_before_and_after_post(self, get, post):
        for stage in ('get', 'post'):
            for status, body, message in self.ERROR_PAGES:
                with self.subTest(stage=stage, status=status, body=body):
                    get.reset_mock(); post.reset_mock()
                    error = MagicMock(status_code=status, text=body, url='https://nz.ua/login')
                    get.return_value = error if stage == 'get' else MagicMock(
                        status_code=200, text='<meta name="csrf-token" content="synthetic">')
                    post.return_value = error
                    valid, info = NZService().login('synthetic', 'synthetic')
                    self.assertFalse(valid)
                    self.assertIn(message, info)
                    if stage == 'get':
                        post.assert_not_called()
