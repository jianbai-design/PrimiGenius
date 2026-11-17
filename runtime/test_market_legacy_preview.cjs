'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), http = require('node:http');
const crypto = require('node:crypto'), { Writable, EventEmitter } = require('node:stream');
const { register, ADMIN_ID, REPOSITORY, sha256 } = require('../src/github-market-main');
const { LegacyPreviewStore } = require('../src/market-legacy-preview');

async function run() {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'market-legacy-test-'));
    fs.writeFileSync(path.join(directory, 'github-market-session.bin'), 'test-token');
    const bytes = Buffer.from('verified legacy archive fixture'), digest = crypto.createHash('sha256').update(bytes).digest('hex');
    const source = { repository: 'author/PrimiGenius-market-uploads', asset_id: 123, sha256: digest };
    const record = { kind: 'release', author: { id: 10 }, source, files: { 'config.json': {}, 'run.py': {}, 'binary.gz': {} } };
    const state = { schema: 1, administrator_id: ADMIN_ID, blocked: [], reviewers: [], audit: [], plugins: {}, requests: { 1: record } };
    const asset = { size: bytes.length, browser_download_url: `https://github.com/${source.repository}/releases/download/old/plugin.zip` };
    let downloads = 0, quit; const paths = [], events = [], calls = [], handlers = {};
    const sender = { mainFrame: {}, isDestroyed: () => false, send: (channel, value) => events.push({ channel, value }) };
    const server = http.createServer((req, res) => {
        let body = ''; req.on('data', chunk => body += chunk); req.on('end', () => {
            assert.equal(req.headers['x-primigenius-desktop'], 'fixture-token');
            const data = JSON.parse(body); assert(fs.readFileSync(data.zipPath).equals(bytes));
            res.writeHead(data.file === 'binary.gz' ? 400 : 200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify(data.file === 'binary.gz' ? { error: '二进制文件不能作为文本预览' } : { text: 'contents of ' + data.file }));
        });
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const net = { request(options) {
        const headers = {}, req = new Writable({ write(_c, _e, done) { done(); }, final(done) { done(); setImmediate(() => {
            if (req.cancelled) return;
            const url = new URL(options.url); calls.push({ url: url.href, headers });
            let value, status = 200;
            if (url.pathname.includes('/contents/market-state.json')) {
                assert.equal(headers.Authorization, 'Bearer test-token'); value = { content: Buffer.from(JSON.stringify(state)).toString('base64') };
            } else {
                assert(!headers.Authorization, 'Public origins and acceleration routes must never receive the GitHub token');
                if (url.hostname === 'api.github.com') { status = 503; value = { message: 'origin unavailable' }; }
                else if (url.hostname === 'gh-proxy.com') value = { owner: { id: 999 }, private: false }; // Reject a fast invalid mirror.
                else value = url.pathname.includes('/releases/assets/') ? asset : { owner: { id: 10 }, private: false };
            }
            const res = new EventEmitter(); res.statusCode = status; req.emit('response', res); res.emit('data', Buffer.from(JSON.stringify(value))); res.emit('end');
        }); } });
        req.setHeader = (name, value) => headers[name] = value; req.abort = () => { req.cancelled = true; }; return req;
    } };
    try {
        register({ app: { getPath: () => directory, on: (_event, fn) => quit = fn }, ipcMain: { handle: (name, fn) => handlers[name] = fn },
            safeStorage: { isEncryptionAvailable: () => true, decryptString: value => value.toString() },
            getWindow: () => ({ webContents: sender }), getBackendPort: () => server.address().port, getBackendToken: () => 'fixture-token',
            getNetworkMode: () => 'cn', net, download: async (url, target, verify, progress) => {
                assert.equal(url, asset.browser_download_url); downloads++; paths.push(target);
                progress.log('Selecting built-in route'); progress.onProgress({ percent: 20, transferred: '0.2', total: '1.0', speed: '0.1' });
                await new Promise(resolve => setTimeout(resolve, 30)); fs.writeFileSync(target, bytes); await verify(target);
            } });
        const invoke = (name, args) => handlers[name]({ sender, senderFrame: sender.mainFrame }, args);
        const session = 'a'.repeat(32), args = { number: 1, page: 0, session, requestId: 'b'.repeat(32) };
        const result = await Promise.all(['config.json', 'run.py'].map(file => invoke('market-preview', { ...args, file })));
        assert(result.every(part => part.text.startsWith('contents of '))); assert.equal(downloads, 1, 'Concurrent files share one accelerated attachment download');
        assert(fs.existsSync(paths[0]));
        const beforeSwitch = events.length;
        await invoke('market-preview', { ...args, file: 'run.py' });
        assert.equal(downloads, 1, 'Selecting a different file after the first read completes must reuse the ZIP');
        assert(!events.slice(beforeSwitch).some(event => event.value.phase === 'download'), 'A cached file switch must not replay download progress');
        await assert.rejects(invoke('market-preview', { ...args, file: 'binary.gz' }), /二进制/);
        await invoke('market-preview', { ...args, file: 'config.json' }); assert.equal(downloads, 1, 'A binary-file error must not discard the verified ZIP');
        assert(calls.some(call => call.url.includes('v6.gh-proxy.org')));
        assert(events.some(event => event.channel === 'market-preview-progress' && event.value.phase === 'download' && event.value.percent === 20));
        assert(events.some(event => event.value.phase === 'verify'));
        await invoke('market-preview-close', { session }); assert(!fs.existsSync(path.dirname(paths[0])));
        await assert.rejects(invoke('market-preview', { ...args, file: 'config.json' }), /已关闭/);
        await invoke('market-preview', { ...args, session: undefined, file: 'config.json' });
        await invoke('market-preview', { ...args, session: undefined, file: 'run.py' });
        assert.equal(downloads, 2, 'Older callers without a dialog ID also share one verified ZIP');
        quit(); assert(paths.every(file => !fs.existsSync(path.dirname(file))), 'Application shutdown releases legacy-compatible attachments');

        // Failed integrity and close-during-download both remove their own temporary directory.
        let corruptPath;
        const store = new LegacyPreviewStore({ hash: sha256, maxSize: 1024,
            readPublic: async (route, validate) => { const value = route.includes('/assets/') ? asset : { owner: { id: 10 }, private: false }; assert(validate(value)); return value; },
            download: async (_url, file, verify) => { corruptPath = file; fs.writeFileSync(file, Buffer.alloc(bytes.length, 0)); await verify(file); }
        });
        const retryId = 'e'.repeat(32);
        await assert.rejects(store.read({ id: retryId, source, author: { id: 10 }, progress() {}, readFile: async () => {} }), /摘要不一致/);
        assert(!fs.existsSync(path.dirname(corruptPath)));
        store.download = async (_url, file, verify) => { fs.writeFileSync(file, bytes); await verify(file); };
        assert.equal(await store.read({ id: retryId, source, author: { id: 10 }, progress() {}, readFile: async () => 'retry succeeded' }), 'retry succeeded');
        assert.equal(store.sessions.get(retryId).timer, undefined, 'An open inspect window must not expire its verified ZIP');
        store.close(retryId, false);
        assert.equal(await store.read({ id: retryId, source, author: { id: 10 }, progress() {}, readFile: async () => 'reopened after eviction' }), 'reopened after eviction');
        store.close(retryId);
        let releaseDownload, pendingPath;
        store.download = async (_url, file, verify) => {
            pendingPath = file; await new Promise(resolve => releaseDownload = resolve); fs.writeFileSync(file, bytes); await verify(file);
        };
        const pendingId = 'd'.repeat(32), pending = store.read({ id: pendingId, source, author: { id: 10 }, progress() {}, readFile: async () => {} });
        await new Promise(resolve => setImmediate(resolve)); assert(releaseDownload);
        const cancelled = assert.rejects(pending, /已关闭/); store.close(pendingId); releaseDownload(); await cancelled;
        assert(!fs.existsSync(path.dirname(pendingPath)));
        let compatibilityDownloads = 0, compatibilityPath;
        store.download = async (_url, file, verify) => { compatibilityDownloads++; compatibilityPath = file; fs.writeFileSync(file, bytes); await verify(file); };
        const oldCaller = { source, author: { id: 10 }, progress() {}, readFile: async () => 'legacy contents' };
        await store.read(oldCaller); await store.read(oldCaller);
        assert.equal(compatibilityDownloads, 1, 'An older caller without a session ID must also reuse the verified attachment');
        store.closeAll(); assert(!fs.existsSync(path.dirname(compatibilityPath)));
        const explicitId = 'f'.repeat(32);
        await store.read({ ...oldCaller, id: explicitId });
        assert.equal(store.sessions.get(explicitId).timer, undefined, 'An open inspect dialog must not expire its verified attachment');
        store.close(explicitId);
        console.log('PASS: legacy metadata acceleration despite origin failure, invalid mirror rejection, no token leakage, progress forwarding, one ZIP per dialog, binary errors retain cache, checksum failure/close cleanup and late-read rejection');
    } finally { await new Promise(resolve => server.close(resolve)); fs.rmSync(directory, { recursive: true, force: true });
        for (const target of paths) if (fs.existsSync(path.dirname(target))) fs.rmSync(path.dirname(target), { recursive: true, force: true }); }
}
run().catch(error => { console.error(error); process.exitCode = 1; });
