"""Legacy storage selection and non-destructive startup regression checks."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
import runtime_selection as selection
from podman_manager import PodmanManager


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'build', prefix='runtime-selection-')
        self.addCleanup(self.temp.cleanup)
        self.install = Path(self.temp.name) / '新版 安装目录'
        self.data = self.install / 'podman-data'
        self.config = self.install / 'podman-config' / 'machine-runtime.json'
        self.entries = []
        singleton = patch.object(PodmanManager, '_instance', None)
        singleton.start()
        self.addCleanup(singleton.stop)

    def save(self, machine=selection.STABLE, **values):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps(dict(channel='stable', machine_name=machine,
                                              data_dir=str(self.data), **values)), encoding='utf-8')

    def disk(self, machine, data=None, layout=0, registered=True, content=b'original images and containers'):
        data = data or self.data
        locations = list(selection.disk_locations(data, machine))
        folder = locations[layout]
        folder.mkdir(parents=True, exist_ok=True)
        disk = folder / 'ext4.vhdx'
        disk.write_bytes(content)
        if registered:
            self.entries.append(dict(name=selection.distro_name(machine), base=str(folder),
                                     disk_name='ext4.vhdx', version=2))
        return disk

    def plan(self, channel='stable'):
        return selection.select_runtime(channel, self.data, self.config, self.entries)

    def test_every_legacy_layout_is_reused(self):
        for layout in range(len(list(selection.disk_locations(self.data, selection.LEGACY)))):
            with self.subTest(layout=layout):
                self.entries.clear()
                disk = self.disk(selection.LEGACY, layout=layout)
                plan = self.plan()
                self.assertEqual(plan['machine'], selection.LEGACY)
                self.assertEqual(plan['error'], '')
                self.assertFalse(plan['pending'])
                self.assertEqual(disk.read_bytes(), b'original images and containers')

    def test_missing_persisted_stable_uses_complete_legacy(self):
        self.save(requested_resources={'cpus': 6})
        self.entries.append(dict(name='podman-primigenius-stable', base=str(self.data / 'missing')))
        disk = self.disk(selection.LEGACY, layout=4)
        before = self.config.read_bytes()
        plan = self.plan()
        self.assertEqual(plan['machine'], selection.LEGACY)
        self.assertFalse(plan['error'])
        self.assertIn('recovered existing', plan['reason'])
        self.assertEqual(before, self.config.read_bytes())
        self.assertEqual(disk.read_bytes(), b'original images and containers')

    def test_old_installation_is_found_at_its_registered_location(self):
        old = Path(self.temp.name) / '旧 安装目录' / 'podman-data'
        (old.parent / 'podman-config').mkdir(parents=True)
        disk = self.disk(selection.LEGACY, data=old, layout=4)
        self.save()
        plan = self.plan()
        self.assertEqual(plan['machine'], selection.LEGACY)
        self.assertEqual(Path(plan['data_dir']), old)
        self.assertFalse(plan['error'])
        self.assertEqual(disk.read_bytes(), b'original images and containers')
        self.assertFalse(self.data.exists())

    def test_external_custom_storage_with_old_identity_is_owned(self):
        old = Path(self.temp.name) / '历史应用' / 'user-chosen-volume'
        metadata = old.parent / 'podman-config' / 'machine-runtime.json'
        metadata.parent.mkdir(parents=True)
        metadata.write_text(json.dumps(dict(machine_name=selection.LEGACY, data_dir=str(old))))
        self.disk(selection.LEGACY, data=old, layout=4)
        self.assertEqual(Path(self.plan()['data_dir']), old)

    @unittest.skipUnless(os.name == 'nt', 'Windows extended path convention')
    def test_extended_length_wsl_base_path_keeps_original_storage(self):
        old = Path(self.temp.name) / '旧安装' / 'podman-data'
        (old.parent / 'podman-config').mkdir(parents=True)
        self.disk(selection.LEGACY, data=old, layout=4)
        self.entries[0]['base'] = '\\\\?\\' + self.entries[0]['base']
        plan = self.plan()
        self.assertFalse(plan['error'])
        self.assertEqual(plan['machine'], selection.LEGACY)
        self.assertEqual(Path(plan['data_dir']), old)

    def test_other_software_default_machine_is_not_adopted(self):
        disk = self.disk(selection.LEGACY, data=Path(self.temp.name)/'PodmanDesktop'/'storage')
        plan = self.plan()
        self.assertEqual(plan['machine'], selection.STABLE)
        self.assertFalse(plan['error'])
        self.assertEqual(disk.read_bytes(), b'original images and containers')

    def test_empty_folder_is_not_a_legacy_machine(self):
        (self.data/'containers/podman/machine/wsl'/selection.LEGACY).mkdir(parents=True)
        self.assertEqual(self.plan()['machine'], selection.STABLE)

    def test_valid_persisted_stable_remains_selected_with_both_disks_present(self):
        self.save()
        self.disk(selection.LEGACY, layout=4)
        self.disk(selection.STABLE, layout=2)
        self.assertEqual(self.plan()['machine'], selection.STABLE)

    def test_missing_legacy_uses_an_available_owned_stable_environment(self):
        self.save(selection.LEGACY)
        self.entries.append(dict(name=selection.LEGACY,base=str(self.data/'missing')))
        self.disk(selection.STABLE)
        plan = self.plan()
        self.assertEqual(plan['machine'], selection.STABLE)
        self.assertFalse(plan['error'])

    def test_both_missing_disks_provision_a_free_name_and_preserve_metadata(self):
        self.save()
        before = self.config.read_bytes()
        for name in (selection.STABLE, selection.LEGACY):
            self.entries.append(dict(name=selection.distro_name(name),base=str(self.data/name/'missing')))
        plan = self.plan()
        self.assertFalse(plan['error'])
        self.assertEqual(plan['machine'], selection.STABLE + '-2')
        self.assertTrue(plan['pending'])
        self.assertEqual(self.config.read_bytes(),before)
        self.assertFalse(self.data.exists())

    def test_zero_byte_disk_is_not_a_complete_runtime(self):
        self.save()
        self.disk(selection.STABLE,content=b'')
        self.assertIn('empty',self.plan()['error'])

    def test_unregistered_old_disk_is_preserved_beside_a_new_runtime(self):
        disk = self.disk(selection.LEGACY,layout=4,registered=False)
        plan = self.plan()
        self.assertFalse(plan['error'])
        self.assertTrue(plan['pending'])
        self.assertNotEqual(Path(plan['data_dir']), self.data)
        self.assertTrue(disk.exists())

    def test_preserved_backup_remains_untouched_when_a_new_runtime_is_created(self):
        backup = self.install.with_name(self.install.name+'.primigenius-preserved')
        backup.mkdir()
        (backup/'original.vhdx').write_bytes(b'backup')
        plan = self.plan()
        self.assertFalse(plan['error'])
        self.assertTrue(plan['pending'])
        self.assertEqual((backup/'original.vhdx').read_bytes(), b'backup')

    def test_duplicate_registered_names_are_not_selected(self):
        self.disk(selection.LEGACY)
        self.entries.append(dict(self.entries[0]))
        self.assertIn('Multiple WSL registrations',self.plan()['error'])

    def test_malformed_config_is_preserved_and_blocks_initialization(self):
        self.save()
        self.config.write_bytes(b'{incomplete config')
        self.assertTrue(self.plan()['error'])
        self.assertEqual(self.config.read_bytes(),b'{incomplete config')

    def test_access_denied_is_not_treated_as_an_empty_installation(self):
        with patch.object(selection,'read_registrations',side_effect=PermissionError('registry denied')):
            plan=selection.select_runtime('stable',self.data,self.config)
        self.assertIn('registry denied',plan['error'])

    def test_disk_access_error_does_not_switch_to_a_different_machine(self):
        self.save()
        self.disk(selection.STABLE)
        self.disk(selection.LEGACY,layout=4)
        real=selection._disk_status
        target=self.entries[0]['base']
        def probe(path):
            if path and str(path.parent)==target: return 'inaccessible'
            return real(path)
        with patch.object(selection,'_disk_status',side_effect=probe):
            self.assertIn('inaccessible',self.plan()['error'])

    def test_unselected_inaccessible_disk_does_not_block_healthy_legacy(self):
        self.save(selection.LEGACY)
        self.disk(selection.LEGACY,layout=4)
        self.disk(selection.STABLE)
        target=self.entries[-1]['base']
        real=selection._disk_status
        with patch.object(selection,'_disk_status',side_effect=lambda path:
                          'inaccessible' if path and str(path.parent)==target else real(path)):
            plan=self.plan()
        self.assertEqual(plan['machine'],selection.LEGACY)
        self.assertFalse(plan['error'])

    def test_first_install_can_restart_before_and_after_provisioning(self):
        with patch.dict(os.environ,{'PRIMIGENIUS_CHANNEL':'stable'}), \
                patch.object(selection,'read_registrations',side_effect=lambda:self.entries):
            first=PodmanManager(app_install_dir=str(self.install))
            self.assertTrue(json.loads(self.config.read_text(encoding='utf-8'))['provisioning_pending'])
            with patch.object(PodmanManager,'_instance',None):
                pending=PodmanManager(app_install_dir=str(self.install))
            self.assertFalse(pending._runtime_selection_error)
            self.disk(selection.STABLE)
            with patch.object(PodmanManager,'_instance',None):
                ready=PodmanManager(app_install_dir=str(self.install))
            self.assertFalse(ready._runtime_selection_error)
            self.assertFalse(json.loads(self.config.read_text(encoding='utf-8'))['provisioning_pending'])

    def test_dev_channel_never_adopts_stable_or_default(self):
        self.disk(selection.LEGACY,layout=4)
        self.disk(selection.STABLE,layout=2)
        plan=self.plan('dev')
        self.assertEqual(plan['machine'],selection.DEV)
        self.assertFalse(plan['error'])

    def test_fresh_and_pending_first_install_can_initialize(self):
        self.assertTrue(self.plan()['pending'])
        self.save(provisioning_pending=True)
        self.assertFalse(self.plan()['error'])

    def test_lost_existing_registration_automatically_provisions_a_new_runtime(self):
        self.save(provisioning_pending=False)
        plan = self.plan()
        self.assertFalse(plan['error'])
        self.assertEqual(plan['machine'], selection.STABLE)
        self.assertTrue(plan['pending'])

    def test_external_unverified_legacy_selection_does_not_block_new_stable(self):
        self.save(selection.LEGACY)
        old = Path(self.temp.name) / '旧安装' / 'podman-data'
        disk = self.disk(selection.LEGACY, data=old, layout=4)
        before = copy.deepcopy(self.entries)
        plan = self.plan()
        self.assertFalse(plan['error'])
        self.assertEqual(plan['machine'], selection.STABLE)
        self.assertTrue(plan['pending'])
        self.assertEqual(self.entries, before)
        self.assertEqual(disk.read_bytes(), b'original images and containers')

    def test_used_stable_names_are_skipped_and_pending_choice_survives_restart(self):
        self.save()
        for machine in (selection.STABLE, selection.STABLE + '-2'):
            self.entries.append(dict(name=selection.distro_name(machine), base=str(self.data / machine / 'missing')))
        with patch.dict(os.environ, {'PRIMIGENIUS_CHANNEL': 'stable'}), \
                patch.object(selection, 'read_registrations', return_value=self.entries):
            manager = PodmanManager(app_install_dir=str(self.install))
            self.assertEqual(manager.machine_name, selection.STABLE + '-3')
            root = manager.podman_data_dir
            original_selection = list(self.config.parent.glob('registration-recovery/selection-*.json'))
            self.assertEqual(len(original_selection), 1)
            self.assertEqual(json.loads(original_selection[0].read_text(encoding='utf-8'))['machine_name'], selection.STABLE)
            with patch.object(PodmanManager, '_instance', None):
                restarted = PodmanManager(app_install_dir=str(self.install))
            self.assertEqual(restarted.machine_name, manager.machine_name)
            self.assertEqual(restarted.podman_data_dir, root)
            self.assertTrue(restarted._runtime_provisioning_pending)

    def test_healthy_numbered_runtime_is_reused(self):
        self.save(selection.STABLE + '-2')
        disk = self.disk(selection.STABLE + '-2')
        plan = self.plan()
        self.assertFalse(plan['error'])
        self.assertFalse(plan['pending'])
        self.assertEqual(plan['machine'], selection.STABLE + '-2')
        self.assertTrue(disk.is_file())

    def test_dev_fallback_uses_only_dev_names(self):
        self.entries.append(dict(name=selection.distro_name(selection.DEV), base=str(self.data/'lost-dev')))
        self.disk(selection.STABLE, layout=2)
        plan = self.plan('dev')
        self.assertFalse(plan['error'])
        self.assertEqual(plan['machine'], selection.DEV + '-2')

    def test_pending_provision_cannot_start_ipc_before_import(self):
        self.save(provisioning_pending=False)
        with patch.dict(os.environ, {'PRIMIGENIUS_CHANNEL': 'stable'}), \
                patch.object(selection, 'read_registrations', return_value=[]):
            manager = PodmanManager(app_install_dir=str(self.install))
        manager.runtime.start = Mock()
        self.assertFalse(manager._start_wsl_podman_service_locked(force=True))
        manager.runtime.start.assert_not_called()
        self.assertEqual(manager._connection_state, 'starting')

    def test_backup_failure_blocks_new_environment_before_import(self):
        self.save()
        original = self.config.read_bytes()
        with patch.dict(os.environ, {'PRIMIGENIUS_CHANNEL': 'stable'}), \
                patch.object(selection, 'read_registrations', return_value=[]), \
                patch('podman_manager.os.fsync', side_effect=OSError('disk full')):
            manager = PodmanManager(app_install_dir=str(self.install))
        self.assertTrue(manager._runtime_selection_error)
        self.assertTrue(manager._wsl_storage_missing)
        self.assertEqual(self.config.read_bytes(), original)

    def test_new_runtime_import_and_next_start_preserve_old_registrations(self):
        self.save()
        self.entries.append(dict(name=selection.distro_name(selection.STABLE), base=str(self.data/'lost')))
        old_entry = dict(self.entries[0])
        bundle = Path(self.temp.name) / 'runtime-bundle'
        bundle.mkdir()
        archive = bundle / 'rootfs.tar.gz'
        archive.write_bytes(b'isolated rootfs fixture')
        calls = []
        with patch.dict(os.environ, {'PRIMIGENIUS_CHANNEL': 'stable'}), \
                patch.object(selection, 'read_registrations', side_effect=lambda: self.entries), \
                patch('wsl_registration.repair_registration', return_value=None):
            manager = PodmanManager(app_install_dir=str(self.install))
            manager.runtime.bundle = str(bundle)
            manager.runtime.validate = Mock(return_value={'files': {'rootfs.tar.gz': hashlib.sha256(archive.read_bytes()).hexdigest()}})
            manager.is_podman_available = Mock(return_value=True)
            manager.is_engine_ready = Mock(return_value=False)
            manager._ensure_wsl2_ready = Mock(return_value=(True, 'ready'))
            manager._is_wsl_distro_registered = Mock(side_effect=lambda: any(
                entry['name'] == manager.wsl_distro_name for entry in self.entries))
            def host(command, **kwargs):
                calls.append(command)
                self.assertEqual(command[1], '--import')
                target = Path(command[3])
                (target/'ext4.vhdx').write_bytes(b'new fake runtime')
                self.entries.append(dict(name=command[2], base=str(target), version=2))
                return subprocess.CompletedProcess(command, 0)
            manager._run_host_command = host
            manager.runtime.start = Mock(return_value=True)
            manager._start_wsl_podman_service = manager._start_wsl_podman_service_locked
            ok, detail = manager.auto_setup()
            self.assertTrue(ok, detail)
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.entries[0], old_entry)
            self.assertFalse(manager._runtime_provisioning_pending)
            with patch.object(PodmanManager, '_instance', None):
                restarted = PodmanManager(app_install_dir=str(self.install))
            self.assertEqual(restarted.machine_name, manager.machine_name)
            self.assertEqual(restarted.podman_data_dir, manager.podman_data_dir)
            self.assertFalse(restarted._runtime_provisioning_pending)

    def test_manager_adopts_old_root_and_persists_only_selection(self):
        self.save(requested_resources={'cpus':6})
        disk=self.disk(selection.LEGACY,layout=4)
        with patch.dict(os.environ,{'PRIMIGENIUS_CHANNEL':'stable'}), \
                patch.object(selection,'read_registrations',return_value=self.entries):
            manager=PodmanManager(app_install_dir=str(self.install))
        self.assertEqual(manager.wsl_distro_name,selection.LEGACY)
        saved=json.loads(self.config.read_text(encoding='utf-8'))
        self.assertEqual(saved['machine_name'],selection.LEGACY)
        self.assertEqual(saved['requested_resources'],{'cpus':6})
        self.assertFalse(saved['provisioning_pending'])
        self.assertEqual(saved['selection_history'][-1]['machine_name'],selection.STABLE)
        self.assertEqual(disk.read_bytes(),b'original images and containers')

    def test_manager_blocks_import_and_preserves_failed_selection_file(self):
        self.save()
        self.disk(selection.STABLE, content=b'')
        before=self.config.read_bytes()
        with patch.dict(os.environ,{'PRIMIGENIUS_CHANNEL':'stable'}), \
                patch.object(selection,'read_registrations',return_value=self.entries):
            manager=PodmanManager(app_install_dir=str(self.install))
            manager._run_host_command=Mock()
            ok,error=manager.machine_init()
        self.assertFalse(ok)
        self.assertTrue(manager._wsl_storage_missing)
        manager._run_host_command.assert_not_called()
        self.assertEqual(self.config.read_bytes(),before)
        self.assertTrue(self.data.exists())

    def test_background_api_reconnect_cannot_bypass_storage_failure(self):
        self.save()
        self.disk(selection.STABLE, content=b'')
        with patch.dict(os.environ,{'PRIMIGENIUS_CHANNEL':'stable'}), \
                patch.object(selection,'read_registrations',return_value=self.entries):
            manager=PodmanManager(app_install_dir=str(self.install))
        manager.runtime.start=Mock()
        self.assertFalse(manager._start_wsl_podman_service_locked(force=True))
        manager.runtime.start.assert_not_called()
        self.assertEqual(manager._connection_state,'attention')

    def test_retry_can_find_legacy_after_disk_is_restored_without_app_restart(self):
        self.save()
        disk = self.disk(selection.STABLE)
        real = selection._disk_status
        probe = lambda path: 'inaccessible' if path and path == disk else real(path)
        with patch.dict(os.environ,{'PRIMIGENIUS_CHANNEL':'stable'}), \
                patch.object(selection,'read_registrations',side_effect=lambda:self.entries), \
                patch.object(selection,'_disk_status',side_effect=probe):
            manager=PodmanManager(app_install_dir=str(self.install))
            self.assertTrue(manager._runtime_selection_error)
            self.entries.clear()  # the original selection is removed externally
            self.disk(selection.LEGACY,layout=4)
            with patch('wsl_registration.repair_registration',return_value=None):
                self.assertTrue(manager._prepare_wsl_registration())
        self.assertEqual(manager.wsl_distro_name,selection.LEGACY)
        self.assertFalse(manager._wsl_storage_missing)

    def test_registry_reader_skips_incomplete_entries_without_losing_later_names(self):
        from runtime.test_wsl_registration import Registry,LXSS
        registry=Registry()
        del registry.nodes[LXSS+r'\{owned}']['DistributionName']
        before=copy.deepcopy(registry.nodes)
        with patch.dict(sys.modules,{'winreg':registry}):
            result=selection.read_registrations()
        self.assertTrue(any(item['name']=='unrelated' for item in result))
        self.assertEqual(registry.nodes,before)
        self.assertEqual(registry.changes,[])


if __name__=='__main__': unittest.main()
