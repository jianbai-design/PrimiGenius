'use strict';
const {app,BrowserWindow,ipcMain}=require('electron');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const fillets=require('../src/vector-corners');
const root=path.resolve(__dirname,'..'),output=path.resolve(process.argv[2]||'');
if(path.dirname(output)!==root||!path.basename(output).startsWith('.tmp-vector-'))throw Error('A disposable workspace directory is required');
fs.mkdirSync(output,{recursive:true});app.setPath('userData',path.join(output,'profile'));
for(const flag of ['no-sandbox','disable-gpu','in-process-gpu'])app.commandLine.appendSwitch(flag);
ipcMain.on('get-backend-port-sync',e=>e.returnValue=65534);
require('../src/vector-export-main').register({app,ipcMain,BrowserWindow,dialog:{showSaveDialog:async()=>({canceled:false,filePath:path.join(output,'rounded.svg')})}});
const report={checks:[],errors:[],performance:{}};let win;
const timeout=setTimeout(()=>finish(Error('Corner acceptance timed out')),180000);
function finish(error){clearTimeout(timeout);if(error)report.errors.push(error.stack||String(error));report.result=report.errors.length?'FAIL':'PASS';fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));if(win&&!win.isDestroyed())win.destroy();app.exit(report.errors.length?1:0);}
const ev=async code=>{try{return await win.webContents.executeJavaScript(code);}catch(error){throw Error(error.message+'\nScript: '+code.slice(0,250));}};
const pause=ms=>new Promise(r=>setTimeout(r,ms||40)),tick=()=>ev('new Promise(r=>requestAnimationFrame(r))');
async function wait(code){for(let i=0;i<150;i++){if(await ev(code))return;await pause();}throw Error('Timed out: '+code);}
async function click(selector){const p=await ev(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)throw Error('Missing target '+${JSON.stringify(selector)});const r=el.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);for(const type of ['mouseMove','mouseDown','mouseUp'])win.webContents.sendInputEvent({type,x:Math.round(p.x),y:Math.round(p.y),button:'left',clickCount:1});await pause();}
async function key(keyCode,modifiers=[]){for(const type of ['keyDown','keyUp'])win.webContents.sendInputEvent({type,keyCode,modifiers});await pause();}
async function open(svg,live){await ev(`currentLang='zh';window.__editorStandalone=${live};window.qaCornerInstance={config:{id:'corners',name:'corners',parameters:[],packages:[]},__standaloneMode:true,_liveEditorSessions:new Map(),outDir:''};renderRPanelUI(qaCornerInstance,document.createElement('div'));qaCornerInstance.__openVectorEditor({name:'corners.svg',path:'',ext:'.svg'},{sessionKey:'corner-'+Date.now(),liveMode:${live},initialSvgText:${JSON.stringify(svg)}});true`);await pause(150);await tick();}
async function close(){await ev("window.__editorStandalone=false;document.querySelector('.r-ve-close-btn').click();true");if(await ev("!!document.querySelector('.ve-confirm')"))await click('.ve-confirm [data-choice="1"]');await wait("!document.querySelector('.r-vector-editor-overlay')");}
const handles=()=>ev("document.querySelectorAll('.ve-corner-handle').length");
async function startDrag(id,index=0,localDX=16,localDY=16){
    const p=await ev(`(()=>{const el=document.querySelector(${JSON.stringify('#'+id)}),m=el.getScreenCTM(),n=document.querySelectorAll('.ve-corner-handle')[${index}];return {x:Number(n.getAttribute('cx')),y:Number(n.getAttribute('cy')),dx:m.a*${localDX}+m.c*${localDY},dy:m.b*${localDX}+m.d*${localDY}};})()`);
    win.webContents.sendInputEvent({type:'mouseMove',x:Math.round(p.x),y:Math.round(p.y)});await pause();
    win.webContents.sendInputEvent({type:'mouseDown',x:Math.round(p.x),y:Math.round(p.y),button:'left',clickCount:1});
    win.webContents.sendInputEvent({type:'mouseMove',x:Math.round(p.x+p.dx),y:Math.round(p.y+p.dy),button:'left'});await tick();return p;
}
async function endDrag(p){win.webContents.sendInputEvent({type:'mouseUp',x:Math.round(p.x+p.dx),y:Math.round(p.y+p.dy),button:'left',clickCount:1});await pause();}
async function moveWithCorners(id,shortcuts=false){
    const sample=()=>ev(`(()=>{const r=document.querySelector(${JSON.stringify('#'+id)}).getBoundingClientRect();return {x:r.x,y:r.y,cx:r.x+r.width/2,cy:r.y+r.height/2,handles:[...document.querySelectorAll('.ve-corner-handle')].map(n=>{const b=n.getBoundingClientRect();return {x:b.x+b.width/2,y:b.y+b.height/2}})};})()`);
    const before=await sample();assert.ok(before.handles.length>=3);await ev('qaCornerSnapshots=0');
    for(const type of ['mouseMove','mouseDown'])win.webContents.sendInputEvent({type,x:Math.round(before.cx),y:Math.round(before.cy),button:'left',clickCount:1,modifiers:['alt']});
    win.webContents.sendInputEvent({type:'mouseMove',x:Math.round(before.cx+85),y:Math.round(before.cy+35),button:'left',modifiers:['alt']});await tick();
    const during=await sample(),dx=during.x-before.x,dy=during.y-before.y;assert.ok(dx>80&&dy>30,'Shape did not move');
    assert.equal(during.handles.length,before.handles.length);
    during.handles.forEach((p,i)=>{assert.ok(Math.abs(p.x-before.handles[i].x-dx)<1,'Corner handle lagged behind horizontal movement');assert.ok(Math.abs(p.y-before.handles[i].y-dy)<1,'Corner handle lagged behind vertical movement');});
    assert.equal(await ev('qaCornerSnapshots'),0,'Moving serialized the document before release');
    win.webContents.sendInputEvent({type:'mouseUp',x:Math.round(before.cx+85),y:Math.round(before.cy+35),button:'left',clickCount:1,modifiers:['alt']});await pause();
    const after=await sample();after.handles.forEach((p,i)=>{assert.ok(Math.abs(p.x-during.handles[i].x)<1&&Math.abs(p.y-during.handles[i].y)<1,'Release changed the corner positions');});
    assert.equal(await ev('qaCornerSnapshots'),1,'Moving must create one undo snapshot');
    if(shortcuts){
        await ev("document.querySelector('.r-ve-paint-trigger').focus();true");await key('z',['control']);
        let restored=await sample();assert.ok(Math.abs(restored.x-before.x)<.01&&Math.abs(restored.y-before.y)<.01,'Ctrl+Z failed with toolbar focus');
        await key('y',['control']);restored=await sample();assert.ok(Math.abs(restored.x-after.x)<.01&&Math.abs(restored.y-after.y)<.01,'Ctrl+Y failed to redo the previous step');
        await ev("document.querySelector('.r-ve-paint-trigger').focus();document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Z',code:'KeyZ',ctrlKey:true,bubbles:true,cancelable:true}));true");
        restored=await sample();assert.ok(Math.abs(restored.x-before.x)<.01&&Math.abs(restored.y-before.y)<.01,'Uppercase Ctrl+Z failed');
    }else await click('[data-tool=undo]');
    await click('#'+id);
}
async function checkPropertyBar(){
    const controls=await ev(`(()=>{const bar=document.querySelector('.r-ve-props-bar');return [...bar.children].filter(n=>!n.hidden&&getComputedStyle(n).display!=='none'&&!n.classList.contains('r-ve-prop-sep')).map(n=>{const r=n.getBoundingClientRect();return {tag:n.tagName,class:n.className,height:r.height,center:r.y+r.height/2,margin:getComputedStyle(n).marginTop}});})()`);
    const center=controls[0].center;
    controls.forEach(c=>{assert.ok(Math.abs(c.center-center)<1,'Property bar vertical alignment: '+JSON.stringify(c));if(['INPUT','SELECT','BUTTON'].includes(c.tag))assert.equal(c.height,28,'Property bar control height: '+c.class);});
}
async function checkShiftMove(id,group=false){
    await click('#'+id);
    const addOther=async(count=2)=>{
        const p=await ev("(()=>{const r=document.querySelector('#ellipse').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()");
        for(const type of ['mouseMove','mouseDown','mouseUp'])win.webContents.sendInputEvent({type,x:Math.round(p.x),y:Math.round(p.y),button:'left',clickCount:1,modifiers:['shift']});await pause();
        assert.equal(await ev("document.querySelectorAll('.r-ve-viewport>svg:first-child [data-ve-selected]').length"),count,'Shift click did not toggle the second shape');
    };
    if(group)await addOther();
    const sample=()=>ev(`(()=>{const r=document.querySelector(${JSON.stringify('#'+id)}).getBoundingClientRect(),other=document.querySelector('#ellipse').getBoundingClientRect();return {x:r.x,y:r.y,cx:r.x+r.width/2,cy:r.y+r.height/2,otherX:other.x,otherY:other.y};})()`);
    for(const vertical of [false,true]){
        const before=await sample();await ev('qaCornerSnapshots=0');
        const input=(type,dx,dy,shift=true)=>win.webContents.sendInputEvent({type,x:Math.round(before.cx+dx),y:Math.round(before.cy+dy),button:'left',clickCount:1,modifiers:shift?['shift']:[]});
        input('mouseMove',0,0);input('mouseDown',0,0);
        input('mouseMove',vertical?15:85,vertical?70:18);await tick();
        let during=await sample();assert.ok(vertical?during.y-before.y>60:during.x-before.x>75,'Shift drag did not start: '+JSON.stringify({id,group,vertical,before,during,selected:await ev("[...document.querySelectorAll('[data-ve-selected]')].map(n=>n.id)")}));
        assert.ok(Math.abs(vertical?during.x-before.x:during.y-before.y)<.01,'Shift drag left its axis');
        input('mouseMove',vertical?110:25,vertical?30:90);await tick();during=await sample();
        assert.ok(Math.abs(vertical?during.x-before.x:during.y-before.y)<.01,'Shift changed axis during one hold');
        if(group){assert.ok(Math.abs(during.otherX-before.otherX-(during.x-before.x))<.01);assert.ok(Math.abs(during.otherY-before.otherY-(during.y-before.y))<.01);}
        input('mouseMove',45,65,false);await tick();during=await sample();
        assert.ok(during.x-before.x>35&&during.y-before.y>55,'Releasing Shift did not restore free movement');
        input('mouseMove',15,90);await tick();during=await sample();
        assert.ok(Math.abs(during.x-before.x)<.01&&during.y-before.y>80,'Repressing Shift failed to choose a new axis');
        assert.equal(await ev('qaCornerSnapshots'),0,'Shift drag serialized the document before release');
        input('mouseUp',15,90);await pause();assert.equal(await ev('qaCornerSnapshots'),1,'Shift drag must create one undo snapshot');
        await click('[data-tool=undo]');await click('#'+id);
        if(group&&!await ev("document.querySelector('#ellipse').hasAttribute('data-ve-selected')"))await addOther();
    }
    if(group)await addOther(1);
    else{
        const p=await sample();for(const type of ['mouseMove','mouseDown','mouseUp'])win.webContents.sendInputEvent({type,x:Math.round(p.cx),y:Math.round(p.cy),button:'left',clickCount:1,modifiers:['shift']});await pause();
        assert.equal(await ev(`document.querySelector(${JSON.stringify('#'+id)}).hasAttribute('data-ve-selected')`),false,'Shift click no longer removes a selected shape');
    }
    await click('#'+id);
}
app.whenReady().then(async()=>{try{
    const box=fillets.geometry([[0,0],[120,0],[120,80],[0,80]]);assert.equal(box.max,40);
    assert.equal((fillets.path(box,500).match(/A/g)||[]).length,4);assert.ok(!fillets.path(box,0).includes('A'));
    assert.equal(fillets.geometry([[0,0],[50,0],[50,50],[25,20],[0,50]]),null);
    assert.equal(fillets.geometry([[0,0],[0,0],[10,10]]),null);
    assert.equal(fillets.geometry([[0,0],null,[10,10]]),null);
    const reverse=fillets.geometry([[0,0],[0,80],[120,80],[120,0]]);assert.equal(reverse.sweep,0);assert.equal(reverse.max,40);
    report.checks.push('bounded convex fillet geometry, exact native arcs, zero/max radius and degenerate inputs');
    win=new BrowserWindow({show:false,width:1320,height:960,webPreferences:{nodeIntegration:true,contextIsolation:false,backgroundThrottling:false}});
    win.webContents.on('console-message',(_event,level,message)=>{if(level>=3)report.errors.push(message);});await win.loadFile(path.join(root,'src/editor.html'));
    await ev("(()=>{const style=document.createElement('style');style.textContent='.r-vector-editor-overlay *{animation:none!important;transition:none!important}';document.head.appendChild(style);window.qaSerialize=XMLSerializer.prototype.serializeToString;window.qaCornerSnapshots=0;XMLSerializer.prototype.serializeToString=function(...args){qaCornerSnapshots++;return qaSerialize.apply(this,args)};})()");
    const fixture='<svg xmlns="http://www.w3.org/2000/svg" width="600" height="420"><style>polygon{fill:#ed1c24;stroke:#223344;stroke-width:2}</style><rect id="rect" x="40" y="45" width="140" height="95" fill="#009cde" stroke="#223344" stroke-width="2"/><g transform="translate(320 110) rotate(27) skewX(12)"><rect id="rotated" x="-50" y="-35" width="100" height="70" fill="#66bb6a"/></g><polygon id="poly" points="90,220 170,265 140,345 40,340 20,265"/><rect id="locked" data-locked="true" x="440" y="55" width="90" height="80" fill="#7744aa"/><ellipse id="ellipse" cx="330" cy="290" rx="55" ry="40" fill="#ed8a23"/></svg>';
    for(const live of [false,true]){
        await open(fixture,live);await click('#rect');assert.equal(await handles(),4);
        await moveWithCorners('rect',true);await checkPropertyBar();
        await checkShiftMove('rect');await checkShiftMove('rotated');await checkShiftMove('rect',true);await click('#rect');
        assert.equal(await ev("getComputedStyle(document.querySelector('.ve-selection-bounds')).fill"),'none','Imported polygon CSS filled the selection frame');
        await ev('qaCornerSnapshots=0');let p=await startDrag('rect');
        assert.ok(await ev("Number(document.querySelector('#rect').getAttribute('rx'))>10"),'Native radius did not update live');
        assert.equal(await ev('qaCornerSnapshots'),0,'Dragging serialized the full document');
        assert.equal(await ev("document.querySelector('#rect').tagName"),'rect');
        await endDrag(p);assert.equal(await ev('qaCornerSnapshots'),1,'A corner gesture must create one undo snapshot');
        const radius=await ev("document.querySelector('#rect').getAttribute('rx')");
        await click('[data-tool=undo]');assert.equal(await ev("document.querySelector('#rect').hasAttribute('rx')"),false);
        await click('[data-tool=redo]');assert.equal(await ev("document.querySelector('#rect').getAttribute('rx')"),radius);
        await click('#rect');await ev('qaCornerSnapshots=0');p=await startDrag('rect');await key('Escape');await endDrag(p);
        assert.equal(await ev("document.querySelector('#rect').getAttribute('rx')"),radius,'Escape failed to restore the original radius');assert.equal(await ev('qaCornerSnapshots'),0);
        await ev("document.querySelector('.ve-corner-handle').focus();true");await key('End');
        assert.equal(await ev("Number(document.querySelector('#rect').getAttribute('rx'))"),47.5);
        await ev("document.querySelector('.ve-corner-handle').focus();true");await key('Home');assert.equal(await ev("Number(document.querySelector('#rect').getAttribute('rx'))"),0);
        await click('#rotated');assert.equal(await handles(),4);await moveWithCorners('rotated');p=await startDrag('rotated');await endDrag(p);
        assert.ok(await ev("Number(document.querySelector('#rotated').getAttribute('rx'))>10"),'Transformed local coordinate drag failed');
        assert.equal(await ev("document.querySelector('#rotated').getAttribute('transform')"),null,'Rounding changed the object transform');
        await click('#poly');assert.equal(await handles(),5);await ev('qaCornerSnapshots=0');p=await startDrag('poly',0,0,22);
        assert.equal(await ev("document.querySelector('#poly').tagName"),'path');assert.equal(await ev("getComputedStyle(document.querySelector('#poly')).fill"),'rgb(237, 28, 36)');
        assert.equal(await ev('qaCornerSnapshots'),0);assert.equal(await ev("(document.querySelector('#poly').getAttribute('d').match(/A/g)||[]).length"),5);
        await endDrag(p);assert.equal(await ev('qaCornerSnapshots'),1);assert.equal(await handles(),5);
        const roundedPath=await ev("document.querySelector('#poly').getAttribute('d')");
        await click('[data-tool=undo]');assert.equal(await ev("document.querySelector('#poly').tagName"),'polygon');
        await click('[data-tool=redo]');await click('#poly');assert.equal(await handles(),5);assert.equal(await ev("document.querySelector('#poly').getAttribute('d')"),roundedPath);
        await moveWithCorners('poly');
        await ev("document.querySelector('.ve-corner-handle').focus();true");await key('Home');assert.equal(await ev("document.querySelector('#poly').getAttribute('d').includes('A')"),false);
        await key('Right');assert.ok(await ev("document.querySelector('#poly').getAttribute('d').includes('A')"));
        await click('#ellipse');assert.equal(await handles(),0);
        await click('#locked');assert.equal(await handles(),0);
        await click('#rect');await click('[data-tool=anchor]');assert.equal(await handles(),0);
        await click('[data-tool=select]');await click('#rect');assert.equal(await handles(),4);
        await ev("document.querySelector('.ve-corner-handle').focus();true");await key('Right',['shift']);
        await tick();fs.writeFileSync(path.join(output,live?'live-rounded.png':'ordinary-rounded.png'),(await win.webContents.capturePage()).toPNG());
        await ev("window.qaRoundedSvg=new XMLSerializer().serializeToString(document.querySelector('.r-ve-viewport>svg:first-child'));true");
        assert.equal(await ev("qaRoundedSvg.includes('ve-corner-handle')"),false,'Transient controls entered the exported SVG');
        const saved=await ev('qaRoundedSvg');await close();await open(saved,live);await click('#poly');assert.equal(await handles(),5,'Reload lost editable polygon corners');await close();
        report.checks.push((live?'live':'ordinary')+': corner controls follow rectangles, nested transforms and rounded polygons during movement; property bar centers align with 28px controls; live radius, single-step undo/redo, Escape, keyboard bounds, styled polygon arcs and zero-radius restore, lock/type/tool filtering and reload without exported controls');
    }
    const large=fixture.replace('</svg>',Array.from({length:5000},(_,i)=>`<rect x="${400+i%50*2}" y="${210+Math.floor(i/50)}" width="1" height="1" fill="#444"/>`).join('')+'</svg>');
    await open(large,true);await click('#rect');await moveWithCorners('rect');await ev('qaCornerSnapshots=0');const p=await startDrag('rect',0,2,2);
    report.performance=await ev("(()=>{const handle=document.querySelector('.ve-corner-handle'),t=performance.now();for(let i=0;i<1000;i++)window.dispatchEvent(new MouseEvent('mousemove',{bubbles:true,cancelable:true,clientX:Number(handle.getAttribute('cx'))+i%30,clientY:Number(handle.getAttribute('cy'))+i%30}));return {inputBurstMs:performance.now()-t};})()");await tick();
    assert.equal(await ev('qaCornerSnapshots'),0);assert.equal(await handles(),4);assert.ok(report.performance.inputBurstMs<500,'Corner input work is not bounded');
    await endDrag(p);assert.equal(await ev('qaCornerSnapshots'),1);
    await close();assert.equal(await ev("document.querySelectorAll('.ve-corner-handles').length"),0);report.checks.push('5,000-object scene: 1,000 pointer samples coalesced, four controls, no mid-gesture document snapshots, one commit and cleanup');
    finish();
}catch(error){finish(error);}});
