'use strict';
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), cp = require('node:child_process');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');

async function checkPage() {
    const { ipcRenderer: ipc } = require('electron');
    const wait = async (test, label) => {
        const until = Date.now() + 10000;
        while (!await test()) { if (Date.now() > until) throw new Error('Timed out: ' + label + ': ' + document.body.textContent.slice(-600)); await new Promise(resolve => setTimeout(resolve, 20)); }
    };
    const tabs = {}, host = document.createElement('main'); document.body.append(host); let lang = 'zh';
    require(path.join(root, 'src/marketplace')).open({ ipc, tabs, language: () => lang, switchTab() {},
        createTabFrame(_id, _caption, mount) { mount(host); } });
    await wait(() => host.querySelector('.market-view-select'), 'market list'); tabs['sys-marketplace'].showReviews();
    const inspect = () => [...host.querySelectorAll('button')].find(button => ['文件与变更', 'Files and changes'].includes(button.textContent));
    await wait(inspect, 'review actions'); inspect().click();
    const select = async (file, expected) => {
        document.querySelector(`.market-inspect-dialog[open] [data-file="${file}"]`).click();
        await wait(() => {
            const segment = document.querySelector('.market-inspect-dialog[open] [data-loaded="true"]');
            return segment?.textContent.includes(expected);
        }, file);
    };
    await select('config.json', 'demo');
    let audit = await ipc.invoke('test-legacy-audit'); assert.equal(audit.downloads, 1); assert(audit.exists);
    await select('build-runtime.ps1', 'Write-Output');
    await select('README.zh-CN.md', 'Third source file');
    document.querySelector('.market-inspect-wrap input').click();
    await wait(() => document.querySelector('.market-inspect-dialog[open] [data-loaded="true"]'), 'wrap reload');
    document.querySelector('.market-inspect-sidebar > button').click();
    await select('config.json', 'demo');
    audit = await ipc.invoke('test-legacy-audit');
    assert.equal(audit.downloads, 1, 'Switching actual file-list entries, wrapping and overview must not redownload the ZIP');
    assert.equal(new Set(audit.sessions).size, 1, 'Renderer, preview adapter and main IPC retain the same dialog ID');
    assert(audit.sessions.every(session => /^[a-f0-9]{32}$/.test(session)));
    assert(!audit.progress.slice(audit.firstRead).includes('download'), 'Cached files must not replay stale download progress');
    document.querySelector('.market-inspect-dialog[open] .market-close').click();
    await wait(async () => !(await ipc.invoke('test-legacy-audit')).exists, 'temporary ZIP removed after dialog closes');
    await wait(() => !document.querySelector('.market-inspect-dialog'), 'dialog removed');
    lang = 'en'; tabs['sys-marketplace'].renderMarket(); tabs['sys-marketplace'].showReviews();
    inspect().click(); await select('build-runtime.ps1', 'Write-Output');
    assert.equal(document.querySelector('.market-inspect-dialog[open] .market-dialog-header strong').textContent, 'Inspect submission');
    audit = await ipc.invoke('test-legacy-audit'); assert.equal(audit.downloads, 2, 'A new inspect window owns a new temporary attachment');
    assert.equal(new Set(audit.sessions).size, 2);
    document.querySelector('.market-inspect-dialog[open] .market-close').click();
    await wait(async () => !(await ipc.invoke('test-legacy-audit')).exists, 'second temporary ZIP cleanup');
    return { firstDialogDownloads: 1, sourceFiles: 3, stableSession: true, wrappingAndOverview: true, closeCleanup: true };
}

