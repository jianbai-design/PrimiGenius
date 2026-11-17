'use strict';
// Use real Chromium pointer/key input: HTMLElement.click() bypasses pointer-events.
const {app,BrowserWindow,ipcMain}=require('electron');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),output=path.resolve(process.argv[2]||'');
if(path.dirname(output)!==root||!path.basename(output).startsWith('.tmp-vector-'))throw Error('A disposable workspace directory is required');
fs.mkdirSync(output,{recursive:true});app.setPath('userData',path.join(output,'profile'));
for(const flag of ['no-sandbox','disable-gpu','in-process-gpu'])app.commandLine.appendSwitch(flag);
ipcMain.on('get-backend-port-sync',e=>e.returnValue=65534);
require('../src/vector-export-main').register({app,ipcMain,BrowserWindow,dialog:{showSaveDialog:async()=>({canceled:true})}});
let win;const report={checks:[],errors:[]};
const timeout=setTimeout(()=>finish(Error('Dialog acceptance timed out')),90000);
function finish(error){clearTimeout(timeout);if(error)report.errors.push(error.stack||String(error));report.result=report.errors.length?'FAIL':'PASS';fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));if(win&&!win.isDestroyed())win.destroy();app.exit(report.errors.length?1:0);}
const evaluate=code=>win.webContents.executeJavaScript(code);
async function waitFor(code,message){for(let i=0;i<150;i++){if(await evaluate(code))return;await new Promise(r=>setTimeout(r,20));}throw Error(message);}
async function click(selector,expectedHit=true){
    const point=await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)throw Error('Missing click target');const r=el.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;return {x,y,hit:document.elementFromPoint(x,y)===el};})()`);
    assert.equal(point.hit,expectedHit,'Native hit testing: '+selector);
    win.webContents.sendInputEvent({type:'mouseMove',x:Math.round(point.x),y:Math.round(point.y)});
    win.webContents.sendInputEvent({type:'mouseDown',x:Math.round(point.x),y:Math.round(point.y),button:'left',clickCount:1});
    win.webContents.sendInputEvent({type:'mouseUp',x:Math.round(point.x),y:Math.round(point.y),button:'left',clickCount:1});
    await new Promise(r=>setTimeout(r,40));
}
async function open(language,key){
    await evaluate(`(async()=>{currentLang=${JSON.stringify(language)};window.__editorStandalone=true;const original='<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300"><text id="label" x="30" y="80">Original</text></svg>';await require('electron').ipcRenderer.invoke('vector-recovery',{key:${JSON.stringify(key)},action:'write',svg:original.replace('Original','Recovered')});_bootstrapStandaloneEditor('dialog.svg',original,${JSON.stringify(key)});})()`);
    await waitFor("!!document.querySelector('.ve-confirm')",'Recovery prompt missing');
    await new Promise(resolve=>setTimeout(resolve,250));
    await evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');
    const layout=await evaluate(`(()=>{const d=document.querySelector('.ve-confirm'),box=d.firstElementChild,p=box.querySelector('p');return {pointer:getComputedStyle(d).pointerEvents,top:p.getBoundingClientRect().top-box.getBoundingClientRect().top,paragraphMargin:getComputedStyle(p).marginTop};})()`);
    assert.equal(layout.pointer,'auto');assert.ok(layout.top<=18,'Excessive top whitespace');assert.equal(layout.paragraphMargin,'0px');
}
async function cleanClose(){
    await evaluate("window.__editorStandalone=false;document.querySelector('.r-ve-close-btn').click()");
    if(await evaluate("!!document.querySelector('.ve-confirm')"))await click('.ve-confirm [data-choice="1"]');
    await waitFor("!document.querySelector('.r-vector-editor-overlay')",'Editor did not close');
}
app.whenReady().then(async()=>{
 try{
    win=new BrowserWindow({show:false,width:1100,height:800,webPreferences:{nodeIntegration:true,contextIsolation:false,backgroundThrottling:false}});
    win.webContents.on('console-message',(_event,level,message)=>{if(level>=3)report.errors.push(message);});
    await win.loadFile(path.join(root,'src/editor.html'));
    await open('zh','later');
    await evaluate("window.qaPointer=document.createElement('style');qaPointer.textContent='.ve-confirm{pointer-events:none!important}';document.head.appendChild(qaPointer);true");
    await click('.ve-confirm [data-choice="2"]',false);
    assert.ok(await evaluate("!!document.querySelector('.ve-confirm')"),'Regression simulation did not reproduce blocked clicks');
    await evaluate('qaPointer.remove()');await click('.ve-confirm [data-choice="2"]');
    await waitFor("!document.querySelector('.ve-confirm')",'Later did not dismiss');
    assert.ok(await evaluate("require('electron').ipcRenderer.invoke('vector-recovery',{key:'later',action:'read'}).then(Boolean)"),'Later removed recovery copy');
    report.checks.push('reproduced inherited pointer block; real mouse dismisses Later');await cleanClose();

    await open('zh','discard');await click('.ve-confirm [data-choice="1"]');
    await waitFor("!document.querySelector('.ve-confirm')",'Discard did not dismiss');
    assert.equal(await evaluate("require('electron').ipcRenderer.invoke('vector-recovery',{key:'discard',action:'read'})"),null);
    report.checks.push('real mouse discards recovery copy');await cleanClose();

    await open('zh','restore');await click('.ve-confirm [data-choice="0"]');
    await waitFor("document.querySelector('.r-ve-viewport #label')?.textContent==='Recovered'",'Restore failed');
    await evaluate("window.__editorStandalone=false;document.querySelector('.r-ve-close-btn').click()");
    await waitFor("!!document.querySelector('.ve-confirm')",'Unsaved prompt missing');await click('.ve-confirm-close');
    await waitFor("!document.querySelector('.ve-confirm')",'Close button did not cancel');assert.ok(await evaluate("!!document.querySelector('.r-vector-editor-overlay')"));
    report.checks.push('real mouse restores; X safely cancels unsaved close');await cleanClose();

    await open('zh','close-x');await click('.ve-confirm-close');await waitFor("!document.querySelector('.ve-confirm')",'Recovery X did not dismiss');
    assert.ok(await evaluate("require('electron').ipcRenderer.invoke('vector-recovery',{key:'close-x',action:'read'}).then(Boolean)"));
    report.checks.push('recovery X retains copy');await cleanClose();

    await open('en','escape');
    fs.writeFileSync(path.join(output,'english.png'),(await win.webContents.capturePage()).toPNG());
    win.webContents.sendInputEvent({type:'keyDown',keyCode:'Escape'});win.webContents.sendInputEvent({type:'keyUp',keyCode:'Escape'});
    await waitFor("!document.querySelector('.ve-confirm')",'Escape did not dismiss');
    assert.ok(await evaluate("require('electron').ipcRenderer.invoke('vector-recovery',{key:'escape',action:'read'}).then(Boolean)"));
    report.checks.push('real Escape dismisses English prompt without deleting copy');await cleanClose();

    await open('zh','native-close');
    fs.writeFileSync(path.join(output,'chinese.png'),(await win.webContents.capturePage()).toPNG());
    const closed=new Promise(resolve=>win.once('closed',resolve));win.webContents.send('editor-request-close');
    await Promise.race([closed,new Promise((_,reject)=>setTimeout(()=>reject(Error('Native close request blocked by recovery prompt')),3000))]);
    report.checks.push('native editor close request works while recovery prompt is open','compact Chinese and English layout');
    finish();
 }catch(error){finish(error);}
});
