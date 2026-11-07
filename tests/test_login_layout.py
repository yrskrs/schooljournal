from django.test import TestCase
from django.urls import reverse
from bs4 import BeautifulSoup

class LoginLayoutTests(TestCase):
    def test_public_login_has_two_separate_forms_without_session_cookie_fields(self):
        response=self.client.get(reverse('login'))
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.context['login_method'],'nz')
        self.assertNotContains(response,'PHPSESSID')
        tree=BeautifulSoup(response.content,"html.parser")
        self.assertEqual(len(tree.select('[data-login-tab]')),2)
        self.assertEqual([e['value'] for e in tree.select('#panel-nz input[name=auth_method]')],['credentials'])
        self.assertFalse(tree.select('input[name=phpsessid]'))
        self.assertTrue(tree.select('#panel-local input[name=username]'))

    def test_failed_local_login_keeps_local_method_and_error(self):
        response=self.client.post(reverse('login'),{'username':'synthetic-missing-user','password':'synthetic'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.context['login_method'],'local')
        self.assertTrue(response.context['form'].errors)
        self.assertContains(response,'data-login-default="local"')

    def test_next_destination_is_preserved_in_local_form(self):
        response=self.client.get(reverse('login'),{'next':'/teacher/?page=2'})
        tree=BeautifulSoup(response.content,"html.parser")
        action=tree.select_one('#panel-local form')['action']
        self.assertIn('next=',action)
        self.assertIn('page%3D2',action)
