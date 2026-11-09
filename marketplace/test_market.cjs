'use strict';
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { verifyManifest, REPOSITORY } = require('../src/github-market-main');
const keys = crypto.generateKeyPairSync('rsa', { modulusLength: 3072 });
const publicKey = keys.publicKey.export({ type: 'spki', format: 'pem' });
const id = 'community_10_demo';
const info = { id, version: '1.0.0', sha256: 'a'.repeat(64), size: 1024, owner_github_id: 10,
    source: `github:${REPOSITORY}`, tag: `${id}-v1.0.0`, asset: `${id}-1.0.0.zip` };
function sign(value) {
    const raw = Buffer.from(JSON.stringify(value));
    return { manifest: raw.toString('base64'), signature: crypto.sign('RSA-SHA256', raw, keys.privateKey).toString('base64') };
}
assert.equal(verifyManifest(sign(info), publicKey, id).id, id);
const tampered = sign(info); tampered.manifest = Buffer.from(JSON.stringify({ ...info, size: 12 })).toString('base64');
assert.throws(() => verifyManifest(tampered, publicKey, id), /签名验证失败/);
for (const changed of [{ source: 'github:attacker/repo' }, { owner_github_id: 99 }, { size: 300 * 1024 * 1024 },
    { tag: '../escape' }, { asset: 'different.zip' }]) {
    assert.throws(() => verifyManifest(sign({ ...info, ...changed }), publicKey, id), /安装清单不合法/);
}
assert.throws(() => verifyManifest(sign(info), publicKey, 'community_99_demo'), /安装清单不合法/);
console.log('Signed manifest checks passed: trusted key, tampering, source, owner, ID, size and release binding.');

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { Writable } = require('node:stream');
const { EventEmitter } = require('node:events');
const { register, requestJSON, ADMIN_ID } = require('../src/github-market-main');
const { errorMessage } = require('../src/marketplace');
const { publicGithubCandidates, isPublicGithubProxyUrl, raceGithubReads, installGithubAvatarFallback } = require('../src/github-public');

function fakeNetwork(respond) {
    const calls = [];
    return { calls, request(options) {
        const request = new Writable({ autoDestroy: false, write(chunk, encoding, done) { request.recordChunks.push(chunk); done(); },
            final(done) { done(); queueMicrotask(() => respond(request, options)); } });
        request.headers = {}; request.recordChunks = [];
        request.setHeader = (name, value) => {
            assert.equal(typeof value, 'string');
            assert.notEqual(name.toLowerCase(), 'content-length', 'Chromium must calculate Content-Length itself');
            request.headers[name.toLowerCase()] = value;
        };
        request.abort = () => { request.aborted = true; };
        calls.push({ options, request });
        return request;
    } };
}
function reply(request, value, statusCode = 200) {
    const response = new EventEmitter(); response.statusCode = statusCode;
    request.emit('response', response);
    response.emit('data', Buffer.from(JSON.stringify(value)));
    response.emit('end');
}
function timeout(request) { request.emit('error', new Error('connect ETIMEDOUT 20.205.243.168:443')); }
function catalog() { return { schema: 1, administrator_id: ADMIN_ID, reviewers: [], blocked: [], plugins: {}, requests: {}, audit: [] }; }
async function registeredScenario(respond, action, options = {}) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'market-network-test-'));
    const vault = path.join(directory, 'github-market-session.bin');
    fs.writeFileSync(vault, 'encrypted-test-session');
    const handlers = new Map();
    const progress = [];
    const frame = {}, sender = { mainFrame: frame, isDestroyed: () => false, send(name, value) { if (name === 'market-progress') progress.push(value); } };
    const net = fakeNetwork(respond);
    let downloads = 0;
    register({ app: { getPath: () => directory, on() {} }, ipcMain: { handle: (name, fn) => handlers.set(name, fn) },
        safeStorage: { isEncryptionAvailable: () => options.encryptionAvailable !== false,
            decryptString: value => value.toString().startsWith('encrypted:') ? value.toString().slice(10) : 'test-token',
            encryptString: token => Buffer.from('encrypted:' + token) },
        dialog: options.dialog || {}, shell: {}, getWindow: () => ({ webContents: sender }), getBackendPort: () => options.port || 1,
        getBackendToken: () => 'desktop-test-secret', getNetworkMode: async () => options.networkMode || 'auto',
        download: async () => { downloads++; }, net });
    const invoke = (name, args = {}) => handlers.get(name)({ sender, senderFrame: frame }, args);
    try { await action({ invoke, net, vault, handlers, progress, downloads: () => downloads }); }
    finally { fs.rmSync(directory, { recursive: true, force: true }); }
}

