'use strict';
const path = require('node:path');
const fs = require('node:fs/promises');
const crypto = require('node:crypto');
const {fileURLToPath} = require('node:url');

// SVG is printed directly by Chromium: paths and live text stay vector in PDF.
async function renderPdf(BrowserWindow, svg, width, height) {
    if (typeof svg !== 'string' || Buffer.byteLength(svg)>64*1024*1024 || !/^\s*<svg[\s>]/i.test(svg) ||
        !Number.isFinite(width) || !Number.isFinite(height) || width<.1 || height<.1 || width>100 || height>100) {
        throw Error('Invalid SVG or physical page size');
    }
    let timer;
    const win = new BrowserWindow({show:false,webPreferences:{nodeIntegration:false,contextIsolation:true,sandbox:true,webSecurity:true,javascript:false}});
    win.webContents.setWindowOpenHandler(()=>({action:'deny'}));
    try {
        const page = '<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src data:; font-src data:"><style>'+
            `@page{size:${width}in ${height}in;margin:0}html,body{margin:0;padding:0;width:${width}in;height:${height}in;overflow:hidden}body>svg{display:block;width:100%;height:100%}`+
            '</style></head><body>'+svg+'</body></html>';
        const task = async()=>{
            await win.loadURL('data:text/html;charset=utf-8,'+encodeURIComponent(page));
            return win.webContents.printToPDF({printBackground:true,preferCSSPageSize:true,pageSize:{width,height},margins:{top:0,bottom:0,left:0,right:0},scale:1});
        };
        return await Promise.race([task(),new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('PDF export timed out')),30000);})]);
    } finally { clearTimeout(timer); if (!win.isDestroyed()) win.destroy(); }
}

function register({app,ipcMain,BrowserWindow,dialog}) {
    const root=path.resolve(__dirname), queues=new Map();
    function trusted(event) {
        let file;
        try { file=fileURLToPath(event.sender.getURL()); } catch (_) {}
        if (event.senderFrame!==event.sender.mainFrame || !file || !['editor.html','index.html'].some(name=>path.resolve(file)===path.join(root,name))) throw Error('Local editor required');
    }
    ipcMain.handle('vector-export-pdf',async(event,payload)=>{
        trusted(event);
        if (queues.has(event.sender.id)) throw Error('An export is already running');
        queues.set(event.sender.id,true);
        try { return await renderPdf(BrowserWindow,payload.svg,payload.width,payload.height); }
        finally { queues.delete(event.sender.id); }
    });
    ipcMain.handle('vector-save-file',async(event,{name,bytes})=>{
        trusted(event);
        if (typeof name!=='string' || !/\.(svg|pdf|png|tiff?)$/i.test(name) || !bytes || bytes.byteLength>192*1024*1024) throw Error('Invalid export');
        const owner=BrowserWindow.fromWebContents(event.sender);
        const result=await dialog.showSaveDialog(owner,{defaultPath:path.basename(name)});
        if (result.canceled || !result.filePath) return null;
        await fs.writeFile(result.filePath,Buffer.from(bytes));
        return result.filePath;
    });
    ipcMain.handle('vector-recovery',async(event,{key,action,svg})=>{
        trusted(event);
        if (typeof key!=='string' || key.length>8192) throw Error('Invalid recovery key');
        const dir=path.join(app.getPath('userData'),'vector-recovery');
        const target=path.join(dir,crypto.createHash('sha256').update(key).digest('hex')+'.svg');
        if (action==='read') {
            try { const stat=await fs.stat(target); if(stat.size>64*1024*1024)throw Error('Recovery file too large');return {svg:await fs.readFile(target,'utf8'),time:stat.mtimeMs}; }
            catch(error){if(error.code==='ENOENT')return null;throw error;}
        }
        if(action==='clear'){await fs.rm(target,{force:true});return true;}
        if(action!=='write' || typeof svg!=='string' || Buffer.byteLength(svg)>64*1024*1024)throw Error('Invalid recovery data');
        await fs.mkdir(dir,{recursive:true});
        // Each write owns a temporary name; a cancelled/failed write never damages the last copy.
        const temporary=target+'.'+crypto.randomBytes(6).toString('hex')+'.tmp';
        try { await fs.writeFile(temporary,svg,'utf8'); await fs.rename(temporary,target); }
        finally { await fs.rm(temporary,{force:true}); }
        return true;
    });
    ipcMain.handle('vector-editor-close-approved',event=>{
        trusted(event);const win=BrowserWindow.fromWebContents(event.sender);
        if (win && path.basename(fileURLToPath(event.sender.getURL()))==='editor.html') win.__vectorCloseApproved=true;
        return true;
    });
}
module.exports={register,renderPdf};
