const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),http=require('node:http');
const {Writable,EventEmitter}=require('node:stream');
const {register,REPOSITORY,ADMIN_ID}=require('../src/github-market-main');
async function run(){
 const id='community_10_demo',bytes=Buffer.from('PK\x03\x04test archive');
 const keys=crypto.generateKeyPairSync('rsa',{modulusLength:3072});
 const info={id,version:'1.0.0',min_app_version:'1.0.0',owner_github_id:10,source:'github:'+REPOSITORY,tag:id+'-v1.0.0',asset:id+'-1.0.0.zip',size:bytes.length,sha256:crypto.createHash('sha256').update(bytes).digest('hex')};
 const raw=Buffer.from(JSON.stringify(info)),published={manifest:raw.toString('base64'),signature:crypto.sign('RSA-SHA256',raw,keys.privateKey).toString('base64')};
 const state={schema:1,administrator_id:ADMIN_ID,reviewers:[],blocked:[],audit:[],requests:{},plugins:{[id]:{id,listed:true,owner:{id:10},published}}};
 const events=[],handlers={};let zipPath,corrupt=false,failBackend=false,backendCalls=0;
 const sender={mainFrame:{},isDestroyed:()=>false,send:(name,value)=>{assert.equal(name,'market-install-progress');assert.equal(value.pluginId,id);events.push(value);}};
 const server=http.createServer((req,res)=>{let body='';req.on('data',chunk=>body+=chunk);req.on('end',()=>{backendCalls++;assert.equal(req.url,'/install-plugin-panel');assert.equal(JSON.parse(body).expected.id,id);res.end(JSON.stringify(failBackend?{status:'failed',message:'install fixture failed'}:{status:'success'}));});});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const read=fs.readFileSync;
 fs.readFileSync=function(file,...args){return path.resolve(String(file))===path.resolve(__dirname,'../src/marketplace-public-key.pem')?keys.publicKey.export({type:'spki',format:'pem'}):read.call(this,file,...args);};
 try{
 register({app:{getPath:()=>'.',getVersion:()=> '1.1.8',on(){}},safeStorage:{isEncryptionAvailable:()=>false},ipcMain:{handle:(name,fn)=>handlers[name]=fn},getWindow:()=>({webContents:sender}),getBackendPort:()=>server.address().port,
 net:{request(){const req=new Writable({write(c,e,cb){cb();},final(cb){cb();queueMicrotask(()=>{const res=new EventEmitter();res.statusCode=200;req.emit('response',res);res.emit('data',Buffer.from(JSON.stringify({content:Buffer.from(JSON.stringify(state)).toString('base64')})));res.emit('end');});}});req.setHeader=()=>{};req.abort=()=>{};return req;}},
 download:async(url,file,verify,progress)=>{zipPath=file;assert(url.includes(info.asset));progress.log('Source selected');progress.onProgress({percent:37,transferred:'1',total:'3',speed:'2'});fs.writeFileSync(file,corrupt?Buffer.from('wrong'):bytes);await verify(file);progress.onProgress({percent:100});}});
 const install=()=>handlers['market-install']({sender,senderFrame:sender.mainFrame},{pluginId:id});
 assert.equal((await install()).ok,true);assert(events.some(e=>e.phase==='download'&&e.percent===37));assert(events.some(e=>e.phase==='install'));assert.equal(events.at(-1).phase,'done');assert.equal(backendCalls,1);assert(!fs.existsSync(path.dirname(zipPath)));
 corrupt=true;await assert.rejects(install(),/完整性/);assert.equal(backendCalls,1);assert.equal(events.at(-1).phase,'failed');assert(!fs.existsSync(path.dirname(zipPath)));
 corrupt=false;failBackend=true;await assert.rejects(install(),/fixture failed/);assert.equal(events.at(-1).phase,'failed');assert(!fs.existsSync(path.dirname(zipPath)));
 console.log('PASS: real signature/hash verification, download progress forwarding, install stages, backend/integrity failures, retry unlock and temporary-file cleanup');
 }finally{fs.readFileSync=read;await new Promise(resolve=>server.close(resolve));}
}
run().catch(error=>{console.error(error);process.exitCode=1;});
