'use strict';
// Exercise Chromium paint/hit testing and real pointer input outside the SVG page.
const {app,BrowserWindow,ipcMain}=require('electron');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),output=path.resolve(process.argv[2]||'');
if(path.dirname(output)!==root||!path.basename(output).startsWith('.tmp-vector-'))throw Error('A disposable workspace directory is required');
fs.mkdirSync(output,{recursive:true});app.setPath('userData',path.join(output,'profile'));
for(const flag of ['no-sandbox','disable-gpu','in-process-gpu'])app.commandLine.appendSwitch(flag);
ipcMain.on('get-backend-port-sync',e=>e.returnValue=65534);
require('../src/vector-export-main').register({app,ipcMain,BrowserWindow,dialog:{showSaveDialog:async()=>({canceled:true})}});
let win;const report={checks:[],errors:[]};
const timeout=setTimeout(()=>finish(Error('Pasteboard acceptance timed out')),60000);
function finish(error){clearTimeout(timeout);if(error)report.errors.push(error.stack||String(error));report.result=report.errors.length?'FAIL':'PASS';fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));if(win&&!win.isDestroyed())win.destroy();app.exit(report.errors.length?1:0);}
const evaluate=code=>win.webContents.executeJavaScript(code);
const pause=()=>new Promise(r=>setTimeout(r,60));
async function input(type,p,modifiers=[]){win.webContents.sendInputEvent({type,x:Math.round(p.x),y:Math.round(p.y),button:'left',clickCount:1,modifiers});await pause();}
async function click(p,modifiers=[]){await input('mouseMove',p,modifiers);await input('mouseDown',p,modifiers);await input('mouseUp',p,modifiers);}
async function drag(a,b){await input('mouseMove',a);await input('mouseDown',a);await input('mouseMove',b);await input('mouseUp',b);}
async function rect(selector){return evaluate(`document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect().toJSON()`);}
async function center(selector){const r=await rect(selector);return {x:r.x+r.width/2,y:r.y+r.height/2};}
async function redVisible(){
    const p=await center('#shape');
    assert.equal(await evaluate(`document.elementFromPoint(${p.x},${p.y})?.id`),'shape','Off-page shape must receive pointer input');
    win.webContents.invalidate();
    await evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');
    const shot=await win.webContents.capturePage(),size=shot.getSize(),bitmap=shot.toBitmap();
    const viewportSize=await evaluate('({width:innerWidth,height:innerHeight})');
    const i=(Math.round((p.y+8)*size.height/viewportSize.height)*size.width+Math.round((p.x+8)*size.width/viewportSize.width))*4;
    fs.writeFileSync(path.join(output,'pasteboard.png'),shot.toPNG());
    assert.ok(bitmap[i+2]>220&&bitmap[i+1]<50&&bitmap[i]<50,'Actual fill must remain painted on the pasteboard: '+JSON.stringify({p,size,pixel:[...bitmap.subarray(i,i+4)]}));
}
app.whenReady().then(async()=>{
 try{
    win=new BrowserWindow({show:false,width:1100,height:800,webPreferences:{nodeIntegration:true,contextIsolation:false,backgroundThrottling:false}});
    await win.loadFile(path.join(root,'src/editor.html'));
    const svg='<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300" viewBox="20 30 400 300"><defs><clipPath id="page"><rect x="20" y="30" width="400" height="300"/></clipPath><clipPath id="plot"><rect x="35" y="50" width="35" height="25"/></clipPath></defs><g id="pageGroup" clip-path="url(#page)"><rect id="shape" x="170" y="140" width="60" height="50" fill="#ff0000"/><g clip-path="url(#plot)"><rect id="clipped" x="35" y="50" width="90" height="25" fill="#0000ff"/></g><g id="rotated" data-user-group="true" transform="rotate(20 300 250)"><rect x="280" y="235" width="40" height="30" fill="#00a050"/></g></g></svg>';
    await evaluate(`(async()=>{await require('electron').ipcRenderer.invoke('vector-recovery',{key:'pasteboard-test',action:'clear'});_bootstrapStandaloneEditor('pasteboard.svg',${JSON.stringify(svg)},'pasteboard-test');})()`);await new Promise(r=>setTimeout(r,350));
    await evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');
    assert.equal(await evaluate("document.querySelectorAll('.r-ve-tool[data-tool=zoomin],.r-ve-tool[data-tool=zoomout]').length"),0);
    const beforeWheel=await rect('.r-ve-viewport > svg'),wheelPoint=await center('.r-ve-canvas-wrap');
    const wheel=async deltaY=>{win.webContents.sendInputEvent({type:'mouseWheel',x:Math.round(wheelPoint.x),y:Math.round(wheelPoint.y),deltaX:0,deltaY,canScroll:true});await pause();await evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');return (await rect('.r-ve-viewport > svg')).width;};
    const firstWheel=await wheel(120),secondWheel=await wheel(-120);
    assert.ok(Math.abs(firstWheel-beforeWheel.width)>5,'Native mouse wheel did not zoom');
    assert.ok((firstWheel-beforeWheel.width)*(secondWheel-firstWheel)<0,'Reverse wheel did not reverse zoom');
    report.checks.push('zoom buttons removed; native wheel zooms in both directions');
    await evaluate(`(()=>{const canvas=document.querySelector('.r-ve-canvas-wrap'),r=canvas.getBoundingClientRect();for(let i=0;i<4;i++)canvas.dispatchEvent(new WheelEvent('wheel',{bubbles:true,cancelable:true,deltaY:120,clientX:r.x+r.width/2,clientY:r.y+r.height/2}));})()`);await pause();
    const initialPage=await rect('.r-ve-viewport > svg'),initialCanvas=await rect('.r-ve-canvas-wrap');
    await evaluate("document.querySelector('[data-tool=move]').click();true");
    const panFrom={x:initialCanvas.x+60,y:initialCanvas.y+60};
    await drag(panFrom,{x:panFrom.x+initialCanvas.x+(initialCanvas.width-initialPage.width)/2-initialPage.x,y:panFrom.y+initialCanvas.y+(initialCanvas.height-initialPage.height)/2-initialPage.y});
    await evaluate("document.querySelector('[data-tool=select]').click();true");
    assert.equal(await evaluate("getComputedStyle(document.querySelector('#pageGroup')).clipPath"),'none');
    assert.notEqual(await evaluate("getComputedStyle(document.querySelector('#clipped').parentElement).clipPath"),'none');
    const clippedHit=await evaluate(`(()=>{const e=document.querySelector('#clipped'),p=new DOMPoint(100,60).matrixTransform(e.getScreenCTM());return document.elementFromPoint(p.x,p.y)?.id;})()`);
    assert.notEqual(clippedHit,'clipped','Local plot clipping must still work');
    report.checks.push('whole-page clipping is bypassed only in editor; local plot clipping is preserved');
    const original=await center('#shape'),page=await rect('.r-ve-viewport > svg'),shape=await rect('#shape'),canvas=await rect('.r-ve-canvas-wrap');
    const empty={x:canvas.x+65,y:canvas.y+50};
    for(const [side,target] of Object.entries({right:{x:page.right+shape.width/2+25,y:original.y},left:{x:page.left-shape.width/2-25,y:original.y},top:{x:original.x,y:page.top-shape.height/2-25},bottom:{x:original.x,y:page.bottom+shape.height/2+25}})){
        await drag(await center('#shape'),target);await redVisible();
        await click(empty);assert.equal(await evaluate("document.querySelectorAll('.r-ve-viewport > svg:first-child [data-ve-selected]').length"),0);
        await drag(await center('#shape'),original);
        const returned=await center('#shape');assert.ok(Math.hypot(returned.x-original.x,returned.y-original.y)<3,'Could not drag back from '+side);
        report.checks.push(side+' pasteboard: painted, deselected, reselected and dragged back');
    }
    await click(await center('#shape'));await click(await center('#rotated'),['shift']);
    assert.equal(await evaluate("document.querySelectorAll('.r-ve-viewport > svg:first-child [data-ve-selected]').length"),2);
    const beforeGroup=await center('#rotated'),destination={x:page.right+shape.width/2+25,y:original.y};
    await drag(await center('#shape'),destination);await redVisible();
    const afterGroup=await center('#rotated');assert.ok(afterGroup.x>beforeGroup.x+100,'Rotated group did not follow multi-selection');
    await drag(await center('#shape'),original);report.checks.push('multi-selection including rotated group moves onto pasteboard and back');
    await click(empty);await click(await center('#shape'));
    const corner=await center('[data-ve-handle="nw"]');await input('mouseMove',{x:corner.x-10,y:corner.y-10});
    assert.equal(await evaluate("document.querySelector('.r-ve-canvas-wrap').classList.contains('ve-cursor-locked')"),false,'Removed corner rotation zone is still active');
    const beforeSize=await rect('#shape');await drag(corner,{x:corner.x-15,y:corner.y-15});
    assert.ok((await rect('#shape')).width>beforeSize.width+10,'Corner resizing stopped working');
    report.checks.push('outside corners do not rotate; corner handles still resize');
    const knob=await center('.ve-selection-rotate'),body=await center('#shape');
    await input('mouseMove',knob);await evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');
    assert.ok(await evaluate("document.querySelector('.r-ve-canvas-wrap').classList.contains('ve-cursor-locked')"),'Rotation knob cursor missing: '+await evaluate("document.querySelector('.r-ve-canvas-wrap').style.cursor"));
    const beforeRotate=await evaluate("document.querySelector('#shape').outerHTML");
    const vx=knob.x-body.x,vy=knob.y-body.y;
    await drag(knob,{x:body.x+(vx-vy)/Math.SQRT2,y:body.y+(vx+vy)/Math.SQRT2});
    assert.notEqual(await evaluate("document.querySelector('#shape').outerHTML"),beforeRotate,'Rotation knob did not rotate');
    report.checks.push('dedicated rotation knob still rotates');
    const saved=await evaluate(`(()=>{const s=document.querySelector('.r-ve-viewport > svg');return new XMLSerializer().serializeToString(s);})()`);
    assert.ok(saved.includes('clip-path="url(#page)"'),'Saved source lost original page clip');
    const exported=await evaluate(`(()=>{const xml=require('./vector-export').exportSvg(${JSON.stringify(saved)},{x:20,y:30,w:400,h:300}),s=new DOMParser().parseFromString(xml,'image/svg+xml').documentElement;return {overflow:s.style.overflow,viewBox:s.getAttribute('viewBox'),pageClip:s.querySelector('#pageGroup').getAttribute('clip-path'),localClip:s.querySelector('#clipped').parentElement.getAttribute('clip-path')};})()`);
    assert.deepEqual(exported,{overflow:'hidden',viewBox:'20 30 400 300',pageClip:'url(#page)',localClip:'url(#plot)'});
    await evaluate("document.querySelector('[data-tool=undo]').click();true");await pause();
    assert.equal(await evaluate("getComputedStyle(document.querySelector('#pageGroup')).clipPath"),'none','Undo lost editing-only clipping bypass');
    report.checks.push('SVG export crops to artboard and retains original clips; undo preserves pasteboard editing');
    fs.writeFileSync(path.join(output,'pasteboard.png'),(await win.webContents.capturePage()).toPNG());
    finish();
 }catch(error){finish(error);}
});
