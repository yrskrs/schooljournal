from unittest.mock import patch
from django.db import DatabaseError
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from apps.accounts.models import User
from apps.schools.models import School
from apps.journals.models import AuditLog
from apps.portal.site_info import release_info
from tests import test_school_isolation_and_access as access_tests


class RegistrationTests(TestCase):
    def data(self, **changes):
        return dict(school_name='Вигадана школа', school_short_name='Тестова',
                    edrpou_code='87654321', first_name='Тест', last_name='Адміністратор',
                    email='synthetic@example.invalid', username='synthetic-school-admin',
                    password1='Synthetic-Registration-5820!', password2='Synthetic-Registration-5820!', **changes)

    def test_register_creates_school_admin_and_logs_in(self):
        response = self.client.post(reverse('register_school'), self.data())
        self.assertRedirects(response, reverse('portal:superadmin_dashboard'))
        user = User.objects.get(username='synthetic-school-admin')
        self.assertEqual(user.role, User.Role.SCHOOL_ADMIN)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superadmin_user)
        self.assertTrue(user.is_school_admin_user)
        self.assertTrue(user.check_password(self.data()['password1']))
        self.assertEqual(user.school.edrpou_code, '87654321')
        self.assertEqual(AuditLog.objects.filter(school=user.school).count(), 1)

    def test_invalid_password_or_duplicate_username_creates_no_school(self):
        for changes in ({'password2': 'mismatch'}, {'password1': '12345678', 'password2': '12345678'}):
            data = self.data()
            data.update(changes)
            self.assertEqual(self.client.post(reverse('register_school'), data).status_code, 200)
            self.assertEqual(School.objects.count(), 0)
        User.objects.create_user(username='synthetic-school-admin', password='Synthetic-Password-823!')
        self.client.post(reverse('register_school'), self.data())
        self.assertEqual(School.objects.count(), 0)

    def test_duplicate_edrpou_is_reported(self):
        School.objects.create(name='Існуюча тестова школа', edrpou_code='87654321')
        response = self.client.post(reverse('register_school'), self.data())
        self.assertContains(response, 'вже зареєстровано')
        self.assertEqual(School.objects.count(), 1)
        self.assertEqual(User.objects.count(), 0)

    def test_registration_rolls_back_if_audit_write_fails(self):
        with patch.object(AuditLog.objects, 'create', side_effect=DatabaseError('synthetic')):
            with self.assertRaises(DatabaseError):
                self.client.post(reverse('register_school'), self.data())
        self.assertEqual(School.objects.count(), 0)
        self.assertEqual(User.objects.count(), 0)

    @override_settings(SCHOOL_REGISTRATION_ENABLED=False)
    def test_registration_can_be_disabled(self):
        self.assertEqual(self.client.get(reverse('register_school')).status_code, 404)
        self.assertEqual(self.client.post(reverse('register_school'), self.data()).status_code, 404)
        self.assertNotContains(self.client.get(reverse('login')), 'Зареєструвати школу')

    def test_registration_requires_csrf(self):
        response = Client(enforce_csrf_checks=True).post(reverse('register_school'), self.data())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(School.objects.count(), 0)

    def test_public_navigation_nz_button_guides_versions_health(self):
        login = self.client.get(reverse('login'))
        self.assertContains(login, 'Зареєструвати школу')
        self.assertContains(login, 'Увійти через портал nz.ua')
        self.assertContains(login, reverse('nz_login'))
        self.assertContains(self.client.get(reverse('portal:guide')), 'Як користуватися журналом')
        self.assertContains(self.client.get(reverse('portal:versions')), release_info()[0])
        health = self.client.get(reverse('portal:health'))
        self.assertEqual(health.json(), {'status': 'ok', 'version': release_info()[0]})
        self.assertIn('no-store', health['Cache-Control'])

    def test_health_reports_database_failure(self):
        with patch('apps.portal.views_public.connection.cursor', side_effect=DatabaseError('synthetic')):
            self.assertEqual(self.client.get(reverse('portal:health')).status_code, 503)


