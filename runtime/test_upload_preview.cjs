'use strict';
// Exercise the actual upload branches in a disposable Electron renderer.
const fs=require('fs'),path=require('path'),cp=require('child_process');
const root=path.resolve(__dirname,'..'),dir=fs.mkdtempSync(path.join(root,'.tmp-upload-preview-'));
const harness=String.raw`
const {app,BrowserWindow,ipcMain}=require('electron'),fs=require('fs'),path=require('path');
app.setPath('userData',path.join(__dirname,'profile'));
for(const name of ['no-sandbox','in-process-gpu','disable-gpu'])app.commandLine.appendSwitch(name);
app.whenReady().then(async()=>{let win,service;try{
 const root=process.argv[2],source=fs.readFileSync(path.join(root,'src/renderer.js'),'utf8');
 const files=['CDS & 中文.txt','second.txt'].map(name=>path.join(__dirname,name));
 for(const file of files)fs.writeFileSync(file,'preview source content\n'.repeat(1000));
 const scrolling=path.join(__dirname,'scrolling.txt');
 fs.writeFileSync(scrolling,Array.from({length:40000},(_,i)=>String(i).padStart(6,'0')+' ACGT'.repeat(12)+'\n').join(''));
 const scrollHash=require('crypto').createHash('sha256').update(fs.readFileSync(scrolling)).digest('hex');
 const csv=path.join(__dirname,'scrolling.csv');fs.writeFileSync(csv,'id,value,分类,是否删除,是否公开,属性,开源协议,备注\n'+Array.from({length:3000},(_,i)=>i+',item'+i+',type,no,yes,镜像,MIT,plain\n').join(''));
 const XLSX=require('xlsx'),book=XLSX.utils.book_new();XLSX.utils.book_append_sheet(book,XLSX.utils.aoa_to_sheet([['id','value'],...Array.from({length:2300},(_,i)=>[i,'item'+i])]),'Data');
 const xlsx=path.join(__dirname,'scrolling.xlsx'),xls=path.join(__dirname,'scrolling.xls');XLSX.writeFile(book,xlsx);XLSX.writeFile(book,xls);
 const fixture=path.join(__dirname,'fixture.html');fs.writeFileSync(fixture,'<!doctype html><style>'+fs.readFileSync(path.join(root,'src/styles.css'),'utf8')+'</style><body></body>');
 win=new BrowserWindow({show:false,width:1200,height:900,webPreferences:{nodeIntegration:true,contextIsolation:false,sandbox:false,backgroundThrottling:false}});
 service=require(path.join(root,'src/file-preview-service')).registerPreviewService(ipcMain,()=>win);
 ipcMain.handle('audit-preview-count',()=>service.activeCount());await win.loadFile(fixture);
 const result=await win.webContents.executeJavaScript('('+async function(root,source,files,scrolling,scrollHash,csv,xlsx,xls){
  const assert=require('assert'),path=require('path'),previewModule=require(path.join(root,'src/file-preview')),{ipcRenderer}=require('electron');
  const wait=async fn=>{for(let i=0;i<300;i++){if(await fn())return;await new Promise(r=>setTimeout(r,10));}throw Error('UI condition timed out: '+document.body.textContent.slice(-500));};
  const escapeHtml=s=>{const el=document.createElement('span');el.textContent=s;return el.innerHTML;};
  let shared;
  const helpers=new Function('path','document','currentLang','t','createFilePreviewEye',source.slice(source.indexOf('function isFileGroupingEnabled('),source.indexOf('async function post('))+';return {updateChips,autoGroupFiles,fileGroupDisplayName};')(path,document,'zh',()=> 'Drop files',files=>shared.createFilePreviewEye(files));
  const sharedCode=source.slice(source.indexOf('// Show the members of a grouped input first.'),source.indexOf('function parseDockerImageSearchTarget('));
  shared=new Function('document','path','currentLang','API_BASE','mountFilePreview','loadPreviewImage','mountHtmlPreview','makeWindowDraggable','makeWindowResizable','escapeHtml','fileGroupDisplayName','decoratePreviewHeader',sharedCode+';return {createFilePreviewEye};')(document,path,'zh','http://127.0.0.1:1',previewModule.mountFilePreview,previewModule.loadPreviewImage,previewModule.mountHtmlPreview,previewModule.makeWindowDraggable,previewModule.makeWindowResizable,escapeHtml,helpers.fileGroupDisplayName,previewModule.decoratePreviewHeader);
  const a=source.indexOf("            if (param.type === 'file' && isMultiFile)"),b=source.indexOf("            } else if (param.type==='color')",a);
  const build=new Function('param','S','row','rStateChanged','createFilePreviewEye','isMultiFile','currentLang','escapeHtml',source.slice(a,b)+'}');
  const fileObjects=files.map(file=>{const f=new File(['unchanged payload'],path.basename(file));Object.defineProperty(f,'path',{value:file});return f;});
  const checkTextSurface=pre=>{
   const style=getComputedStyle(pre);
   assert.equal(style.backgroundColor,'rgb(255, 255, 255)');assert.equal(style.color,'rgb(51, 51, 51)');assert.equal(style.maxHeight,'none');
   assert(pre.querySelector('.cm-content'),'CodeMirror renders text');
   const viewport=pre.closest('.file-preview-scroll') || pre.parentElement;assert(viewport.scrollHeight>viewport.clientHeight);viewport.scrollTop=viewport.scrollHeight;
   if(pre.closest('.wf-preview-window'))assert.equal(getComputedStyle(viewport.nextElementSibling).display,'none','No text progress toolbar');
   const buttons=viewport.nextElementSibling.querySelectorAll('button');
   assert.equal(buttons.length,pre.closest('.wf-preview-window')?0:1,'Text scroll has no page buttons and external open appears only once');
   assert(viewport.scrollTop>0);assert.equal(getComputedStyle(pre).backgroundColor,style.backgroundColor);
  };
  const openEye=async eye=>{
   eye.click();await wait(()=>document.querySelector('.wf-preview-body .cm-editor')?.textContent.includes('preview source content'));
   checkTextSurface(document.querySelector('.wf-preview-body .cm-editor'));
   const header=document.querySelector('.wf-preview-header');
   await wait(()=>header.querySelector('.file-preview-meta')?.textContent.includes('｜'));
   await wait(()=>header.querySelector('.file-preview-lines')?.textContent.includes('1,000'));
   assert.equal(getComputedStyle(header).backgroundColor,'rgb(245, 245, 245)');assert(header.offsetHeight<=36,'Compact preview header');
   assert(header.querySelector('.file-preview-meta').textContent.endsWith('TXT'));
   header.querySelector('button:last-child').click();await wait(async()=>await ipcRenderer.invoke('audit-preview-count')===0);
  };
  const row=document.createElement('div');document.body.append(row);const param={id:'input',type:'file'},state={};let changes=0;
  build(param,state,row,()=>changes++,shared.createFilePreviewEye,false,'zh',escapeHtml);
  const zone=row.querySelector('.r-file-drop'),input=row.querySelector('input');let picker=0;input.click=()=>picker++;
  zone.ondrop({preventDefault(){},dataTransfer:{files:[fileObjects[0]]}});
  assert.strictEqual(param._file,fileObjects[0]);assert.equal(state.input,files[0]);assert(zone.querySelector('.file-chip'));
  const before=changes;await openEye(zone.querySelector('.chip-eye'));assert.equal(picker,0);assert.equal(changes,before);assert.strictEqual(param._file,fileObjects[0]);
  const cli=document.createElement('div');document.body.append(cli);helpers.updateChips({fileLists:{input:[files[0]]},rawFilePool:{input:new Set([files[0]])}},param,cli);
  assert.equal(getComputedStyle(cli.querySelector('.file-chip')).backgroundColor,getComputedStyle(zone.querySelector('.file-chip')).backgroundColor);
  await openEye(cli.querySelector('.chip-eye'));
  zone.querySelector('.r-file-del-btn').click();assert.equal(state.input,'');assert.equal(param._file,null);assert(!zone.querySelector('.file-chip'));assert.equal(picker,0);
  Object.defineProperty(input,'files',{value:[fileObjects[1]]});input.onchange();assert.strictEqual(param._file,fileObjects[1]);assert.equal(state.input,files[1]);
  const multi=document.createElement('div');document.body.append(multi);const pm={id:'many',type:'file',allow_multiple:true},ms={};
  build(pm,ms,multi,()=>{},shared.createFilePreviewEye,true,'zh',escapeHtml);
  const add=multi.querySelector('.r-multifile-add-zone');add.ondrop({preventDefault(){},dataTransfer:{files:fileObjects}});
  add.ondrop({preventDefault(){},dataTransfer:{files:[fileObjects[0]]}});assert.equal(pm._files.length,2);assert.equal(ms.many,files.join(','));
  await openEye(multi.querySelector('.chip-eye'));assert.strictEqual(pm._files[0],fileObjects[0]);
  const target=multi.querySelectorAll('.r-multifile-chip')[1],drop=new Event('drop',{bubbles:true,cancelable:true});
  Object.defineProperty(drop,'dataTransfer',{value:{types:['text/x-primigenius-file-index'],getData:()=> '0'}});target.dispatchEvent(drop);
  assert.strictEqual(pm._files[0],fileObjects[1]);assert.equal(ms.many,[files[1],files[0]].join(','));
  multi.querySelector('.r-multifile-remove').click();assert.deepEqual(pm._files,[fileObjects[0]]);assert.equal(ms.many,files[0]);
  // Run the real R file serialization branch with the original browser File objects.
  const run=source.indexOf('    runBtn.onclick = async () =>',source.indexOf('function renderRPanelUI('));
  const serialStart=source.indexOf("                if (pm.type === 'file')",run),serialEnd=source.indexOf("                } else if (pm.type === 'checkbox')",serialStart);
  const form=new FormData(),params={};
  const serialize=new Function('pm','form','params',source.slice(serialStart,serialEnd)+'}');
  serialize(param,form,params);serialize(pm,form,params);
  assert.equal(form.get('input').name,fileObjects[1].name);assert.equal(await form.get('input').text(),'unchanged payload');
  assert.equal(form.getAll('many').length,1);assert.equal(params.input,fileObjects[1].name);assert.equal(params.many,fileObjects[0].name);
  for(const options of [{is_paired:true},{group_replicates:true},{file_grouping:true}]){
   const el=document.createElement('div');document.body.append(el);const group=[...files],inst={fileLists:{g:[group]},rawFilePool:{g:new Set(files)}};
   helpers.updateChips(inst,{id:'g',...options},el);el.querySelector('.chip-eye').click();assert.equal(document.querySelectorAll('.wf-group-preview-file').length,2);
   document.querySelector('.wf-group-preview-eye').click();await wait(()=>document.querySelector('.wf-preview-body .cm-editor')?.textContent.includes('preview source content'));
   for(const button of [...document.querySelectorAll('.wf-preview-header button:last-child')].reverse())button.click();
   assert.strictEqual(inst.fileLists.g[0],group);el.querySelector('.del-btn').click();assert.equal(inst.fileLists.g.length,0);assert.equal(inst.rawFilePool.g.size,0);el.remove();
  }
  await wait(async()=>await ipcRenderer.invoke('audit-preview-count')===0);
  // File headings, cell values, and hover cards remain text even if they contain HTML.
  const untrusted=path.join(path.dirname(scrolling),'untrusted.csv'),payload='<img src=x onerror=globalThis.previewInjected=true>';
  require('fs').writeFileSync(untrusted,payload+'\n'+payload+'\n');globalThis.previewInjected=false;
  const safeHost=document.createElement('div');safeHost.style.cssText='height:300px;display:flex;';document.body.append(safeHost);
  previewModule.mountFilePreview(safeHost,untrusted,{showExternalOpen:false});await wait(()=>safeHost.querySelector('.tabulator-row .tabulator-cell'));
  assert(!safeHost.querySelector('img'));assert(safeHost.querySelector('.tabulator-header').textContent.includes(payload));
  for(const label of safeHost.querySelectorAll('.tabulator-col-title'))assert(label.getBoundingClientRect().bottom<=label.closest('.tabulator-header').getBoundingClientRect().bottom+1,'Header text must fit inside the header height');
  const valueCell=safeHost.querySelectorAll('.tabulator-row .tabulator-cell')[1];valueCell.dispatchEvent(new MouseEvent('mousemove',{bubbles:true,clientX:100,clientY:100}));
  await wait(()=>document.querySelector('.tabulator-tooltip'));assert(!document.querySelector('.tabulator-tooltip img'));assert(document.querySelector('.tabulator-tooltip').textContent.includes(payload));assert.equal(globalThis.previewInjected,false);
  safeHost.remove();await wait(async()=>await ipcRenderer.invoke('audit-preview-count')===0);
  const purifier=require(path.join(root,'src/preview-sanitize')).default;
  const clean=purifier.sanitize('<svg xmlns="http://www.w3.org/2000/svg" onload="bad()"><script>bad()</script><rect x="10" width="20"/><text>中文</text></svg>',{USE_PROFILES:{svg:true,svgFilters:true}});
  assert(!clean.includes('onload'));assert(!clean.includes('<script'));assert(clean.includes('width="20"'));assert(clean.includes('中文'));
  const wideFile=path.join(path.dirname(scrolling),'horizontal.txt');
  require('fs').writeFileSync(wideFile,'> '+ 'A'.repeat(2000)+'\n'+Array.from({length:18000},(_,i)=>i+' ACGT'.repeat(12)+'\n').join(''));
  const wideHost=document.createElement('div');wideHost.style.cssText='position:fixed;top:0;left:0;width:600px;height:300px;display:flex;z-index:99999;';document.body.append(wideHost);
  previewModule.mountFilePreview(wideHost,wideFile,{kind:'text',showExternalOpen:false});
  const horizontal=wideHost.querySelector('.file-preview-scroll');await wait(()=>horizontal.scrollWidth>horizontal.clientWidth);
  assert.equal(horizontal.scrollTop,0);assert(horizontal.offsetHeight>horizontal.clientHeight,'Horizontal scrollbar is available at the viewport bottom before vertical scrolling');
  const widthBefore=horizontal.scrollWidth,firstGutter=horizontal.querySelector('.cm-gutters'),gutterLeft=firstGutter.getBoundingClientRect().left;
  horizontal.scrollLeft=250;await new Promise(resolve=>setTimeout(resolve,50));assert.equal(horizontal.scrollLeft,250);
  assert(Math.abs(firstGutter.getBoundingClientRect().left-gutterLeft)<=1,'Line numbers remain fixed during horizontal scrolling');
  assert.equal(getComputedStyle(horizontal.querySelector('.cm-scroller')).overflowX,'visible','Segments do not own hidden horizontal scrollbars');
  for(let pass=0;pass<12;pass++){
   const before=horizontal.querySelectorAll('.file-preview-segment').length;
   if(horizontal.lastElementChild.textContent.includes('末尾'))break;
   horizontal.scrollTop=horizontal.scrollHeight;horizontal.dispatchEvent(new Event('scroll'));
   await wait(()=>horizontal.querySelectorAll('.file-preview-segment').length>before||horizontal.lastElementChild.textContent.includes('末尾'));
  }
  await wait(()=>horizontal.querySelector('.file-preview-segment').dataset.loaded==='false');
  assert.equal(horizontal.scrollLeft,250,'Horizontal position survives unloading an offscreen segment');
  assert(horizontal.scrollWidth>=widthBefore-1,'The horizontal range survives unloading the widest segment');
  horizontal.scrollTop=0;await wait(()=>horizontal.querySelector('.file-preview-segment[data-loaded="true"] .cm-content'));
  wideHost.remove();await wait(async()=>await ipcRenderer.invoke('audit-preview-count')===0);
  const long=document.createElement('div');long.style.cssText='height:400px;display:flex;';document.body.append(long);
  previewModule.mountFilePreview(long,scrolling,{kind:'text',showExternalOpen:false});
  const viewport=long.querySelector('.file-preview-scroll'),seen=new Map();
  const capture=()=>{for(const segment of viewport.querySelectorAll('.file-preview-segment'))if(segment.querySelector('.cm-editor'))seen.set(Number(segment.dataset.segment),require(path.join(root,'src/preview-codemirror')).EditorView.findFromDOM(segment.querySelector('.cm-editor'))?.state.doc.toString());};
  await wait(()=>viewport.querySelector('.cm-editor'));capture();
  assert.equal(long.querySelectorAll('button').length,0,'No pagination controls in continuous text');
  for(let pass=0;pass<50;pass++){
   capture();const before=viewport.querySelectorAll('.file-preview-segment').length;
   if(viewport.lastElementChild.textContent==='已到文件末尾')break;
   viewport.scrollTop=viewport.scrollHeight;
   await wait(()=>{capture();return viewport.querySelectorAll('.file-preview-segment').length>before || viewport.lastElementChild.textContent==='已到文件末尾';});
  }
  capture();assert.equal(viewport.lastElementChild.textContent,'已到文件末尾');
  const segments=viewport.querySelectorAll('.file-preview-segment');assert(segments.length>5);
  await wait(()=>viewport.querySelectorAll('.cm-editor').length<=3);
  const full=[...seen].sort((a,b)=>a[0]-b[0]).map(entry=>entry[1]).join('');
  assert.equal(require('crypto').createHash('sha256').update(full).digest('hex'),scrollHash,'Continuous scrolling covers every source character');
  viewport.scrollTop=0;await wait(()=>segments[0].firstChild);assert(segments[0].querySelector('.cm-content').textContent.startsWith('000000'));
  long.remove();await wait(async()=>await ipcRenderer.invoke('audit-preview-count')===0);
  const tableReports=[];
  for(const [file,count] of [[csv,3000],[xlsx,2300],[xls,2300]]){
   const tableHost=document.createElement('div');tableHost.style.cssText='height:400px;display:flex;';document.body.append(tableHost);
   let beats=0,maxGap=0,lastBeat=performance.now();const heartbeat=setInterval(()=>{const now=performance.now();maxGap=Math.max(maxGap,now-lastBeat);lastBeat=now;beats++;},5),started=performance.now();
   const title=document.createElement('div');title.className='r-viewer-window';title.style.cssText='height:400px;display:flex;flex-direction:column;';tableHost.replaceWith(title);
   const header=document.createElement('div');header.className='r-viewer-header';header.innerHTML='<span>file</span><button class="r-viewer-close">×</button>';title.append(header,tableHost);tableHost.style.cssText='display:flex;min-height:0;flex:1;';
   previewModule.decoratePreviewHeader(header,file);
   previewModule.mountFilePreview(tableHost,file,{showExternalOpen:false});
   const view=tableHost.querySelector('.file-preview-scroll'),collected=new Set();
   const captureRows=()=>{for(const el of view.querySelectorAll('.tabulator')){const grid=require(path.join(root,'src/preview-table')).Tabulator.findTable(el)[0];for(const record of grid?.getData()||[]){assert.equal(record.c1,'item'+record.c0);collected.add(Number(record.c0));}}for(const row of view.querySelectorAll('.file-preview-segment[data-loaded="true"] .tabulator-row')){
    const cells=row.querySelectorAll('.tabulator-cell');if(cells.length>1){const id=Number(cells[1].textContent);assert.equal(cells[2].textContent,'item'+id);collected.add(id);}
   }};
   await wait(()=>view.querySelector('.file-preview-segment[data-loaded="true"] .tabulator-row'));const firstPaint=performance.now()-started;
   await wait(()=>header.querySelector('.file-preview-lines').textContent.includes(file===csv?'3,000':file===xlsx?'2,300':'2,301'));
   if(file===csv){
    title.style.cssText+='position:fixed;top:20px;left:20px;width:720px;z-index:100000;';
    await new Promise(resolve=>setTimeout(resolve,80));
    const block=view.querySelector('.file-preview-segment'),heading=block.querySelector('.tabulator-header');
    for(const label of heading.querySelectorAll('.tabulator-col-title'))assert(label.getBoundingClientRect().bottom<=heading.getBoundingClientRect().bottom+1,'Chinese table headings remain fully visible');
    view.scrollTop=350;view.dispatchEvent(new Event('scroll'));await new Promise(resolve=>setTimeout(resolve,50));
    assert(Math.abs(heading.getBoundingClientRect().top-view.getBoundingClientRect().top)<=2,'Header remains pinned while scrolling down');
    const label=heading.querySelector('.tabulator-col-title'),labelBox=label.getBoundingClientRect();
    const hit=document.elementFromPoint(labelBox.left+5,labelBox.top+labelBox.height/2);
    assert(hit?.closest('.tabulator-header'),'Pinned heading is painted above data rows: '+JSON.stringify({label:{x:labelBox.x,y:labelBox.y,width:labelBox.width,height:labelBox.height},header:{y:heading.getBoundingClientRect().y,height:heading.offsetHeight},hit:hit?.outerHTML.slice(0,180)}));
    const row=block.querySelector('.tabulator-row'),cell=row.querySelector('[tabulator-field="c0"]'),left=cell.getBoundingClientRect().left;
    view.scrollLeft=200;view.dispatchEvent(new Event('scroll'));await new Promise(resolve=>setTimeout(resolve,40));
    assert(Math.abs(cell.getBoundingClientRect().left-left)<=2,'First data column remains pinned horizontally');
    const toggle=header.querySelector('.file-preview-freeze input');assert(toggle&&toggle.checked);
    toggle.checked=false;toggle.dispatchEvent(new Event('change'));await new Promise(resolve=>setTimeout(resolve,40));
    assert(heading.getBoundingClientRect().bottom<view.getBoundingClientRect().top,'Disabling freeze releases the header');
    toggle.checked=true;toggle.dispatchEvent(new Event('change'));await new Promise(resolve=>setTimeout(resolve,40));
    assert(Math.abs(heading.getBoundingClientRect().top-view.getBoundingClientRect().top)<=2,'Re-enabling freeze restores the header');
    view.scrollLeft=0;view.scrollTop=0;view.dispatchEvent(new Event('scroll'));
   }
   assert.equal(tableHost.querySelectorAll('button').length,0);assert.equal(getComputedStyle(view.nextElementSibling).display,'none');
   for(let pass=0;pass<50;pass++){
    captureRows();const before=view.querySelectorAll('.file-preview-segment').length;
    if(view.lastElementChild.textContent.includes('末尾'))break;
    view.scrollTop=view.scrollHeight;view.dispatchEvent(new Event('scroll'));
    await wait(()=>{captureRows();return view.querySelectorAll('.file-preview-segment').length>before || view.lastElementChild.textContent.includes('末尾');});
    if(file===csv&&pass===5){
     await new Promise(resolve=>setTimeout(resolve,40));
     const top=view.getBoundingClientRect().top;
     const active=[...view.querySelectorAll('.file-preview-segment[data-loaded="true"]')].find(block=>block.getBoundingClientRect().top<=top+1&&block.getBoundingClientRect().bottom>top+40);
     assert(active&&Number(active.dataset.segment)>0,'Check freezing after crossing multiple data segments');
     const heading=active.querySelector('.tabulator-header');assert(Math.abs(heading.getBoundingClientRect().top-top)<=2,'Later segments keep their heading pinned');
     const label=heading.querySelector('.tabulator-col-title').getBoundingClientRect();assert(document.elementFromPoint(label.left+5,label.top+label.height/2)?.closest('.tabulator-header'),'Frozen corner stays above body cells in later segments');
    }
   }
   await wait(()=>{captureRows();return collected.size===count;});assert(collected.has(0));assert(collected.has(count-1));
   assert(view.querySelector('.tabulator-header'));assert(!/Previous|Next|Page 1|Excel preview|首个工作表/.test(tableHost.textContent));
   await wait(()=>view.querySelectorAll('.file-preview-segment[data-loaded="true"] .tabulator-row').length<=300);
    view.scrollTop=0;await wait(()=>view.querySelector('.file-preview-segment[data-segment="0"][data-loaded="true"] .tabulator-row .tabulator-cell'));assert.equal(view.querySelector('.tabulator-row .tabulator-cell').textContent,'1');
   assert(beats>0);clearInterval(heartbeat);tableReports.push({table:require('path').extname(file),rowsVisited:collected.size,firstPaintMs:Math.round(firstPaint),liveRows:view.querySelectorAll('.file-preview-segment[data-loaded="true"] .tabulator-row').length,maxHeartbeatGapMs:Math.round(maxGap)});
   title.remove();await wait(async()=>await ipcRenderer.invoke('audit-preview-count')===0);
  }
  const output=document.createElement('div');output.className='r-viewer-body';output.style.cssText='display:flex;height:300px;';document.body.append(output);
  previewModule.mountFilePreview(output,files[0],{kind:'text'});await wait(()=>output.querySelector('.cm-editor'));checkTextSurface(output.querySelector('.cm-editor'));output.remove();
  await wait(async()=>await ipcRenderer.invoke('audit-preview-count')===0);
  for(const className of ['r-viewer-header','wf-preview-header']){
   const win=document.createElement('div');win.className='r-viewer-window';
   win.innerHTML='<div class="'+className+'"><span><i class="fas fa-file"></i> file</span><button class="r-viewer-close">×</button></div><div class="r-viewer-footer"><button data-action="openext">Open externally</button></div>';
   document.body.append(win);const header=win.firstElementChild;
   previewModule.decoratePreviewHeader(header,files[0],'zh');await wait(()=>header.querySelector('.file-preview-meta').textContent.includes('｜'));
   assert.equal(header.querySelector('.file-preview-name').textContent,path.basename(files[0]));
   assert.equal(getComputedStyle(header).backgroundColor,'rgb(245, 245, 245)');assert(header.offsetHeight<=36);
   assert.equal(win.querySelectorAll('[data-action="openext"]').length,1);assert(header.querySelector('[data-action="openext"]'));win.remove();
  }
  return {result:'PASS',singleDropAndPicker:true,previewDoesNotMutate:true,multipleReorderAndDelete:true,cliGroupPicker:true,sharedCardStyle:true,continuousScroll:true,boundedTextNodes:true,fullContentHash:true,scrollBack:true,tables:tableReports};
 }.toString()+')('+[root,source,files,scrolling,scrollHash,csv,xlsx,xls].map(x=>JSON.stringify(x)).join(',')+')');
 console.log(JSON.stringify(result));await service.closeAll();win.destroy();app.exit(0);
}catch(error){console.error(error.stack);await service?.closeAll();if(win)win.destroy();app.exit(1);}});
setTimeout(()=>app.exit(2),45000);
`;
let result;
try {
 const script=path.join(dir,'check.cjs');fs.writeFileSync(script,harness);
 const env={...process.env};delete env.ELECTRON_RUN_AS_NODE;
 result=cp.spawnSync(path.join(root,'node_modules/electron/dist/electron.exe'),[script,process.env.PRIMIGENIUS_PREVIEW_TEST_ASAR||root],{env,encoding:'utf8',windowsHide:true,timeout:50000});
 process.stdout.write(result.stdout||'');process.stderr.write(result.stderr||'');
} finally {
 if(path.dirname(path.resolve(dir))!==root || !path.basename(dir).startsWith('.tmp-upload-preview-'))throw Error('Unexpected cleanup target');
 fs.rmSync(dir,{recursive:true,force:true,maxRetries:6,retryDelay:200});
}
process.exitCode=result?.status??1;