if (!process.versions.electron) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'market-legacy-view-'));
    try {
        const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
        const result = cp.spawnSync(path.join(root, 'node_modules/electron/dist/electron.exe'), [__filename, directory],
            { env, windowsHide: true, encoding: 'utf8', timeout: 35000 });
        process.stdout.write(result.stdout || ''); process.stderr.write(result.stderr || ''); process.exitCode = result.status ?? 1;
    } finally { fs.rmSync(directory, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 }); }
} else {
    const { app, BrowserWindow, ipcMain, session } = require('electron');
    const { Writable, EventEmitter } = require('node:stream');
    const crypto = require('node:crypto'), http = require('node:http');
    const { register, ADMIN_ID } = require('../src/github-market-main');
    const directory = process.argv[2]; app.setPath('userData', path.join(directory, 'profile'));
    app.disableHardwareAcceleration(); app.commandLine.appendSwitch('in-process-gpu');
    app.whenReady().then(async () => {
        let window, server; const paths = [];
        try {
            const bytes = Buffer.from('verified dialog ZIP fixture');
            const source = { repository: 'author/PrimiGenius-market-uploads', asset_id: 123,
                sha256: crypto.createHash('sha256').update(bytes).digest('hex') };
            const contents = { 'config.json': '{"id":"demo"}\n', 'build-runtime.ps1': 'Write-Output "hello"\n', 'README.zh-CN.md': 'Third source file\n' };
            const record = { kind: 'release', status: 'pending_review', plugin_id: 'community_10_demo', version: '1.0.0',
                source, author: { id: 10, login: 'author' }, metadata: { name: 'Legacy plugin' }, sha256: 'a'.repeat(64),
                files: Object.fromEntries(Object.entries(contents).map(([name, text]) => [name, { size: Buffer.byteLength(text) }])), changes: [] };
            const catalog = { schema: 1, administrator_id: ADMIN_ID, blocked: [], reviewers: [], audit: [], plugins: {}, requests: { 1: record } };
            const sessions = [], progress = []; let downloads = 0, firstRead;
            server = http.createServer((req, res) => {
                let raw = ''; req.on('data', chunk => raw += chunk); req.on('end', () => {
                    const data = JSON.parse(raw); assert(fs.readFileSync(data.zipPath).equals(bytes));
                    res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ text: contents[data.file] }));
                });
            });
            await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
            session.defaultSession.webRequest.onBeforeRequest({ urls: ['http://*/*', 'https://*/*'] }, (_req, done) => done({ cancel: true }));
            window = new BrowserWindow({ show: false, width: 1280, height: 850, webPreferences: { nodeIntegration: true, contextIsolation: false, sandbox: false } });
            fs.mkdirSync(app.getPath('userData'), { recursive: true }); fs.writeFileSync(path.join(app.getPath('userData'), 'github-market-session.bin'), 'fixture');
            register({ app, getWindow: () => window, getBackendPort: () => server.address().port, getBackendToken: () => 'fixture', getNetworkMode: () => 'cn',
                safeStorage: { isEncryptionAvailable: () => true, decryptString: () => 'fixture' },
                ipcMain: { handle(name, fn) { ipcMain.handle(name, (event, args) => {
                    if (name === 'market-preview') sessions.push(args.session); return fn(event, args);
                }); } },
                net: { request(options) {
                    const req = new Writable({ write(_c, _e, done) { done(); }, final(done) { done(); setImmediate(() => {
                        if (req.cancelled) return;
                        const url = new URL(options.url);
                        const value = url.pathname === '/user' ? { id: ADMIN_ID, login: 'admin' } :
                            url.pathname.includes('/contents/market-state.json') ? { content: Buffer.from(JSON.stringify(catalog)).toString('base64') } :
                            url.pathname.includes('/releases/assets/') ? { size: bytes.length,
                                browser_download_url: `https://github.com/${source.repository}/releases/download/old/plugin.zip` } : { owner: { id: 10 }, private: false };
                        const res = new EventEmitter(); res.statusCode = 200; req.emit('response', res); res.emit('data', Buffer.from(JSON.stringify(value))); res.emit('end');
                    }); } }); req.setHeader = () => {}; req.abort = () => req.cancelled = true; return req;
                } },
                download: async (_url, file, verify, callbacks) => {
                    downloads++; paths.push(file); callbacks.onProgress({ transferred: '0.1', total: '1.0', speed: '1.0', percent: 10 });
                    await new Promise(resolve => setTimeout(resolve, 50)); fs.writeFileSync(file, bytes); await verify(file);
                } });
            const send = window.webContents.send.bind(window.webContents);
            window.webContents.send = (channel, value) => {
                if (channel === 'market-preview-progress') { progress.push(value.phase); if (value.phase === 'read' && firstRead === undefined) firstRead = progress.length; }
                return send(channel, value);
            };
            ipcMain.handle('test-legacy-audit', () => ({ downloads, sessions, progress, firstRead, exists: paths.some(file => fs.existsSync(file)) }));
            const fixture = path.join(directory, 'fixture.html');
            fs.writeFileSync(fixture, '<meta charset="utf-8"><style>' + fs.readFileSync(path.join(root, 'src/styles.css'), 'utf8') + '</style><body></body>');
            await window.loadFile(fixture);
            const result = await window.webContents.executeJavaScript(`const root=${JSON.stringify(root)}; const assert=require('node:assert/strict'),path=require('node:path'); (${checkPage.toString()})();`);
            console.log('PASS: real Electron file clicks → IPC → verified legacy ZIP reuse', JSON.stringify(result));
            await new Promise(resolve => server.close(resolve)); window.destroy(); app.exit(0);
        } catch (error) {
            console.error(error.stack); if (server) await new Promise(resolve => server.close(resolve));
            window?.destroy(); for (const file of paths) fs.rmSync(path.dirname(file), { recursive: true, force: true, maxRetries: 5, retryDelay: 100 }); app.exit(1);
        }
    });
}
