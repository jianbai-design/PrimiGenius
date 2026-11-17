'use strict';
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), cp = require('node:child_process');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');

async function checkPage() {
    const { open } = require(path.join(root, 'src/marketplace'));
    const wait = async (test, label) => {
        const until = Date.now() + 12000;
        while (!test()) { if (Date.now() > until) throw new Error('Timed out: ' + label); await new Promise(resolve => setTimeout(resolve, 25)); }
    };
    const style = document.createElement('style'); style.textContent = require('fs').readFileSync(path.join(root, 'src/styles.css'), 'utf8'); document.head.append(style);
    const parts = Array.from({ length: 48 }, (_, n) => `// fragment ${n}\n` + 'const sequence = "中文😀";\r\n'.repeat(1600) + `// fragment-end ${n}\n`);
    assert(Buffer.byteLength(parts.join('')) > 2 * 1024 * 1024);
    const calls = [], tabs = {}, host = document.createElement('main'); document.body.append(host);
    let lang = 'zh';
    const record = { kind: 'release', plugin_id: 'community_10_demo', original_id: 'demo', version: '1.0.0', status: 'pending_review', author: { id: 10, login: 'author' },
        metadata: { name: '在线源码预览' }, sha256: 'a'.repeat(64), source_verified: true, source: { commit: 'b'.repeat(40) },
        files: { 'scripts/large.py': { size: Buffer.byteLength(parts.join('')) }, 'config.json': { size: 10 },
            'scripts/sub/run.py': { size: 20 }, 'tests/run.py': { size: 20 }, 'runtime/rootfs.tar.gz': { size: 1000000 } },
        changes: [{ path: 'scripts/sub/run.py', change: 'changed' }, { path: 'tests/run.py', change: 'added' }] };
    const ipc = { async invoke(channel, args) {
        if (channel === 'market-preview-close') return { ok: true };
        if (channel === 'market-download-counts') return { ok: true, counts: {} };
        if (channel === 'market-list') return { user: { id: 134767426, login: 'admin', role: 'admin' }, state: {
            requests: { 1: record }, plugins: {}, blocked: [], reviewers: [], audit: [] } };
        assert.equal(channel, 'market-preview'); calls.push(args);
        assert.equal(args.file, 'scripts/large.py');
        await new Promise(resolve => setTimeout(resolve, 5));
        return { text: parts[args.page], startLine: args.page * 1602 + 1, hasNext: args.page + 1 < parts.length };
    } };
    open({ ipc, tabs, language: () => lang, switchTab() {}, createTabFrame(_id, _label, mount) { mount(host); } });
    await wait(() => host.querySelector('.market-view-select'), 'market loaded');
    tabs['sys-marketplace'].showReviews();
    const inspectButton = () => [...host.querySelectorAll('button')].find(button => ['文件与变更', 'Files and changes'].includes(button.textContent));
    await wait(inspectButton, 'review actions'); inspectButton().click();
    await wait(() => document.querySelector('.market-inspect-dialog'), 'inspect dialog');
    const tree = document.querySelector('.market-inspect-file-list');
    const scriptsFolder = tree.querySelector('[data-directory="scripts"]');
    assert.equal(tree.querySelectorAll(':scope > details').length, 3, 'runtime/scripts/tests are root folders');
    assert(tree.querySelector(':scope > [data-file="config.json"]'), 'Root files stay outside folders');
    assert(scriptsFolder.querySelector('[data-directory="scripts/sub"] [data-file="scripts/sub/run.py"]'));
    assert.equal(tree.querySelector('[data-file="scripts/sub/run.py"] .market-inspect-file-path').textContent, 'run.py');
    assert.equal(tree.querySelector('[data-file="tests/run.py"]').title, 'tests/run.py', 'Duplicate basenames keep distinct full paths');
    assert.equal(tree.querySelector('[data-file="scripts/sub/run.py"] .market-inspect-badge').textContent, '修改');
    scriptsFolder.querySelector('summary').click(); assert.equal(scriptsFolder.open, false);
    const search = document.querySelector('.market-inspect-search');
    search.value = 'SUB/RUN.PY'; search.dispatchEvent(new Event('input'));
    assert(scriptsFolder.open); assert(scriptsFolder.querySelector('details').open, 'Search expands matching ancestors');
    assert(tree.querySelector('[data-directory="tests"]').hidden);
    assert(tree.querySelector('[data-file="config.json"]').hidden);
    assert(!tree.querySelector('[data-file="scripts/sub/run.py"]').hidden);
    search.value = 'unmatched-file'; search.dispatchEvent(new Event('input'));
    assert.equal(tree.querySelector('.market-inspect-empty').hidden, false);
    search.value = ''; search.dispatchEvent(new Event('input'));
    assert.equal(scriptsFolder.open, false, 'Clearing search restores the user collapse state');
    assert.equal(tree.querySelector('[data-directory="tests"]').hidden, false);
    scriptsFolder.querySelector('summary').click(); assert(scriptsFolder.open);
    assert.equal(calls.length, 0, 'Folding and searching do not download or read files');
    document.querySelector('[data-file="scripts/large.py"]').click();
    let viewport;
    await wait(() => (viewport = document.querySelector('.market-inspect-text .file-preview-scroll')) && viewport.querySelector('[data-loaded="true"]'), 'first fragment');
    assert(calls.length <= 2, 'Opening a file must not fetch every fragment');
    assert(![...document.querySelectorAll('button')].some(button => /下一页|上一页|Next page|Previous page/.test(button.textContent)));
    for (let n = 0; n < 150 && viewport.querySelectorAll('.file-preview-segment').length < parts.length; n++) {
        viewport.scrollTop = viewport.scrollHeight; viewport.dispatchEvent(new Event('scroll'));
        await new Promise(resolve => setTimeout(resolve, 40));
    }
    await wait(() => viewport.querySelectorAll('.file-preview-segment').length === parts.length, 'complete file');
    await new Promise(resolve => setTimeout(resolve, 200));
    assert(viewport.querySelectorAll('[data-loaded="true"]').length < 8, `Old rendered fragments must be reclaimed: ${viewport.querySelectorAll('[data-loaded="true"]').length}, viewport ${viewport.clientHeight}/${viewport.scrollHeight}; ` + [...viewport.querySelectorAll('[data-loaded="true"]')].map(block => [block.dataset.segment, Math.round(block.getBoundingClientRect().top), block.offsetHeight]).join(';'));
    assert.equal(viewport.querySelector('[data-segment="0"]').dataset.loaded, 'false');
    assert(calls.some(call => call.page === parts.length - 1), 'The final fragment is accessible');
    viewport.scrollTop = 0; viewport.dispatchEvent(new Event('scroll'));
    await wait(() => viewport.querySelector('[data-segment="0"]').dataset.loaded === 'true', 'scroll back to first fragment');
    assert(calls.filter(call => call.page === 0).length >= 2, 'Reclaimed content can be revisited');
    const checkbox = document.querySelector('.market-inspect-wrap input'); checkbox.click();
    await wait(() => document.querySelector('.market-inspect-text [data-loaded="true"]'), 'wrap toggle');
    assert.equal(checkbox.checked, false);
    document.querySelector('.market-inspect-dialog .market-close').click();
    await new Promise(resolve => setTimeout(resolve, 80)); const closedReads = calls.length;
    await new Promise(resolve => setTimeout(resolve, 120)); assert.equal(calls.length, closedReads, 'Closing the dialog stops fragment requests');
    lang = 'en'; tabs['sys-marketplace'].renderMarket(); tabs['sys-marketplace'].showReviews();
    await wait(inspectButton, 'English review'); inspectButton().click();
    await wait(() => document.querySelector('.market-inspect-dialog[open]'), 'English inspect');
    assert(document.querySelector('.market-inspect-dialog[open]').textContent.includes('Source commit'), document.querySelector('.market-inspect-dialog[open]').textContent);
    assert.equal(document.querySelector('.market-inspect-dialog[open] [data-file="scripts/sub/run.py"] .market-inspect-badge').textContent, 'Changed');
    const englishFolder = document.querySelector('.market-inspect-dialog[open] [data-directory="scripts"]');
    englishFolder.querySelector('summary').click(); assert.equal(englishFolder.open, false);
    document.querySelector('.market-inspect-dialog .market-close').click();
    const slowHost = document.createElement('div'); slowHost.style.cssText = 'height:220px;display:flex'; document.body.append(slowHost);
    const slowIPC = new (require('events').EventEmitter)(), statusUpdates = [];
    slowIPC.invoke = (_channel, args) => new Promise(resolve => {
        const progress = setInterval(() => slowIPC.emit('market-preview-progress', {}, { requestId: args.requestId,
            phase: 'download', transferred: '1.0', total: '2.0', speed: '0.5' }), 40);
        setTimeout(() => { clearInterval(progress); resolve({ text: 'slow legacy contents\n', startLine: 1, hasNext: false }); }, 280);
    });
    const originalTimeout = window.setTimeout;
    window.setTimeout = (fn, ms, ...args) => originalTimeout(fn, ms === 150000 ? 150 : ms === 30000 ? 30 : ms, ...args);
    let stopSlow;
    try {
        stopSlow = require(path.join(root, 'src/market-source-preview')).mountSourcePreview(slowHost, {
            ipc: slowIPC, number: 1, file: 'slow.py', lang: 'zh', wrapLines: true, legacy: true,
            onStatus: message => statusUpdates.push(message)
        });
        await wait(() => slowHost.querySelector('[data-loaded="true"]'), 'legacy read survives old timeout and active progress renews its deadline');
        assert(statusUpdates.some(message => message.includes('1.0 / 2.0 MiB')));
        assert(!statusUpdates.some(message => message.includes('超时')));
    } finally { stopSlow?.(); window.setTimeout = originalTimeout; slowHost.remove(); }
    assert.equal(slowIPC.listenerCount('market-preview-progress'), 0, 'Disposal releases IPC progress listeners');
    return { fragments: parts.length, bytes: Buffer.byteLength(parts.join('')), loadedAtOnce: 'under 8', bilingual: true,
        directoryTree: true, nestedFolders: true, searchRestoresCollapse: true };
}

