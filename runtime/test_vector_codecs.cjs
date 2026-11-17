'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=path.resolve(__dirname,'../src'),messages=[];
const context={console,navigator:{userAgent:'geometry-test'},setTimeout,clearTimeout,setInterval,clearInterval,postMessage:message=>messages.push(message)};
context.self=context;vm.createContext(context);
context.importScripts=(...files)=>files.forEach(file=>vm.runInContext(fs.readFileSync(path.join(source,file),'utf8'),context,{filename:file}));
vm.runInContext(fs.readFileSync(path.join(source,'vector-geometry-worker.js'),'utf8'),context);
function job(type,data){messages.length=0;context.onmessage({data:{id:1,type,...data}});assert.equal(messages.length,1);assert.equal(messages[0].error,undefined);return messages[0].result;}
function area(d){const p=new context.paper.CompoundPath({pathData:d,insert:false});p.reorient(true,true);const result=Math.abs(p.area);p.remove();return result;}
const square='M0 0H10V10H0Z',overlap='M5 0H15V10H5Z';
for(const [operation,expected]of Object.entries({union:150,subtract:50,intersect:50,exclude:100,'minus-back':50})){
    const {d}=job('boolean',{operation,paths:[{d:square},{d:overlap}]});assert.ok(Math.abs(area(d)-expected)<.001,operation+' area');
}
const curved='M0 0C0 10 10 10 10 0L0 0Z';
const {d}=job('boolean',{operation:'union',paths:[{d:curved,matrix:[2,0,0,2,20,10]},{d:square}]});
assert.match(d,/[cC]/);assert.ok(Math.abs(area(d)-(area(curved)*4+100))<.01,'Curves and transforms must preserve area');
const hole='M0 0H10V10H0Z M3 3H7V7H3Z';
const ring=job('boolean',{operation:'union',paths:[{d:hole,fillRule:'evenodd'},{d:'M20 0H30V10H20Z'}]});
assert.ok(Math.abs(area(ring.d)-184)<.01,'Even-odd holes must survive');
const nodes=job('nodes',{d:curved});assert.ok(nodes.paths.some(p=>p.segments.some(v=>v.i.some(n=>n!==0)||v.o.some(n=>n!==0))));
const pixels=new Uint8Array([255,0,0,255,0,255,0,128]),encoded=job('tiff',{rgba:pixels.buffer,width:2,height:1,dpi:300});
const v=new DataView(encoded);assert.equal(v.getUint16(0,true),0x4949);assert.equal(v.getUint16(2,true),42);
const tags=new Map();for(let i=0;i<v.getUint16(8,true);i++){const at=10+i*12;tags.set(v.getUint16(at,true),{type:v.getUint16(at+2,true),count:v.getUint32(at+4,true),value:v.getUint32(at+8,true)});}
assert.equal(tags.get(256).value,2);assert.equal(tags.get(257).value,1);assert.equal(tags.get(259).value,1);assert.equal(tags.get(338).value,2);
assert.deepEqual([...new Uint8Array(encoded,tags.get(273).value)],[...pixels]);assert.equal(v.getUint32(tags.get(282).value,true)/v.getUint32(tags.get(282).value+4,true),300);
const {tiff,tiffSize}=require('../src/vector-codecs');assert.equal(tiffSize(2,1),encoded.byteLength);assert.throws(()=>tiff(pixels.buffer,0,1));assert.throws(()=>tiff(pixels.buffer,32000001,1));assert.throws(()=>tiff(pixels.buffer,2,2));
console.log(JSON.stringify({result:'PASS',checks:['all five Boolean areas','transformed cubic curves','even-odd holes','Bézier handles','TIFF header/pixels/alpha/DPI','allocation limits']}));
