'use strict';
const cache=new Map();
exports.available=function(family){
    if(!family||/^(sans-serif|serif|monospace|system-ui)$/i.test(family))return true;
    if(cache.has(family))return cache.get(family);
    const canvas=document.createElement('canvas'),ctx=canvas.getContext('2d');
    const text='MWil0123456789汉字Ωμ',safe=family.replace(/["\\]/g,'');
    const measure=font=>{ctx.font='72px '+font;return ctx.measureText(text).width;};
    const present=['monospace','serif'].some(fallback=>Math.abs(measure('"'+safe+'",'+fallback)-measure(fallback))>.01);
    canvas.width=canvas.height=0;if(cache.size>128)cache.clear();cache.set(family,present);return present;
};
