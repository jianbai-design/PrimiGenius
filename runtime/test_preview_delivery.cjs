'use strict';
// Exercise preview code inside ASAR with production dependencies only.
const fs=require('fs'),path=require('path'),cp=require('child_process');
const root=path.resolve(__dirname,'..'),directory=fs.mkdtempSync(path.join(root,'.tmp-packaged-preview-'));
(async()=>{
    try{
        const stage=path.join(directory,'app');fs.mkdirSync(stage);
        fs.cpSync(path.join(root,'src'),path.join(stage,'src'),{recursive:true});
        for(const file of ['package.json','main.js'])fs.copyFileSync(path.join(root,file),path.join(stage,file));
        const lock=JSON.parse(fs.readFileSync(path.join(root,'package-lock.json'),'utf8'));
        let packages=0;
        for(const [name,metadata] of Object.entries(lock.packages)){
            if(!name||metadata.dev||!name.startsWith('node_modules/'))continue;
            const source=path.join(root,name);
            if(metadata.optional&&!fs.existsSync(source))continue;
            fs.cpSync(source,path.join(stage,name),{recursive:true});packages++;
        }
        const archive=path.join(directory,'app.asar');await require('@electron/asar').createPackage(stage,archive);
        console.log(JSON.stringify({productionPackages:packages,archiveMiB:fs.statSync(archive).size/1048576}));
        for(const test of ['test_upload_preview.cjs','test_special_preview.cjs','test_archive_preview.cjs']){
            const result=cp.spawnSync(process.execPath,[path.join(__dirname,test)],{
                env:{...process.env,PRIMIGENIUS_PREVIEW_TEST_ASAR:archive},encoding:'utf8',windowsHide:true,timeout:75000});
            process.stdout.write(result.stdout||'');process.stderr.write(result.stderr||'');
            if(result.status!==0)throw Error('Packaged preview test failed: '+test);
        }
    }finally{
        const target=path.resolve(directory);
        if(path.dirname(target)!==root||!path.basename(target).startsWith('.tmp-packaged-preview-'))throw Error('Unexpected cleanup target');
        fs.rmSync(target,{recursive:true,force:true,maxRetries:6,retryDelay:200});
    }
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
