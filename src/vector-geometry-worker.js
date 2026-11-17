'use strict';
// No document/canvas: Paper's geometry-only Project uses a Size instead of a view.
importScripts('vector-codecs.js');
let scope;
function paperScope() {
    if (!scope) {
        importScripts('vendor/vector/paper-core-0.12.18.min.js');
        scope = new paper.PaperScope();
        new scope.Project(new scope.Size(1,1));
        scope.view.autoUpdate = false;
    }
    scope.activate();
    scope.project.clear();
    return scope;
}
function pathItem(s, data) {
    const item = new s.CompoundPath({pathData:data.d,insert:false});
    item.fillRule = data.fillRule === 'evenodd' ? 'evenodd' : 'nonzero';
    if (data.matrix) item.transform(new s.Matrix(...data.matrix));
    return item;
}
self.onmessage = event => {
    const {id,type,...job} = event.data;
    try {
        if (type === 'tiff') {
            const buffer = VectorCodecs.tiff(job.rgba,job.width,job.height,job.dpi);
            self.postMessage({id,result:buffer},[buffer]); return;
        }
        const s = paperScope();
        if (type === 'boolean') {
            if (!Array.isArray(job.paths) || job.paths.length<2 || job.paths.length>200) throw Error('Select 2–200 paths');
            const method = {union:'unite',subtract:'subtract',intersect:'intersect',exclude:'exclude','minus-back':'subtract'}[job.operation];
            if (!method) throw Error('Unknown path operation');
            let result = pathItem(s,job.paths[0]);
            for (const data of job.paths.slice(1)) {
                const next = pathItem(s,data), previous = result;
                result = job.operation === 'minus-back' ? next[method](previous,{insert:false}) : previous[method](next,{insert:false});
                previous.remove(); next.remove();
            }
            self.postMessage({id,result:{d:result.pathData}});
        } else {
            const item = pathItem(s,{d:job.d});
            const paths = item.children || [item];
            if (type === 'simplify') paths.forEach(p=>p.simplify(Math.max(.01,Math.min(100,Number(job.tolerance)||1))));
            if (type === 'nodes' || type === 'normalize') {
                const nodes = paths.map(p=>({closed:p.closed,segments:p.segments.map(v=>({p:[v.point.x,v.point.y],i:[v.handleIn.x,v.handleIn.y],o:[v.handleOut.x,v.handleOut.y]}))}));
                self.postMessage({id,result:{d:item.pathData,paths:nodes}});
            } else if (type === 'simplify') self.postMessage({id,result:{d:item.pathData}});
            else throw Error('Unknown geometry request');
        }
        scope.project.clear();
    } catch(error) {
        scope?.project.clear();
        self.postMessage({id,error:String(error.message || error)});
    }
};
