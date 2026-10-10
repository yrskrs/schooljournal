#!/usr/bin/env python3
"""Fetch the configured repository; public GitHub clones work without SSH keys."""
import argparse
import os
import re
import subprocess
from urllib.parse import urlsplit


def github_https(remote):
    match = re.fullmatch(r'git@github\.com:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?', remote)
    if match:
        return 'https://github.com/' + match.group(1) + '.git'
    parsed = urlsplit(remote)
    if parsed.scheme == 'ssh' and parsed.hostname == 'github.com' and parsed.username == 'git' and parsed.port in (None, 22):
        path = parsed.path.strip('/')
        if re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', path):
            return 'https://github.com/' + path.removesuffix('.git') + '.git'
    return remote


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--branch', help='Fetch only this branch; otherwise fetch branches and tags.')
    args = parser.parse_args()
    remote = subprocess.check_output(['git', 'remote', 'get-url', 'origin'], text=True).strip()
    url = github_https(remote)
    command = ['git', 'fetch', '--no-tags' if args.branch else '--tags', url]
    if args.branch:
        subprocess.run(['git', 'check-ref-format', '--branch', args.branch], check=True, stdout=subprocess.DEVNULL)
        command.append('refs/heads/' + args.branch + ':refs/remotes/origin/' + args.branch)
    else:
        command.append('refs/heads/*:refs/remotes/origin/*')
    environment = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    try:
        subprocess.run(command, check=True, env=environment)
    except subprocess.CalledProcessError:
        raise SystemExit('Не вдалося отримати оновлення. Перевірте інтернет та доступ до налаштованого репозиторію; робочі дані не змінено.')
    print('Код отримано через HTTPS із GitHub.' if url.startswith('https://github.com/') else 'Код отримано з налаштованого origin.')


if __name__ == '__main__':
    main()
