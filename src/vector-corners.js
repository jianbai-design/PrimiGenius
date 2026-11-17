'use strict';
// Convex shape fillets: bounded local geometry; no scene scans or snapshots per frame.
const ns='http://www.w3.org/2000/svg';
const fmt=n=>String(+n.toFixed(6));
function geometry(points){
    if(points.length<3||points.length>100||!points.every(p=>Array.isArray(p)&&p.length===2&&p.every(Number.isFinite)))return null;
    let winding=0;
    const corners=points.map((p,i)=>{
        const prev=points[(i+points.length-1)%points.length],next=points[(i+1)%points.length];
        const a=[prev[0]-p[0],prev[1]-p[1]],b=[next[0]-p[0],next[1]-p[1]],la=Math.hypot(...a),lb=Math.hypot(...b);
        if(la<1e-6||lb<1e-6)return null;
        const cross=b[0]*a[1]-b[1]*a[0],sign=Math.sign(cross);
        if(Math.abs(cross)<1e-8||winding&&winding!==sign)return null;
        winding=sign;
        const u=a.map(v=>v/la),v=b.map(v=>v/lb),dot=Math.max(-1,Math.min(1,u[0]*v[0]+u[1]*v[1]));
        const sin=Math.sqrt((1-dot)/2),cos=Math.sqrt((1+dot)/2),length=Math.hypot(u[0]+v[0],u[1]+v[1]);
        if(sin<1e-6||cos<1e-6||length<1e-6)return null;
        return {p,u,v,sin,cot:cos/sin,bisector:[(u[0]+v[0])/length,(u[1]+v[1])/length],edge:lb};
    });
    if(corners.some(c=>!c))return null;
    const max=Math.min(...corners.map((c,i)=>c.edge/(c.cot+corners[(i+1)%corners.length].cot)));
    return {points,corners,max,sweep:winding>0?1:0};
}
function path(data,radius){
    const r=Math.max(0,Math.min(data.max,radius)),point=p=>p.map(fmt).join(' ');
    if(r<1e-6)return 'M'+data.points.map(point).join('L')+'Z';
    return data.corners.map((c,i)=>{
        const t=r*c.cot,a=c.p.map((v,k)=>v+c.u[k]*t),b=c.p.map((v,k)=>v+c.v[k]*t);
        return (i?'L':'M')+point(a)+'A'+fmt(r)+' '+fmt(r)+' 0 0 '+data.sweep+' '+point(b);
    }).join('')+'Z';
}
exports.geometry=geometry;
exports.path=path;

