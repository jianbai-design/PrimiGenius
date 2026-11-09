'use strict';
// Real Electron rendering, using disposable DOCX files and no network services.
const fs = require('fs'), path = require('path'), cp = require('child_process');
const root = path.resolve(__dirname, '..');
const directory = fs.mkdtempSync(path.join(root, '.tmp-docx-preview-'));
const harness = `
const {app,BrowserWindow,session}=require('electron');
const fs=require('fs'),path=require('path'),assert=require('assert/strict'),crypto=require('crypto');
const root=process.argv[2];
app.setPath('userData',path.join(__dirname,'profile'));
for(const flag of ['no-sandbox','in-process-gpu','disable-gpu'])app.commandLine.appendSwitch(flag);
app.whenReady().then(async()=>{let win;try{
 const JSZip=require(path.join(root,'node_modules/jszip'));
 const zip=new JSZip();
 zip.file('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>');
 zip.file('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>');
 zip.file('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:rPr><w:b/><w:color w:val="C62828"/></w:rPr><w:t>中文 DOCX 文档</w:t></w:r></w:p><w:tbl><w:tblPr><w:tblBorders><w:top w:val="single" w:sz="4"/><w:bottom w:val="single" w:sz="4"/></w:tblBorders></w:tblPr><w:tblGrid><w:gridCol w:w="3000"/><w:gridCol w:w="3000"/></w:tblGrid><w:tr><w:tc><w:p><w:r><w:t>基因</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>GeneA</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p><w:r><w:t>原始文件保持不变</w:t></w:r></w:p><w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="720" w:right="720" w:bottom="720" w:left="720"/></w:sectPr></w:body></w:document>');
 const file=path.join(__dirname,'example.docx');fs.writeFileSync(file,await zip.generateAsync({type:'nodebuffer'}));
 const before=crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
 const invalid=path.join(__dirname,'invalid.docx');fs.writeFileSync(invalid,'not a ZIP');
 const large=path.join(__dirname,'large.docx');fs.writeFileSync(large,'');fs.truncateSync(large,32*1024*1024+1);
 const fixture=path.join(__dirname,'fixture.html');fs.writeFileSync(fixture,'<!doctype html><meta charset="utf-8"><body style="margin:0"></body>');
 let network=0;session.defaultSession.webRequest.onBeforeRequest({urls:['http://*/*','https://*/*']},(_details,callback)=>{network++;callback({cancel:true});});
 win=new BrowserWindow({show:false,width:1000,height:800,webPreferences:{nodeIntegration:true,contextIsolation:false,sandbox:false,backgroundThrottling:false}});await win.loadFile(fixture);
 await win.webContents.executeJavaScript('globalThis.mountPreview=require('+JSON.stringify(path.join(root,'src/file-preview'))+').mountFilePreview;globalThis.previewContainer=document.createElement("div");previewContainer.style.cssText="width:100vw;height:100vh";document.body.append(previewContainer);globalThis.stopPreview=mountPreview(previewContainer,'+JSON.stringify(file)+',{kind:"text",lang:"zh",showExternalOpen:false});true');
 const wait=async expression=>{for(let i=0;i<300;i++){if(await win.webContents.executeJavaScript(expression))return;await new Promise(r=>setTimeout(r,20));}throw Error('DOCX condition timed out: '+await win.webContents.executeJavaScript('document.body.innerText'));};
 await wait('!!previewContainer.querySelector("iframe[data-rendered=true]")');
 assert.equal(await win.webContents.executeJavaScript('previewContainer.querySelector("iframe").getAttribute("sandbox")'),'allow-scripts');
 win.webContents.debugger.attach('1.3');const send=(method,params={})=>win.webContents.debugger.sendCommand(method,params);
 const {frameTree}=await send('Page.getFrameTree');const child=frameTree.childFrames.find(item=>item.frame.url==='about:srcdoc');assert(child);
 const {executionContextId}=await send('Page.createIsolatedWorld',{frameId:child.frame.id,worldName:'docx-check'});
 const {result}=await send('Runtime.evaluate',{contextId:executionContextId,expression:'JSON.stringify({text:document.body.innerText,tables:document.querySelectorAll("table").length,node:typeof require,styled:[...document.querySelectorAll("span")].some(e=>getComputedStyle(e).color==="rgb(198, 40, 40)")})',returnByValue:true});
 const rendered=JSON.parse(result.value);assert(rendered.text.includes('中文 DOCX 文档'));assert(rendered.text.includes('GeneA'));assert.equal(rendered.tables,1);assert.equal(rendered.node,'undefined');assert(rendered.styled);
 await win.webContents.executeJavaScript('stopPreview();assertUnused=previewContainer.querySelector("iframe")===null');assert(await win.webContents.executeJavaScript('assertUnused'));
 for(const [target,expected] of [[invalid,'无法预览'],[large,'32 MB']]){
  await win.webContents.executeJavaScript('stopPreview=mountPreview(previewContainer,'+JSON.stringify(target)+',{kind:"text",lang:"zh",showExternalOpen:false});true');
  await wait('previewContainer.innerText.includes('+JSON.stringify(expected)+')');
  assert.equal(await win.webContents.executeJavaScript('previewContainer.querySelectorAll("iframe").length'),0);
 }
 for(let i=0;i<3;i++){
  await win.webContents.executeJavaScript('stopPreview=mountPreview(previewContainer,'+JSON.stringify(file)+',{lang:"en",showExternalOpen:'+(i===0)+'});true');await wait('!!previewContainer.querySelector("iframe[data-rendered=true]")');
  if(i===0){
   assert(await win.webContents.executeJavaScript('previewContainer.querySelector(".r-viewer-footer button")!==null'));
   assert(await win.webContents.executeJavaScript('previewContainer.lastElementChild.getBoundingClientRect().top>=previewContainer.querySelector("iframe").getBoundingClientRect().bottom-1'),'DOCX actions stay below the document');
  }else assert(await win.webContents.executeJavaScript('previewContainer.lastElementChild.hidden'),'No empty DOCX toolbar');
  await win.webContents.executeJavaScript('stopPreview()');
 }
 assert.equal(network,0);assert.equal(crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex'),before);
 console.log('PASS: actual offline DOCX rendering (Chinese, styled text, table); shared preview dispatch, opaque sandbox/no Node, invalid and oversized files, repeat open/close, unchanged original.');win.destroy();app.exit(0);
}catch(error){console.error(error.stack);if(win)win.destroy();app.exit(1)}});setTimeout(()=>app.exit(2),30000);
`;
let result;
try {
    const file = path.join(directory, 'check.cjs'); fs.writeFileSync(file, harness);
    const env = {...process.env}; delete env.ELECTRON_RUN_AS_NODE;
    result = cp.spawnSync(path.join(root, 'node_modules/electron/dist/electron.exe'), [file, root],
        {env, encoding:'utf8', windowsHide:true, timeout:40000});
    process.stdout.write(result.stdout || ''); process.stderr.write(result.stderr || '');
} finally {
    const target = path.resolve(directory);
    if (path.dirname(target) !== root || !path.basename(target).startsWith('.tmp-docx-preview-')) throw Error('Unsafe cleanup target');
    fs.rmSync(target, {recursive:true, force:true, maxRetries:6, retryDelay:200});
}
process.exitCode = result?.status ?? 1;
