'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { Writable, EventEmitter } = require('node:stream');
const { register, REPOSITORY, ADMIN_ID } = require('../src/github-market-main');
const { publicGithubCandidates, raceGithubReads } = require('../src/github-public');
const source = fs.readFileSync(path.join(__dirname, '../main.js'), 'utf8');
function section(start, end) { const from=source.indexOf(start);return source.slice(from,source.indexOf(end,from)); }
async function run() {
    const id='community_10_demo';
    const releases=[
        {tag_name:id+'-v1.0.0',assets:[{name:id+'-1.0.0.zip',download_count:9},{name:'signature.sig',download_count:100}]},
        {tag_name:id+'-v1.1.0',assets:[{name:id+'-1.1.0.zip',download_count:4}]},
        {tag_name:id+'-v1.2.0',assets:[{name:id+'.zip.part1',download_count:3},{name:id+'.zip.part2',download_count:2}]},
        {tag_name:'other-v1.0.0',assets:[{name:'other.zip',download_count:1000}]}
    ];
    const ctx=vm.createContext({console,Date,publicGithubCandidates,raceGithubReads,
        PLUGIN_REGISTRY_REPO:'owner/store',PLUGIN_INSTALL_COUNT_CACHE_TTL:1800000,
        pluginReleaseHistoryCache:{data:null,time:0},otherReleaseHistoryCaches:new Map(),
        _getPluginDownloadNetworkMode:async()=> 'cn',_fetchJson:async()=>releases});
    vm.runInContext(section('function _pluginReleaseHistoryApiCandidates(', 'function _decodePluginRegistryPayload('),ctx);
    for (const mode of ['auto', 'cn']) {
        ctx._getPluginDownloadNetworkMode = async () => mode;
        ctx._fetchJson = async (url, timeout, options) => {
            // Reproduce a fast mirror with week-old zero counts and a rate-limited origin.
            if (url.startsWith('https://z2.gh-proxy.cn/')) {
                return releases.map(release => ({ ...release, assets: release.assets.map(asset => ({ ...asset, download_count: 0 })) }));
            }
            if (new URL(url).hostname === 'api.github.com') throw Error('HTTP 403 rate limit');
            await new Promise(resolve => setTimeout(resolve, 5));
            assert.equal(options.noCache, true, 'Every statistics network read must revalidate cached responses');
            assert.equal(options.anonymous, true, 'Public statistics must not forward credentials to mirrors');
            return releases;
        };
        for (const repository of ['owner/store', REPOSITORY]) {
            const history = await ctx._fetchPluginReleaseHistory({ repository, forceRefresh: true });
            assert.equal(ctx._pluginInstallCountsFromReleases([{ id }], history.releases)[id], 15,
                `${mode}: a fast stale mirror must not win the statistics race`);
            assert.equal(history.stale, false);
        }
    }
    ctx.pluginReleaseHistoryCache = { data: null, time: 0 };
    ctx.otherReleaseHistoryCaches.clear();
    ctx._getPluginDownloadNetworkMode = async () => 'cn';
    ctx._fetchJson = async (url, timeout, options) => {
        assert.equal(options.noCache, true, 'Initial statistics reads must also revalidate');
        return releases;
    };
    const counts=ctx._pluginInstallCountsFromReleases([{id},{id:'community_10_zero'}],releases);
    assert.equal(counts[id],15);assert.equal(counts.community_10_zero,0);
    await ctx._fetchPluginReleaseHistory();
    let reads=0;
    ctx._fetchJson=async url=>{reads++;assert(url.includes(REPOSITORY));return [];};
    const first=await ctx._fetchPluginReleaseHistory({repository:REPOSITORY});
    assert.equal(first.releases.length,0,'Market must not reuse the official store cache');
    const before=reads;await ctx._fetchPluginReleaseHistory({repository:REPOSITORY});assert.equal(reads,before);
    ctx._fetchJson=async()=>{throw Error('offline');};
    assert.equal((await ctx._fetchPluginReleaseHistory({repository:REPOSITORY,forceRefresh:true})).stale,true);
    assert.equal((await ctx._fetchPluginReleaseHistory()).releases.length,4,'Official store cache remains independent');

    const handlers={};const frame={},sender={mainFrame:frame};
    const state={schema:1,administrator_id:ADMIN_ID,plugins:{[id]:{id}},requests:{},reviewers:[],blocked:[],audit:[]};
    let finishCounts, requestedForce, countCalls=0;
    register({app:{getPath:()=>'.',on(){}},ipcMain:{handle:(name,fn)=>handlers[name]=fn},
        safeStorage:{isEncryptionAvailable:()=>false},getWindow:()=>({webContents:sender}),
        getDownloadCounts:async(plugins,force)=>{countCalls++;requestedForce=force;assert.equal(plugins[0].id,id);return new Promise(resolve=>finishCounts=resolve);},
        net:{request(){const req=new Writable({write(c,e,done){done();},final(done){done();queueMicrotask(()=>{
            const res=new EventEmitter();res.statusCode=200;req.emit('response',res);
            res.emit('data',Buffer.from(JSON.stringify({content:Buffer.from(JSON.stringify(state)).toString('base64')})));res.emit('end');
        });}});req.setHeader=()=>{};req.abort=()=>{};return req;}}});
    const invoke=(name,args={})=>handlers[name]({sender,senderFrame:frame},args);
    await invoke('market-list');assert.equal(countCalls,0,'Listing must not wait for statistics');
    const pending=invoke('market-download-counts',{force:true});assert.equal(requestedForce,true);
    finishCounts({counts});assert.equal((await pending).counts[id],15);
    console.log('PASS: fresh statistics in auto/CN modes with a rate-limited origin, cumulative package counts, zero counts, independent repository caches, stale fallback and nonblocking statistics IPC');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
