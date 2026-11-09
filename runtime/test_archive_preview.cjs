'use strict';
// Disposable read-only archive acceptance, including the real Electron entry point.
const fs=require('fs'),path=require('path'),os=require('os'),zlib=require('zlib'),assert=require('assert/strict'),{Worker}=require('worker_threads');
const root=path.resolve(__dirname,'..'),directory=fs.mkdtempSync(path.join(root,'.tmp-archive-preview-')),active=new Set();
function open(file){
    const worker=new Worker(path.join(root,'src/archive-preview-worker.js'),{workerData:{path:file,kind:'archive'},resourceLimits:{maxOldGenerationSizeMb:256}});active.add(worker);
    return {read(request){return new Promise((resolve,reject)=>{
        const timer=setTimeout(()=>{cleanup();reject(Error('Archive test timed out'));},15000);
        const cleanup=()=>{clearTimeout(timer);worker.removeListener('message',message);worker.removeListener('error',error);};
        const message=result=>{cleanup();resolve(result);},error=err=>{cleanup();reject(err);};worker.once('message',message);worker.once('error',error);worker.postMessage({page:request});
    });},async close(){await worker.terminate();active.delete(worker);}};
}
async function tarFixture(file,entries,gzip=false){
    const pack=require('tar-stream').pack(),chunks=[];pack.on('data',chunk=>chunks.push(chunk));
    const done=new Promise((resolve,reject)=>{pack.once('end',resolve);pack.once('error',reject);});
    for(const [name,content,type='file'] of entries)pack.entry({name,type,linkname:type==='symlink'?'../outside.txt':undefined},type==='file'?content:undefined);
    pack.finalize();await done;const bytes=Buffer.concat(chunks);fs.writeFileSync(file,gzip?zlib.gzipSync(bytes):bytes);
}
const harness=String.raw`
const {app,BrowserWindow,ipcMain,session}=require('electron'),fs=require('fs'),path=require('path'),assert=require('assert/strict');
app.setPath('userData',path.join(__dirname,'electron-profile'));for(const flag of ['no-sandbox','in-process-gpu','disable-gpu'])app.commandLine.appendSwitch(flag);
app.whenReady().then(async()=>{let win,service;try{
 const root=process.argv[2],uiRoot=process.env.PRIMIGENIUS_PREVIEW_TEST_ASAR||root,zip=process.argv[3],gz=process.argv[4],trees=JSON.parse(process.argv[5]);
 const fixture=path.join(__dirname,'fixture.html');fs.writeFileSync(fixture,'<!doctype html><meta charset="utf-8"><style>'+fs.readFileSync(path.join(uiRoot,'src/styles.css'),'utf8')+'</style><body></body>');
 let network=0;session.defaultSession.webRequest.onBeforeRequest({urls:['http://*/*','https://*/*']},(_details,callback)=>{network++;callback({cancel:true});});
 win=new BrowserWindow({show:false,width:1000,height:700,webPreferences:{nodeIntegration:true,contextIsolation:false,sandbox:false,backgroundThrottling:false}});
 service=require(path.join(uiRoot,'src/file-preview-service')).registerPreviewService(ipcMain,()=>win);ipcMain.handle('archive-active',()=>service.activeCount());await win.loadFile(fixture);
 win.webContents.on('console-message',(_event,level,message)=>{if(level>=2&&!message.includes('Electron Security Warning'))console.error(message);});
 const report=await win.webContents.executeJavaScript('('+async function(uiRoot,zip,gz,trees){
  const assert=require('assert/strict'),path=require('path'),{ipcRenderer}=require('electron'),preview=require(path.join(uiRoot,'src/file-preview'));
  const wait=async fn=>{for(let i=0;i<700;i++){if(await fn())return;await new Promise(resolve=>setTimeout(resolve,10));}throw Error('UI timeout: '+document.body.textContent.slice(-700));};
  const escapeHtml=text=>{const span=document.createElement('span');span.textContent=text;return span.innerHTML;};
  const source=require('fs').readFileSync(path.join(uiRoot,'src/renderer.js'),'utf8');
  const code=source.slice(source.indexOf('// Show the members of a grouped input first.'),source.indexOf('function parseDockerImageSearchTarget('));
  const shared=new Function('document','path','currentLang','API_BASE','mountFilePreview','loadPreviewImage','mountHtmlPreview','makeWindowDraggable','makeWindowResizable','escapeHtml','fileGroupDisplayName','decoratePreviewHeader',code+';return {showFilePreview,createFilePreviewEye};')(document,path,'zh','http://127.0.0.1:1',preview.mountFilePreview,preview.loadPreviewImage,preview.mountHtmlPreview,preview.makeWindowDraggable,preview.makeWindowResizable,escapeHtml,()=>'',preview.decoratePreviewHeader);
  let maxGap=0,last=performance.now();const heartbeat=setInterval(()=>{const now=performance.now();maxGap=Math.max(maxGap,now-last);last=now;},10);
  const started=performance.now(),eye=shared.createFilePreviewEye(zip);document.body.append(eye);eye.click();
  await wait(()=>document.querySelectorAll('.archive-preview-entry').length>0);
  const firstPaint=performance.now()-started;
  function checkLayout(host){
   const box=host.getBoundingClientRect(),left=host.querySelector('.archive-preview-sidebar').getBoundingClientRect(),right=host.querySelector('.archive-preview-content').getBoundingClientRect();
   assert(Math.abs(left.top-right.top)<1,'Member list and preview must share the same top edge');
   assert(Math.abs(left.bottom-right.bottom)<1,'Both panes must use the full available height');
   assert(Math.abs(left.right-right.left)<1,'Preview must sit directly beside the member list');
   assert(Math.abs(right.right-box.right)<1,'Preview must fill the right side without a blank region');
  }
  checkLayout(document.querySelector('.archive-preview'));
  const previewWindow=document.querySelector('.wf-preview-window');
  for(const [width,height] of [[600,400],[900,600]]){previewWindow.style.width=width+'px';previewWindow.style.height=height+'px';checkLayout(document.querySelector('.archive-preview'));}
  assert.equal(document.querySelectorAll('.wf-preview-header .fa-external-link-alt').length,1);
  document.querySelector('[data-entry="0"]').click();await wait(()=>document.querySelector('.cm-content')?.textContent.includes('safe member'));
  assert.equal(globalThis.archiveScript,undefined,'Member names/content never execute HTML');
  const list=document.querySelector('.archive-preview-list');
  for(let i=0;i<10&&!document.querySelector('.archive-preview-load-more').hidden;i++){list.scrollTop=list.scrollHeight;list.dispatchEvent(new Event('scroll'));await new Promise(resolve=>setTimeout(resolve,60));}
  assert(document.querySelector('.archive-preview-load-more').hidden);
  assert.equal(getComputedStyle(document.querySelector('.archive-preview-sidebar > .archive-preview-heading')).display,'none','No member-count row or reserved space');
  assert(document.querySelectorAll('.archive-preview-entry').length<40,'Member rows are virtualized');
  list.scrollTop=0;list.dispatchEvent(new Event('scroll'));document.querySelector('[data-entry="1"]').click();
  await wait(()=>document.querySelector('.archive-preview-image')?.complete&&document.querySelector('.archive-preview-image').naturalWidth===1);
  document.querySelector('.wf-preview-header button:last-child').click();await wait(async()=>await ipcRenderer.invoke('archive-active')===0);
  for(const tree of trees){
  shared.showFilePreview(tree,path.basename(tree));
  await wait(()=>document.querySelector('[data-directory="scripts/"]'));
  const folderStyle=getComputedStyle(document.querySelector('[data-directory="scripts/"] > span'));
  assert.equal(folderStyle.fontStyle,'italic');assert(Number(folderStyle.fontWeight)>=600,'Folder names are bold');
  const fileStyle=getComputedStyle([...document.querySelectorAll('.archive-preview-entry')].find(row=>row.textContent.includes('config.json')).querySelector('span'));
  assert.equal(fileStyle.fontStyle,'normal');assert(Number(fileStyle.fontWeight)<600,'File names keep normal styling');
  assert(document.querySelector('[data-action="parent"]').disabled,'Root has no parent');
  document.querySelector('[data-directory="scripts/"]').click();
  assert.equal(document.querySelector('.archive-preview-path').textContent,'scripts/');
  assert(![...document.querySelectorAll('.archive-preview-entry')].some(row=>row.textContent.includes('config.json')),'Only the current folder is displayed');
  [...document.querySelectorAll('.archive-preview-entry')].find(row=>row.textContent.includes('run.py')).click();
  await wait(()=>document.querySelector('.cm-content')?.textContent.includes('nested script content'));
  document.querySelector('[data-directory="scripts/deep/"]').click();
  [...document.querySelectorAll('.archive-preview-entry')].find(row=>row.textContent.includes('inner.txt')).click();
  await wait(()=>document.querySelector('.cm-content')?.textContent.includes('deep content'));
  document.querySelector('[data-action="parent"]').click();assert.equal(document.querySelector('.archive-preview-path').textContent,'scripts/');
  document.querySelector('[data-action="root"]').click();assert.equal(document.querySelector('.archive-preview-path').textContent,'/');
  document.querySelector('[data-directory="implicit/"]').click();
  [...document.querySelectorAll('.archive-preview-entry')].find(row=>row.textContent.includes('child.txt')).click();
  await wait(()=>document.querySelector('.cm-content')?.textContent.includes('implicit directory content'));
  document.querySelector('[data-action="root"]').click();
  document.querySelector('[data-directory="later/"]').click();
  await wait(()=>[...document.querySelectorAll('.archive-preview-entry')].some(row=>row.textContent.includes('late.txt')));
  [...document.querySelectorAll('.archive-preview-entry')].find(row=>row.textContent.includes('late.txt')).click();
  await wait(()=>document.querySelector('.cm-content')?.textContent.includes('later batch content'));
  document.querySelector('[data-action="root"]').click();document.querySelector('[data-directory="empty/"]').click();
  await wait(()=>document.querySelector('.archive-preview-member')?.textContent.includes('此目录为空'));
  document.querySelector('.wf-preview-header button:last-child').click();await wait(async()=>await ipcRenderer.invoke('archive-active')===0);
  }
  shared.showFilePreview(gz,path.basename(gz));await wait(()=>document.querySelector('.cm-content')?.textContent.includes('sequence-000000'));
  assert(!document.querySelector('.archive-preview-sidebar'),'GZIP text opens directly');
  assert(!document.querySelector('button')?.textContent.includes('Next'));
  const viewport=document.querySelector('.file-preview-scroll');viewport.scrollTop=viewport.scrollHeight;viewport.dispatchEvent(new Event('scroll'));
  await wait(()=>document.querySelectorAll('.file-preview-segment').length>=2);
  document.querySelector('.wf-preview-header button:last-child').click();await wait(async()=>await ipcRenderer.invoke('archive-active')===0);
  // R result viewers call the same mount with an older dark inline body style.
  const host=document.createElement('div');host.style.cssText='display:flex;height:500px;background:#1e1e1e;color:#cfd8dc';document.body.append(host);
  const stop=preview.mountFilePreview(host,zip,{kind:'text',lang:'zh',showExternalOpen:false});await wait(()=>host.querySelector('.archive-preview-entry'));
  checkLayout(host);
  assert.equal(getComputedStyle(host).backgroundColor,'rgb(255, 255, 255)');host.remove();await wait(async()=>await ipcRenderer.invoke('archive-active')===0);stop();
  clearInterval(heartbeat);return {firstPaintMs:Math.round(firstPaint),maxHeartbeatGapMs:Math.round(maxGap),virtualList:true,memberTextAndImage:true,directoryFormats:trees.map(file=>path.basename(file)),implicitAndEmptyFolders:true,laterBatchChildren:true,gzipContinuous:true,closedSessions:0};
 }.toString()+')('+[uiRoot,zip,gz,trees].map(JSON.stringify).join(',')+')');
 assert.equal(network,0);await service.closeAll();console.log(JSON.stringify({ui:'PASS',offline:true,...report}));win.destroy();app.exit(0);
}catch(error){console.error(error.stack);await service?.closeAll();win?.destroy();app.exit(1);}});setTimeout(()=>app.exit(2),35000);
`;
(async()=>{
 const iconv=require('iconv-lite'),JSZip=require('jszip'),crypto=require('crypto');
 const text='safe member <script>globalThis.archiveScript=1</script>\n',image=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lWQAAAAASUVORK5CYII=','base64');
 const zip=new JSZip();zip.file('<img onerror=archiveScript=1>.txt',text);zip.file('image.png',image);for(let i=0;i<350;i++)zip.file('entry-'+i+'.txt','member '+i);
 const zipPath=path.join(directory,'members.zip');fs.writeFileSync(zipPath,await zip.generateAsync({type:'nodebuffer',compression:'DEFLATE'}));
 const tree=new JSZip();tree.file('config.json','root content');tree.file('scripts/run.py','nested script content');tree.file('scripts/deep/inner.txt','deep content');
 tree.file('implicit/child.txt','implicit directory content',{createFolders:false});tree.folder('empty');tree.folder('later');
 for(let i=0;i<150;i++)tree.file('root-'+i+'.txt','root member');tree.file('later/late.txt','later batch content');
 const treePath=path.join(directory,'directories.zip');fs.writeFileSync(treePath,await tree.generateAsync({type:'nodebuffer',compression:'DEFLATE'}));
 const trees=[treePath],tarEntries=[['config.json','root content'],['scripts/',null,'directory'],['scripts/run.py','nested script content'],['scripts/deep/inner.txt','deep content'],['implicit/child.txt','implicit directory content'],['empty/',null,'directory'],['later/',null,'directory'],...Array.from({length:150},(_,i)=>['root-'+i+'.txt','root member']),['later/late.txt','later batch content']];
 for(const gzip of [false,true]){const file=path.join(directory,gzip?'directories.tar.gz':'directories.tar');await tarFixture(file,tarEntries,gzip);trees.push(file);}
 const zipReader=open(zipPath),first=await zipReader.read({action:'list',page:0});assert.equal(first.entries.length,100);assert(first.hasNext);
 assert.equal((await zipReader.read({action:'text',entry:0,page:0})).text,text);assert.deepEqual(Buffer.from((await zipReader.read({action:'blob',entry:1})).bytes),image);
 let all=[...first.entries];for(let page=1;;page++){const result=await zipReader.read({action:'list',page});assert(!result.error,result.error);all.push(...result.entries);if(!result.hasNext)break;}
 assert.equal(all.length,352);await zipReader.close();
 const limitZip=new JSZip();limitZip.file('huge.png',Buffer.alloc(17*1024*1024));const limitPath=path.join(directory,'limit.zip');fs.writeFileSync(limitPath,await limitZip.generateAsync({type:'nodebuffer',compression:'DEFLATE'}));
 const limited=open(limitPath);assert.match((await limited.read({action:'blob',entry:0})).error,/16 MB/);await limited.close();
 const encrypted=Buffer.from(fs.readFileSync(zipPath));for(let i=0;i<encrypted.length-10;i++){
  if(encrypted.readUInt32LE(i)===0x04034b50)encrypted.writeUInt16LE(encrypted.readUInt16LE(i+6)|1,i+6);
  if(encrypted.readUInt32LE(i)===0x02014b50)encrypted.writeUInt16LE(encrypted.readUInt16LE(i+8)|1,i+8);
 }
 const encryptedPath=path.join(directory,'encrypted.zip');fs.writeFileSync(encryptedPath,encrypted);const secured=open(encryptedPath);assert.equal((await secured.read({action:'list',page:0})).entries[0].encrypted,true);assert.match((await secured.read({action:'text',entry:0})).error,/Encrypted/);await secured.close();
 const gz=path.join(directory,'sequences.fastq.gz'),original=Array.from({length:100000},(_,i)=>'sequence-'+String(i).padStart(6,'0')+' '+('ACGT'.repeat(12))+'\r\n').join('');fs.writeFileSync(gz,zlib.gzipSync(original));
 const before=new Map(),hash=file=>crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
 for(const file of [zipPath,gz,...trees])before.set(file,hash(file));
 const reader=open(gz),started=performance.now();let joined='',page=0,result,initial;
 do{result=await reader.read({action:'text',page:page++});assert(!result.error,result.error);initial??=result.text;joined+=result.text;}while(result.hasNext);
 assert.equal(joined,original);assert.equal((await reader.read({action:'text',page:0})).text,initial,'Old chunks can be reread without a disk cache');
 console.log(JSON.stringify({worker:'PASS',textMiB:Buffer.byteLength(original)/1048576,streamAndBacktrackMs:Math.round(performance.now()-started)}));await reader.close();
 for(const encoding of ['utf8','gbk','utf16le','utf16be']){
  const file=path.join(directory,encoding+'.gz'),content='中文 😀 one\r\ntwo\rthree\n'.repeat(35000);fs.writeFileSync(file,zlib.gzipSync(iconv.encode(content,encoding,{addBOM:encoding.startsWith('utf16')})));
  const stream=open(file);let combined='',page=0,current;do{current=await stream.read({action:'text',page:page++});assert(!current.error,current.error);combined+=current.text;}while(current.hasNext);
  assert.equal(combined,iconv.decode(zlib.gunzipSync(fs.readFileSync(file)),encoding));await stream.close();
 }
 for(const gzip of [false,true]){
  const file=path.join(directory,gzip?'members.tar.gz':'members.tar');await tarFixture(file,[['nested/',null,'directory'],['nested/readme.txt',text],['unsafe-link',null,'symlink'],...Array.from({length:205},(_,i)=>['same-name.txt','duplicate '+i])],gzip);
  const archive=open(file),listed=await archive.read({action:'list',page:0});assert(!listed.error,listed.error);assert.equal(listed.entries.length,100);assert.equal(listed.entries[2].type,'link');
  assert.match((await archive.read({action:'text',entry:2})).error,/links/);assert.equal((await archive.read({action:'text',entry:1})).text,text);
  assert.equal((await archive.read({action:'text',entry:207})).text,'duplicate 204');assert.equal((await archive.read({action:'list',page:1})).entries.length,100);assert.equal((await archive.read({action:'list',page:2})).entries.length,8);await archive.close();
 }
 const bad=path.join(directory,'bad.gz');fs.writeFileSync(bad,Buffer.from('not gzip'));const broken=open(bad);assert((await broken.read({action:'text'})).error);await broken.close();
 const largeTar=path.join(directory,'large-first.tar.gz');await tarFixture(largeTar,[['first.txt',Buffer.alloc(2*1024*1024,65)],['second.txt','second']],true);
 const largeTarReader=open(largeTar),largeTarFirst=await largeTarReader.read({action:'list',page:0});assert(!largeTarFirst.error,largeTarFirst.error);assert.equal(largeTarFirst.entries.length,1,'Large TAR header is returned before draining the member');assert.equal((await largeTarReader.read({action:'list',page:1})).entries[0].name,'second.txt');await largeTarReader.close();
 const binary=path.join(directory,'binary.gz');fs.writeFileSync(binary,zlib.gzipSync(Buffer.from([1,0,3,4])));const binaryReader=open(binary);assert.match((await binaryReader.read({action:'text'})).error,/Binary/);await binaryReader.close();
 const changed=path.join(directory,'changed.zip');fs.copyFileSync(zipPath,changed);const changeReader=open(changed);await changeReader.read({action:'list',page:0});fs.appendFileSync(changed,'x');assert.match((await changeReader.read({action:'list',page:1})).error,/File changed/);await changeReader.close();
 // Service creates no preview cache directory for archive sessions and cancels pending work.
 const {EventEmitter}=require('events'),owner=new EventEmitter();owner.mainFrame={};const event={sender:owner,senderFrame:owner.mainFrame},handlers={};
 const service=require('../src/file-preview-service').registerPreviewService({handle:(name,fn)=>handlers[name]=fn},()=>({webContents:owner}));
 const cacheRoot=path.join(os.tmpdir(),'PrimiGenius-preview-'+crypto.createHash('sha256').update(path.join(root,'src')).digest('hex').slice(0,16));
 const cacheBefore=fs.existsSync(cacheRoot)?fs.readdirSync(cacheRoot):[];
 const id=handlers['file-preview-open'](event,gz,'archive');assert((await handlers['file-preview-page'](event,id,{action:'text',page:0})).text.startsWith('sequence-000000'));
 assert.deepEqual(fs.existsSync(cacheRoot)?fs.readdirSync(cacheRoot):[],cacheBefore,'Archive sessions create no disk cache');
 await handlers['file-preview-close'](event,id);assert.equal(service.activeCount(),0);assert.equal(owner.listenerCount('destroyed'),0);
 const canceled=handlers['file-preview-open'](event,gz,'archive'),pending=handlers['file-preview-page'](event,canceled,{action:'text',page:9999}),rejected=assert.rejects(pending,/Preview closed/);await service.closeAll();await rejected;
 const script=path.join(directory,'electron-check.cjs');fs.writeFileSync(script,harness);
 const env={...process.env};delete env.ELECTRON_RUN_AS_NODE;
 const output=require('child_process').spawnSync(path.join(root,'node_modules/electron/dist/electron.exe'),[script,root,zipPath,gz,JSON.stringify(trees)],{env,encoding:'utf8',windowsHide:true,timeout:40000});
 process.stdout.write(output.stdout||'');process.stderr.write(output.stderr||'');if(output.error)console.error(output.error);assert.equal(output.status,0,'Electron archive preview acceptance');
 for(const [file,digest] of before)assert.equal(hash(file),digest,'Sources are unchanged');
 console.log('PASS: ZIP/TAR/TAR.GZ member lists, duplicate members, link rejection, GZIP UTF-8/GBK/UTF-16, continuous reads/backtracking, corrupt/binary/changed files, cancellation, no extraction/cache, unchanged source bytes.');
})().catch(error=>{console.error(error.stack);process.exitCode=1;}).finally(async()=>{
 await Promise.all([...active].map(worker=>worker.terminate()));
 const target=path.resolve(directory);if(path.dirname(target)!==root||!path.basename(target).startsWith('.tmp-archive-preview-'))throw Error('Unexpected cleanup target');
 fs.rmSync(target,{recursive:true,force:true,maxRetries:6,retryDelay:200});
});
