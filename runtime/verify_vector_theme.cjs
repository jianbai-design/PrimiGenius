'use strict';
// Theme checks use real editor DOM/input in ordinary and standalone live modes.
const {app,BrowserWindow,ipcMain}=require('electron');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),output=path.resolve(process.argv[2]||'');
if(path.dirname(output)!==root||!path.basename(output).startsWith('.tmp-vector-'))throw Error('A disposable workspace directory is required');
fs.mkdirSync(output,{recursive:true});app.setPath('userData',path.join(output,'profile'));
for(const flag of ['no-sandbox','disable-gpu','in-process-gpu'])app.commandLine.appendSwitch(flag);
ipcMain.on('get-backend-port-sync',e=>e.returnValue=65534);
require('../src/vector-export-main').register({app,ipcMain,BrowserWindow,dialog:{showSaveDialog:async()=>({canceled:true})}});
let win;const report={checks:[],errors:[]};const timeout=setTimeout(()=>finish(Error('Theme acceptance timed out')),120000);
function finish(error){clearTimeout(timeout);if(error)report.errors.push(error.stack||String(error));report.result=report.errors.length?'FAIL':'PASS';fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));if(win&&!win.isDestroyed())win.destroy();app.exit(report.errors.length?1:0);}
const ev=code=>win.webContents.executeJavaScript(code),pause=ms=>new Promise(r=>setTimeout(r,ms||60));
const tick=()=>ev('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))');
async function wait(code){for(let i=0;i<150;i++){if(await ev(code))return;await pause();}throw Error('Timed out: '+code);}
async function hover(selector){const p=await ev(`(()=>{const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);win.webContents.sendInputEvent({type:'mouseMove',x:Math.round(p.x),y:Math.round(p.y)});await pause();return p;}
async function click(selector,right=false){await ev(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({block:'nearest'});true`);const p=await hover(selector);assert.ok(await ev(`document.querySelector(${JSON.stringify(selector)}).contains(document.elementFromPoint(${p.x},${p.y}))`),'Obstructed click: '+selector);for(const type of ['mouseDown','mouseUp'])win.webContents.sendInputEvent({type,x:Math.round(p.x),y:Math.round(p.y),button:right?'right':'left',clickCount:1});await pause();if(right)await tick();}
function rgb(value){return value.match(/[\d.]+/g).slice(0,3).map(Number);}
function luminance(color){return rgb(color).map(x=>{x/=255;return x<=.04045?x/12.92:((x+.055)/1.055)**2.4;}).reduce((s,v,i)=>s+v*[.2126,.7152,.0722][i],0);}
async function light(selector){const colors=await ev(`(()=>{const node=document.querySelector(${JSON.stringify(selector)});if(!node)throw Error('Missing theme surface: '+${JSON.stringify(selector)});const c=getComputedStyle(node);return {bg:c.backgroundColor,fg:c.color};})()`);assert.ok(rgb(colors.bg).every(v=>v>=225),'Dark surface remains: '+selector+' '+colors.bg);return colors;}
async function readable(selector,minimum=4.5){const c=await light(selector),a=luminance(c.bg),b=luminance(c.fg);assert.ok((Math.max(a,b)+.05)/(Math.min(a,b)+.05)>=minimum,'Insufficient text contrast: '+selector);}
async function shot(name){win.webContents.invalidate();await tick();fs.writeFileSync(path.join(output,name+'.png'),(await win.webContents.capturePage()).toPNG());}
async function open(svg,live,key){await ev(`currentLang='zh';window.__editorStandalone=${live};window.qaThemeInstance={config:{id:'theme',name:'theme',parameters:[],packages:[]},__standaloneMode:true,_liveEditorSessions:new Map(),outDir:''};renderRPanelUI(qaThemeInstance,document.createElement('div'));qaThemeInstance.__openVectorEditor({name:'theme.svg',path:'',ext:'.svg'},{sessionKey:${JSON.stringify(key)},liveMode:${live},initialSvgText:${JSON.stringify(svg)}});true`);await pause(250);await tick();}
async function close(){await ev("window.__editorStandalone=false;document.querySelector('.r-ve-close-btn').click();true");if(await ev("!!document.querySelector('.ve-confirm')"))await click('.ve-confirm [data-choice="1"]');await wait("!document.querySelector('.r-vector-editor-overlay')");}
app.whenReady().then(async()=>{try{
    win=new BrowserWindow({show:false,width:1400,height:1000,webPreferences:{nodeIntegration:true,contextIsolation:false,backgroundThrottling:false}});
    win.webContents.on('console-message',(_event,level,message)=>{if(level>=3)report.errors.push(message);});await win.loadFile(path.join(root,'src/editor.html'));
    // editor.html normally hides Close; emulate index.html for the ordinary mode.
    await ev("(()=>{const s=document.createElement('style');s.textContent='.r-vector-editor-overlay:not(.r-ve-standalone) .r-ve-close-btn{display:flex!important}.r-vector-editor-overlay,.r-ve-ctx-menu{animation:none!important}.r-vector-editor-overlay *{animation:none!important;transition:none!important}';document.head.append(s);})()");
    const fixture='<svg xmlns="http://www.w3.org/2000/svg" width="600" height="420"><rect id="paint" x="50" y="70" width="120" height="90" fill="currentColor"/><text id="label" x="220" y="130" font-size="22">科研图形</text></svg>';
    for(const live of [false,true]){
        const mode=live?'live':'ordinary',svg=live?fixture.replace('<svg ','<svg color="#5830a8" '):fixture;
        await open(svg,live,'theme-'+mode);
        await light('.r-ve-window');await light('.r-ve-canvas-wrap');await light('.r-ve-props-bar');await readable('.r-ve-props-bar input[type=number]');
        assert.equal(await ev("getComputedStyle(document.querySelector('#paint')).fill"),live?'rgb(88, 48, 168)':'rgb(0, 0, 0)','UI theme changed currentColor artwork');
        await hover('.ve-shape-tool');const hoverColors=await ev("(()=>{const s=getComputedStyle(document.querySelector('.ve-shape-tool'));return {bg:s.backgroundColor,fg:s.color};})()");assert.ok(luminance(hoverColors.fg)<.4,'Hovered icon disappeared on light surface');
        await click('.ve-shape-tool',true);assert.equal(await ev("document.querySelectorAll('.ve-shape-menu button').length"),4);await readable('.ve-shape-menu');await shot(mode+'-shape-tools');
        await click('[data-shape-tool=square]');assert.equal(await ev("document.querySelector('.r-ve-tool.active').dataset.tool"),'square');
        for(const type of ['keyDown','keyUp'])win.webContents.sendInputEvent({type,keyCode:'Control'});await tick();
        assert.equal(await ev("document.querySelector('.r-ve-tool.active').dataset.tool"),'select');
        await click('#paint');await click('.ve-advanced-menu summary');await readable('.ve-advanced-items');await shot(mode+'-menu');await click('.ve-advanced-items [data-panel=transform]');await wait("!!document.querySelector('.ve-workbench-panel input')");await readable('.ve-workbench-panel');await readable('.ve-workbench-panel input');await click('.ve-workbench-panel header button');
        await click('[data-paint=fill]');await click('.r-ve-swatches [data-action=gradient]');await wait("!!document.querySelector('.r-ve-gradient-panel')");await readable('.r-ve-gradient-panel');await light('.r-ve-grad-type[data-type=radial]');await shot(mode+'-gradient');await click('.r-ve-grad-close');
        // Opening the live gradient now paints the shape; deselect before testing Key Bar insertion.
        for(const type of ['keyDown','keyUp'])win.webContents.sendInputEvent({type,keyCode:'Escape'});await tick();
        await click('.r-ve-insert-keybar-btn');await wait("!!document.querySelector('.r-ve-keybar-panel')");await readable('.r-ve-keybar-panel');await light('.r-ve-kb-mode[data-mode="2color"]');await click('.r-ve-kb-close');
        await click('.r-ve-export-unified-btn');await wait("!!document.querySelector('.r-ve-export-fmt')");await readable('.r-ve-export-fmt');await light('.r-ve-export-preview');await shot(mode+'-export');await click('.r-ve-export-dlg-close');
        await click('[data-tool=text]',true);await wait("!!document.querySelector('.r-ve-text-panel')");await readable('.r-ve-text-panel');await readable('.r-ve-text-panel-content');await click('.r-ve-text-panel-close');
        await click('#paint',true);await wait("!!document.querySelector('.r-ve-ctx-menu')");await readable('.r-ve-ctx-menu');await click('.r-ve-ctx-item[data-action=rotate]');await readable('#rot-angle-input');await click('#rot-cancel-btn');
        await click('.ve-advanced-menu summary');await click('[data-command=rulers]');await wait("!!document.querySelector('.ve-rulers')");assert.deepEqual(await ev("[...document.querySelector('.ve-rulers').getContext('2d').getImageData(1,1,1,1).data]"),[248,250,252,255]);
        await ev("(()=>{const i=document.querySelector('.r-ve-stroke-width');i.value='3';i.dispatchEvent(new Event('input',{bubbles:true}));i.dispatchEvent(new Event('change',{bubbles:true}));})()");await tick();
        await ev("window.__editorStandalone=false;document.querySelector('.r-ve-close-btn').click();true");await wait("!!document.querySelector('.ve-confirm')");await readable('.ve-confirm>div');await shot(mode+'-confirm');await click('.ve-confirm [data-choice="2"]');await wait("!document.querySelector('.ve-confirm')");
        report.checks.push(mode+': light canvas/toolbars, readable hover/menus/panels/dialogs, light rulers and preserved SVG currentColor');await close();
    }
    if(process.argv[3]){const actual=fs.readFileSync(path.resolve(process.argv[3]),'utf8');for(const live of [false,true]){await open(actual,live,'heatmap-'+live);await shot(live?'heatmap-live':'heatmap-ordinary');await close();}report.checks.push('actual R heatmap in ordinary and standalone live windows');}
    finish();
}catch(error){finish(error);}});
