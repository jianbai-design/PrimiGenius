'use strict';
// One worker per editor, loaded on demand, released after 15 seconds without work.
exports.createJobs = function() {
    let worker, counter = 0, idle;
    const pending = new Map();
    function dispose(reason = 'Editor closed') {
        clearTimeout(idle); worker?.terminate(); worker = null;
        for (const request of pending.values()) { clearTimeout(request.timer); request.reject(Error(reason)); }
        pending.clear();
    }
    function run(type, data, transfer = []) {
        clearTimeout(idle);
        if (!worker) {
            worker = new Worker(new URL('vector-geometry-worker.js',document.baseURI).href);
            worker.onmessage = ({data}) => {
                const request = pending.get(data.id); if (!request) return;
                pending.delete(data.id); clearTimeout(request.timer);
                data.error ? request.reject(Error(data.error)) : request.resolve(data.result);
                if (!pending.size) idle = setTimeout(()=>dispose(),15000);
            };
            worker.onerror = error => dispose(error.message || 'Geometry worker failed');
        }
        const id = ++counter;
        return new Promise((resolve,reject)=>{
            const timer = setTimeout(()=>dispose('Geometry operation timed out; original artwork is unchanged'),30000);
            pending.set(id,{resolve,reject,timer});
            try { worker.postMessage({id,type,...data},transfer); }
            catch(error) { dispose(error.message); }
        });
    }
    return {run,dispose};
};
