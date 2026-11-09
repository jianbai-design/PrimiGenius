'use strict';
const {EventEmitter} = require('events');
const {ipcRenderer} = require('electron');
const countClients=new Map();
ipcRenderer.on('file-preview-count',(_event,id,result)=>countClients.get(id)?.emit('line-count',result));
class PreviewClient extends EventEmitter {
    constructor(filePath,kind,request=page=>page) {
        super();this.closed=false;this.request=request;
        this.session=ipcRenderer.invoke('file-preview-open',filePath,kind);
        this.session.then(id=>{if(!this.closed)countClients.set(id,this);}).catch(()=>{});
        this.session.catch(error=>{if(!this.closed)this.emit('error',error);});
    }
    postMessage({id,page}) {
        this.session.then(session=>{
            if(this.closed)return;
            return ipcRenderer.invoke('file-preview-page',session,this.request(page)).then(result=>{
                if(!this.closed)this.emit('message',{...result,id});
            });
        }).catch(error=>{if(!this.closed)this.emit('error',error);});
    }
    terminate() {
        if(this.closed)return;this.closed=true;
        this.session.then(session=>{countClients.delete(session);return ipcRenderer.invoke('file-preview-close',session);}).catch(()=>{});
    }
}

function mountFilePreview(container, filePath, {kind='table', lang='zh', onDimensions=()=>{}, showExternalOpen=true}={}) {
    if (/\.docx$/i.test(filePath)) {
        return require('./docx-preview').mountDocxPreview(container,filePath,{lang,onDimensions,showExternalOpen});
    }
    if (/\.(tiff?|pdb|cif|mmcif|sdf|mol2|xyz|bam|cram|bcf|bw|bigwig|bb|bigbed)$|\.(vcf|bed|gff3?|gtf)(\.gz)?$/i.test(filePath)) return require('./special-preview').mountSpecialPreview(container,filePath,{lang});
    if (/\.(zip|tar|tgz|gz)$/i.test(filePath)) return require('./archive-preview').mountArchivePreview(container,filePath,{lang});
    return mountScrollingPreview(container,filePath,{kind,lang,showExternalOpen,onDimensions});
}

