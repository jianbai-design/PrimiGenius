'use strict';
// Stream XLSX XML into a disposable, indexed cache. Never extract into source directories.
const fs=require('fs'),path=require('path');
const yauzl=require('yauzl'),{SaxesParser}=require('saxes');
const LIMIT=512*1024*1024;
class WorkbookPreview {
    constructor(file,cacheDir){
        this.externalCache=!!cacheDir;
        this.directory=cacheDir || require('./preview-cache').create();
        this.offsets=[];this.lengths=[];this.starts=[];this.header=[];this.rows=0;this.bytes=0;this.done=false;this.waiters=[];
        this.ready=this.build(file).catch(error=>{this.error=error;this.done=true;this.notify();});
    }
    notify(){for(const wake of this.waiters.splice(0))wake();}
    async xml(name,onOpen,onText,onClose){
        const entry=this.entries.get(name);if(!entry)return;
        if(entry.uncompressedSize>2*1024*1024*1024)throw Error('Worksheet XML exceeds the preview budget');
        const stream=await new Promise((resolve,reject)=>this.zip.openReadStream(entry,(error,result)=>error?reject(error):resolve(result)));
        const parser=new SaxesParser(),decoder=new TextDecoder();
        parser.on('opentag',node=>onOpen?.(node.name.split(':').pop(),node.attributes));
        parser.on('text',text=>onText?.(text));parser.on('cdata',text=>onText?.(text));
        parser.on('closetag',node=>onClose?.(node.name.split(':').pop()));
        parser.on('error',error=>{throw error;});
        for await(const chunk of stream){parser.write(decoder.decode(chunk,{stream:true}));await new Promise(resolve=>setTimeout(resolve,1));}
        parser.write(decoder.decode());parser.close();
    }
    async build(file){
        this.zip=await new Promise((resolve,reject)=>yauzl.open(file,{lazyEntries:true,autoClose:false},(error,zip)=>error?reject(error):resolve(zip)));
        try{
            this.entries=new Map();await new Promise((resolve,reject)=>{
                this.zip.on('error',reject);this.zip.on('entry',entry=>{
                    if(this.entries.size>=10000){reject(Error('Too many entries in workbook'));return;}
                    this.entries.set(entry.fileName,entry);this.zip.readEntry();
                });this.zip.once('end',resolve);this.zip.readEntry();
            });
            let relation,date1904=false;const relations=new Map();
            await this.xml('xl/workbook.xml',(name,a)=>{if(name==='sheet'&&!relation){relation=a['r:id'];this.sheetName=a.name;}if(name==='workbookPr')date1904=['1','true'].includes(a.date1904);});
            await this.xml('xl/_rels/workbook.xml.rels',(name,a)=>{if(name==='Relationship'&&a.TargetMode!=='External')relations.set(a.Id,a.Target);});
            const target=relations.get(relation)||'worksheets/sheet1.xml';
            const sheet=target.startsWith('/')?target.slice(1):path.posix.normalize('xl/'+target);
            if(!this.entries.has(sheet))throw Error('No readable worksheet');
            const sharedOffsets=[],sharedLengths=[],sharedFile=path.join(this.directory,'strings.bin');let sharedBytes=0,value='',inText=false;
            const shared=fs.openSync(sharedFile,'w+');
            this.handles=[shared];
            await this.xml('xl/sharedStrings.xml',name=>{if(name==='si')value='';if(name==='t')inText=true;},text=>{if(inText){value+=text;if(value.length>2*1024*1024)throw Error('A cell exceeds the preview budget');}},name=>{
                if(name==='t')inText=false;if(name==='si'){
                    const buffer=Buffer.from(value);if(sharedBytes+buffer.length>LIMIT)throw Error('Workbook string cache exceeds 512 MB');
                    sharedOffsets.push(sharedBytes);sharedLengths.push(buffer.length);fs.writeSync(shared,buffer);sharedBytes+=buffer.length;
                }
            });
            const formats=new Map(),styles=[];let inCellXfs=false;
            await this.xml('xl/styles.xml',(name,a)=>{if(name==='numFmt')formats.set(Number(a.numFmtId),a.formatCode);if(name==='cellXfs')inCellXfs=true;if(name==='xf'&&inCellXfs)styles.push(Number(a.numFmtId||0));},null,name=>{if(name==='cellXfs')inCellXfs=false;});
            const SSF=require('xlsx').SSF,lru=new Map();
            const string=id=>{
                if(lru.has(id))return lru.get(id);
                if(!Number.isSafeInteger(id)||sharedOffsets[id]===undefined)return '';
                const buffer=Buffer.alloc(sharedLengths[id]);fs.readSync(shared,buffer,0,buffer.length,sharedOffsets[id]);
                const value=buffer.toString('utf8');lru.set(id,value);if(lru.size>256)lru.delete(lru.keys().next().value);return value;
            };
            const pages=fs.openSync(path.join(this.directory,'pages.bin'),'w+');this.handles.push(pages);this.pages=pages;
            let row=[],page=[],cell,t='',capture=false,first=true,characters=0;
            const flush=()=>{
                if(!page.length)return;
                const buffer=Buffer.from(JSON.stringify(page));if(sharedBytes+this.bytes+buffer.length>LIMIT)throw Error('Workbook preview cache exceeds 512 MB');
                this.starts.push(this.rows-page.length+1);this.offsets.push(this.bytes);this.lengths.push(buffer.length);fs.writeSync(pages,buffer);this.bytes+=buffer.length;page=[];characters=0;this.notify();
            };
            await this.xml(sheet,(name,a)=>{
                if(name==='row')row=[];
                if(name==='c'){cell={type:a.t,style:Number(a.s||0),ref:a.r};t='';}
                if(name==='v'||name==='t')capture=true;
            },text=>{if(capture){t+=text;if(t.length>2*1024*1024)throw Error('A cell exceeds the preview budget');}},name=>{
                if(name==='v'||name==='t')capture=false;
                if(name==='c'&&cell){
                    let col=0;for(const char of (cell.ref?.match(/^[A-Z]+/)?.[0]||'A'))col=col*26+char.charCodeAt(0)-64;col--;
                    if(col>=16384)throw Error('Invalid worksheet column');
                    let value=t;
                    if(cell.type==='s')value=string(Number(t));
                    else if(cell.type==='b')value=t==='1'?'TRUE':'FALSE';
                    else if(!cell.type||cell.type==='n'){if(t!==''){const number=Number(t),format=styles[cell.style]||0;try{value=SSF.format(formats.get(format)||format,number,{date1904});}catch(_){value=t;}}}
                    row[col]=value;characters+=String(value).length;cell=null;
                }
                if(name==='row'&&row.some(value=>value!==''&&value!=null)){
                    if(first){this.header=row;first=false;characters=0;}
                    else{page.push(row);this.rows++;if(page.length===100||characters>=2*1024*1024)flush();}
                }
            });
            flush();this.done=true;this.notify();
        }finally{this.zip.close();}
    }
    async page(index){
        while(this.offsets[index]===undefined&&!this.done)await new Promise(resolve=>this.waiters.push(resolve));
        if(this.error&&this.offsets[index]===undefined)throw this.error;
        if(this.offsets[index]===undefined)return {header:this.header,rows:[],page:index,hasNext:false,sheetName:this.sheetName};
        const buffer=Buffer.alloc(this.lengths[index]);fs.readSync(this.pages,buffer,0,buffer.length,this.offsets[index]);
        const rows=JSON.parse(buffer.toString('utf8'));
        return {header:this.header,rows,page:index,startRow:this.starts[index],hasNext:!this.done||index+1<this.offsets.length,sheetName:this.sheetName,note:this.error?.message||''};
    }
    close(){for(const fd of this.handles||[])try{fs.closeSync(fd);}catch(_){};this.zip?.close();if(!this.externalCache)fs.rmSync(this.directory,{recursive:true,force:true});}
}
module.exports={WorkbookPreview};
