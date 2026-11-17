'use strict';
const {ipcRenderer}=require('electron');
const {units}=require('./vector-codecs');
const ns='http://www.w3.org/2000/svg';
const drawable='g,rect,circle,ellipse,line,path,polygon,polyline,text,image,use';
const number=(value,fallback=0)=>Number.isFinite(Number(value))?Number(value):fallback;
const finite=value=>Number.isFinite(value);
const makeSvg=(tag,attrs={})=>{const node=document.createElementNS(ns,tag);for(const [key,value]of Object.entries(attrs))node.setAttribute(key,String(value));return node;};
const matrixText=m=>`matrix(${m.a},${m.b},${m.c},${m.d},${m.e},${m.f})`;

exports.mount=function(h) {
    const zh=h.lang==='zh',tr=(cn,en)=>zh?cn:en;
    const initial=h.snapshot(),unitFactor=unit=>units[unit]*(h.unitScale?.()||1);
    const state={panel:'',tool:'select',unit:'mm',rulers:false,isolation:null,dirty:false,revision:0,closed:false,
        saved:initial,current:initial,labels:[tr('打开','Open')],position:0};
    const bar=document.createElement('div');bar.className='ve-advanced-controls';
    bar.innerHTML=`<details class="ve-advanced-menu"><summary aria-haspopup="menu" aria-expanded="false">${tr('高级功能','Advanced')} ▾</summary><div class="ve-advanced-items" role="menu">`+
        [['transform','变换','Transform'],['history','历史','History']]
            .map(([id,cn,en])=>`<button type="button" role="menuitem" data-panel="${id}">${tr(cn,en)}</button>`).join('')+
        `<button type="button" role="menuitemcheckbox" data-command="rulers" aria-checked="false">${tr('标尺','Rulers')}</button></div></details><span class="ve-dirty-indicator" role="status" hidden></span>`;
    h.overlay.querySelector('.r-ve-toolbar-right').prepend(bar);
    const advanced=bar.querySelector('details'),summary=bar.querySelector('summary');
    const panel=document.createElement('aside');panel.className='ve-workbench-panel';panel.hidden=true;
    h.canvas.appendChild(panel);
    const adornments=makeSvg('g',{'class':'ve-workbench-overlays','pointer-events':'none'});
    h.draw().appendChild(adornments);
    let renderFrame=0,recoveryTimer=0,recoveryWrite=Promise.resolve(),recoveryRevision=-1,tree=[],treeSvg=null,treeDirty=true;
    let snapTargets=null,nodeData=null,nodeTarget=null,nodePick=null,nodeDrag=null,guideDrag=null,rulerCanvas=null,selectedGuide=-1;
    let jobToken=0,geometryBusy=false,nodeFrame=0;
    let noticeTimer=0;
    let penDraft=null,penDrag=null,penHover=null,penFrame=0,nodeLoading=Promise.resolve(),changingNodes=false;
    const anchorTools=new Set(['anchor','add-anchor','delete-anchor']);
    let recoveryCheckedKey=null,cancelRecoveryPrompt=null;
    const on=(target,event,fn,options={})=>target.addEventListener(event,fn,{...options,signal:h.signal});
    const corners=require('./vector-corners').mount({...h,tool:()=>state.tool});
    const penButton=h.overlay.querySelector('.ve-pen-tool'),penMenu=h.overlay.querySelector('.ve-pen-menu');
    const penTools=[['bezier','钢笔','Pen','fa-pen-nib'],['curvature','曲率钢笔','Curvature pen','fa-bezier-curve'],['add-anchor','添加锚点','Add anchor','fa-plus'],['delete-anchor','删除锚点','Delete anchor','fa-minus']];
    penMenu.innerHTML=penTools.map(([id,cn,en,icon])=>`<button type="button" role="menuitem" data-path-tool="${id}"><i class="fas ${icon}" aria-hidden="true"></i>${tr(cn,en)}</button>`).join('');
    function closePenMenu(){penMenu.hidden=true;penButton.setAttribute('aria-expanded','false');}
    function choosePen(tool){const item=penTools.find(item=>item[0]===tool);if(!item)return;closePenMenu();penButton.dataset.tool=tool;penButton.innerHTML=`<i class="fas ${item[3]}" aria-hidden="true"></i>`;penButton.title=tr(item[1]+'；右键展开工具集',item[2]+'; right-click for tools');h.activateTool(tool,penButton);}
    on(penButton,'contextmenu',event=>{event.preventDefault();event.stopPropagation();closeMenu();penMenu.hidden=!penMenu.hidden;penButton.setAttribute('aria-expanded',String(!penMenu.hidden));if(!penMenu.hidden)penMenu.querySelector('button').focus();});
    penMenu.querySelectorAll('button').forEach(b=>b.onclick=()=>choosePen(b.dataset.pathTool));
    on(document,'mousedown',event=>{if(!event.target.closest('.ve-pen-group'))closePenMenu();},{capture:true});
    on(penMenu,'keydown',event=>{const items=[...penMenu.querySelectorAll('button')],at=items.indexOf(document.activeElement);if(['ArrowUp','ArrowDown'].includes(event.key)){event.preventDefault();items[(at+(event.key==='ArrowUp'?items.length-1:1))%items.length].focus();}});
    function report(error){
        if(state.closed)return;const message=String(error.message||error);h.notify(message);
        let note=h.canvas.querySelector('.ve-editor-notice');if(!note){note=document.createElement('div');note.className='ve-editor-notice';note.setAttribute('role','status');h.canvas.appendChild(note);}
        note.textContent=message;note.title=message;clearTimeout(noticeTimer);noticeTimer=setTimeout(()=>note.remove(),7000);
    }
    function requestRender(){if(!renderFrame&&!state.closed)renderFrame=requestAnimationFrame(()=>{renderFrame=0;render();});}
    function closeMenu(){advanced.open=false;summary.setAttribute('aria-expanded','false');}
    function setPanel(name){closeMenu();state.panel=state.panel===name?'':name;panel.dataset.panel=state.panel;panel.hidden=!state.panel;nodeData=null;nodeTarget=null;nodePick=null;jobToken++;requestRender();paint();}
    bar.querySelectorAll('[data-panel]').forEach(button=>button.onclick=()=>setPanel(button.dataset.panel));
    bar.querySelector('[data-command="rulers"]').onclick=event=>{state.rulers=!state.rulers;event.currentTarget.setAttribute('aria-checked',state.rulers);closeMenu();paintRulers();};
    on(advanced,'toggle',()=>summary.setAttribute('aria-expanded',String(advanced.open)));
    on(document,'mousedown',event=>{if(!bar.contains(event.target))closeMenu();},{capture:true});
    on(document,'keydown',event=>{if(advanced.open&&event.key==='Escape'){event.preventDefault();event.stopImmediatePropagation();closeMenu();summary.focus();}},{capture:true});
    on(bar,'keydown',event=>{
        if(!['ArrowDown','ArrowUp','Home','End'].includes(event.key))return;
        event.preventDefault();advanced.open=true;const items=[...advanced.querySelectorAll('button')],index=items.indexOf(document.activeElement);
        const next=event.key==='Home'?0:event.key==='End'?items.length-1:event.key==='ArrowDown'?(index+1)%items.length:(index<=0?items.length:index)-1;
        items[next].focus();
    });
    function nodeLabel(el){return el.getAttribute('data-ve-name')||el.getAttribute('aria-label')||el.id||el.tagName;}
    function selectedRoots(){return h.roots(h.selected()).filter(el=>h.svg()?.contains(el)&&el.getAttribute('data-role')!=='background');}
    function modifiable(el){return el.getAttribute('data-locked')!=='true'&&!el.closest('[data-locked="true"]');}
    function mutate(label,fn){try{fn();h.save(label);}catch(error){report(error);}}
    function heading(title){panel.replaceChildren();const top=document.createElement('header');top.textContent=title;const close=document.createElement('button');close.textContent='×';close.setAttribute('aria-label',tr('关闭面板','Close panel'));close.onclick=()=>setPanel(state.panel);top.appendChild(close);panel.appendChild(top);}
    function button(label,fn,parent=panel){const node=document.createElement('button');node.type='button';node.textContent=label;node.onclick=()=>{if(node.disabled)return;node.disabled=true;node.setAttribute('aria-busy','true');Promise.resolve().then(fn).catch(report).finally(()=>{node.disabled=false;node.removeAttribute('aria-busy');});};parent.appendChild(node);return node;}
    function hint(text){const p=document.createElement('p');p.className='ve-panel-hint';p.textContent=text;panel.appendChild(p);return p;}
    function field(label,value,type='number',parent=panel){const wrap=document.createElement('label');wrap.textContent=label;const input=document.createElement('input');input.type=type;input.value=value;if(type==='number')input.step='any';wrap.appendChild(input);parent.appendChild(wrap);return input;}
    function selectField(label,options,value,parent=panel){const wrap=document.createElement('label');wrap.textContent=label;const input=document.createElement('select');for(const [key,text]of options){const option=document.createElement('option');option.value=key;option.textContent=text;input.appendChild(option);}input.value=value;wrap.appendChild(input);parent.appendChild(wrap);return input;}
    function grid(){const element=document.createElement('div');element.className='ve-panel-grid';panel.appendChild(element);return element;}
    function unitField(parent=panel){const unit=selectField(tr('单位','Unit'),[['mm','mm'],['pt','pt'],['px','px']],state.unit,parent);unit.onchange=()=>{state.unit=unit.value;requestRender();paintRulers();};return unit;}
    function combinedBounds(els=selectedRoots()){
        let x=Infinity,y=Infinity,right=-Infinity,bottom=-Infinity;
        for(const el of els){const b=h.bounds(el);if(!b)continue;x=Math.min(x,b.x);y=Math.min(y,b.y);right=Math.max(right,b.x+b.w);bottom=Math.max(bottom,b.y+b.h);}
        return finite(x)?{x,y,w:right-x,h:bottom-y}:null;
    }
    function transformRoots(els,matrix){
        const rootCTM=h.svg().getScreenCTM();
        for(const el of els){if(!modifiable(el))continue;const parent=el.parentNode.getScreenCTM(),toParent=parent.inverse().multiply(rootCTM);
            const local=toParent.multiply(matrix).multiply(toParent.inverse());
            el.setAttribute('transform',matrixText(local)+' '+(el.getAttribute('transform')||''));h.bake(el);}
    }
    const rootMatrix=()=>h.svg().createSVGMatrix();
    function translate(els,x,y){transformRoots(els,rootMatrix().translate(x,y));}
    function applyBounds(values,linked){
        const els=selectedRoots(),b=combinedBounds(els);if(!b)return;
        const w=values.w??b.w,hh=values.h??b.h;
        let sx=b.w>1e-8?w/b.w:1,sy=b.h>1e-8?hh/b.h:1;
        if(linked){if(values.w!==undefined)sy=sx;else if(values.h!==undefined)sx=sy;}
        if(![sx,sy,values.x??b.x,values.y??b.y].every(finite)||sx<=0||sy<=0)throw Error(tr('尺寸必须为正数','Size must be positive'));
        transformRoots(els,rootMatrix().translate(values.x??b.x,values.y??b.y).scaleNonUniform(sx,sy).translate(-b.x,-b.y));
        h.select(els);
    }
    function renderObjects(){
        heading(tr('对象与图层','Objects and layers'));
        const search=field(tr('搜索','Search'),'','search');search.placeholder=tr('名称或类型','Name or type');
        const list=document.createElement('div');list.className='ve-object-list';list.tabIndex=0;panel.appendChild(list);
        if(treeDirty||treeSvg!==h.svg()){
            tree=[];treeSvg=h.svg();treeDirty=false;
            const walker=document.createTreeWalker(treeSvg,NodeFilter.SHOW_ELEMENT,{acceptNode:el=>{
                if(el.matches('defs,clipPath,mask,pattern,marker,symbol,style,metadata,title,desc'))return NodeFilter.FILTER_REJECT;
                if(el.getAttribute('data-role')==='background'||el.matches('tspan'))return NodeFilter.FILTER_REJECT;
                return el.matches(drawable)?NodeFilter.FILTER_ACCEPT:NodeFilter.FILTER_SKIP;
            }});
            let el;while((el=walker.nextNode())){let depth=0,p=el.parentElement;while(p&&p!==treeSvg){depth++;p=p.parentElement;}tree.push({el,depth});}
        }
        let filtered=tree;const rowHeight=30;
        const fill=()=>{
            const scroll=list.scrollTop,start=Math.max(0,Math.floor(scroll/rowHeight)-4),end=Math.min(filtered.length,start+26);
            list.replaceChildren();const spacer=document.createElement('div');spacer.style.height=filtered.length*rowHeight+'px';spacer.style.position='relative';list.appendChild(spacer);
            for(let index=start;index<end;index++){
                const {el,depth}=filtered[index];const row=document.createElement('div');row.className='ve-object-row';row._veObject=el;row.classList.toggle('selected',h.selected().includes(el));
                row.style.cssText=`position:absolute;top:${index*rowHeight}px;height:${rowHeight}px;left:0;right:0;padding-left:${Math.min(depth,8)*9}px`;
                const hidden=el.getAttribute('visibility')==='hidden'||getComputedStyle(el).display==='none';
                button(hidden?'○':'●',()=>mutate(tr('显示／隐藏','Visibility'),()=>{if(hidden){el.removeAttribute('visibility');el.style.setProperty('display','inline','important');}else el.setAttribute('visibility','hidden');}),row).title=tr('显示／隐藏','Visibility');
                button(el.getAttribute('data-locked')==='true'?'🔒':'◇',()=>mutate(tr('锁定／解锁','Lock'),()=>el.setAttribute('data-locked',el.getAttribute('data-locked')==='true'?'false':'true')),row).title=tr('锁定／解锁','Lock');
                const name=document.createElement('button');name.className='ve-object-name';name.textContent=nodeLabel(el);name.title=name.textContent;
                name.onclick=e=>h.select(e.shiftKey?[...new Set([...h.selected(),el])]:[el]);
                name.ondblclick=()=>{
                    const edit=document.createElement('input');edit.type='text';edit.value=nodeLabel(el);edit.maxLength=256;edit.setAttribute('aria-label',tr('对象名称','Object name'));name.replaceWith(edit);edit.focus();edit.select();let finished=false;
                    const finish=commit=>{if(finished)return;finished=true;if(commit)mutate(tr('重命名','Rename'),()=>el.setAttribute('data-ve-name',edit.value.slice(0,256)));requestRender();};
                    edit.onblur=()=>finish(true);edit.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();finish(true);}else if(e.key==='Escape'){e.preventDefault();e.stopPropagation();finish(false);}};
                };row.appendChild(name);spacer.appendChild(row);
            }
        };
        list.onscroll=fill;search.oninput=()=>{const q=search.value.toLowerCase();filtered=q?tree.filter(({el})=>nodeLabel(el).toLowerCase().includes(q)):tree;list.scrollTop=0;fill();};fill();
        hint(tr(`${tree.length} 个对象；列表按需显示。双击名称重命名。`,`${tree.length} objects; rows are virtualized. Double-click a name to rename.`));
        const actions=grid();
        button(tr('进入分组','Enter group'),enterGroup,actions);
        button(tr('退出分组','Exit group'),exitGroup,actions);
        button(tr('解除裁剪','Release clipping'),releaseClipping,actions);
        if(state.isolation)hint(tr('正在隔离编辑：','Isolation: ')+nodeLabel(state.isolation));
    }
    function enterGroup(){const el=h.selected()[0];if(h.selected().length===1&&el?.tagName.toLowerCase()==='g'){state.isolation=el;h.select([]);requestRender();paint();}}
    function exitGroup(){const old=state.isolation;state.isolation=null;h.select(old?[old]:[]);requestRender();paint();}
    function releaseClipping(){mutate(tr('解除裁剪','Release clipping'),()=>{
        for(const el of selectedRoots().filter(modifiable))for(const node of [el,...el.querySelectorAll('[clip-path],[class],[style*="clip-path"]')]){
            if(node.closest('defs,clipPath,mask')||getComputedStyle(node).clipPath==='none'&&!node.hasAttribute('data-ve-artboard-clip'))continue;
            node.removeAttribute('clip-path');node.removeAttribute('data-ve-artboard-clip');node.style.setProperty('clip-path','none','important');
        }
    });}
    function contextItems(){return [
        {icon:'fa-list',label:tr('对象与图层…','Objects and layers…'),action:'objects',run:()=>setPanel('objects')},
        {icon:'fa-sign-in-alt',label:tr('进入分组','Enter group'),action:'enter-group',disabled:h.selected().length!==1||selectedRoots()[0]?.tagName.toLowerCase()!=='g',run:enterGroup},
        {icon:'fa-sign-out-alt',label:tr('退出分组','Exit group'),action:'exit-group',disabled:!state.isolation,run:exitGroup},
        {icon:'fa-crop-alt',label:tr('解除裁剪','Release clipping'),action:'release-clipping',disabled:!selectedRoots().some(modifiable),run:releaseClipping}
    ];}
    function renderTransform(){
        heading(tr('精确变换','Transform'));const b=combinedBounds();if(!b){hint(tr('请选择对象','Select objects'));return;}
        const fields=grid();unitField(fields);const link=selectField(tr('比例','Proportions'),[['yes',tr('锁定','Linked')],['no',tr('自由','Free')]],'yes',fields);
        const controls={};for(const [key,label]of [['x','X'],['y','Y'],['w',tr('宽','Width')],['h',tr('高','Height')]])controls[key]=field(label,+(b[key]/unitFactor(state.unit)).toFixed(4),'number',fields);
        const rotation=field(tr('旋转角度（增量）','Rotate by (°)'),0,'number',fields);
        for(const key of Object.keys(controls))controls[key].onchange=()=>mutate(tr('精确变换','Transform'),()=>applyBounds({[key]:number(controls[key].value)*unitFactor(state.unit)},link.value==='yes'));
        rotation.onchange=()=>mutate(tr('旋转','Rotate'),()=>{const angle=number(rotation.value);transformRoots(selectedRoots(),rootMatrix().translate(b.x+b.w/2,b.y+b.h/2).rotate(angle).translate(-b.x-b.w/2,-b.y-b.h/2));h.select(selectedRoots());});
        hint(tr('X/Y 为图形外接框左上角；旋转围绕选区中心。','X/Y use the top-left of visual bounds; rotation uses the selection center.'));
        renderStroke();
    }
    function renderStroke(){
        const el=selectedRoots()[0];if(!el)return;const g=grid();
        const dash=field(tr('虚线（如 6 3）','Dashes (e.g. 6 3)'),el.getAttribute('stroke-dasharray')||'','text',g);
        const cap=selectField(tr('端点','Line cap'),[['butt',tr('平头','Butt')],['round',tr('圆头','Round')],['square',tr('方头','Square')]],el.getAttribute('stroke-linecap')||'butt',g);
        const arrows=selectField(tr('箭头','Arrowheads'),[['none',tr('无','None')],['end',tr('末端','End')],['both',tr('两端','Both')]],el.hasAttribute('marker-start')?'both':el.hasAttribute('marker-end')?'end':'none',g);
        dash.onchange=()=>{if(!/^\s*(?:none|\d+(?:\.\d+)?(?:[ ,]+\d+(?:\.\d+)?)*)?\s*$/.test(dash.value))return report(tr('请输入非负虚线长度','Enter nonnegative dash lengths'));mutate(tr('虚线','Dashes'),()=>selectedRoots().forEach(n=>{n.setAttribute('stroke-dasharray',dash.value||'none');n.style.setProperty('stroke-dasharray',dash.value||'none');}));};
        cap.onchange=()=>mutate(tr('端点','Line cap'),()=>selectedRoots().forEach(n=>{n.setAttribute('stroke-linecap',cap.value);n.style.setProperty('stroke-linecap',cap.value);}));
        arrows.onchange=()=>mutate(tr('箭头','Arrowheads'),()=>{
            let defs=h.svg().querySelector(':scope>defs');if(!defs){defs=makeSvg('defs');h.svg().prepend(defs);}
            for(const n of selectedRoots()){
                for(const side of ['start','end']){
                    const id='ve-arrow-'+side+'-'+crypto.randomUUID();
                    if(arrows.value==='none'||side==='start'&&arrows.value!=='both'){n.removeAttribute('marker-'+side);n.style.removeProperty('marker-'+side);continue;}
                    const marker=makeSvg('marker',{id,viewBox:'0 0 10 10',refX:side==='start'?1:9,refY:5,markerWidth:5,markerHeight:5,orient:'auto',markerUnits:'strokeWidth'});
                    marker.appendChild(makeSvg('path',{d:side==='start'?'M10 0L0 5L10 10Z':'M0 0L10 5L0 10Z',fill:getComputedStyle(n).stroke||'#000'}));defs.appendChild(marker);n.setAttribute('marker-'+side,`url(#${id})`);n.style.setProperty('marker-'+side,`url(#${id})`);
                }
            }
        });
    }
    function render(){
        if(state.closed)return;
        bar.querySelector('.ve-dirty-indicator').textContent=state.dirty?tr('未保存','Unsaved'):tr('已保存','Saved');
        bar.querySelectorAll('[data-panel]').forEach(b=>b.classList.toggle('active',b.dataset.panel===state.panel));
        if(state.panel&&!h.svg()){heading(tr('正在载入图形','Loading figure'));return;}
        if(state.panel==='objects')renderObjects();else if(state.panel==='transform')renderTransform();else if(state.panel==='nodes')renderNodes();else if(state.panel==='history')renderHistory();
    }
    function changedSelection(){
        selectedGuide=-1;
        snapTargets=null;jobToken++;nodeData=null;nodeTarget=null;nodePick=null;
        if(state.panel==='objects'&&!treeDirty&&treeSvg===h.svg()){
            const chosen=new Set(h.selected());panel.querySelectorAll('.ve-object-row').forEach(row=>row.classList.toggle('selected',chosen.has(row._veObject)));
        }else requestRender();
        paint();
        if(anchorTools.has(state.tool)&&!changingNodes)loadSelectedNodes();
    }
    function loadSelectedNodes(){const el=selectedRoots()[0];if(!geometryBusy&&selectedRoots().length===1&&el&&modifiable(el)&&h.pathData(el))nodeLoading=beginNodes(el).catch(report);return nodeLoading;}
    function setTool(tool){
        selectedGuide=-1;
        corners.finish();
        finishPen();
        state.tool=tool;closePenMenu();nodeDrag=null;nodeData=null;nodeTarget=null;nodePick=null;jobToken++;
        if(anchorTools.has(tool)){state.panel='nodes';panel.dataset.panel='nodes';panel.hidden=false;loadSelectedNodes();}
        else if(state.panel==='nodes'){state.panel='';panel.dataset.panel='';panel.hidden=true;}
        requestRender();paint();
    }
    function committed(svg,label=tr('编辑','Edit')){
        if(svg===state.current)return;
        state.current=svg;state.dirty=svg!==state.saved;state.revision++;treeDirty=true;snapTargets=null;
        state.labels=state.labels.slice(0,state.position+1);state.labels.push(label);if(state.labels.length>30)state.labels.shift();state.position=state.labels.length-1;
        queueRecovery();requestRender();paint();
    }
    function replaced(svg,direction=0,fresh=false){
        if(fresh)state.saved=svg;
        if(fresh)recoveryRevision=-1;
        state.current=svg;state.dirty=svg!==state.saved;state.revision++;treeDirty=true;treeSvg=null;tree=[];snapTargets=null;state.isolation=null;
        if(direction)state.position=Math.max(0,Math.min(state.labels.length-1,state.position+direction));
        else{state.labels=[fresh?tr('载入','Load'):tr('恢复','Restore')];state.position=0;}
        changedSelection();queueRecovery();
        if(fresh)startRecoveryCheck();
    }
    function renderHistory(){heading(tr('操作历史','History'));hint(tr('点击记录回到该状态；最多保留 30 步。','Choose a state to undo/redo; up to 30 states are retained.'));state.labels.forEach((label,index)=>{const b=button(`${index===state.position?'● ':'○ '}${label}`,()=>{
            const steps=index-state.position;for(let i=0;i<Math.abs(steps);i++)steps<0?h.undo():h.redo();
        });b.className='ve-history-row';});}
    function queueRecovery(){
        if(!state.dirty){clearTimeout(recoveryTimer);recoveryTimer=0;if(recoveryRevision>=0){const key=h.key;recoveryRevision=-1;recoveryWrite=recoveryWrite.catch(()=>{}).then(()=>ipcRenderer.invoke('vector-recovery',{key,action:'clear'}));}return;}
        if(!recoveryTimer)recoveryTimer=setTimeout(()=>{recoveryTimer=0;flushRecovery().catch(report);},8000);
    }
    function flushRecovery(){
        if(!state.dirty||state.closed||recoveryRevision===state.revision)return recoveryWrite;
        const svg=state.current,revision=state.revision,key=h.key;
        recoveryWrite=recoveryWrite.catch(()=>{}).then(()=>ipcRenderer.invoke('vector-recovery',{key,action:'write',svg})).then(()=>{if(h.key===key)recoveryRevision=revision;});return recoveryWrite;
    }
    async function markSaved(svg,key=h.key){
        if(key!==h.key)return;
        state.saved=svg;state.dirty=state.current!==svg;clearTimeout(recoveryTimer);recoveryTimer=0;
        await recoveryWrite.catch(()=>{});await ipcRenderer.invoke('vector-recovery',{key,action:'clear'});
        recoveryRevision=-1;queueRecovery();requestRender();
    }
    async function save(){
        if(!h.svg())return false;
        finishPen();h.flushText();const svg=h.snapshot(),key=h.key;
        const result=await ipcRenderer.invoke('vector-save-file',{name:h.filename.replace(/\.[^.]+$/,'')+'.svg',bytes:new TextEncoder().encode(svg)});
        if(result)await markSaved(svg,key);
        return !!result;
    }
    function confirm(message,labels,onReady){
        return new Promise(resolve=>{
            const dialog=document.createElement('div');dialog.className='ve-confirm';dialog.setAttribute('role','dialog');dialog.setAttribute('aria-modal','true');
            const box=document.createElement('div'),text=document.createElement('p'),actions=document.createElement('div');
            text.textContent=message;text.id='ve-confirm-'+crypto.randomUUID();dialog.setAttribute('aria-labelledby',text.id);actions.className='ve-confirm-actions';
            const previousFocus=document.activeElement;let settled=false;
            const finish=choice=>{if(settled)return;settled=true;h.signal.removeEventListener('abort',abort);dialog.remove();if(previousFocus?.isConnected)previousFocus.focus({preventScroll:true});resolve(choice);};
            const abort=()=>finish(labels.length-1);
            const close=button('×',()=>{},box);close.className='ve-confirm-close';close.setAttribute('aria-label',tr('关闭提示','Close dialog'));close.title=tr('关闭提示','Close dialog');close.onclick=()=>finish(labels.length-1);
            box.append(text,actions);dialog.appendChild(box);
            labels.forEach((label,i)=>{const b=button(label,()=>{},actions);b.onclick=()=>finish(i);b.dataset.choice=String(i);});
            dialog.addEventListener('keydown',event=>{
                if(event.key==='Escape'){event.preventDefault();event.stopPropagation();finish(labels.length-1);}
                else if(event.key==='Tab'){
                    const buttons=[...dialog.querySelectorAll('button')],first=buttons[0],last=buttons[buttons.length-1];
                    if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
                    else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
                }
            });
            h.overlay.appendChild(dialog);actions.querySelector('button')?.focus();
            h.signal.addEventListener('abort',abort,{once:true});
            onReady?.(abort);
        });
    }
    async function requestClose(){
        corners.finish();finishPen();h.flushText();if(!state.dirty)return true;
        const choice=await confirm(tr('图形有未保存的修改。','The figure has unsaved changes.'),[tr('保存并关闭','Save and close'),tr('放弃修改','Discard'),tr('取消','Cancel')]);
        if(choice===2)return false;if(choice===0&&!(await save()))return false;
        clearTimeout(recoveryTimer);await recoveryWrite.catch(()=>{});await ipcRenderer.invoke('vector-recovery',{key:h.key,action:'clear'}).catch(report);return true;
    }
    async function beforeReplace(){
        cancelRecoveryPrompt?.();cancelRecoveryPrompt=null;corners.finish();finishPen();
        if(!state.dirty)return true;
        const choice=await confirm(tr('R 产生了新图形。是否保存当前编辑，再载入新结果？','R produced a new figure. Save edits before loading it?'),[tr('保存并载入','Save and load'),tr('放弃并载入','Discard and load'),tr('保留当前编辑','Keep editing')]);
        if(choice===2)return false;if(choice===0&&!(await save()))return false;
        clearTimeout(recoveryTimer);recoveryTimer=0;await recoveryWrite.catch(()=>{});await ipcRenderer.invoke('vector-recovery',{key:h.key,action:'clear'});return true;
    }
    async function checkRecovery(){
        const revision=state.revision,key=h.key;
        try{const copy=await ipcRenderer.invoke('vector-recovery',{key,action:'read'});if(!copy||state.closed||revision!==state.revision||key!==h.key)return;
            if(copy.svg===state.current){await ipcRenderer.invoke('vector-recovery',{key,action:'clear'});return;}
            const choice=await confirm(tr('发现上次未保存的图形，可恢复继续编辑。','An unsaved recovery copy is available.'),[tr('恢复','Restore'),tr('丢弃副本','Discard copy'),tr('稍后','Later')],cancel=>cancelRecoveryPrompt=cancel);
            cancelRecoveryPrompt=null;if(state.closed||key!==h.key||revision!==state.revision)return;
            if(choice===0&&!state.closed){h.restore(copy.svg);replaced(h.snapshot());state.dirty=true;}
            else if(choice===1)await ipcRenderer.invoke('vector-recovery',{key,action:'clear'});
        }catch(error){h.notify(tr('恢复副本暂不可用：','Recovery unavailable: ')+error.message);}
    }
    function startRecoveryCheck(){if(recoveryCheckedKey!==h.key&&h.svg()){recoveryCheckedKey=h.key;checkRecovery();}}
    function dispose(){
        corners.dispose();
        state.closed=true;clearTimeout(recoveryTimer);clearTimeout(noticeTimer);cancelAnimationFrame(renderFrame);cancelAnimationFrame(nodeFrame);jobToken++;
        cancelAnimationFrame(penFrame);penDraft=null;penDrag=null;penHover=null;tree=[];snapTargets=null;nodeData=null;nodeTarget=null;nodeDrag=null;adornments.remove();rulerCanvas?.remove();panel.remove();bar.remove();
    }

    function renderNodes(){
        heading(tr('路径与贝塞尔节点','Paths and Bézier nodes'));
        const selected=selectedRoots();if(selected.length!==1){hint(tr('请选择一个路径或基本形状','Select one path or basic shape'));return;}
        const el=selected[0];if(!h.pathData(el)){hint(tr('文字、图像和分组不能直接编辑节点','Text, images and groups do not expose path nodes'));return;}
        if(!modifiable(el)){hint(tr('请先解锁对象','Unlock the object first'));return;}
        if(!nodeData||nodeTarget!==el){button(tr('编辑节点','Edit nodes'),()=>beginNodes(el));hint(tr('节点按需载入。基本形状会转为路径，可撤销。','Nodes load on demand. Basic shapes become editable paths; Undo restores them.'));}
        else {
            hint(tr('拖动方点移动节点，拖动圆点调整曲线；按 Esc 退出。','Drag square anchors or circular curve handles; Esc exits.'));
            const actions=grid();button(tr('增加节点','Add anchor'),()=>editNode('add'),actions);button(tr('删除节点','Delete anchor'),()=>editNode('delete'),actions);
            button(tr('平滑节点','Smooth'),()=>editNode('smooth'),actions);button(tr('尖角节点','Corner'),()=>editNode('corner'),actions);
        }
        const tolerance=field(tr('简化容差（px）','Simplify tolerance (px)'),1);
        button(tr('简化路径','Simplify path'),async()=>{
            if(geometryBusy)return;geometryBusy=true;const token=++jobToken,d=h.pathData(el),revision=state.revision;
            try{const result=await h.jobs.run('simplify',{d,tolerance:number(tolerance.value,1)*unitFactor('px')});
                if(state.closed||token!==jobToken||revision!==state.revision||!h.svg().contains(el))return;
                mutate(tr('简化路径','Simplify path'),()=>{const target=toPath(el);target.setAttribute('d',result.d);h.select([target]);});
            }finally{geometryBusy=false;}
        });
    }
    function toPath(el){
        if(el.tagName.toLowerCase()==='path')return el;
        const d=h.pathData(el);if(!d)throw Error('Unsupported shape');
        const path=makeSvg('path',{d});for(const attr of el.attributes)if(!/^(x|y|x1|y1|x2|y2|width|height|cx|cy|r|rx|ry|points)$/i.test(attr.name))path.setAttribute(attr.name,attr.value);
        el.replaceWith(path);return path;
    }
    async function beginNodes(el){
        if(geometryBusy)return;geometryBusy=true;const token=++jobToken,revision=state.revision;
        try{
            const result=await h.jobs.run('nodes',{d:h.pathData(el)});
            if(state.closed||token!==jobToken||revision!==state.revision||!h.svg().contains(el))return;
            const count=result.paths.reduce((n,p)=>n+p.segments.length,0);
            if(count>4000)throw Error(tr('节点超过 4000，请先简化路径以保持流畅。','More than 4,000 anchors; simplify the path first.'));
            h.select([el]);nodeTarget=el;nodeData=result.paths;nodePick=null;requestRender();paint();return el;
        }finally{geometryBusy=false;if(!state.closed&&anchorTools.has(state.tool)&&token!==jobToken&&h.selected()[0]!==nodeTarget)loadSelectedNodes();}
    }
    function pathString(paths){
        const point=p=>p.map(v=>+v.toFixed(6)).join(' '),add=(a,b)=>[a[0]+b[0],a[1]+b[1]];
        return paths.map(path=>{if(!path.segments.length)return '';let d='M'+point(path.segments[0].p);const n=path.segments.length;
            for(let i=1;i<(path.closed?n+1:n);i++){const a=path.segments[(i-1)%n],b=path.segments[i%n];
                d+=(a.o.some(v=>v!==0)||b.i.some(v=>v!==0))?'C'+point(add(a.p,a.o))+' '+point(add(b.p,b.i))+' '+point(b.p):'L'+point(b.p);
            }return d+(path.closed?'Z':'');}).join('');
    }
    function curveHandles(){
        const path=nodeData[0],all=path.segments,n=all.length;
        all.forEach((v,index)=>{
            if(v.corner||n<2){v.i=[0,0];v.o=[0,0];return;}
            const prev=all[index?index-1:path.closed?n-1:0].p,next=all[index<n-1?index+1:path.closed?0:n-1].p;
            const divisor=!path.closed&&(index===0||index===n-1)?3:6;
            v.o=[(next[0]-prev[0])/divisor,(next[1]-prev[1])/divisor];v.i=v.o.map(x=>-x);
        });
    }
    function penDown(e){
        cancelAnimationFrame(penFrame);penFrame=0;penHover=null;
        if(e.detail>=2&&state.tool==='bezier'){finishPen();return;}
        const at=h.point(e),p=[at.x,at.y];
        if(!penDraft){
            h.select([]);const style=h.penStyle(),target=makeSvg('path',{d:'',fill:'none',stroke:style.stroke==='none'?'#333333':style.stroke,'stroke-width':style.width,opacity:style.opacity,'vector-effect':'non-scaling-stroke'});
            h.svg().appendChild(target);penDraft={target,style};nodeTarget=target;nodeData=[{closed:false,segments:[]}];
        }
        const path=nodeData[0],all=path.segments,m=h.svg().getScreenCTM();
        const distance=v=>Math.hypot(m.a*(v.p[0]-p[0])+m.c*(v.p[1]-p[1]),m.b*(v.p[0]-p[0])+m.d*(v.p[1]-p[1]));
        if(all.length>=3&&distance(all[0])<=8){path.closed=true;if(state.tool==='curvature')curveHandles();finishPen();return;}
        if(state.tool==='curvature'&&e.detail>=2){const index=all.findIndex(v=>distance(v)<=8);if(index>=0){all[index].corner=!all[index].corner;curveHandles();writeNodes(false);}return;}
        if(all.length&&distance(all[all.length-1])<1)return;
        if(all.length>=4000){report(tr('单条路径最多 4000 个锚点。','A path supports at most 4,000 anchors.'));return;}
        all.push({p,i:[0,0],o:[0,0]});nodePick={path:0,index:all.length-1};
        penDrag={index:all.length-1,start:{x:e.clientX,y:e.clientY}};
        if(state.tool==='curvature')curveHandles();writeNodes(false);
    }
    function penMove(e){
        if(!penDraft||!penDrag)return;
        const p=h.point(e),v=nodeData[0].segments[penDrag.index];
        if(Math.hypot(e.clientX-penDrag.start.x,e.clientY-penDrag.start.y)>2){
            if(state.tool==='curvature'){v.p=[p.x,p.y];curveHandles();}
            else{v.o=[p.x-v.p[0],p.y-v.p[1]];v.i=e.altKey?[0,0]:v.o.map(x=>-x);}
        }
        if(!penFrame)penFrame=requestAnimationFrame(()=>{penFrame=0;writeNodes(false);});
    }
    function finishPen(){
        if(!penDraft)return;
        cancelAnimationFrame(penFrame);penFrame=0;penDrag=null;penHover=null;
        const {target,style}=penDraft,path=nodeData?.[0];penDraft=null;
        if(!path||path.segments.length<2){target.remove();nodeData=null;nodeTarget=null;nodePick=null;paint();return;}
        target.setAttribute('d',pathString([path]));target.setAttribute('fill',path.closed?style.fill:'none');
        h.save(tr('钢笔绘制','Draw path'));nodeData=null;nodeTarget=null;nodePick=null;h.select([target]);paint();
    }
    function paintPenPreview(){
        if(!penDraft||penDrag||!penHover||!nodeData?.[0]?.segments.length)return;
        const all=nodeData[0].segments,n=all.length,m=nodeTarget.getScreenCTM();if(!m)return;
        const copy=v=>({p:[...v.p],i:[...v.i],o:[...v.o],corner:v.corner});
        const screen=p=>[m.a*p[0]+m.c*p[1]+m.e,m.b*p[0]+m.d*p[1]+m.f];
        const first=screen(all[0].p),cursor=screen(penHover),closing=n>=3&&Math.hypot(first[0]-cursor[0],first[1]-cursor[1])<=8;
        const last=copy(all[n-1]),next=closing?copy(all[0]):{p:[...penHover],i:[0,0],o:[0,0]};
        let segments=[last,next];
        if(state.tool==='curvature'){
            const previous=n>1?all[n-2].p:last.p;
            if(!last.corner){last.o=[(next.p[0]-previous[0])/(n>1?6:3),(next.p[1]-previous[1])/(n>1?6:3)];last.i=last.o.map(x=>-x);}
            if(!closing){next.i=[(last.p[0]-next.p[0])/3,(last.p[1]-next.p[1])/3];next.o=next.i.map(x=>-x);}
            else if(!next.corner){const following=all[1].p;next.o=[(following[0]-last.p[0])/6,(following[1]-last.p[1])/6];next.i=next.o.map(x=>-x);}
            if(n>1)segments.unshift(copy(all[n-2]));
        }
        const group=screenGroup('ve-pen-preview');if(!group)return;
        const transformed=segments.map(v=>({p:screen(v.p),i:[m.a*v.i[0]+m.c*v.i[1],m.b*v.i[0]+m.d*v.i[1]],o:[m.a*v.o[0]+m.c*v.o[1],m.b*v.o[0]+m.d*v.o[1]]}));
        group.appendChild(makeSvg('path',{d:pathString([{segments:transformed,closed:false}]),fill:'none',stroke:'#527dff','stroke-width':1.5,'pointer-events':'none'}));
        const end=transformed[transformed.length-1].p;group.appendChild(makeSvg('circle',{cx:end[0],cy:end[1],r:closing?5:3,fill:'none',stroke:'#527dff','stroke-width':1.5}));
    }
    function addAnchorAt(event){
        if(!nodeData||!nodeTarget||!modifiable(nodeTarget))return;
        const m=nodeTarget.getScreenCTM(),sx=event.clientX,sy=event.clientY;
        const point=(a,b,t)=>{const u=1-t,x=u*u*u*a.p[0]+3*u*u*t*(a.p[0]+a.o[0])+3*u*t*t*(b.p[0]+b.i[0])+t*t*t*b.p[0],y=u*u*u*a.p[1]+3*u*u*t*(a.p[1]+a.o[1])+3*u*t*t*(b.p[1]+b.i[1])+t*t*t*b.p[1];return Math.hypot(m.a*x+m.c*y+m.e-sx,m.b*x+m.d*y+m.f-sy);};
        let best=null;
        nodeData.forEach((path,pi)=>{for(let i=0;i<path.segments.length-(path.closed?0:1);i++){
            const a=path.segments[i],b=path.segments[(i+1)%path.segments.length];
            for(let k=0;k<=12;k++){const t=k/12,d=point(a,b,t);if(!best||d<best.d)best={path:pi,index:i,t,d,a,b};}
        }});
        if(!best)return;let lo=Math.max(.001,best.t-1/12),hi=Math.min(.999,best.t+1/12);
        for(let k=0;k<14;k++){const a=lo+(hi-lo)/3,b=hi-(hi-lo)/3;if(point(best.a,best.b,a)<point(best.a,best.b,b))hi=b;else lo=a;}
        const t=(lo+hi)/2;if(point(best.a,best.b,t)>9)return;
        if(nodeData.reduce((n,path)=>n+path.segments.length,0)>=4000)return report(tr('最多编辑 4000 个锚点。','At most 4,000 anchors can be edited.'));
        nodePick={path:best.path,index:best.index};editNode('add',t);return true;
    }
    function writeNodes(commit=true){
        if(!nodeTarget||!nodeData||!h.svg().contains(nodeTarget))return;
        if(nodeTarget.tagName.toLowerCase()!=='path'){
            const data=nodeData,pick=nodePick,target=toPath(nodeTarget);changingNodes=true;
            try{h.select([target]);}finally{changingNodes=false;}
            nodeTarget=target;nodeData=data;nodePick=pick;
        }
        nodeTarget.setAttribute('d',pathString(nodeData));h.refresh();paint();if(commit)h.save(tr('编辑节点','Edit anchors'));
    }
    function editNode(action,t=.5){
        if(!nodePick||!nodeData)return hint(tr('请先点击一个方形节点','Choose an anchor first'));
        const path=nodeData[nodePick.path],segments=path.segments,index=nodePick.index,n=segments.length,v=segments[index];
        if(action==='delete'){if(n<=2)throw Error(tr('路径至少保留两个节点','Keep at least two anchors'));segments.splice(index,1);nodePick=null;}
        else if(action==='corner'){v.i=[0,0];v.o=[0,0];}
        else if(action==='smooth'){
            const previous=segments[(index+n-1)%n].p,next=segments[(index+1)%n].p;
            v.o=[(next[0]-previous[0])/6,(next[1]-previous[1])/6];v.i=v.o.map(x=>-x);
        } else if(action==='add'){
            if(!path.closed&&index===n-1)throw Error(tr('请选择一段路径的起点','Choose the start of a segment'));
            const next=segments[(index+1)%n],a=v.p,b=[a[0]+v.o[0],a[1]+v.o[1]],d=next.p,c=[d[0]+next.i[0],d[1]+next.i[1]];
            const mid=(x,y)=>[x[0]+(y[0]-x[0])*t,x[1]+(y[1]-x[1])*t],ab=mid(a,b),bc=mid(b,c),cd=mid(c,d),abc=mid(ab,bc),bcd=mid(bc,cd),p=mid(abc,bcd);
            v.o=[ab[0]-a[0],ab[1]-a[1]];next.i=[cd[0]-d[0],cd[1]-d[1]];
            segments.splice(index+1,0,{p,i:[abc[0]-p[0],abc[1]-p[1]],o:[bcd[0]-p[0],bcd[1]-p[1]]});nodePick={path:nodePick.path,index:index+1};
        }
        writeNodes();requestRender();
    }
    function screenGroup(className){
        const m=h.draw().getScreenCTM()?.inverse();if(!m)return null;
        const group=makeSvg('g',{'class':className,transform:matrixText(m)});adornments.appendChild(group);return group;
    }
    let nodeHits=[];
    function paintNodes(){
        nodeHits=[];const editing=(state.panel==='nodes'||anchorTools.has(state.tool)||penDraft)&&nodeData&&nodeTarget&&h.svg().contains(nodeTarget);
        h.overlay.classList.toggle('ve-editing-nodes',!!editing);if(!editing)return;
        const m=nodeTarget.getScreenCTM();if(!m)return;const group=screenGroup('ve-node-overlays');if(!group)return;
        const rect=h.canvas.getBoundingClientRect(),screen=p=>({x:m.a*p[0]+m.c*p[1]+m.e,y:m.b*p[0]+m.d*p[1]+m.f});
        for(let pi=0;pi<nodeData.length;pi++)for(let i=0;i<nodeData[pi].segments.length;i++){
            const v=nodeData[pi].segments[i],p=screen(v.p);if(p.x<rect.left-20||p.x>rect.right+20||p.y<rect.top-20||p.y>rect.bottom+20)continue;
            const chosen=nodePick?.path===pi&&nodePick.index===i;
            if(chosen)for(const key of ['i','o']){
                const control=screen([v.p[0]+v[key][0],v.p[1]+v[key][1]]);
                group.appendChild(makeSvg('line',{x1:p.x,y1:p.y,x2:control.x,y2:control.y,stroke:'#ffb74d','stroke-width':1}));
                group.appendChild(makeSvg('circle',{cx:control.x,cy:control.y,r:4,fill:'#fff',stroke:'#ffb74d'}));nodeHits.push({...control,path:pi,index:i,key});
            }
            group.appendChild(makeSvg('rect',{x:p.x-3.5,y:p.y-3.5,width:7,height:7,fill:chosen?'#ffb74d':'#fff',stroke:'#ffb74d'}));nodeHits.push({...p,path:pi,index:i,key:'p'});
        }
    }
    on(h.canvas,'mousedown',e=>{
        if(e.button!==0||e.target.closest('.ve-workbench-panel,.ve-confirm')||h.space())return;
        if(state.rulers){const r=h.canvas.getBoundingClientRect();if(e.clientX-r.left<20||e.clientY-r.top<20)return;}
        if(state.tool==='bezier'||state.tool==='curvature'){e.preventDefault();e.stopImmediatePropagation();penDown(e);return;}
        if(anchorTools.has(state.tool)){
            const hit=nodeHits.find(p=>p.key==='p'&&Math.hypot(p.x-e.clientX,p.y-e.clientY)<=7)||
                (state.tool==='delete-anchor'?null:nodeHits.find(p=>Math.hypot(p.x-e.clientX,p.y-e.clientY)<=7));
            if(nodeData&&hit&&modifiable(nodeTarget)){
                e.preventDefault();e.stopImmediatePropagation();nodePick={path:hit.path,index:hit.index};
                if(state.tool==='delete-anchor'){try{editNode('delete');}catch(error){report(error);}return;}
                if(state.tool==='add-anchor'){try{addAnchorAt(e);}catch(error){report(error);}return;}
                nodeDrag={...hit,original:nodeTarget.cloneNode(true),inverse:nodeTarget.getScreenCTM().inverse(),changed:false};paint();return;
            }
            e.preventDefault();e.stopImmediatePropagation();
            if(state.tool==='add-anchor'&&nodeData&&addAnchorAt(e))return;
            const el=e.target.closest?.('path,rect,circle,ellipse,line,polygon,polyline');
            if(el&&h.svg().contains(el)&&!el.closest('defs,clipPath,mask')&&modifiable(el)){
                if(el===nodeTarget&&nodeData){if(state.tool==='add-anchor')addAnchorAt(e);else{nodePick=null;paint();}}
                else{h.select([el]);if(state.tool==='add-anchor')nodeLoading.then(target=>{if(target===nodeTarget)addAnchorAt(e);}).catch(report);}
            }else{h.select([]);}
            return;
        }
        if(state.panel==='nodes'&&nodeData){
            const hit=nodeHits.find(p=>Math.hypot(p.x-e.clientX,p.y-e.clientY)<=7);
            if(hit&&modifiable(nodeTarget)){
                e.preventDefault();e.stopImmediatePropagation();nodePick={path:hit.path,index:hit.index};
                nodeDrag={...hit,original:nodeTarget.cloneNode(true),inverse:nodeTarget.getScreenCTM().inverse(),changed:false};paint();
            }
        }
    },{capture:true});
    on(window,'mousemove',e=>{
        if(penDrag){e.preventDefault();e.stopImmediatePropagation();penMove(e);return;}
        if(penDraft){
            const inside=h.canvas.contains(e.target)&&!e.target.closest('.ve-workbench-panel,.ve-confirm');
            const p=inside?h.point(e):null;penHover=p?[p.x,p.y]:null;
            if(!penFrame)penFrame=requestAnimationFrame(()=>{penFrame=0;paint();});
            if(inside)e.stopImmediatePropagation();return;
        }
        if(!nodeDrag)return;e.preventDefault();e.stopImmediatePropagation();
        const m=nodeDrag.inverse,p=[m.a*e.clientX+m.c*e.clientY+m.e,m.b*e.clientX+m.d*e.clientY+m.f];
        const segment=nodeData[nodeDrag.path].segments[nodeDrag.index];
        segment[nodeDrag.key]=nodeDrag.key==='p'?p:[p[0]-segment.p[0],p[1]-segment.p[1]];nodeDrag.changed=true;
        if(!nodeFrame)nodeFrame=requestAnimationFrame(()=>{nodeFrame=0;writeNodes(false);});
    },{capture:true});
    on(h.canvas,'mouseleave',()=>{if(penHover){penHover=null;paint();}});
    on(window,'mouseup',e=>{if(penDrag){e.preventDefault();e.stopImmediatePropagation();penMove(e);cancelAnimationFrame(penFrame);penFrame=0;writeNodes(false);penDrag=null;return;}if(!nodeDrag)return;e.preventDefault();e.stopImmediatePropagation();cancelAnimationFrame(nodeFrame);nodeFrame=0;const changed=nodeDrag.changed;nodeDrag=null;if(changed)writeNodes(true);requestRender();},{capture:true});
    on(document,'keydown',e=>{
        if(h.overlay.querySelector('.ve-confirm'))return;
        if(e.target?.closest?.('.r-ve-swatches,.ve-shape-menu'))return;
        if(e.target?.closest?.('input,textarea,select,[contenteditable="true"]'))return;
        if(e.key==='Escape'&&selectedGuide>=0){e.preventDefault();e.stopImmediatePropagation();selectedGuide=-1;paint();return;}
        if(!penMenu.hidden&&e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();closePenMenu();penButton.focus();return;}
        if(penDraft&&['Escape','Enter'].includes(e.key)){e.preventDefault();e.stopImmediatePropagation();finishPen();return;}
        if(penDraft&&(e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='z'){e.preventDefault();e.stopImmediatePropagation();nodeData[0].segments.pop();if(!nodeData[0].segments.length)finishPen();else{if(state.tool==='curvature')curveHandles();writeNodes(false);}return;}
        if(!e.ctrlKey&&!e.metaKey&&!e.altKey&&e.key.toLowerCase()==='a'){e.preventDefault();e.stopImmediatePropagation();h.overlay.querySelector('[data-tool="anchor"]').click();return;}
        if(!e.ctrlKey&&!e.metaKey&&!e.altKey&&e.key.toLowerCase()==='p'){e.preventDefault();e.stopImmediatePropagation();choosePen('bezier');return;}
        if(nodeData&&nodePick&&state.panel==='nodes'&&['Delete','Backspace'].includes(e.key)){
            e.preventDefault();e.stopImmediatePropagation();try{editNode('delete');}catch(error){report(error);}return;
        }
        if(e.key==='Escape'&&nodeDrag){e.preventDefault();e.stopImmediatePropagation();cancelAnimationFrame(nodeFrame);nodeFrame=0;const original=nodeDrag.original;nodeTarget.replaceWith(original);nodeDrag=null;nodeTarget=null;nodeData=null;h.select([original]);h.refresh();paint();requestRender();}
        else if(e.key==='Escape'&&state.panel==='nodes'&&nodeData){nodeData=null;nodeTarget=null;paint();requestRender();}
        else if(e.key==='Escape'&&state.isolation){state.isolation=null;paint();requestRender();}
    },{capture:true});

    function canvasBounds(){return {...(h.origin?.()||{x:0,y:0}),w:h.width(),h:h.height()};}

    function snapDrag(els,dx,dy,ignore=false,axis=null){
        if(ignore)return {dx,dy};
        if(!snapTargets){
            const excluded=new Set(els),xs=[],ys=[],boxes=[];
            const accept=el=>!excluded.has(el)&&!els.some(root=>root.contains(el))&&!el.closest('defs,clipPath,mask,pattern,marker,symbol')&&el.getAttribute('visibility')!=='hidden';
            let targets=h.svg().querySelectorAll('rect,circle,ellipse,line,path,polygon,polyline,text,image,g[data-user-group="true"]');
            if(targets.length>4000){
                // ponytail: bound synchronous bbox work on large plots. A scene index
                // can extend snapping beyond this local neighborhood if needed later.
                const local=new Set(),rect=h.canvas.getBoundingClientRect();
                for(const el of els){let p=el;for(let i=0;p&&i<120;i++,p=p.previousElementSibling)local.add(p);p=el.nextElementSibling;for(let i=0;p&&i<120;i++,p=p.nextElementSibling)local.add(p);}
                const source=combinedBounds(els),m=h.svg().getScreenCTM();
                if(source){const cx=m.a*(source.x+source.w/2)+m.e,cy=m.d*(source.y+source.h/2)+m.f;
                    for(const dx of [-150,-75,0,75,150])for(const dy of [-150,-75,0,75,150]){
                        const x=cx+dx,y=cy+dy;if(x<rect.left||x>rect.right||y<rect.top||y>rect.bottom)continue;
                        for(const node of document.elementsFromPoint(x,y))if(node!==h.svg()&&h.svg().contains(node)&&node.matches(drawable))local.add(node);
                    }
                }
                targets=local;
            }
            // Build once at the start of a gesture. Keep the candidates nearest to the selection.
            const origin=combinedBounds(els),near=[];
            for(const el of targets){if(!accept(el))continue;const b=h.bounds(el);if(b)near.push({b,d:origin?Math.hypot(b.x-origin.x,b.y-origin.y):0});}
            near.sort((a,b)=>a.d-b.d);
            for(const {b}of near.slice(0,2000)){xs.push(b.x,b.x+b.w/2,b.x+b.w);ys.push(b.y,b.y+b.h/2,b.y+b.h);boxes.push(b);}
            const board=canvasBounds();xs.push(board.x,board.x+board.w/2,board.x+board.w);ys.push(board.y,board.y+board.h/2,board.y+board.h);
            for(const guide of guides()){(guide.axis==='x'?xs:ys).push(guide.value);}
            xs.sort((a,b)=>a-b);ys.sort((a,b)=>a-b);snapTargets={xs,ys,boxes,origin};
        }
        const b=snapTargets.origin;if(!b)return {dx,dy};const tolerance=6/Math.max(.01,h.zoom());
        const nearest=(targets,values)=>{let found=null;for(const value of values){let lo=0,hi=targets.length;while(lo<hi){const mid=(lo+hi)>>1;targets[mid]<value?lo=mid+1:hi=mid;}
            for(const index of [lo-1,lo]){if(index<0||index>=targets.length)continue;const delta=targets[index]-value;if(Math.abs(delta)<=tolerance&&(!found||Math.abs(delta)<Math.abs(found.delta)))found={delta,value:targets[index]};}}
            return found;};
        const x=axis==='vertical'?null:nearest(snapTargets.xs,[b.x+dx,b.x+b.w/2+dx,b.x+b.w+dx]),y=axis==='horizontal'?null:nearest(snapTargets.ys,[b.y+dy,b.y+b.h/2+dy,b.y+b.h+dy]);
        if(x)dx+=x.delta;if(y)dy+=y.delta;
        let spacing=null,verticalSpacing=null;
        if(!x&&axis!=='vertical'){const left=snapTargets.boxes.filter(o=>o.x+o.w<=b.x+dx&&o.y<b.y+dy+b.h&&o.y+o.h>b.y+dy).sort((a,c)=>c.x+c.w-a.x-a.w)[0];
            const right=snapTargets.boxes.filter(o=>o.x>=b.x+dx+b.w&&o.y<b.y+dy+b.h&&o.y+o.h>b.y+dy).sort((a,c)=>a.x-c.x)[0];
            if(left&&right){const centered=(left.x+left.w+right.x-b.w)/2,delta=centered-b.x-dx;if(Math.abs(delta)<=tolerance){dx+=delta;spacing={left,right,y:b.y+dy+b.h/2};}}
        }
        if(!y&&axis!=='horizontal'){const top=snapTargets.boxes.filter(o=>o.y+o.h<=b.y+dy&&o.x<b.x+dx+b.w&&o.x+o.w>b.x+dx).sort((a,c)=>c.y+c.h-a.y-a.h)[0];
            const bottom=snapTargets.boxes.filter(o=>o.y>=b.y+dy+b.h&&o.x<b.x+dx+b.w&&o.x+o.w>b.x+dx).sort((a,c)=>a.y-c.y)[0];
            if(top&&bottom){const centered=(top.y+top.h+bottom.y-b.h)/2,delta=centered-b.y-dy;if(Math.abs(delta)<=tolerance){dy+=delta;verticalSpacing={top,bottom,x:b.x+dx+b.w/2};}}
        }
        paintSmartGuides(x?.value,y?.value,spacing,verticalSpacing);return {dx,dy};
    }
    function guides(){try{return JSON.parse(decodeURIComponent(h.svg()?.getAttribute('data-ve-guides')||'%5B%5D')).filter(g=>['x','y'].includes(g.axis)&&finite(g.value));}catch(_){return [];}}
    function guideAt(event){
        const matrix=h.svg()?.getScreenCTM();if(!matrix)return -1;
        let m;try{m=matrix.inverse();}catch(_){return -1;}
        const p={x:m.a*event.clientX+m.c*event.clientY+m.e,y:m.b*event.clientX+m.d*event.clientY+m.f};
        let nearest=-1,distance=6;
        guides().forEach((guide,index)=>{
            const normal=guide.axis==='x'?Math.hypot(m.a,m.c):Math.hypot(m.b,m.d);
            const pixels=Math.abs(p[guide.axis]-guide.value)/normal;
            if(pixels<distance){nearest=index;distance=pixels;}
        });return nearest;
    }
    function selectGuide(index){
        h.overlay.querySelector('.r-ve-inline-editor')?._commit?.();
        document.activeElement?.blur();h.select([]);selectedGuide=index;paint();
    }
    function deleteGuide(){
        if(selectedGuide<0)return false;
        const all=guides(),index=selectedGuide;selectedGuide=-1;
        if(index<all.length)mutate(tr('删除辅助线','Delete guide'),()=>{all.splice(index,1);h.svg().setAttribute('data-ve-guides',encodeURIComponent(JSON.stringify(all)));});
        paint();return true;
    }
    function paintSmartGuides(x,y,spacing,verticalSpacing){
        adornments.querySelectorAll('.ve-smart-guide').forEach(n=>n.remove());const group=screenGroup('ve-smart-guide');if(!group)return;
        const m=h.svg().getScreenCTM(),rect=h.canvas.getBoundingClientRect(),screen=(x,y)=>({x:m.a*x+m.c*y+m.e,y:m.b*x+m.d*y+m.f});
        if(finite(x)){const p=screen(x,0);group.appendChild(makeSvg('line',{x1:p.x,y1:rect.top,x2:p.x,y2:rect.bottom,stroke:'#ed75cc','stroke-width':1}));}
        if(finite(y)){const p=screen(0,y);group.appendChild(makeSvg('line',{x1:rect.left,y1:p.y,x2:rect.right,y2:p.y,stroke:'#ed75cc','stroke-width':1}));}
        if(spacing){const a=screen(spacing.left.x+spacing.left.w,spacing.y),b=screen(spacing.right.x,spacing.y);group.appendChild(makeSvg('line',{x1:a.x,y1:a.y,x2:b.x,y2:b.y,stroke:'#ed75cc','stroke-width':1,'stroke-dasharray':'3 3'}));const text=makeSvg('text',{x:(a.x+b.x)/2,y:a.y-7,fill:'#ed75cc','font-size':11});text.textContent=tr('等间距','Equal gaps');group.appendChild(text);}
        if(verticalSpacing){const a=screen(verticalSpacing.x,verticalSpacing.top.y+verticalSpacing.top.h),b=screen(verticalSpacing.x,verticalSpacing.bottom.y);group.appendChild(makeSvg('line',{x1:a.x,y1:a.y,x2:b.x,y2:b.y,stroke:'#ed75cc','stroke-width':1,'stroke-dasharray':'3 3'}));const text=makeSvg('text',{x:a.x+5,y:(a.y+b.y)/2,fill:'#ed75cc','font-size':11});text.textContent=tr('等间距','Equal gaps');group.appendChild(text);}
    }
    function paint(){
        corners.refresh();
        adornments.replaceChildren();if(state.closed||!h.svg())return;
        const group=screenGroup('ve-workbench-overlays-group');if(!group)return;const m=h.svg().getScreenCTM();if(!m)return;
        const bounds=b=>{const p={x:m.a*b.x+m.c*b.y+m.e,y:m.b*b.x+m.d*b.y+m.f};return {...p,w:b.w*Math.hypot(m.a,m.b),h:b.h*Math.hypot(m.c,m.d)};};
        if(state.isolation&&h.svg().contains(state.isolation)){const b=h.bounds(state.isolation);if(b){const r=bounds(b);group.appendChild(makeSvg('rect',{x:r.x-5,y:r.y-5,width:r.w+10,height:r.h+10,fill:'none',stroke:'#81c784','stroke-dasharray':'4 3'}));}}
        if(state.rulers){const rect=h.canvas.getBoundingClientRect();guides().forEach((guide,index)=>{
            const at=bounds({x:guide.axis==='x'?guide.value:0,y:guide.axis==='y'?guide.value:0,w:0,h:0}),selected=index===selectedGuide;
            const line=makeSvg('line',{class:'ve-ruler-guide','data-guide-index':index,'data-selected':String(selected),x1:guide.axis==='x'?at.x:rect.left,y1:guide.axis==='y'?at.y:rect.top,x2:guide.axis==='x'?at.x:rect.right,y2:guide.axis==='y'?at.y:rect.bottom,stroke:selected?'#1976d2':'#4dd0e1','stroke-width':selected?2:1});
            for(const name of ['stroke','stroke-width'])line.style.setProperty(name,line.getAttribute(name),'important');
            line.style.setProperty('pointer-events','none','important');group.appendChild(line);
        });}
        paintNodes();paintPenPreview();
    }
    function paintRulers(){
        if(!state.rulers){selectedGuide=-1;rulerCanvas?.remove();rulerCanvas=null;paint();return;}
        if(!rulerCanvas){rulerCanvas=document.createElement('canvas');rulerCanvas.className='ve-rulers';h.canvas.appendChild(rulerCanvas);}
        const width=h.canvas.clientWidth,height=h.canvas.clientHeight;rulerCanvas.width=width;rulerCanvas.height=height;
        const ctx=rulerCanvas.getContext('2d'),theme=getComputedStyle(h.overlay);ctx.fillStyle=theme.getPropertyValue('--ve-control').trim()||'#f8fafc';ctx.fillRect(0,0,width,20);ctx.fillRect(0,0,20,height);ctx.fillStyle=theme.getPropertyValue('--ve-text').trim()||'#26384b';ctx.strokeStyle=theme.getPropertyValue('--ve-muted').trim()||'#63788e';ctx.font='10px sans-serif';
        const m=h.svg()?.getScreenCTM();if(!m)return;const rect=h.canvas.getBoundingClientRect(),factor=unitFactor(state.unit),px=Math.abs(m.a)*factor;
        const raw=70/Math.max(.001,px),base=10**Math.floor(Math.log10(raw)),step=[1,2,5,10].map(x=>x*base).find(x=>x>=raw);
        for(const axis of ['x','y']){const size=axis==='x'?width:height,origin=(axis==='x'?m.e-rect.left:m.f-rect.top),scale=axis==='x'?m.a:m.d;
            const first=Math.floor(-origin/(scale*factor*step));for(let i=first;i<first+size/35+5;i++){const value=i*step,at=origin+value*factor*scale;if(at<20||at>size)continue;ctx.beginPath();axis==='x'?(ctx.moveTo(at,15),ctx.lineTo(at,20)):(ctx.moveTo(15,at),ctx.lineTo(20,at));ctx.stroke();ctx.save();axis==='x'?ctx.fillText(String(+value.toPrecision(5)),at+2,11):(ctx.translate(11,at-2),ctx.rotate(-Math.PI/2),ctx.fillText(String(+value.toPrecision(5)),0,0));ctx.restore();}}
        ctx.fillText(state.unit,1,11);paint();
    }
    on(h.canvas,'mousedown',e=>{
        if(!state.rulers||e.button!==0||e.target.closest('.ve-workbench-panel,.ve-confirm')||h.space())return;
        const r=h.canvas.getBoundingClientRect(),x=e.clientX-r.left,y=e.clientY-r.top;
        if(x<20||y<20){e.preventDefault();e.stopImmediatePropagation();selectedGuide=-1;guideDrag={axis:y<20?'y':'x',value:0};return;}
        if(state.tool==='select'){
            const index=guideAt(e);
            if(index>=0){e.preventDefault();e.stopImmediatePropagation();selectGuide(index);return;}
        }
        if(selectedGuide>=0){selectedGuide=-1;paint();}
    },{capture:true});
    on(window,'mousemove',e=>{if(!guideDrag)return;e.preventDefault();e.stopImmediatePropagation();const p=h.point(e);guideDrag.value=guideDrag.axis==='x'?p.x:p.y;paintSmartGuides(guideDrag.axis==='x'?guideDrag.value:undefined,guideDrag.axis==='y'?guideDrag.value:undefined);},{capture:true});
    on(window,'mouseup',e=>{if(!guideDrag)return;e.preventDefault();e.stopImmediatePropagation();const current=guideDrag;guideDrag=null;mutate(tr('添加辅助线','Add guide'),()=>h.svg().setAttribute('data-ve-guides',encodeURIComponent(JSON.stringify([...guides(),current].slice(-100)))));if(state.tool==='select')selectGuide(guides().length-1);else paint();},{capture:true});
    on(h.canvas,'dblclick',e=>{
        if(!state.rulers||state.tool!=='select'||h.space()||e.target.closest('.ve-workbench-panel,.ve-confirm'))return;
        const index=guideAt(e);if(index>=0){e.preventDefault();e.stopImmediatePropagation();selectedGuide=index;deleteGuide();}
    },{capture:true});
    // Overlays are updated only after a document/selection/view change.
    requestRender();startRecoveryCheck();
    return {committed,replaced,changedSelection,viewChanged:()=>{paint();paintRulers();},requestClose,beforeReplace,save,markSaved,dispose,flushRecovery,contextItems,setTool,finishPath:finishPen,
        deleteGuide,hasSelectedGuide:()=>selectedGuide>=0,
        cornerAt:corners.hit,beginCorner:corners.begin,flushCorners:corners.flush,refreshCorners:corners.refresh,roundingActive:corners.active,
        isolation:()=>state.isolation,snapDrag,clearGuides:()=>{adornments.querySelectorAll('.ve-smart-guide').forEach(n=>n.remove());},dirty:()=>state.dirty,notify:report};
};
