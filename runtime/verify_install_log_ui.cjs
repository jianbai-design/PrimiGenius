'use strict';
// Isolated real-renderer checks; simulated install responses never install packages.
const {app,BrowserWindow,ipcMain,session}=require('electron');
const fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'..');
const profile=path.resolve(process.argv[2]||'');
if(path.dirname(profile)!==root||!path.basename(profile).startsWith('.tmp-install-log-'))throw Error('Explicit disposable workspace profile required');
fs.mkdirSync(profile,{recursive:true});app.setPath('userData',path.join(profile,'profile'));
ipcMain.on('get-backend-port-sync',event=>event.returnValue=65534);
let storeState='done';
ipcMain.handle('qa-install-scenario',(_event,state)=>{storeState=state;});
ipcMain.handle('plugin-store-list',()=>({ok:true,plugins:[{id:'store-log-fixture',name:'store fixture',type:'linux',version:'1',status:'not_installed',category:'QA'}]}));
ipcMain.handle('plugin-update-download',async()=>{await new Promise(r=>setTimeout(r,100));return storeState==='done'?{ok:true}:{ok:false,reason:'store failure details'};});
let win;
const deadline=setTimeout(()=>finish(1,{error:'UI check timed out'}),45000);
function finish(code,result){
    clearTimeout(deadline);fs.writeFileSync(path.join(profile,'result.json'),JSON.stringify(result,null,2));
    console.log(JSON.stringify(result));if(win&&!win.isDestroyed())win.destroy();app.exit(code);
}
app.whenReady().then(async()=>{
    try{
        session.defaultSession.webRequest.onBeforeRequest({urls:['http://*/*','https://*/*']},(_details,callback)=>callback({cancel:true}));
        win=new BrowserWindow({width:1400,height:1000,show:false,webPreferences:{nodeIntegration:true,contextIsolation:false,backgroundThrottling:false}});
        await win.loadFile(path.join(root,'src/index.html'));
        const result=await win.webContents.executeJavaScript(`(async()=>{
            const assert=(ok,message)=>{if(!ok)throw Error(message);};
            const wait=async predicate=>{for(let i=0;i<250;i++){if(predicate())return;await new Promise(r=>setTimeout(r,20));}throw Error('UI condition timed out');};
            const report={result:'PASS',cases:[]};
            let scenario, next=0, activeStreams=0;
            const jobs=new Map();
            const config={id:'log-ui-fixture',name:'日志验收',type:'R_panel',packages:['fixtureA','fixtureB'],parameters:[]};
            window.EventSource=class {constructor(){activeStreams++;this.closed=false;}close(){if(!this.closed){this.closed=true;activeStreams--;}}};
            window.fetch=async(input,options={})=>{
                const url=new URL(String(input),API_BASE), route=url.pathname;
                let value={status:'success'};
                if(route==='/get-plugins')value.plugins=[config];
                else if(route==='/r/list-packages')value.packages=scenario?.completed&&scenario.state==='done'?config.packages.map(package=>({package,version:'1'})):[];
                else if(route==='/r/check-runtime')value={status:'success',ready:true,runtime_state:'ready',repo_mode:'auto'};
                else if(route==='/r/install-packages'){
                    if(scenario.initialError)return new Response(JSON.stringify({status:'error',message:'本次请求失败：测试错误详情'}));
                    const job='job-'+(++next),run='run-'+next;
                    jobs.set(job,{...scenario,run,polls:0});scenario.job=job;
                    value={status:'success',job_id:job,...(scenario.legacy?{}:{run_id:run})};
                }else if(route==='/r/install-status'){
                    const job=jobs.get(url.searchParams.get('job_id'));job.polls++;
                    const state=job.polls===1?'running':job.state;
                    scenario.completed=state!=='running';
                    const lines=Array.from({length:40},(_,i)=>'本次完整日志 '+i+' '+(i===39?'安装失败详情':'中文 🧬'));
                    value={status:'success',state,exit_code:state==='failed'?1:0,log:lines,
                        ...(job.legacy?{}:{run_id:job.run,has_more:state!=='running',events:[{seq:1,runId:job.run,line:lines[0]}]})};
                }else if(route==='/logs/history'){
                    value={status:'success',events:[{seq:1,line:'本次完整日志 0 中文 🧬'},{seq:2,line:'安装失败详情'}],nextSeq:2,has_more:false};
                    if(url.searchParams.get('runId')==='*')value.events=[{seq:3,line:url.searchParams.get('scope')==='session'?'CURRENT_SESSION_ONLY':'OLD_PLUGIN_HISTORY'}];
                }
                else if(route==='/r/list-outputs')value.files=[];
                return new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});
            };
            await init();
            currentLang='zh';
            // The ordinary analysis log keeps its existing full-history action.
            const ordinary=document.createElement('div'),box=document.createElement('div');ordinary.append(box);document.body.append(ordinary);
            attachLogArchiveLink(box,'analysis-run');assert(ordinary.querySelector('.log-archive-button'),'Analysis history action disappeared');ordinary.remove();
            showLogHistory('*');
            await wait(()=>document.querySelector('.log-history-dialog pre')?.textContent.includes('CURRENT_SESSION_ONLY'));
            let sessionDialog=document.querySelector('.log-history-dialog[open]');
            assert(sessionDialog.getAttribute('aria-label').includes('本次启动'),'Welcome history scope is not labelled');
            assert(new URL(sessionDialog.querySelector('.log-history-export').href).searchParams.get('scope')==='session','Export scope differs from welcome history');
            assert(!sessionDialog.textContent.includes('OLD_PLUGIN_HISTORY'),'Old plugin history leaked into welcome history');
            sessionDialog.close();report.welcomeSessionScope=true;
            for(const mode of ['single','batch','manager'])for(const legacy of [false,true])for(const state of ['done','failed']){
                scenario={state,legacy,completed:false};notifyStatusChanged('r-packages');
                let holder,panel,button;
                if(mode==='manager'){
                    if(tabs['sys-r-packages'])closeTab('sys-r-packages');
                    openRPackagesTab();holder=document.getElementById('view-sys-r-packages')||document.querySelector('.rpkg-install-log')?.closest('.page-view');
                    await wait(()=>document.querySelector('.rpkg-install-missing-btn'));
                    panel=document.querySelector('.rpkg-install-log');button=document.querySelector('.rpkg-install-missing-btn');
                }else{
                    holder=document.createElement('div');holder.className='page-view active';document.body.append(holder);
                    renderRPanelUI({config:{...config,id:config.id+'-'+mode},state:{},tabId:'qa'},holder);
                    await wait(()=>holder.querySelector('.r-pkg-btn')?.dataset.busy==='0');
                    panel=holder.querySelector('.r-install-log');button=holder.querySelector(mode==='single'?'.r-pkg-btn':'.r-install-all-btn');
                }
                assert(button,'Install action missing: '+mode);button.click();
                await wait(()=>scenario.job&&jobs.get(scenario.job).polls>=1);
                assert(!panel.querySelector('.log-archive-button'),'Transient installation shows full-log button');
                await wait(()=>scenario.completed);
                const dialog=document.querySelector('.log-history-dialog[open]');
                if(state==='failed'){
                    assert(dialog,'Failure did not open full logs: '+mode+'/'+legacy);
                    await wait(()=>dialog.querySelector('pre').textContent.includes('本次完整日志 0'));
                    assert(dialog.querySelector('pre').textContent.includes('安装失败详情'),'Failure details missing');
                    assert(dialog.querySelector('.log-history-header .log-history-export'),'Export is not in modal header');
                    if(legacy)assert(dialog.querySelector('.log-history-export').href.startsWith('blob:'),'Legacy error log export missing');
                    dialog.close();
                }else assert(!dialog,'Successful installation opened failure dialog');
                assert(panel.style.display==='none','Terminal install did not close immediately');
                assert(activeStreams===0,'Terminal installation retained SSE connection');
                report.cases.push({mode,legacy,state});
                if(mode==='manager')closeTab('sys-r-packages');else{viewStatus.dispose(holder);holder.remove();}
            }
            for(const state of ['done','failed']){
                await ipcRenderer.invoke('qa-install-scenario',state);
                openPluginStoreTab();
                await wait(()=>document.querySelector('.plugin-store-action'));
                document.querySelector('.plugin-store-action').click();
                assert(!document.querySelector('.plugin-store-install-log .log-archive-button'),'Store transient log shows history action');
                await wait(()=>!document.querySelector('.plugin-store-install-log'));
                const dialog=document.querySelector('.log-history-dialog[open]');
                if(state==='failed'){
                    assert(dialog?.querySelector('pre').textContent.includes('store failure details'),'Store failure details not opened');dialog.close();
                }else assert(!dialog,'Store success opened failure history');
                await new Promise(r=>setTimeout(r,50));closeTab('sys-plugin-store');report.cases.push({mode:'store',state});
            }
            // Failure before a job/run ID exists must still expose and export details.
            scenario={state:'failed',initialError:true};notifyStatusChanged('r-packages');
            const holder=document.createElement('div');holder.className='page-view active';document.body.append(holder);
            renderRPanelUI({config,state:{}},holder);await wait(()=>holder.querySelector('.r-pkg-btn')?.dataset.busy==='0');
            holder.querySelector('.r-pkg-btn').click();
            await wait(()=>document.querySelector('.log-history-dialog[open]'));
            const dialog=document.querySelector('.log-history-dialog[open]');
            assert(dialog.querySelector('pre').textContent.includes('本次请求失败'),'Initial error details missing');dialog.close();
            viewStatus.dispose(holder);holder.remove();report.initialError=true;
            return report;
        })()`);
        finish(0,result);
    }catch(error){finish(1,{result:'FAIL',error:error.stack||String(error)});}
});
