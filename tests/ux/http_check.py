#!/usr/bin/env python3
"""Full HTTP workflow on the isolated UX Compose project, with synthetic data."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'integration'))
import run as h

h.COMMAND = ['docker', 'compose', '-f', str(ROOT / 'compose.yaml')]
h.BASE.update(test='http://127.0.0.1:19180', work='http://127.0.0.1:19100', journal='http://127.0.0.1:19185')
os.environ.setdefault('CHECK_EDITOR_IP', '10.253.142.20')
os.environ.setdefault('CHECK_IP_NETWORK', 'schooljournal-ux-ip-check')

if __name__ == '__main__':
    h.main()
