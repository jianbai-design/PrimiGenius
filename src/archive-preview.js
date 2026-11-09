'use strict';
const path=require('path'),{ipcRenderer}=require('electron');
const {PreviewClient,mountScrollingPreview}=require('./file-preview');
const images={'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.gif':'image/gif','.webp':'image/webp','.bmp':'image/bmp','.svg':'image/svg+xml'};
function mountArchivePreview(container,filePath,{lang='zh'}={}){
    const zh=lang==='zh',doc=container.ownerDocument,win=doc.defaultView;
    const gzip=/\.gz$/i.test(filePath)&&!/\.tar\.gz$/i.test(filePath);
    if(gzip)return mountScrollingPreview(container,filePath.replace(/\.gz$/i,''),{kind:'text',lang,showExternalOpen:false,countRows:false,
        client:new PreviewClient(filePath,'archive',page=>({action:'text',entry:0,page}))});
    container.replaceChildren();container.classList.add('archive-preview');
    container.style.cssText+=';display:flex;flex-direction:row;flex:1;min-height:0;padding:0;overflow:hidden;background:#fff;color:#333;';
    const sidebar=doc.createElement('div'),heading=doc.createElement('div'),list=doc.createElement('div'),content=doc.createElement('div'),title=doc.createElement('div'),body=doc.createElement('div');
    const navigation=doc.createElement('div'),up=doc.createElement('button'),root=doc.createElement('button'),location=doc.createElement('span'),loadMore=doc.createElement('button');
    navigation.className='archive-preview-nav';location.className='archive-preview-path';
    up.type=root.type=loadMore.type='button';up.textContent='↑';root.textContent='/';
    up.title=zh?'返回上一级':'Parent directory';root.title=zh?'返回根目录':'Archive root';
    up.setAttribute('aria-label',up.title);root.setAttribute('aria-label',root.title);up.dataset.action='parent';root.dataset.action='root';
    loadMore.className='archive-preview-load-more';loadMore.textContent=zh?'继续读取成员':'Load more members';
    navigation.append(up,root,location);
    sidebar.className='archive-preview-sidebar';heading.className='archive-preview-heading';list.className='archive-preview-list';
    heading.hidden=true;heading.setAttribute('role','status');
    list.setAttribute('aria-label',zh?'压缩包成员':'Archive members');content.className='archive-preview-content';title.className='archive-preview-heading';body.className='archive-preview-member';
    body.textContent=zh?'点击左侧文件预览':'Select a file to preview';
    sidebar.append(heading,navigation,list,loadMore);content.append(title,body);container.append(sidebar,content);
    const directory=(name,path,parent)=>({name,path,parent,type:'directory',folders:new Map(),files:[],rows:null});
    const archiveRoot=directory('','',null);let currentDirectory=archiveRoot;
    let session,closed=false,busy=false,page=0,more=true,stopMember,selected=-1,selection=0,blobSession,blobURL;
    function addEntry(entry){
        const parts=entry.name.replace(/\\/g,'/').split('/').filter(part=>part&&part!=='.');
        if(!parts.length)return;
        let parent=archiveRoot;
        const depth=entry.type==='directory'?parts.length:parts.length-1;
        for(let index=0;index<depth;index++){
            const name=parts[index];
            if(!parent.folders.has(name)){parent.folders.set(name,directory(name,parent.path+name+'/',parent));parent.rows=null;}
            parent=parent.folders.get(name);
        }
        if(entry.type!=='directory'){parent.files.push({...entry,label:parts[parts.length-1]});parent.rows=null;}
    }
    function rows(){return currentDirectory.rows??=[...currentDirectory.folders.values(),...currentDirectory.files];}
    function enter(folder){
        releaseMember();selected=-1;currentDirectory=folder;list.scrollTop=0;
        title.textContent=folder.path||path.basename(filePath);title.title=title.textContent;
        body.replaceChildren();body.textContent=zh?'点击左侧文件预览':'Select a file to preview';render();
        if(!folder.folders.size&&!folder.files.length&&more)load();
    }
    up.onclick=()=>{if(currentDirectory.parent)enter(currentDirectory.parent);};root.onclick=()=>enter(archiveRoot);loadMore.onclick=()=>load();
    const observer=new win.MutationObserver(()=>{if(!container.isConnected)dispose();});observer.observe(doc.body,{childList:true,subtree:true});
    const open=ipcRenderer.invoke('file-preview-open',filePath,'archive').then(id=>{session=id;if(closed)return ipcRenderer.invoke('file-preview-close',id);});
    function releaseMember(){selection++;stopMember?.();stopMember=null;if(blobSession){ipcRenderer.invoke('file-preview-close',blobSession).catch(()=>{});blobSession=null;}if(blobURL){win.URL.revokeObjectURL(blobURL);blobURL=null;}}
    function dispose(){if(closed)return;closed=true;observer.disconnect();list.removeEventListener('scroll',scroll);releaseMember();open.then(()=>{if(session)return ipcRenderer.invoke('file-preview-close',session);}).catch(()=>{});}
    function render(){
        const entries=rows(),start=Math.max(0,Math.floor(list.scrollTop/40)-5),end=Math.min(entries.length,start+Math.ceil(list.clientHeight/40)+10);
        up.disabled=root.disabled=!currentDirectory.parent;location.textContent=currentDirectory.path||'/';location.title=location.textContent;
        loadMore.hidden=!more;loadMore.disabled=busy;
        loadMore.textContent=busy?(zh?'正在读取成员…':'Loading members…'):(zh?'继续读取成员':'Load more members');
        if(!entries.length&&currentDirectory.parent&&selected===-1)body.textContent=more
            ?(zh?'目录成员尚未读取完成，可点击“继续读取成员”。':'Directory members are still being scanned; choose “Load more members”.')
            :(zh?'此目录为空。':'This directory is empty.');
        const top=doc.createElement('div'),bottom=doc.createElement('div');top.style.height=start*40+'px';bottom.style.height=(entries.length-end)*40+'px';
        const buttons=entries.slice(start,end).map(entry=>{
            const button=doc.createElement('button');button.type='button';button.className='archive-preview-entry';
            if(entry.type==='directory')button.dataset.directory=entry.path;else button.dataset.entry=entry.id;
            button.classList.toggle('selected',entry.id===selected);button.setAttribute('aria-pressed',String(entry.id===selected));button.title=entry.name;
            const name=doc.createElement('span'),size=doc.createElement('small');name.textContent=entry.type==='directory'?'▸ '+entry.name+'/':entry.label;
            size.textContent=entry.type==='directory'?(zh?'目录':'Directory'):entry.type==='link'?(zh?'链接':'Link'):entry.encrypted?(zh?'已加密':'Encrypted'):formatSize(entry.size);
            button.append(name,size);button.onclick=()=>entry.type==='directory'?enter(entry):choose(entry);return button;
        });list.replaceChildren(top,...buttons,bottom);
    }
    async function load(){
        if(busy||!more||closed)return;busy=true;loadMore.disabled=true;loadMore.textContent=zh?'正在读取成员…':'Loading members…';
        try{
            await open;if(closed)return;
            const result=await ipcRenderer.invoke('file-preview-page',session,{action:'list',page});if(closed)return;
            if(result.error)throw Error(result.error);
            for(const entry of result.entries)addEntry(entry);page++;more=result.hasNext;
            heading.hidden=!result.limited;
            heading.textContent=result.limited?(zh?'已达预览列表上限':'Preview list limit reached'):'';
            render();
        }catch(error){if(!closed){heading.hidden=false;heading.textContent=error.message;more=false;}}
        finally{busy=false;if(!closed)render();}
    }
    function scroll(){render();if(list.scrollTop+list.clientHeight>=list.scrollHeight-400)load();}
    list.addEventListener('scroll',scroll,{passive:true});
    async function choose(entry){
        releaseMember();const current=selection;selected=entry.id;render();title.textContent=entry.name;title.title=entry.name;body.replaceChildren();
        if(entry.type!=='file'||entry.encrypted){body.textContent=entry.type==='directory'?(zh?'请在列表中选择目录内的文件。':'Select a file inside this directory.'):zh?'链接或加密成员请使用压缩软件查看。':'Use your archive application for links or encrypted members.';return;}
        const ext=path.extname(entry.name).toLowerCase(),mime=images[ext];
        if(mime){
            body.textContent=zh?'正在读取…':'Loading…';
            try{
                const id=await ipcRenderer.invoke('file-preview-open',filePath,'archive');
                if(closed||selection!==current){await ipcRenderer.invoke('file-preview-close',id);return;}blobSession=id;
                const result=await ipcRenderer.invoke('file-preview-page',id,{action:'blob',entry:entry.id});
                await ipcRenderer.invoke('file-preview-close',id);if(blobSession===id)blobSession=null;
                if(closed||selection!==current)return;if(result.error)throw Error(result.error);
                blobURL=win.URL.createObjectURL(new win.Blob([result.bytes],{type:mime}));
                const view=doc.createElement('img');view.alt=entry.name;view.decoding='async';
                view.src=blobURL;view.className='archive-preview-image';view.title=entry.name;body.replaceChildren(view);
            }catch(error){if(!closed&&selection===current){if(blobSession){ipcRenderer.invoke('file-preview-close',blobSession).catch(()=>{});blobSession=null;}body.textContent=error.message;}}
            return;
        }
        if(/\.(zip|gz|tar|tgz|7z|rar|xlsx?|docx?|pptx?|pdf|bam|bai|cram|bcf|exe|dll|so|tiff?|ico)$/i.test(entry.name)){
            body.textContent=zh?'此类型暂不支持包内预览，请使用压缩软件查看。':'This member type is not available for in-archive preview; use your archive application.';return;
        }
        stopMember=mountScrollingPreview(body,entry.name,{kind:'text',lang,showExternalOpen:false,countRows:false,
            client:new PreviewClient(filePath,'archive',page=>({action:'text',entry:entry.id,page}))});
    }
    load();return dispose;
}
function formatSize(bytes){let unit=0;const units=['B','KB','MB','GB','TB'];while(bytes>=1024&&unit<units.length-1){bytes/=1024;unit++;}return `${unit?bytes.toFixed(1):bytes} ${units[unit]}`;}
module.exports={mountArchivePreview};
