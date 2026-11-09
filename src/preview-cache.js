'use strict';
const fs=require('fs'),os=require('os'),path=require('path'),crypto=require('crypto');
// This installation owns only these caches. Never sweep user output or plugin folders.
const root=path.join(os.tmpdir(),'PrimiGenius-preview-'+crypto.createHash('sha256').update(__dirname).digest('hex').slice(0,16));
let initialized=false;
function alive(pid){try{process.kill(pid,0);return true;}catch(error){return error.code!=='ESRCH';}}
function create(){
    fs.mkdirSync(root,{recursive:true});
    const stat=fs.lstatSync(root);if(!stat.isDirectory()||stat.isSymbolicLink())throw Error('Invalid preview cache directory');
    if(!initialized){
        initialized=true;
        for(const entry of fs.readdirSync(root,{withFileTypes:true})){
            const match=/^(\d+)-[0-9a-f-]{36}$/.exec(entry.name);
            if(!match||!entry.isDirectory()||entry.isSymbolicLink())continue;
            const directory=path.join(root,entry.name);
            try{
                const owner=JSON.parse(fs.readFileSync(path.join(directory,'owner.json'),'utf8'));
                if(owner.version===1&&owner.pid===Number(match[1])&&!alive(owner.pid))
                    fs.promises.rm(directory,{recursive:true,force:true,maxRetries:4,retryDelay:100}).catch(()=>{});
            }catch(_){/* Unrecognized directories are never deleted. */}
        }
    }
    const directory=path.join(root,process.pid+'-'+crypto.randomUUID());fs.mkdirSync(directory);
    fs.writeFileSync(path.join(directory,'owner.json'),JSON.stringify({version:1,pid:process.pid}));
    return directory;
}
module.exports={create};
