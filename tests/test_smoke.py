import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from avdlite.smoke import Device, verify_reboot


class SmokeTests(unittest.TestCase):
    def device(self):
        device = Device(Path('sdk'), Path('evidence'), {})
        device.process = Mock()
        device.process.poll.return_value = None
        return device

    def test_reboot_accepts_disconnect_only_after_a_new_valid_boot_and_readiness(self):
        before, after = '11111111-1111-1111-1111-111111111111', '22222222-2222-2222-2222-222222222222'
        for request in ('', subprocess.TimeoutExpired(['adb', 'shell', 'svc', 'power', 'reboot'], 60)):
            with self.subTest(request=request):
                device = self.device()
                device.shell = Mock(side_effect=[before, request, '', before, 'error: device offline', after])
                device.ready = Mock()
                with patch('avdlite.smoke.time.sleep') as sleep:
                    self.assertEqual(device.reboot(), {'before': before, 'after': after})
                self.assertEqual(sleep.call_count, 3)
                device.shell.assert_any_call('svc', 'power', 'reboot', check=False, timeout=60)
                device.ready.assert_called_once()

    def test_reboot_without_a_new_boot_id_fails(self):
        device = self.device()
        before = '11111111-1111-1111-1111-111111111111'
        device.shell = Mock(side_effect=[before, '', before])
        device.ready = Mock()
        with patch('avdlite.smoke.time.monotonic', side_effect=[0, 0, 421]), patch('avdlite.smoke.time.sleep'):
            with self.assertRaisesRegex(TimeoutError, 'new boot_id'):
                device.reboot()
        device.ready.assert_not_called()
        device.shell = Mock(return_value='error: device offline')
        with self.assertRaisesRegex(ValueError, 'Invalid boot_id'):
            device.reboot()
        self.assertEqual(device.shell.call_count, 1)

    def test_poll_timeout_retries_but_emulator_exit_and_command_errors_fail(self):
        device = self.device()
        probe = Mock(side_effect=[subprocess.TimeoutExpired(['adb'], 30), 'ready'])
        with patch('avdlite.smoke.time.sleep'):
            self.assertEqual(device.wait(probe, 'not ready'), 'ready')
        with self.assertRaisesRegex(RuntimeError, 'denied'):
            device.wait(Mock(side_effect=RuntimeError('permission denied')), 'not ready')
        device.process.poll.return_value = 1
        device.process.returncode = 1
        with self.assertRaisesRegex(RuntimeError, 'Emulator exited early'):
            device.wait(probe, 'not ready')

    def test_readiness_waits_for_user_storage_after_boot_and_package_manager(self):
        device = self.device()
        device.shell = Mock(side_effect=['1', 'package:/system/SystemUI.apk', 'RUNNING_LOCKED', 'RUNNING_UNLOCKING', 'RUNNING_UNLOCKED'])
        with patch('avdlite.smoke.time.sleep') as sleep:
            device.ready()
        self.assertEqual(sleep.call_count, 2)
        device.shell.assert_called_with('am', 'get-started-user-state', '0', check=False)

    def test_schedule_is_checked_before_reboot_and_afterward_without_rewriting(self):
        expected = {'ui_night_mode': '3', 'dark_theme_custom_start_time': '79200000000', 'dark_theme_custom_end_time': '21600000000'}
        for persisted in (True, False):
            with self.subTest(persisted=persisted):
                device = self.device()
                rebooted = False
                writes = []

                def shell(*args, **kwargs):
                    if args[:4] == ('settings', '--user', '0', 'get'):
                        return expected[args[-1]] if not rebooted or persisted else 'null'
                    writes.append(args)
                    return ''

                def reboot():
                    nonlocal rebooted
                    self.assertEqual(len(writes), 3)
                    rebooted = True
                    return {'before': 'old', 'after': 'new'}

                device.shell = Mock(side_effect=shell)
                device.reboot = Mock(side_effect=reboot)
                if persisted:
                    self.assertEqual(verify_reboot(device)['night_schedule_after_reboot'], expected)
                else:
                    with self.assertRaisesRegex(AssertionError, 'persistence verification failed'):
                        verify_reboot(device)
                self.assertEqual(len(writes), 3)
                device.reboot.assert_called_once()

    def test_unapplied_schedule_cannot_start_reboot(self):
        device = self.device()
        device.shell = Mock(return_value='null')
        device.reboot = Mock()
        with patch('avdlite.smoke.time.monotonic', side_effect=[0, 0, 421]), patch('avdlite.smoke.time.sleep'):
            with self.assertRaisesRegex(TimeoutError, 'not applied before reboot'):
                verify_reboot(device)
        device.reboot.assert_not_called()


if __name__ == '__main__':
    unittest.main()
