'use strict';
// Run with Electron and an explicit disposable output/profile directory.
const {app,BrowserWindow,ipcMain} = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname,'..');
const asarArgument = process.argv.find(argument=>argument.startsWith('--asar='));
const uiRoot = asarArgument ? path.resolve(asarArgument.slice(7)) : root;
const output = path.resolve(process.argv[2]);
if (!path.basename(output).startsWith('.tmp-vector-') || path.dirname(output)!==root) throw Error('A disposable workspace profile is required');
fs.mkdirSync(output,{recursive:true});
app.setPath('userData',path.join(output,'profile'));
const graphics = require('../src/gpu-policy').configureGraphics(app,{PRIMIGENIUS_GPU_MODE:process.argv.includes('--software')?'software':'auto'});
ipcMain.on('get-backend-port-sync',event=>{event.returnValue=65534;});
ipcMain.handle('rendering-diagnostics',()=>graphics.snapshot());
let window;
const report = {mode:process.argv.includes('--software')?'software':'auto',packaged:!!asarArgument,errors:[]};
const startedAt = performance.now();
const timeout = setTimeout(()=>{report.errors.push('Timed out');finish(1);},45000);
function finish(code) {
    clearTimeout(timeout);
    try { fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(report,null,2)); }
    catch(error) { report.errors.push('Report file: '+error.message); }
    console.log('QA_RESULT '+JSON.stringify(report));
    if(window&&!window.isDestroyed())window.destroy();
    app.exit(code);
}
app.whenReady().then(async()=>{
    report.electronReadyMs = performance.now() - startedAt;
    console.log('QA Electron ready');
    try {
        window = new BrowserWindow({width:1500,height:1050,show:false,webPreferences:{nodeIntegration:true,contextIsolation:false,backgroundThrottling:false}});
        window.webContents.on('console-message',(_event,level,message,line,source)=>{if(level>=3)report.errors.push(message+' @ '+source+':'+line);});
        await window.loadFile(path.join(uiRoot,'src/editor.html'));
        report.pageLoadedMs = performance.now() - startedAt;
        window.webContents.debugger.attach('1.3');
        await window.webContents.debugger.sendCommand('HeapProfiler.collectGarbage');
        report.domBefore = await window.webContents.debugger.sendCommand('Memory.getDOMCounters');
        report.rendererBefore = await window.webContents.executeJavaScript('process.getProcessMemoryInfo()');
        console.log('QA editor page loaded');
        report.editor = await window.webContents.executeJavaScript(`(async()=>{
            const onError=event=>console.error(event.error?.stack||event.message);
            window.addEventListener('error',onError);
            const assert=(value,message)=>{if(!value)throw Error(message);};
            const result={};
            let activeListeners=0;
            const originalAdd=EventTarget.prototype.addEventListener;
            EventTarget.prototype.addEventListener=function(type,callback,options){
                if((this===document||this===window)&&options?.signal){
                    activeListeners++;
                    originalAdd.call(options.signal,'abort',()=>activeListeners--,{once:true});
                }
                return originalAdd.call(this,type,callback,options);
            };
            const labels=Array.from({length:2000},(_,i)=>'<text x="'+(20+(i%50)*23)+'" y="'+(150+Math.floor(i/50)*14)+'" font-size="8">'+i+'</text>').join('');
            const source='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 800"><text id="editable" x="100" y="80" font-size="26">原始文字</text>'+labels+'</svg>';
            const started=performance.now();
            currentLang='zh';
            _bootstrapStandaloneEditor('兼容性 测试.svg',source,'qa-vector');
            await document.fonts.ready;
            result.openMs=performance.now()-started;
            const overlay=document.querySelector('.r-vector-editor-overlay');
            assert(overlay,'Editor was not created');
            const viewport=overlay.querySelector('.r-ve-viewport');
            const wrap=overlay.querySelector('.r-ve-canvas-wrap');
            const svg=()=>viewport.querySelector('svg');
            const button=tool=>overlay.querySelector('[data-tool="'+tool+'"]');
            const point=(x,y)=>{const m=svg().getScreenCTM();return{x:m.a*x+m.c*y+m.e,y:m.b*x+m.d*y+m.f};};
            const mouse=(type,p,target=wrap)=>target.dispatchEvent(new MouseEvent(type,{bubbles:true,cancelable:true,button:0,buttons:type==='mouseup'?0:1,clientX:p.x,clientY:p.y}));
            const {SvgTextIndex}=require(${JSON.stringify(path.join(uiRoot,'src/vector-runtime.js'))});
            let measured=0;
            const index=new SvgTextIndex(svg(),element=>{measured++;const b=element.getBBox();const m=svg().getScreenCTM().inverse().multiply(element.getScreenCTM());return{x:m.a*b.x+m.c*b.y+m.e,y:m.b*b.x+m.d*b.y+m.f,w:b.width,h:b.height};});
            let p=point(100,75);
            const first=index.query(p.x,p.y,10);assert(first.some(element=>element.id==='editable'),'Index lost text hit');
            const measuredOnce=measured;
            const queryTimes=[];
            for(let batch=0;batch<30;batch++){
                const began=performance.now();
                for(let i=0;i<100;i++)index.query(p.x,p.y,10);
                queryTimes.push((performance.now()-began)/100);
            }
            queryTimes.sort((a,b)=>a-b);
            result.indexQuery={batches:30,queriesPerBatch:100,p50Ms:queryTimes[15],p95Ms:queryTimes[28]};
            for(let i=0;i<100;i++)index.query(p.x,p.y,10);
            assert(measured===measuredOnce,'Unchanged text scene rebuilt on every pointer query');
            result.indexedTextCount=measuredOnce;result.textCandidates=first.length;
            svg().querySelector('#editable').setAttribute('x','500');
            p=point(500,75);assert(index.query(p.x,p.y,10).some(element=>element.id==='editable'),'Synchronous mutation did not invalidate index');
            svg().querySelector('#editable').setAttribute('x','100');index.dispose();
            // A new white shape must survive redo; old restore code re-imported
            // SVG and removed all white rectangles, including edited user shapes.
            const fill=overlay.querySelector('.r-ve-fill');fill.value='#ffffff';fill.dispatchEvent(new Event('input',{bubbles:true}));
            button('rectangle').click();
            mouse('mousedown',point(800,50));
            for(let i=1;i<=200;i++)mouse('mousemove',point(800+i/2,50+i/4));
            mouse('mouseup',point(900,100));
            let drawn=svg().querySelectorAll('rect:not([data-role="background"])');
            assert(drawn.length===1,'Drawing did not create one rectangle');
            assert(Math.abs(+drawn[0].getAttribute('width')-100)<2/Math.abs(svg().getScreenCTM().a),'Final pointer position lost before commit: '+drawn[0].outerHTML);
            const transform=viewport.style.transform;
            button('undo').click();assert(svg().querySelectorAll('rect:not([data-role="background"])').length===0,'Undo failed');
            button('redo').click();drawn=svg().querySelectorAll('rect:not([data-role="background"])');
            assert(drawn.length===1&&drawn[0].getAttribute('fill')==='#ffffff','Redo lost the white user shape');
            assert(viewport.style.transform===transform,'Undo/redo changed viewport');
            assert(svg().querySelectorAll('[data-role="background"]').length===1,'Undo duplicated background');
            result.pointerMoves=200;result.undoRedo=true;
            button('pen').click();mouse('mousedown',point(600,90));
            for(let i=1;i<=500;i++)mouse('mousemove',point(600+i/10,90+Math.sin(i/10)*10));
            mouse('mouseup',point(650,90));
            assert(svg().querySelector('polyline').points.numberOfItems===501,'Freehand samples were dropped');
            result.freehandSamples=501;
            // Use the actual right-click text editor, including partial selection.
            button('select').click();
            let text=svg().querySelector('#editable');p=point(120,72);
            mouse('mousedown',p,text);mouse('mouseup',p,text);
            mouse('contextmenu',p,text);
            const edit=Array.from(overlay.querySelectorAll('.r-ve-ctx-item')).find(row=>row.textContent.includes('文本编辑'));
            assert(edit,'Text context action missing');edit.click();
            const input=overlay.querySelector('.r-ve-text-panel-content');assert(input,'Text panel missing');
            input.focus();input.setSelectionRange(0,2);input.dispatchEvent(new Event('select'));
            const originX=svg().querySelector('#editable').getAttribute('x');
            overlay.querySelector('.r-ve-text-panel [data-format="bold"]').click();
            assert(input.selectionStart===0&&input.selectionEnd===2,'Partial text selection was lost after formatting');
            assert(svg().querySelector('#editable').getAttribute('x')===originX,'Partial formatting moved text origin');
            assert(overlay.querySelector('.ve-text-range-highlights')?.childNodes.length,'Canvas text range highlight missing');
            result.partialFormatting=true;
            input.value='更新后的中文 🧬';input.dispatchEvent(new Event('input',{bubbles:true}));
            overlay.querySelector('.r-ve-text-panel-close').click();
            assert(svg().querySelector('#editable').textContent==='更新后的中文 🧬','Text update failed');
            button('undo').click();assert(svg().querySelector('#editable').textContent==='原始文字','Text undo failed');
            button('redo').click();assert(svg().querySelector('#editable').textContent==='更新后的中文 🧬','Text redo failed');
            result.textEditing=true;
            let exported;
            const create=URL.createObjectURL.bind(URL);
            const anchorClick=HTMLAnchorElement.prototype.click;
            URL.createObjectURL=blob=>{if(blob.type.startsWith('image/svg+xml'))exported=blob;return create(blob);};
            HTMLAnchorElement.prototype.click=function(){};
            overlay.querySelector('.r-ve-export-unified-btn').click();
            const format=overlay.querySelector('.r-ve-export-fmt');format.value='svg';format.dispatchEvent(new Event('change'));
            overlay.querySelector('.r-ve-export-confirm').click();
            assert(exported,'Vector export did not produce an SVG');
            const xml=await exported.text();
            assert(xml.includes('更新后的中文')&&xml.includes('<text')&&!xml.includes('data:image/png'),'Vector export rasterized or lost text');
            URL.createObjectURL=create;
            HTMLAnchorElement.prototype.click=anchorClick;
            result.vectorExport=true;
            window.__editorStandalone=false;
            overlay.querySelector('.r-ve-close-btn').click();
            assert(activeListeners===0,'Editor retained global event handlers');
            for(let i=0;i<10;i++){
                _bootstrapStandaloneEditor('repeat.svg','<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><text x="10" y="30">x</text></svg>','repeat-'+i);
                document.querySelector('.r-ve-close-btn').click();
            }
            assert(activeListeners===0&&!document.querySelector('.r-vector-editor-overlay'),'Repeated close retained editor handlers or DOM');
            const opens=[];
            for(let i=0;i<30;i++){
                const began=performance.now();
                _bootstrapStandaloneEditor('large.svg',source,'large-repeat-'+i);
                document.querySelector('.r-ve-viewport svg').getBBox();
                opens.push(performance.now()-began);
                document.querySelector('.r-ve-close-btn').click();
                await new Promise(resolve=>requestAnimationFrame(resolve));
            }
            opens.sort((a,b)=>a-b);
            result.largeSceneOpen={samples:30,textNodes:2001,p50Ms:opens[15],p95Ms:opens[28]};
            assert(activeListeners===0,'Large scene cycles retained handlers');
            EventTarget.prototype.addEventListener=originalAdd;
            window.removeEventListener('error',onError);
            result.closedEditorListeners=activeListeners;result.repeatedOpenClose=10;
            return result;
        })()`);
        report.rendererAfter = await window.webContents.executeJavaScript('process.getProcessMemoryInfo()');
        await new Promise(resolve=>setTimeout(resolve,1000));
        await window.webContents.debugger.sendCommand('HeapProfiler.collectGarbage');
        await window.webContents.debugger.sendCommand('HeapProfiler.collectGarbage');
        report.domAfterGC = await window.webContents.debugger.sendCommand('Memory.getDOMCounters');
        report.rendererAfterGC = await window.webContents.executeJavaScript('process.getProcessMemoryInfo()');
        if(report.domAfterGC.nodes > report.domBefore.nodes + 200) throw Error('Closed scenes retained DOM nodes after collection');
        window.webContents.debugger.detach();
        report.graphics=graphics.snapshot();
        report.result='PASS';
        finish(0);
    }catch(error){report.result='FAIL';report.errors.push(error.stack||String(error));finish(1);}
});
