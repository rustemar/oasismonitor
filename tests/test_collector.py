import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import metrics_collector as m

ENTITY = 'AA=BB, C'
NODE = 'node='
STATUS = {'software_version': '26.1', 'identity': {'node': NODE},
          'registration': {'descriptor': {'entity_id': ENTITY, 'expiration': 125}, 'last_registration': '2026-11-09T10:00:00Z'},
          'consensus': {'latest_height': 123, 'latest_epoch': 124, 'is_validator': True, 'p2p': {'peers': ['p1', 'p2']}},
          'runtimes': {'abc': {'committee': {'latest_round': 10, 'latest_height': 122, 'executor_roles': ['worker'], 'peers': ['x']}},
                       'def': {'committee': {'latest_round': 20, 'latest_height': 123, 'executor_roles': None}}}}
ADDRESS = 'oasis1qpqz8g88kvw49m402k8m2r6nv4p62vsdkv5d0u6r'
ACCOUNT = f'''Active Delegations to this Account:
  Total: 1200.5 ROSE (123 shares)
  Delegations:
    - From:   {ADDRESS} (self)
      Amount: 200.25 ROSE (23 shares)

Commission Schedule:
  Rates:
    (1) start: epoch 100
        rate:  15.0%
    (2) start: epoch 200
        rate:  20.0%
'''


class CollectorTest(unittest.TestCase):
    def test_status_multiruntime_and_legacy_selection(self):
        with patch.object(m, 'command', return_value=json.dumps(STATUS)), patch.dict(os.environ, {}, clear=True):
            text, ok = m.collect('/node', 'unix:/node.sock', 'abc', False)
        self.assertTrue(ok)
        self.assertIn(r'entity=AA\=BB\,\ C,identityNode=node\=', text)
        self.assertIn('latestHeight=123.0', text)
        self.assertIn('peersCount=2.0', text)
        self.assertIn('runtimesLatestRound=10.0', text)
        self.assertIn('oasis_runtime,', text)
        self.assertIn('runtime_id=def latestRound=20.0', text)
        self.assertNotIn('currentReward=', text)
        self.assertNotIn('runtimesStorageRolesCount=', text)

    def test_ambiguous_runtime_omits_legacy_fields(self):
        with patch.object(m, 'command', return_value=json.dumps(STATUS)), patch.dict(os.environ, {}, clear=True):
            text, ok = m.collect('/node', 'unix:/node.sock', '', False)
        self.assertTrue(ok)
        self.assertNotIn('runtimesLatestRound=', text)
        self.assertEqual(text.count('oasis_runtime,'), 2)

    def test_failure_emits_only_down(self):
        with patch.object(m, 'command', side_effect=subprocess.TimeoutExpired('node', 7)), patch.dict(os.environ, {}, clear=True):
            text, ok = m.collect('/node', 'unix:/missing.sock', '', False)
        self.assertFalse(ok)
        self.assertIn('up=0.0', text)
        self.assertNotIn('latestHeight', text)

    def test_stake_schedule_does_not_use_future_rate(self):
        with patch.object(m, 'command', side_effect=[ADDRESS, ACCOUNT]):
            fields = m.get_stake('/node', 'unix:/sock', 'pub', 124)
        self.assertEqual(fields, {'activeStake': '1200.5', 'selfStake': '200.25', 'commissionRate': '15.0'})

    def test_optional_price_failure_does_not_hide_node(self):
        with patch.object(m, 'command', return_value=json.dumps(STATUS)), patch.object(m.urllib.request, 'urlopen', side_effect=OSError('offline')), patch.dict(os.environ, {'OASIS_PRICE': 'coingecko'}, clear=True):
            text, ok = m.collect('/node', 'unix:/sock', 'abc', False)
        self.assertTrue(ok)
        self.assertIn('up=1.0', text)
        self.assertNotIn('rosePrice=', text)

    def test_optional_stake_failure_does_not_hide_node(self):
        with patch.object(m, 'command', side_effect=[json.dumps(STATUS), subprocess.CalledProcessError(1, 'stake')]), patch.dict(os.environ, {}, clear=True):
            text, ok = m.collect('/node', 'unix:/sock', 'abc', True)
        self.assertTrue(ok)
        self.assertIn('up=1.0', text)
        self.assertNotIn('activeStake=', text)

    def test_atomic_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'metrics.lp'
            p.write_text('old')
            m.write_atomic(p, 'new\n')
            self.assertEqual(p.read_text(), 'new\n')
            self.assertEqual(p.stat().st_mode & 0o777, 0o644)
            self.assertEqual(list(Path(tmp).iterdir()), [p])


if __name__ == '__main__':
    unittest.main()
