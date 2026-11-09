'use strict';
const {Worker} = require('worker_threads');
const fs = require('fs'), path = require('path'), os = require('os'), assert = require('assert/strict');
const iconv = require('iconv-lite'), XLSX = require('xlsx');
const root = fs.mkdtempSync(path.join(os.tmpdir(),'pg-preview-'));
const active = new Set();
function open(file,kind='table') {
    const worker = new Worker(path.resolve(__dirname,'../src/file-preview-worker.js'),{workerData:{path:file,kind,cacheDir:fs.mkdtempSync(path.join(root,'sheet-cache-'))}});
    active.add(worker);
    let counted;
    worker.on('message',result=>{if(result.event==='line-count')counted=result;});
    return {async read(page=0) {
        return new Promise((resolve,reject)=>{
            const timer=setTimeout(()=>reject(Error('preview timeout')),10000);
            const done=()=>{clearTimeout(timer);worker.removeListener('error',error);worker.removeListener('message',message);};
            const error=e=>{done();reject(e);},message=result=>{if(result.event==='line-count')return;done();resolve(result);};
            worker.once('error',error);worker.on('message',message);worker.postMessage({page});
        });
    }, async count(){
        for(let i=0;i<500;i++){if(counted)return counted;await new Promise(resolve=>setTimeout(resolve,10));}
        throw Error('Line count timeout');
    }, async close(){await worker.terminate();active.delete(worker);}};
}
(async()=>{
    for(const encoding of ['utf8','gbk','utf16le','utf16be']) {
        const file=path.join(root,encoding+'.csv');
        const data='name,value\r\n"中文,名字","line1\nline2 ""quoted"""\r\n'+Array.from({length:205},(_,i)=>`row${i},${i}\n`).join('');
        fs.writeFileSync(file,iconv.encode(data,encoding,{addBOM:encoding.startsWith('utf16')}));
        const preview=open(file),first=await preview.read();
        assert(!first.error,first.error);assert.equal(first.rows.length,100);assert.deepEqual(first.rows[0],['中文,名字','line1\nline2 "quoted"']);
        const second=await preview.read(1);assert.equal(second.rows[0][0],'row99');
        assert.deepEqual((await preview.read(0)).rows,first.rows);
        const last=await preview.read(2);assert.equal(last.rows.length,6);assert(!last.hasNext);
        assert.equal((await preview.count()).rows,206);assert.equal((await preview.count()).label,'dataRows');
        fs.appendFileSync(file,'changed');assert.match((await preview.read()).error,/File changed/);
        await preview.close();
    }
    const large=path.join(root,'large.tsv');
    fs.writeFileSync(large,'id\tvalue\n'+Array.from({length:1000},(_,i)=>`${i}\tvalue\n`).join(''));
    fs.truncateSync(large,1024*1024*1024); // sparse 1 GiB; preview must not read or scan all of it
    const preview=open(large),start=performance.now(),first=await preview.read();
    assert.equal(first.rows.length,100);assert(first.bytesRead<200000);assert.equal(first.rows[99][0],'99');
    console.log(`Sparse 1 GiB TSV first page: ${(performance.now()-start).toFixed(1)} ms, ${first.bytesRead} bytes read`);
    await preview.close();
    for(const encoding of ['utf8','gbk','utf16le','utf16be']) {
        const file=path.join(root,'text-'+encoding+'.txt'),original='中文 😀 abc\n'.repeat(40000);
        fs.writeFileSync(file,iconv.encode(original,encoding,{addBOM:encoding.startsWith('utf16')}));
        const text=open(file,'text');let combined='',page=0,result;
        do {result=await text.read(page++);assert(!result.error,result.error);if(result.hasNext){assert(result.text.endsWith('\n'));assert(!result.continuedLine);}combined+=result.text;}while(result.hasNext);
        assert.equal(combined,iconv.decode(fs.readFileSync(file),encoding));assert.equal((await text.count()).rows,40000);await text.close();
    }
    const longLine=path.join(root,'long-line.txt'),longContent='ACGT'.repeat(150000);
    fs.writeFileSync(longLine,longContent);
    const longPreview=open(longLine,'text');let longResult,joined='',longPage=0;
    do {longResult=await longPreview.read(longPage++);if(longResult.hasNext)assert(longResult.continuedLine);joined+=longResult.text;}while(longResult.hasNext);
    assert.equal(joined,longContent);await longPreview.close();
    const book=XLSX.utils.book_new();XLSX.utils.book_append_sheet(book,XLSX.utils.aoa_to_sheet([['id','value'],...Array.from({length:2500},(_,i)=>[i,'v'+i])]),'Data');
    for(const ext of ['xlsx','xls']) {
        const file=path.join(root,'test.'+ext);XLSX.writeFile(book,file);
        const excel=open(file),first=await excel.read();assert.equal(first.rows[0][0],'0');assert.equal(first.rows.length,100);
        const last=await excel.read(24);assert.equal(last.rows[99][0],'2499');if(last.hasNext)assert.equal((await excel.read(25)).rows.length,0);
        assert.equal((await excel.count()).rows,ext==='xlsx'?2500:2501);await excel.close();
    }
    // Shared strings, number/date formatting and XML escaping retain SheetJS display values.
    const formatted=XLSX.utils.book_new(),sheet=XLSX.utils.aoa_to_sheet([['name','fraction','date','flag'],['中文 & <gene>',0.125,new Date('2026-10-31T00:00:00Z'),true]]);
    sheet.B2.z='0.0%';sheet.C2.z='yyyy-mm-dd';XLSX.utils.book_append_sheet(formatted,sheet,'显示值');
    const formattedPath=path.join(root,'formatted.xlsx');XLSX.writeFile(formatted,formattedPath,{bookSST:true});
    const reference=XLSX.readFile(formattedPath),expected=XLSX.utils.sheet_to_json(reference.Sheets[reference.SheetNames[0]],{header:1,raw:false});
    const formattedPreview=open(formattedPath),formattedPage=await formattedPreview.read();assert.deepEqual(formattedPage.header,expected[0]);assert.deepEqual(formattedPage.rows,expected.slice(1));await formattedPreview.close();
    // A larger ZIP worksheet is indexed on disk and can be revisited without a full JSON worksheet in the renderer.
    const JSZip=require('jszip'),zip=new JSZip(),largeRows=30000;
    zip.file('xl/workbook.xml','<workbook xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Generic" r:id="first"/></sheets></workbook>');
    zip.file('xl/_rels/workbook.xml.rels','<Relationships><Relationship Id="first" Target="worksheets/custom.xml"/></Relationships>');
    zip.file('xl/worksheets/custom.xml','<worksheet><sheetData><row><c r="A1" t="inlineStr"><is><t>id</t></is></c></row>'+Array.from({length:largeRows},(_,i)=>`<row><c r="A${i+2}"><v>${i}</v></c></row>`).join('')+'</sheetData></worksheet>');
    const largeBook=path.join(root,'large.xlsx');fs.writeFileSync(largeBook,await zip.generateAsync({type:'nodebuffer',compression:'DEFLATE'}));
    const largeExcel=open(largeBook),largeFirst=await largeExcel.read();assert.equal(largeFirst.rows[0][0],'0');
    assert.equal((await largeExcel.read(299)).rows[99][0],'29999');assert.equal((await largeExcel.read(0)).rows[0][0],'0');assert.equal((await largeExcel.count()).rows,largeRows);await largeExcel.close();
    const wide=path.join(root,'wide.csv');
    fs.writeFileSync(wide,Array.from({length:150},(_,i)=>'col'+i).join(',')+'\n'+Array.from({length:150},()=> 'x'.repeat(2200)).join(','));
    const widePreview=open(wide),widePage=await widePreview.read();
    assert.equal(widePage.rows[0].length,150);assert.equal(widePage.rows[0][0].length,2200);assert.equal(widePage.columns,150);await widePreview.close();
    const largeFields=path.join(root,'large-fields.csv');fs.writeFileSync(largeFields,'id,value\n'+Array.from({length:20},(_,i)=>i+','+'x'.repeat(300000)+'\n').join(''));
    const fieldPreview=open(largeFields);let fieldPage=0,fieldRows=0,fieldResult;
    do{fieldResult=await fieldPreview.read(fieldPage++);assert(!fieldResult.error,fieldResult.error);assert.equal(fieldResult.startRow,fieldRows+1);assert(fieldResult.rows.length<100);for(const row of fieldResult.rows){assert.equal(row[0],String(fieldRows++));assert.equal(row[1].length,300000);}}while(fieldResult.hasNext);
    assert.equal(fieldRows,20);assert.deepEqual((await fieldPreview.read(0)).rows[0][0],'0');await fieldPreview.close();
    const malformed=path.join(root,'malformed.csv');fs.writeFileSync(malformed,'a,b\n"unterminated');
    const bad=open(malformed);assert.match((await bad.read()).error,/Unclosed/);await bad.close();
    const oversized=path.join(root,'oversized.xls');fs.writeFileSync(oversized,'');fs.truncateSync(oversized,129*1024*1024);
    const limited=open(oversized);assert.match((await limited.read()).error,/128 MB/);await limited.close();
    const {EventEmitter}=require('events');
    const owner=new EventEmitter();owner.mainFrame={};
    const notifications=new Map();owner.send=(channel,id,result)=>{assert.equal(channel,'file-preview-count');notifications.set(id,result);};
    const handlers={},event={sender:owner,senderFrame:owner.mainFrame};
    const service=require('../src/file-preview-service').registerPreviewService({handle:(name,fn)=>{handlers[name]=fn;}},()=>({webContents:owner}));
    assert.throws(()=>handlers['file-preview-open']({sender:{}},large,'table'),/application window/);
    assert.throws(()=>handlers['file-preview-open'](event,'relative.csv','table'),/Invalid/);
    const id=handlers['file-preview-open'](event,large,'table');
    assert.equal((await handlers['file-preview-page'](event,id,0)).rows.length,100);
    await handlers['file-preview-close'](event,id);
    assert.equal(service.activeCount(),0);assert.equal(owner.listenerCount('destroyed'),0);
    const canceled=handlers['file-preview-open'](event,large,'table');
    const pending=handlers['file-preview-page'](event,canceled,0);
    const rejected=assert.rejects(pending,/Preview closed/);await service.closeAll();await rejected;
    assert.equal(service.activeCount(),0);
    const cachedFile=path.join(root,'count-cache.csv');fs.writeFileSync(cachedFile,'name\na\nb\n');
    async function countThroughService(){
        const key=handlers['file-preview-open'](event,cachedFile,'table');
        await handlers['file-preview-page'](event,key,0);
        for(let i=0;i<500&&!notifications.has(key);i++)await new Promise(resolve=>setTimeout(resolve,10));
        const result=notifications.get(key);assert(result && !result.error);await handlers['file-preview-close'](event,key);return result.rows;
    }
    assert.equal(await countThroughService(),2);assert.equal(await countThroughService(),2);
    fs.appendFileSync(cachedFile,'c\n');assert.equal(await countThroughService(),3);
    const cache=require('../src/preview-cache'),owned=cache.create(),parent=path.dirname(owned);
    assert.equal(JSON.parse(fs.readFileSync(path.join(owned,'owner.json'))).pid,process.pid);
    // Only this test's cache is examined and removed; other process caches remain untouched.
    fs.rmSync(owned,{recursive:true,force:true});
    assert(!fs.readdirSync(parent).some(name=>name.startsWith(process.pid+'-')),'Closed previews release their cache directories');
    for(const content of ['', 'a', 'a\r\nb\rc\n', '\n\n', 'x'.repeat(1024*1024-1)+'\r\nb']) {
        const file=path.join(root,'physical-lines.txt');fs.writeFileSync(file,content);
        const preview=open(file,'text');await preview.read();
        const expected=(content.match(/\r\n|\r|\n/g)||[]).length+(content && !/[\r\n]$/.test(content)?1:0);
        assert.equal((await preview.count()).rows,expected);await preview.close();
    }
    console.log('PASS: paging/backtracking, CSV multiline/escaped quotes, UTF-8/GBK/UTF-16 boundaries, file changes, bounded first read, XLS/XLSX limits.');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
    await Promise.all([...active].map(worker=>worker.terminate()));
    // Only this explicitly created disposable fixture directory is removed.
    if(path.dirname(root)!==os.tmpdir() || !path.basename(root).startsWith('pg-preview-'))throw Error('Unexpected cleanup target');
    fs.rmSync(root,{recursive:true,force:true});
});
