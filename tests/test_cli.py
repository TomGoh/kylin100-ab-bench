import contextlib
import io
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


if __name__ == '__main__':
    unittest.main()