class SchoolAdminScopeTests(TestCase):
    def setUp(self):
        access_tests.SchoolIsolationAndAccessTestCase.setUp(self)
        self.admin = User.objects.create_user(username='synthetic-scoped-admin', password='Synthetic-823!',
                                             role=User.Role.SCHOOL_ADMIN, school=self.school1)
        self.client.force_login(self.admin)

    def test_admin_pages_render_only_own_school(self):
        for name in ('dashboard', 'schools', 'teachers', 'curriculum', 'assignments', 'students', 'audit'):
            response = self.client.get(reverse('portal:superadmin_' + name))
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, 'Школа №2')
            self.assertNotContains(response, 'teacher2')
        self.assertRedirects(self.client.get(reverse('portal:dashboard')), reverse('portal:superadmin_dashboard'))

    def test_foreign_school_cannot_be_selected(self):
        for name in ('curriculum', 'assignments', 'students'):
            url = reverse('portal:superadmin_' + name)
            self.assertEqual(self.client.get(url, {'school_id': self.school2.pk}).status_code, 404)
            self.assertEqual(self.client.post(url, {'school_id': self.school2.pk, 'action': 'create'}).status_code, 404)

    def test_foreign_school_cannot_be_edited_or_teacher_created(self):
        self.assertEqual(self.client.post(reverse('portal:superadmin_schools'), {
            'action': 'edit', 'school_id': self.school2.pk, 'name': 'forbidden'}).status_code, 404)
        self.assertEqual(self.client.post(reverse('portal:superadmin_teachers'), {
            'action': 'create', 'school_id': self.school2.pk, 'username': 'forbidden', 'password': 'Synthetic-Password!',
            'last_name': 'Вигаданий', 'first_name': 'Тест'}).status_code, 404)
        self.assertFalse(User.objects.filter(username='forbidden').exists())

    def test_foreign_teacher_password_and_status_cannot_be_changed(self):
        url = reverse('portal:superadmin_teachers')
        for action in ('reset_password', 'toggle_active'):
            self.assertEqual(self.client.post(url, {'action': action, 'teacher_id': self.teacher2.pk,
                                                    'new_password': 'Synthetic-Replacement!'}).status_code, 404)
        self.teacher2.refresh_from_db()
        self.assertTrue(self.teacher2.is_active)
        self.assertTrue(self.teacher2.check_password('password123'))

    def test_can_create_own_teacher_but_not_another_school(self):
        response = self.client.post(reverse('portal:superadmin_teachers'), {
            'action': 'create', 'school_id': self.school1.pk, 'username': 'own-new-teacher', 'password': 'Synthetic-Password!',
            'last_name': 'Вигаданий', 'first_name': 'Тест'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(User.objects.get(username='own-new-teacher').school, self.school1)
        self.assertEqual(self.client.post(reverse('portal:superadmin_schools'), {'action': 'create', 'name': 'forbidden'}).status_code, 403)
        self.assertEqual(self.client.post(reverse('portal:superadmin_schools'), {'action': 'toggle_active', 'school_id': self.school1.pk}).status_code, 403)

    def test_teacher_does_not_get_admin_access(self):
        self.client.force_login(self.teacher1)
        self.assertEqual(self.client.get(reverse('portal:superadmin_dashboard')).status_code, 403)


    def test_can_edit_own_school_and_invalid_data_is_rejected(self):
        url = reverse('portal:superadmin_schools')
        self.client.post(url, {'action': 'edit', 'school_id': self.school1.pk, 'name': 'Власна оновлена школа'})
        self.school1.refresh_from_db()
        self.assertEqual(self.school1.name, 'Власна оновлена школа')
        self.client.post(url, {'action': 'edit', 'school_id': self.school1.pk, 'name': ''})
        self.school1.refresh_from_db()
        self.assertEqual(self.school1.name, 'Власна оновлена школа')
