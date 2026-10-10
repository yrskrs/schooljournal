import json
import subprocess
from unittest.mock import patch
from django.test import SimpleTestCase
from scripts.check_candidate import check_candidate


class CandidateEnvironmentTests(SimpleTestCase):
    def test_resolved_quotes_json_and_multiline_values_reach_candidate_without_command_line_secrets(self):
        values = {'SECRET_KEY': 'synthetic-private-test-only', 'ROSTER_PEERS': '[{"url":"http://test:8000","token":"synthetic-token"}]',
                  'MULTILINE_VALUE': 'line one\nline two', 'UNSET': None}
        with patch('scripts.check_candidate.subprocess.run') as run:
            check_candidate('synthetic-candidate:local', {'services': {'web': {'environment': values}}})
        command = run.call_args.args[0]
        self.assertNotIn('--env-file', command)
        self.assertNotIn(values['SECRET_KEY'], ' '.join(command))
        # Execute only the environment assignment, not Docker or Django startup.
        with patch('os.environ', {}) as environment:
            exec(run.call_args.kwargs['input'].split('sys.argv =')[0])
            self.assertEqual(environment['ROSTER_PEERS'], values['ROSTER_PEERS'])
            self.assertEqual(environment['MULTILINE_VALUE'], values['MULTILINE_VALUE'])
            self.assertEqual(environment['SKIP_INIT'], '1')
            self.assertNotIn('UNSET', environment)

    def test_failed_candidate_check_propagates_before_database_migration(self):
        with patch('scripts.check_candidate.subprocess.run', side_effect=subprocess.CalledProcessError(1, ['docker'])):
            with self.assertRaises(subprocess.CalledProcessError):
                check_candidate('synthetic-candidate:local', {'services': {'web': {'environment': {}}}})
