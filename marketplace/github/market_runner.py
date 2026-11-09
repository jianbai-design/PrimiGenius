"""Trusted GitHub Actions controller. Never execute code from submitted plugins."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

try:
    from plugin_archive import extract_plugin, valid_plugin_id
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
    from plugin_archive import extract_plugin, valid_plugin_id

ADMIN_ID = 134767426
MAX_SIZE = 256 * 1024 * 1024
VERSION = re.compile(r'^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*))?$')


def initial_state():
    return {'schema': 1, 'administrator_id': ADMIN_ID, 'reviewers': [], 'blocked': [],
            'plugins': {}, 'requests': {}, 'audit': []}


def version_key(value):
    m = VERSION.fullmatch(value)
    if not m:
        raise ValueError('版本号须为语义版本，例如 1.0.0')
    prerelease = m.group(4)
    labels = tuple((0, int(x)) if x.isdigit() else (1, x) for x in prerelease.split('.')) if prerelease else ()
    return tuple(map(int, m.groups()[:3])) + (0 if prerelease else 1, labels)


class API:
    def __init__(self, repository=None):
        self.repo = repository or os.environ['GITHUB_REPOSITORY']
        self.token = os.environ.get('GITHUB_TOKEN', '')

    def request(self, route, method='GET', data=None):
        url = 'https://api.github.com' + route if route.startswith('/') else route
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname not in ('api.github.com', 'uploads.github.com'):
            raise ValueError('Unexpected GitHub API endpoint')
        headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'PrimiGenius-Market'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False).encode()
            headers['Content-Type'] = 'application/json'
        elif data is not None:
            headers['Content-Type'] = 'application/octet-stream'
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=180) as response:
            raw = response.read(16 * 1024 * 1024)
            return json.loads(raw) if raw else {}

    def repo_api(self, route, method='GET', data=None):
        return self.request('/repos/' + self.repo + route, method, data)

    def state(self):
        result = self.repo_api('/contents/market-state.json?ref=main')
        blob = self.repo_api('/git/blobs/' + result['sha']) if result.get('encoding') == 'none' else result
        return json.loads(base64.b64decode(blob['content'])), result['sha']

    def save(self, state, sha):
        return self.repo_api('/contents/market-state.json', 'PUT', {
            'message': 'Apply market review and collaboration decisions', 'sha': sha,
            'content': base64.b64encode(json.dumps(state, ensure_ascii=False, indent=2).encode()).decode(), 'branch': 'main'})

    def comment(self, number, message):
        return self.repo_api('/issues/' + str(number) + '/comments', 'POST', {'body': message})


def issue_data(event):
    issue = event['issue']
    if issue.get('pull_request'):
        raise ValueError('市场使用 Issues 提交，请勿提交可执行工作流或 PR')
    raw = issue.get('body') or ''
    if len(raw) > 16000:
        raise ValueError('提交描述过长')
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get('schema') != 1 or data.get('kind') not in ('release', 'collaboration', 'management'):
        raise ValueError('不是有效的市场提交')
    return data


def actor_from(event):
    actor = event.get('comment', event['issue'])['user']
    if actor.get('type') != 'User' or not isinstance(actor.get('id'), int):
        raise ValueError('操作必须由 GitHub 用户提交')
    return {'id': actor['id'], 'login': actor['login']}


def allowed(state, actor):
    if actor['id'] in state['blocked'] and actor['id'] != ADMIN_ID:
        raise ValueError('此账号已被平台禁用')


def owns(plugin, actor):
    return actor['id'] in (ADMIN_ID, plugin['owner']['id'])


def platform(state, actor, plugin=None):
    if actor['id'] == ADMIN_ID:
        return
    if actor['id'] not in state['reviewers']:
        raise ValueError('需要平台管理员或审核员权限')
    if plugin and actor['id'] in [plugin['owner']['id']] + [x['id'] for x in plugin['collaborators']]:
        raise ValueError('审核员不能审核自己参与的插件')


def download_source(api, source, actor, destination):
    repo = source.get('repository', '')
    if not re.fullmatch(r'[A-Za-z0-9-]+/PrimiGenius-market-uploads', repo):
        raise ValueError('安装包必须来自提交者自己的市场上传仓库')
    repository = api.request('/repos/' + repo)
    if repository['owner']['id'] != actor['id'] or repository.get('private'):
        raise ValueError('上传仓库必须公开且归属于提交者')
    release_id, asset_id = source.get('release_id'), source.get('asset_id')
    if not isinstance(release_id, int) or not isinstance(asset_id, int):
        raise ValueError('Release 或附件 ID 不合法')
    release = api.request('/repos/' + repo + '/releases/' + str(release_id))
    asset = next((x for x in release['assets'] if x['id'] == asset_id), None)
    if not asset or not 0 < asset['size'] <= MAX_SIZE or not re.fullmatch('[a-f0-9]{64}', str(source.get('sha256', ''))):
        raise ValueError('上传附件不存在或大小/摘要不合法')
    expected_url = 'https://github.com/' + repo + '/releases/download/'
    if not asset['browser_download_url'].startswith(expected_url):
        raise ValueError('附件地址不合法')
    # Public download only. No API token may be forwarded to redirects or mirrors.
    class SafeRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            parsed = urllib.parse.urlsplit(newurl)
            if parsed.scheme != 'https' or parsed.hostname not in ('github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com', 'github-releases.githubusercontent.com'):
                raise ValueError('Unexpected release download redirect')
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    req = urllib.request.Request(asset['browser_download_url'], headers={'User-Agent': 'PrimiGenius-Market'})
    digest, total = hashlib.sha256(), 0
    with urllib.request.build_opener(SafeRedirect()).open(req, timeout=180) as response, destination.open('wb') as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_SIZE or total > asset['size']:
                raise ValueError('安装包超过大小限制')
            digest.update(chunk)
            output.write(chunk)
    if total != asset['size'] or digest.hexdigest() != source['sha256']:
        raise ValueError('提交文件摘要不匹配，不能继续审核')


def prepare_release(api, state, event, output, existing=None):
    data = issue_data(event) if existing is None else existing
    author = actor_from(event) if existing is None else existing['author']
    allowed(state, author)
    source = data['source']
    with tempfile.TemporaryDirectory() as temp:
        source_zip = Path(temp, 'source.zip')
        download_source(api, source, author, source_zip)
        extracted = Path(temp, 'plugin')
        extracted.mkdir()
        cfg = extract_plugin(source_zip, extracted)
        version_key(str(cfg.get('version', '')))
        if not cfg.get('command_template') and not cfg.get('script'):
            raise ValueError('插件必须提供 command_template 或 script')
        for field in ('command_template', 'script', 'type'):
            if field in cfg and not isinstance(cfg[field], str):
                raise ValueError('插件字段必须是文本: ' + field)
        for field in ('name', 'description'):
            value = cfg.get(field, '')
            if not isinstance(value, (str, dict)) or isinstance(value, dict) and any(not isinstance(v, str) for v in value.values()):
                raise ValueError('插件名称和描述必须是文本或中英文字段')
        minimum = str(cfg.get('min_app_version', '0.0.0'))
        if not re.fullmatch(r'\d+\.\d+\.\d+', minimum):
            raise ValueError('min_app_version 格式不合法')
        original = cfg['id']
        plugin_id = data.get('plugin_id', 'new')
        if plugin_id == 'new':
            plugin_id = 'community_' + str(author['id']) + '_' + original[:90]
            if plugin_id in state['plugins']:
                raise ValueError('插件已存在，请从原插件管理区提交更新')
            owner = author
        else:
            p = state['plugins'].get(plugin_id)
            if not p or original not in (p['original_id'], plugin_id):
                raise ValueError('更新与原插件 ID 不一致')
            owner = p['owner']
        if not valid_plugin_id(plugin_id):
            raise ValueError('市场插件 ID 不合法')
        if existing is not None:
            # Frozen author identity is part of the approved package, including username spelling.
            owner = existing['owner']
        cfg['id'] = plugin_id
        cfg['developer_signature'] = dict(cfg.get('developer_signature') or {}, developer=owner['login'])
        cfg['marketplace'] = {'source': 'github:' + api.repo, 'owner_github_id': owner['id'],
                              'owner_login': owner['login'], 'submitted_by': author}
        Path(extracted, 'config.json').write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
        files = {}
        package = output / 'package.zip'
        with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(extracted.rglob('*')):
                if not file.is_file():
                    continue
                name = file.relative_to(extracted).as_posix()
                raw = file.read_bytes()
                if re.search(r'(^|/)(\.env|id_rsa|id_ed25519)$', name) or re.search(r'private.*\.(pem|key)$', name, re.I):
                    raise ValueError('安装包包含疑似凭据文件')
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o100755 if file.stat().st_mode & 0o111 else 0o100644) << 16
                archive.writestr(info, raw)
                files[name] = {'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw)}
        size = package.stat().st_size
        if not 0 < size <= MAX_SIZE:
            raise ValueError('标准安装包超过 256 MiB 限制')
        with package.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        old_files = state['plugins'].get(plugin_id, {}).get('files', {})
        changes = [{'path': name, 'change': 'added' if name not in old_files else 'removed' if name not in files else 'changed'}
                   for name in sorted(set(files) | set(old_files)) if files.get(name) != old_files.get(name)]
        record = {'kind': 'release', 'issue': event['issue']['number'], 'author': author, 'owner': owner,
                  'plugin_id': plugin_id, 'original_id': data.get('original_id', original), 'version': cfg['version'],
                  'source': source, 'metadata': cfg, 'sha256': digest, 'size': size, 'files': files, 'changes': changes,
                  'changelog': str(data.get('changelog', ''))[:8000],
                  'status': 'pending_review' if author['id'] == owner['id'] else 'awaiting_author'}
        if existing is not None and digest != existing['sha256']:
            raise ValueError('重新打包结果与已审核摘要不一致，请重新提交')
        return record


def parse_command(event):
    command = (event.get('comment', {}).get('body') or '').strip()
    if not command.startswith('/market '):
        return None, []
    parts = command.split()
    return parts[1], parts[2:]


def apply(state, event, prepared=None, publish=None):
    """Authorization is repeated against the latest trusted state before every write."""
    state = copy.deepcopy(state)
    if state.get('administrator_id') != ADMIN_ID:
        raise ValueError('平台管理员身份配置不一致')
    actor = actor_from(event)
    allowed(state, actor)
    number = str(event['issue']['number'])
    event_id = ('comment:' + str(event['comment']['id'])) if 'comment' in event else 'issue:' + number
    if any(x['event'] == event_id for x in state['audit']):
        return state, '此操作已处理'
    command, args = parse_command(event)
    if 'comment' not in event:
        data = issue_data(event)
        if number in state['requests']:
            return state, '此提交已登记'
        if data['kind'] == 'release':
            if not prepared or prepared['author'] != actor:
                raise ValueError('缺少有效的安装包检查结果')
            r = copy.deepcopy(prepared)
            p = state['plugins'].get(r['plugin_id'])
            if not p:
                if r['owner']['id'] != actor['id'] or r['plugin_id'] != 'community_' + str(actor['id']) + '_' + r['original_id'][:90]:
                    raise ValueError('不能冒用其他作者身份')
                p = {'id': r['plugin_id'], 'original_id': r['original_id'], 'owner': actor,
                     'collaborators': [], 'metadata': r['metadata'], 'published': None, 'files': {}, 'listed': True}
                state['plugins'][p['id']] = p
            if r['owner']['id'] != p['owner']['id']:
                raise ValueError('插件归属已变化')
            r['status'] = 'pending_review' if actor['id'] == p['owner']['id'] else 'awaiting_author'
            if p['published'] and version_key(r['version']) <= version_key(p['published']['version']):
                raise ValueError('更新版本必须高于已发布版本')
            if any(x.get('plugin_id') == r['plugin_id'] and x.get('version') == r['version'] and x.get('status') not in ('rejected', 'withdrawn') for x in state['requests'].values()):
                raise ValueError('此插件版本已有提交，请使用新版本号')
            state['requests'][number] = r
            message = '安装包已检查，等待作者接纳。' if r['status'] == 'awaiting_author' else '安装包已检查，等待平台审核。'
            message += '\n\nSHA256: `' + r['sha256'] + '`\n\n' + json.dumps(r['changes'], ensure_ascii=False)[:24000]
        elif data['kind'] == 'collaboration':
            p = state['plugins'].get(data.get('plugin_id'))
            if not p or owns(p, actor) or any(x['id'] == actor['id'] for x in p['collaborators']):
                raise ValueError('插件不存在或已属于此账号')
            state['requests'][number] = {'kind': 'collaboration', 'plugin_id': p['id'], 'author': actor,
                                         'message': str(data.get('message', ''))[:2000], 'status': 'awaiting_author'}
            message = '合作申请已登记，等待插件作者处理。'
        else:
            state['requests'][number] = {'kind': 'management', 'author': actor, 'status': 'open'}
            message = '管理操作入口已登记。操作权限由 GitHub Actions 验证。'
            if data.get('command'):
                nested = copy.deepcopy(event)
                nested['comment'] = {'id': 'issue-' + number, 'user': event['issue']['user'],
                                     'body': data['command'], 'created_at': event['issue']['created_at']}
                state, message = apply(state, nested, prepared, publish)
    elif command:
        r = state['requests'].get(number)
        if not r:
            raise ValueError('该提交尚未登记，请等待初始检查完成后重新操作')
        p = state['plugins'].get(r.get('plugin_id'))
        if command in ('author-accept', 'author-reject', 'approve', 'reject', 'withdraw'):
            if r['kind'] != 'release' or not args or args[0] != r['sha256']:
                raise ValueError('必须审核当前安装包的完整 SHA256')
            if command.startswith('author-'):
                if not owns(p, actor) or r['status'] != 'awaiting_author':
                    raise ValueError('只有作者可处理待接纳贡献')
                r['status'] = 'pending_review' if command == 'author-accept' else 'rejected'
                if command == 'author-accept' and r['author']['id'] != p['owner']['id'] and not any(x['id'] == r['author']['id'] for x in p['collaborators']):
                    p['collaborators'].append(r['author'])
            elif command == 'withdraw':
                if actor['id'] not in (ADMIN_ID, r['author']['id'], p['owner']['id']) or r['status'] not in ('awaiting_author', 'pending_review'):
                    raise ValueError('不能撤回此版本')
                r['status'] = 'withdrawn'
            else:
                platform(state, actor, p)
                if actor['id'] == r['author']['id'] and actor['id'] != ADMIN_ID:
                    raise ValueError('审核员不能审核自己的提交')
                if r['status'] != 'pending_review':
                    raise ValueError('该版本尚未获作者接纳或已处理')
                if command == 'approve':
                    if not p['listed'] or p['owner']['id'] in state['blocked'] or r['author']['id'] in state['blocked']:
                        raise ValueError('插件已下架或相关账号已被禁用')
                    if p['published'] and version_key(r['version']) <= version_key(p['published']['version']):
                        raise ValueError('此版本不能覆盖更新的已发布版本')
                    if not prepared or prepared['sha256'] != r['sha256'] or publish is None:
                        raise ValueError('待发布文件未通过重新验证')
                    p['published'] = publish(r)
                    p['metadata'], p['files'], r['status'] = r['metadata'], r['files'], 'published'
                else:
                    if len(args) < 2:
                        raise ValueError('退回时必须填写原因')
                    r['status'] = 'rejected'
            r['reason'] = ' '.join(args[1:])[:2000]
            message = '版本状态已更新：' + r['status']
        elif command in ('collaborate-accept', 'collaborate-reject'):
            if r['kind'] != 'collaboration' or r['status'] != 'awaiting_author' or not owns(p, actor):
                raise ValueError('只有作者可以处理待审核合作申请')
            r['status'] = 'accepted' if command == 'collaborate-accept' else 'rejected'
            if r['status'] == 'accepted' and not any(x['id'] == r['author']['id'] for x in p['collaborators']):
                p['collaborators'].append(r['author'])
            message = '合作申请已处理：' + r['status']
        elif command in ('reviewer-add', 'reviewer-remove', 'ban', 'unban'):
            if actor['id'] != ADMIN_ID or len(args) != 1 or not args[0].isdigit() or int(args[0]) == ADMIN_ID:
                raise ValueError('只有平台管理员可以管理其他账号权限')
            target = int(args[0])
            collection = state['reviewers'] if command.startswith('reviewer-') else state['blocked']
            if command in ('reviewer-add', 'ban'):
                if target not in collection:
                    collection.append(target)
            elif target in collection:
                collection.remove(target)
            message = '账号权限已更新：' + command + ' ' + str(target)
        elif command in ('unlist', 'restore', 'collaborator-remove'):
            if not args or args[0] not in state['plugins']:
                raise ValueError('插件不存在')
            target = state['plugins'][args[0]]
            if command == 'unlist':
                if not owns(target, actor):
                    platform(state, actor)
                target['listed'] = False
            elif command == 'restore':
                if actor['id'] != ADMIN_ID:
                    raise ValueError('恢复上架需要平台管理员权限')
                target['listed'] = True
            else:
                if not owns(target, actor) or len(args) != 2 or not args[1].isdigit():
                    raise ValueError('只有作者可以移除合作者')
                target['collaborators'] = [x for x in target['collaborators'] if x['id'] != int(args[1])]
            message = '插件管理操作已完成：' + command
        else:
            raise ValueError('不支持的市场命令')
    else:
        return state, ''
    state['audit'].append({'event': event_id, 'actor': actor, 'issue': int(number),
                           'action': command or 'submit', 'at': event.get('comment', event['issue'])['created_at']})
    # Retain event IDs durably: rerunning an old workflow must not restore revoked permissions.
    # ponytail: one JSON index; shard requests/audit when the client response approaches 16 MiB.
    return state, message


def publish(api, record, directory):
    package = directory / 'package.zip'
    with package.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != record['sha256']:
            raise ValueError('发布文件与审核摘要不一致')
    tag = record['plugin_id'] + '-v' + record['version']
    name = record['plugin_id'] + '-' + record['version'] + '.zip'
    manifest = {'id': record['plugin_id'], 'version': record['version'], 'sha256': record['sha256'],
                'size': record['size'], 'owner_github_id': record['owner']['id'], 'source': 'github:' + api.repo,
                'tag': tag, 'asset': name, 'min_app_version': record['metadata'].get('min_app_version', '0.0.0')}
    raw = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
    signed = subprocess.run(['node', str(Path(__file__).with_name('sign.js'))], input=raw,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout.decode().strip()
    envelope = {'manifest': base64.b64encode(raw).decode(), 'signature': signed}
    try:
        remote = api.repo_api('/releases/tags/' + urllib.parse.quote(tag, safe=''))
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        remote = api.repo_api('/releases', 'POST', {'tag_name': tag, 'name': tag, 'draft': True,
                             'body': '作者: @' + record['owner']['login'] + '\n\n' + record['changelog'], 'make_latest': 'false'})
    manifest_bytes = json.dumps(envelope).encode()
    for filename, raw_file in ((name, package.read_bytes()), (name + '.manifest.json', manifest_bytes)):
        digest = 'sha256:' + hashlib.sha256(raw_file).hexdigest()
        assets = api.repo_api('/releases/' + str(remote['id']) + '/assets?per_page=100')
        old = next((x for x in assets if x['name'] == filename), None)
        if old and old.get('digest') == digest:
            continue
        if old:
            if not remote.get('draft'):
                raise ValueError('已发布附件摘要不一致，禁止覆盖')
            api.repo_api('/releases/assets/' + str(old['id']), 'DELETE')
        uploaded = api.request('https://uploads.github.com/repos/' + api.repo + '/releases/' + str(remote['id']) +
                               '/assets?name=' + urllib.parse.quote(filename, safe=''), 'POST', raw_file)
        if uploaded.get('digest') and uploaded['digest'] != digest:
            raise ValueError('GitHub 上传摘要不一致')
    if remote.get('draft'):
        api.repo_api('/releases/' + str(remote['id']), 'PATCH', {'draft': False, 'make_latest': 'false'})
    return dict(manifest, **envelope, url='https://github.com/' + api.repo + '/releases/tag/' + tag)


def main():
    phase = sys.argv[1]
    api = API()
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
    directory = Path('market-prepared')
    directory.mkdir(exist_ok=True)
    if phase == 'prepare':
        state, _ = api.state()
        prepared = None
        actor = actor_from(event)
        allowed(state, actor)
        command, _ = parse_command(event)
        if 'comment' not in event and issue_data(event)['kind'] == 'release':
            prepared = prepare_release(api, state, event, directory)
        elif command == 'approve':
            r = state['requests'].get(str(event['issue']['number']))
            if not r:
                raise ValueError('提交尚未登记')
            platform(state, actor, state['plugins'][r['plugin_id']])
            # Verify permission, state and approval hash before downloading any untrusted bytes.
            apply(state, event, {'sha256': r['sha256']}, lambda _: {'version': r['version']})
            prepared = prepare_release(api, state, event, directory, r)
        Path(directory, 'record.json').write_text(json.dumps(prepared, ensure_ascii=False), encoding='utf-8')
        # Every non-publication decision is also preflighted with the read-only token.
        if command != 'approve':
            apply(state, event, prepared)
    elif phase == 'commit':
        prepared = json.loads(Path(directory, 'record.json').read_text(encoding='utf-8'))
        for attempt in range(5):
            state, sha = api.state()
            updated, message = apply(state, event, prepared, lambda r: publish(api, r, directory))
            if not message:
                return
            try:
                api.save(updated, sha)
                api.comment(event['issue']['number'], message)
                break
            except urllib.error.HTTPError as exc:
                if exc.code not in (409, 422) or attempt == 4:
                    raise
                time.sleep(attempt + 1)
    elif phase == 'failure':
        api.comment(event['issue']['number'], '市场操作未完成，已保留原发布状态。请查看本次检查或发布日志：\n' +
                    'https://github.com/' + api.repo + '/actions/runs/' + os.environ['GITHUB_RUN_ID'])
    else:
        raise ValueError('Unknown phase')


if __name__ == '__main__':
    main()