function mountScrollingPreview(container,filePath,{kind,lang,showExternalOpen,onDimensions=()=>{},client:providedClient,countRows=true}) {
    const zh=lang==='zh',doc=container.ownerDocument;
    container.replaceChildren();
    container.style.cssText+=';display:flex;flex-direction:column;min-height:0;overflow:hidden;flex:1;';
    const toolbar=doc.createElement('div'),status=doc.createElement('span'),viewport=doc.createElement('div'),tail=doc.createElement('div');
    toolbar.className='r-viewer-footer';
    toolbar.style.cssText='color:#555;display:flex;align-items:center;flex-shrink:0;';
    toolbar.append(status);
    status.style.display='none';
    if(!showExternalOpen)toolbar.style.display='none';
    if(showExternalOpen){const open=doc.createElement('button');open.type='button';open.className='r-viewer-action-btn';open.textContent=zh?'系统应用打开完整文件':'Open full file';open.onclick=()=>require('electron').shell.openPath(filePath);toolbar.append(open);}
    viewport.className='file-preview-scroll';
    viewport.tabIndex=0;
    viewport.setAttribute('aria-label',zh?'文件内容，连续滚动查阅':'File contents, continuous scrolling');
    viewport.style.cssText='overflow:auto;overflow-anchor:none;flex:1;min-height:0;background:#fff;'+(kind==='text'?'padding:0 0 12px;':'');
    tail.style.cssText='padding:8px 12px;color:#777;font-size:12px;';
    viewport.append(tail);container.append(viewport,toolbar);
    const isTable=kind==='table',table=isTable?doc.createElement('div'):null;
    if(table){table.className='preview-data-table';table.style.tableLayout='fixed';table.style.minWidth='100%';viewport.insertBefore(table,tail);}
    let freezeControl,freezeInput,tableHeaderDispose,columnWidths=[];
    if(isTable){
        freezeControl=doc.createElement('label');freezeControl.className='file-preview-freeze';
        freezeInput=doc.createElement('input');freezeInput.type='checkbox';freezeInput.checked=true;
        freezeControl.append(freezeInput,doc.createTextNode(zh?'锁定首行和首列':'Freeze first row and column'));
        freezeControl.title=zh?'固定表头和第一列数据（含行号）':'Keep the header and first data column (including row numbers) visible';
        // Some callers mount the body before attaching it to the preview window.
        queueMicrotask(()=>{
            const header=container.closest('.wf-preview-window,.r-viewer-window')?.querySelector('.wf-preview-header,.r-viewer-header');
            const open=header?.querySelector('.fa-external-link-alt')?.closest('button');
            if(open)open.parentElement.insertBefore(freezeControl,open);
            else if(header)header.insertBefore(freezeControl,header.lastElementChild);
            else {toolbar.style.display='flex';toolbar.append(freezeControl);}
        });
        freezeControl.addEventListener('mousedown',e=>e.stopPropagation());
        freezeInput.onchange=()=>{tableHeaderDispose?.setFrozen(freezeInput.checked);for(const block of blocks)block._dispose?.setFrozen?.(freezeInput.checked);};
    }
    const client=providedClient||new PreviewClient(filePath,kind),blocks=[],visible=new Set(),wanted=new Set();
    function lineCountLabel(text,title=''){
        const label=container.closest('.wf-preview-window,.r-viewer-window')?.querySelector('.file-preview-lines');
        if(label){label.textContent=text;label.title=title;}
    }
    if(countRows)queueMicrotask(()=>lineCountLabel(zh?'｜ 行数统计中…':'｜ Counting rows…'));
    client.on('line-count',result=>{
        if(result.error){lineCountLabel(zh?'｜ 行数暂不可用':'｜ Row count unavailable');return;}
        const unit=result.label==='dataRows'?(zh?'数据行':'data rows'):(zh?'行':'rows');
        const note=result.label==='sheetRows'?(zh?'当前工作表范围，含表头及范围内空行':'Current worksheet range, including headers and blank rows'):result.label==='dataRows'?(zh?'不含表头；引号内换行不另计一行':'Excludes the header; quoted newlines stay in one record'):'';
        lineCountLabel(`｜ ${result.rows.toLocaleString()} ${unit}`,note);
    });
    let busy=false,done=false,disposed=false,serial=0,timeout,scheduled;
    const dispose=()=>{if(disposed)return;disposed=true;clearTimeout(timeout);clearTimeout(scheduled);viewport.removeEventListener('scroll',onScroll);intersection.disconnect();mutation.disconnect();client.terminate();for(const block of blocks)block._dispose?.();tableHeaderDispose?.();freezeControl?.remove();};
    const mutation=new MutationObserver(()=>{if(!container.isConnected)dispose();});
    mutation.observe(doc.body,{childList:true,subtree:true});
    function fail(message){toolbar.style.display='flex';status.style.display='';status.textContent=message;tail.textContent=message;dispose();}
    function request(){
        if(disposed || busy)return;
        for(const index of [...wanted])if(index<blocks.length && !visible.has(index))wanted.delete(index);
        const index=wanted.values().next().value;
        if(index===undefined)return;
        wanted.delete(index);busy=index+1;
        tail.textContent=zh?'正在读取…':'Loading…';
        timeout=setTimeout(()=>fail(zh?'读取超时，请重新打开或使用系统应用。':'Read timed out; reopen or use the system application.'),30000);
        client.postMessage({id:++serial,page:index});
    }
    function trim(){
        // Keep only nearby content. Measured placeholders preserve scroll positions.
        for(let index=0;index<blocks.length;index++){
            const block=blocks[index];
            if(block.dataset.loaded==='true' && !visible.has(index)){
                const height=block.offsetHeight;
                block._dispose?.();block._dispose=null;
                block.style.height=height+'px';block.replaceChildren();
                block.dataset.loaded='false';
            }
        }
    }
    const intersection=new IntersectionObserver(entries=>{
        for(const entry of entries){
            if(entry.target===tail){if(entry.isIntersecting && !done)wanted.add(blocks.length);continue;}
            const index=Number(entry.target.dataset.segment);
            if(entry.isIntersecting){visible.add(index);if(entry.target.dataset.loaded!=='true')wanted.add(index);}
            else visible.delete(index);
        }
        clearTimeout(scheduled);scheduled=setTimeout(()=>{trim();request();},16);
    },{root:viewport,rootMargin:'800px 0px'});
    function onScroll(){
        // A fast scroll may keep the sentinel intersecting across two loads.
        if(!done && viewport.scrollTop+viewport.clientHeight>=viewport.scrollHeight-800){wanted.add(blocks.length);request();}
    }
    viewport.addEventListener('scroll',onScroll,{passive:true});
    intersection.observe(tail);
    client.on('message',result=>{
        if(disposed || result.id!==serial)return;
        clearTimeout(timeout);const index=busy-1;busy=false;
        if(!container.isConnected){dispose();return;}
        if(result.error){fail(result.error);return;}
        let block=blocks[index];
        if(!block){
            block=doc.createElement('div');block.className='file-preview-segment';block.dataset.segment=String(index);
            blocks.push(block);if(isTable)table.append(block);else viewport.insertBefore(block,tail);intersection.observe(block);
            done=!result.hasNext;
        }
        const oldHeight=block.offsetHeight;
        const above=oldHeight>0 && block.getBoundingClientRect().bottom<viewport.getBoundingClientRect().top;
        block._dispose?.();block.replaceChildren();block.style.height='';
        if(isTable && !tableHeaderDispose){
            const header=doc.createElement('div');header.className='file-preview-table-header';table.prepend(header);
            tableHeaderDispose=require('./preview-components').table(header,{...result,rows:[]},{
                frozen:freezeInput.checked,viewport,headerOnly:true,
                onColumnWidths:widths=>{columnWidths=widths;for(const segment of blocks)segment._dispose?.setColumnWidths?.(widths);}
            });
        }
        block._dispose=isTable?require('./preview-components').table(block,result,{frozen:freezeInput.checked,viewport,showHeader:false,columnWidths}):require('./preview-components').text(block,result,filePath);
        if(result.sheetName)onDimensions(result.sheetName);
        if(result.note){toolbar.style.display='flex';status.style.display='';status.textContent=result.note;}
        block.dataset.loaded='true';
        if(above)viewport.scrollTop+=block.offsetHeight-oldHeight;
        tail.textContent=done?(isTable && result.note?(zh?'已到快速预览末尾':'End of quick preview'):(zh?'已到文件末尾':'End of file')):(zh?'继续向下滚动以读取后续内容':'Scroll down for more content');
        request();
    });
    client.on('error',error=>fail(error.message));
    wanted.add(0);request();
    return dispose;
}
function decoratePreviewHeader(header,filePath,lang='zh') {
    const doc=header.ownerDocument,title=header.querySelector(':scope > span');
    if(!title)return;
    const icon=title.querySelector('i'),dims=title.querySelector('.wf-table-dims,.r-table-dims,.r-text-dims');
    const name=doc.createElement('span'),meta=doc.createElement('span'),lines=doc.createElement('span');
    name.className='file-preview-name';name.textContent=require('path').basename(filePath);name.title=filePath;
    meta.className='file-preview-meta';title.classList.add('file-preview-title');
    lines.className='file-preview-meta file-preview-lines';
    title.replaceChildren(...[icon,name,meta,dims,lines].filter(Boolean));
    if(dims)dims.style.color='#666';
    if(!header.querySelector('.fa-external-link-alt')) {
        const prior=header.closest('.r-viewer-window')?.querySelector('[data-action="openext"]');
        const open=prior || doc.createElement('button');open.type='button';open.className='r-viewer-open-external';
        open.title=lang==='zh'?'使用系统应用打开':'Open with system application';open.setAttribute('aria-label',open.title);
        open.innerHTML='<i class="fas fa-external-link-alt" aria-hidden="true"></i>';
        open.onclick=()=>require('electron').shell.openPath(filePath);
        header.insertBefore(open,header.lastElementChild);
    }
    (async()=>{
        try {
            const fs=require('fs').promises,stat=await fs.stat(filePath),ext=require('path').extname(filePath).slice(1).toLowerCase();
            let type=ext.toUpperCase(),size=stat.size,unit=0;
            const units=['B','KB','MB','GB','TB'];
            while(size>=1024 && unit<units.length-1){size/=1024;unit++;}
            if(['fa','fasta','fna','faa','fas','pep','txt'].includes(ext) || !/^[a-z0-9]{1,8}$/.test(ext)) {
                const handle=await fs.open(filePath,'r');
                try {const sample=Buffer.alloc(512);const {bytesRead}=await handle.read(sample,0,sample.length,0);
                    if(sample.subarray(0,bytesRead).toString('utf8').trimStart().startsWith('>'))type='FASTA';
                    else if(!/^[a-z0-9]{1,8}$/.test(ext))type=sample.subarray(0,bytesRead).includes(0)?'FILE':'TEXT';
                } finally {await handle.close();}
            }
            if(['fq','fastq'].includes(ext))type='FASTQ';
            meta.textContent=`｜ ${unit?size.toFixed(1):size} ${units[unit]} ｜ ${type || 'FILE'}`;
        }catch(_){meta.textContent='';}
    })();
}
function measureSvgBounds(svg,doc) {
    return new Promise((resolve,reject)=>{
        const frame=doc.createElement('iframe');
        frame.setAttribute('sandbox','allow-same-origin');
        frame.style.cssText='position:fixed;left:-10000px;top:0;width:1px;height:1px;visibility:hidden;pointer-events:none;';
        const finish=(error,bounds)=>{clearTimeout(timer);frame.remove();error?reject(error):resolve(bounds);};
        const timer=setTimeout(()=>finish(new Error('SVG measurement timed out')),5000);
        frame.onload=()=>{
            try {
                const box=frame.contentDocument.querySelector('svg').getBBox();
                // Include the origin and a small allowance for strokes at the edges.
                const x=Math.min(0,Math.floor(box.x)-2),y=Math.min(0,Math.floor(box.y)-2);
                finish(null,{x,y,width:Math.ceil(box.x+box.width)+2-x,height:Math.ceil(box.y+box.height)+2-y});
            }catch(error){finish(error);}
        };
        // Measure in an isolated document with scripts and external resources blocked.
        frame.srcdoc=`<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">${new XMLSerializer().serializeToString(svg)}`;
        doc.body.append(frame);
    });
}
async function loadPreviewImage(image, filePath, lang='zh') {
    const original = require('url').pathToFileURL(filePath).href;
    image.decoding = 'async';
    if (/\.svg$/i.test(filePath)) {
        try {
            const source=await require('fs').promises.readFile(filePath,'utf8');
            if(!image.isConnected)return;
            const doc=new DOMParser().parseFromString(source,'image/svg+xml');
            const svg=doc.documentElement;
            if(svg.localName==='svg' && !doc.querySelector('parsererror') && !svg.hasAttribute('viewBox')) {
                const explicit=['width','height'].every(name=>svg.hasAttribute(name) && !svg.getAttribute(name).includes('%'));
                const bounds=explicit?{x:0,y:0,width:svg.width.baseVal.value,height:svg.height.baseVal.value}:await measureSvgBounds(svg,image.ownerDocument);
                if(!image.isConnected)return;
                const {x,y,width,height}=bounds;
                if(width>0 && height>0 && Number.isFinite(width) && Number.isFinite(height)) {
                    svg.setAttribute('viewBox',`${x} ${y} ${width} ${height}`);
                    if(!explicit){svg.setAttribute('width',width);svg.setAttribute('height',height);}
                    // Keep the source untouched; decode the preview as an image, never inline script.
                    const url=URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(svg)],{type:'image/svg+xml'}));
                    const cleanup=new MutationObserver(()=>{if(!image.isConnected){URL.revokeObjectURL(url);cleanup.disconnect();}});
                    cleanup.observe(image.ownerDocument.body,{childList:true,subtree:true});
                    image.src=url;
                    return;
                }
            }
        } catch (_) { /* Keep the browser's original decoder for unsupported SVG input. */ }
    }
    try {
        const stat = await require('fs').promises.stat(filePath);
        if (stat.size > 8*1024*1024 && /\.(png|jpe?g|bmp|webp|tiff?)$/i.test(filePath)) {
            const thumbnail = await require('electron').ipcRenderer.invoke('preview-image-thumbnail',filePath);
            if(!image.isConnected)return;
            image.src=thumbnail;
            const full=image.ownerDocument.createElement('button');full.className='r-viewer-action-btn';
            full.textContent=lang==='zh'?'缩略预览 · 加载原图':'Thumbnail · Load original';
            full.onclick=()=>{image.src=original;full.remove();};
            (image.closest('.r-viewer-window')?.querySelector('.r-viewer-footer') || image.parentElement).append(full);
            return;
        }
    } catch (_) { /* Unsupported OS/image codecs keep the browser's file-backed decoder. */ }
    if(image.isConnected)image.src=original;
}
async function mountHtmlPreview(container, filePath, lang='zh', {showExternalOpen=true}={}) {
    container.textContent=lang==='zh'?'正在加载…':'Loading…';
    try {
        const stat=await require('fs').promises.stat(filePath);
        if(!container.isConnected)return;
        if(stat.size>2*1024*1024) {
            mountFilePreview(container,filePath,{kind:'text',lang,showExternalOpen});
            return;
        }
        const frame=container.ownerDocument.createElement('iframe');
        frame.style.cssText='width:100%;height:100%;border:none;flex:1;';
        frame.setAttribute('sandbox','allow-scripts');
        frame.src=require('url').pathToFileURL(filePath).href;
        container.replaceChildren(frame);
    } catch(error) { if(container.isConnected)container.textContent=error.message; }
}
function mountImageZoom(overlay, image) {
    const body=overlay.querySelector('.r-viewer-img-body');
    const label=overlay.querySelector('.r-viewer-zoom-label');
    let scale=1, fitting=true;
    function update() {
        if(!image.naturalWidth || !body.clientWidth || !body.clientHeight)return;
        if(fitting){image.style.width='0px';image.style.height='0px';}
        if(fitting)scale=Math.min(1, Math.max(1,body.clientWidth-24)/image.naturalWidth, Math.max(1,body.clientHeight-24)/image.naturalHeight);
        // Size the layout box too, so zoomed content remains scrollable to every edge.
        image.style.width=image.naturalWidth*scale+'px';
        image.style.height=image.naturalHeight*scale+'px';
        label.textContent=Math.round(scale*100)+'%';
        if(fitting){body.scrollLeft=0;body.scrollTop=0;}
    }
    function zoom(factor){fitting=false;scale=Math.max(0.01,Math.min(scale*factor,10));update();}
    overlay.querySelector('[data-action="zoomin"]').onclick=()=>zoom(1.25);
    overlay.querySelector('[data-action="zoomout"]').onclick=()=>zoom(1/1.25);
    overlay.querySelector('[data-action="zoomfit"]').onclick=()=>{fitting=true;update();};
    const listen=pointerScope(overlay);
    listen(image,'load',()=>{fitting=true;update();});
    body.addEventListener('wheel',e=>{e.preventDefault();zoom(e.deltaY>0?0.9:1.1);},{passive:false});
    const resize=new ResizeObserver(()=>{if(fitting)update();});
    resize.observe(body);
    const cleanup=new MutationObserver(()=>{
        if(!overlay.isConnected){resize.disconnect();cleanup.disconnect();}
    });
    cleanup.observe(overlay.ownerDocument.body,{childList:true,subtree:true});
    update();
}
function pointerScope(element) {
    const doc=element.ownerDocument,controller=new AbortController();
    const observer=new MutationObserver(()=>{
        if(!element.isConnected){controller.abort();observer.disconnect();}
    });
    observer.observe(doc.body,{childList:true,subtree:true});
    return (target,type,callback)=>target.addEventListener(type,callback,{signal:controller.signal});
}
function makeWindowDraggable(overlay, win, header) {
    const listen=pointerScope(overlay),doc=overlay.ownerDocument;
    let start, pressedOnBackdrop=false;
    // A drag that starts inside the window must never become a backdrop click.
    overlay.addEventListener('mousedown',e=>{pressedOnBackdrop=e.target===overlay;},true);
    overlay.addEventListener('click',e=>{
        if(e.target===overlay && !pressedOnBackdrop){e.stopImmediatePropagation();e.preventDefault();}
    },true);
    header.style.cursor='move';
    listen(header,'mousedown',e=>{
        if(e.button!==0 || e.target.closest('button,a,input,select'))return;
        const rect=win.getBoundingClientRect();start={x:e.clientX,y:e.clientY,left:rect.left,top:rect.top};
        win.style.position='fixed';win.style.margin='0';e.preventDefault();
    });
    listen(doc,'mousemove',e=>{if(start){win.style.left=start.left+e.clientX-start.x+'px';win.style.top=start.top+e.clientY-start.y+'px';}});
    listen(doc,'mouseup',()=>{start=null;});
}
function makeWindowResizable(win) {
    const listen=pointerScope(win),doc=win.ownerDocument,handle=doc.createElement('div');
    handle.className='r-viewer-resize-handle';win.append(handle);
    let start;
    listen(handle,'mousedown',e=>{if(e.button!==0)return;const rect=win.getBoundingClientRect();
        start={x:e.clientX,y:e.clientY,width:win.offsetWidth,height:win.offsetHeight};
        Object.assign(win.style,{position:'fixed',margin:'0',left:rect.left+'px',top:rect.top+'px'});
        e.preventDefault();e.stopPropagation();});
    listen(doc,'mousemove',e=>{if(start){win.style.width=Math.max(400,start.width+e.clientX-start.x)+'px';win.style.height=Math.max(300,start.height+e.clientY-start.y)+'px';}});
    listen(doc,'mouseup',()=>{start=null;});
}
module.exports={mountImageZoom,mountFilePreview,loadPreviewImage,mountHtmlPreview,makeWindowDraggable,makeWindowResizable,decoratePreviewHeader,PreviewClient,mountScrollingPreview};
