'use strict';
const {app,BrowserWindow,ipcMain}=require('electron');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),output=path.resolve(process.argv[2]||'');
if(path.dirname(output)!==root||!path.basename(output).startsWith('.tmp-vector-'))throw Error('A disposable workspace directory is required');
fs.mkdirSync(output,{recursive:true});app.setPath('userData',path.join(output,'profile'));
for(const flag of ['no-sandbox','disable-gpu','in-process-gpu'])app.commandLine.appendSwitch(flag);
ipcMain.on('get-backend-port-sync',e=>e.returnValue=65534);
require('../src/vector-export-main').register({app,ipcMain,BrowserWindow,dialog:{showSaveDialog:async()=>({canceled:true})}});
let win;const report={checks:[],errors:[]};const timeout=setTimeout(()=>finish(Error('Guide acceptance timed out')),150000);
function finish(error){clearTimeout(timeout);if(error)report.errors.push(error.stack||String(error));report.result=report.errors.length?'FAIL':'PASS';fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));if(win&&!win.isDestroyed())win.destroy();app.exit(report.errors.length?1:0);}
const ev=code=>win.webContents.executeJavaScript(code),pause=ms=>new Promise(r=>setTimeout(r,ms||45));
const tick=()=>ev('new Promise(r=>requestAnimationFrame(r))');
async function wait(code){for(let i=0;i<150;i++){if(await ev(code))return;await pause();}throw Error('Timed out: '+code);}
async function mouse(type,p,more={}){win.webContents.sendInputEvent({type,x:Math.round(p.x),y:Math.round(p.y),button:'left',clickCount:1,...more});}
async function pointClick(p){for(const type of ['mouseMove','mouseDown','mouseUp'])await mouse(type,p);await pause();await tick();}
async function click(selector){const p=await ev(`(()=>{const el=document.querySelector(${JSON.stringify(selector)}),r=el.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;return {x,y,hit:el.contains(document.elementFromPoint(x,y))};})()`);assert.ok(p.hit,'Obstructed: '+selector);await pointClick(p);}
async function key(keyCode){win.webContents.sendInputEvent({type:'keyDown',keyCode});win.webContents.sendInputEvent({type:'keyUp',keyCode});await pause();await tick();}
async function rulers(){await click('.ve-advanced-menu summary');await click('[data-command=rulers]');}
const guides=()=>ev("JSON.parse(decodeURIComponent(document.querySelector('.r-ve-viewport>svg:first-child').getAttribute('data-ve-guides')||'%5B%5D'))");
const selected=()=>ev("document.querySelectorAll('.ve-ruler-guide[data-selected=true]').length");
async function guidePoint(index,offset=0){return ev(`(()=>{const svg=document.querySelector('.r-ve-viewport>svg:first-child'),g=JSON.parse(decodeURIComponent(svg.getAttribute('data-ve-guides')))[${index}],m=svg.getScreenCTM(),x=g.axis==='x'?g.value:250,y=g.axis==='y'?g.value:180;return{x:m.a*x+m.c*y+m.e+(g.axis==='x'?${offset}:0),y:m.b*x+m.d*y+m.f+(g.axis==='y'?${offset}:0)};})()`);}
async function createGuide(axis,value){const p=await ev(`(()=>{const r=document.querySelector('.r-ve-canvas-wrap').getBoundingClientRect(),m=document.querySelector('.r-ve-viewport>svg:first-child').getScreenCTM(),x=${axis==='x'?value:250},y=${axis==='y'?value:180};return {start:{x:r.left+${axis==='x'?8:100},y:r.top+${axis==='y'?8:100}},end:{x:m.a*x+m.c*y+m.e,y:m.b*x+m.d*y+m.f}};})()`);await mouse('mouseMove',p.start);await mouse('mouseDown',p.start);await mouse('mouseMove',p.end);await mouse('mouseUp',p.end);await pause();await tick();}
async function open(svg,live){await ev(`currentLang='zh';window.__editorStandalone=${live};window.qaGuideInstance={config:{id:'guides',name:'guides',parameters:[],packages:[]},__standaloneMode:true,_liveEditorSessions:new Map(),outDir:''};renderRPanelUI(qaGuideInstance,document.createElement('div'));qaGuideInstance.__openVectorEditor({name:'guides.svg',path:'',ext:'.svg'},{sessionKey:'guide-'+Date.now(),liveMode:${live},initialSvgText:${JSON.stringify(svg)}});true`);await pause(150);await tick();}
async function close(){await ev("window.__editorStandalone=false;document.querySelector('.r-ve-close-btn').click();true");await pause();if(await ev("!!document.querySelector('.ve-confirm')"))await click('.ve-confirm [data-choice="1"]');await wait("!document.querySelector('.r-vector-editor-overlay')");}
app.whenReady().then(async()=>{try{
    win=new BrowserWindow({show:false,width:1320,height:960,webPreferences:{nodeIntegration:true,contextIsolation:false,backgroundThrottling:false}});await win.loadFile(path.join(root,'src/editor.html'));
    await ev("(()=>{window.qaGuideSnapshots=0;window.qaGuideSerialize=XMLSerializer.prototype.serializeToString;XMLSerializer.prototype.serializeToString=function(...args){qaGuideSnapshots++;return qaGuideSerialize.apply(this,args)};})()");
    const fixture='<svg xmlns="http://www.w3.org/2000/svg" width="600" height="400"><style>line{stroke:#000!important;stroke-width:9px!important}</style><rect id="a" x="50" y="60" width="100" height="60" fill="#0088cc"/><line id="artworkLine" x1="80" y1="300" x2="180" y2="300" stroke="#000"/></svg>';
    for(const live of process.argv.includes('--live-only')?[true]:[false,true]){
        await open(fixture,live);await rulers();await createGuide('y',190);assert.equal((await guides()).length,1);assert.equal(await selected(),1,'New guide must become selected');
        assert.equal(await ev("getComputedStyle(document.querySelector('.ve-ruler-guide')).strokeWidth"),'2px','Imported CSS overrides the selected guide');
        await key('Escape');assert.equal(await selected(),0);await click('#a');assert.equal(await ev("document.querySelector('#a').getAttribute('data-ve-selected')"),'true');
        await ev('qaGuideSnapshots=0');await pointClick(await guidePoint(0));assert.equal(await selected(),1);
        assert.equal(await ev("document.querySelectorAll('.r-ve-viewport>svg:first-child [data-ve-selected]').length"),0,'Guide selection left artwork selected');assert.equal(await ev('qaGuideSnapshots'),0,'Selecting a guide serialized the document');
        await key('Delete');assert.equal((await guides()).length,0);assert.equal(await ev('qaGuideSnapshots'),1);assert.equal(await ev("!!document.querySelector('#a')&&!!document.querySelector('#artworkLine')"),true);
        await click('[data-tool=undo]');assert.equal((await guides()).length,1);assert.equal(await selected(),0);await key('Delete');assert.equal((await guides()).length,1,'Undo left a stale guide selection');
        await click('[data-tool=redo]');assert.equal((await guides()).length,0);await click('[data-tool=undo]');
        await pointClick(await guidePoint(0));await key('Backspace');assert.equal((await guides()).length,0);await click('[data-tool=undo]');
        await pointClick(await guidePoint(0));await click('[data-tool=delete]');assert.equal((await guides()).length,0);await click('[data-tool=undo]');
        await pointClick(await guidePoint(0));await key('Escape');await click('#a');await key('Delete');assert.equal(await ev("!!document.querySelector('#a')"),false);assert.equal((await guides()).length,1);await click('[data-tool=undo]');
        await createGuide('x',350);assert.equal((await guides()).length,2);await key('Delete');assert.equal((await guides()).length,1);assert.equal((await guides())[0].axis,'y');
        const zoom=await ev("(()=>{const r=document.querySelector('.r-ve-canvas-wrap').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()");
        for(let i=0;i<8;i++)win.webContents.sendInputEvent({type:'mouseWheel',x:Math.round(zoom.x),y:Math.round(zoom.y),deltaX:0,deltaY:120});await pause(150);await tick();
        await pointClick(await guidePoint(0,4));assert.equal(await selected(),1,'Zoomed guide hit tolerance is not in screen pixels');
        await pointClick(await guidePoint(0,9));assert.equal(await selected(),0,'A distant click still selected the guide');
        await click('.r-ve-stroke-width');const width=await ev("document.querySelector('.r-ve-stroke-width').value");await pointClick(await guidePoint(0));await key('Delete');assert.equal((await guides()).length,0);assert.equal(await ev("document.querySelector('.r-ve-stroke-width').value"),width,'Guide deletion edited a property input');await click('[data-tool=undo]');
        await pointClick(await guidePoint(0));await rulers();assert.equal(await selected(),0);assert.equal(await ev("document.querySelectorAll('.ve-ruler-guide').length"),0);await key('Delete');assert.equal((await guides()).length,1);await rulers();
        await pointClick(await guidePoint(0));const xml=await ev("new XMLSerializer().serializeToString(document.querySelector('.r-ve-viewport>svg:first-child'))");assert.ok(!xml.includes('ve-ruler-guide'),'Guide overlays entered exported artwork');
        await tick();fs.writeFileSync(path.join(output,live?'live-guide.png':'ordinary-guide.png'),(await win.webContents.capturePage()).toPNG());await close();
        await open(xml,live);await rulers();assert.equal((await guides()).length,1);assert.equal(await ev("document.querySelectorAll('.ve-ruler-guide').length"),1);
        const again=await guidePoint(0);await pointClick(again);await mouse('mouseDown',again,{clickCount:2});await mouse('mouseUp',again,{clickCount:2});await pause();await tick();
        assert.equal((await guides()).length,0,'Double-click deletion was lost');await close();
        report.checks.push((live?'live':'ordinary')+': real ruler drags, selected-guide highlight, Delete/Backspace/trash, one-step undo/redo, artwork isolation, zero snapshots on selection, vertical guides, zoom-independent tolerance, input focus, hidden-guide deselection and SVG reload');
    }
    finish();
}catch(error){finish(error);}});
