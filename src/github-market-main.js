'use strict';
const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');
const http = require('http');
const https = require('https');
const { isPublicGithubProxyUrl } = require('./github-public');

const REPOSITORY = 'jianbai-design/PrimiGenius-community-plugins';
const ADMIN_ID = 134767426;
const MAX_SIZE = 256 * 1024 * 1024;

function requestJSON(url, { method = 'GET', data, token = '', file, timeout = 30000, desktopToken = '', net, signal } = {}) {
    return new Promise((resolve, reject) => {
        const target = new URL(url);
        const isLocal = target.protocol === 'http:' && target.hostname === '127.0.0.1';
        const publicCatalog = target.origin === 'https://raw.githubusercontent.com' &&
            target.pathname === `/${REPOSITORY}/main/market-state.json` && method === 'GET' && !file && data === undefined;
        const publicProxy = isPublicGithubProxyUrl(target.href) && method === 'GET' && !token && !desktopToken && !file && data === undefined;
        if (target.username || target.password || (token && publicCatalog) ||
            (!isLocal && !publicCatalog && !publicProxy && (target.protocol !== 'https:' || !['api.github.com', 'uploads.github.com', 'github.com'].includes(target.hostname)))) {
            reject(new Error('不支持的 GitHub 请求地址')); return;
        }
        const headers = { Accept: 'application/json', 'User-Agent': 'PrimiGenius-Market' };
        if (method === 'GET') { headers['Cache-Control'] = 'no-cache'; headers.Pragma = 'no-cache'; }
        if (token && !isLocal) headers.Authorization = `Bearer ${token}`;
        if (isLocal && desktopToken) headers['X-PrimiGenius-Desktop'] = desktopToken;
        let bytes;
        if (file) {
            headers['Content-Type'] = 'application/zip';
            headers['Content-Length'] = fs.statSync(file).size;
        } else if (data !== undefined) {
            bytes = Buffer.from(JSON.stringify(data));
            headers['Content-Type'] = 'application/json';
            headers['Content-Length'] = bytes.length;
        }
        let req, stream, deadline, finished = false;
        function cleanup() {
            clearTimeout(deadline);
            if (signal) signal.removeEventListener('abort', abort);
        }
        function abort() { fail(Object.assign(new Error('GitHub read cancelled'), { name: 'AbortError' })); }
        function fail(error) {
            if (finished) return;
            finished = true;
            cleanup();
            if (stream) stream.destroy();
            if (req) { if (typeof req.abort === 'function') req.abort(); else req.destroy(); }
            if (!isLocal && /net::ERR_INVALID_ARGUMENT/.test(error.message || '')) {
                const invalid = new Error('GitHub 请求参数不合法，请更新客户端后重试');
                invalid.code = 'GITHUB_REQUEST_INVALID'; reject(invalid); return;
            }
            if (/ETIMEDOUT|ECONNRESET|ECONNREFUSED|ENOTFOUND|EAI_AGAIN|net::ERR_/.test(error.message || '')) {
                const friendly = new Error(isLocal ? '本地打包服务连接失败，请确认软件后端已启动后重试' :
                    `GitHub 连接失败（${target.hostname}），请检查网络或系统代理后重试`);
                friendly.code = isLocal ? 'LOCAL_BACKEND_UNAVAILABLE' : error.code || 'GITHUB_NETWORK_ERROR';
                reject(friendly);
            } else reject(error);
        }
        const response = res => {
            const chunks = [];
            let size = 0;
            res.on('data', chunk => {
                if (finished) return;
                size += chunk.length;
                if (size > 16 * 1024 * 1024) fail(new Error('GitHub 响应过大'));
                else chunks.push(chunk);
            });
            res.on('error', fail);
            res.on('aborted', () => fail(new Error('GitHub 响应中断，请重试')));
            res.on('end', () => {
                if (finished) return;
                let result;
                try { result = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}'); }
                catch (_) { fail(new Error(`GitHub 返回无效响应（HTTP ${res.statusCode}）`)); return; }
                if (res.statusCode < 200 || res.statusCode >= 300) {
                    const error = new Error(result.error || result.message || `GitHub HTTP ${res.statusCode}`);
                    error.status = res.statusCode;
                    fail(error);
                } else { finished = true; cleanup(); resolve(result); }
            });
        };
        if (signal && signal.aborted) { abort(); return; }
        if (signal) signal.addEventListener('abort', abort, { once: true });
        if (!isLocal && net) {
            // Chromium follows system proxy/PAC settings; reject redirects before sending credentials elsewhere.
            req = net.request({ url: target.href, method, redirect: 'error', useSessionCookies: false });
            // Chromium calculates Content-Length after end(); setting it manually causes ERR_INVALID_ARGUMENT.
            for (const [name, value] of Object.entries(headers)) if (name.toLowerCase() !== 'content-length') req.setHeader(name, String(value));
            req.on('response', response);
        } else req = (isLocal ? http : https).request(target, { method, headers }, response);
        // Socket-only timeouts do not bound DNS/TCP connection establishment.
        deadline = setTimeout(() => fail(new Error(isLocal ? '本地打包服务响应超时，请稍后重试' : 'GitHub 连接超时，请检查网络或系统代理后重试')), timeout);
        req.on('error', fail);
        if (file) {
            stream = fs.createReadStream(file);
            stream.on('error', fail);
            req.on('close', () => stream.destroy());
            stream.pipe(req);
        } else { if (bytes) req.write(bytes); req.end(); }
    });
}

