'use strict';
const MAX_PIXELS=32_000_000;
async function imageDataUrl(url,signal){
    const response=await fetch(url,{signal});
    if(!response.ok)throw Error('Image request failed (HTTP '+response.status+')');
    const type=(response.headers.get('content-type')||'').split(';')[0].trim().toLowerCase();
    if(!type.startsWith('image/'))throw Error('Image response has an unsupported content type');
    return 'data:'+type+';base64,'+Buffer.from(await response.arrayBuffer()).toString('base64');
}
async function inlineImages(svg,signal){
    const requests=new Map();
    await Promise.all([...svg.querySelectorAll('image')].map(async image=>{
        const href=image.getAttribute('href')||image.getAttributeNS('http://www.w3.org/1999/xlink','href');
        if(!href||href.startsWith('data:')||href.startsWith('#'))return;
        if(!requests.has(href))requests.set(href,imageDataUrl(new URL(href,image.baseURI).href,signal));
        image.setAttribute('href',await requests.get(href));
        image.removeAttributeNS('http://www.w3.org/1999/xlink','href');
    }));
}
function density(svg){const n=Number(svg.getAttribute('data-ve-units-per-inch'));return n>0&&Number.isFinite(n)?n:96;}
function exportSvg(source,board){
    const doc=new DOMParser().parseFromString(source,'image/svg+xml'),svg=doc.documentElement;
    if(svg.tagName!=='svg'||doc.querySelector('parsererror'))throw Error('Invalid SVG document');
    if(![board.x,board.y,board.w,board.h].every(Number.isFinite)||board.w<=0||board.h<=0)throw Error('Invalid artboard');
    const perInch=density(svg);
    svg.setAttribute('width',board.w/perInch+'in');svg.setAttribute('height',board.h/perInch+'in');svg.setAttribute('viewBox',`${board.x} ${board.y} ${board.w} ${board.h}`);
    svg.style.removeProperty('width');svg.style.removeProperty('height');svg.style.setProperty('overflow','hidden');
    svg.querySelectorAll('[data-ve-selected]').forEach(el=>el.removeAttribute('data-ve-selected'));
    return new XMLSerializer().serializeToString(svg);
}
async function encodeRaster(canvas,format,dpi,jobs){
    if(format==='tif'){
        const pixels=canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height);
        return jobs.run('tiff',{rgba:pixels.data.buffer,width:canvas.width,height:canvas.height,dpi},[pixels.data.buffer]);
    }
    const blob=await new Promise((resolve,reject)=>canvas.toBlob(value=>value?resolve(value):reject(Error('PNG encoding failed')),'image/png'));
    return blob.arrayBuffer();
}
module.exports={exportSvg,encodeRaster,imageDataUrl,inlineImages,MAX_PIXELS};
