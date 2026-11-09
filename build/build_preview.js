'use strict';
const path=require('path'),fs=require('fs');
const root=path.resolve(__dirname,'..');
const bundled=new Set();
for(const name of ['codemirror','table','sanitize']){
    const result=require('esbuild').buildSync({
        entryPoints:[path.join(root,`src/preview-${name}-entry.js`)],outfile:path.join(root,`src/preview-${name}.js`),
        bundle:true,format:'cjs',platform:'browser',target:'chrome120',minify:true,legalComments:'external',metafile:true});
    for(const input of Object.keys(result.metafile.inputs)){
        const match=/(?:^|\/)node_modules\/((?:@[^/]+\/)?[^/]+)/.exec(input.replace(/\\/g,'/'));
        if(match)bundled.add(match[1]);
    }
}
// Ship only the browser assets used by the generic preview, together with their licenses.
const destination=path.join(root,'src/vendor/file-preview');fs.mkdirSync(destination,{recursive:true});
for(const [module,relative,name] of [
    ['3dmol','3Dmol-min.js','3dmol.min.js'],['igv','igv.min.js','igv.min.js'],
    ['geotiff','../dist-browser/geotiff.js','geotiff.js'],['openseadragon','openseadragon.min.js','openseadragon.min.js'],
    ['tabulator-tables','../css/tabulator.min.css','tabulator.min.css']]){
    const input=path.resolve(path.dirname(require.resolve(module)),relative);
    fs.copyFileSync(input,path.join(destination,name));
    const packageRoot=path.join(root,'node_modules',module);
    const license=fs.readdirSync(packageRoot).find(file=>/^licen[cs]e(\.|$)/i.test(file));
    if(!license)throw Error('Missing preview dependency license: '+module);
    fs.copyFileSync(path.join(packageRoot,license),path.join(destination,module+'-LICENSE.txt'));
}
const notices=[];
for(const module of [...bundled].sort()){
    const packageRoot=path.join(root,'node_modules',module),metadata=require(path.join(packageRoot,'package.json'));
    const license=fs.readdirSync(packageRoot).find(file=>/^licen[cs]e(\.|$)/i.test(file));
    if(!license)throw Error('Missing bundled dependency license: '+module);
    notices.push(`${module} ${metadata.version}\n${fs.readFileSync(path.join(packageRoot,license),'utf8')}`);
}
fs.writeFileSync(path.join(destination,'bundled-LICENSES.txt'),notices.join('\n\n'));
