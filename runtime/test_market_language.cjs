'use strict';
// Render the actual marketplace in an isolated Electron window, without GitHub requests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const cp = require('node:child_process');
const root = path.resolve(__dirname, '..');

async function checkPage() {
    const { open, errorMessage } = require(path.join(root, 'src/marketplace'));
    const rateLimit = "Error invoking remote method 'market-list': Error: API rate limit exceeded for 192.0.2.1. (Authenticated requests get a higher rate limit.)";
    let lang = 'en', mode = 'limit', reads = 0;
    const tabs = {}, host = document.createElement('main');
    document.body.append(host);
    const warning = '插件列表已加载，但暂时无法验证 GitHub 登录状态，请稍后刷新';
    const ipc = { async invoke(channel) {
        if (channel === 'market-download-counts') return { ok: true, counts: {} };
        assert.equal(channel, 'market-list'); reads++;
        if (mode === 'limit') throw new Error(rateLimit);
        if (mode === 'network') throw new Error('GitHub 连接失败（api.github.com），请检查网络或系统代理后重试');
        return { user: null, warning: mode === 'warning' ? warning : '',
            state: { plugins: {}, blocked: [], requests: {}, reviewers: [], audit: [] } };
    } };
    const settle = () => new Promise(resolve => setTimeout(resolve, 0));
    const banner = () => host.querySelector('.market-notice').textContent;
    const switchLanguage = value => {
        const before = reads;
        lang = value; tabs['sys-marketplace'].renderMarket();
        assert.equal(reads, before, 'Changing language must not retry GitHub requests');
    };
    const refresh = async () => { host.querySelector('.plugin-store-refresh').click(); await settle(); };
    open({ ipc, tabs, language: () => lang, switchTab() {},
        createTabFrame(_id, _caption, mount) { mount(host); },
        githubLogin: async () => { throw new Error(rateLimit); } });
    await settle();
    assert.match(banner(), /GitHub API rate limit exceeded/);
    switchLanguage('zh'); assert.match(banner(), /请求次数已达上限/);
    assert(!banner().includes('192.0.2.1'));
    switchLanguage('en'); assert.match(banner(), /rate limit exceeded/);
    assert.equal(reads, 1);

    mode = 'ok'; await refresh(); assert.equal(banner(), '');
    const search = host.querySelector('.plugin-store-search');
    search.value = 'retained query'; search.dispatchEvent(new Event('input'));
    host.querySelectorAll('.plugin-store-filter')[2].click(); await settle();
    mode = 'limit'; await refresh();
    assert.match(banner(), /showing the last successfully loaded data/);
    switchLanguage('zh');
    assert.match(banner(), /请求次数已达上限.*正在显示上次成功读取的数据/);
    assert.equal(host.querySelector('.plugin-store-search').value, 'retained query');
    assert.equal(host.querySelector('.plugin-store-filter.active').textContent, 'R 插件');
    switchLanguage('en'); assert.match(banner(), /rate limit exceeded.*last successfully loaded data/);

    mode = 'network'; await refresh();
    assert.match(banner(), /Cannot connect to GitHub/);
    switchLanguage('zh'); assert.match(banner(), /连接 GitHub 失败/);
    mode = 'warning'; await refresh(); assert.equal(banner(), warning);
    switchLanguage('en'); assert.match(banner(), /sign-in could not be verified/);

    // Action errors use the same live notice, without going through list loading.
    host.querySelector('.github-login-button').click(); await settle();
    assert.match(banner(), /rate limit exceeded/);
    switchLanguage('zh'); assert.match(banner(), /请求次数已达上限/);
    assert.equal(errorMessage(new Error('Unknown server detail'), 'zh'), 'Unknown server detail');
    console.log('PASS: immediate EN/ZH marketplace error switching, rate limits, network errors, stale-data suffix, account warning, action errors, retained filters and zero language-switch requests');
}

if (!process.versions.electron) {
    const directory = fs.mkdtempSync(path.join(root, 'build/market-language-test-'));
    try {
        const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
        const result = cp.spawnSync(path.join(root, 'node_modules/electron/dist/electron.exe'),
            [__filename, directory], { env, encoding: 'utf8', windowsHide: true, timeout: 30000 });
        process.stdout.write(result.stdout || ''); process.stderr.write(result.stderr || '');
        process.exitCode = result.status ?? 1;
    } finally {
        assert.equal(path.dirname(directory), path.join(root, 'build'));
        fs.rmSync(directory, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
    }
} else {
    const { app, BrowserWindow, session } = require('electron');
    app.setPath('userData', path.join(process.argv[2], 'profile'));
    app.disableHardwareAcceleration();
    app.commandLine.appendSwitch('in-process-gpu');
    app.whenReady().then(async () => {
        let window;
        try {
            session.defaultSession.webRequest.onBeforeRequest({ urls: ['http://*/*', 'https://*/*'] },
                (_request, done) => done({ cancel: true }));
            window = new BrowserWindow({ show: false, webPreferences: { nodeIntegration: true, contextIsolation: false, sandbox: false } });
            await window.loadURL('data:text/html,<meta charset="utf-8"><body></body>');
            await window.webContents.executeJavaScript(`const root=${JSON.stringify(root)}; const assert=require('node:assert/strict'), path=require('node:path'); (${checkPage.toString()})();`);
            console.log('PASS: isolated Electron marketplace language regression');
            window.destroy(); app.exit(0);
        } catch (error) {
            console.error(error.stack); if (window) window.destroy(); app.exit(1);
        }
    });
}