exports.mount=function(h){
    const tr=(cn,en)=>h.lang==='zh'?cn:en;
    const on=(node,event,fn,options={})=>node.addEventListener(event,fn,{...options,signal:h.signal});
    const make=(tag,attrs)=>{const node=document.createElementNS(ns,tag);for(const [k,v]of Object.entries(attrs)){node.setAttribute(k,v);if(['fill','stroke','stroke-width'].includes(k))node.style.setProperty(k,v,'important');}return node;};
    let group=null,model=null,handles=[],drag=null,frame=0;
    function describe(el){
        const tag=el.tagName.toLowerCase();let points,radius=0;
        if(tag==='rect'){
            const x=el.x.baseVal.value,y=el.y.baseVal.value,w=el.width.baseVal.value,ht=el.height.baseVal.value;
            if(w<=0||ht<=0)return null;
            points=[[x,y],[x+w,y],[x+w,y+ht],[x,y+ht]];
            const style=getComputedStyle(el),rx=parseFloat(style.rx),ry=parseFloat(style.ry);
            radius=Number.isFinite(rx)?rx:Number.isFinite(ry)?ry:0;
        }else if(tag==='polygon'){
            if(el.points.numberOfItems>100)return null;
            points=Array.from({length:el.points.numberOfItems},(_,i)=>{const p=el.points.getItem(i);return [p.x,p.y];});
        }else if(tag==='path'&&el.hasAttribute('data-ve-corner-points')){
            try{points=JSON.parse(el.getAttribute('data-ve-corner-points'));radius=Number(el.getAttribute('data-ve-corner-radius'));}catch(_){return null;}
            if(!Array.isArray(points)||!Number.isFinite(radius))return null;
        }else return null;
        const data=geometry(points);if(!data)return null;
        radius=Math.max(0,Math.min(data.max,radius));
        // Manual path edits invalidate the saved polygon, so rounding cannot erase them.
        if(tag==='path'&&el.getAttribute('d')!==path(data,radius))return null;
        return {el,tag,data,radius};
    }
    function position(){
        for(const handle of handles){
            const c=handle.corner,d=handle.base+(handle.limit-handle.base)*model.radius/model.data.max;
            const x=c.p[0]+c.bisector[0]*d,y=c.p[1]+c.bisector[1]*d,m=handle.matrix;
            handle.x=m.a*x+m.c*y+m.e;handle.y=m.b*x+m.d*y+m.f;
            handle.node.setAttribute('cx',handle.x);handle.node.setAttribute('cy',handle.y);
            handle.node.setAttribute('aria-valuenow',fmt(model.radius));
            handle.node.firstChild.textContent=tr('拖动调整所有圆角；Esc 取消','Drag to round all corners; Esc cancels')+' ('+fmt(model.radius)+')';
        }
    }
    function refresh(){
        if(drag)return;
        group?.remove();group=null;model=null;handles=[];
        const selected=h.selected();
        if(h.tool()!=='select'||selected.length!==1||selected[0].closest('[data-locked="true"]')||selected[0].getAttribute('data-role')==='background')return;
        model=describe(selected[0]);if(!model)return;
        const m=model.el.getScreenCTM(),draw=h.draw(),drawMatrix=draw?.getScreenCTM();
        if(!m||!drawMatrix)return;
        let inv;try{inv=drawMatrix.inverse();}catch(_){return;}
        group=make('g',{class:'ve-corner-handles','pointer-events':'none',transform:`matrix(${inv.a},${inv.b},${inv.c},${inv.d},${inv.e},${inv.f})`});
        model.data.corners.forEach((corner,index)=>{
            const b=corner.bisector,pixelsPerUnit=Math.hypot(m.a*b[0]+m.c*b[1],m.b*b[0]+m.d*b[1]),limit=model.data.max/corner.sin;
            if(limit*pixelsPerUnit<15)return; // Keep small shapes' resize handles usable.
            const node=make('circle',{class:'ve-corner-handle',r:5,fill:'#fff',stroke:'#527dff','stroke-width':2,tabindex:0,role:'slider','aria-label':tr('圆角半径','Corner radius'),'aria-valuemin':0,'aria-valuemax':fmt(model.data.max),'data-corner':index});
            node.appendChild(make('title',{}));group.appendChild(node);
            handles.push({node,corner,matrix:m,limit,base:Math.min(16/pixelsPerUnit,limit*.4)});
            node.onkeydown=event=>{
                if(!['ArrowLeft','ArrowDown','ArrowRight','ArrowUp','Home','End'].includes(event.key))return;
                event.preventDefault();event.stopPropagation();
                const next=event.key==='Home'?0:event.key==='End'?model.data.max:model.radius+(['ArrowLeft','ArrowDown'].includes(event.key)?-1:1)*(event.shiftKey?10:1);
                write(next);h.refresh();h.save(tr('圆角','Rounded corners'));refresh();handles.find(v=>v.node.dataset.corner===String(index))?.node.focus();
            };
        });
        draw.appendChild(group);position();
    }
    function hit(x,y){
        if(h.tool()!=='select'||drag||h.space())return null;
        let best=null;
        handles.forEach((handle,index)=>{const distance=Math.hypot(handle.x-x,handle.y-y);if(distance<=8&&(!best||distance<best.distance))best={type:'corner',handle:true,index,distance};});
        return best;
    }
    function write(radius){
        radius=Math.max(0,Math.min(model.data.max,radius));
        if(Math.abs(model.radius-radius)<1e-7)return;
        model.radius=radius;
        if(model.tag==='rect'){
            for(const name of ['rx','ry']){model.el.setAttribute(name,fmt(radius));model.el.style.setProperty(name,fmt(radius)+'px','important');}
        }else{
            if(model.tag==='polygon'){
                const old=model.el,style=getComputedStyle(old),replacement=make('path',{});
                for(const attr of old.attributes)if(attr.name!=='points')replacement.setAttribute(attr.name,attr.value);
                for(const key of ['fill','fill-opacity','fill-rule','stroke','stroke-width','stroke-opacity','stroke-dasharray','stroke-dashoffset','stroke-linecap','stroke-linejoin','stroke-miterlimit','opacity','paint-order'])replacement.style.setProperty(key,style.getPropertyValue(key),'important');
                replacement.setAttribute('d',path(model.data,radius));
                old.replaceWith(replacement);model.el=replacement;model.tag='path';
                replacement.setAttribute('data-ve-corner-points',JSON.stringify(model.data.points));
                replacement.setAttribute('data-ve-corner-radius',fmt(radius));
                h.select([replacement]);
            }
            // Use the stored precision for repeatable reload/undo validation.
            model.radius=Number(fmt(radius));
            model.el.setAttribute('d',path(model.data,model.radius));model.el.setAttribute('data-ve-corner-radius',fmt(model.radius));
        }
        position();
    }
    function begin(index,event){
        const handle=handles[index];if(!handle||!model)return;
        let inverse;try{inverse=model.el.getScreenCTM().inverse();}catch(_){return;}
        drag={handle,inverse,start:{x:event.clientX,y:event.clientY},radius:model.radius,pending:model.radius,original:model.el.cloneNode(true),changed:false};
    }
    function flush(){cancelAnimationFrame(frame);frame=0;if(drag){write(drag.pending);drag.changed=drag.changed||Math.abs(model.radius-drag.radius)>1e-7;}}
    function finish(){
        if(!drag)return;flush();const changed=drag.changed;drag=null;
        if(changed){h.refresh();h.save(tr('圆角','Rounded corners'));}refresh();
    }
    function cancel(){
        if(!drag)return;cancelAnimationFrame(frame);frame=0;
        const original=drag.original;drag=null;model.el.replaceWith(original);h.select([original]);refresh();
    }
    on(window,'mousemove',event=>{
        if(!drag)return;event.preventDefault();event.stopImmediatePropagation();
        const d=drag,m=d.inverse,dx=event.clientX-d.start.x,dy=event.clientY-d.start.y,b=d.handle.corner.bisector;
        const localX=m.a*dx+m.c*dy,localY=m.b*dx+m.d*dy;
        d.pending=Math.max(0,Math.min(model.data.max,d.radius+(localX*b[0]+localY*b[1])*model.data.max/(d.handle.limit-d.handle.base)));
        if(!frame)frame=requestAnimationFrame(()=>{frame=0;flush();});
    },{capture:true});
    on(window,'mouseup',event=>{if(drag){event.preventDefault();event.stopImmediatePropagation();finish();}},{capture:true});
    on(window,'keydown',event=>{
        if(drag&&(event.key==='Escape'||(event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='z')){event.preventDefault();event.stopImmediatePropagation();cancel();}
    },{capture:true});
    on(window,'blur',finish);
    on(h.canvas,'wheel',finish,{capture:true});
    h.signal.addEventListener('abort',()=>{cancelAnimationFrame(frame);drag=null;group?.remove();},{once:true});
    return {refresh,hit,begin,flush,finish,cancel,active:()=>!!drag,dispose:()=>{cancelAnimationFrame(frame);drag=null;group?.remove();}};
};
