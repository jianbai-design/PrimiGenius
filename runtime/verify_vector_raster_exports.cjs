'use strict';
const {app,BrowserWindow,ipcMain}=require('electron');
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),output=path.resolve(process.argv[2]||'');
if(path.dirname(output)!==root||!path.basename(output).startsWith('.tmp-vector-'))throw Error('A disposable workspace directory is required');
fs.mkdirSync(output,{recursive:true});app.setPath('userData',path.join(output,'profile'));
for(const flag of ['no-sandbox','disable-gpu','in-process-gpu'])app.commandLine.appendSwitch(flag);
let server,win,pngBytes,reads=0,saved=0,unavailable=false;
ipcMain.on('get-backend-port-sync',e=>e.returnValue=server.address().port);
require('../src/vector-export-main').register({app,ipcMain,BrowserWindow,dialog:{showSaveDialog:async()=>({canceled:false,filePath:path.join(output,'export-'+(++saved)+'.svg')})}});
const report={checks:[],errors:[]};const timeout=setTimeout(()=>finish(Error('Raster export acceptance timed out')),150000);
function finish(error){clearTimeout(timeout);if(error)report.errors.push(error.stack||String(error));report.result=report.errors.length?'FAIL':'PASS';fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));if(win&&!win.isDestroyed())win.destroy();server?.closeAllConnections();server?.close();app.exit(report.errors.length?1:0);}
const ev=code=>win.webContents.executeJavaScript(code),pause=ms=>new Promise(r=>setTimeout(r,ms||40));
async function wait(code){for(let i=0;i<200;i++){if(await ev(code))return;await pause();}throw Error('Timed out: '+code);}
async function click(selector){const p=await ev(`(()=>{const el=document.querySelector(${JSON.stringify(selector)}),r=el.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;return{x,y,hit:el.contains(document.elementFromPoint(x,y))};})()`);assert.ok(p.hit,'Obstructed: '+selector);for(const type of ['mouseMove','mouseDown','mouseUp'])win.webContents.sendInputEvent({type,x:Math.round(p.x),y:Math.round(p.y),button:'left',clickCount:1});await pause();}
async function open(name,svg=''){await ev(`currentLang='zh';window.__editorStandalone=false;window.qaRasterInstance={config:{id:'raster',name:'raster',parameters:[],packages:[]},__standaloneMode:true,_liveEditorSessions:new Map(),outDir:''};renderRPanelUI(qaRasterInstance,document.createElement('div'));qaRasterInstance.__openVectorEditor({name:${JSON.stringify(name)},path:'fixture.png',ext:'.'+${JSON.stringify(name)}.split('.').pop()},{sessionKey:'raster-'+Date.now(),initialSvgText:${JSON.stringify(svg)}});true`);await wait("!!document.querySelector('.r-ve-viewport>svg:first-child image')");await pause(100);}
async function close(){await ev("document.querySelector('.r-ve-close-btn').click();true");await pause();if(await ev("!!document.querySelector('.ve-confirm')"))await click('.ve-confirm [data-choice="1"]');await wait("!document.querySelector('.r-vector-editor-overlay')");}
async function preview(format){await click('.r-ve-export-unified-btn');await ev(`(()=>{const f=document.querySelector('.r-ve-export-fmt');f.value=${JSON.stringify(format)};f.dispatchEvent(new Event('change'));const d=document.querySelector('.r-ve-export-dpi');d.value=96;d.dispatchEvent(new Event('input'));})()`);await wait("!document.querySelector('.r-ve-export-confirm').disabled");await wait("document.querySelector('.r-ve-export-dims').textContent!=='—'&&document.querySelector('.r-ve-export-loading').style.display==='none'&&document.querySelector('.r-ve-export-canvas').width>10");}
async function previewPixels(){return ev("(()=>{const c=document.querySelector('.r-ve-export-canvas'),ctx=c.getContext('2d');return [.25,.75].map(x=>Array.from(ctx.getImageData(Math.floor(c.width*x),Math.floor(c.height/2),1,1).data));})()");}
function colors(pixels){assert.deepEqual(pixels,[[240,20,40,255],[20,180,220,255]],'Missing source pixels or broken-image placeholder: '+JSON.stringify(pixels));}
app.whenReady().then(async()=>{try{
    server=http.createServer((req,res)=>{res.setHeader('Access-Control-Allow-Origin','*');res.setHeader('Cache-Control','no-store');if(unavailable||req.url.includes('missing.png')){res.writeHead(404);res.end();return;}reads++;res.setHeader('Content-Type','image/png');res.end(pngBytes);});
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    win=new BrowserWindow({show:false,width:1320,height:960,webPreferences:{nodeIntegration:true,contextIsolation:false,backgroundThrottling:false}});await win.loadFile(path.join(root,'src/editor.html'));
    pngBytes=Buffer.from(await ev("(()=>{const c=document.createElement('canvas');c.width=96;c.height=64;const ctx=c.getContext('2d');ctx.fillStyle='rgb(240,20,40)';ctx.fillRect(0,0,48,64);ctx.fillStyle='rgb(20,180,220)';ctx.fillRect(48,0,48,64);return c.toDataURL('image/png').split(',')[1];})()"),'base64');
    await ev("window.qaRasterDownloads=[];window.qaRasterURL=URL.createObjectURL;URL.createObjectURL=function(blob){if(['image/png','image/tiff','application/pdf'].includes(blob.type))qaRasterDownloads.push(blob);return qaRasterURL.call(URL,blob)};HTMLAnchorElement.prototype.click=function(){};true");
    const url='http://127.0.0.1:'+server.address().port+'/r/serve-file?path=fixture.png';
    const linked='<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="96" height="64" viewBox="0 0 96 64"><image width="96" height="64" xlink:href="'+url+'"/></svg>';
    for(const kind of ['png','linked-svg']){
        await open(kind==='png'?'fixture.png':'linked.svg',kind==='png'?'':linked);
        if(kind==='png'){
            const uri=await ev("document.querySelector('.r-ve-viewport>svg:first-child image').getAttribute('href')");assert.ok(uri.startsWith('data:image/png;base64,'));assert.deepEqual(Buffer.from(uri.split(',')[1],'base64'),pngBytes,'Import changed original raster bytes');
        }
        for(const format of ['tif','png','svg','pdf']){
            await preview(format);colors(await previewPixels());
            const beforeReads=reads;
            await ev("(()=>{const d=document.querySelector('.r-ve-export-dpi');for(const n of [72,150,300,96]){d.value=n;d.dispatchEvent(new Event('input'));}})()");await pause(200);assert.equal(reads,beforeReads,'DPI changes fetched the image repeatedly');
            const count=await ev('qaRasterDownloads.length');await click('.r-ve-export-confirm');
            if(format==='svg'){
                await wait("!document.querySelector('.r-ve-export-confirm')");const xml=fs.readFileSync(path.join(output,'export-'+saved+'.svg'),'utf8');assert.match(xml,/href="data:image\/png;base64,/);assert.ok(!xml.includes(url));
                report.savedSvg=xml;
            }else{
                await wait('qaRasterDownloads.length>'+count);await wait("!document.querySelector('.r-ve-export-confirm')");
                if(format==='png'){
                    colors(await ev("(async()=>{const blob=qaRasterDownloads.at(-1),u=qaRasterURL.call(URL,blob),im=new Image();await new Promise((r,j)=>{im.onload=r;im.onerror=j;im.src=u});const c=document.createElement('canvas');c.width=im.naturalWidth;c.height=im.naturalHeight;const ctx=c.getContext('2d');ctx.drawImage(im,0,0);URL.revokeObjectURL(u);return [.25,.75].map(x=>Array.from(ctx.getImageData(Math.floor(c.width*x),Math.floor(c.height/2),1,1).data));})()"));
                }else{
                    const bytes=Buffer.from(await ev('qaRasterDownloads.at(-1).arrayBuffer().then(b=>Array.from(new Uint8Array(b)))'));
                    if(format==='tif'){
                        const tags=new Map();for(let i=0;i<bytes.readUInt16LE(8);i++){const at=10+i*12;tags.set(bytes.readUInt16LE(at),bytes.readUInt32LE(at+8));}
                        const width=tags.get(256),height=tags.get(257),offset=tags.get(273);colors([.25,.75].map(x=>Array.from(bytes.subarray(offset+(Math.floor(height/2)*width+Math.floor(width*x))*4,offset+(Math.floor(height/2)*width+Math.floor(width*x))*4+4))));
                    }else{assert.match(bytes.subarray(0,5).toString(),/%PDF-/);const content=bytes.toString('latin1');assert.match(content,/\/Width 96\b/);assert.match(content,/\/Height 64\b/);}
                }
            }
        }
        if(kind==='linked-svg')assert.equal(await ev("document.querySelector('.r-ve-viewport>svg:first-child image').getAttributeNS('http://www.w3.org/1999/xlink','href')"),url,'Export mutated the original linked SVG');
        await close();report.checks.push(kind+': visible original pixels in preview/TIFF/PNG, self-contained SVG with original bytes, bitmap embedded in PDF, no refetch per DPI change');
    }
    const actualArg=process.argv.find(v=>v.startsWith('--actual-png='));
    if(actualArg){
        const actualPath=path.resolve(actualArg.slice('--actual-png='.length));pngBytes=fs.readFileSync(actualPath);
        await open('actual.png');await preview('png');
        const difference=await ev("(async()=>{const actual=document.querySelector('.r-ve-export-canvas'),w=actual.width,h=actual.height,ref=document.createElement('canvas');ref.width=w;ref.height=h;const im=new Image();await new Promise((r,j)=>{im.onload=r;im.onerror=j;im.src=document.querySelector('.r-ve-viewport>svg:first-child image').getAttribute('href')});ref.getContext('2d').drawImage(im,0,0,w,h);const a=actual.getContext('2d').getImageData(0,0,w,h).data,b=ref.getContext('2d').getImageData(0,0,w,h).data;let error=0,n=0;for(let i=0;i<a.length;i+=4*13)for(let c=0;c<3;c++){error+=Math.abs(a[i+c]-b[i+c]);n++;}ref.width=ref.height=0;return error/n;})()");
        assert.ok(difference<8,'Actual PNG differs from its source or shows a placeholder: '+difference);report.actualPngMeanPixelDifference=difference;
        await click('.r-ve-export-dlg-close');await close();report.checks.push('actual 3300x2100 PNG preview compared with source pixels');
    }
    const savedSvg=report.savedSvg;delete report.savedSvg;
    unavailable=true;await open('offline.svg',savedSvg);await preview('png');colors(await previewPixels());await click('.r-ve-export-dlg-close');await close();report.checks.push('exported SVG reopens and previews with original HTTP resource unavailable');
    await open('missing.svg',linked.replace(url,'http://127.0.0.1:'+server.address().port+'/missing.png'));await click('.r-ve-export-unified-btn');await wait("document.querySelector('.r-ve-export-size-info').textContent.includes('已停止导出')");assert.equal(await ev("document.querySelector('.r-ve-export-confirm').disabled"),true);await click('.r-ve-export-dlg-close');await close();
    report.checks.push('missing linked image shows an error and blocks export');finish();
}catch(error){finish(error);}});
