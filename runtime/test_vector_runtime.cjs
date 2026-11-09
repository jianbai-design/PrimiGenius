'use strict';
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const {TextHistory, latestFrame} = require('../src/vector-runtime');
const {configureGraphics} = require('../src/gpu-policy');

let seed = 37;
const random = max => { seed = (Math.imul(seed,1664525)+1013904223)>>>0; return seed % max; };
const history = new TextHistory(30);
let text = '<svg><text>中文 🧬</text></svg>';
let states = [text], future = [];
history.commit(text);
for (let index=0;index<2000;index++) {
    const action = random(5);
    if (action === 0) {
        if (states.length > 1) { future.push(states.pop()); text=states.at(-1); assert.equal(history.undo(),text); }
        else assert.equal(history.undo(),null);
    } else if (action === 1) {
        if (future.length) { text=future.pop(); states.push(text); assert.equal(history.redo(),text); }
        else assert.equal(history.redo(),null);
    } else {
        const start = random(text.length+1), count = random(Math.min(6,text.length-start)+1);
        const replacement = ['x','中文','🧬','\ud800','\u0000',''][random(6)];
        const next=text.slice(0,start)+replacement+text.slice(start+count);
        history.commit(next);
        if (next!==text) { text=next; states.push(text); if(states.length>30)states.shift(); future=[]; }
    }
}
history.reset(); assert.equal(history.undo(),null); assert.equal(history.retainedBytes,0);
const large = '<svg>'+'x'.repeat(1024*1024)+'</svg>';
for(let index=0;index<30;index++) history.commit(large+index);
assert.ok(history.retainedBytes < large.length*2+1024);
const historyBytes=history.retainedBytes;
for(let index=28;index>=0;index--) assert.equal(history.undo(),large+index);
assert.equal(history.undo(),null);
history.commit(large+'new'); assert.equal(history.redo(),null);

const frames = new Map(); let next=0, moves=[];
const scheduler = latestFrame(value=>moves.push(value),cb=>{frames.set(++next,cb);return next;},id=>frames.delete(id));
for(let index=0;index<1000;index++)scheduler.schedule(index);
assert.equal(frames.size,1);
const [id,callback]=frames.entries().next().value; frames.delete(id); callback();
assert.deepEqual(moves,[999]);
scheduler.schedule('mouseup');scheduler.flush();assert.deepEqual(moves,[999,'mouseup']);assert.equal(frames.size,0);
scheduler.schedule('closed');scheduler.cancel();assert.equal(frames.size,0);

for(const mode of ['auto','software']) {
    const app = new EventEmitter(); let disabled=0;
    Object.assign(app,{disableHardwareAcceleration:()=>disabled++,isReady:()=>true,getGPUFeatureStatus:()=>({gpu_compositing:mode==='auto'?'enabled':'disabled_software'})});
    const graphics=configureGraphics(app,{PRIMIGENIUS_GPU_MODE:mode});
    assert.equal(disabled,mode==='software'?1:0);
    app.emit('gpu-info-update'); app.emit('child-process-gone',{}, {type:'GPU'});
    assert.equal(graphics.snapshot().gpuFailures,1);
    assert.equal(graphics.snapshot().mode,mode);
}
console.log(JSON.stringify({result:'PASS',randomHistoryOperations:2000,coalescedPointerEvents:1000,
    retainedHistoryBytes:historyBytes,fullSnapshotBytes:(large.length+2)*2*30}));
