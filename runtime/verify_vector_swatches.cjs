'use strict';
const {app,BrowserWindow,ipcMain}=require('electron');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),output=path.resolve(process.argv[2]||'');
if(path.dirname(output)!==root||!path.basename(output).startsWith('.tmp-vector-'))throw Error('A disposable workspace directory is required');
fs.mkdirSync(output,{recursive:true});app.setPath('userData',path.join(output,'profile'));
for(const flag of ['no-sandbox','disable-gpu','in-process-gpu'])app.commandLine.appendSwitch(flag);
ipcMain.on('get-backend-port-sync',e=>e.returnValue=65534);
require('../src/vector-export-main').register({app,ipcMain,BrowserWindow,dialog:{showSaveDialog:async()=>({canceled:true})}});
let win;const report={checks:[],errors:[],performance:{}};const timeout=setTimeout(()=>finish(Error('Swatch acceptance timed out')),180000);
function finish(error){clearTimeout(timeout);if(error)report.errors.push(error.stack||String(error));report.result=report.errors.length?'FAIL':'PASS';fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));if(win&&!win.isDestroyed())win.destroy();app.exit(report.errors.length?1:0);}
const ev=async code=>{try{return await win.webContents.executeJavaScript(code);}catch(error){throw Error(error.message+'\nScript: '+code.slice(0,250));}},pause=ms=>new Promise(r=>setTimeout(r,ms||60));
const tick=()=>ev('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))');
async function wait(code){for(let i=0;i<160;i++){if(await ev(code))return;await pause();}throw Error('Timed out: '+code);}
async function click(selector,shift=false){await ev(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({block:'nearest'});true`);const p=await ev(`(()=>{const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);assert.ok(await ev(`document.querySelector(${JSON.stringify(selector)}).contains(document.elementFromPoint(${p.x},${p.y}))`),'Obstructed: '+selector);for(const type of ['mouseMove','mouseDown','mouseUp'])win.webContents.sendInputEvent({type,x:Math.round(p.x),y:Math.round(p.y),button:'left',clickCount:1,modifiers:shift?['shift']:[]});await pause();}
async function key(keyCode){win.webContents.sendInputEvent({type:'keyDown',keyCode});if(keyCode==='Return')win.webContents.sendInputEvent({type:'char',keyCode:'\r'});win.webContents.sendInputEvent({type:'keyUp',keyCode});await pause();}
async function open(svg,live){await ev(`currentLang='zh';window.__editorStandalone=${live};window.qaSwatchInstance={config:{id:'swatch',name:'swatch',parameters:[],packages:[]},__standaloneMode:true,_liveEditorSessions:new Map(),outDir:''};renderRPanelUI(qaSwatchInstance,document.createElement('div'));qaSwatchInstance.__openVectorEditor({name:'swatch.svg',path:'',ext:'.svg'},{sessionKey:'swatch-'+Date.now(),liveMode:${live},initialSvgText:${JSON.stringify(svg)}});true`);await pause(250);await tick();}
async function close(){await ev("window.__editorStandalone=false;document.querySelector('.r-ve-close-btn').click();true");if(await ev("!!document.querySelector('.ve-confirm')"))await click('.ve-confirm [data-choice="1"]');await wait("!document.querySelector('.r-vector-editor-overlay')");}
const swatch=value=>'.r-ve-swatches .r-ve-swatch[data-value="'+value+'"]';
const fill=id=>ev(`getComputedStyle(document.querySelector(${JSON.stringify('#'+id)})).fill`);
app.whenReady().then(async()=>{try{
    win=new BrowserWindow({show:false,width:1320,height:960,webPreferences:{nodeIntegration:true,contextIsolation:false,backgroundThrottling:false}});
    win.webContents.on('console-message',(_event,level,message)=>{if(level>=3)report.errors.push(message);});await win.loadFile(path.join(root,'src/editor.html'));
    await ev("(()=>{const s=document.createElement('style');s.textContent='.r-vector-editor-overlay,.r-vector-editor-overlay *{animation:none!important;transition:none!important}';document.head.append(s);window.qaSerialize=XMLSerializer.prototype.serializeToString;window.qaSnapshots=0;XMLSerializer.prototype.serializeToString=function(...args){qaSnapshots++;return qaSerialize.apply(this,args)};})()");
    const fixture='<svg xmlns="http://www.w3.org/2000/svg" width="600" height="400"><style>.cssPaint{fill:#cc3311!important}</style><defs><linearGradient id="shared"><stop stop-color="red"/><stop offset="1" stop-color="blue"/></linearGradient></defs><rect id="a" class="cssPaint" x="50" y="60" width="90" height="70" stroke="black" stroke-width="3"/><rect id="b" x="190" y="60" width="90" height="70" fill="#008844"/><rect id="locked" x="330" y="60" width="90" height="70" fill="#663399" data-locked="true"/><rect id="g1" x="50" y="210" width="90" height="60" fill="url(#shared)"/><rect id="g2" x="190" y="210" width="90" height="60" fill="url(#shared)"/></svg>';
    for(const live of [false,true]){
        await open(fixture,live);await click('#a');assert.equal(await fill('a'),'rgb(204, 51, 17)');
        assert.equal(await ev("!!document.querySelector('.r-ve-swatches')"),false,'Palette must build lazily');
        assert.equal(await ev("getComputedStyle(document.querySelector('.r-ve-fill')).display"),'none');
        await ev('qaSnapshots=0');await click('[data-paint=fill]');assert.equal(await ev('qaSnapshots'),0,'Opening palette serialized the document');
        assert.equal(await ev("document.querySelectorAll('.r-ve-swatches>.r-ve-swatch-grid button').length"),72);
        const initial=await ev("document.querySelector('#a').outerHTML");
        const p=await ev("(()=>{const r=document.querySelector('.r-ve-swatches .r-ve-swatch[data-value=\"#ed1c24\"]').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()");
        win.webContents.sendInputEvent({type:'mouseMove',x:Math.round(p.x),y:Math.round(p.y)});await tick();assert.equal(await ev("document.querySelector('#a').outerHTML"),initial);assert.equal(await ev('qaSnapshots'),0);
        await click(swatch('#00a651'));assert.equal(await fill('a'),'rgb(0, 166, 81)');assert.equal(await ev('qaSnapshots'),1,'One click must create only one history snapshot');
        await click('[data-tool=undo]');await click('#a');assert.equal(await fill('a'),'rgb(204, 51, 17)');
        await click('[data-paint=stroke]');await click(swatch('none'));assert.equal(await ev("getComputedStyle(document.querySelector('#a')).stroke"),'none');assert.equal(await ev("document.querySelector('[data-paint=stroke] .r-ve-paint-chip').dataset.kind"),'none');
        await key('Escape');assert.equal(await ev("document.querySelector('#a').getAttribute('data-ve-selected')"),'true');
        await click('#b',true);assert.equal(await ev("document.querySelector('[data-paint=fill] .r-ve-paint-chip').dataset.kind"),'mixed');
        await click('[data-paint=fill]');await ev('qaSnapshots=0');await click(swatch('#ed1c24'));assert.equal(await fill('a'),await fill('b'));assert.equal(await ev('qaSnapshots'),1);
        await click('#locked',true);await click('[data-paint=fill]');await click(swatch('#2e3192'));assert.equal(await fill('locked'),'rgb(102, 51, 153)');assert.equal(await ev("document.querySelector('[data-paint=fill] .r-ve-paint-chip').dataset.kind"),'mixed');
        await click(swatch('none'));assert.equal(await fill('a'),'none');assert.equal(await fill('b'),'none');assert.equal(await fill('locked'),'rgb(102, 51, 153)');
        await key('Escape');await click('[data-paint=fill]');await click('.r-ve-swatch-hex');await ev("document.querySelector('.r-ve-swatch-hex').value='#123ABC';true");await key('Return');assert.equal(await fill('b'),'rgb(18, 58, 188)');
        await ev("document.querySelector('.r-ve-swatch-hex').value='#GGGGGG';true");const valid=await fill('b');await key('Return');assert.equal(await fill('b'),valid);assert.equal(await ev("document.querySelector('.r-ve-swatch-hex').getAttribute('aria-invalid')"),'true');
        await ev("window.qaCustom=0;document.querySelector('.r-ve-fill').showPicker=()=>qaCustom++;true");await click('.r-ve-swatches [data-action=custom]');assert.equal(await ev('qaCustom'),1);
        await click('#g1');assert.equal(await ev("document.querySelector('[data-paint=fill] .r-ve-paint-chip').dataset.kind"),'gradient');
        await click('[data-paint=fill]');await click('.r-ve-swatches [data-action=gradient]');await wait("!!document.querySelector('.r-ve-gradient-panel')");assert.equal(await ev("!!document.querySelector('.r-ve-grad-apply,.r-ve-grad-remove')"),false);
        // Input alone must change the actual SVG, with no snapshot until the gesture ends.
        await ev("(()=>{qaSnapshots=0;window.qaOriginal=document.querySelector('#shared').outerHTML;const c=document.querySelector('.r-ve-grad-color');c.value='#00ff00';c.dispatchEvent(new Event('input',{bubbles:true}));})()");await tick();
        assert.equal(await ev('qaSnapshots'),0);assert.ok((await fill('g1')).includes('#bioeng_grad_'));assert.equal(await ev("document.querySelector('#shared').outerHTML"),await ev('qaOriginal'));
        assert.equal(await ev("document.querySelector('.r-ve-viewport>svg:first-child linearGradient:last-of-type stop').getAttribute('stop-color')"),'#00ff00');
        await ev("document.querySelector('.r-ve-grad-color').dispatchEvent(new Event('change',{bubbles:true}));true");assert.equal(await ev('qaSnapshots'),1);
        assert.equal(await ev("document.querySelectorAll('.r-ve-grad-stops-list').length"),0,'Per-stop rows must be removed');
        assert.equal(await ev("document.querySelectorAll('.r-ve-grad-position').length"),1,'There must be one shared position control');
        await ev('qaSnapshots=0');await click('.r-ve-grad-marker:last-child');
        assert.equal(await ev("document.querySelectorAll('.r-ve-grad-marker.is-selected').length"),1);
        assert.equal(await ev("document.querySelector('.r-ve-grad-position').value"),'100');
        assert.equal(await ev('qaSnapshots'),0,'Selecting a stop must not modify the document');
        await ev("(()=>{const o=document.querySelector('.r-ve-grad-opacity');o.value=35;o.dispatchEvent(new Event('input',{bubbles:true}));})()");await tick();
        assert.equal(await ev("document.querySelector('.r-ve-viewport>svg:first-child linearGradient:last-of-type stop:last-child').getAttribute('stop-opacity')"),'0.35');
        assert.equal(await ev("document.querySelector('.r-ve-viewport>svg:first-child linearGradient:last-of-type stop:first-child').getAttribute('stop-opacity')"),'1');
        assert.equal(await ev('qaSnapshots'),0,'Opacity input must remain a live preview');
        await ev("document.querySelector('.r-ve-grad-opacity').dispatchEvent(new Event('change',{bubbles:true}));true");assert.equal(await ev('qaSnapshots'),1);
        await click('.r-ve-grad-marker:first-child');assert.equal(await ev("document.querySelector('.r-ve-grad-opacity').value"),'100');
        await click('.r-ve-grad-marker:last-child');assert.equal(await ev("document.querySelector('.r-ve-grad-opacity').value"),'35');
        assert.ok((await ev("document.querySelector('.r-ve-grad-preview').style.backgroundImage")).includes('0.35'),'The bar must preview transparency');
        // A burst of angle input reuses one definition and commits just once.
        await ev("(()=>{qaSnapshots=0;window.qaGradCount=document.querySelectorAll('.r-ve-viewport>svg:first-child linearGradient,.r-ve-viewport>svg:first-child radialGradient').length;const a=document.querySelector('.r-ve-grad-angle');for(let i=1;i<=80;i++){a.value=i;a.dispatchEvent(new Event('input',{bubbles:true}));}})()");await tick();assert.equal(await ev('qaSnapshots'),0);
        assert.equal(await ev("document.querySelectorAll('.r-ve-viewport>svg:first-child linearGradient,.r-ve-viewport>svg:first-child radialGradient').length"),await ev('qaGradCount'));
        await ev("document.querySelector('.r-ve-grad-angle').dispatchEvent(new Event('change',{bubbles:true}));true");assert.equal(await ev('qaSnapshots'),1);
        await click('.r-ve-grad-type[data-type=radial]');assert.ok(await ev("document.querySelector('.r-ve-viewport>svg:first-child radialGradient[id^=bioeng_grad_]')!==null"));
        assert.equal(await ev("!!document.querySelector('.r-ve-grad-add-stop')"),false);
        assert.equal(await ev("parseFloat(getComputedStyle(document.querySelector('.r-ve-grad-preview')).height)"),16);
        await click('.r-ve-grad-preview');assert.equal(await ev("document.querySelector('.r-ve-viewport>svg:first-child radialGradient[id^=bioeng_grad_]').querySelectorAll('stop').length"),3);
        assert.equal(await ev("document.querySelector('.r-ve-grad-position').value"),'50','New stop must become selected');
        assert.equal(await ev("document.querySelector('.r-ve-grad-opacity').value"),'100','New stop must not inherit opacity from a reused SVG node');
        assert.equal(await ev("document.querySelector('.r-ve-viewport>svg:first-child radialGradient[id^=bioeng_grad_]').querySelectorAll('stop')[1].getAttribute('stop-opacity')"),'1');
        await ev("(()=>{qaSnapshots=0;const p=document.querySelector('.r-ve-grad-position');p.value=5;p.dispatchEvent(new Event('input',{bubbles:true}));})()");await tick();
        assert.equal(await ev("document.querySelector('.r-ve-viewport>svg:first-child radialGradient[id^=bioeng_grad_]').querySelectorAll('stop')[1].getAttribute('offset')"),'0.05');
        assert.equal(await ev('qaSnapshots'),0);
        await ev("document.querySelector('.r-ve-grad-position').dispatchEvent(new Event('change',{bubbles:true}));true");assert.equal(await ev('qaSnapshots'),1);
        // Chromium must deliver the two physical clicks to the same circle.
        await ev("(()=>{window.qaStopPicker=0;window.qaStopPickerInput=null;window.qaShowStopPicker=HTMLInputElement.prototype.showPicker;HTMLInputElement.prototype.showPicker=function(){if(this.closest('.r-ve-grad-selected-stop')){qaStopPicker++;qaStopPickerInput=this;}else qaShowStopPicker.call(this);};qaSnapshots=0;})()");
        const dot=await ev("(()=>{const r=document.querySelectorAll('.r-ve-grad-marker')[1].getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2};})()");
        win.webContents.sendInputEvent({type:'mouseMove',x:Math.round(dot.x),y:Math.round(dot.y)});
        for(const clickCount of [1,2]){for(const type of ['mouseDown','mouseUp'])win.webContents.sendInputEvent({type,x:Math.round(dot.x),y:Math.round(dot.y),button:'left',clickCount});await pause(30);}
        await pause();assert.equal(await ev('qaStopPicker'),1,'Double-click did not open the existing stop color control');assert.equal(await ev('qaSnapshots'),0,'Clicking a circle changed the gradient');
        assert.equal(await ev("document.querySelectorAll('.r-ve-grad-marker').length"),3,'Double-click added an unwanted stop');
        await ev("qaStopPickerInput.value='#abcdef';qaStopPickerInput.dispatchEvent(new Event('input',{bubbles:true}));true");await tick();
        assert.equal(await ev("document.querySelector('.r-ve-viewport>svg:first-child radialGradient[id^=bioeng_grad_]').querySelectorAll('stop')[1].getAttribute('stop-color')"),'#abcdef');
        await ev("qaStopPickerInput.dispatchEvent(new Event('change',{bubbles:true}));HTMLInputElement.prototype.showPicker=qaShowStopPicker;qaStopPickerInput=null;true");assert.equal(await ev('qaSnapshots'),1);
        const marker=await ev("(()=>{const r=document.querySelectorAll('.r-ve-grad-marker')[1].getBoundingClientRect(),b=document.querySelector('.r-ve-grad-preview').getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2,toX:b.x+b.width*.7};})()");
        await ev('qaSnapshots=0');win.webContents.sendInputEvent({type:'mouseDown',x:Math.round(marker.x),y:Math.round(marker.y),button:'left',clickCount:1});win.webContents.sendInputEvent({type:'mouseMove',x:Math.round(marker.toX),y:Math.round(marker.y),button:'left'});await tick();
        assert.equal(await ev("document.querySelector('.r-ve-viewport>svg:first-child radialGradient[id^=bioeng_grad_]').querySelectorAll('stop')[1].getAttribute('offset')"),'0.7');assert.equal(await ev('qaSnapshots'),0);
        win.webContents.sendInputEvent({type:'mouseUp',x:Math.round(marker.toX),y:Math.round(marker.y),button:'left',clickCount:1});await pause();assert.equal(await ev('qaSnapshots'),1);
        assert.equal(await ev("document.querySelector('.r-ve-grad-position').value"),'70');
        assert.equal(await ev("document.querySelectorAll('.r-ve-grad-marker.is-selected').length"),1);
        fs.writeFileSync(path.join(output,live?'live-gradient.png':'ordinary-gradient.png'),(await win.webContents.capturePage()).toPNG());
        // Crossing other stops keeps the shared fields bound to the same object.
        await ev("(()=>{const p=document.querySelector('.r-ve-grad-position');p.value=100;p.dispatchEvent(new Event('input',{bubbles:true}));p.dispatchEvent(new Event('change',{bubbles:true}));const c=document.querySelector('.r-ve-grad-color');c.value='#654321';c.dispatchEvent(new Event('input',{bubbles:true}));c.dispatchEvent(new Event('change',{bubbles:true}));})()");await tick();
        assert.equal(await ev("document.querySelector('.r-ve-grad-color').value"),'#654321');
        assert.equal(await ev("document.querySelector('.r-ve-grad-opacity').value"),'100');
        await click('.r-ve-grad-delete');assert.equal(await ev("document.querySelector('.r-ve-viewport>svg:first-child radialGradient[id^=bioeng_grad_]').querySelectorAll('stop').length"),2);
        assert.equal(await ev("document.querySelector('.r-ve-grad-delete').disabled"),true);
        await click('[data-paint=fill]');await click(swatch('#00a651'));assert.equal(await fill('g1'),'rgb(0, 166, 81)');assert.equal(await ev("!!document.querySelector('.r-ve-gradient-panel')"),false);assert.ok(await ev("!!document.querySelector('#shared')"),'Changing one gradient deleted another object\'s definition');assert.ok((await fill('g2')).includes('#shared'));
        await click('[data-tool=undo]');await click('#g1');assert.ok((await fill('g1')).includes('#bioeng_grad_'),'Undo solid color did not restore gradient');
        await click('[data-paint=fill]');await click('.r-ve-swatches [data-action=gradient]');await click('.r-ve-grad-marker:last-child');
        assert.equal(await ev("document.querySelector('.r-ve-grad-opacity').value"),'35','Reopening an undone gradient must retain stop opacity');
        await ev("document.querySelector('.r-ve-grad-marker:last-child').focus();true");await key('Space');
        assert.equal(await ev("document.querySelector('.r-ve-grad-position').value"),'100');
        await key('Delete');assert.ok(await ev("!!document.querySelector('#g1')"),'Marker keyboard input deleted the artwork');
        assert.equal(await ev("document.querySelectorAll('.r-ve-grad-marker').length"),2);
        await click('.r-ve-grad-preview');assert.equal(await ev("document.querySelectorAll('.r-ve-grad-marker').length"),3);
        await ev("document.querySelector('.r-ve-grad-marker.is-selected').focus();true");await key('Delete');
        assert.equal(await ev("document.querySelectorAll('.r-ve-grad-marker').length"),2,'Delete must remove the selected stop');
        await click('.r-ve-grad-close');
        await click('[data-paint=stroke]');await click('.r-ve-swatches [data-action=gradient]');assert.ok((await ev("getComputedStyle(document.querySelector('#g1')).stroke")).includes('#bioeng_grad_'),'Opening a new gradient must preview it immediately');
        await ev("(()=>{const c=document.querySelector('.r-ve-grad-color');c.value='#123456';c.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('.r-ve-grad-close').click();})()");assert.equal(await ev("document.querySelectorAll('.r-ve-viewport>svg:first-child linearGradient[id^=bioeng_grad_] stop')[0].getAttribute('stop-color')"),'#123456','Closing must flush pending input');
        await click('[data-paint=stroke]');await click(swatch('#000000'));assert.equal(await ev("getComputedStyle(document.querySelector('#g1')).stroke"),'rgb(0, 0, 0)');await key('Escape');
        await ev(`(()=>{const svg=document.querySelector('.r-ve-viewport>svg:first-child'),defs=svg.querySelector('defs');defs.insertAdjacentHTML('beforeend','<pattern id="qaPattern" width="4" height="4" patternUnits="userSpaceOnUse"><rect width="2" height="4" fill="#123456"/></pattern>');document.querySelector('#g1').setAttribute('fill','url(#qaPattern)');document.querySelector('#g1').style.setProperty('fill','url(#qaPattern)','important');})()`);
        await click('#g2');await click('#g1');assert.equal(await ev("document.querySelector('[data-paint=fill] .r-ve-paint-chip').dataset.kind"),'pattern');assert.ok((await ev("getComputedStyle(document.querySelector('[data-paint=fill] .r-ve-paint-chip')).backgroundImage")).includes('repeating-linear-gradient'),'Pattern preview must use a lightweight CSS indicator');
        await click('[data-paint=fill]');await tick();fs.writeFileSync(path.join(output,live?'live.png':'ordinary.png'),(await win.webContents.capturePage()).toPNG());await key('Escape');
        report.checks.push((live?'live':'ordinary')+': swatches, CSS paints, mixed/locked selections, HEX validation, selected-stop shared color/opacity/position, live SVG transparency and checkerboard preview, stop identity across reordering, native drag/double-click and keyboard deletion, opacity after undo/reopen, shared definitions, solid replacement, one-step undo and pending-input close');await close();
    }
    const large='<svg xmlns="http://www.w3.org/2000/svg" width="600" height="400">'+Array.from({length:5000},(_,i)=>`<rect id="p${i}" x="${10+i%100*5}" y="${10+Math.floor(i/100)*5}" width="3" height="3" fill="#004488"/>`).join('')+'</svg>';
    await open(large,true);
    await ev("(()=>{const e=document.querySelector('#p0'),r=e.getBoundingClientRect();for(const type of ['mousedown','mouseup','contextmenu'])e.dispatchEvent(new MouseEvent(type,{bubbles:true,cancelable:true,clientX:r.x+1,clientY:r.y+1}));document.querySelector('.r-ve-ctx-item[data-action=\"sel-type\"]').click();})()");await tick();
    assert.equal(await ev("document.querySelectorAll('.r-ve-viewport>svg:first-child [data-ve-selected]').length"),5000);
    await ev('qaSnapshots=0');await click('[data-paint=fill]');
    const stats=await ev("(()=>{const popup=document.querySelector('.r-ve-swatches'),trigger=document.querySelector('[data-paint=fill]'),times=[],n=popup.querySelectorAll('*').length;for(let i=0;i<20;i++){trigger.click();const t=performance.now();trigger.click();times.push(performance.now()-t);}const t=performance.now();popup.querySelector('[data-value=\"#00a651\"]').click();return {warmOpenMs:times.sort((a,b)=>a-b)[10],batchMs:performance.now()-t,snapshots:qaSnapshots,nodesBefore:n,nodesAfter:popup.querySelectorAll('*').length,reused:popup===document.querySelector('.r-ve-swatches')};})()");
    assert.equal(stats.snapshots,1);assert.equal(stats.reused,true);assert.ok(stats.nodesAfter-stats.nodesBefore<=13,'Palette DOM grew without bounds');assert.ok(stats.warmOpenMs<100&&stats.batchMs<1500,'Palette work exceeded bounds');assert.equal(await fill('p4999'),'rgb(0, 166, 81)');report.performance=stats;
    await click('.r-ve-swatches [data-action=gradient]');assert.ok((await fill('p4999')).includes('#bioeng_grad_'));
    const burst=await ev("(()=>{qaSnapshots=0;const a=document.querySelector('.r-ve-grad-angle'),t=performance.now();for(let i=1;i<=100;i++){a.value=i;a.dispatchEvent(new Event('input',{bubbles:true}));}return performance.now()-t;})()");await tick();
    assert.equal(await ev('qaSnapshots'),0);assert.equal(await ev("document.querySelectorAll('.r-ve-viewport>svg:first-child linearGradient').length"),1,'Drag created extra gradient definitions');
    await ev("document.querySelector('.r-ve-grad-angle').dispatchEvent(new Event('change',{bubbles:true}));true");assert.equal(await ev('qaSnapshots'),1);report.performance.gradientInputBurstMs=burst;
    await close();assert.equal(await ev("document.querySelectorAll('.r-ve-swatches').length"),0);report.checks.push('5,000-object batch: reused constant-size popup, no hover snapshots, single commit and cleanup');finish();
}catch(error){finish(error);}});
