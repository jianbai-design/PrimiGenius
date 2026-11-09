const assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path'), http = require('node:http');
const { Writable, EventEmitter } = require('node:stream');
const { register, REPOSITORY, ADMIN_ID, submissionReadme } = require('../src/github-market-main');

async function run() {
    const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'market-records-'));
    const source = path.join(temporary, 'source.zip'); fs.writeFileSync(source, 'source fixture');
    fs.writeFileSync(path.join(temporary, 'github-market-session.bin'), 'test token');
    const config = { id: 'demo', name: { zh: '测试插件', en: 'Demo plugin' }, description: 'Full description', type: 'CLI',
        version: '1.0.0', command_template: 'echo test', parameters: [{ name: 'input', type: 'file', help: 'Help' }] };
    const prepared = [];
    const server = http.createServer((req, res) => {
        let body = ''; req.on('data', chunk => body += chunk); req.on('end', () => {
            assert.equal(req.url, '/prepare-plugin-upload');
            assert.equal(req.headers['x-primigenius-desktop'], 'desktop');
            const { version } = JSON.parse(body), zipPath = path.join(temporary, 'copy-' + prepared.length + '.zip');
            fs.writeFileSync(zipPath, 'prepared ' + version); prepared.push(zipPath);
            res.end(JSON.stringify({ status: 'success', zipPath, config: { ...config, version } }));
        });
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const files = new Map([['uploads.json', { sha: 'initial', content: Buffer.from('{"primigenius_market_uploads":1,"custom":"preserved"}').toString('base64') }]]);
    let issues = 0, releases = 0, uploads = 0, conflict = true, failRecord = true;
    const releaseData = new Map(), handlers = {};
    const sender = { mainFrame: {}, isDestroyed: () => false, send() {} };
    const repository = 'author/PrimiGenius-market-uploads';
    const state = { schema: 1, administrator_id: ADMIN_ID, reviewers: [], blocked: [], audit: [], requests: {}, plugins: {} };
    const net = { request(options) {
        const chunks = [];
        const request = new Writable({ write(chunk, encoding, done) { chunks.push(chunk); done(); }, final(done) {
            done(); queueMicrotask(() => {
                const url = new URL(options.url), route = url.pathname, method = options.method;
                let status = 200, value;
                const data = method === 'POST' || method === 'PUT' || method === 'PATCH' ?
                    (url.hostname === 'uploads.github.com' ? null : JSON.parse(Buffer.concat(chunks).toString())) : null;
                if (route === '/user') value = { id: 10, login: 'author' };
                else if (route.includes('/contents/market-state.json')) value = { content: Buffer.from(JSON.stringify(state)).toString('base64') };
                else if (route === '/repos/' + repository) value = { owner: { id: 10 }, private: false };
                else if (route.includes('/contents/')) {
                    const name = route.split('/contents/')[1];
                    if (method === 'GET') { value = files.get(name); if (!value) { status = 404; value = {}; } }
                    else if (name.startsWith('submissions/') && failRecord) { status = 500; value = { message: 'record unavailable' }; failRecord = false; }
                    else if (name === 'uploads.json' && conflict) { status = 409; value = { message: 'conflict' }; conflict = false; }
                    else {
                        assert.equal(data.sha, files.get(name)?.sha);
                        value = { sha: String(Date.now()) + Math.random(), content: data.content }; files.set(name, value);
                    }
                } else if (url.hostname === 'uploads.github.com') { uploads++; value = { id: uploads }; }
                else if (route.endsWith('/releases') && method === 'POST') {
                    releases++; value = { ...data, id: releases, created_at: '2026-10-30T00:00:00Z' }; releaseData.set(releases, value);
                } else if (route.endsWith('/assets')) value = [];
                else if (route.includes('/releases/') && method === 'PATCH') {
                    value = Object.assign(releaseData.get(Number(route.split('/').at(-1))), data);
                } else if (route.endsWith('/issues')) { issues++; value = { number: issues, html_url: `https://github.com/${REPOSITORY}/issues/${issues}` }; }
                else throw new Error('Unexpected request: ' + options.url);
                const response = new EventEmitter(); response.statusCode = status;
                request.emit('response', response); response.emit('data', Buffer.from(JSON.stringify(value))); response.emit('end');
            });
        } });
        request.setHeader = () => {}; request.abort = () => {}; return request;
    } };
    try {
        register({ app: { getPath: () => temporary, on() {} }, ipcMain: { handle: (name, fn) => handlers[name] = fn }, net,
            safeStorage: { isEncryptionAvailable: () => true, decryptString: bytes => bytes.toString() },
            getWindow: () => ({ webContents: sender }), getBackendPort: () => server.address().port, getBackendToken: () => 'desktop',
            dialog: { showOpenDialog: async () => ({ filePaths: [source] }) } });
        const invoke = (name, args) => handlers[name]({ sender, senderFrame: sender.mainFrame }, args);
        const selected = await invoke('market-select-source', {});
        const args = { selection: selected.id, version: '1.0.0', changelog: 'Full changes' };
        await assert.rejects(invoke('market-submit', args), /审核申请已提交.*record unavailable/);
        assert.equal(issues, 1);
        await assert.rejects(invoke('market-submit', { ...args, changelog: 'changed after submission' }), /保持原账号、插件和更新说明/);
        assert.equal((await invoke('market-submit', args)).issue, 1);
        assert.equal(issues, 1, 'Retrying metadata must not create another review issue');
        assert.equal(uploads, 1); assert.equal(releases, 1);
        let index = JSON.parse(Buffer.from(files.get('uploads.json').content, 'base64'));
        assert.equal(index.custom, 'preserved'); assert.equal(index.primigenius_market_uploads, 1);
        let tag = Object.keys(index.submissions)[0];
        const record = JSON.parse(Buffer.from(files.get(`submissions/${tag}.json`).content, 'base64'));
        assert.deepEqual(record.metadata, config); assert.equal(record.owner.login, 'author');
        assert.equal(record.plugin_id, 'community_10_demo'); assert.equal(record.changelog, args.changelog);
        assert.match(record.sha256, /^[a-f0-9]{64}$/); assert(record.size > 0);
        const readme = Buffer.from(files.get('README.md').content, 'base64').toString();
        assert(readme.includes('测试插件 / Demo plugin')); assert(readme.includes(`submissions/${tag}.json`));
        assert(releaseData.get(1).body.includes(record.review.url)); assert.equal(releaseData.get(1).name, '测试插件 / Demo plugin 1.0.0');
        files.set('README.md', { sha: 'custom', content: Buffer.from('Custom author README').toString('base64') });
        const second = await invoke('market-select-source', {});
        await invoke('market-submit', { ...args, selection: second.id, version: '1.1.0' });
        index = JSON.parse(Buffer.from(files.get('uploads.json').content, 'base64'));
        assert.equal(Object.keys(index.submissions).length, 2);
        assert.equal(Buffer.from(files.get('README.md').content, 'base64').toString(), 'Custom author README');
        const malicious = structuredClone(record); malicious.metadata.name = '<img src=x> [evil](https://evil.test)';
        const escaped = submissionReadme(repository, { [tag]: malicious });
        assert(!escaped.includes('<img')); assert(!escaped.includes('[evil]('));
        assert(prepared.every(file => !fs.existsSync(file))); assert.equal(fs.readFileSync(source, 'utf8'), 'source fixture');
        console.log('PASS: full config records, readable releases/README, preserved index/history/custom README, conflict retry, no duplicate issues on metadata retry, safe Markdown and cleanup');
    } finally { await new Promise(resolve => server.close(resolve)); fs.rmSync(temporary, { recursive: true, force: true }); }
}
run().catch(error => { console.error(error); process.exitCode = 1; });
