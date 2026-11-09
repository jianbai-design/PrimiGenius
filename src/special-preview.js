'use strict';
const fs=require('fs').promises,path=require('path'),crypto=require('crypto'),{ipcRenderer}=require('electron');
function kind(file){return /\.tiff?$/i.test(file)?'tiff':/\.(pdb|cif|mmcif|sdf|mol2|xyz)$/i.test(file)?'structure':'genome';}
async function asset(file){return fs.readFile(path.join(__dirname,'vendor/file-preview',file),'utf8');}
function mountSpecialPreview(container,filePath,{lang='zh'}={}){
    const doc=container.ownerDocument,win=doc.defaultView,token=crypto.randomBytes(16).toString('hex'),type=kind(filePath),zh=lang==='zh';
    const status=doc.createElement('div'),frame=doc.createElement('iframe');
    status.style.cssText='padding:8px 12px;background:#f5f5f5;color:#555;font-size:12px;';status.textContent=zh?'正在加载预览组件…':'Loading preview…';
    frame.setAttribute('sandbox','allow-scripts');frame.title=zh?'文件预览':'File preview';frame.style.cssText='width:100%;border:0;flex:1;min-height:0;';
    container.style.cssText+=';display:flex;flex-direction:column;min-height:0;overflow:hidden;flex:1;background:#fff;';container.replaceChildren(status,frame);
    const footer=doc.createElement('div');
    container.style.padding='0';container.style.position='relative';
    if(type!=='genome'){
        status.style.cssText+='position:absolute;top:0;left:0;right:0;z-index:1;';
    }
    footer.className='r-viewer-footer';footer.style.flexShrink='0';
    footer.innerHTML=type==='genome'
        ?`<button class="r-viewer-action-btn" data-action="choose">${zh?'添加参考基因组/索引':'Add reference / indexes'}</button>`
        :type==='structure'
        ?`<button class="r-viewer-action-btn" data-action="cartoon">${zh?'飘带':'Cartoon'}</button><button class="r-viewer-action-btn" data-action="sticks">${zh?'棒状':'Sticks'}</button><button class="r-viewer-action-btn" data-action="fit"><i class="fas fa-expand"></i> ${zh?'适应窗口':'Fit'}</button>`
        :`<button class="r-viewer-action-btn" data-action="zoomout" title="${zh?'缩小':'Zoom out'}"><i class="fas fa-search-minus"></i></button><span class="r-viewer-zoom-label">100%</span><button class="r-viewer-action-btn" data-action="zoomin" title="${zh?'放大':'Zoom in'}"><i class="fas fa-search-plus"></i></button><button class="r-viewer-action-btn" data-action="zoomfit"><i class="fas fa-expand"></i> Fit</button><div style="flex:1"></div><button class="r-viewer-action-btn" data-action="download"><i class="fas fa-download"></i> ${zh?'下载':'Download'}</button>`;
    footer.onclick=async event=>{
        const button=event.target.closest('button');if(!button||disposed)return;
        const action=button.dataset.action;
        if(!['choose','download'].includes(action)){frame.contentWindow.postMessage({token,type:'control',action},'*');return;}
        button.disabled=true;
        try{
            if(action==='choose'){
                const result=await ipcRenderer.invoke('open-file-dialog',{properties:['openFile','multiSelections'],title:zh?'选择参考基因组及索引文件':'Select reference genome and indexes'});
                if(disposed)return;
                for(const file of result?.filePaths||[])if(!sources.some(source=>source.name===path.basename(file)))await add(file);
                if(!disposed)frame.contentWindow.postMessage({token,type:'files',files:sources},'*');
                return;
            }
            const selected=await ipcRenderer.invoke('save-file-dialog',{defaultPath:path.basename(filePath)});
            if(!selected.canceled&&selected.filePath&&path.resolve(selected.filePath)!==path.resolve(filePath))await fs.copyFile(filePath,selected.filePath);
        }catch(error){if(!disposed){status.hidden=false;status.textContent=error.message;}}
        finally{button.disabled=false;}
    };
    container.append(footer);
    let disposed=false,timer;const sources=[];
    const dispose=()=>{if(disposed)return;disposed=true;clearTimeout(timer);observer.disconnect();win.removeEventListener('message',message);frame.remove();footer?.remove();for(const source of sources)ipcRenderer.invoke('file-preview-source-close',source.id).catch(()=>{});sources.length=0;};
    const observer=new win.MutationObserver(()=>{if(!container.isConnected)dispose();});observer.observe(doc.body,{childList:true,subtree:true});
    async function add(file){const item=await ipcRenderer.invoke('file-preview-source-open',file);if(disposed){ipcRenderer.invoke('file-preview-source-close',item.id).catch(()=>{});return;}sources.push(item);return item;}
    async function message(event){
        if(disposed||event.source!==frame.contentWindow||event.data?.token!==token)return;
        if(event.data.type==='ready'){clearTimeout(timer);status.textContent=event.data.text||'';status.hidden=type!=='genome';frame.dataset.rendered='true';}
        if(event.data.type==='zoom'&&type==='tiff')footer.querySelector('.r-viewer-zoom-label').textContent=event.data.text+'%';
        if(event.data.type==='error'){clearTimeout(timer);status.hidden=false;status.textContent=event.data.text||'Preview failed';}
    }
    win.addEventListener('message',message);
    timer=setTimeout(()=>{if(!disposed)status.textContent=zh?'预览加载较慢，可关闭窗口取消，或使用系统应用打开。':'Preview is taking longer; close to cancel or open externally.';},30000);
    (async()=>{try{
        if(type==='structure'&&(await fs.stat(filePath)).size>32*1024*1024)throw Error(zh?'结构文件超过 32 MB 交互预览预算，请使用专用工具。':'Structure exceeds the 32 MB interactive-preview budget.');
        await add(filePath);if(disposed)return;
        const libraries=type==='tiff'?await Promise.all([asset('geotiff.js'),asset('openseadragon.min.js')]):[await asset(type==='structure'?'3dmol.min.js':'igv.min.js')];
        const script=text=>`<script nonce="${token}">${text.replace(/<\/script/gi,'<\\/script')}<\/script>`;
        const controller=await fs.readFile(path.join(__dirname,'special-preview-frame.js'),'utf8');
        if(disposed)return;
        frame.onload=()=>{if(!disposed)frame.contentWindow.postMessage({token,type:'init',kind:type,files:sources,zh},'*');};
        frame.srcdoc=`<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'nonce-${token}' 'wasm-unsafe-eval'; connect-src pg-preview: blob: data:; style-src 'unsafe-inline'; img-src data: blob: pg-preview:; font-src data: blob:; worker-src blob:;"><style>html,body{margin:0;height:100%;font:13px system-ui;color:#333;background:#fff}#viewer{position:relative;width:100%;height:100%;min-height:200px}button,select{padding:5px 10px;border:1px solid #ddd;border-radius:5px;background:white;color:#333}button{cursor:pointer}</style><div id="viewer"></div>${libraries.map(script).join('')}${script(`const previewToken='${token}';`+controller)}`;
    }catch(error){if(!disposed){status.textContent=error.message;dispose();}}})();
    return dispose;
}
module.exports={mountSpecialPreview,kind};
