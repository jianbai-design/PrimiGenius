"""Security and workflow acceptance checks; all fixtures live in temporary directories."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import os
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT / 'marketplace/github'))
from plugin_archive import extract_plugin, inspect_archive, replace_plugin, package_upload, pack_source
import market_runner as market


def event(number, uid=10, kind='release', plugin_id='new', command=None, comment_id=100):
    user = {'id': uid, 'login': 'user' + str(uid), 'type': 'User'}
    result = {'issue': {'number': number, 'user': user, 'created_at': '2026-10-22T00:00:00Z',
                        'body': json.dumps({'schema': 1, 'kind': kind, 'plugin_id': plugin_id})}}
    if command:
        result['comment'] = {'id': number * 1000 + comment_id, 'user': user, 'created_at': '2026-10-22T01:00:00Z', 'body': '/market ' + command}
    return result


def release(number=1, uid=10, owner=10, version='1.0.0'):
    return {'kind': 'release', 'issue': number, 'author': {'id': uid, 'login': 'user' + str(uid)},
            'owner': {'id': owner, 'login': 'user' + str(owner)}, 'plugin_id': 'community_10_demo',
            'original_id': 'demo', 'version': version, 'sha256': hashlib.sha256(version.encode()).hexdigest(),
            'size': 100, 'status': 'pending_review' if uid == owner else 'awaiting_author',
            'metadata': {'id': 'community_10_demo', 'version': version}, 'files': {}, 'changes': [], 'source': {}, 'changelog': ''}


class Permissions(unittest.TestCase):
    def setUp(self):
        self.r = release()
        self.state, _ = market.apply(market.initial_state(), event(1), self.r)
        self.state['reviewers'] = [20, 30]

    def decide(self, uid, command, state=None, prepared=None):
        return market.apply(state or self.state, event(1, uid, command=command), prepared,
                            lambda r: {'version': r['version'], 'sha256': r['sha256']})

    def test_ordinary_user_cannot_publish(self):
        with self.assertRaisesRegex(ValueError, '审核员'):
            self.decide(99, 'approve ' + self.r['sha256'], prepared=self.r)

    def test_digest_is_bound_to_approval(self):
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            self.decide(20, 'approve ' + 'f' * 64, prepared=self.r)

    def test_reviewer_cannot_self_review(self):
        state = copy.deepcopy(self.state)
        state['reviewers'].append(10)
        with self.assertRaisesRegex(ValueError, '自己参与'):
            self.decide(10, 'approve ' + self.r['sha256'], state, self.r)

    def test_admin_has_highest_permission(self):
        state, _ = self.decide(market.ADMIN_ID, 'approve ' + self.r['sha256'], prepared=self.r)
        self.assertEqual(state['requests']['1']['status'], 'published')

    def test_collaboration_then_contribution_needs_two_reviews(self):
        request = event(2, 40, 'collaboration', 'community_10_demo')
        state, _ = market.apply(self.state, request)
        with self.assertRaisesRegex(ValueError, '只有作者'):
            market.apply(state, event(2, 99, command='collaborate-accept'))
        state, _ = market.apply(state, event(2, 10, command='collaborate-accept'))
        self.assertEqual(state['plugins']['community_10_demo']['collaborators'][0]['id'], 40)
        contribution = release(3, 40, 10, '1.1.0')
        state, _ = market.apply(state, event(3, 40, plugin_id='community_10_demo'), contribution)
        with self.assertRaisesRegex(ValueError, '尚未获作者接纳'):
            market.apply(state, event(3, 20, command='approve ' + contribution['sha256']), contribution, lambda _: {})
        state, _ = market.apply(state, event(3, 10, command='author-accept ' + contribution['sha256'], comment_id=101))
        self.assertEqual(state['requests']['3']['status'], 'pending_review')
        state, _ = market.apply(state, event(3, 20, command='approve ' + contribution['sha256'], comment_id=102), contribution,
                                lambda r: {'version': r['version']})
        self.assertEqual(state['plugins']['community_10_demo']['published']['version'], '1.1.0')

    def test_accepted_update_grants_shared_management(self):
        contribution = release(3, 40, 10, '1.1.0')
        state, _ = market.apply(self.state, event(3, 40, plugin_id='community_10_demo'), contribution)
        state, _ = market.apply(state, event(3, 10, command='author-accept ' + contribution['sha256']))
        self.assertEqual(state['plugins']['community_10_demo']['owner']['id'], 10)
        self.assertIn(40, [x['id'] for x in state['plugins']['community_10_demo']['collaborators']])

    def test_cannot_claim_other_authors_id(self):
        forged = release(3, 40, 10, '1.1.0')
        with self.assertRaisesRegex(ValueError, '冒用'):
            market.apply(market.initial_state(), event(3, 40), forged)

    def test_admin_only_role_changes_and_protection(self):
        management = event(5, market.ADMIN_ID, 'management')
        state, _ = market.apply(self.state, management)
        with self.assertRaisesRegex(ValueError, '平台管理员'):
            market.apply(state, event(5, 20, command='reviewer-add 99'))
        state, _ = market.apply(state, event(5, market.ADMIN_ID, command='reviewer-add 99'))
        self.assertIn(99, state['reviewers'])
        with self.assertRaises(ValueError):
            market.apply(state, event(5, market.ADMIN_ID, command='ban ' + str(market.ADMIN_ID), comment_id=200))

    def test_management_issue_is_atomic(self):
        management = event(6, market.ADMIN_ID, 'management')
        data = json.loads(management['issue']['body']); data['command'] = '/market reviewer-add 50'
        management['issue']['body'] = json.dumps(data)
        state, _ = market.apply(self.state, management)
        self.assertIn(50, state['reviewers'])

    def test_old_workflow_cannot_restore_revoked_permissions(self):
        management = event(5, market.ADMIN_ID, 'management')
        state, _ = market.apply(self.state, management)
        grant = event(5, market.ADMIN_ID, command='reviewer-add 50', comment_id=210)
        state, _ = market.apply(state, grant)
        state, _ = market.apply(state, event(5, market.ADMIN_ID, command='reviewer-remove 50', comment_id=211))
        state['audit'].extend({'event': 'historical:' + str(i)} for i in range(1001))
        state, _ = market.apply(state, event(6, market.ADMIN_ID, 'management'))
        state, _ = market.apply(state, grant)
        self.assertNotIn(50, state['reviewers'])

    def test_blocked_and_demoted_accounts_cannot_publish(self):
        state = copy.deepcopy(self.state); state['blocked'] = [20]
        with self.assertRaisesRegex(ValueError, '禁用'):
            self.decide(20, 'approve ' + self.r['sha256'], state, self.r)
        state['blocked'] = []; state['reviewers'].remove(20)
        with self.assertRaisesRegex(ValueError, '审核员'):
            self.decide(20, 'approve ' + self.r['sha256'], state, self.r)

    def test_cannot_downgrade_and_duplicate_is_idempotent(self):
        state, _ = self.decide(20, 'approve ' + self.r['sha256'], prepared=self.r)
        again, _ = self.decide(20, 'approve ' + self.r['sha256'], state, self.r)
        self.assertEqual(state, again)
        old = release(3, 10, 10, '0.9.0')
        with self.assertRaisesRegex(ValueError, '高于'):
            market.apply(state, event(3), old)

    def test_state_is_not_mutated_when_publication_fails(self):
        before = copy.deepcopy(self.state)
        def fail(_): raise RuntimeError('network interruption')
        with self.assertRaises(RuntimeError):
            market.apply(self.state, event(1, 20, command='approve ' + self.r['sha256']), self.r, fail)
        self.assertEqual(before, self.state)


class Archives(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, name='plugin.zip', config=None, extras=None):
        target = self.root / name
        with zipfile.ZipFile(target, 'w') as archive:
            archive.writestr('config.json', json.dumps(config or {'id': 'demo', 'version': '1.0.0', 'command_template': 'echo test'}))
            for file, content in (extras or {}).items():
                entry = zipfile.ZipInfo(file)
                entry.filename = file
                archive.writestr(entry, content)
        return target

    def test_reject_unsafe_and_conflicting_paths(self):
        for name in ('../escape', '/absolute', 'a\\escape', 'C:/escape', 'CON.txt', 'a./file', 'config.JSON'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                with zipfile.ZipFile(self.archive(extras={name: 'bad'})) as archive: inspect_archive(archive)
        self.assertFalse((self.root.parent / 'escape').exists())

    def test_upload_version_fills_missing_and_overrides_existing_without_changing_source(self):
        for old in (None, '0.1.0', 'invalid'):
            cfg = {'id': 'demo', 'name': {'zh': '示例'}, 'command_template': 'echo test'}
            if old is not None:
                cfg['version'] = old
            source = self.archive(config=cfg, extras={'bin/program': b'\x00\xffprogram'})
            before = source.read_bytes()
            output = self.root / 'upload.zip'
            result, _ = package_upload(source, output, '2.1.0-beta.1')
            self.assertEqual(result, dict(cfg, version='2.1.0-beta.1'))
            self.assertEqual(source.read_bytes(), before)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(inspect_archive(archive)[0]['version'], '2.1.0-beta.1')
                self.assertEqual(archive.read('bin/program'), b'\x00\xffprogram')
            market.version_key(result['version'])

    def test_upload_from_source_and_wrapped_zip_preserves_assets_and_modes(self):
        folder = self.root / 'source'; folder.mkdir()
        (folder / 'config.json').write_text('{"id":"demo"}', encoding='utf-8')
        (folder / 'run.sh').write_bytes(b'#!/bin/sh\necho test\n')
        packed = self.root / 'packed.zip'; pack_source(folder, packed)
        package_upload(packed, self.root / 'folder-upload.zip', '1.0.0')
        self.assertNotIn('version', json.loads((folder / 'config.json').read_text()))
        wrapped = self.root / 'wrapped.zip'
        with zipfile.ZipFile(wrapped, 'w') as archive:
            archive.writestr('plugin/config.json', '{"id":"demo","version":"0.1.0"}')
            script = zipfile.ZipInfo('plugin/run.sh'); script.external_attr = 0o100755 << 16
            archive.writestr(script, b'#!/bin/sh\necho test\n')
        package_upload(wrapped, self.root / 'wrapped-upload.zip', '1.2.3')
        with zipfile.ZipFile(self.root / 'wrapped-upload.zip') as archive:
            self.assertEqual(inspect_archive(archive)[0]['version'], '1.2.3')
            self.assertEqual(archive.getinfo('plugin/run.sh').external_attr >> 16, 0o100755)
            self.assertEqual(archive.read('plugin/run.sh'), (folder / 'run.sh').read_bytes())

    def test_upload_version_validation_and_archive_safety(self):
        source = self.archive()
        output = self.root / 'upload.zip'
        for version in (None, '', ' ', '1.0', 'v1.0.0', '01.0.0', '1.0.0-', '1.0.0-a..b', '1.0.0\n', 1):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, '版本号'):
                package_upload(source, output, version)
            self.assertFalse(output.exists())
        with self.assertRaisesRegex(ValueError, '覆盖'):
            package_upload(source, source, '1.0.0')
        with self.assertRaisesRegex(ValueError, '不安全'):
            package_upload(self.archive(extras={'../escape': 'bad'}), output, '1.0.0')

    def test_upload_http_route_requires_desktop_session_and_cleans_failed_output(self):
        import ast
        import secrets
        from flask import Flask, jsonify, request
        tree = ast.parse((ROOT / 'backend/app.py').read_text(encoding='utf-8-sig'))
        handler = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'package_plugin_source')
        app = Flask('upload-route-test')
        namespace = dict(app=app, os=os, secrets=secrets, request=request, jsonify=jsonify,
                         tempfile=tempfile, package_upload=package_upload, pack_source=pack_source)
        exec(compile(ast.Module(body=[handler], type_ignores=[]), '<real upload route>', 'exec'), namespace)
        source = self.archive(config={'id': 'demo', 'command_template': 'echo test'})
        client = app.test_client()
        original_mkstemp = tempfile.mkstemp
        def scratch_file(**kwargs): return original_mkstemp(dir=self.root, **kwargs)
        with patch.dict(os.environ, PRIMIGENIUS_DESKTOP_TOKEN='test-secret'), patch.object(tempfile, 'mkstemp', scratch_file):
            self.assertEqual(client.post('/prepare-plugin-upload', json={'zipPath': str(source), 'version': '1.0.0'}).status_code, 403)
            headers = {'X-PrimiGenius-Desktop': 'test-secret'}
            bad = client.post('/prepare-plugin-upload', headers=headers, json={'zipPath': str(source)})
            self.assertEqual(bad.status_code, 400)
            self.assertFalse(list(self.root.glob('primigenius-market-*')))
            good = client.post('/prepare-plugin-upload', headers=headers, json={'zipPath': str(source), 'version': '1.2.3'})
            self.assertEqual(good.status_code, 200)
            with zipfile.ZipFile(good.get_json()['zipPath']) as archive:
                self.assertEqual(inspect_archive(archive)[0]['version'], '1.2.3')

    def test_reject_symlink(self):
        target = self.archive()
        with zipfile.ZipFile(target, 'a') as archive:
            entry = zipfile.ZipInfo('link'); entry.external_attr = 0o120777 << 16; archive.writestr(entry, '../outside')
        with self.assertRaisesRegex(ValueError, '符号链接'):
            with zipfile.ZipFile(target) as archive: inspect_archive(archive)

    def test_successful_upgrade_and_failed_upgrade_preserve_old(self):
        destination = self.root / 'installed'
        first = self.archive('first.zip'); replace_plugin(first, destination)
        bad = self.archive('bad.zip', extras={'../escape': 'bad'})
        with self.assertRaises(ValueError): replace_plugin(bad, destination)
        self.assertEqual(json.loads((destination / 'config.json').read_text())['version'], '1.0.0')
        replacement = self.archive('replacement.zip', {'id': 'demo', 'version': '1.1.0'})
        replace_plugin(replacement, destination)
        self.assertEqual(json.loads((destination / 'config.json').read_text())['version'], '1.1.0')
        self.assertFalse(list(self.root.glob('.plugin-install-*')))

    def test_reject_cross_source_overwrite(self):
        destination = self.root / 'installed'; replace_plugin(self.archive(), destination)
        other = self.archive('other.zip', {'id': 'demo', 'version': '1.1.0', 'marketplace': {'source': 'community', 'owner_github_id': 40}})
        with self.assertRaisesRegex(ValueError, '不同来源'):
            replace_plugin(other, destination)

    def test_failed_swap_restores_previous_version(self):
        destination = self.root / 'installed'; replace_plugin(self.archive(), destination)
        upgrade = self.archive('upgrade.zip', {'id': 'demo', 'version': '2.0.0'})
        original = os.replace
        def fail_new(source, target):
            if Path(source).name == 'new':
                raise PermissionError('simulated locked file')
            return original(source, target)
        with patch('plugin_archive.os.replace', side_effect=fail_new), self.assertRaises(PermissionError):
            replace_plugin(upgrade, destination)
        self.assertEqual(json.loads((destination / 'config.json').read_text())['version'], '1.0.0')
        self.assertFalse(list(self.root.glob('.plugin-install-*')))

    def test_failed_rollback_retains_backup(self):
        destination = self.root / 'installed'; replace_plugin(self.archive(), destination)
        upgrade = self.archive('upgrade.zip', {'id': 'demo', 'version': '2.0.0'})
        original = os.replace
        def fail_restore(source, target):
            if Path(source).name in ('new', 'previous'):
                raise PermissionError('simulated filesystem failure')
            return original(source, target)
        with patch('plugin_archive.os.replace', side_effect=fail_restore), self.assertRaisesRegex(RuntimeError, '保留'):
            replace_plugin(upgrade, destination)
        backups = list(self.root.glob('.plugin-install-*/previous/config.json'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text())['version'], '1.0.0')

    def test_prepare_is_deterministic_and_rewrites_only_identity(self):
        source = self.archive(extras={'scripts/run.sh': '#!/bin/sh\necho test\n'})
        e = event(1)
        data = json.loads(e['issue']['body']); data['source'] = {'sha256': 'a' * 64}; e['issue']['body'] = json.dumps(data)
        class FakeAPI: repo = 'jianbai-design/PrimiGenius-community-plugins'
        def copy_source(api, descriptor, actor, target): target.write_bytes(source.read_bytes())
        first, second = self.root / 'a', self.root / 'b'; first.mkdir(); second.mkdir()
        with patch.object(market, 'download_source', copy_source):
            one = market.prepare_release(FakeAPI(), market.initial_state(), e, first)
            two = market.prepare_release(FakeAPI(), market.initial_state(), e, second)
        self.assertEqual(one['sha256'], two['sha256'])
        self.assertEqual(one['metadata']['id'], 'community_10_demo')
        self.assertEqual(one['metadata']['developer_signature']['developer'], 'user10')
        self.assertEqual(one['metadata']['command_template'], 'echo test')

    def test_malformed_display_metadata_is_rejected(self):
        source = self.archive(config={'id': 'demo', 'version': '1.0.0', 'command_template': 'echo test',
                                      'name': {'zh': {'toString': 'invalid'}}})
        e = event(1)
        data = json.loads(e['issue']['body']); data['source'] = {}; e['issue']['body'] = json.dumps(data)
        class FakeAPI: repo = 'jianbai-design/PrimiGenius-community-plugins'
        def copy_source(api, descriptor, actor, target): target.write_bytes(source.read_bytes())
        output = self.root / 'output'; output.mkdir()
        with patch.object(market, 'download_source', copy_source), self.assertRaisesRegex(ValueError, '名称和描述'):
            market.prepare_release(FakeAPI(), market.initial_state(), e, output)


if __name__ == '__main__':
    unittest.main()
