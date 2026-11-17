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
    // Installation state survives success, failed retries, rerenders and language changes.
    const installIPC=new (require('events').EventEmitter)(),installTabs={},installHost=document.createElement('main');document.body.append(installHost);
    const repository='jianbai-design/PrimiGenius-community-plugins',origin={source:'github:'+repository,owner_github_id:10};
    const versions={fresh:null,update:'1.0.0',same:'2.0.0',newer:'3.0.0',conflict:'2.0.0',unknown:'invalid',race:null};
    const plugins={},installed={};let installCalls=0,pluginRefreshes=0,failInstall=false,failInstalled=false;
    for(const [name,version]of Object.entries(versions)){
        const id='community_10_'+name;plugins[id]={id,listed:true,owner:{id:10,login:'author'},collaborators:[],metadata:{name,type:'cli'},published:{version:'2.0.0'}};
        if(version)installed[id]={id,version,marketplace:{...origin}};
    }
    installed.community_10_conflict.marketplace.source='github:other/store';
    installIPC.invoke=async(channel,args)=>{
        if(channel==='market-list')return {user:null,repository,state:{plugins,requests:{},blocked:[],reviewers:[],audit:[]}};
        if(channel==='market-download-counts')return {ok:true,counts:{}};
        if(channel==='market-installed'){if(failInstalled)throw Error('local read failed');return {ok:true,plugins:structuredClone(installed)};}
        if(channel==='market-install'){
            installCalls++;if(failInstall)throw Error('install failed');
            installed[args.pluginId]={id:args.pluginId,version:'2.0.0',marketplace:{...origin}};
            return {ok:true,id:args.pluginId,version:'2.0.0',skipped:args.pluginId.endsWith('_race')};
        }
        throw Error('Unexpected install IPC: '+channel);
    };
    const settleInstall=async()=>{for(let i=0;i<6;i++)await settle();};
    const installButton=name=>installHost.querySelector('[data-market-install="community_10_'+name+'"]');
    lang='zh';open({ipc:installIPC,tabs:installTabs,language:()=>lang,switchTab(){},createTabFrame(_id,_caption,mount){mount(installHost);},refreshPlugins:async()=>{pluginRefreshes++;}});await settleInstall();
    for(const [name,caption]of Object.entries({fresh:'安装',update:'更新',same:'已安装',newer:'本地版本较新',conflict:'来源冲突',unknown:'本地版本未知'}))assert.equal(installButton(name).textContent,caption);
    for(const name of ['same','newer','conflict','unknown']){assert.equal(installButton(name).disabled,true);installButton(name).click();}
    assert.equal(installCalls,0);installButton('fresh').click();await settleInstall();
    assert.equal(installCalls,1);assert.equal(pluginRefreshes,1);assert.equal(installButton('fresh').textContent,'已安装');assert.equal(installButton('fresh').disabled,true,'Action wrapper re-enabled an installed plugin');
    installButton('fresh').click();await settleInstall();assert.equal(installCalls,1);
    failInstall=true;installButton('update').click();await settleInstall();assert.equal(installButton('update').textContent,'更新');assert.equal(installButton('update').disabled,false);
    assert.equal(installIPC.listenerCount('market-install-progress'),0);
    failInstall=false;installButton('update').click();await settleInstall();assert.equal(installButton('update').disabled,true);assert.equal(installButton('update').textContent,'已安装');
    installButton('race').click();await settleInstall();assert.equal(installButton('race').disabled,true);assert.match(installHost.querySelector('.market-notice').textContent,/跳过重复安装/);
    lang='en';installTabs['sys-marketplace'].renderMarket();assert.equal(installButton('fresh').textContent,'Installed');assert.equal(installButton('newer').textContent,'Newer version installed');
    assert.equal(installButton('same').disabled,true);lang='zh';installTabs['sys-marketplace'].renderMarket();assert.equal(installButton('same').textContent,'已安装');
    failInstalled=true;installHost.querySelector('.plugin-store-refresh').click();await settleInstall();assert.equal(installButton('same').disabled,true);assert.match(installButton('same').textContent,/检查失败/);
    failInstalled=false;installHost.querySelector('.plugin-store-refresh').click();await settleInstall();assert.equal(installButton('same').textContent,'已安装');
    console.log('PASS: marketplace install/update/installed/newer/source/unknown labels, duplicate-click prevention, post-install disabled state, retry, stale-state no-op, EN/ZH rerenders and local-read recovery');
    // Use the actual upload dialog and every progress stage emitted by the main process.
    const uploadIPC = new (require('events').EventEmitter)(), uploadTabs = {}, uploadHost = document.createElement('main'); document.body.append(uploadHost);
    let listReads = 0, submits = 0, rejectUpload;
    uploadIPC.invoke = async channel => {
        if (channel === 'market-download-counts') return { ok: true, counts: {} };
        if (channel === 'market-list') { listReads++; return { user: { id: 134767426, login: 'admin', role: 'admin' },
            state: { plugins: {}, blocked: [], requests: {}, reviewers: [], audit: [] } }; }
        if (channel === 'market-select-source') return { id: 'fixture', name: 'plugin.zip', size: 1024, sha256: 'a'.repeat(64) };
        if (channel === 'market-submit') { submits++; return new Promise((_resolve, reject) => rejectUpload = reject); }
        if (channel === 'market-discard-source') return { ok: true };
        throw new Error('Unexpected upload IPC: ' + channel);
    };
    lang = 'en';
    open({ ipc: uploadIPC, tabs: uploadTabs, language: () => lang, switchTab() {}, createTabFrame(_id, _caption, mount) { mount(uploadHost); } });
    await settle();
    [...uploadHost.querySelectorAll('button')].find(button => button.textContent === 'Upload plugin').click();
    const dialog = document.querySelector('.market-upload-dialog'); assert(dialog);
    [...dialog.querySelectorAll('button')].find(button => button.textContent === 'Choose ZIP').click(); await settle();
    dialog.querySelector('.market-version-row input').value = '2.0.1';
    dialog.querySelector('textarea').value = '优化界面参数排版';
    [...dialog.querySelectorAll('button')].find(button => button.textContent === 'Upload and submit').click(); await settle();
    assert(rejectUpload);
    const source = require('fs').readFileSync(path.join(root, 'src/github-market-main.js'), 'utf8');
    const stages = [...new Set([...source.matchAll(/progress\('([^']+)'\)/g)].map(match => match[1]))];
    assert.equal(stages.length, 10);
    const beforeLanguageSwitch = listReads;
    for (const stage of stages) {
        uploadIPC.emit('market-progress', {}, { selection: 'fixture', text: stage + '…' });
        const english = dialog.querySelector('.market-dialog-status').textContent;
        assert(!/[\u3400-\u9fff]/.test(english), 'Untranslated upload stage: ' + english);
        assert(english.endsWith('…'));
        lang = 'zh'; uploadTabs['sys-marketplace'].renderMarket();
        assert.equal(dialog.querySelector('.market-dialog-status').textContent, stage + '…');
        lang = 'en'; uploadTabs['sys-marketplace'].renderMarket();
        assert.equal(dialog.querySelector('.market-dialog-status').textContent, english, 'Current upload stage changes language without waiting for another event');
    }
    assert.equal(listReads, beforeLanguageSwitch, 'Stage localization must not retry network operations'); assert.equal(submits, 1);
    assert.equal(dialog.querySelector('textarea').value, '优化界面参数排版', 'User change notes are not translated');
    const latest = dialog.querySelector('.market-dialog-status').textContent;
    uploadIPC.emit('market-progress', {}, { selection: 'another-upload', text: '本地准备安装包…' });
    assert.equal(dialog.querySelector('.market-dialog-status').textContent, latest);
    rejectUpload(new Error("Error invoking remote method 'market-submit': Error: 同步可浏览的源码与滚动预览索引失败：retry test")); await settle();
    assert.match(dialog.querySelector('.market-dialog-status').textContent, /^Syncing browsable source and scrolling preview index failed: retry test$/);
    dialog.querySelector('.market-close').click(); await settle();
    await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    assert.equal(uploadIPC.listenerCount('market-progress'), 0);
    console.log('PASS: all 10 upload stages in EN/ZH, immediate language switching, localized failure context, preserved user notes, isolated upload events and listener cleanup');
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
