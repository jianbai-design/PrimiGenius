const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {Writable,EventEmitter}=require('node:stream');
const {register,ADMIN_ID}=require('../src/github-market-main');
async function run(){
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'market-profile-test-'));
 fs.writeFileSync(path.join(directory,'github-market-session.bin'),'fixture');
 const handlers={},opened=[],sender={mainFrame:{}},id=242235968;let reads=0,holdLogin=false,finishLogin;
 try{
 register({app:{getPath:()=>directory,on(){}},ipcMain:{handle:(name,fn)=>handlers[name]=fn},safeStorage:{isEncryptionAvailable:()=>true,decryptString:()=> 'fixture-token'},getWindow:()=>({webContents:sender}),shell:{openExternal:async url=>opened.push(url)},
 net:{request(options){const req=new Writable({write(c,e,done){done();},final(done){done();queueMicrotask(()=>{
  const profile=options.url.endsWith('/user')?{id:ADMIN_ID,login:'admin'}:{id,login:'reviewer',name:'Reviewer Name',company:'Lab',location:'Shanghai',bio:'Public profile'};
  if(!options.url.endsWith('/user'))reads++;
  const respond=()=>{const res=new EventEmitter();res.statusCode=200;req.emit('response',res);res.emit('data',Buffer.from(JSON.stringify(profile)));res.emit('end');};
  if(holdLogin&&options.url.endsWith('/user'))finishLogin=respond;else respond();
 });}});req.setHeader=()=>{};req.abort=()=>{};return req;}}});
 const invoke=(name,args)=>handlers[name]({sender,senderFrame:sender.mainFrame},args);
 const person=await invoke('market-resolve-user',{value:String(id)});
 assert.equal(person.name,'Reviewer Name');assert.equal(person.company,'Lab');assert.equal(person.location,'Shanghai');assert.equal(person.bio,'Public profile');assert.equal(person.avatar_url,`https://avatars.githubusercontent.com/u/${id}?v=4`);
 assert.equal((await invoke('market-resolve-user',{value:'reviewer'})).id,id);assert.equal(reads,1,'Cache numeric ID and login aliases');
 await invoke('market-open',{url:'https://github.com/reviewer'});assert.equal(opened.length,1);
 for(const url of ['https://evil.example/reviewer','https://evil@github.com/reviewer','https://github.com/reviewer/repository'])await assert.rejects(invoke('market-open',{url}));
 await assert.rejects(invoke('market-resolve-user',{value:'bad/name'}));assert.equal(reads,1);
 assert.equal((await invoke('market-session')).token,'fixture-token');
 await assert.rejects(handlers['market-session']({sender:{},senderFrame:{}},{}),/主程序窗口/);
 holdLogin=true;const late=invoke('market-login',{token:'late-token'});await new Promise(resolve=>setImmediate(resolve));
 await invoke('market-logout',{});finishLogin();await assert.rejects(late,/已取消/);
 assert(!fs.existsSync(path.join(directory,'github-market-session.bin')),'A late login must not recreate the signed-out vault');
 console.log('PASS: reviewer public profile fields, cached lookup aliases, input validation and safe GitHub profile navigation');
 }finally{fs.rmSync(directory,{recursive:true,force:true});}
}
run().catch(error=>{console.error(error);process.exitCode=1;});
