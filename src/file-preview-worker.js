'use strict';
const {parentPort, workerData} = require('worker_threads');
const fs = require('fs');
const path = require('path');
const iconv = require('iconv-lite');
const ROWS = 100, TEXT_BYTES = 256 * 1024;
let fd, initial, encoding, width = 1, bom = 0, header, excel;
const offsets = [];
const lineOffsets = [1];
const rowOffsets = [1], PAGE_CHARS=2*1024*1024;
let bytesRead = 0;
let counting = false;
let streamedBook;
function startLineCount() {
    if(counting)return;counting=true;
    const emit=(rows,label)=>parentPort.postMessage({event:'line-count',rows,label,size:initial.size,mtimeMs:initial.mtimeMs,kind:workerData.kind});
    const cached=workerData.cachedCount;
    if(cached && cached.size===initial.size && cached.mtimeMs===initial.mtimeMs && cached.kind===workerData.kind){emit(cached.rows,cached.label);return;}
    if(excel){emit(excel.totalRows,'sheetRows');return;}
    const logical=workerData.kind==='table',buffer=Buffer.alloc(1024*1024);
    let position=bom,rows=0,quoted=false,nonblank=false,previousCR=false,lastNewline=false;
    function scan() {
        try {
            const count=fs.readSync(fd,buffer,0,buffer.length,position);position+=count;
            if(!logical && count){
                if(width===2 && count%2)throw Error('Incomplete UTF-16 character');
                const text=width===1?buffer.toString('latin1',0,count):iconv.decode(buffer.subarray(0,count),encoding);
                rows+=(text.match(/\r\n|\r|\n/g)||[]).length;
                if(previousCR && text[0]==='\n')rows--;
                previousCR=text.endsWith('\r');lastNewline=/[\r\n]$/.test(text);
            }
            for(let i=0;logical && i<count;i+=width){
                const code=width===1?buffer[i]:encoding==='utf16le'?buffer.readUInt16LE(i):buffer.readUInt16BE(i);
                if(logical && code===34)quoted=!quoted;
                const newline=(code===10 || code===13) && (!logical || !quoted);
                if(newline){
                    if(!(code===10 && previousCR)){if(!logical || nonblank)rows++;nonblank=false;}
                    previousCR=code===13;lastNewline=true;
                }else{
                    previousCR=false;lastNewline=false;
                    if(code!==32 && code!==9)nonblank=true;
                }
            }
            if(count && position<initial.size){setTimeout(scan,2);return;}
            const current=fs.statSync(workerData.path);
            if(current.size!==initial.size || current.mtimeMs!==initial.mtimeMs)throw Error('File changed');
            if(logical && quoted)throw Error('Unclosed quoted field');
            if(logical){if(nonblank)rows++;rows=Math.max(0,rows-1);}
            else if(initial.size>bom && !lastNewline)rows++;
            emit(rows,logical?'dataRows':'lines');
        }catch(error){parentPort.postMessage({event:'line-count',error:error.message});}
    }
    setTimeout(scan,2);
}
function read(buffer, position) {
    const count = fs.readSync(fd, buffer, 0, buffer.length, position);
    bytesRead += count;
    return count;
}
function init() {
    fd = fs.openSync(workerData.path, 'r');
    initial = fs.fstatSync(fd);
    const sample = Buffer.alloc(Math.min(8192, initial.size));
    read(sample, 0);
    if (sample[0] === 255 && sample[1] === 254) { encoding = 'utf16le'; width = 2; bom = 2; }
    else if (sample[0] === 254 && sample[1] === 255) { encoding = 'utf16be'; width = 2; bom = 2; }
    else {
        try { new TextDecoder('utf-8', {fatal:true}).decode(sample, {stream:true}); encoding = 'utf8'; }
        catch (_) { encoding = 'gbk'; }
        if (sample[0] === 239 && sample[1] === 187 && sample[2] === 191) bom = 3;
    }
    offsets[0] = bom;
}
// Fixed-size byte windows, with offsets retained only for pages actually visited.
function reader(position) {
    const buffer = Buffer.alloc(64 * 1024);
    let index = 0, count = 0;
    function byte() {
        if (index === count) { count = read(buffer, position); index = 0; if (!count) return -1; }
        position++;
        return buffer[index++];
    }
    return { get position() { return position; }, record() {
        const bytes = [];
        let quoted = false;
        while (true) {
            const a = byte();
            if (a < 0) {
                if (quoted) throw Error('Unclosed quoted field / 引号字段未闭合');
                return bytes.length ? iconv.decode(Buffer.from(bytes), encoding).replace(/\r?\n$/, '') : null;
            }
            bytes.push(a);
            let code = a;
            if (width === 2) {
                const b = byte();
                if (b < 0) throw Error('Incomplete UTF-16 character');
                bytes.push(b); code = encoding === 'utf16le' ? a + b * 256 : a * 256 + b;
            }
            if (code === 34) quoted = !quoted;
            if (code === 10 && !quoted) return iconv.decode(Buffer.from(bytes), encoding).replace(/\r?\n$/, '');
            if (bytes.length > 2 * 1024 * 1024) throw Error('A record exceeds the 2 MB preview limit. Please open the original file. / 单条记录超过 2 MB，请打开原文件。');
        }
    }};
}
function cells(record, delimiter) {
    const values = [];
    let value = '', quoted = false;
    for (let i = 0; i < record.length; i++) {
        const char = record[i];
        if (char === '"') {
            if (quoted && record[i + 1] === '"') { value += '"'; i++; }
            else quoted = !quoted;
        } else if (char === delimiter && !quoted) {
            values.push(value);
            if(values.length>=16384)throw Error('A record exceeds 16,384 columns; open externally. / 单条记录超过 16,384 列，请使用系统应用打开。');
            value = '';
        } else value += char;
    }
    values.push(value);
    return values;
}
function table(page) {
    const delimiter = path.extname(workerData.path).toLowerCase() === '.csv' ? ',' : '\t';
    if (!header) {
        const scan = reader(bom);
        let line;
        do { line = scan.record(); } while (line !== null && !line.trim());
        header = cells(line || '', delimiter);
        offsets[0] = scan.position;
    }
    if (offsets[page] === undefined) throw Error('Page not indexed');
    const scan = reader(offsets[page]), rows = [];let characters=0;
    while (rows.length < ROWS) {
        const record = scan.record();
        if (record === null) break;
        if (!record.trim()) continue;
        const parsed = cells(record, delimiter);
        rows.push(parsed);
        characters+=record.length;if(characters>=PAGE_CHARS)break;
    }
    let next = scan.position, lookahead;
    do { next = scan.position; lookahead = scan.record(); } while (lookahead !== null && !lookahead.trim());
    offsets[page + 1] = next;
    rowOffsets[page+1]=rowOffsets[page]+rows.length;
    return {header, rows, page, startRow:rowOffsets[page],columns:Math.max(header.length,...rows.map(row=>row.length)),hasNext:lookahead !== null,
        note: ''};
}
function spreadsheet(page) {
    if (!excel) {
        if (initial.size > 128 * 1024 * 1024) throw Error('Legacy XLS exceeds the 128 MB preview budget; use the system application. / XLS 超过 128 MB 预览预算。');
        const XLSX = require('xlsx');
        const workbook = XLSX.readFile(workerData.path, {cellDates:true, sheets:0, dense:true});
        const name = workbook.SheetNames[0], sheet = workbook.Sheets[name];
        const range = sheet && sheet['!ref'] ? XLSX.utils.decode_range(sheet['!ref']) : null;
        const header = range ? XLSX.utils.sheet_to_json(sheet,{header:1,raw:false,defval:'',range:{s:{r:range.s.r,c:range.s.c},e:{r:range.s.r,c:range.e.c}}})[0] || [] : [];
        excel = {XLSX,sheet,range,header,name,totalRows:range ? range.e.r+1 : 0,starts:[(range?.s.r||0)+1]};
    }
    const start=excel.starts[page]??((excel.range?.s.r||0)+1+page*ROWS),end=Math.min(excel.totalRows-1,start+ROWS-1);
    const rows=start<=end ? excel.XLSX.utils.sheet_to_json(excel.sheet,{header:1,raw:false,defval:'',blankrows:true,range:{s:{r:start,c:excel.range.s.c},e:{r:end,c:excel.range.e.c}}}) : [];
    let characters=0,keep=0;for(const row of rows){characters+=row.reduce((sum,value)=>sum+String(value).length,0);keep++;if(characters>=PAGE_CHARS)break;}
    rows.length=keep;excel.starts[page+1]=start+keep;
    return {header:excel.header,rows,page,startRow:start-(excel.range?.s.r||0),hasNext:start+keep<excel.totalRows,sheetName:excel.name,note:''};
}
function text(page) {
    if (offsets[page] === undefined) throw Error('Page not indexed');
    const start = offsets[page], buffer = Buffer.alloc(TEXT_BYTES + 4);
    const count = read(buffer, start);
    let end = Math.min(count,TEXT_BYTES);
    if (start + end < initial.size) {
        if (width === 2) {
            end -= end % 2;
            const unit = encoding === 'utf16le' ? buffer.readUInt16LE(end-2) : buffer.readUInt16BE(end-2);
            if (unit >= 0xd800 && unit <= 0xdbff) end -= 2;
        } else if (encoding === 'utf8') { while (end > 0 && (buffer[end] & 0xc0) === 0x80) end--; }
        else { let i=0; while(i<end) { const next=i+(buffer[i]>=0x81?2:1); if(next>end){end=i;break;} i=next; } }
    }
    // Prefer a complete line without scanning beyond this bounded byte window.
    let continuedLine = false;
    if (start + end < initial.size) {
        let lineEnd = -1;
        if (width === 2) {
            for (let i=end-2;i>=0;i-=2) {
                const unit=encoding==='utf16le'?buffer.readUInt16LE(i):buffer.readUInt16BE(i);
                if(unit===10){lineEnd=i+2;break;}
            }
        } else {
            const newline=buffer.lastIndexOf(10,end-1);
            if(newline>=0)lineEnd=newline+1;
        }
        if(lineEnd>0)end=lineEnd;
        else continuedLine=true;
    }
    offsets[page+1] = start+end;
    const value=iconv.decode(buffer.subarray(0,end),encoding);
    lineOffsets[page+1]=lineOffsets[page]+(value.match(/\r\n|\r|\n/g)||[]).length;
    return {text:value, startLine:lineOffsets[page], page, hasNext:start+end<initial.size,
        continuedLine,
        note:''};
}
parentPort.on('message', async ({page=0,id}) => {
    try {
        if (fd === undefined) init();
        const current = fs.statSync(workerData.path);
        if (current.size !== initial.size || current.mtimeMs !== initial.mtimeMs) throw Error('文件已变化，请重新打开预览。 / File changed; reopen preview.');
        if (!Number.isSafeInteger(page) || page < 0) throw Error('Invalid page');
        let result;
        if(workerData.kind !== 'text' && /\.xlsx$/i.test(workerData.path)) {
            if(!streamedBook) {
                streamedBook=new (require('./xlsx-preview').WorkbookPreview)(workerData.path,workerData.cacheDir);
                streamedBook.ready.then(()=>{if(!streamedBook.error)parentPort.postMessage({event:'line-count',rows:streamedBook.rows,label:'dataRows',size:initial.size,mtimeMs:initial.mtimeMs,kind:workerData.kind});});
            }
            result=await streamedBook.page(page);
        } else result=workerData.kind==='text'?text(page):/\.xls$/i.test(workerData.path)?spreadsheet(page):table(page);
        parentPort.postMessage({id,...result,bytesRead});
        if(!streamedBook)startLineCount();
    } catch (error) { parentPort.postMessage({id,error:error.message}); }
});