async function sha256(file) {
    const hash = crypto.createHash('sha256');
    for await (const chunk of fs.createReadStream(file)) hash.update(chunk);
    return hash.digest('hex');
}

function verifyManifest(published, publicKey, id) {
    if (!published || typeof published.manifest !== 'string' || published.manifest.length > 16384 ||
        typeof published.signature !== 'string' || published.signature.length > 4096) throw new Error('缺少市场签名');
    const key = crypto.createPublicKey(publicKey);
    const raw = Buffer.from(published.manifest, 'base64');
    if (key.asymmetricKeyType !== 'rsa' || key.asymmetricKeyDetails.modulusLength < 3072 ||
        !crypto.verify('RSA-SHA256', raw, key, Buffer.from(published.signature, 'base64'))) throw new Error('市场签名验证失败，已阻止安装');
    const info = JSON.parse(raw.toString('utf8'));
    if (info.id !== id || !/^community_\d+_[A-Za-z0-9_.-]+$/.test(id) || info.source !== `github:${REPOSITORY}` ||
        !/^[a-f0-9]{64}$/.test(info.sha256) || !Number.isSafeInteger(info.owner_github_id) ||
        !id.startsWith(`community_${info.owner_github_id}_`) || !Number.isSafeInteger(info.size) || info.size <= 0 || info.size > MAX_SIZE ||
        !/^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$/.test(info.version) ||
        info.tag !== `${id}-v${info.version}` || info.asset !== `${id}-${info.version}.zip`) throw new Error('市场安装清单不合法');
    return info;
}

function submissionRecord(selection, account, owner, pluginId, changelog, issue) {
    return { schema: 1, plugin_id: pluginId, original_id: selection.config.id,
        version: selection.version, metadata: selection.config,
        owner: { id: owner.id, login: owner.login }, submitted_by: { id: account.id, login: account.login },
        changelog, sha256: selection.sha256, size: selection.size,
        source: selection.source, tag: selection.release.tag_name,
        review: { issue: issue.number, url: issue.html_url },
        submitted_at: selection.release.created_at || new Date().toISOString() };
}

function submissionText(value) {
    if (value && typeof value === 'object') return [value.zh, value.en].filter(v => typeof v === 'string').join(' / ');
    return typeof value === 'string' ? value : '';
}

