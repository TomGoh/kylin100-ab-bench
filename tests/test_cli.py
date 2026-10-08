import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from abbench.__main__ import main


class EntryPointTests(unittest.TestCase):
    def invoke(self, argv):
        with patch('sys.argv', ['abbench', *argv]), contextlib.redirect_stdout(io.StringIO()):
            return main()

    def test_doctor_has_its_own_bounded_timeout_option(self):
        with patch('abbench.doctor.snapshot', return_value={'acquisition_complete': True}) as snapshot:
            self.assertEqual(self.invoke(['doctor', '--serial', 'board', '--out', '/new', '--timeout', '7']), 0)
            snapshot.assert_called_once_with('board', '/new', 7)

    def test_idle_preserves_integer_duration_and_failure_exit(self):
        with patch('abbench.idle.run_idle', return_value={'valid': False, 'reason': 'screen_state'}) as run:
            self.assertEqual(self.invoke(['idle', '--serial', 'board', '--out', '/new', '--kind', 'screen_off_standby', '--duration', '30', '--mode', 'native']), 2)
            self.assertIs(type(run.call_args.kwargs['duration_s']), int)

    def test_geekbench_cli_returns_failure_without_inventing_success(self):
        with patch('abbench.geekbench_runner.run_geekbench', return_value={'valid': False}) as run:
            self.assertEqual(self.invoke(['geekbench', '--serial', 'board', '--out', '/new', '--kind', 'gpu', '--mode', 'xhyper']), 2)
            self.assertEqual(run.call_args.kwargs['api'], 'Vulkan')

    def test_supply_preserves_evidence_and_requires_verified_external_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / 'profile.json'
            profile.write_text(json.dumps({'serial': 'physical-board'}))
            for verified, expected_rc in ((False, 2), (True, 0)):
                out = Path(tmp) / f'{verified}.json'
                with patch('abbench.supply.read_supply', return_value={'raw': 'observed'}) as read, \
                     patch('abbench.supply.verify_supply_snapshot', return_value={'verified_off': verified}) as qualify:
                    rc = self.invoke(['supply', '--serial', '192.0.2.8:5555', '--profile', str(profile), '--out', str(out)])
                self.assertEqual(rc, expected_rc)
                read.assert_called_once_with('192.0.2.8:5555')
                qualify.assert_called_once_with({'raw': 'observed'}, physical_serial='physical-board', profile={'serial': 'physical-board'})
                stored = json.loads(out.read_text())
                self.assertEqual(stored['valid'], verified)
                self.assertFalse(stored['sensor_calibrated'])
                self.assertEqual(stored['supply']['raw'], 'observed')


if __name__ == '__main__':
    unittest.main()
