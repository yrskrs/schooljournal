import json
from unittest.mock import patch, MagicMock
from django.test import TestCase, Client
from django.urls import reverse
from apps.accounts.models import User, School
from apps.portal.services.updates import (
    get_current_local_version,
    check_updates_available,
    get_update_status,
    parse_version_tuple,
    parse_changelog_markdown,
    get_releases_between,
)


class SystemUpdatesTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name='Тестова школа')
        self.admin = User.objects.create_user(
            username='test_admin',
            password='Password123!',
            school=self.school,
            role=User.Role.SCHOOL_ADMIN,
            is_staff=True
        )
        self.teacher = User.objects.create_user(
            username='test_teacher',
            password='Password123!',
            school=self.school,
            role=User.Role.TEACHER
        )
        self.client = Client()

    def test_version_tuple_parsing(self):
        self.assertEqual(parse_version_tuple('1.3.13'), (1, 3, 13))
        self.assertEqual(parse_version_tuple('1.4.0-rc1'), (1, 4, 0, 1))
        self.assertTrue(parse_version_tuple('1.3.14') > parse_version_tuple('1.3.13'))
        self.assertFalse(parse_version_tuple('1.3.13') > parse_version_tuple('1.3.13'))

    def test_unauthenticated_access_redirects_to_login(self):
        resp = self.client.get(reverse('portal:system_updates'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/accounts/login/', resp.url)

    def test_admin_can_view_updates_page(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('portal:system_updates'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Оновлення системи')
        self.assertContains(resp, 'Поточна встановлена версія')
        self.assertContains(resp, get_current_local_version())
        self.assertTrue(resp.context['can_update'])

    def test_teacher_can_view_page_but_cannot_start_update(self):
        self.client.force_login(self.teacher)
        resp = self.client.get(reverse('portal:system_updates'))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context['can_update'])

        start_resp = self.client.post(reverse('portal:updates_start_api'), {'version': '1.3.14'})
        self.assertEqual(start_resp.status_code, 403)

    def test_check_updates_api_authenticated(self):
        self.client.force_login(self.admin)
        with patch('apps.portal.views_updates.check_updates_available') as mock_check:
            mock_check.return_value = {
                'ok': True,
                'current_version': '1.3.13',
                'latest_version': '1.3.14',
                'has_update': True,
                'release': {
                    'version': '1.3.14',
                    'title': 'Нові функції оновлення',
                    'date': '2026-10-12',
                    'changes': ['Додано розділ оновлення на сайті'],
                },
            }
            resp = self.client.get(reverse('portal:updates_check_api'))
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data['ok'])
            self.assertEqual(data['latest_version'], '1.3.14')
            self.assertTrue(data['has_update'])
            self.assertEqual(data['release']['title'], 'Нові функції оновлення')

    def test_check_updates_service_via_updater(self):
        with patch('apps.portal.services.updates.urllib.request.urlopen') as mock_urlopen:
            resp = MagicMock()
            resp.read.return_value = json.dumps({
                'ok': True,
                'current_version': '1.3.13',
                'latest_version': '1.3.14',
                'has_update': True,
                'release': {'title': 'Тест через updater'}
            }).encode('utf-8')
            resp.__enter__.return_value = resp
            mock_urlopen.return_value = resp
            result = check_updates_available()
            self.assertTrue(result['ok'])
            self.assertEqual(result['latest_version'], '1.3.14')
            self.assertTrue(result['has_update'])

    def test_check_updates_service_fallback_github(self):
        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, 'full_url') else str(req)
            if 'updater' in url:
                raise Exception("Updater service down")
            if url.endswith('VERSION'):
                resp = MagicMock()
                resp.read.return_value = b'1.3.14'
                resp.__enter__.return_value = resp
                return resp
            if url.endswith('RELEASES.json'):
                resp = MagicMock()
                resp.read.return_value = json.dumps([{
                    'version': '1.3.14',
                    'title': 'Прямий чек з GitHub',
                    'changes': ['Зміни']
                }]).encode('utf-8')
                resp.__enter__.return_value = resp
                return resp
            raise ValueError(f"Unexpected url {url}")

        with patch('apps.portal.services.updates.urllib.request.urlopen', side_effect=fake_urlopen):
            result = check_updates_available()
            self.assertTrue(result['ok'])
            self.assertEqual(result['latest_version'], '1.3.14')
            self.assertTrue(result['has_update'])

    def test_update_status_api(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('portal:updates_status_api'))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn('state', data)
        self.assertIn('progress', data)

    def test_admin_can_trigger_update_api(self):
        self.client.force_login(self.admin)
        with patch('apps.portal.views_updates.start_update', return_value={'ok': True, 'message': 'Оновлення розпочато'}):
            resp = self.client.post(reverse('portal:updates_start_api'), {'version': '1.3.14'})
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data['ok'])

    def test_rollback_api_requires_admin(self):
        self.client.force_login(self.teacher)
        resp = self.client.post(reverse('portal:updates_rollback_api'), {'backup_file': '/tmp/test.tar.gz'})
        self.assertEqual(resp.status_code, 403)

        self.client.force_login(self.admin)
        with patch('apps.portal.views_updates.start_rollback', return_value={'ok': True, 'message': 'Відновлення запущено'}):
            resp = self.client.post(reverse('portal:updates_rollback_api'), {'backup_file': '/tmp/test.tar.gz'})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()['ok'])

    def test_parse_changelog_markdown(self):
        sample_md = """# Журнал змін

## 1.3.13 — 2026-10-12

- Перший пункт змін версії 13
- Другий пункт змін версії 13

## 1.3.12 — 2026-10-11

- Перший пункт версії 12
"""
        releases = parse_changelog_markdown(sample_md)
        self.assertEqual(len(releases), 2)
        self.assertEqual(releases[0]['version'], '1.3.13')
        self.assertEqual(releases[0]['date'], '2026-10-12')
        self.assertEqual(len(releases[0]['changes']), 2)
        self.assertIn('Перший пункт змін версії 13', releases[0]['changes'])
        self.assertEqual(releases[1]['version'], '1.3.12')
        self.assertEqual(len(releases[1]['changes']), 1)

    def test_get_releases_between_intermediate_versions(self):
        releases = [
            {'version': '1.3.14', 'changes': ['майбутня']},
            {'version': '1.3.13', 'changes': ['пункт 13']},
            {'version': '1.3.12', 'changes': ['пункт 12']},
            {'version': '1.3.11', 'changes': ['пункт 11']},
            {'version': '1.3.10', 'changes': ['поточна']},
            {'version': '1.3.9', 'changes': ['стара']},
        ]
        # Current is 1.3.10, target is 1.3.13. Must return 1.3.13, 1.3.12, 1.3.11
        matched = get_releases_between(releases, '1.3.10', '1.3.13')
        self.assertEqual(len(matched), 3)
        self.assertEqual([r['version'] for r in matched], ['1.3.13', '1.3.12', '1.3.11'])

    def test_check_updates_api_returns_all_intermediate_releases(self):
        self.client.force_login(self.admin)
        mock_releases = [
            {'version': '1.3.14', 'date': '2026-10-13', 'title': 'Виправлення', 'changes': ['Зміна 14']},
            {'version': '1.3.13', 'date': '2026-10-12', 'title': 'Оновлення', 'changes': ['Зміна 13']},
        ]
        with patch('apps.portal.views_updates.check_updates_available') as mock_check:
            mock_check.return_value = {
                'ok': True,
                'current_version': '1.3.12',
                'latest_version': '1.3.14',
                'has_update': True,
                'releases': mock_releases,
                'releases_count': 2,
                'release': mock_releases[0],
            }
            resp = self.client.get(reverse('portal:updates_check_api'))
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data['ok'])
            self.assertEqual(data['releases_count'], 2)
            self.assertEqual(len(data['releases']), 2)
            self.assertEqual(data['releases'][0]['version'], '1.3.14')
            self.assertEqual(data['releases'][1]['version'], '1.3.13')

