"""Check a candidate with resolved Compose settings, without exposing secrets."""
import json
import subprocess
import sys


def check_candidate(image, config):
    environment = {key: str(value) for key, value in config['services']['web'].get('environment', {}).items()
                   if value is not None}
    environment['SKIP_INIT'] = '1'
    # Docker --env-file and Compose parse quotes differently. Pass the already
    # resolved environment on stdin, supporting quoted JSON and multiline values.
    bootstrap = ('import json, os, runpy, sys\n'
                 'os.environ.update(json.loads(' + repr(json.dumps(environment)) + '))\n'
                 'sys.argv = ["manage.py", "check"]\n'
                 'runpy.run_path("/app/manage.py", run_name="__main__")\n')
    subprocess.run(['docker', 'run', '--rm', '-i', '--entrypoint', 'python', image, '-'],
                   input=bootstrap, text=True, check=True)


if __name__ == '__main__':
    check_candidate(sys.argv[1], json.load(sys.stdin))
