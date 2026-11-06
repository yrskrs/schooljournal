"""Non-editor pages avoid the editor bundle; compression preserves form contents."""
import gzip
from django.test import TestCase, RequestFactory
from django.template.loader import render_to_string
from django.contrib.auth.models import AnonymousUser


class PageOptimizationTests(TestCase):
    def test_login_compresses_without_losing_form_or_csrf(self):
        plain = self.client.get('/accounts/login/', HTTP_ACCEPT_ENCODING='identity')
        packed = self.client.get('/accounts/login/', HTTP_ACCEPT_ENCODING='gzip')
        self.assertEqual(packed.status_code, 200)
        self.assertEqual(packed.headers['Content-Encoding'], 'gzip')
        html = gzip.decompress(packed.content).decode()
        self.assertIn('csrfmiddlewaretoken', html)
        self.assertIn('name="username"', html)
        self.assertNotIn('/static/js/journal.js', html)
        self.assertLess(len(packed.content),len(plain.content)//2)

    def test_editor_script_remains_on_journal_page(self):
        from tests.test_journal_workflows import JournalWorkflowsTests
        JournalWorkflowsTests.setUp(self)
        response = self.client.get(f'/journal/{self.journal.pk}/')
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'/static/js/journal.js')
        self.assertContains(response,'hx-post=')
