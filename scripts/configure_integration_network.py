#!/usr/bin/env python3
"""Host-side maintenance: keep only explicit, unique shared API aliases.

Compose adds the generic service alias `app` to every attached network. Two
independent projects can therefore collide despite explicit unique aliases.
Run after recreating containers; private networks and published ports stay intact.
"""
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]

def docker(*args):
    return subprocess.run(['docker',*args],cwd=ROOT,check=True,capture_output=True,text=True).stdout

def main():
    options=['compose','--env-file',str(ROOT/'.env'),'-f',str(ROOT/'compose.yaml')]
    for name in ('.release-image.yaml','compose.override.yaml'):
        if (ROOT/name).exists():options+=['-f',str(ROOT/name)]
    config=json.loads(docker(*options,'config','--format','json'))
    network=config.get('networks',{}).get('integration')
    if not network:
        print('Standalone configuration: no shared aliases to update.');return
    name=network['name']
    for service,definition in config['services'].items():
        data=definition.get('networks',{}).get('integration')
        if data is None:continue
        desired=list((data or {}).get('aliases',[]))
        for container in docker(*options,'ps','-q',service).split():
            info=json.loads(docker('inspect',container))[0]
            current=info['NetworkSettings']['Networks'].get(name)
            if not current:raise RuntimeError('Configured integration network is not attached.')
            native_name=info['Name'].lstrip('/')
            allowed={native_name,container,container[:12],*desired}
            aliases=current.get('Aliases') or []
            if set(aliases).issubset(allowed) and set(desired).issubset(aliases):continue
            docker('network','disconnect',name,container)
            arguments=['network','connect']
            for alias in desired:arguments+=['--alias',alias]
            if (data or {}).get('ipv4_address'):arguments+=['--ip',data['ipv4_address']]
            try:docker(*arguments,name,container)
            except Exception:
                restore=['network','connect']
                for alias in aliases:restore+=['--alias',alias]
                docker(*restore,name,container)
                raise
            print('Configured unique integration aliases:',service,', '.join(desired) or native_name)
    containers=json.loads(docker('network','inspect',name))[0].get('Containers',{})
    aliases={}
    for container in containers:
        state=json.loads(docker('inspect',container))[0]['NetworkSettings']['Networks'][name]
        for alias in state.get('Aliases') or []:aliases.setdefault(alias,[]).append(container)
    collisions=[key for key,owners in aliases.items() if len(set(owners))>1]
    if collisions:raise RuntimeError('Conflicting integration aliases: '+', '.join(collisions))
    print('Integration network aliases are unique.')

if __name__=='__main__':main()
