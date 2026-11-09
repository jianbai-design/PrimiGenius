'use strict';
const {Worker} = require('worker_threads');
const path=require('path'),crypto=require('crypto'),fs=require('fs');
const {Readable}=require('stream');
function registerPreviewService(ipcMain, getWindow) {
    const sessions=new Map(),counts=new Map(),sources=new Map(),closing=new Set();
    const authorize=event=>{
        if(event.sender!==getWindow()?.webContents || event.senderFrame!==event.sender.mainFrame) throw Error('Preview requires the application window');
    };
    const close=id=>{
        const session=sessions.get(id);if(!session)return;
        sessions.delete(id);clearTimeout(session.timer);
        session.owner.removeListener('destroyed',session.onClose);
        session.pending?.reject(Error('Preview closed'));
        const cleanup=session.worker.terminate().then(()=>session.cacheDir?fs.promises.rm(session.cacheDir,{recursive:true,force:true,maxRetries:4,retryDelay:100}):undefined);
        closing.add(cleanup);cleanup.finally(()=>closing.delete(cleanup)).catch(()=>{});
        return cleanup;
    };
    ipcMain.handle('file-preview-open',(event,filePath,kind)=>{
        authorize(event);
        if(typeof filePath!=='string' || !path.isAbsolute(filePath) || !['text','table','archive'].includes(kind))throw Error('Invalid preview request');
        if(sessions.size>=16)throw Error('Close an existing preview before opening more files');
        const cacheDir=kind==='archive'?null:require('./preview-cache').create();
        const id=crypto.randomUUID();let worker;
        try{worker=new Worker(path.join(__dirname,kind==='archive'?'archive-preview-worker.js':'file-preview-worker.js'),{
            workerData:{path:filePath,kind,cacheDir,cachedCount:counts.get(filePath)},resourceLimits:{maxOldGenerationSizeMb:256}});}
        catch(error){if(cacheDir)fs.rmSync(cacheDir,{recursive:true,force:true});throw error;}
        const session={worker,cacheDir,owner:event.sender,onClose:()=>close(id)};
        sessions.set(id,session);event.sender.once('destroyed',session.onClose);
        worker.on('message',result=>{
            if(result.event==='line-count'){
                if(!result.error){counts.delete(filePath);counts.set(filePath,result);while(counts.size>64)counts.delete(counts.keys().next().value);}
                if(sessions.has(id) && !session.owner.isDestroyed?.())session.owner.send?.('file-preview-count',id,result);
                return;
            }
            const pending=session.pending;if(!pending)return;session.pending=null;clearTimeout(session.timer);pending.resolve(result);
        });
        worker.on('error',error=>{session.pending?.reject(error);session.pending=null;close(id);});
        worker.on('exit',()=>close(id));
        return id;
    });
    ipcMain.handle('file-preview-page',(event,id,page)=>{
        authorize(event);const session=sessions.get(id);
        if(!session || session.owner!==event.sender)throw Error('Preview closed');
        if(session.pending)throw Error('Preview request already in progress');
        return new Promise((resolve,reject)=>{
            session.pending={resolve,reject};session.timer=setTimeout(()=>close(id),30000);
            session.worker.postMessage({page});
        });
    });
    ipcMain.handle('file-preview-close',(event,id)=>{authorize(event);if(sessions.get(id)?.owner===event.sender)return close(id);});
    ipcMain.handle('file-preview-source-open',(event,filePath)=>{
        authorize(event);if(typeof filePath!=='string'||!path.isAbsolute(filePath))throw Error('Invalid preview file');
        const stat=fs.statSync(filePath);if(!stat.isFile())throw Error('Invalid preview file');
        if(sources.size>=256)throw Error('Too many preview sources');
        const id=crypto.randomUUID(),onClose=()=>release(id);sources.set(id,{path:filePath,owner:event.sender,onClose,streams:new Set()});event.sender.once('destroyed',onClose);
        return {id,name:path.basename(filePath),size:stat.size,url:`pg-preview://${id}/${encodeURIComponent(path.basename(filePath))}`};
    });
    function release(id){const source=sources.get(id);if(source){source.owner.removeListener('destroyed',source.onClose);sources.delete(id);for(const stream of source.streams)stream.destroy();}}
    ipcMain.handle('file-preview-source-close',(event,id)=>{authorize(event);if(sources.get(id)?.owner===event.sender)release(id);});
    async function serve(request){
        const source=sources.get(new URL(request.url).hostname);
        const headers={'Access-Control-Allow-Origin':'*','Access-Control-Allow-Headers':'Range','Access-Control-Expose-Headers':'Content-Range,Accept-Ranges,Content-Length','Cache-Control':'no-store','Accept-Ranges':'bytes'};
        if(!source)return new Response('Preview closed',{status:404,headers});
        if(request.method==='OPTIONS')return new Response(null,{status:204,headers});
        if(!['GET','HEAD'].includes(request.method))return new Response(null,{status:405,headers});
        try{
            const size=(await fs.promises.stat(source.path)).size,range=request.headers.get('range');
            let start=0,end=size-1,status=200;
            if(range){const match=/^bytes=(\d*)-(\d*)$/.exec(range);if(!match)return new Response(null,{status:416,headers});
                start=match[1]?Number(match[1]):Math.max(0,size-Number(match[2]));end=match[1]&&match[2]?Math.min(Number(match[2]),size-1):size-1;
                if(!Number.isSafeInteger(start)||start<0||start>end||start>=size)return new Response(null,{status:416,headers:{...headers,'Content-Range':`bytes */${size}`}});
                status=206;headers['Content-Range']=`bytes ${start}-${end}/${size}`;
            }
            headers['Content-Length']=String(Math.max(0,end-start+1));headers['Content-Type']='application/octet-stream';
            let body=null;
            if(request.method!=='HEAD'&&size){const stream=fs.createReadStream(source.path,{start,end});source.streams.add(stream);stream.once('close',()=>source.streams.delete(stream));body=Readable.toWeb(stream);}
            return new Response(body,{status,headers});
        }catch(_){return new Response('File unavailable',{status:404,headers});}
    }
    return {closeAll:()=>{for(const id of sources.keys())release(id);for(const id of sessions.keys())close(id);return Promise.all([...closing]);},activeCount:()=>sessions.size,installProtocol:protocol=>protocol.handle('pg-preview',serve),serve};
}
module.exports={registerPreviewService};
