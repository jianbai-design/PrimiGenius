'use strict';
// Real pointer/keyboard input validates menu visibility, hit testing and ordering.
const {app,BrowserWindow,ipcMain}=require('electron');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),output=path.resolve(process.argv[2]||'');
if(path.dirname(output)!==root||!path.basename(output).startsWith('.tmp-vector-'))throw Error('A disposable workspace directory is required');
fs.mkdirSync(output,{recursive:true});app.setPath('userData',path.join(output,'profile'));
for(const flag of ['no-sandbox','disable-gpu','in-process-gpu'])app.commandLine.appendSwitch(flag);
ipcMain.on('get-backend-port-sync',e=>e.returnValue=65534);
require('../src/vector-export-main').register({app,ipcMain,BrowserWindow,dialog:{showSaveDialog:async()=>({canceled:true})}});
let win;const report={checks:[],errors:[]};
const timeout=setTimeout(()=>finish(Error('Menu acceptance timed out')),120000);
function finish(error){clearTimeout(timeout);if(error)report.errors.push(error.stack||String(error));report.result=report.errors.length?'FAIL':'PASS';fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));if(win&&!win.isDestroyed())win.destroy();app.exit(report.errors.length?1:0);}
const ev=async code=>{try{return await win.webContents.executeJavaScript(code);}catch(error){throw Error(error.message+'\nScript: '+code.slice(0,500));}},pause=()=>new Promise(r=>setTimeout(r,60));
async function tick(){await ev('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))');}
async function wait(code){for(let i=0;i<100;i++){if(await ev(code))return;await pause();}throw Error('Timed out: '+code);}
async function point(selector){return ev(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});if(!e)throw Error('Missing '+${JSON.stringify(selector)});const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);}
async function click(selector,{right=false,shift=false}={}){
    await ev(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({block:'nearest',inline:'nearest'});true`);
    const p=await point(selector);
    assert.ok(await ev(`document.querySelector(${JSON.stringify(selector)}).contains(document.elementFromPoint(${p.x},${p.y}))`),'Click target is obstructed: '+selector);
    for(const type of ['mouseMove','mouseDown','mouseUp'])win.webContents.sendInputEvent({type,x:Math.round(p.x),y:Math.round(p.y),button:right?'right':'left',clickCount:1,modifiers:shift?['shift']:[]});
    await pause();
    if(right)await tick();
}
async function key(keyCode){win.webContents.sendInputEvent({type:'keyDown',keyCode});if(keyCode==='Return')win.webContents.sendInputEvent({type:'char',keyCode:'\r'});win.webContents.sendInputEvent({type:'keyUp',keyCode});await pause();}
async function menu(name){await click('.ve-advanced-menu summary');await click('.ve-advanced-items [data-panel="'+name+'"]');await wait(`document.querySelector('.ve-workbench-panel').dataset.panel===${JSON.stringify(name)}&&!document.querySelector('.ve-workbench-panel').hidden&&!!document.querySelector('.ve-workbench-panel header button')`);assert.equal(await ev("document.querySelector('.ve-advanced-menu').open"),false);}
async function closePanel(){await wait("!!document.querySelector('.ve-workbench-panel header button')");await click('.ve-workbench-panel header button');await wait("document.querySelector('.ve-workbench-panel').hidden");}
async function checkToolbarLanguage(zh){
    const info=await ev(`(()=>{const items=[...document.querySelectorAll('.r-ve-toolbar button[title],.r-ve-props-bar [title]')];const status=document.querySelector('.ve-dirty-indicator');return {titles:items.map(n=>n.title),select:document.querySelector('[data-tool=select]').title,undo:document.querySelector('[data-tool=undo]').title,statusHidden:getComputedStyle(status).display==='none'&&status.getClientRects().length===0};})()`);
    assert.ok(info.titles.length>=25,'Toolbar tooltip coverage is incomplete');
    assert.ok(info.titles.every(title=>/[\u3400-\u9fff]/.test(title)===zh),'Wrong tooltip language: '+JSON.stringify(info.titles));
    assert.equal(info.select,zh?'选择工具（单按 Ctrl）':'Select (tap Ctrl)');assert.equal(info.undo,zh?'撤销':'Undo');assert.ok(info.statusHidden,'Saved/unsaved text is still visible');
}
async function ctx(action){await click('#a',{right:true});await click('.r-ve-ctx-item[data-action="'+action+'"]');}
const order=()=>ev("[...document.querySelector('#layer').children].filter(n=>n.tagName==='rect').map(n=>n.id).join(',')");
app.whenReady().then(async()=>{try{
    win=new BrowserWindow({show:false,width:1320,height:960,webPreferences:{nodeIntegration:true,contextIsolation:false,backgroundThrottling:false}});
    win.webContents.on('console-message',(_event,level,message)=>{if(level>=3)console.log('RENDERER '+message);});
    await win.loadFile(path.join(root,'src/editor.html'));
    const svg='<svg xmlns="http://www.w3.org/2000/svg" width="600" height="400"><g id="layer" transform="translate(10 10)"><rect id="a" x="40" y="80" width="60" height="50" fill="red"/><rect id="b" x="150" y="80" width="60" height="50" fill="green"/><rect id="c" x="260" y="80" width="60" height="50" fill="blue"/><rect id="d" x="370" y="80" width="60" height="50" fill="orange"/></g></svg>';
    await ev(`(async()=>{currentLang='zh';await require('electron').ipcRenderer.invoke('vector-recovery',{key:'menus',action:'clear'});_bootstrapStandaloneEditor('menus.svg',${JSON.stringify(svg)},'menus');})()`);
    await new Promise(r=>setTimeout(r,250));await tick();
    await checkToolbarLanguage(true);
    assert.equal(await ev("document.querySelectorAll('.ve-workbench-bar,[data-command=snap],[data-command=save],button[data-panel=objects]').length"),0);
    assert.ok(await ev("document.querySelector('.r-ve-toolbar-right .ve-advanced-menu summary').textContent.includes('高级功能')"));
    assert.equal(await ev("document.querySelector('.ve-advanced-items [data-panel=nodes]')"),null);
    await click('#a');await menu('transform');await closePanel();assert.equal(await ev("document.querySelector('[data-panel=layout]')"),null);await menu('history');await closePanel();
    report.checks.push('removed redundant row, node menu and buttons; layout feature removed; advanced transform and history open via real clicks');
    await click('.ve-advanced-menu summary');await click('.ve-advanced-items [data-command=rulers]');assert.ok(await ev("!!document.querySelector('.ve-rulers')"));
    await click('.ve-advanced-menu summary');assert.equal(await ev("document.querySelector('[data-command=rulers]').getAttribute('aria-checked')"),'true');await key('Escape');assert.equal(await ev("document.querySelector('.ve-advanced-menu').open"),false);assert.equal(await ev("document.querySelector('#a').getAttribute('data-ve-selected')"),'true');
    await click('.ve-advanced-menu summary');await click('.ve-advanced-items [data-command=rulers]');assert.equal(await ev("!!document.querySelector('.ve-rulers')"),false);
    await click('.ve-advanced-menu summary');await click('.r-ve-props-bar label');assert.equal(await ev("document.querySelector('.ve-advanced-menu').open"),false);
    report.checks.push('rulers toggle in menu; outside click and Escape dismiss without losing selection');
    await click('#a');await click('#b',{shift:true});await ctx('up');assert.equal(await order(),'c,a,b,d');
    await ctx('down');assert.equal(await order(),'a,b,c,d');await ctx('top');assert.equal(await order(),'c,d,a,b');await ctx('bottom');assert.equal(await order(),'a,b,c,d');
    assert.equal(await ev("document.querySelector('#a').parentNode.id"),'layer');assert.equal(await ev("document.querySelector('#layer').getAttribute('transform')"),'translate(10 10)');
    assert.equal(await ev("document.querySelector('.r-ve-viewport > svg').firstElementChild.getAttribute('data-role')"),'background');
    report.checks.push('multi-object forward, backward, front and back preserve order, transformed parent and page background');
    await ctx('lock');assert.equal(await ev("document.querySelector('#a').getAttribute('data-locked')"),'true');assert.equal(await ev("document.querySelector('#b').getAttribute('data-locked')"),'true');assert.equal(await ev("document.querySelector('#a').style.opacity"),'');
    const locked=await order();await ctx('top');assert.equal(await order(),locked);await ctx('lock');
    await ctx('hide');assert.equal(await ev("document.querySelector('#a').getAttribute('visibility')"),'hidden');assert.equal(await ev("document.querySelector('#b').getAttribute('visibility')"),'hidden');
    // A hidden object has no paint target. Open the object list from blank canvas.
    await click('.r-ve-canvas-wrap',{right:true});await click('.r-ve-ctx-item[data-action=objects]');await wait("!!document.querySelector('.ve-object-row')");
    await ev("[...document.querySelectorAll('.ve-object-row')].find(r=>r._veObject.id==='a').querySelector('button').click();true");await wait("document.querySelector('#a').getAttribute('visibility')!=='hidden'");await closePanel();
    report.checks.push('context lock/hide affect multi-selection; object list remains reachable to reveal hidden objects');
    await checkToolbarLanguage(true);report.checks.push('Chinese tooltips cover top tools and properties; unsaved text stays hidden after editing');
    await click('.ve-advanced-menu summary');await tick();fs.writeFileSync(path.join(output,'chinese.png'),(await win.webContents.capturePage()).toPNG());await key('Escape');
    // Reopen in English, retaining the same compact menu structure.
    await ev("window.__editorStandalone=false;document.querySelector('.r-ve-close-btn').click();true");await wait("!!document.querySelector('.ve-confirm')");await click('.ve-confirm [data-choice="1"]');await wait("!document.querySelector('.r-vector-editor-overlay')");
    await ev(`currentLang='en';window.__editorStandalone=true;_bootstrapStandaloneEditor('english.svg',${JSON.stringify(svg)},'menus-en');true`);await new Promise(r=>setTimeout(r,250));await tick();
    await checkToolbarLanguage(false);report.checks.push('English tooltips and hidden saved status');
    await click('.ve-advanced-menu summary');assert.equal(await ev("document.querySelector('.ve-advanced-items [data-panel=transform]').textContent"),'Transform');
    await key('Down');assert.equal(await ev("document.activeElement.dataset.panel"),'transform');await key('Return');await wait("document.querySelector('.ve-workbench-panel').dataset.panel==='transform'");await closePanel();
    await click('.ve-advanced-menu summary');await tick();fs.writeFileSync(path.join(output,'english.png'),(await win.webContents.capturePage()).toPNG());
    report.checks.push('Chinese and English layout; native arrow/Enter menu navigation');finish();
}catch(error){finish(error);}});
