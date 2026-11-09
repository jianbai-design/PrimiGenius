const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(require('node:path').join(__dirname, '../src/renderer.js'), 'utf8');
async function main() {
    let tick, finish, calls = 0;
    const polling = vm.createContext({console, setInterval: f => {tick=f; return 7;}});
    vm.runInContext(source.slice(source.indexOf('function setNonOverlappingInterval('), source.indexOf('function runtimeStateLabel(')), polling);
    assert.equal(polling.setNonOverlappingInterval(() => {calls++; return new Promise(r => finish=r);}, 100), 7);
    const running = tick(); await tick(); await tick();
    assert.equal(calls, 1); finish(); await running;
    const next = tick(); assert.equal(calls, 2); finish(); await next;
    let detached = 0, cancelled = 0;
    polling.clearInterval = () => cancelled++;
    const owner = {isConnected:false};
    polling.setNonOverlappingInterval(() => {throw Error('closed view polled');},100,owner,()=>detached++);
    await tick(); assert.equal(cancelled,1); assert.equal(detached,1);
    const requests = [];
    const cacheCode = source.slice(source.indexOf('        const _dockerPanelRequestCache ='), source.indexOf('        const fetchPodmanInfo ='));
    const cache = vm.createContext({Date, Map, AbortSignal, fetch: () => new Promise((resolve, reject) => requests.push({resolve, reject}))});
    vm.runInContext(cacheCode + '\nglobalThis.read = fetchDockerPanelJson; globalThis.invalidate = invalidateDockerPanelCache;', cache);
    const first = cache.read('settings', '/settings');
    const shared = cache.read('settings', '/settings');
    assert.equal(requests.length, 1);
    cache.invalidate('settings');
    const fresh = cache.read('settings', '/settings');
    requests[1].resolve({json: async () => 'new'});
    assert.equal(await fresh, 'new');
    requests[0].resolve({json: async () => 'old'});
    await Promise.all([first, shared]);
    assert.equal(await cache.read('settings', '/settings'), 'new');
    cache.invalidate('settings');
    const failed = cache.read('settings', '/settings').catch(() => null);
    cache.invalidate('settings');
    const newer = cache.read('settings', '/settings');
    requests[3].resolve({json: async () => 'newer'});
    await newer;
    requests[2].reject(new Error('old request failed'));
    await failed;
    assert.equal(await cache.read('settings', '/settings'), 'newer');
    const pending = new Map(); let id = 0; let visible = true;
    const containerCalls = [], imageCalls = [];
    const ctx = vm.createContext({String, setTimeout: f => {pending.set(++id, f); return id;}, clearTimeout: i => pending.delete(i),
        loadContainerList: force => containerCalls.push(force), loadLocalImages: () => imageCalls.push(true),
        statusResources: {'r-runtime': {invalidate() {}}},
        _dockerEventsNeeded: () => visible});
    const events = source.slice(source.indexOf('        function _onDockerEvent(evt) {'), source.indexOf('        function _startDockerEventsListener()'));
    vm.runInContext('let _dockerEventsDebounce=null, _pendingContainerRefresh=false, _pendingImageRefresh=false, _pendingForceFresh=false, _lastContainersData={}, _lastContainersTime=1;\n' + events, ctx);
    const flush = () => {const list = [...pending.values()];pending.clear();list.forEach(f => f());};
    ctx._onDockerEvent({Type:'container', Action:'die'});
    for (let i=0;i<100;i++) ctx._onDockerEvent({Type:'container',Action:'start'});
    assert.equal(pending.size,1); flush();
    assert.deepEqual(containerCalls,[true]); assert.equal(imageCalls.length,0);
    ctx._onDockerEvent({Type:'image',Action:'pull'}); flush();
    assert.equal(containerCalls.length,1); assert.equal(imageCalls.length,1);
    ctx._onDockerEvent({Type:'image',Action:'tag'}); visible=false; flush();
    assert.equal(imageCalls.length,1);
    {
    const {createStatusResource, createViewStatusRegistry} = require('../src/status-state');
    const queue = []; let reads = 0;
    const resource = createStatusResource(() => {reads++; return new Promise((resolve,reject) => queue.push({resolve,reject}));});
    const checks = Array.from({length:20}, () => resource.read());
    await Promise.resolve(); assert.equal(reads,1);
    resource.invalidate(); const fresh = resource.read(); await Promise.resolve();
    queue[1].resolve('installed'); assert.equal(await fresh,'installed');
    queue[0].resolve('missing');
    assert.deepEqual(await Promise.all(checks), Array(20).fill('installed'));
    assert.equal(await resource.read(),'installed'); assert.equal(reads,2);
    resource.invalidate(); const failed = resource.read(); await Promise.resolve();
    resource.invalidate(); const newest = resource.read(); await Promise.resolve();
    queue[3].resolve('deleted'); queue[2].reject(new Error('stale failure'));
    assert.equal(await failed,'deleted'); assert.equal(await newest,'deleted');
    resource.invalidate(); const error = resource.read(); await Promise.resolve();
    queue[4].reject(new Error('offline')); await assert.rejects(error,/offline/);
    const recovered = resource.read(); await Promise.resolve();
    queue[5].resolve('reconnected'); assert.equal(await recovered,'reconnected');
    const flushTasks = () => new Promise(resolve => setImmediate(resolve));
    const views = [{isConnected:true,active:true,contains:()=>false},{isConnected:true,active:false,contains:()=>false}];
    const paints=[0,0], disposed=[0,0]; let complete;
    const registry=createViewStatusRegistry(view=>view.active);
    views.forEach((view,i)=>registry.bind(view,['packages'],()=>{paints[i]++; if(i===0 && paints[i]===1)return new Promise(resolve=>complete=resolve);},()=>disposed[i]++));
    registry.refresh(); await flushTasks(); assert.deepEqual(paints,[1,0]);
    for(let i=0;i<100;i++)registry.refresh(['packages']);
    await flushTasks(); assert.deepEqual(paints,[1,1]);
    complete(); await flushTasks(); assert.deepEqual(paints,[2,1]);
    registry.dispose(views[0]); registry.refresh(['packages']); await flushTasks();
    assert.deepEqual(paints,[2,2]); assert.deepEqual(disposed,[1,0]);
    views[1].isConnected=false; registry.refresh(); assert.deepEqual(disposed,[1,1]);
    registry.dispose(); assert.deepEqual(disposed,[1,1]);
    const runState = vm.createContext({console, _tabRunState:new Map(),runLogStreams:new Map(),currentLang:'zh'});
    vm.runInContext(source.slice(source.indexOf('function _restoreTabRunState('),source.indexOf('function switchTab(')), runState);
    const button={disabled:false,style:{}};
    runState._tabRunState.set('one',{runId:'job',btnEl:button,type:'r'}); runState.runLogStreams.set('job',{});
    runState._restoreTabRunState('one'); assert.equal(button.disabled,true);
    }
    {
    const labels = {running:['运行中','Running'],created:['已创建','Created'],exited:['已退出','Exited'],stopped:['已停止','Stopped'],
        dead:['已死亡','Dead'],paused:['已暂停','Paused'],restarting:['重启中','Restarting'],removing:['删除中','Removing']};
    const badges = Object.keys(labels).flatMap(status => [0,1].map(() => ({
        classList:['docker-container-status-badge','status-'+status],textContent:labels[status][0],status
    })));
    const statuses = vm.createContext({currentLang:'zh',dockerView:{querySelectorAll:()=>badges}});
    vm.runInContext(source.slice(source.indexOf('function containerStatusLabel('),source.indexOf('function updateLanguage(')),statuses);
    const switchStart=source.indexOf("        dockerView.querySelectorAll('.docker-container-status-badge').forEach");
    const switchCode=source.slice(switchStart,source.indexOf("        if (typeof dockerView.__updateContainerDetailI18n",switchStart));
    for(const lang of ['en','zh','en']){
        statuses.currentLang=lang;vm.runInContext(switchCode,statuses);
        for(const badge of badges)assert.equal(badge.textContent,labels[badge.status][lang==='zh'?0:1]);
    }
    assert.equal(statuses.containerStatusLabel('future-state'),'future-state');
    let finishList,rendered;
    Object.assign(statuses,{console,AbortSignal,auxApiUrl:url=>url,t:key=>key,
        containerList:{querySelector:()=>({})},_lastContainersData:null,_lastContainersTime:0,
        fetch:()=>new Promise(resolve=>finishList=resolve),
        _renderContainers:(data,zh)=>rendered={zh,label:statuses.containerStatusLabel(data.containers[0].status)}});
    vm.runInContext(source.slice(source.indexOf('        async function _readContainerList()'),source.indexOf('        async function loadContainerStats(')),statuses);
    statuses.currentLang='zh';const listRead=statuses._readContainerList();statuses.currentLang='en';
    finishList({json:async()=>({status:'success',containers:[{status:'running'}]})});await listRead;
    assert.deepEqual(rendered,{zh:false,label:'Running'},'A slow refresh must render the current language');
    let finishDetail;const detail={innerHTML:''};
    statuses.containerList.querySelector=()=>detail;
    statuses.fetch=()=>new Promise(resolve=>finishDetail=resolve);
    statuses.escapeHtml=String;
    vm.runInContext(source.slice(source.indexOf('        async function loadContainerDetail('),source.indexOf('        function stopContainerLogStream(')),statuses);
    statuses.currentLang='zh';const detailRead=statuses.loadContainerDetail('abcdefghijkl');statuses.currentLang='en';
    finishDetail({json:async()=>({status:'success',container:{name:'example',image:'example:latest',runtime:{status:'running'}}})});await detailRead;
    assert.match(detail.innerHTML,/>Running</);assert.doesNotMatch(detail.innerHTML,/运行中/);
    console.log('PASS: container status switches immediately in cards/details; slow list/detail responses preserve the current language');
    }
    {
    const flush = () => new Promise(resolve => setImmediate(resolve));
    let finishMeta, metaCalls = 0, painted = [], pins = [], fetches = [];
    const feedCache = new Map();
    const community = vm.createContext({console, currentLang:'zh', currentSource:'discussion', currentCategoryId:'all', currentIssueState:'all',
        categoryList:[], categoryRequest:null, feedRequestId:0, _ghCommunityMeta:null, _ghPinnedDiscussionCache:{numbers:[]},
        contentEl:{isConnected:true}, listEl:{style:{},innerHTML:'',querySelector:()=>null}, detailEl:{style:{}},
        _ghGetToken:()=> 'isolated-test-token', _ghGetNetworkMode:async()=> 'cn',
        _ghNetworkPolicyByMode:()=>({perPage:12}), GITHUB_COMMUNITY:{owner:'owner',repo:'repo',perPage:20},
        _ghLoadCommunityMeta:()=> {metaCalls++; return new Promise(resolve => finishMeta=meta => {community._ghCommunityMeta=meta;resolve(meta);});},
        _ghLoadPinnedDiscussionNumbers:()=>new Promise(resolve=>pins.push(resolve)),
        _ghFetch:()=>new Promise(resolve=>fetches.push(items=>resolve({ok:true,json:async()=>items}))), _ghHeaders:()=>({}),
        renderCategorySelect:()=>{}, renderCategoryFilters:()=>{}, syncFormMode:()=>{}, handleScopeError:()=>{}, t:String,
        renderDiscussionRows:items=>painted=items, renderIssueRows:items=>painted=items,
        getFeedCache:(key,force)=>force?null:feedCache.get(key), setFeedCache:(key,items)=>feedCache.set(key,items), clearFeedCache:()=>feedCache.clear(),
        discussionRankScore:d=>d.pinned?2000:0});
    vm.runInContext(source.slice(source.indexOf('        async function ensureDiscussionCategories()'),source.indexOf('        function renderCategoryFilters()'))
        +source.slice(source.indexOf('        async function loadIssueList('),source.indexOf('        tabs[tabId].refreshFeed =')),community);
    const discussions=[{number:1,title:'older',updated_at:'2026-10-01'}, {number:2,title:'newer',updated_at:'2026-10-02',pinned:true}];
    const reading=community.loadDiscussionList(false);await flush();
    assert.equal(fetches.length,1,'Slow metadata must not delay the public list request');
    fetches.shift()(discussions);await flush();
    assert.equal(painted.length,2,'Rows must appear while category and pin requests remain pending');
    await reading;assert.equal(metaCalls,1);
    const categoriesAgain=community.ensureDiscussionCategories();assert.equal(metaCalls,1,'Share the pending category request');
    finishMeta({categories:[{id:'category',name:'General'}]});await categoriesAgain;
    pins.shift()([1]);await flush();
    assert.equal(painted.find(d=>d.number===1).pinned,true);
    assert.equal(painted.find(d=>d.number===2).pinned,true,'Keep the API pin flag');
    const refreshing=community.loadDiscussionList(true);await flush();fetches.shift()(discussions);await refreshing;
    community.currentSource='issue';const issues=community.loadFeed(true);await flush();
    fetches.shift()([{number:3,title:'issue'}]);await issues;
    pins.shift()([1]);await flush();assert.equal(painted[0].number,3,'Late pin results must not overwrite another feed');
    community.currentSource='discussion';const oldRead=community.loadFeed(true);await flush();
    community.currentSource='issue';const newRead=community.loadFeed(true);await flush();
    const oldFetch=fetches.shift(),newFetch=fetches.shift();newFetch([{number:4,title:'latest'}]);await newRead;
    oldFetch(discussions);await oldRead;assert.equal(painted[0].number,4,'Late list results must not overwrite a newer request');
    let graphCalls=0,htmlCalls=0;
    const pinned=vm.createContext({Date,Number,currentLang:'zh',_ghPinnedDiscussionCache:{ts:0,numbers:[]},
        _ghGetToken:()=> 'isolated-test-token', GITHUB_COMMUNITY:{},
        _ghGraphQL:async()=>{graphCalls++;return {repository:{pinnedDiscussions:{nodes:[]}}};},
        _fetchTextViaHttps:async()=>{htmlCalls++;throw Error('Unnecessary HTML request');}});
    vm.runInContext(source.slice(source.indexOf('async function _ghLoadPinnedDiscussionNumbers('),source.indexOf('// GitHub Device Flow login')),pinned);
    await pinned._ghLoadPinnedDiscussionNumbers(false);await pinned._ghLoadPinnedDiscussionNumbers(false);
    assert.equal(graphCalls,1);assert.equal(htmlCalls,0,'An empty successful pin result is cached and needs no HTML fallback');
    const {EventEmitter}=require('node:events');let timeout,cleared=0;
    const req=new EventEmitter();req.end=()=>{};req.destroy=error=>req.emit('error',error);
    const html=vm.createContext({Date,URL,require:()=>({request:()=>req}),setTimeout:fn=>{timeout=fn;return 1;},clearTimeout:()=>cleared++});
    vm.runInContext(source.slice(source.indexOf('function _fetchTextViaHttps('),source.indexOf('\nfunction ',source.indexOf('function _fetchTextViaHttps(')+10)),html);
    const stuck=html._fetchTextViaHttps('https://github.com/example');timeout();
    await assert.rejects(stuck,/timed out/);assert.equal(cleared,1);
    console.log('PASS: community rows bypass slow metadata/pins, pending metadata is shared, stale results are ignored, empty pins are cached, HTML fallback times out');
    }
    const nodes = [];
    const box = {appendChild: node => {nodes.push(node); node.remove = () => {const i=nodes.indexOf(node); if(i>=0)nodes.splice(i,1);};}};
    const logView = vm.createContext({LOG_COMPACT_THRESHOLD: 1200, scheduleLogCompaction: () => {}, scheduleLogScrollToBottom: () => {}});
    vm.runInContext(source.slice(source.indexOf('function appendLogNodeToBox('), source.indexOf('function getLogNodeText(')), logView);
    for (let i=0; i<2000; i++) logView.appendLogNodeToBox(box, {dataset:{archivedRunId:'r'}});
    assert.equal(nodes.length, 1200);
    for (let i=0; i<100; i++) logView.appendLogNodeToBox(box, {dataset:{}});
    assert.equal(nodes.length, 1300); // never discard UI-only messages with no archive
    assert.equal(box._archivedLogNodes.length, 1200);
    console.log('PASS: shared requests, stale success/failure isolation, 101-event coalescing, terminal-state retention, selective refresh, hidden-view suppression, R read coalescing, archive-only DOM eviction');
}
main().catch(e => {console.error(e); process.exitCode=1;});
