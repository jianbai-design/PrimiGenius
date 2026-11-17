'use strict';
const fs=require('fs'),path=require('path'),cp=require('child_process');
const root=path.resolve(__dirname,'..'),temp=fs.mkdtempSync(path.join(root,'.tmp-annotation-preview-'));
const harness=String.raw`
const {app,BrowserWindow,ipcMain,session}=require('electron'),fs=require('fs'),path=require('path'),assert=require('assert/strict');
app.setPath('userData',path.join(__dirname,'profile'));
for(const flag of ['no-sandbox','in-process-gpu','disable-gpu'])app.commandLine.appendSwitch(flag);
app.whenReady().then(async()=>{let win,service;try{
 const root=process.argv[2];let network=0,indexDialogs=0;
 session.defaultSession.webRequest.onBeforeRequest({urls:['http://*/*','https://*/*']},(_details,callback)=>{network++;callback({cancel:true});});
 const html=path.join(__dirname,'fixture.html');fs.writeFileSync(html,'<!doctype html><style>'+fs.readFileSync(path.join(root,'src/styles.css'),'utf8')+'</style><body></body>');
 win=new BrowserWindow({show:false,width:1100,height:800,webPreferences:{nodeIntegration:true,contextIsolation:false,sandbox:false,backgroundThrottling:false}});
 service=require(path.join(root,'src/file-preview-service')).registerPreviewService(ipcMain,()=>win);
 ipcMain.handle('open-file-dialog',()=>{indexDialogs++;throw Error('Index selection is not needed');});
 ipcMain.handle('annotation-preview-count',()=>service.activeCount());
 await win.loadFile(html);
 const result=await win.webContents.executeJavaScript('('+async function(root,temp){
  const fs=require('fs'),path=require('path'),assert=require('assert/strict'),{ipcRenderer}=require('electron');
  const preview=require(path.join(root,'src/file-preview')),cm=require(path.join(root,'src/preview-codemirror'));
  const source=fs.readFileSync(path.join(root,'src/renderer.js'),'utf8');
  const code=source.slice(source.indexOf('function showFilePreview('),source.indexOf('\nfunction createFilePreviewEye('));
  const escapeHtml=s=>{const el=document.createElement('span');el.textContent=s;return el.innerHTML;};
  const show=new Function('path','currentLang','mountFilePreview','loadPreviewImage','mountHtmlPreview','makeWindowDraggable','makeWindowResizable','escapeHtml','decoratePreviewHeader',code+';return showFilePreview;')(path,'zh',preview.mountFilePreview,preview.loadPreviewImage,preview.mountHtmlPreview,preview.makeWindowDraggable,preview.makeWindowResizable,escapeHtml,preview.decoratePreviewHeader);
  const wait=async check=>{for(let i=0;i<400;i++){if(await check())return;await new Promise(resolve=>setTimeout(resolve,10));}throw Error('Preview timed out: '+document.body.textContent.slice(-300));};
  const cases=[
   ['mcscanx_input.all.gff','chr1\tgene_FIRST\t100\t200\n'.repeat(140000)],
   ['annotation.gff3','##gff-version 3\nchr1\tsource\tgene\t100\t200\t.\t+\t.\tID=gene_FIRST\n'],
   ['annotation.gtf','chr1\tsource\tgene\t100\t200\t.\t+\t.\tgene_id "gene_FIRST";\n'],
   ['features.bed','chr1\t100\t200\tgene_FIRST\n'],
   ['variants.vcf','##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\nchr1\t100\tgene_FIRST\tA\tG\t.\tPASS\t.\n'],
   ['annotation.gff3.gz','##gff-version 3\nchr1\tsource\tgene\t100\t200\t.\t+\t.\tID=gene_FIRST\n']
  ];
  for(const [name,content] of cases){
   const file=path.join(temp,name),bytes=name.endsWith('.gz')?require('zlib').gzipSync(content):Buffer.from(content);
   fs.writeFileSync(file,bytes);show(file,name);
   await wait(()=>document.querySelector('.wf-preview-body .cm-content')?.textContent.includes('gene_FIRST'));
   assert.equal(document.querySelector('.wf-preview-header .file-preview-lines'),null);
   assert(!document.body.textContent.includes('行数统计'));
   assert.equal(document.querySelectorAll('.wf-preview-body iframe').length,0);
   const editor=document.querySelector('.wf-preview-body .cm-editor');
   assert(cm.EditorView.findFromDOM(editor).state.doc.toString().startsWith(content.slice(0,24)),'Header and first feature are preserved');
   document.querySelector('.wf-preview-header button:last-child').click();
   await wait(async()=>await ipcRenderer.invoke('annotation-preview-count')===0);
   assert.deepEqual(fs.readFileSync(file),bytes,'Preview does not modify the source');
  }
  const host=document.createElement('div');host.style.cssText='height:300px;display:flex';document.body.append(host);
  const stop=preview.mountFilePreview(host,path.join(temp,cases[0][0]));
  await wait(()=>host.querySelector('.cm-content')?.textContent.includes('gene_FIRST'));
  assert.equal(host.querySelectorAll('.tabulator,iframe').length,0,'Default mount also uses text for annotations');
  stop();host.remove();await wait(async()=>await ipcRenderer.invoke('annotation-preview-count')===0);
  const tableFile=path.join(temp,'large.tsv');fs.writeFileSync(tableFile,'id\tvalue\n'+Array.from({length:50000},(_,i)=>i+'\tvalue'+i+'\n').join(''));
  show(tableFile,'large.tsv');
  const tableView=document.querySelector('.wf-preview-body .file-preview-scroll');
  await wait(()=>tableView.querySelector('.tabulator-row'));
  assert.equal(document.querySelector('.wf-preview-header .file-preview-lines'),null);
  assert(document.querySelector('.file-preview-freeze input').checked,'Freeze control stays available');
  await new Promise(resolve=>setTimeout(resolve,100));
  for(let i=0;i<3;i++){
   const before=tableView.querySelectorAll('.file-preview-segment').length;
   tableView.scrollTop=tableView.scrollHeight;tableView.dispatchEvent(new Event('scroll'));
   await wait(()=>tableView.querySelectorAll('.file-preview-segment').length>before);
  }
  tableView.scrollTop=0;tableView.dispatchEvent(new Event('scroll'));
  await wait(()=>tableView.querySelector('.file-preview-segment[data-segment="0"][data-loaded="true"] .tabulator-row'));
  assert.equal(tableView.querySelector('.file-preview-segment[data-segment="0"] .tabulator-row [tabulator-field="c0"]').textContent,'0');
  document.querySelector('.wf-preview-header button:last-child').click();await wait(async()=>await ipcRenderer.invoke('annotation-preview-count')===0);
  return {result:'PASS',formats:cases.map(item=>item[0]),largeGffBytes:Buffer.byteLength(cases[0][1]),defaultMount:true,unchangedSources:true,noRowCount:true,tableScrollAndBacktrack:true};
 }.toString()+')('+JSON.stringify(root)+','+JSON.stringify(__dirname)+')');
 assert.equal(network,0);assert.equal(indexDialogs,0);await service.closeAll();
 console.log(JSON.stringify({...result,offline:true,noReferenceOrIndex:true}));win.destroy();app.exit(0);
}catch(error){console.error(error.stack);await service?.closeAll();win?.destroy();app.exit(1);}});
setTimeout(()=>app.exit(2),30000);
`;
let result;
try{
 const script=path.join(temp,'check.cjs');fs.writeFileSync(script,harness);
 const env={...process.env};delete env.ELECTRON_RUN_AS_NODE;
 result=cp.spawnSync(path.join(root,'node_modules/electron/dist/electron.exe'),[script,root],{env,encoding:'utf8',windowsHide:true,timeout:35000});
 process.stdout.write(result.stdout||'');process.stderr.write(result.stderr||'');
}finally{
 if(path.dirname(path.resolve(temp))!==root||!path.basename(temp).startsWith('.tmp-annotation-preview-'))throw Error('Unexpected cleanup target');
 fs.rmSync(temp,{recursive:true,force:true,maxRetries:6,retryDelay:200});
}
process.exitCode=result?.status??1;
