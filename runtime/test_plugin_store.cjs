'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const { publicGithubCandidates, raceGithubReads } = require('../src/github-public');
const main = fs.readFileSync(path.join(__dirname, '../main.js'), 'utf8');
const renderer = fs.readFileSync(path.join(__dirname, '../src/renderer.js'), 'utf8');
const section = (text, start, end) => {
    const from = text.indexOf(start), to = text.indexOf(end, from + start.length);
    assert.ok(from >= 0 && to > from, start);
    return text.slice(from, to);
};
const flush = () => new Promise(resolve => setImmediate(resolve));
async function run() {
    const reads = [];
    let route = url => url.startsWith('https://gh-proxy.com/') ? '<html>error</html>'
        : url.startsWith('https://gh-proxy.org/') ? { plugins: [{ id: 'demo', version: '1.0' }] } : undefined;
    const transport = vm.createContext({console: {log() {}}, URL, Buffer, AbortController, setTimeout, clearTimeout,
        publicGithubCandidates, raceGithubReads, _getPluginDownloadNetworkMode: async () => 'cn',
        process: {env: {GITHUB_TOKEN: 'isolated-test-token'}},
        https: {get(url, options, respond) {
            const req = new EventEmitter(); req.setTimeout = () => {};
            req.destroy = error => { req.destroyed = true; req.emit('error', error); };
            reads.push({url, options, req});
            setTimeout(() => {
                const payload = route(url);
                if (req.destroyed || payload === undefined) return;
                const res = new EventEmitter(); res.statusCode = 200; res.headers = {};
                res.setEncoding = () => {}; res.resume = () => {};
                respond(res); res.emit('data', typeof payload === 'string' ? payload : JSON.stringify(payload)); res.emit('end');
            }, 5);
            return req;
        }}});
    vm.runInContext(section(main, 'function _fetchJson(', 'function _pluginReleaseHistoryApiCandidates(')
        + section(main, 'function _decodePluginRegistryPayload(', 'async function _fetchFreshestPluginRegistryCandidate('), transport);
    const raw = 'https://raw.githubusercontent.com/owner/repo/main/plugins-registry-cli.json';
    const result = await transport._fetchFirstPluginRegistryCandidate([raw], 200, 'test');
    assert.equal(result.registry.plugins[0].id, 'demo');
    assert.ok(result.source.startsWith('https://gh-proxy.org/'), 'Reject the fastest invalid response');
    assert.ok(reads.some(read => read.req.destroyed), 'Cancel losing requests');
    assert.ok(reads.every(read => !read.options.headers.Authorization), 'Public racing never carries a token');
    route = () => ({});
    await transport._fetchJson('https://mirror.example/https://api.github.com/repos/owner/repo');
    assert.equal(reads.at(-1).options.headers.Authorization, undefined, 'An embedded API hostname is not the official origin');
    await transport._fetchJson('https://api.github.com/repos/owner/repo');
    assert.equal(reads.at(-1).options.headers.Authorization, 'Bearer isolated-test-token');
    await transport._fetchJson('https://api.github.com/repos/owner/repo', 200, {anonymous:true});
    assert.equal(reads.at(-1).options.headers.Authorization, undefined);
    route = () => undefined;
    await assert.rejects(transport._fetchJson(raw, 10), /timeout/);
    const controller = new AbortController(); controller.abort();
    await assert.rejects(transport._fetchJson(raw, 200, {signal:controller.signal}), {name:'AbortError'});
    transport.UPDATE_MIRROR_RELEASE_API_CANDIDATES = ['https://api.github.com/repos/owner/repo/releases/latest'];
    transport.PLUGIN_RELEASE_API_CANDIDATES = transport.UPDATE_MIRROR_RELEASE_API_CANDIDATES;
    vm.runInContext(section(main, 'async function _checkLatestReleaseViaMirrors(', 'async function _emitMirrorFallbackUpdateIfNeeded(')
        +section(main, 'async function _fetchReleaseNotes()', '// 检查插件更新')
        +section(main, 'function _pluginReleaseApiCandidates(', 'function _releaseNotesFromUpdates('),transport);
    route = url => url.startsWith('https://gh-proxy.com/https://api.github.com/') ? {tag_name:'v1.2.3',body:'Public release notes'} : undefined;
    const update = await transport._checkLatestReleaseViaMirrors();
    assert.ok(update.apiUrl.startsWith('https://gh-proxy.com/'));
    assert.equal(update.data.tag_name,'v1.2.3');
    assert.equal(await transport._fetchReleaseNotes(),'Public release notes');
    assert.equal(await transport._fetchReleaseNotesForTag('demo-v1.2.3'),'Public release notes');
    assert.ok(reads.filter(read=>read.options.headers.Authorization).every(read=>new URL(read.url).origin==='https://api.github.com'));

    const wrapper = fs.readFileSync(path.join(__dirname, '../build/electron-builder-wrapper.js'),'utf8');
    const toolURL='https://github.com/electron-userland/electron-builder-binaries/releases/download/winCodeSign-2.6.0/tool.7z';
    const toolReads=[];
    const tools=vm.createContext({publicGithubCandidates,path,PROJECT_DIR:'.',TOOL_URL:toolURL,TOOL_ARCHIVE_PATH:'tool.7z',
        fs:{readFileSync:()=>'{"mode":"cn"}'},downloadFile:async url=>{
            toolReads.push(url);if(url.startsWith('https://gh-proxy.com/'))throw Error('Mirror unavailable');
        }});
    vm.runInContext(section(wrapper,'async function downloadToolArchive()', 'function extractArchive('),tools);
    await tools.downloadToolArchive();
    assert.deepEqual(toolReads,['https://gh-proxy.com/'+toolURL,toolURL]);
    let connectionTimeout,removed=0;
    const stalledTool=new EventEmitter();stalledTool.destroy=error=>stalledTool.emit('error',error);
    const toolTransport=vm.createContext({path,ensureDirectory:()=>{},fs:{rmSync:()=>removed++},
        https:{get:()=>stalledTool},clearTimeout:()=>{},setTimeout:(callback,ms)=>{assert.equal(ms,10000);connectionTimeout=callback;return 1;}});
    vm.runInContext(section(wrapper,'function downloadFile(', 'async function downloadToolArchive()'),toolTransport);
    const toolRead=toolTransport.downloadFile(toolURL,'tool.7z');connectionTimeout();
    await assert.rejects(toolRead,/timed out/);assert.equal(removed,1);

    const history = vm.createContext({console: {log() {}}, Date, publicGithubCandidates, raceGithubReads,
        PLUGIN_REGISTRY_REPO:'owner/repo', PLUGIN_INSTALL_COUNT_CACHE_TTL:1000,
        pluginReleaseHistoryCache:{data:null,time:0}, _getPluginDownloadNetworkMode:async () => 'cn',
        _fetchJson:async url => {
            if (!url.startsWith('https://v6.gh-proxy.org/')) return {invalid:true};
            return url.endsWith('page=1') ? Array.from({length:100}, (_,id)=>({id})) : [{id:100}];
        }});
    vm.runInContext(section(main, 'function _pluginReleaseHistoryApiCandidates(', 'function _pluginIdForRelease('), history);
    assert.equal((await history._fetchPluginReleaseHistory()).releases.length,101, 'Keep paginated download counts');
    history._fetchJson = async () => {throw Error('offline');};
    const cached = await history._fetchPluginReleaseHistory({forceRefresh:true});
    assert.equal(cached.stale,true);assert.equal(cached.releases.length,101);
    const fresh = vm.createContext({console:{log() {}}, Date, _uniqueUrls:urls=>urls,
        _fetchJson:async url=>({generated_at:url==='old'?'2025-01-01':'2026-01-01',plugins:[]})});
    vm.runInContext(section(main, 'function _decodePluginRegistryPayload(', 'function _splitRegistryCandidateSets('), fresh);
    assert.equal((await fresh._fetchFreshestPluginRegistryCandidate(['old','new'],100,'test')).source,'new');

    const handlers = {}; let finishHistory, historyCalls = 0;
    const registry = {plugins:[{id:'demo',version:'1.0',registry_kind:'cli'}]};
    const ipc = vm.createContext({Date, ipcMain:{handle:(name,handler)=>handlers[name]=handler},
        pluginRegistryCache:{data:registry,time:Date.now()}, pluginReleaseHistoryCache:{data:null,time:0},
        PLUGIN_INSTALL_COUNT_CACHE_TTL:1000, app:{getVersion:()=> '1.1.8'},
        _fetchPluginRegistry:async()=>registry, _getLocalPlugins:async()=>({}), _normalizeVersion:String, _isNewerVersion:()=>false,
        _fetchPluginReleaseHistory:()=>{historyCalls++;return new Promise(resolve=>finishHistory=resolve);},
        _pluginInstallCountsFromReleases:()=>({demo:42})});
    vm.runInContext(section(main, "ipcMain.handle('plugin-store-install-counts'", 'app.whenReady().then(async () => {'),ipc);
    const listing = await handlers['plugin-store-list']({},{});
    assert.equal(listing.plugins.length,1); assert.equal(historyCalls,0,'Do not fetch history before showing cards');
    const counting = handlers['plugin-store-install-counts']({},{});
    assert.equal(historyCalls,1); finishHistory({releases:[]});
    assert.equal((await counting).counts.demo,42);

    const counts = []; let renders=0;
    const view = vm.createContext({tabs:{store:{}}, tabId:'store', t:String, escapeHtml:String,activeStoreInstallCount:0,
        syncChrome:()=>{}, renderStore:()=>renders++, listEl:{innerHTML:''},
        ipcRenderer:{invoke:async name => name==='plugin-store-list'
            ? {ok:true,plugins:[{id:'demo',install_count:null}]}
            : new Promise(resolve=>counts.push(resolve))}});
    vm.runInContext(section(renderer, '        async function loadStore(', '        tabs[tabId].renderStore ='),view);
    await view.loadStore(); assert.equal(renders,1,'Cards render before statistics resolve');
    counts.shift()({ok:true,counts:{demo:42}});await flush();
    assert.equal(view.tabs.store.storePlugins[0].install_count,42);assert.equal(renders,2);
    await view.loadStore(); const oldCounts=counts.shift();
    await view.loadStore(); oldCounts({ok:true,counts:{demo:999}});await flush();
    assert.equal(view.tabs.store.storePlugins[0].install_count,null,'Ignore statistics for an older listing');
    counts.shift()({ok:false});await flush();assert.equal(view.tabs.store.storePlugins.length,1);
    await view.loadStore(); const beforeCounts=renders; view.activeStoreInstallCount=1;
    counts.shift()({ok:true,counts:{demo:42}});await flush();
    assert.equal(renders,beforeCounts,'Statistics must not rebuild an active installation button');
    assert.equal(view.tabs.store.storePlugins[0].install_count,42);
    console.log('PASS: accelerated catalog/update/notes reads validate winners and isolate credentials; build tools time out and fall back; store cards precede optional counts');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
