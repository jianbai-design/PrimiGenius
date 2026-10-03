import inspect
import os
import sys
import threading
import unittest
from unittest import mock


BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import podman_manager


class WslSelfRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.manager = object.__new__(podman_manager.PodmanManager)
        self.manager.wsl_distro_name = 'podman-primigenius-dev'
        self.manager._wsl_recovery_lock = threading.Lock()
        self.manager._api_client_lock = threading.RLock()
        self.manager._api_client = None
        self.manager._api_service_proc = None
        self.manager._wsl_service_started = False
        self.manager._wsl_distro_cache = {'value': False, 'time': 1.0}
        self.manager._wsl_ip_cache = {'value': None, 'time': 1.0}
        self.manager._engine_ready_cache = {'value': False, 'time': 1.0}
        self.manager._connection_state = 'degraded'
        self.manager._service_failures = 2
        self.manager._service_retry_after = 999.0
        self.manager._wsl_recovery_retry_after = 0.0
        self.manager._last_wsl_recovery_error = ''
        self.manager._run_host_command = mock.Mock(return_value=None)
        self.manager._restart_wsl_control_service_elevated = mock.Mock(return_value=False)
        self.manager._confirm_host_wide_wsl_recovery = mock.Mock(return_value=True)

    def test_distro_termination_is_the_first_and_only_needed_stage(self):
        self.manager._wait_for_wsl_distro = mock.Mock(return_value=True)

        ok, message = self.manager._recover_unresponsive_wsl()

        self.assertTrue(ok)
        self.assertIn('restarting the PrimiGenius environment', message)
        self.manager._run_host_command.assert_called_once_with(
            ['wsl', '--terminate', 'podman-primigenius-dev'], timeout=12
        )
        self.manager._restart_wsl_control_service_elevated.assert_not_called()

    def test_wsl_shutdown_is_used_when_distro_termination_does_not_recover(self):
        self.manager._wait_for_wsl_distro = mock.Mock(side_effect=[False, True])
        self.manager._run_host_command.return_value = mock.Mock(returncode=0)

        ok, message = self.manager._recover_unresponsive_wsl()

        self.assertTrue(ok)
        self.assertIn('virtual machine layer', message)
        self.assertEqual(
            [call.args[0] for call in self.manager._run_host_command.call_args_list],
            [
                ['wsl', '--terminate', 'podman-primigenius-dev'],
                ['wsl', '--shutdown'],
            ],
        )
        self.manager._restart_wsl_control_service_elevated.assert_not_called()

    def test_declined_host_wide_recovery_does_not_shutdown_wsl_or_request_uac(self):
        self.manager._wait_for_wsl_distro = mock.Mock(return_value=False)
        self.manager._confirm_host_wide_wsl_recovery.return_value = False

        ok, message = self.manager._recover_unresponsive_wsl()

        self.assertFalse(ok)
        self.assertIn('cancelled', message)
        self.manager._run_host_command.assert_called_once_with(
            ['wsl', '--terminate', 'podman-primigenius-dev'], timeout=12
        )
        self.manager._restart_wsl_control_service_elevated.assert_not_called()

    def test_elevated_service_recovery_is_the_final_stage(self):
        self.manager._wait_for_wsl_distro = mock.Mock(side_effect=[False, False, True])
        self.manager._run_host_command.return_value = mock.Mock(returncode=0)
        self.manager._restart_wsl_control_service_elevated.return_value = True

        with mock.patch.object(podman_manager.time, 'sleep'):
            ok, message = self.manager._recover_unresponsive_wsl()

        self.assertTrue(ok)
        self.assertIn('Windows service', message)
        self.manager._restart_wsl_control_service_elevated.assert_called_once_with()

    def test_failed_or_cancelled_uac_preserves_the_registered_environment(self):
        self.manager._wait_for_wsl_distro = mock.Mock(side_effect=[False, False])
        self.manager._run_host_command.return_value = mock.Mock(returncode=0)

        ok, message = self.manager._recover_unresponsive_wsl()

        self.assertFalse(ok)
        self.assertIn('data was preserved', message)
        self.assertEqual(self.manager._connection_state, 'recovery_failed')
        all_commands = repr(self.manager._run_host_command.call_args_list)
        self.assertNotIn('--unregister', all_commands)
        self.assertNotIn('machine rm', all_commands)

    def test_failed_recovery_has_cooldown_instead_of_repeating_uac(self):
        self.manager._wait_for_wsl_distro = mock.Mock(side_effect=[False, False])
        self.manager._run_host_command.return_value = mock.Mock(returncode=0)
        first_ok, first_message = self.manager._recover_unresponsive_wsl()
        first_command_count = self.manager._run_host_command.call_count

        second_ok, second_message = self.manager._recover_unresponsive_wsl()

        self.assertFalse(first_ok)
        self.assertFalse(second_ok)
        self.assertEqual(second_message, first_message)
        self.assertEqual(self.manager._run_host_command.call_count, first_command_count)
        self.manager._restart_wsl_control_service_elevated.assert_called_once_with()

    def test_unresponsive_wsl_shutdown_stops_recovery_and_requests_windows_restart(self):
        self.manager._wait_for_wsl_distro = mock.Mock(return_value=False)

        ok, message = self.manager._recover_unresponsive_wsl()

        self.assertFalse(ok)
        self.assertIn('restart Windows', message)
        self.assertEqual(self.manager._run_host_command.call_count, 2)
        self.manager._restart_wsl_control_service_elevated.assert_not_called()

    def test_recovery_implementation_has_no_destructive_wsl_operations(self):
        source = inspect.getsource(podman_manager.PodmanManager._recover_unresponsive_wsl)
        self.assertNotIn('--unregister', source)
        self.assertNotIn('machine_remove', source)
        self.assertNotIn('rmtree', source)
        self.assertNotIn('Remove-Item', source)

    def test_recovery_targets_each_supported_distribution_name_exactly(self):
        for distro_name in (
            'podman-primigenius-dev',
            'podman-primigenius-stable',
            'podman-machine-default',
        ):
            with self.subTest(distro_name=distro_name):
                self.manager.wsl_distro_name = distro_name
                self.manager._run_host_command.reset_mock()
                self.manager._wait_for_wsl_distro = mock.Mock(return_value=True)

                ok, _ = self.manager._recover_unresponsive_wsl()

                self.assertTrue(ok)
                self.manager._run_host_command.assert_called_once_with(
                    ['wsl', '--terminate', distro_name], timeout=12
                )

    def test_suspend_preparation_stops_service_then_owned_distro(self):
        self.manager._stop_wsl_podman_service = mock.Mock()
        self.manager._run_host_command.return_value = mock.Mock(returncode=0)

        with mock.patch.object(podman_manager.platform, 'system', return_value='Windows'):
            ok, message = self.manager.prepare_for_host_suspend()

        self.assertTrue(ok)
        self.assertIn('stopped before system suspend', message)
        self.manager._stop_wsl_podman_service.assert_called_once_with()
        self.manager._run_host_command.assert_called_once_with(
            ['wsl', '--terminate', 'podman-primigenius-dev'], timeout=12
        )

    def test_service_restart_uses_fixed_modern_and_legacy_service_names(self):
        self.manager._restart_wsl_control_service_elevated = (
            podman_manager.PodmanManager._restart_wsl_control_service_elevated.__get__(self.manager)
        )
        with mock.patch.object(podman_manager.platform, 'system', return_value='Windows'), \
                mock.patch.object(podman_manager.shutil, 'which', return_value=r'C:\Windows\powershell.exe'), \
                mock.patch.object(podman_manager, '_run_elevated_windows_command', return_value=0) as elevated:
            ok = self.manager._restart_wsl_control_service_elevated()

        self.assertTrue(ok)
        executable, args = elevated.call_args.args[:2]
        self.assertEqual(executable, r'C:\Windows\powershell.exe')
        script = args[-1]
        self.assertIn("'WslService'", script)
        self.assertIn("'LxssManager'", script)
        self.assertNotIn(self.manager.wsl_distro_name, script)


class RuntimeChannelTests(unittest.TestCase):
    def test_frozen_distribution_defaults_to_stable_channel(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(podman_manager.sys, 'frozen', True, create=True):
            self.assertEqual(podman_manager._runtime_channel(), 'stable')

    def test_stable_machine_maps_to_expected_wsl_distribution(self):
        machine_name = podman_manager.choose_machine_name('stable')
        self.assertEqual(machine_name, 'primigenius-stable')
        self.assertEqual(
            podman_manager.machine_to_wsl_distro_name(machine_name),
            'podman-primigenius-stable',
        )


if __name__ == '__main__':
    unittest.main()
