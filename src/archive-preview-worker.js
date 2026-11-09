'use strict';
// Read-only archive access. No extraction, temporary files, or plugin processes.
const {parentPort,workerData}=require('worker_threads');
const fs=require('fs'),path=require('path'),zlib=require('zlib'),iconv=require('iconv-lite');
const PAGE_BYTES=256*1024,LIST_ROWS=100,MAX_ENTRIES=100000,BLOB_BYTES=16*1024*1024;
const format=/\.zip$/i.test(workerData.path)?'zip':/\.(tar|tgz)$|\.tar\.gz$/i.test(workerData.path)?'tar':'gzip';
const snapshot=fs.statSync(workerData.path);
if(!snapshot.isFile())throw Error('Not a regular file / 不是普通文件');
function metadata(id,name,size,type='file',encrypted=false){
    if(!Number.isSafeInteger(size)||size<0||name.length>65536)throw Error('Invalid archive member / 无效的压缩包成员');
    return {id,name,size,type,encrypted};
}
async function archive(){
    if(format==='zip'){
        const zip=await require('yauzl').openPromise(workerData.path,{autoClose:false,lazyEntries:true});
        const entries=zip.eachEntry();let id=0;
        return {async next(){
            const item=await entries.next();if(item.done)return item;
            const entry=item.value,mode=(entry.externalFileAttributes>>>16)&0xf000;
            const info=metadata(id++,entry.fileName,entry.uncompressedSize,/\/$/.test(entry.fileName)?'directory':mode===0xa000?'link':'file',entry.isEncrypted());
            return {value:{info,open:()=>zip.openReadStreamPromise(entry)}};
        },close(){zip.close();}};
    }
    if(format==='gzip'){
        let sent=false;return {async next(){if(sent)return {done:true};sent=true;
            return {value:{info:metadata(0,path.basename(workerData.path).replace(/\.gz$/i,''),0),open:async()=>{
                const input=fs.createReadStream(workerData.path),output=zlib.createGunzip();
                input.on('error',error=>output.destroy(error));output.once('close',()=>input.destroy());input.pipe(output);return output;
            }}};
        },close(){}};
    }
    const parser=new (require('tar').Parse)({strict:true,maxMetaEntrySize:1024*1024});
    const input=fs.createReadStream(workerData.path,{highWaterMark:64*1024});
    const gunzip=/\.(tgz|gz)$/i.test(workerData.path)?zlib.createGunzip():null;
    let queued,active,pending,ended=false,error,id=0,closed=false;
    const fail=err=>{error=err;pending?.reject(err);pending=null;};
    input.on('error',fail);parser.on('error',fail);gunzip?.on('error',fail);
    parser.on('entry',stream=>{
        stream.on('error',fail);
        const end=new Promise(resolve=>{stream.once('end',resolve);stream.once('close',resolve);});
        const type=['File','OldFile','ContiguousFile'].includes(stream.type)?'file':stream.type==='Directory'?'directory':'link';
        const item={info:metadata(id++,stream.path,stream.size,type),open:async()=>stream,stream,end};
        if(pending){const waiter=pending;pending=null;active=item;waiter.resolve({value:item});}else queued=item;
    });
    parser.on('end',()=>{ended=true;pending?.resolve({done:true});pending=null;});
    if(gunzip)input.pipe(gunzip).pipe(parser);else input.pipe(parser);
    return {async next(){
        if(active){active.stream.resume();await active.end;active=null;}
        if(error)throw error;
        if(queued){active=queued;queued=null;return {value:active};}
        if(ended)return {done:true};
        return new Promise((resolve,reject)=>{pending={resolve,reject};});
    },close(){if(closed)return;closed=true;input.destroy();gunzip?.destroy();parser.abort(Error('Preview closed'));active?.stream.destroy();}};
}
let listing,listPage=0,listDone=false,listBytes=0,listCount=0;
const listed=new Map();
async function list(page){
    if(listed.has(page))return listed.get(page);
    if(page!==listPage)throw Error('Member list not indexed / 请顺序读取成员列表');
    if(!listing)listing=await archive();
    const entries=[],started=performance.now();
    while(entries.length<LIST_ROWS && listCount<MAX_ENTRIES && listBytes<8*1024*1024){
        const item=await listing.next();if(item.done){listDone=true;break;}entries.push(item.value.info);listCount++;listBytes+=item.value.info.name.length*2+128;
        // Show a large TAR member's header before draining its body for the next header.
        if(format==='tar'&&(item.value.info.size>=PAGE_BYTES||performance.now()-started>40))break;
    }
    const limited=listCount>=MAX_ENTRIES||listBytes>=8*1024*1024;
    if(listDone||limited){listing.close();listing=null;}
    const result={entries,hasNext:!listDone&&!limited,limited,page};listed.set(listPage++,result);return result;
}
async function member(id){
    if(!Number.isSafeInteger(id)||id<0||id>=MAX_ENTRIES)throw Error('Invalid member');
    const source=await archive();
    try{
        for(let index=0;index<=id;index++){
            const item=await source.next();if(item.done)break;
            if(index!==id)continue;
            if(item.value.info.type!=='file')throw Error('Directories and links are not opened / 不打开目录或链接');
            if(item.value.info.encrypted)throw Error('Encrypted member: use your archive application / 加密成员请使用压缩软件打开');
            const stream=await item.value.open();stream.on('error',()=>{});
            return {stream,info:item.value.info,close(){stream.destroy();source.close();}};
        }
        throw Error('Member not found / 未找到成员');
    }catch(error){source.close();throw error;}
}
let textSource,iterator,decoder,pendingText='',ended=false,textPage=0,startLine=1,selected,previousCR=false;
const textCache=new Map();
async function reset(id){
    textSource?.close();textSource=await member(id);selected=id;iterator=textSource.stream[Symbol.asyncIterator]();
    decoder=null;pendingText='';ended=false;textPage=0;startLine=1;previousCR=false;textCache.clear();
}
async function fill(){
    let item=await iterator.next();
    if(item.done){ended=true;pendingText+=decoder?.end()||'';textSource.close();return;}
    if(!decoder){
        const chunks=[item.value];let size=item.value.length;
        while(size<8192){const next=await iterator.next();if(next.done){ended=true;break;}chunks.push(next.value);size+=next.value.length;}
        const sample=Buffer.concat(chunks,size);item={value:sample};
        let encoding='utf8';
        if(sample[0]===255&&sample[1]===254)encoding='utf16le';
        else if(sample[0]===254&&sample[1]===255)encoding='utf16be';
        else{
            if(sample.includes(0))throw Error('Binary member: text preview unavailable / 二进制成员无法按文本预览');
            try{new TextDecoder('utf-8',{fatal:true}).decode(sample,{stream:true});}catch(_){encoding='gbk';}
            if([...sample.subarray(0,8192)].filter(code=>code<9||(code>13&&code<32)).length>80)throw Error('Binary member / 二进制成员');
        }
        decoder=iconv.getDecoder(encoding);
    }
    pendingText+=decoder.write(item.value);
    if(ended){pendingText+=decoder.end();textSource.close();}
}
async function text(id,page){
    const key=id+':'+page;
    if(textCache.has(key))return textCache.get(key);
    // ponytail: keep eight recent chunks in RAM; older backward reads restart decompression instead of writing a disk cache.
    if(selected!==id||page<textPage)await reset(id);
    while(textPage<=page){
        while(!ended&&pendingText.length<=PAGE_BYTES)await fill();
        let length=Math.min(PAGE_BYTES,pendingText.length);
        if(!ended){const newline=pendingText.lastIndexOf('\n',length-1);if(newline>=0)length=newline+1;}
        if(length&&length<pendingText.length&&/[\uD800-\uDBFF]/.test(pendingText[length-1]))length--;
        const value=pendingText.slice(0,length);pendingText=pendingText.slice(length);
        const result={page:textPage,text:value,startLine,hasNext:!ended||!!pendingText,note:''};
        startLine+=(value.match(/\r\n|\r|\n/g)||[]).length-(previousCR&&value[0]==='\n'?1:0);previousCR=value.endsWith('\r');
        textCache.set(id+':'+textPage,result);while(textCache.size>8)textCache.delete(textCache.keys().next().value);textPage++;
        if(!result.hasNext&&textPage<=page)throw Error('End of member / 已到成员末尾');
    }
    return textCache.get(key);
}
async function blob(id){
    const source=await member(id),chunks=[];let size=0;
    try{
        if(source.info.size>BLOB_BYTES)throw Error('Image exceeds the 16 MB in-memory preview budget / 图片超过 16 MB 内存预览预算');
        for await(const chunk of source.stream){size+=chunk.length;if(size>BLOB_BYTES)throw Error('Member exceeds 16 MB / 成员超过 16 MB');chunks.push(chunk);}
        return {bytes:Buffer.concat(chunks,size)};
    }finally{source.close();}
}
let busy=false;
parentPort.on('message',async({page:request})=>{
    if(busy){parentPort.postMessage({error:'Preview busy'});return;}busy=true;
    try{
        const stat=fs.statSync(workerData.path);
        if(stat.size!==snapshot.size||stat.mtimeMs!==snapshot.mtimeMs)throw Error('File changed; reopen preview / 文件已变化，请重新打开');
        const {action,page=0,entry=0}=request||{};
        if(!Number.isSafeInteger(page)||page<0)throw Error('Invalid page');
        let result;
        if(action==='list')result=await list(page);
        else if(action==='text')result=await text(entry,page);
        else if(action==='blob')result=await blob(entry);
        else throw Error('Invalid archive request');
        parentPort.postMessage(result);
    }catch(error){textSource?.close();textSource=null;selected=undefined;parentPort.postMessage({error:error.message});}
    finally{busy=false;}
});
