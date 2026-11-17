'use strict';
// One lazily built, delegated popover serves both paint controls. No canvas or polling.
const COLORS=[
    '#ffffff','#000000','#ed1c24','#fff200','#00a651','#00aeef','#2e3192','#ec008c','#c1272d','#f15a24','#f7931e','#fbb03b',
    '#fff799','#d9e021','#8cc63f','#39b54a','#009245','#006837','#22b573','#00a99d','#29abe2','#0071bc','#1b1464','#662d91',
    '#93278f','#9e005d','#d4145a','#ed1e79','#f49ac2','#c7b299','#998675','#736357','#534741','#c69c6d','#a67c52','#8c6239',
    '#603813','#42210b','#fde0dd','#fcc5c0','#f768a1','#dd3497','#ae017e','#7a0177','#d9f0a3','#addd8e','#78c679','#238443',
    '#e0f3f8','#abd9e9','#74add1','#4575b4','#313695','#e8ddf4','#bba5d7','#8c6bb1','#6a51a3','#54278f','#fee8c8','#fdbb84',
    '#fff7bc','#fec44f','#fe9929','#d95f0e','#e0e0e0','#cccccc','#b3b3b3','#999999','#808080','#666666','#4d4d4d'
];
function hex(value){const match=String(value).trim().match(/^#?([\da-f]{3}|[\da-f]{6})$/i);if(!match)return null;const raw=match[1];return '#'+(raw.length===3?[...raw].map(c=>c+c).join(''):raw).toLowerCase();}
function solid(value){const direct=hex(value);if(direct)return direct;const rgb=String(value).match(/^rgba?\(\s*([\d.]+)[, ]+([\d.]+)[, ]+([\d.]+)/);return rgb?'#'+rgb.slice(1).map(n=>Math.min(255,Math.max(0,Math.round(Number(n)))).toString(16).padStart(2,'0')).join(''):null;}

exports.mount=function(h){
    const tr=(cn,en)=>h.lang==='zh'?cn:en,fields={},recent=[],cached={};let popup=null,active=null,positionFrame=0;
    const on=(node,type,fn,options={})=>node.addEventListener(type,fn,{...options,signal:h.signal});
    for(const attr of ['fill','stroke'])fields[attr]={input:h.overlay.querySelector('.r-ve-'+attr),trigger:h.overlay.querySelector(`[data-paint="${attr}"]`),none:h.overlay.querySelector('.r-ve-'+attr+'-none'),gradient:h.overlay.querySelector('.r-ve-'+attr+'-gradient-btn')};
    function paint(attr){
        const selected=h.selected();let value;
        for(const el of selected){const next=getComputedStyle(el).getPropertyValue(attr).trim();if(value!==undefined&&next!==value)return {kind:'mixed',value:'mixed',preview:''};value=next;}
        value=value===undefined?(h.none(attr)?'none':fields[attr].input.value):value;
        if(value==='none'||value==='transparent')return {kind:'none',value:'none',preview:''};
        const reference=value.match(/#([^"')]+)["']?\s*\)/),gradient=reference&&h.svg()?.querySelector('#'+CSS.escape(reference[1]));
        if(gradient?.matches('linearGradient,radialGradient')){
            const stops=[...gradient.querySelectorAll('stop')].slice(0,16).map(stop=>{const style=getComputedStyle(stop),offset=stop.getAttribute('offset')||'0';return style.stopColor+' '+(offset.includes('%')?offset:Math.min(100,Math.max(0,Number(offset)*100))+'%');});
            return {kind:'gradient',value,preview:(gradient.tagName==='radialGradient'?'radial-gradient(circle,':'linear-gradient(90deg,')+stops.join(',')+')'};
        }
        // Pattern thumbnails are indicators, without rasterizing or loading artwork URLs.
        if(reference)return {kind:'pattern',value,preview:'repeating-linear-gradient(45deg,#e3e9f0 0 3px,#91a0b1 3px 5px)'};
        return {kind:'solid',value:solid(value)||fields[attr].input.value,preview:value};
    }
    function sync(){
        for(const attr of ['fill','stroke']){
            const state=paint(attr),field=fields[attr];cached[attr]=state;
            const chip=field.trigger.querySelector('.r-ve-paint-chip');chip.dataset.kind=state.kind;chip.style.setProperty('--paint',state.preview||'transparent');
            field.trigger.title=(attr==='fill'?tr('填充','Fill'):tr('描边','Stroke'))+'：'+(state.kind==='mixed'?tr('混合颜色','Mixed colors'):state.kind==='none'?tr('无颜色','None'):state.kind==='gradient'?tr('渐变','Gradient'):state.kind==='pattern'?tr('图案','Pattern'):state.value.toUpperCase())+'；'+tr('点击选择色卡，Shift+点击自定义颜色','Click for swatches; Shift-click for custom color');
            field.trigger.setAttribute('aria-label',field.trigger.title);
        }
        if(active&&popup)markCurrent();
    }
    function close(restoreFocus=false){if(!active)return;const trigger=fields[active].trigger;trigger.setAttribute('aria-expanded','false');active=null;popup.hidden=true;cancelAnimationFrame(positionFrame);positionFrame=0;if(restoreFocus)trigger.focus({preventScroll:true});}
    function position(){
        if(!active||!popup)return;const r=fields[active].trigger.getBoundingClientRect(),p=popup.getBoundingClientRect(),gap=6;
        popup.style.left=Math.max(8,Math.min(r.left,innerWidth-p.width-8))+'px';
        popup.style.top=Math.max(8,Math.min(r.bottom+gap,innerHeight-p.height-8))+'px';
    }
    function tile(value){const b=document.createElement('button');b.type='button';b.className='r-ve-swatch';b.dataset.value=value;b.title=value==='none'?tr('无颜色','None'):value.toUpperCase();b.setAttribute('role','menuitemradio');b.setAttribute('aria-label',b.title);if(value==='none')b.classList.add('is-none');else b.style.backgroundColor=value;return b;}
    function build(){
        if(popup)return;popup=document.createElement('div');popup.className='r-ve-swatches';popup.hidden=true;popup.setAttribute('role','dialog');popup.setAttribute('aria-label',tr('色卡','Swatches'));
        popup.innerHTML=`<header><strong></strong><button type="button" data-action="close" aria-label="${tr('关闭色卡','Close swatches')}">×</button></header><div class="r-ve-swatch-grid" role="menu"></div><section class="r-ve-swatch-recent" hidden><span>${tr('最近使用','Recent colors')}</span><div class="r-ve-swatch-grid" role="menu"></div></section><form><label>HEX<input class="r-ve-swatch-hex" type="text" maxlength="7" spellcheck="false" aria-label="${tr('十六进制颜色','Hex color')}" placeholder="#RRGGBB"></label><button type="submit">${tr('应用','Apply')}</button></form><div class="r-ve-swatch-actions"><button type="button" data-action="custom">${tr('自定义颜色…','Custom color…')}</button><button type="button" data-action="gradient">${tr('渐变…','Gradient…')}</button></div><div class="r-ve-swatch-error" role="alert"></div>`;
        const grid=popup.querySelector('.r-ve-swatch-grid');grid.append(tile('none'),...COLORS.map(tile));h.overlay.appendChild(popup);
        on(popup,'click',event=>{const button=event.target.closest('button');if(!button)return;if(button.dataset.value)apply(button.dataset.value);else if(button.dataset.action==='close')close(true);else if(button.dataset.action==='custom')custom(active);else if(button.dataset.action==='gradient'){const attr=active;close();fields[attr].gradient.click();}});
        on(popup,'submit',event=>{event.preventDefault();const input=popup.querySelector('input'),value=hex(input.value);if(value){apply(value);}else{popup.querySelector('.r-ve-swatch-error').textContent=tr('请输入有效的 HEX 颜色，例如 #1976D2。','Enter a valid HEX color, e.g. #1976D2.');input.setAttribute('aria-invalid','true');}});
        on(popup,'keydown',event=>{if(event.target.tagName==='INPUT')return;const items=[...popup.querySelectorAll('.r-ve-swatch')].filter(el=>!el.closest('[hidden]'));let at=items.indexOf(document.activeElement);if(at<0)return;let next=at;if(event.key==='ArrowRight')next++;else if(event.key==='ArrowLeft')next--;else if(event.key==='ArrowDown')next+=12;else if(event.key==='ArrowUp')next-=12;else if(event.key==='Home')next=0;else if(event.key==='End')next=items.length-1;else return;event.preventDefault();event.stopPropagation();items[(next+items.length)%items.length].focus();});
    }
    function markCurrent(){
        const state=cached[active];popup.querySelector('strong').textContent=active==='fill'?tr('填充色卡','Fill swatches'):tr('描边色卡','Stroke swatches');
        popup.querySelector('input').value=state.kind==='solid'?state.value.toUpperCase():fields[active].input.value.toUpperCase();
        popup.querySelector('.r-ve-swatch-error').textContent='';popup.querySelector('input').removeAttribute('aria-invalid');
        popup.querySelectorAll('[data-value]').forEach(b=>b.setAttribute('aria-checked',String(b.dataset.value===state.value)));
    }
    function open(attr,event){
        if(event.shiftKey){custom(attr);return;}if(active===attr){close();return;}close();build();active=attr;popup.hidden=false;fields[attr].trigger.setAttribute('aria-expanded','true');markCurrent();position();(popup.querySelector('[aria-checked="true"]')||popup.querySelector('.r-ve-swatch')).focus({preventScroll:true});
    }
    function custom(attr){
        close();const input=fields[attr].input;try{if(input.showPicker)input.showPicker();else input.click();}catch(_){build();active=attr;popup.hidden=false;fields[attr].trigger.setAttribute('aria-expanded','true');markCurrent();position();popup.querySelector('input').focus();}
    }
    function apply(value){
        if(!active)return;const field=fields[active];
        if(value==='none')h.clear(active);
        else{field.input.value=value;field.input.dispatchEvent(new Event('input',{bubbles:true}));field.input.dispatchEvent(new Event('change',{bubbles:true}));const old=recent.indexOf(value);if(old>=0)recent.splice(old,1);recent.unshift(value);recent.length=Math.min(recent.length,12);const section=popup.querySelector('.r-ve-swatch-recent');section.hidden=false;section.querySelector('.r-ve-swatch-grid').replaceChildren(...recent.map(tile));}
        markCurrent();
    }
    for(const attr of ['fill','stroke']){
        on(fields[attr].trigger,'click',event=>open(attr,event));
        on(fields[attr].input,'input',()=>{const chip=fields[attr].trigger.querySelector('.r-ve-paint-chip');chip.dataset.kind='solid';chip.style.setProperty('--paint',fields[attr].input.value);});
        on(fields[attr].input,'change',sync);
    }
    on(document,'mousedown',event=>{if(active&&!popup.contains(event.target)&&!fields.fill.trigger.contains(event.target)&&!fields.stroke.trigger.contains(event.target))close();},{capture:true});
    on(document,'keydown',event=>{if(active&&event.key==='Escape'){event.preventDefault();event.stopImmediatePropagation();close(true);}},{capture:true});
    on(window,'resize',()=>{if(active&&!positionFrame)positionFrame=requestAnimationFrame(()=>{positionFrame=0;position();});});
    h.signal.addEventListener('abort',()=>{cancelAnimationFrame(positionFrame);popup?.remove();recent.length=0;},{once:true});
    sync();return {sync,close};
};
