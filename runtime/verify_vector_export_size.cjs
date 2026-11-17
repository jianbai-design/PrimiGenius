'use strict';
const {app,BrowserWindow,ipcMain}=require('electron');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {tiffSize}=require('../src/vector-codecs');
const root=path.resolve(__dirname,'..'),output=path.resolve(process.argv[2]||'');
if(path.dirname(output)!==root||!path.basename(output).startsWith('.tmp-vector-'))throw Error('A disposable workspace directory is required');
fs.mkdirSync(output,{recursive:true});app.setPath('userData',path.join(output,'profile'));
for(const flag of ['no-sandbox','disable-gpu','in-process-gpu'])app.commandLine.appendSwitch(flag);
ipcMain.on('get-backend-port-sync',e=>e.returnValue=65534);
require('../src/vector-export-main').register({app,ipcMain,BrowserWindow,dialog:{showSaveDialog:async()=>({canceled:true})}});
let win;const report={checks:[],errors:[],sizes:{}};
const timeout=setTimeout(()=>finish(Error('Export-size acceptance timed out')),150000);
function finish(error){clearTimeout(timeout);if(error)report.errors.push(error.stack||String(error));report.result=report.errors.length?'FAIL':'PASS';fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));if(win&&!win.isDestroyed())win.destroy();app.exit(report.errors.length?1:0);}
const ev=code=>win.webContents.executeJavaScript(code),pause=ms=>new Promise(r=>setTimeout(r,ms||40));
async function wait(code){for(let i=0;i<180;i++){if(await ev(code))return;await pause();}throw Error('Timed out: '+code);}
async function click(selector){const p=await ev(`(()=>{const el=document.querySelector(${JSON.stringify(selector)}),r=el.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;return{x,y,hit:el.contains(document.elementFromPoint(x,y))};})()`);assert.ok(p.hit,'Obstructed: '+selector);for(const type of ['mouseMove','mouseDown','mouseUp'])win.webContents.sendInputEvent({type,x:Math.round(p.x),y:Math.round(p.y),button:'left',clickCount:1});await pause();}
async function change(selector,value,type='input'){await ev(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});el.value=${JSON.stringify(value)};el.dispatchEvent(new Event(${JSON.stringify(type)},{bubbles:true}));})()`);}
const sizeText=()=>ev("document.querySelector('.r-ve-export-size-info').textContent");
const human=n=>n>=1048576?(n/1048576).toFixed(2)+' MB':(n/1024).toFixed(1)+' KB';
app.whenReady().then(async()=>{try{
    win=new BrowserWindow({show:false,width:1320,height:960,webPreferences:{nodeIntegration:true,contextIsolation:false,backgroundThrottling:false}});
    win.webContents.on('console-message',(_event,level,message)=>{if(level>=3)report.errors.push(message);});await win.loadFile(path.join(root,'src/editor.html'));
    const svg='<svg xmlns="http://www.w3.org/2000/svg" width="792pt" height="504pt" viewBox="0 0 792 504"><rect x="40" y="40" width="300" height="130" fill="#ed8a23"/><text x="60" y="230" font-size="36">Export size</text></svg>';
    for(const [live,language]of [[false,'zh'],[true,'en']]){
        await ev(`currentLang=${JSON.stringify(language)};window.__editorStandalone=${live};window.qaExportInstance={config:{id:'export-size',name:'export-size',parameters:[],packages:[]},__standaloneMode:true,_liveEditorSessions:new Map(),outDir:''};renderRPanelUI(qaExportInstance,document.createElement('div'));qaExportInstance.__openVectorEditor({name:'export-size.svg',path:'',ext:'.svg'},{sessionKey:'export-size-'+Date.now(),liveMode:${live},initialSvgText:${JSON.stringify(svg)}});true`);await pause(200);
        assert.equal(await ev("!!document.querySelector('[data-panel=layout]')"),false);
        assert.deepEqual(await ev("[...document.querySelectorAll('.ve-advanced-items [data-panel]')].map(n=>n.dataset.panel)"),['transform','history']);
        await click('.r-ve-export-unified-btn');await wait("!!document.querySelector('.r-ve-export-dpi')");
        assert.equal(await ev("document.querySelector('.r-ve-export-inch-w').value"),'11');assert.equal(await ev("document.querySelector('.r-ve-export-inch-h').value"),'7');
        await change('.r-ve-export-dpi','300');await wait("document.querySelector('.r-ve-export-dims').textContent==='3300 × 2100 px'");
        const medium=await sizeText();assert.ok(medium.includes(human(tiffSize(3300,2100))));
        await click('[data-dpi="600"]');await wait("document.querySelector('.r-ve-export-dims').textContent==='6600 × 4200 px'");
        const high=await sizeText();assert.ok(high.includes(human(tiffSize(6600,4200))));assert.notEqual(high,medium);
        assert.equal(await ev("Math.max(document.querySelector('.r-ve-export-canvas').width,document.querySelector('.r-ve-export-canvas').height)"),900,'DPI change allocated a full-size raster for the preview');
        report.sizes[language]={dpi300:medium,dpi600:high};
        await change('.r-ve-export-fmt','png','change');await wait("document.querySelector('.r-ve-export-size-info').textContent.includes('PNG')");
        assert.match(await sizeText(),language==='zh'?/像素数据.*压缩结果/:/Pixel data.*compression/);
        await change('.r-ve-export-fmt','pdf','change');await wait("document.querySelector('.r-ve-export-size-info').textContent.includes('PDF')");assert.match(await ev("document.querySelector('.r-ve-export-dims').textContent"),/11 × 7 in/);
        await change('.r-ve-export-fmt','svg','change');await wait("document.querySelector('.r-ve-export-dims').textContent.includes('SVG')");assert.match(await sizeText(),/≈ .* KB/);
        // Rapid format/DPI changes must not let stale image callbacks restore preview sizes.
        await change('.r-ve-export-fmt','png','change');await change('.r-ve-export-dpi','150');await change('.r-ve-export-fmt','tif','change');await change('.r-ve-export-dpi','600');
        await wait("document.querySelector('.r-ve-export-size-info').textContent.includes('TIF')");await pause(250);assert.ok((await sizeText()).includes(human(tiffSize(6600,4200))));
        await ev("window.qaExportedTiff=null;window.qaExportCreate=URL.createObjectURL;URL.createObjectURL=function(blob){if(blob.type==='image/tiff')qaExportedTiff=blob;return qaExportCreate.call(URL,blob)};window.qaExportAnchor=HTMLAnchorElement.prototype.click;HTMLAnchorElement.prototype.click=function(){};true");
        await click('.r-ve-lock-ratio');await change('.r-ve-export-inch-w','1');await change('.r-ve-export-inch-h','0.7');await change('.r-ve-export-dpi','300');
        await wait("document.querySelector('.r-ve-export-dims').textContent==='300 × 210 px'");assert.ok((await sizeText()).includes(human(tiffSize(300,210))));
        await click('.r-ve-export-confirm');await wait('!!qaExportedTiff');assert.equal(await ev('qaExportedTiff.size'),tiffSize(300,210),'Displayed TIFF estimate does not match actual exported bytes');
        const header=await ev('qaExportedTiff.arrayBuffer().then(b=>Array.from(new Uint8Array(b,0,4)))');assert.deepEqual(header,[73,73,42,0]);
        await ev("URL.createObjectURL=qaExportCreate;HTMLAnchorElement.prototype.click=qaExportAnchor;true");
        await click('.r-ve-export-unified-btn');await change('.r-ve-export-dpi','1200');await wait("document.querySelector('.r-ve-export-size-info').textContent.includes('3200')||document.querySelector('.r-ve-export-size-info').textContent.includes('32 megapixels')");
        await change('.r-ve-export-dpi','');await wait("document.querySelector('.r-ve-export-dims').textContent==='—'");
        await change('.r-ve-export-dpi','600');await wait("document.querySelector('.r-ve-export-dims').textContent==='6600 × 4200 px'");
        await ev('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))');
        fs.writeFileSync(path.join(output,language+'-export.png'),(await win.webContents.capturePage()).toPNG());await click('.r-ve-export-dlg-close');
        await ev("window.__editorStandalone=false;document.querySelector('.r-ve-close-btn').click();true");
        if(await ev("!!document.querySelector('.ve-confirm')"))await click('.ve-confirm [data-choice="1"]');await wait("!document.querySelector('.r-vector-editor-overlay')");
        report.checks.push(language+': layout removed, correct TIFF size at 300/600 DPI, 900px preview bound, honest PNG/PDF metrics, SVG bytes, stale-preview guard, actual TIFF byte equality, invalid/oversized DPI and cleanup');
    }
    finish();
}catch(error){finish(error);}});