(async () => {
    const publicURL = `https://api.github.com/repos/${REPOSITORY}/contents/market-state.json?ref=main`;
    const rawURL = `https://raw.githubusercontent.com/${REPOSITORY}/main/market-state.json`;
    const apiCandidates = publicGithubCandidates(publicURL, {}, 'cn');
    assert.equal(apiCandidates.length, 4);
    assert(!apiCandidates.some(url => url.startsWith('https://z2.gh-proxy.cn/')), 'Exclude the stale API mirror');
    assert(apiCandidates[0].startsWith('https://gh-proxy.com/'));
    assert.equal(apiCandidates.at(-1), publicURL);
    assert.equal(publicGithubCandidates(publicURL)[0], publicURL);
    assert.equal(publicGithubCandidates(rawURL).length, 9);
    const avatar = 'https://avatars.githubusercontent.com/u/134767426?v=4';
    const archive = 'https://github.com/owner/repo/archive/refs/heads/main.zip';
    const asset = 'https://github.com/owner/repo/releases/download/v1/tool.zip';
    for (const url of [avatar, archive, asset]) {
        assert.deepEqual(publicGithubCandidates(url), [url, 'https://gh-proxy.com/' + url]);
        assert.deepEqual(publicGithubCandidates(url, {token:'secret'}), [url]);
        const privateUrl = url + (url.includes('?') ? '&' : '?') + 'access_token=secret';
        assert.deepEqual(publicGithubCandidates(privateUrl), [privateUrl]);
    }
    let avatarError;
    installGithubAvatarFallback({addEventListener:(event, callback, capture)=>{assert.equal(event,'error');assert.equal(capture,true);avatarError=callback;}});
    const img = {tagName:'IMG',src:avatar};avatarError({target:img});
    assert.equal(img.src,'https://gh-proxy.com/' + avatar);avatarError({target:img});
    assert.equal(img.src,'https://gh-proxy.com/' + avatar,'An unavailable avatar proxy must not loop');
    const privateAvatar={tagName:'IMG',src:avatar+'&access_token=secret'};avatarError({target:privateAvatar});
    assert.equal(privateAvatar.src,avatar+'&access_token=secret');
    for (const options of [{ method: 'POST' }, { body: '{}' }, { data: {} }, { token: 'test-token' },
        { credentials: 'include' }, { headers: { aUtHoRiZaTiOn: 'Bearer test-token' } },
        { headers: new Headers({ Cookie: 'session=test' }) }, { headers: [['Authorization', 'test-token']] }]) {
        assert.deepEqual(publicGithubCandidates(publicURL, options, 'cn'), [publicURL]);
    }
    for (const url of [publicURL + '&access_token=private', publicURL + '&client_secret=private',
        publicURL.replace('https:', 'http:'), publicURL.replace('api.github.com', 'api.github.com.evil.example'),
        publicURL.replace('api.github.com', 'user:password@api.github.com'), publicURL.replace('api.github.com', 'api.github.com:8443')]) {
        assert.deepEqual(publicGithubCandidates(url, {}, 'cn'), [url]);
    }
    assert(isPublicGithubProxyUrl(apiCandidates[0]));
    assert(!isPublicGithubProxyUrl(apiCandidates[0] + '&access_token=private'));
    assert(!isPublicGithubProxyUrl('https://untrusted.example/' + publicURL));
    const publicTransport = fakeNetwork(request => reply(request, { ok: true }));
    for (const options of [{ token: 'test-token' }, { method: 'POST', data: {} }, { desktopToken: 'secret' }]) {
        await assert.rejects(requestJSON(apiCandidates[0], { net: publicTransport, ...options }), /不支持/);
    }
    assert.equal(publicTransport.calls.length, 0, 'Credentials and writes must fail before contacting a proxy');
    assert.deepEqual(await requestJSON(apiCandidates[0], { net: publicTransport }), { ok: true });
    assert.equal(publicTransport.calls[0].request.headers.authorization, undefined);
    const cancelled = [];
    const winner = await raceGithubReads(['stalled', 'invalid', 'valid'], async (url, controller) => {
        if (url === 'invalid') throw new Error('Invalid JSON');
        if (url === 'valid') return { schema: 1 };
        return new Promise((resolve, reject) => controller.signal.addEventListener('abort', () => {
            cancelled.push(url); reject(new Error('cancelled'));
        }, { once: true }));
    });
    assert.equal(winner.schema, 1); assert.deepEqual(cancelled, ['stalled']);
    const attempted = [];
    assert.equal(await raceGithubReads(['direct', 'mirror'], async url => { attempted.push(url); return url; }, 50), 'direct');
    assert.deepEqual(attempted, ['direct'], 'Fast direct reads must cancel delayed mirrors');
    await assert.rejects(raceGithubReads(['bad', 'also-bad'], async () => { throw new Error('offline'); }), AggregateError);

    await registeredScenario((request, { url }) => {
        if (url === apiCandidates[0]) reply(request, { content: Buffer.from('{"schema":0}').toString('base64') });
        else if (isPublicGithubProxyUrl(url)) reply(request, { content: Buffer.from(JSON.stringify(catalog())).toString('base64') });
        else timeout(request);
    }, async ({ invoke, net, downloads }) => {
        await assert.rejects(invoke('market-list'), /连接失败/);
        const proxies = net.calls.filter(call => isPublicGithubProxyUrl(call.options.url));
        assert.equal(proxies.length, 0, 'Mutable listing state must never come from a mirror');
        const before = proxies.length;
        await assert.rejects(invoke('market-install', { pluginId: id }), /连接失败/);
        assert.equal(net.calls.filter(call => isPublicGithubProxyUrl(call.options.url)).length, before);
        assert.equal(downloads(), 0, 'Mirrored catalogs must not authorize installation');
    }, { networkMode: 'cn' });
    await registeredScenario((request, { url }) => {
        if (url.startsWith('https://gh-proxy.com/https://raw.')) reply(request, '<html>not a catalog</html>');
        else if (url.startsWith('https://gh-proxy.org/https://raw.')) reply(request, catalog());
        else timeout(request);
    }, async ({ invoke, net }) => {
        await assert.rejects(invoke('market-list'), /连接失败/);
        assert(net.calls.every(call => call.options.url.startsWith('https://api.github.com/')));
    }, { networkMode: 'cn' });
    const renderer = fs.readFileSync(path.join(__dirname, '../src/renderer.js'), 'utf8');
    const communityCode = renderer.slice(renderer.indexOf('function _ghHeaders('), renderer.indexOf('function _ghHttpError(')) +
        renderer.slice(renderer.indexOf('function _ghFetchCandidateUrls('), renderer.indexOf('async function _ghFetch('));
    const communityCalls = [];
    const community = require('node:vm').runInNewContext(communityCode + '\n({ _ghHeaders, _ghFetchFastest, _ghFetchCandidateUrls })', {
        publicGithubCandidates, raceGithubReads, URL, AbortController, setTimeout, clearTimeout,
        _ghGetToken: () => 'test-token', _ghHttpError: response => new Error('HTTP ' + response.status),
        fetch: async (url, options) => {
            communityCalls.push({ url, options });
            return new Response(url === 'html' ? '<html>not JSON</html>' : url === 'object' ? '{"message":"not a list"}' : '[]');
        }
    });
    assert.equal(community._ghHeaders().Authorization, 'token test-token');
    assert.equal(community._ghHeaders(true).Authorization, undefined);
    assert.equal(community._ghFetchCandidateUrls(publicURL, { headers: community._ghHeaders() }, 'cn').length, 1);
    const response = await community._ghFetchFastest(['html', 'object', 'valid'], { headers: community._ghHeaders(true) },
        1000, 'cn', 'https://api.github.com/repos/example/repo/issues');
    assert.deepEqual(await response.json(), []);
    assert.equal(communityCalls.length, 3, 'Invalid HTML and wrong-shaped JSON must not win a community read');
    assert(communityCalls.every(call => call.options.credentials === 'omit' && call.options.redirect === 'error' && !call.options.headers.Authorization));
    console.log('Public acceleration checks passed: API/Raw routing, credential exclusion, validated winners, hedge cancellation and official-only market state.');

    const native = fakeNetwork(request => reply(request, { ok: true }));
    assert.deepEqual(await requestJSON('https://api.github.com/user', { net: native, token: 'test-token' }), { ok: true });
    assert.equal(native.calls[0].options.redirect, 'error');
    assert.equal(native.calls[0].options.useSessionCookies, false);
    assert.equal(native.calls[0].request.headers.authorization, 'Bearer test-token');
    assert.deepEqual(await requestJSON('https://api.github.com/repos/demo/test/releases', { method: 'POST', data: { name: 'release' }, net: native }), { ok: true });
    assert.equal(native.calls[1].request.headers['content-length'], undefined);
    const invalid = fakeNetwork(request => request.emit('error', new Error('net::ERR_INVALID_ARGUMENT')));
    await assert.rejects(requestJSON('https://api.github.com/repos/demo/test/releases', { method: 'POST', data: {}, net: invalid }),
        error => error.code === 'GITHUB_REQUEST_INVALID' && /请求参数/.test(error.message) && !/连接失败/.test(error.message));
    const closed = require('node:http').createServer();
    await new Promise(resolve => closed.listen(0, '127.0.0.1', resolve));
    const closedPort = closed.address().port;
    await new Promise(resolve => closed.close(resolve));
    await assert.rejects(requestJSON(`http://127.0.0.1:${closedPort}/prepare-plugin-upload`, { method: 'POST', data: {}, timeout: 1000 }),
        error => error.code === 'LOCAL_BACKEND_UNAVAILABLE' && /本地打包服务/.test(error.message) && !/GitHub/.test(error.message));
    await assert.rejects(requestJSON(`https://raw.githubusercontent.com/${REPOSITORY}/main/market-state.json`,
        { net: native, token: 'test-token' }), /不支持/);
    await assert.rejects(requestJSON('https://api.github.com@attacker.example/user', { net: native, token: 'test-token' }), /不支持/);
    const stalled = fakeNetwork(() => {});
    const started = Date.now();
    await assert.rejects(requestJSON('https://api.github.com/user', { net: stalled, timeout: 35 }), /连接超时/);
    assert(Date.now() - started < 1000, 'A stalled connection must be bounded before a socket exists');
    assert(stalled.calls[0].request.aborted);

    await registeredScenario((request, options) => {
        if (options.url.startsWith('https://raw.githubusercontent.com/')) reply(request, catalog());
        else timeout(request);
    }, async ({ invoke, net, vault, downloads }) => {
        await assert.rejects(invoke('market-list'), /连接失败/);
        assert(fs.existsSync(vault), 'A network outage must not erase a valid session');
        const rawCalls = net.calls.filter(x => x.options.url.startsWith('https://raw.githubusercontent.com/'));
        assert.equal(rawCalls.length, 0);
        await assert.rejects(invoke('market-install', { pluginId: id }), /连接失败/);
        assert.equal(downloads(), 0, 'A fallback catalog must not authorize installation');
        assert.equal(net.calls.filter(x => x.options.url.startsWith('https://raw.githubusercontent.com/')).length, 0);
    });
    let failPrimary = false, profileCalls = 0;
    await registeredScenario((request, options) => {
        if (options.url === 'https://api.github.com/user') { profileCalls++; reply(request, { id: ADMIN_ID, login: 'jianbai-design' }); }
        else if (options.url.startsWith('https://raw.githubusercontent.com/')) reply(request, catalog());
        else if (failPrimary) timeout(request);
        else reply(request, { encoding: 'base64', content: Buffer.from(JSON.stringify(catalog())).toString('base64') });
    }, async ({ invoke, net }) => {
        assert.equal((await invoke('market-list')).user.role, 'admin');
        failPrimary = true;
        await assert.rejects(invoke('market-list', { force: true }), /连接失败/);
        failPrimary = false;
        const refreshed = await invoke('market-list', { force: true });
        assert.equal(refreshed.state.schema, 1);
        assert.deepEqual(refreshed.state.plugins, {});
        assert.equal(refreshed.warning, '', 'A valid empty market is a normal result, including after refresh');
        assert.equal(profileCalls, 1, 'A refresh should not repeat a recently verified profile lookup');
        assert(net.calls.every(x => x.options.url.startsWith('https://api.github.com/')), 'Refresh must use the official state, including an empty catalog');
    });

    await registeredScenario((request, { url }) => {
        if (url.endsWith('/user')) reply(request, { id: ADMIN_ID, login: 'test-admin' });
        else if (url === publicURL) reply(request, { encoding: 'none', sha: 'a'.repeat(40) });
        else if (url === `https://api.github.com/repos/${REPOSITORY}/git/blobs/${'a'.repeat(40)}`) {
            reply(request, { encoding: 'base64', content: Buffer.from(JSON.stringify(catalog())).toString('base64') });
        } else throw new Error('Large catalogs must also use only the official API');
    }, async ({ invoke, net }) => {
        const refreshed = await invoke('market-list', { force: true });
        assert.equal(refreshed.state.schema, 1);
        assert(net.calls.some(call => call.options.url.includes('/git/blobs/')));
        assert(net.calls.every(call => call.options.url.startsWith('https://api.github.com/')));
    }, { networkMode: 'cn' });

    const current = catalog();
    current.plugins[id] = { id, owner: { id: 10, login: 'author' }, collaborators: [], listed: false, published: info };
    current.audit.push({ event: 'unlist:1' });
    const stale = structuredClone(current); stale.plugins[id].listed = true; stale.audit = [];
    let offline = false;
    await registeredScenario((request, { url }) => {
        if (url.endsWith('/user')) reply(request, { id: ADMIN_ID, login: 'jianbai-design' });
        else if (url === publicURL) {
            if (offline) timeout(request);
            else setTimeout(() => reply(request, { content: Buffer.from(JSON.stringify(current)).toString('base64') }), 850);
        } else if (url.includes('raw.githubusercontent.com/')) reply(request, stale);
        else reply(request, { content: Buffer.from(JSON.stringify(stale)).toString('base64') });
    }, async ({ invoke, net }) => {
        const first = await invoke('market-list');
        assert.equal(first.state.plugins[id].listed, false, 'A faster mirror must not resurrect an officially unlisted plugin');
        assert(!net.calls.some(call => isPublicGithubProxyUrl(call.options.url)), 'Slow official reads must not race stale mirrors even in CN mode');
        assert.equal((await invoke('market-list', { force: true })).state.plugins[id].listed, false);
        offline = true;
        const now = Date.now;
        try {
            Date.now = () => now() + 16000;
            await assert.rejects(invoke('market-list'), /连接失败/);
        } finally { Date.now = now; }
        await assert.rejects(invoke('market-list', { force: true }), /连接失败/);
        const requests = net.calls.length;
        await assert.rejects(invoke('market-install', { pluginId: id }), /连接失败/);
        assert.equal(net.calls.length, requests + 1, 'Installation must still use only the official catalog');
        offline = false;
        assert.equal((await invoke('market-list', { force: true })).state.plugins[id].listed, false);
        assert(net.calls.every(call => call.options.url.startsWith('https://api.github.com/')));
    }, { networkMode: 'cn' });
    await registeredScenario(request => timeout(request), async ({ invoke }) => {
        await assert.rejects(invoke('market-list'), /连接失败/);
    });
    await registeredScenario((request, options) => {
        if (options.url === 'https://api.github.com/user') reply(request, { message: 'Bad credentials' }, 401);
        else reply(request, { content: Buffer.from(JSON.stringify(catalog())).toString('base64') });
    }, async ({ invoke, vault }) => {
        const result = await invoke('market-list');
        assert.equal(result.user, null); assert.equal(result.accountUnavailable, false);
        assert(!fs.existsSync(vault), 'A confirmed 401 should invalidate the session');
    });
    const message = errorMessage(new Error("Error invoking remote method 'market-list': Error: connect ETIMEDOUT 20.205.243.168:443"));
    assert.match(message, /GitHub/); assert.doesNotMatch(message, /20\.205|remote method|ETIMEDOUT/);
    console.log('Network regression checks passed: Chromium transport, absolute deadline, official catalog and blobs, slow official versus stale mirror, outage recovery, 401 and fail-closed installs.');

    let verifiedId = 10, authChecks = 0;
    await registeredScenario((request, { url }) => {
        if (url.endsWith('/user')) {
            authChecks++;
            assert.equal(request.headers.authorization, 'Bearer shared-community-token');
            reply(request, { id: verifiedId, login: 'verified-user' });
        } else reply(request, { content: Buffer.from(JSON.stringify(catalog())).toString('base64') });
    }, async ({ invoke, vault, handlers }) => {
        assert(!handlers.has('market-login-start') && !handlers.has('market-login-poll'), 'Only the shared community Device Flow should remain');
        const result = await invoke('market-login', { token: 'shared-community-token', user: { id: ADMIN_ID, role: 'admin' } });
        assert.equal(result.user.id, 10, 'A caller cannot choose the GitHub identity');
        assert(!JSON.stringify(result).includes('shared-community-token'), 'Login IPC must never return credentials');
        assert.equal(fs.readFileSync(vault, 'utf8'), 'encrypted:shared-community-token');
        assert.equal((await invoke('market-list')).user.role, 'user');
        assert.equal(authChecks, 1, 'The verified login profile should populate the existing identity cache');
        verifiedId = ADMIN_ID;
        await invoke('market-login', { token: 'shared-community-token' });
        assert.equal((await invoke('market-list', { force: true })).user.role, 'admin');
        assert.equal(authChecks, 2, 'Reauthentication must replace the cached identity');
        await invoke('market-logout'); assert(!fs.existsSync(vault));
    });
    await registeredScenario(() => { throw new Error('Invalid input must not call GitHub'); }, async ({ invoke, vault, net }) => {
        for (const token of [undefined, '', {}, 'bad token', 'bad\ntoken', 'x'.repeat(4097)]) {
            await assert.rejects(invoke('market-login', { token }), /凭据不合法/);
        }
        assert.equal(net.calls.length, 0); assert.equal(fs.readFileSync(vault, 'utf8'), 'encrypted-test-session');
    });
    for (const scenario of ['outage', 'unauthorized', 'invalid-profile']) {
        await registeredScenario(request => {
            if (scenario === 'outage') timeout(request);
            else if (scenario === 'unauthorized') reply(request, { message: 'Bad credentials' }, 401);
            else reply(request, { id: 'forged-id', login: 'forged' });
        }, async ({ invoke, vault }) => {
            await assert.rejects(invoke('market-login', { token: 'candidate-token' }));
            assert.equal(fs.readFileSync(vault, 'utf8'), 'encrypted-test-session', 'Failed account verification must preserve the previous session');
        });
    }
    await registeredScenario(() => { throw new Error('Unavailable encryption must fail before GitHub'); }, async ({ invoke, net }) => {
        await assert.rejects(invoke('market-login', { token: 'candidate-token' }), /加密不可用/);
        assert.equal(net.calls.length, 0);
    }, { encryptionAvailable: false });
    console.log('Shared-login checks passed: GitHub-verified identity, encrypted storage boundary, role/cache refresh, malformed credentials, verification failures and encryption availability.');

    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'market-upload-test-'));
    const source = path.join(directory, 'original.zip'); fs.writeFileSync(source, 'original-plugin');
    const prepared = [], versions = [];
    const server = require('node:http').createServer((req, res) => {
        assert.equal(req.url, '/prepare-plugin-upload');
        assert.equal(req.headers['x-primigenius-desktop'], 'desktop-test-secret');
        const chunks = [];
        req.on('data', chunk => chunks.push(chunk));
        req.on('end', () => {
            assert.equal(Number(req.headers['content-length']), Buffer.concat(chunks).length, 'The Node loopback transport must retain Content-Length');
            const data = JSON.parse(Buffer.concat(chunks)); versions.push(data.version);
            const file = path.join(directory, `prepared-${versions.length}.zip`);
            fs.writeFileSync(file, `plugin-version-${data.version}`); prepared.push(file);
            res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ status: 'success', zipPath: file, config: { id: 'demo', version: data.version, name: 'Demo' } }));
        });
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    let issues = 0, releases = 0, uploads = 0;
    const uploadRecords = new Map([['uploads.json', { sha: 'initial', content: Buffer.from('{"primigenius_market_uploads":1}').toString('base64') }]]);
    try {
        await registeredScenario((request, { url, method }) => {
            if (url.endsWith('/user')) reply(request, { id: ADMIN_ID, login: 'jianbai-design' });
            else if (url.includes('/contents/market-state.json')) reply(request, { content: Buffer.from(JSON.stringify(catalog())).toString('base64') });
            else if (url.includes('/contents/')) {
                const name = url.split('/contents/')[1];
                if (method === 'PUT') {
                    uploadRecords.set(name, { sha: String(uploadRecords.size), content: JSON.parse(Buffer.concat(request.recordChunks)).content });
                    reply(request, {});
                } else reply(request, uploadRecords.get(name) || {}, uploadRecords.has(name) ? 200 : 404);
            }
            else if (url.includes('uploads.github.com/')) { uploads++; reply(request, { id: uploads }); }
            else if (url.endsWith('/releases') && method === 'POST') { releases++; reply(request, { id: 100, tag_name: 'submission-' + 'a'.repeat(32) }); }
            else if (url.includes('/releases/100/assets')) reply(request, []);
            else if (url.endsWith('/issues')) { issues++; reply(request, issues === 1 ? { message: 'retry test' } : { number: 2, html_url: 'https://github.com/test/issues/2' }, issues === 1 ? 500 : 201); }
            else reply(request, { owner: { id: ADMIN_ID }, private: false });
        }, async ({ invoke, net, progress }) => {
            const selection = await invoke('market-select-source', { folder: false });
            for (const version of [undefined, '', '1.0', '01.0.0', '1.0.0\n']) {
                await assert.rejects(invoke('market-submit', { selection: selection.id, version }), /版本号/);
            }
            assert.equal(net.calls.length, 0, 'Invalid versions must be rejected before any GitHub request');
            assert.equal(versions.length, 0);
            await assert.rejects(invoke('market-submit', { selection: selection.id, version: '1.0.0' }), /提交市场审核申请失败：retry test/);
            assert(progress.some(event => event.text.includes('本地准备安装包')));
            assert(progress.some(event => event.text.includes('上传 GitHub 插件附件')));
            assert(progress.some(event => event.text.includes('提交市场审核申请')));
            assert(fs.existsSync(prepared[0]));
            assert.equal((await invoke('market-submit', { selection: selection.id, version: '1.1.0' })).issue, 2);
            assert.deepEqual(versions, ['1.0.0', '1.1.0']);
            assert.equal(releases, 1); assert.equal(uploads, 2);
            assert(prepared.every(file => !fs.existsSync(file)), 'Superseded and submitted upload copies must be cleaned');
            assert.equal(fs.readFileSync(source, 'utf8'), 'original-plugin');
        }, { port: server.address().port, dialog: { showOpenDialog: async () => ({ canceled: false, filePaths: [source] }) } });
    } finally { await new Promise(resolve => server.close(resolve)); fs.rmSync(directory, { recursive: true, force: true }); }
    console.log('Upload checks passed: required version, local preparation before GitHub, changed-version retry, original preservation and temporary-copy cleanup.');
})().catch(error => { console.error(error); process.exitCode = 1; });
