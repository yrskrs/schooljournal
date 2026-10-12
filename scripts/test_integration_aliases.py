"""An unrelated project's generic alias must not break this project's maintenance."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('alias_helper',Path(__file__).with_name('configure_integration_network.py'))
helper=importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)

class IntegrationAliasTests(unittest.TestCase):
    def fake_docker(self,*args):
        if args[0]=='compose' and args[-3:]==('config','--format','json'):
            return json.dumps({'networks':{'integration':{'name':'shared'}},'services':{'web':{'networks':{'integration':{'aliases':['own-api']}}}}})
        if args[0]=='compose' and args[-3:]==('ps','-q','web'):
            return 'own'
        if args[:2]==('network','inspect'):
            return json.dumps([{'Containers':{key:{} for key in self.aliases}}])
        if args[0]=='inspect':
            key=args[1]
            return json.dumps([{'Name':'/'+key,'NetworkSettings':{'Networks':{'shared':{'Aliases':self.aliases[key]}}}}])
        raise AssertionError(args)

    def setUp(self):
        self.aliases={'own':['own','own-api'],'other-a':['other-a','app'],'other-b':['other-b','app']}

    def test_unrelated_generic_alias_collision_does_not_block_own_site(self):
        with patch.object(helper,'docker',side_effect=self.fake_docker):
            helper.main()

    def test_own_api_alias_collision_still_fails(self):
        self.aliases['other-a'].append('own-api')
        with patch.object(helper,'docker',side_effect=self.fake_docker), self.assertRaisesRegex(RuntimeError,'own-api'):
            helper.main()

if __name__=='__main__': unittest.main()