function submissionSummary(record) {
    const plain = value => submissionText(value).replace(/[\r\n]+/g, ' ').slice(0, 500);
    const escape = value => plain(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/[\\`*_{}\[\]()#+.!|~]/g, '\\$&');
    const { metadata } = record;
    return `### ${escape(metadata.name || record.original_id)} · ${escape(record.version)}\n\n` +
        `- 插件 ID / Plugin ID: ${escape(record.plugin_id)}\n` +
        `- 作者 / Owner: ${escape(record.owner.login)}\n` +
        `- 提交者 / Submitted by: ${escape(record.submitted_by.login)}\n` +
        `- 类型 / Type: ${escape(metadata.type || 'CLI')}\n` +
        `- 提交时间 / Submitted: ${escape(record.submitted_at)}\n` +
        `- 安装包大小 / Size: ${record.size} bytes\n` +
        `- SHA256: \`${record.sha256}\`\n\n` +
        `${escape(metadata.description)}\n\n` +
        `更新说明 / Changes: ${escape(record.changelog)}\n\n`;
}

function submissionReadme(repository, records) {
    return '<!-- primigenius-market-submissions -->\n# PrimiGenius 插件提交记录 / Plugin submissions\n\n' +
        '此处保存作者上传的插件及完整配置；审核和当前上架状态以平台市场记录为准。\n' +
        'Uploaded packages and full configurations are archived here. Review and listing status are maintained by the marketplace.\n\n' +
        Object.values(records).filter(r => /^submission-[a-f0-9]{32}$/.test(r.tag)).reverse().map(record =>
            submissionSummary(record) +
            `[完整配置与提交信息 / Full record](https://github.com/${repository}/blob/HEAD/submissions/${record.tag}.json) · ` +
            `[安装包 / Package](https://github.com/${repository}/releases/tag/${record.tag}) · ` +
            `[审核记录 / Review](https://github.com/${REPOSITORY}/issues/${Number(record.review.issue)})\n\n`).join('---\n\n');
}

function register({ app, ipcMain, safeStorage, dialog, shell, getWindow, getBackendPort, getBackendToken, getDownloadCounts, download, net }) {
    let cache = null, accountCache = null, busyInstall = false, authVersion = 0;
    const publicProfiles = new Map();
    const request = (url, options) => requestJSON(url, { ...options, net });
    const selections = new Map();
    const vault = () => path.join(app.getPath('userData'), 'github-market-session.bin');
    const readToken = () => {
        if (!safeStorage.isEncryptionAvailable()) return '';
        try { return safeStorage.decryptString(fs.readFileSync(vault())); } catch (_) { return ''; }
    };
    const forget = () => {
        authVersion++; fs.rmSync(vault(), { force: true }); cache = null; accountCache = null;
        const window = getWindow();
        if (window && !window.webContents.isDestroyed?.()) window.webContents.send?.('github-session-cleared');
    };
    async function api(route, method = 'GET', data, options = {}) {
        try { return await request('https://api.github.com' + route, { method, data, token: readToken(), ...options }); }
        catch (error) { if (error.status === 401) forget(); throw error; }
    }
    async function user() {
        const token = readToken();
        if (!token) throw new Error('请先登录 GitHub');
        const fingerprint = crypto.createHash('sha256').update(token).digest('hex');
        if (accountCache && accountCache.fingerprint === fingerprint && Date.now() - accountCache.at < 300000) return accountCache.value;
        const value = await api('/user', 'GET', undefined, { timeout: 5000 });
        accountCache = { value, fingerprint, at: Date.now() };
        return value;
    }
    function validateState(value) {
        if (!value || value.schema !== 1 || value.administrator_id !== ADMIN_ID || !value.plugins || !value.requests ||
            !Array.isArray(value.reviewers) || !Array.isArray(value.blocked) || !Array.isArray(value.audit)) throw new Error('自由市场注册表无效');
        return value;
    }
    async function state(force = false, authoritative = false) {
        if (!authoritative && cache && !force && Date.now() - cache.at < 15000) return cache.value;
        const route = `/repos/${REPOSITORY}/contents/market-state.json?ref=main`;
        // Listing and permission changes are live state. A faster cached mirror may undo an unlist.
        const result = await api(route, 'GET', undefined, { timeout: 6000 });
        let blob = result;
        if (result.encoding === 'none') {
            if (!/^[a-f0-9]{40}$/.test(result.sha || '')) throw new Error('自由市场目录摘要无效');
            blob = await api(`/repos/${REPOSITORY}/git/blobs/${result.sha}`, 'GET', undefined, { timeout: 6000 });
        }
        const value = validateState(JSON.parse(Buffer.from(blob.content, 'base64').toString('utf8')));
        cache = { at: Date.now(), value };
        return value;
    }
    async function issue(title, body) {
        return api(`/repos/${REPOSITORY}/issues`, 'POST', { title, body: JSON.stringify({ schema: 1, ...body }, null, 2) });
    }
    function handle(name, callback) {
        ipcMain.handle(name, async (event, args = {}) => {
            if (!getWindow() || event.sender !== getWindow().webContents || event.senderFrame !== event.sender.mainFrame) throw new Error('需要主程序窗口');
            return callback(args, event);
        });
    }
    handle('market-list', async ({ force }) => {
        const [catalog, identity] = await Promise.all([state(!!force),
            readToken() ? user().then(value => ({ value }), error => ({ error })) : { value: null }]);
        const account = readToken() ? identity.value : null;
        return { state: catalog, user: account ? { id: account.id, login: account.login,
            role: account.id === ADMIN_ID ? 'admin' : catalog.reviewers.includes(account.id) ? 'reviewer' : 'user' } : null,
            warning: identity.error ? '插件列表已加载，但暂时无法验证 GitHub 登录状态，请稍后刷新' : '',
            accountUnavailable: !!identity.error && identity.error.status !== 401 && !!readToken(),
            repository: REPOSITORY };
    });
    handle('market-download-counts', async ({ force }) => {
        try {
            if (!getDownloadCounts) throw new Error('下载统计暂不可用');
            const plugins = Object.values(cache ? cache.value.plugins : {});
            return { ok: true, ...await getDownloadCounts(plugins, !!force) };
        } catch (error) {
            return { ok: false, reason: error.message || String(error) };
        }
    });
    handle('market-session', async () => ({ token: readToken() }));
    handle('market-login', async ({ token }) => {
        const version = ++authVersion;
        if (!safeStorage.isEncryptionAvailable()) throw new Error('系统凭据加密不可用，不能安全保存登录信息');
        if (typeof token !== 'string' || !token || token.length > 4096 || /\s/.test(token)) throw new Error('GitHub 授权凭据不合法');
        // Community owns Device Flow and its UI; trust only the identity verified by GitHub here.
        const account = await request('https://api.github.com/user', { token, timeout: 5000 });
        if (!Number.isSafeInteger(account.id) || account.id <= 0 || typeof account.login !== 'string' || !account.login) {
            throw new Error('GitHub 返回的账号信息不合法');
        }
        if (version !== authVersion) throw new Error('登录操作已取消');
        const encrypted = safeStorage.encryptString(token);
        fs.mkdirSync(app.getPath('userData'), { recursive: true });
        fs.writeFileSync(vault(), encrypted);
        accountCache = { value: account, fingerprint: crypto.createHash('sha256').update(token).digest('hex'), at: Date.now() };
        cache = null;
        return { user: { id: account.id, login: account.login } };
    });
    handle('market-logout', async () => { forget(); return { ok: true }; });
    handle('market-select-source', async ({ folder }) => {
        if (selections.size >= 4) throw new Error('请先完成或关闭现有上传');
        const selected = await dialog.showOpenDialog(getWindow(), folder ? { properties: ['openDirectory'] } :
            { properties: ['openFile'], filters: [{ name: 'Plugin ZIP', extensions: ['zip'] }] });
        if (selected.canceled) return null;
        let file = selected.filePaths[0], temporary = false;
        try {
            if (folder) {
                const result = await request(`http://127.0.0.1:${getBackendPort()}/package-plugin-source`,
                    { method: 'POST', data: { directory: file }, timeout: 300000, desktopToken: getBackendToken() });
                if (result.status !== 'success') throw new Error(result.message || '源码打包失败');
                file = result.zipPath;
                temporary = true;
            }
            const size = fs.statSync(file).size;
            if (!size || size > MAX_SIZE) throw new Error('安装包须小于或等于 256 MiB');
            const id = crypto.randomBytes(24).toString('hex');
            const digest = await sha256(file);
            selections.set(id, { file, temporary, size, sha256: digest });
            return { id, name: path.basename(selected.filePaths[0]), size, sha256: digest };
        } catch (error) { if (temporary) fs.rmSync(file, { force: true }); throw error; }
    });
    handle('market-discard-source', async ({ id }) => {
        const selection = selections.get(id);
        if (selection && !selection.busy) {
            if (selection.temporary) fs.rmSync(selection.file, { force: true });
            selections.delete(id);
        }
        return { ok: true };
    });
    handle('market-submit', async ({ selection: id, pluginId = 'new', changelog = '', version }, event) => {
        const selection = selections.get(id);
        if (!selection || selection.busy) throw new Error('请先选择安装包，或等待当前上传完成');
        if (typeof changelog !== 'string' || changelog.length > 8000 || !/^(new|community_\d+_[A-Za-z0-9_.-]+)$/.test(pluginId)) throw new Error('提交信息不合法');
        if (typeof version !== 'string' || version.length > 128 || !/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$/.test(version)) {
            throw new Error('版本号必填，格式例如 1.0.0 或 1.0.0-beta.1');
        }
        selection.busy = true;
        let stage = '本地准备安装包';
        const progress = text => {
            stage = text;
            if (!event.sender.isDestroyed()) event.sender.send('market-progress', { selection: id, text: text + '…' });
        };
        try {
            progress('本地准备安装包');
            if (await sha256(selection.file) !== selection.sha256) throw new Error('源文件已变化，请重新选择');
            if (selection.version !== version) {
                const result = await request(`http://127.0.0.1:${getBackendPort()}/prepare-plugin-upload`, {
                    method: 'POST', data: { zipPath: selection.file, version }, timeout: 300000, desktopToken: getBackendToken() });
                if (result.status !== 'success') throw new Error(result.message || '上传副本打包失败');
                try {
                    const size = fs.statSync(result.zipPath).size;
                    if (!size || size > MAX_SIZE) throw new Error('安装包须小于或等于 256 MiB');
                    const digest = await sha256(result.zipPath);
                    if (selection.temporary) fs.rmSync(selection.file, { force: true });
                    Object.assign(selection, { file: result.zipPath, temporary: true, size, sha256: digest, version, config: result.config, source: null, issue: null, issueInput: null });
                } catch (error) { fs.rmSync(result.zipPath, { force: true }); throw error; }
            }
            progress('验证 GitHub 账号');
            const account = await user();
            if (selection.issue && (selection.issueInput.accountId !== account.id ||
                selection.issueInput.pluginId !== pluginId || selection.issueInput.changelog !== changelog)) {
                throw new Error('审核申请已提交，请保持原账号、插件和更新说明后重试补全仓库信息');
            }
            progress('读取市场审核与权限信息');
            const catalog = await state(true, true);
            if (catalog.blocked.includes(account.id)) throw new Error('此账号已被平台禁用');
            if (await sha256(selection.file) !== selection.sha256) throw new Error('源文件已变化，请重新选择');
            if (!selection.source) {
                const repository = `${account.login}/PrimiGenius-market-uploads`;
                progress('检查个人上传仓库');
                let staging;
                try { staging = await api(`/repos/${repository}`); }
                catch (error) {
                    if (error.status !== 404) throw error;
                    staging = await api('/user/repos', 'POST', { name: 'PrimiGenius-market-uploads', private: false,
                        description: 'PrimiGenius community plugin submissions', has_issues: false, has_wiki: false });
                    await api(`/repos/${repository}/contents/uploads.json`, 'PUT', {
                        message: 'Initialize plugin upload storage', content: Buffer.from('{"primigenius_market_uploads":1}').toString('base64') });
                }
                if (staging.owner.id !== account.id || staging.private) throw new Error('上传仓库必须公开且属于当前账号');
                let marker;
                try { marker = await api(`/repos/${repository}/contents/uploads.json`); }
                catch (error) { if (error.status !== 404) throw error; }
                if (!marker || JSON.parse(Buffer.from(marker.content, 'base64').toString('utf8')).primigenius_market_uploads !== 1) {
                    throw new Error('同名上传仓库缺少市场标识，已停止上传以免改动其他项目');
                }
                progress('准备 GitHub Release');
                if (!selection.release) selection.release = await api(`/repos/${repository}/releases`, 'POST', {
                    tag_name: `submission-${crypto.randomBytes(16).toString('hex')}`,
                    name: `${submissionText(selection.config.name) || selection.config.id} ${version}`.slice(0, 200), draft: false,
                    body: 'Submitted from PrimiGenius. Publication requires author acceptance and platform review.' });
                const release = selection.release;
                progress('检查已上传附件');
                const assets = await api(`/repos/${repository}/releases/${release.id}/assets?per_page=100`);
                let asset = assets.find(item => item.name === 'plugin.zip');
                if (asset && asset.digest !== `sha256:${selection.sha256}`) {
                    await api(`/repos/${repository}/releases/assets/${asset.id}`, 'DELETE');
                    asset = null;
                }
                progress('上传 GitHub 插件附件');
                if (!asset) asset = await request(`https://uploads.github.com/repos/${repository}/releases/${release.id}/assets?name=plugin.zip`,
                    { method: 'POST', file: selection.file, token: readToken(), timeout: 600000 });
                if (asset.digest && asset.digest !== `sha256:${selection.sha256}`) throw new Error('GitHub 上传摘要不一致');
                selection.source = { repository, release_id: release.id, asset_id: asset.id, sha256: selection.sha256 };
            }
            progress('提交市场审核申请');
            const result = selection.issue || await issue(pluginId === 'new' ? '[插件发布] 新插件' : `[插件更新] ${pluginId}`, {
                kind: 'release', plugin_id: pluginId, source: selection.source, changelog });
            selection.issue = result;
            selection.issueInput = { accountId: account.id, pluginId, changelog };
            {
                progress('记录作者仓库中的插件信息（审核申请已提交，失败可重试补全）');
                const repository = selection.source.repository;
                const owner = pluginId === 'new' ? account : catalog.plugins[pluginId]?.owner;
                if (!owner) throw new Error('插件作者信息不存在');
                const record = submissionRecord(selection, account, owner,
                    pluginId === 'new' ? `community_${account.id}_${selection.config.id.slice(0, 90)}` : pluginId, changelog, result);
                const readContent = async name => {
                    try { return await api(`/repos/${repository}/contents/${name}`); }
                    catch (error) { if (error.status === 404) return null; throw error; }
                };
                const writeContent = (name, text, previous) => api(`/repos/${repository}/contents/${name}`, 'PUT', {
                    message: `Record plugin submission ${selection.release.tag_name}`,
                    content: Buffer.from(text).toString('base64'), ...(previous ? { sha: previous.sha } : {}) });
                const recordPath = `submissions/${record.tag}.json`;
                await writeContent(recordPath, JSON.stringify(record, null, 2) + '\n', await readContent(recordPath));
                // Re-read on conflicts so simultaneous submissions never overwrite each other's index entries.
                let records;
                for (let attempt = 0; attempt < 3; attempt++) {
                    const previous = await readContent('uploads.json');
                    const index = previous ? JSON.parse(Buffer.from(previous.content, 'base64').toString('utf8')) : {};
                    if (index.primigenius_market_uploads !== 1) throw new Error('上传仓库标识已变化');
                    const summary = { ...record, metadata: { name: record.metadata.name, description: record.metadata.description, type: record.metadata.type } };
                    records = { ...index.submissions, [record.tag]: summary };
                    try { await writeContent('uploads.json', JSON.stringify({ ...index, submissions: records }, null, 2) + '\n', previous); break; }
                    catch (error) { if (error.status !== 409 || attempt === 2) throw error; }
                }
                const readme = await readContent('README.md');
                if (!readme || Buffer.from(readme.content, 'base64').toString('utf8').startsWith('<!-- primigenius-market-submissions -->')) {
                    const latest = await readContent('uploads.json');
                    records = JSON.parse(Buffer.from(latest.content, 'base64').toString('utf8')).submissions;
                    await writeContent('README.md', submissionReadme(repository, records), readme);
                }
                await api(`/repos/${repository}/releases/${selection.release.id}`, 'PATCH', {
                    name: `${submissionText(selection.config.name) || selection.config.id} ${version}`.slice(0, 200),
                    body: submissionSummary(record) +
                        `[完整配置与提交信息 / Full record](https://github.com/${repository}/blob/HEAD/${recordPath})\n\n` +
                        `[审核记录 / Review](${result.html_url})\n\n此为提交存档，审核及当前上架状态请查看市场。` });
            }
            selections.delete(id);
            if (selection.temporary) fs.rmSync(selection.file, { force: true });
            cache = null;
            return { issue: result.number, url: result.html_url };
        } catch (error) {
            const contextual = new Error(stage + '失败：' + error.message);
            contextual.code = error.code;
            throw contextual;
        } finally { selection.busy = false; }
    });
    handle('market-collaborate', async ({ pluginId, message = '' }) => {
        await user();
        if (!/^community_\d+_[A-Za-z0-9_.-]+$/.test(pluginId) || typeof message !== 'string' || message.length > 2000) throw new Error('合作申请不合法');
        const result = await issue(`[合作申请] ${pluginId}`, { kind: 'collaboration', plugin_id: pluginId, message });
        return { issue: result.number, url: result.html_url };
    });
    handle('market-command', async ({ number, command }) => {
        await user();
        if (typeof command !== 'string' || command.length > 4000 || !/^\/market (author-accept|author-reject|approve|reject|withdraw|collaborate-accept|collaborate-reject|reviewer-add|reviewer-remove|ban|unban|unlist|restore|collaborator-remove)( |$)/.test(command)) throw new Error('不支持的市场操作');
        let result;
        if (number) {
            if (!Number.isSafeInteger(number) || number <= 0) throw new Error('申请编号不合法');
            result = await api(`/repos/${REPOSITORY}/issues/${number}/comments`, 'POST', { body: command });
        } else result = await issue('[市场管理] ' + command.split(' ')[1], { kind: 'management', command });
        cache = null;
        return { url: result.html_url };
    });
    handle('market-resolve-user', async ({ value }) => {
        await user();
        if (typeof value !== 'string' || !/^[A-Za-z0-9-]{1,40}$/.test(value)) throw new Error('GitHub 用户名或账号 ID 不合法');
        const cached = publicProfiles.get(value.toLowerCase());
        if (cached && Date.now() - cached.at < 300000) return cached.value;
        const account = await api(/^\d+$/.test(value) ? `/user/${value}` : `/users/${value}`, 'GET', undefined, { timeout: 6000 });
        if (!Number.isSafeInteger(account.id) || account.id <= 0 || typeof account.login !== 'string' || !account.login) throw new Error('GitHub 返回的账号信息不合法');
        const profile = { id: account.id, login: account.login, name: account.name || '',
            avatar_url: `https://avatars.githubusercontent.com/u/${account.id}?v=4`,
            company: account.company || '', location: account.location || '', bio: account.bio || '' };
        const entry = { value: profile, at: Date.now() };
        publicProfiles.set(String(profile.id), entry); publicProfiles.set(profile.login.toLowerCase(), entry);
        return profile;
    });
    handle('market-preview', async ({ number, file }) => {
        if (!Number.isSafeInteger(number) || number <= 0 || typeof file !== 'string' || file.length > 1000) throw new Error('预览请求不合法');
        const catalog = await state();
        const record = catalog.requests[String(number)];
        if (!record || record.kind !== 'release' || !Object.prototype.hasOwnProperty.call(record.files, file)) throw new Error('文件不存在');
        const source = record.source;
        if (!/^[A-Za-z0-9-]+\/PrimiGenius-market-uploads$/.test(source.repository) || !Number.isSafeInteger(source.asset_id)) throw new Error('原始附件地址不合法');
        const repository = await api(`/repos/${source.repository}`);
        if (repository.owner.id !== record.author.id || repository.private) throw new Error('上传仓库归属不一致');
        const asset = await api(`/repos/${source.repository}/releases/assets/${source.asset_id}`);
        if (!asset.browser_download_url.startsWith(`https://github.com/${source.repository}/releases/download/`) || asset.size > MAX_SIZE) throw new Error('附件不合法');
        const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'primigenius-market-preview-'));
        try {
            const zip = path.join(temporary, 'source.zip');
            await download(asset.browser_download_url, zip, async candidate => {
                if (fs.statSync(candidate).size !== asset.size || await sha256(candidate) !== source.sha256) throw new Error('原始附件摘要不一致');
                return true;
            });
            return await request(`http://127.0.0.1:${getBackendPort()}/preview-plugin-archive`, {
                method: 'POST', data: { zipPath: zip, file }, timeout: 120000, desktopToken: getBackendToken() });
        } finally { fs.rmSync(temporary, { recursive: true, force: true }); }
    });
    handle('market-open', async ({ url }) => {
        const target = new URL(url);
        const profile = /^\/[A-Za-z0-9-]{1,39}\/?$/.test(target.pathname);
        if (target.protocol !== 'https:' || target.hostname !== 'github.com' || target.username || target.password || target.port ||
            (!profile && !target.pathname.startsWith('/' + REPOSITORY + '/'))) throw new Error('不是自由市场仓库或 GitHub 用户主页地址');
        await shell.openExternal(target.href);
    });
    handle('market-install', async ({ pluginId }, event) => {
        if (busyInstall) throw new Error('请等待当前安装完成');
        busyInstall = true;
        let temporary;
        const progress = update => {
            if (!event.sender.isDestroyed()) event.sender.send('market-install-progress', { pluginId, ...update });
        };
        try {
            progress({ phase: 'verify', text: '正在验证插件上架状态与签名' });
            const catalog = await state(true, true);
            const plugin = catalog.plugins[pluginId];
            if (!plugin || !plugin.listed || catalog.blocked.includes(plugin.owner.id) || !plugin.published) throw new Error('插件未发布或已下架');
            const key = fs.readFileSync(path.join(__dirname, 'marketplace-public-key.pem'), 'utf8');
            const info = verifyManifest(plugin.published, key, pluginId);
            const minimum = String(info.min_app_version).split('.').map(Number), current = app.getVersion().split('.').map(Number);
            if (minimum.length !== 3 || minimum.some(n => !Number.isSafeInteger(n))) throw new Error('最低软件版本声明不合法');
            for (let i = 0; i < 3; i++) { if (current[i] > minimum[i]) break; if (current[i] < minimum[i]) throw new Error(`需要 PrimiGenius ${info.min_app_version} 或更新版本`); }
            temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'primigenius-market-install-'));
            const zip = path.join(temporary, 'plugin.zip');
            const url = `https://github.com/${REPOSITORY}/releases/download/${encodeURIComponent(info.tag)}/${encodeURIComponent(info.asset)}`;
            progress({ phase: 'download', text: '开始下载插件安装包' });
            await download(url, zip, async file => {
                progress({ phase: 'verify', text: '正在校验安装包大小与 SHA256' });
                if (fs.statSync(file).size !== info.size || await sha256(file) !== info.sha256) throw new Error('安装包完整性校验失败，已阻止安装');
                return true;
            }, {
                onProgress: value => progress({ ...value, phase: 'download' }),
                log: text => progress({ phase: 'log', text })
            });
            progress({ phase: 'install', text: '校验通过，正在安装插件' });
            const result = await request(`http://127.0.0.1:${getBackendPort()}/install-plugin-panel`, {
                method: 'POST', data: { zipPath: zip, expected: { id: info.id, version: info.version, source: info.source, owner_github_id: info.owner_github_id } }, timeout: 120000 });
            if (result.status !== 'success') throw new Error(result.message || '插件安装失败');
            progress({ phase: 'done', text: '插件安装成功' });
            return { ok: true, id: info.id, version: info.version };
        } catch (error) {
            progress({ phase: 'failed', text: error.message || String(error) });
            throw error;
        } finally { if (temporary) fs.rmSync(temporary, { recursive: true, force: true }); busyInstall = false; }
    });
    app.on('will-quit', () => { for (const item of selections.values()) if (item.temporary) fs.rmSync(item.file, { force: true }); });
}

module.exports = { register, verifyManifest, requestJSON, sha256, REPOSITORY, ADMIN_ID, submissionRecord, submissionReadme, submissionSummary };