if (!process.versions.electron) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'market-source-view-'));
    try {
        const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
        const result = cp.spawnSync(path.join(root, 'node_modules/electron/dist/electron.exe'), [__filename, directory],
            { env, windowsHide: true, encoding: 'utf8', timeout: 45000 });
        process.stdout.write(result.stdout || ''); process.stderr.write(result.stderr || ''); process.exitCode = result.status ?? 1;
    } finally { fs.rmSync(directory, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }); }
} else {
    const { app, BrowserWindow, session } = require('electron');
    app.setPath('userData', path.join(process.argv[2], 'profile')); app.disableHardwareAcceleration(); app.commandLine.appendSwitch('in-process-gpu');
    app.whenReady().then(async () => {
        let window;
        try {
            session.defaultSession.webRequest.onBeforeRequest({ urls: ['http://*/*', 'https://*/*'] }, (_request, done) => done({ cancel: true }));
            window = new BrowserWindow({ show: false, width: 1280, height: 850, webPreferences: { nodeIntegration: true, contextIsolation: false, sandbox: false } });
            await window.loadURL('data:text/html,<meta charset="utf-8"><body></body>');
            const result = await window.webContents.executeJavaScript(`const root=${JSON.stringify(root)}; const assert=require('node:assert/strict'),path=require('node:path'); (${checkPage.toString()})();`);
            console.log('PASS: Electron online source continuous scrolling, full file, bounded rendered content, backward scrolling, wrapping, close cleanup and bilingual UI', JSON.stringify(result));
            window.destroy(); app.exit(0);
        } catch (error) { console.error(error.stack); window?.destroy(); app.exit(1); }
    });
}
