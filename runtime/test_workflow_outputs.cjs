'use strict';
const fs=require('fs'),path=require('path'),cp=require('child_process');
const root=path.resolve(__dirname,'..'),temp=fs.mkdtempSync(path.join(root,'.tmp-workflow-outputs-'));
const harness=String.raw`
const {app,BrowserWindow}=require('electron'),fs=require('fs'),path=require('path');
app.setPath('userData',path.join(__dirname,'profile'));
app.commandLine.appendSwitch('no-sandbox');app.commandLine.appendSwitch('disable-gpu');
app.whenReady().then(async()=>{let win;try{
 const root=process.argv[2],output=path.join(__dirname,'outputs');fs.mkdirSync(output);
 for(const name of ['first.tsv','second.tsv','kept.fa'])fs.writeFileSync(path.join(output,name),'original result\n');
 win=new BrowserWindow({show:false,webPreferences:{nodeIntegration:true,contextIsolation:false,sandbox:false}});
 await win.loadURL('data:text/html,<body></body>');
 const result=await win.webContents.executeJavaScript('('+async function(root,output){
  const assert=require('assert/strict'),fs=require('fs'),path=require('path');
  const source=fs.readFileSync(path.join(root,'src/renderer.js'),'utf8');
  const execute=(code,scope,async=false)=>{const Constructor=async?Object.getPrototypeOf(async function(){}).constructor:Function;return new Constructor(...Object.keys(scope),code)(...Object.values(scope));};
  const getPluginText=value=>typeof value==='object'?value.zh:value;
  const initialStart=source.indexOf('            const buildOutputRow =');
  const initial=source.slice(initialStart,source.indexOf('            if (expectedOutputs.length',initialStart));
  const build=execute(initial+';return buildOutputRow;',{getPluginText,currentLang:'zh',nodeId:'upstream'});
  const nodeEl=document.createElement('div');nodeEl.className='wf-node';nodeEl.dataset.id='upstream';
  const list=document.createElement('div');list.className='wf-output-list';nodeEl.append(list);document.body.append(nodeEl);
  const expected=[{id:'table',type:'tsv',label:'Tables'},{id:'sequence',type:'fa',label:'Sequences'}];nodeEl._expectedOutputs=expected;
  expected.forEach((entry,index)=>list.append(build(entry,index,entry.id)));
  assert.equal(list.querySelectorAll('input[type=checkbox]').length,0);
  assert.equal(list.querySelectorAll('.wf-port.out').length,2);
  const originalKeys=[...list.querySelectorAll('.wf-port.out')].map(port=>port.dataset.key);
  const target=id=>{const el=document.createElement('div');el.className='wf-node';el.dataset.id=id;el.dataset.pluginId='receiver';document.body.append(el);return el;};
  const connected=target('connected'),unconnected=target('unconnected');
  const connections=[{from:{nodeId:'upstream',port:0,portKey:originalKeys[0]},to:{nodeId:'connected',portKey:'connected::input::in::0'}}];
  const files=fs.readdirSync(output).map(name=>path.join(output,name));
  const before=files.map(file=>fs.readFileSync(file,'utf8'));
  const isSystemLogFile=()=>false;
  const matchStart=source.indexOf('    function fileMatchesParam(');
  const helpers=execute(source.slice(matchStart,source.indexOf('    // Auto-select files for a node parameter',matchStart))+';return {fileMatchesParam,matchFileToOutputEntry};',{isSystemLogFile});
  const previews=[],scope={nodeEl,outDir:output,path,currentLang:'zh',connections,getPluginText,isSystemLogFile,...helpers,
   listDir:async()=>files,showFilePreview:file=>previews.push(file),beginPendingConnection:()=>{},log:()=>{},updateConnections:()=>{},
   allPlugins:[{id:'receiver',parameters:[{id:'input',type:'file',extensions:['tsv'],allow_multiple:true}]}],
   isFileGroupingEnabled:()=>false};
  const rebuildStart=source.indexOf('                // Scan output directory and populate output files section');
  const rebuild=source.slice(rebuildStart,source.indexOf('                // Force a synchronous layout reflow',rebuildStart));
  await execute(rebuild,scope,true);
  assert.equal(list.querySelectorAll('input[type=checkbox]').length,0);
  assert.deepEqual([...list.querySelectorAll('.wf-port.out')].map(port=>port.dataset.key),originalKeys,'Output rebuild preserves existing connection keys');
  assert.equal(list.querySelectorAll('.wf-output-filechip').length,3,'All produced files remain visible');
  list.querySelector('.wf-output-eye').click();assert.equal(previews.length,1,'File preview stays available');
  const passStart=source.indexOf('                // Auto-populate downstream nodes: directly assign files through connections');
  const pass=source.slice(passStart,source.indexOf('                // Force reflow and update connections after downstream file passing',passStart));
  await execute(pass,scope,true);
  assert.deepEqual(connected._fileLists.input,files.filter(file=>file.endsWith('.tsv')));
  assert.equal(unconnected._fileLists,undefined,'Unconnected nodes receive nothing');
  assert(!connected._fileLists.input.some(file=>file.endsWith('.fa')),'Unconnected outputs are not passed');
  connections.length=0;connected._fileLists={};connected._rawFilePool={};
  await execute(pass,scope,true);assert.deepEqual(connected._fileLists,{},'Removing the connection prevents subsequent passing');
  assert.deepEqual(files.map(file=>fs.readFileSync(file,'utf8')),before,'Output selection does not delete or modify generated files');
  return {result:'PASS',noOutputCheckboxes:true,preservedPortKeys:true,connectedOutputsOnly:true,disconnectStopsPassing:true,previewAvailable:true,unchangedFiles:true};
 }.toString()+')('+JSON.stringify(root)+','+JSON.stringify(output)+')');
 console.log(JSON.stringify(result));win.destroy();app.exit(0);
}catch(error){console.error(error.stack);win?.destroy();app.exit(1);}});
setTimeout(()=>app.exit(2),20000);
`;
let result;
try{
 const script=path.join(temp,'check.cjs');fs.writeFileSync(script,harness);
 const env={...process.env};delete env.ELECTRON_RUN_AS_NODE;
 result=cp.spawnSync(path.join(root,'node_modules/electron/dist/electron.exe'),[script,root],{env,encoding:'utf8',windowsHide:true,timeout:25000});
 process.stdout.write(result.stdout||'');process.stderr.write(result.stderr||'');
}finally{
 if(path.dirname(path.resolve(temp))!==root||!path.basename(temp).startsWith('.tmp-workflow-outputs-'))throw Error('Unexpected cleanup target');
 fs.rmSync(temp,{recursive:true,force:true,maxRetries:6,retryDelay:200});
}
process.exitCode=result?.status??1;
