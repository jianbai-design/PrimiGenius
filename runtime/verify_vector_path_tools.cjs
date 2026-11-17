'use strict';
const {app,BrowserWindow,ipcMain}=require('electron');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),output=path.resolve(process.argv[2]||'');
if(path.dirname(output)!==root||!path.basename(output).startsWith('.tmp-vector-'))throw Error('A disposable workspace directory is required');
fs.mkdirSync(output,{recursive:true});app.setPath('userData',path.join(output,'profile'));
for(const flag of ['no-sandbox','disable-gpu','in-process-gpu'])app.commandLine.appendSwitch(flag);
ipcMain.on('get-backend-port-sync',e=>e.returnValue=65534);
let cancelSave=false,win;
require('../src/vector-export-main').register({app,ipcMain,BrowserWindow,dialog:{showSaveDialog:async()=>cancelSave?{canceled:true}:{canceled:false,filePath:path.join(output,'paths.svg')}}});
const report={checks:[],errors:[]};
const timeout=setTimeout(()=>finish(Error('Path acceptance timed out')),180000);
function finish(error){clearTimeout(timeout);if(error)report.errors.push(error.stack||String(error));report.result=report.errors.length?'FAIL':'PASS';fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));if(win&&!win.isDestroyed())win.destroy();app.exit(report.errors.length?1:0);}
const ev=async code=>{try{return await win.webContents.executeJavaScript(code);}catch(error){throw Error(error.message+'\nScript: '+code.slice(0,350));}};
const pause=ms=>new Promise(r=>setTimeout(r,ms||70));
const tick=()=>ev('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))');
async function wait(code){for(let i=0;i<160;i++){if(await ev(code))return;await pause();}throw Error('Timed out: '+code);}
async function input(type,p,button='left',clickCount=1){win.webContents.sendInputEvent({type,x:Math.round(p.x),y:Math.round(p.y),button,clickCount});await pause();}
async function doubleClick(p){await input('mouseMove',p);for(const count of [1,2]){await input('mouseDown',p,'left',count);await input('mouseUp',p,'left',count);}}
async function clickPoint(p,button='left'){await input('mouseMove',p,button);await input('mouseDown',p,button);await input('mouseUp',p,button);}
async function click(selector,right=false){await ev(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({block:'nearest'});true`);const p=await ev(`(()=>{const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);assert.ok(await ev(`document.querySelector(${JSON.stringify(selector)}).contains(document.elementFromPoint(${p.x},${p.y}))`),'Obstructed: '+selector);await clickPoint(p,right?'right':'left');}
async function svgPoint(x,y){return ev(`(()=>{const m=document.querySelector('.r-ve-viewport>svg').getScreenCTM();return {x:m.a*${x}+m.c*${y}+m.e,y:m.b*${x}+m.d*${y}+m.f};})()`);}
async function drag(a,b){await input('mouseMove',a);await input('mouseDown',a);await input('mouseMove',b);await input('mouseUp',b);}
async function previewAt(x,y,curved=false){
    const before=await ev("document.querySelector('.r-ve-viewport>svg>path:last-child').getAttribute('d')");
    await input('mouseMove',await svgPoint(x,y));await wait("!!document.querySelector('.ve-pen-preview path')");
    const first=await ev("document.querySelector('.ve-pen-preview path').getAttribute('d')");
    if(curved)assert.ok(first.includes('C'),'Curvature preview is not curved');
    await input('mouseMove',await svgPoint(x+20,y+15));await tick();
    assert.notEqual(await ev("document.querySelector('.ve-pen-preview path').getAttribute('d')"),first,'Preview did not follow pointer');
    assert.equal(await ev("document.querySelector('.r-ve-viewport>svg>path:last-child').getAttribute('d')"),before,'Hover changed actual path');
    assert.equal(await ev("!!document.querySelector('.r-ve-viewport>svg:first-child .ve-pen-preview')"),false,'Preview entered SVG artwork');
    if(curved){win.webContents.invalidate();await tick();fs.writeFileSync(path.join(output,'live-preview.png'),(await win.webContents.capturePage()).toPNG());}
}
async function key(keyCode,modifiers=[]){win.webContents.sendInputEvent({type:'keyDown',keyCode,modifiers});if(keyCode==='Return')win.webContents.sendInputEvent({type:'char',keyCode:'\r',modifiers});win.webContents.sendInputEvent({type:'keyUp',keyCode,modifiers});await pause();}
async function pen(mode){await click('.ve-pen-tool',true);await wait("!document.querySelector('.ve-pen-menu').hidden");await click('[data-path-tool="'+mode+'"]');}
const nodes=()=>ev("document.querySelectorAll('.ve-node-overlays rect').length");
const keyA='file:c:\\outputs\\run-a\\same.svg',keyB='file:c:\\outputs\\run-b\\same.svg';
const copy=(key,action,svg)=>ev(`require('electron').ipcRenderer.invoke('vector-recovery',${JSON.stringify({key,action,svg})})`);
async function open(svg,source){await ev(`currentLang='en';window.__editorStandalone=true;_bootstrapStandaloneEditor('same.svg',${JSON.stringify(svg)},'tool::same.svg',${JSON.stringify(source)});true`);await pause(300);await tick();}
async function closeClean(){await ev("window.__editorStandalone=false;document.querySelector('.r-ve-close-btn').click();true");await wait("!document.querySelector('.r-vector-editor-overlay')");}
app.whenReady().then(async()=>{try{
    win=new BrowserWindow({show:false,width:1320,height:960,webPreferences:{nodeIntegration:true,contextIsolation:false,backgroundThrottling:false}});
    win.webContents.on('console-message',(_event,level,message)=>{if(level>=3)report.errors.push(message);});
    await win.loadFile(path.join(root,'src/editor.html'));
    const fixture='<svg xmlns="http://www.w3.org/2000/svg" width="600" height="400"><rect class="plain" x="60" y="40" width="70" height="40" fill="blue" stroke="#222" stroke-width="2"/><path id="curve" d="M80 120C130 80 180 160 230 120L230 180L80 180Z" fill="#e0c080" stroke="#222" stroke-width="2"/></svg>';
    await copy('tool::same.svg','write',fixture.replace('curve','legacy'));await copy(keyA,'clear');await copy(keyB,'clear');
    await open(fixture,'C:\\outputs\\run-a\\same.svg');
    assert.equal(await ev("!!document.querySelector('.ve-confirm')"),false,'Legacy shared backup leaked into new file identity');
    assert.equal(await ev("!!document.querySelector('.r-ve-tool[data-tool=pen]')"),false,'Freehand pencil must be removed');
    assert.equal(await ev("document.querySelectorAll('.r-ve-toolbar-center>.r-ve-tool[data-tool=polygon],.r-ve-toolbar-center>.r-ve-tool[data-tool=square],.r-ve-toolbar-center>.r-ve-tool[data-tool=circle]').length"),0,'Shapes must share one toolbar slot');
    await click('.ve-shape-tool',true);assert.equal(await ev("document.querySelectorAll('.ve-shape-menu button').length"),4);
    await key('Escape');assert.equal(await ev("document.querySelector('.ve-shape-menu').hidden"),true);
    await click('.ve-shape-tool',true);await key('Down');await key('Return');
    assert.equal(await ev("document.querySelector('.r-ve-tool.active').dataset.tool"),'square','Shape menu must support keyboard selection');
    await click('.ve-shape-tool',true);await click('[data-shape-tool=polygon]');await click('.native-category-dialog .dlg-cancel');
    assert.equal(await ev("document.querySelector('.r-ve-tool.active').dataset.tool"),'square','Canceling polygon options changed tools');
    for(const [tool,tag]of [['rectangle','rect'],['square','rect'],['circle','ellipse'],['polygon','polygon']]){
        await click('.ve-shape-tool',true);await click('[data-shape-tool='+tool+']');
        if(tool==='polygon'){await ev("document.querySelector('.native-category-dialog input').value='7';true");await click('.native-category-dialog .dlg-ok');}
        assert.equal(await ev("document.querySelector('.r-ve-tool.active').dataset.tool"),tool);
        assert.equal(await ev("document.querySelector('.ve-shape-tool').dataset.tool"),tool);
        const before=await ev("document.querySelector('.r-ve-viewport>svg').children.length");
        await drag(await svgPoint(310,40),await svgPoint(400,95));
        assert.equal(await ev("document.querySelector('.r-ve-viewport>svg').lastElementChild.tagName"),tag);
        if(tool==='square')assert.equal(await ev("document.querySelector('.r-ve-viewport>svg').lastElementChild.getAttribute('width')"),await ev("document.querySelector('.r-ve-viewport>svg').lastElementChild.getAttribute('height')"));
        if(tool==='rectangle')assert.notEqual(await ev("document.querySelector('.r-ve-viewport>svg').lastElementChild.getAttribute('width')"),await ev("document.querySelector('.r-ve-viewport>svg').lastElementChild.getAttribute('height')"));
        if(tool==='polygon')assert.equal(await ev("document.querySelector('.r-ve-viewport>svg').lastElementChild.points.numberOfItems"),7);
        win.webContents.sendInputEvent({type:'keyDown',keyCode:'Control'});await key('z',['control']);win.webContents.sendInputEvent({type:'keyUp',keyCode:'Control'});await pause();
        assert.equal(await ev("document.querySelector('.r-ve-viewport>svg').children.length"),before,'Ctrl+Z did not undo shape');
        assert.equal(await ev("document.querySelector('.r-ve-tool.active').dataset.tool"),tool,'Ctrl shortcut unexpectedly selected another tool');
        await key('Control');assert.equal(await ev("document.querySelector('.r-ve-tool.active').dataset.tool"),'select');
    }
    for(const tool of ['move','line','text','anchor','bezier']){
        await click('[data-tool='+tool+']');await key('Control');
        assert.equal(await ev("document.querySelector('.r-ve-tool.active').dataset.tool"),'select','Ctrl tap failed from '+tool);
    }
    await click('[data-tool=line]');await ev("document.querySelector('.r-ve-stroke-width').focus();true");await key('Control');
    assert.equal(await ev("document.querySelector('.r-ve-tool.active').dataset.tool"),'line','Ctrl tap during input editing switched tools');
    await ev("document.activeElement.blur();true");
    win.webContents.sendInputEvent({type:'keyDown',keyCode:'Control'});await click('[data-tool=move]');win.webContents.sendInputEvent({type:'keyUp',keyCode:'Control'});await pause();
    assert.equal(await ev("document.querySelector('.r-ve-tool.active').dataset.tool"),'move','Ctrl mouse action unexpectedly switched tools');
    await key('Control');
    report.checks.push('one right-click shape toolset, keyboard menu and polygon cancel, all four shape geometries, no pencil; Ctrl taps select while Ctrl+Z, mouse actions and typing preserve tools');
    assert.equal(await ev("document.querySelector('.r-ve-tool[data-tool=select]').nextElementSibling.dataset.tool"),'anchor');
    assert.equal(await ev("!!document.querySelector('.ve-advanced-items [data-panel=nodes]')"),false);
    await click('.plain');await doubleClick(await svgPoint(95,60));await wait("document.querySelectorAll('.ve-node-overlays rect').length===4");
    assert.equal(await ev("document.querySelector('.r-ve-tool.active').dataset.tool"),'anchor');
    assert.equal(await ev("document.querySelector('.plain').tagName"),'rect');assert.equal(await ev("document.querySelector('.ve-dirty-indicator').textContent"),'Saved');
    await click('.ve-advanced-menu summary');await click('[data-command=rulers]');
    const rulerFrom=await ev("(()=>{const r=document.querySelector('.r-ve-canvas-wrap').getBoundingClientRect();return {x:r.x+100,y:r.y+10};})()");
    await drag(rulerFrom,{x:rulerFrom.x,y:rulerFrom.y+70});
    assert.ok(await ev("JSON.parse(decodeURIComponent(document.querySelector('.r-ve-viewport>svg').getAttribute('data-ve-guides'))).length===1"),'Anchor tool blocked ruler guides');
    await click('[data-tool=undo]');await click('.plain');await wait("document.querySelectorAll('.ve-node-overlays rect').length===4");
    await click('.ve-advanced-menu summary');await click('[data-command=rulers]');
    const basicCorner=await svgPoint(60,40),cancelledCorner=await svgPoint(80,50);await input('mouseDown',basicCorner);await input('mouseMove',cancelledCorner);await tick();
    assert.equal(await ev("document.querySelector('.plain').tagName"),'path');await key('Escape');await input('mouseUp',cancelledCorner);await wait("document.querySelectorAll('.ve-node-overlays rect').length===4");
    assert.equal(await ev("document.querySelector('.plain').tagName"),'rect');assert.equal(await ev("document.querySelector('.ve-dirty-indicator').textContent"),'Saved');
    await click('[data-tool=select]');await clickPoint(await svgPoint(20,200));await pen('add-anchor');await clickPoint(await svgPoint(95,40));await wait("document.querySelectorAll('.ve-node-overlays rect').length===5");
    assert.equal(await ev("document.querySelector('.plain').tagName"),'path');await click('[data-tool=undo]');assert.equal(await ev("document.querySelector('.plain').tagName"),'rect');await click('[data-tool=select]');
    report.checks.push('double-click selected shape activates anchors without dirtying; first-click insertion on unnamed basic shapes is undoable');
    await click('#curve');await click('[data-tool=anchor]');await wait("document.querySelectorAll('.ve-node-overlays rect').length===4");
    const a=await svgPoint(80,120),b=await svgPoint(100,130);await drag(a,b);
    assert.ok(await ev("Math.abs(document.querySelector('#curve').getPointAtLength(0).x-100)<1"),'Anchor drag failed');
    await click('[data-tool=undo]');await clickPoint(await svgPoint(150,155));await wait("document.querySelectorAll('.ve-node-overlays rect').length===4");
    const length=await ev("document.querySelector('#curve').getTotalLength()");
    await pen('add-anchor');await wait("document.querySelectorAll('.ve-node-overlays rect').length===4");
    const addPoint=await ev("(()=>{const e=document.querySelector('#curve'),p=e.getPointAtLength(e.getTotalLength()*.18),m=e.getScreenCTM();return {x:m.a*p.x+m.c*p.y+m.e,y:m.b*p.x+m.d*p.y+m.f};})()");
    await clickPoint(addPoint);await wait("document.querySelectorAll('.ve-node-overlays rect').length===5");
    assert.ok(Math.abs(await ev("document.querySelector('#curve').getTotalLength()")-length)<.05,'Adding an anchor changed the curve');
    await pen('delete-anchor');await wait("document.querySelectorAll('.ve-node-overlays rect').length===5");await clickPoint(addPoint);await wait("document.querySelectorAll('.ve-node-overlays rect').length===4");
    await click('[data-tool=undo]');await clickPoint(await svgPoint(150,155));await wait("document.querySelectorAll('.ve-node-overlays rect').length===5");
    report.checks.push('dedicated anchor tool; dragging, exact curve splitting, deletion and undo');
    await pen('bezier');await clickPoint(await svgPoint(70,250));await previewAt(130,260);await drag(await svgPoint(140,250),await svgPoint(160,220));await clickPoint(await svgPoint(210,270));await key('Return');
    assert.equal(await ev("!!document.querySelector('.ve-pen-preview')"),false,'Finished pen retained preview');
    const bezier=await ev("document.querySelector('.r-ve-viewport>svg>path:last-child').getAttribute('d')");assert.ok(bezier.includes('C')&&!bezier.includes('NaN'),'Pen did not create a cubic path');
    const count=await ev("document.querySelectorAll('.r-ve-viewport>svg>path').length");await click('[data-tool=undo]');assert.equal(await ev("document.querySelectorAll('.r-ve-viewport>svg>path').length"),count-1);await click('[data-tool=redo]');
    await pen('curvature');for(const [x,y]of [[70,310],[140,340]])await clickPoint(await svgPoint(x,y));await previewAt(210,300,true);await clickPoint(await svgPoint(210,300));await key('Return');
    const curvature=await ev("document.querySelector('.r-ve-viewport>svg>path:last-child').getAttribute('d')");assert.ok(curvature.includes('C')&&!curvature.includes('NaN'),'Curvature pen did not create a smooth path');
    await pen('bezier');for(const [x,y]of [[350,250],[410,250],[400,310],[350,250]])await clickPoint(await svgPoint(x,y));
    assert.ok(await ev("document.querySelector('.r-ve-viewport>svg>path:last-child').getAttribute('d').endsWith('Z')"),'Click-first closure failed');
    report.checks.push('right-click pen toolset; live ordinary/curvature previews do not mutate artwork; Bézier handles, closed paths and undo/redo');
    await tick();win.webContents.invalidate();await tick();fs.writeFileSync(path.join(output,'paths.png'),(await win.webContents.capturePage()).toPNG());
    await click('.ve-pen-tool',true);await tick();fs.writeFileSync(path.join(output,'pen-tools.png'),(await win.webContents.capturePage()).toPNG());await key('Escape');
    await wait(`require('electron').ipcRenderer.invoke('vector-recovery',{key:${JSON.stringify(keyA)},action:'read'}).then(Boolean)`);
    await click('.r-ve-export-unified-btn');await wait("!!document.querySelector('.r-ve-export-fmt')");
    await ev("document.querySelector('.r-ve-export-fmt').value='svg';document.querySelector('.r-ve-export-fmt').dispatchEvent(new Event('change'));true");
    cancelSave=true;await click('.r-ve-export-confirm');await pause(150);assert.ok(await copy(keyA,'read'),'Cancelled SVG export removed recovery');assert.equal(await ev("document.querySelector('.ve-dirty-indicator').textContent"),'Unsaved');
    cancelSave=false;await click('.r-ve-export-confirm');await wait("document.querySelector('.ve-dirty-indicator').textContent==='Saved'");assert.equal(await copy(keyA,'read'),null);assert.ok(fs.readFileSync(path.join(output,'paths.svg'),'utf8').includes(curvature));
    await closeClean();report.checks.push('cancelled SVG save retains recovery; successful SVG export writes file and clears recovery/dirty state');
    const recovered=fixture.replace('</svg>','<text id="recovered" x="30" y="40">Recovered A</text></svg>');await copy(keyA,'write',recovered);
    await open(fixture,'C:\\outputs\\run-b\\same.svg');assert.equal(await ev("!!document.querySelector('.ve-confirm')"),false,'Different output directory reused old backup');assert.ok(await copy(keyA,'read'));await closeClean();
    await open(fixture,'C:\\outputs\\run-a\\same.svg');await wait("!!document.querySelector('.ve-confirm')");await click('.ve-confirm [data-choice="0"]');await wait("!!document.querySelector('#recovered')");
    await ev(`window.__standaloneEditorUpdate(${JSON.stringify(fixture)},'C:\\\\outputs\\\\run-b\\\\same.svg');true`);await wait("!!document.querySelector('.ve-confirm')");await click('.ve-confirm [data-choice="1"]');await wait("!document.querySelector('#recovered')&&!document.querySelector('.ve-confirm')");assert.equal(await copy(keyA,'read'),null);await closeClean();
    report.checks.push('same filename in different runs has independent recovery; same-file restore and live-update discard remain functional');
    finish();
}catch(error){finish(error);}});
