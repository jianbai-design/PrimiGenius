'use strict';
// Offline acceptance using independent synthetic files, without loading any plugin.
const fs=require('fs'),path=require('path'),cp=require('child_process');
const root=path.resolve(__dirname,'..'),directory=fs.mkdtempSync(path.join(root,'.tmp-special-preview-'));
const harness=String.raw`
const {app,BrowserWindow,session,protocol,ipcMain}=require('electron');
const fs=require('fs'),path=require('path'),assert=require('assert/strict'),crypto=require('crypto');
const root=process.argv[2],uiRoot=process.argv[3]||root;
app.setPath('userData',path.join(__dirname,'profile'));
for(const flag of ['no-sandbox','in-process-gpu','ignore-gpu-blocklist','enable-webgl'])app.commandLine.appendSwitch(flag);
app.commandLine.appendSwitch('use-angle','swiftshader');
protocol.registerSchemesAsPrivileged([{scheme:'pg-preview',privileges:{standard:true,secure:true,supportFetchAPI:true,corsEnabled:true,stream:true}}]);
app.whenReady().then(async()=>{let win,service;try{
 const geotiff=await import(require('url').pathToFileURL(path.join(root,'node_modules/geotiff/dist-module/geotiff.js')).href);
 const tiff=path.join(__dirname,'image.tif'),rgb=new Uint8Array(256*256*3);for(let i=0;i<rgb.length;i+=3)rgb.set([200,70,30],i);
 fs.writeFileSync(tiff,Buffer.from(geotiff.writeArrayBuffer(rgb,{width:256,height:256,PhotometricInterpretation:2,BitsPerSample:[8,8,8]})));
 const gray=path.join(__dirname,'gray.tif');fs.writeFileSync(gray,Buffer.from(geotiff.writeArrayBuffer(new Uint8Array(256*256).fill(128),{width:256,height:256,PhotometricInterpretation:1,BitsPerSample:[8]})));
 const rgba=new Uint8Array(256*256*4);for(let i=0;i<rgba.length;i+=4)rgba.set([200,70,30,128],i);
 const alpha=path.join(__dirname,'alpha.tif');fs.writeFileSync(alpha,Buffer.from(geotiff.writeArrayBuffer(rgba,{width:256,height:256,PhotometricInterpretation:2,BitsPerSample:[8,8,8,8],ExtraSamples:[2]})));
 const pdb=path.join(__dirname,'molecule.pdb');fs.writeFileSync(pdb,'ATOM      1  N   GLY A   1       0.000   0.000   0.000  1.00 10.00           N  \nATOM      2  CA  GLY A   1       1.450   0.000   0.000  1.00 10.00           C  \nATOM      3  C   GLY A   1       2.100   1.300   0.000  1.00 10.00           C  \nEND\n'.replace(/\n/g,'\n'));
 const fasta=path.join(__dirname,'reference.fa'),fai=fasta+'.fai',bed=path.join(__dirname,'features.bed');
 fs.writeFileSync(fasta,'>chr1\n'+('ACGT'.repeat(12)+'AC\n').repeat(20));fs.writeFileSync(fai,'chr1\t1000\t6\t50\t51\n');fs.writeFileSync(bed,'chr1\t100\t200\tGeneA\n');
 const files=[tiff,gray,alpha,pdb,fasta,fai,bed],hash=file=>crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex'),before=files.map(hash);
 const fixture=path.join(__dirname,'fixture.html');fs.writeFileSync(fixture,'<!doctype html><meta charset="utf-8"><style>'+fs.readFileSync(path.join(uiRoot,'src/styles.css'),'utf8')+'</style><body style="margin:0"></body>');
 let network=0;session.defaultSession.webRequest.onBeforeRequest({urls:['http://*/*','https://*/*']},(_details,callback)=>{network++;callback({cancel:true});});
 win=new BrowserWindow({show:false,width:1000,height:800,webPreferences:{nodeIntegration:true,contextIsolation:false,sandbox:false,backgroundThrottling:false}});
 const errors=[];win.webContents.on('console-message',(_event,level,message)=>{if(level>=2)errors.push(message.slice(0,500));});
 service=require(path.join(uiRoot,'src/file-preview-service')).registerPreviewService(ipcMain,()=>win);service.installProtocol(protocol);
 ipcMain.handle('open-file-dialog',()=>({filePaths:[fasta,fai]}));
 const downloaded=path.join(__dirname,'downloaded.tif');let saveCalls=0;ipcMain.handle('save-file-dialog',()=>({canceled:saveCalls++>0,filePath:downloaded}));await win.loadFile(fixture);
 const run=expression=>win.webContents.executeJavaScript(expression);
 const wait=async(expression)=>{for(let i=0;i<500;i++){if(await run(expression))return;await new Promise(r=>setTimeout(r,20));}throw Error('Preview timed out: '+await run('document.body.innerText')+'\n'+errors.join('\n'));};
 await run('globalThis.host=document.createElement("div");host.style.cssText="width:100vw;height:100vh";document.body.append(host);globalThis.mount=require('+JSON.stringify(path.join(uiRoot,'src/file-preview'))+').mountFilePreview;true');
 // Byte-range serving is generic and revoking a capability makes it inaccessible.
 const ranges=await run('('+async function(file){const {ipcRenderer}=require('electron'),source=await ipcRenderer.invoke('file-preview-source-open',file);const response=await fetch(source.url,{headers:{Range:'bytes=0-3'}}),bytes=[...new Uint8Array(await response.arrayBuffer())];const invalid=await fetch(source.url,{headers:{Range:'bytes=999999-'}});await ipcRenderer.invoke('file-preview-source-close',source.id);return {status:response.status,range:response.headers.get('Content-Range'),bytes,invalid:invalid.status,closed:(await fetch(source.url)).status};}.toString()+')('+JSON.stringify(tiff)+')');
 assert.equal(ranges.status,206);assert.deepEqual(ranges.bytes,[...fs.readFileSync(tiff).subarray(0,4)]);assert.equal(ranges.invalid,416);assert.equal(ranges.closed,404);
 win.webContents.debugger.attach('1.3');const send=(method,params={})=>win.webContents.debugger.sendCommand(method,params);
 // Inspect the painted tile canvas, including its pixels, rather than just canvas existence.
 await send('Page.enable');
 await send('Page.addScriptToEvaluateOnNewDocument',{source:'const originalPutImageData=CanvasRenderingContext2D.prototype.putImageData;CanvasRenderingContext2D.prototype.putImageData=function(...args){const result=originalPutImageData.apply(this,args);document.documentElement.dataset.tilePixel=JSON.stringify([...this.getImageData(0,0,1,1).data]);return result;};'});
 async function inspect(expression){const {frameTree}=await send('Page.getFrameTree'),child=frameTree.childFrames.find(item=>item.frame.url==='about:srcdoc');assert(child);const {executionContextId}=await send('Page.createIsolatedWorld',{frameId:child.frame.id,worldName:'preview-audit'});const {result,exceptionDetails}=await send('Runtime.evaluate',{contextId:executionContextId,expression,returnByValue:true});if(exceptionDetails)throw Error(JSON.stringify(exceptionDetails));return result.value;}
 const reports=[];
 for(const [kind,file,expectedPixel] of [['tiff',tiff,[200,70,30,255]],['tiff',gray,[127,127,127,255]],['tiff',alpha,[200,70,30,128]],['structure',pdb],['genome',bed]]){
  errors.length=0;const started=Date.now();await run('globalThis.stopPreview=mount(host,'+JSON.stringify(file)+',{lang:"zh",showExternalOpen:false});true');
  await wait('!!host.querySelector("iframe[data-rendered=true]")');
  assert.equal(await run('host.querySelector("iframe").getAttribute("sandbox")'),'allow-scripts');
  assert.equal(await inspect('typeof require'),'undefined');
  if(kind==='tiff'){
   const color=JSON.parse(await inspect('document.documentElement.dataset.tilePixel'));
   assert(color.every((value,index)=>Math.abs(value-expectedPixel[index])<=1),'Rendered TIFF pixel must match the source: '+color);
   assert(await run('host.firstElementChild.hidden'),'TIFF has no extra status banner after loading');
   assert(await run('host.querySelector(".r-viewer-footer").getBoundingClientRect().top>=host.querySelector("iframe").getBoundingClientRect().bottom-1'),'Same bottom toolbar layout as other images');
   assert.equal(await run('host.querySelectorAll(".r-viewer-footer .r-viewer-action-btn").length'),4);
   assert.equal(await inspect('document.getElementById("tools")'),null);
   const home=await inspect('Math.round(Math.min(document.getElementById("viewer").clientWidth/256,document.getElementById("viewer").clientHeight/256)*100)');
   await run('host.querySelector("[data-action=zoomfit]").click();true');await wait('Math.abs(Number.parseFloat(host.querySelector(".r-viewer-zoom-label").textContent)-'+home+')<=1');
   await run('host.querySelector("[data-action=zoomin]").click();true');await wait('Number.parseFloat(host.querySelector(".r-viewer-zoom-label").textContent)>'+home);
   await run('host.querySelector("[data-action=zoomfit]").click();true');await wait('Math.abs(Number.parseFloat(host.querySelector(".r-viewer-zoom-label").textContent)-'+home+')<=1');
   await run('host.querySelector("[data-action=zoomout]").click();true');await wait('Number.parseFloat(host.querySelector(".r-viewer-zoom-label").textContent)<'+home);
   if(file===tiff){
    await run('host.querySelector("[data-action=download]").click();true');
    for(let i=0;i<100&&!fs.existsSync(downloaded);i++)await new Promise(resolve=>setTimeout(resolve,10));
    assert.equal(hash(downloaded),hash(tiff),'Download preserves the original TIFF bytes');
    await wait('!host.querySelector("[data-action=download]").disabled');
    await run('host.querySelector("[data-action=download]").click();true');await wait('!host.querySelector("[data-action=download]").disabled');assert.equal(hash(downloaded),hash(tiff));
   }
  }
  if(kind==='structure'){
   assert(await run('host.firstElementChild.hidden'));
   assert(await run('host.querySelector(".r-viewer-footer").getBoundingClientRect().top>=host.querySelector("iframe").getBoundingClientRect().bottom-1'));
   assert.equal(await inspect('document.getElementById("tools")'),null);
   assert.deepEqual(await run('[...host.querySelectorAll(".r-viewer-footer button")].map(button=>button.dataset.action)'),['cartoon','sticks','fit']);
   for(const action of ['cartoon','sticks','fit']){await run('host.querySelector("[data-action='+action+']").click();true');await new Promise(resolve=>setTimeout(resolve,30));assert(await run('host.firstElementChild.hidden'),'Structure controls remain functional without preview errors');}
  }
  if(kind==='genome'){
   assert(await run('host.querySelector(".r-viewer-footer").getBoundingClientRect().top>=host.querySelector("iframe").getBoundingClientRect().bottom-1'));
   await run('host.querySelector("[data-action=choose]").click();true');
   await wait('host.innerText.includes("本地基因组轨道预览")');
   const text=await inspect('document.getElementById("viewer").shadowRoot.textContent');assert(text.includes('features.bed'),text.slice(-1200)+'\n'+errors.join('\n'));
  }
  assert(await inspect('(document.getElementById("viewer").shadowRoot||document).querySelectorAll("canvas").length>0'),kind+' renders a canvas');
  reports.push({kind,readyMs:Date.now()-started});await run('stopPreview();true');assert.equal(await run('host.querySelectorAll("iframe").length'),0);
 }
 assert.equal(network,0,'All preview assets and data must be local');assert.deepEqual(files.map(hash),before);await service.closeAll();
 console.log(JSON.stringify({result:'PASS',opaqueSandbox:true,noPluginResources:true,rangeServingAndRevocation:true,offline:true,unchangedSources:true,previews:reports}));win.destroy();app.exit(0);
}catch(error){console.error(error.stack);await service?.closeAll();if(win)win.destroy();app.exit(1);}});setTimeout(()=>app.exit(2),60000);
`;
let result;
try{
 const script=path.join(directory,'check.cjs');fs.writeFileSync(script,harness);
 const env={...process.env};delete env.ELECTRON_RUN_AS_NODE;
 result=cp.spawnSync(path.join(root,'node_modules/electron/dist/electron.exe'),[script,root,process.env.PRIMIGENIUS_PREVIEW_TEST_ASAR||root],{env,encoding:'utf8',windowsHide:true,timeout:65000});
 process.stdout.write(result.stdout||'');process.stderr.write(result.stderr||'');
}finally{
 const target=path.resolve(directory);if(path.dirname(target)!==root||!path.basename(target).startsWith('.tmp-special-preview-'))throw Error('Unsafe cleanup target');
 fs.rmSync(target,{recursive:true,force:true,maxRetries:6,retryDelay:200});
}
process.exitCode=result?.status??1;
