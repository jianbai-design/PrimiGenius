const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../src/renderer.js'),'utf8');
const code=source.slice(source.indexOf('let _ghRestorePromise ='),source.indexOf('function _ghHeaders('));
function fixture(localToken,vaultToken){
 const store=new Map(localToken?[['gh_oauth_token',localToken]]:[]),listeners={};let notifications=0,logins=0;
 const user={id:20,login:'reviewer',role:'reviewer'};
 const context=vm.createContext({tabs:{community:{onGithubAuthChanged:()=>notifications++},market:{onGithubAuthChanged:()=>notifications++}},
 localStorage:{getItem:key=>store.get(key)||null,setItem:(key,value)=>store.set(key,value),removeItem:key=>store.delete(key)},
 ipcRenderer:{on:(name,fn)=>listeners[name]=fn,invoke:async(name,args)=>{
  if(name==='market-session')return {token:vaultToken};
  if(name==='market-login'){logins++;vaultToken=args.token;return {user};}
  if(name==='market-list')return {user};
  if(name==='market-logout'){vaultToken='';return {ok:true};}
 }}});
 vm.runInContext(code,context);
 return {context,store,listeners,vault:()=>vaultToken,logins:()=>logins,notifications:()=>notifications};
}
(async()=>{
 const legacy=fixture('community-token','');await legacy.context._ghRestoreSharedSession();
 assert.equal(legacy.vault(),'community-token');assert.equal(legacy.context._ghGetUser().role,'reviewer');assert(legacy.notifications()>=2);
 await Promise.all([legacy.context._ghSyncMarketSession(),legacy.context._ghSyncMarketSession()]);assert.equal(legacy.logins(),1,'Do not repeatedly log into both surfaces');
 await legacy.context._ghClearAuth();assert.equal(legacy.vault(),'');assert.equal(legacy.context._ghGetToken(),'');assert.equal(legacy.context._ghGetUser(),null);
 const restored=fixture('','market-vault-token');await restored.context._ghRestoreSharedSession();assert.equal(restored.context._ghGetToken(),'market-vault-token');assert.equal(restored.context._ghGetUser().id,20);
 restored.listeners['github-session-cleared']();assert.equal(restored.context._ghGetToken(),'');assert.equal(restored.context._ghGetUser(),null);
 const stale=fixture('','');let finish;
 stale.context.ipcRenderer.invoke=async name=>name==='market-session'?new Promise(resolve=>finish=resolve):({ok:true});
 const pending=stale.context._ghRestoreSharedSession();await stale.context._ghClearAuth();finish({token:'stale'});await pending;
 assert.equal(stale.context._ghGetToken(),'','Late restore must not resurrect a signed-out account');
 console.log('PASS: community-token migration, encrypted-vault restoration, shared role, one-time synchronization, cross-surface logout and stale-restore protection');
})().catch(error=>{console.error(error);process.exitCode=1;});
