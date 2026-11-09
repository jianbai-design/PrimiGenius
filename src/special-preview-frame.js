/* Runs only in the opaque preview frame, without Node or plugin resources. */
'use strict';
let config,viewer,pool;
const surface=document.getElementById('viewer');
const report=(type,text)=>parent.postMessage({token:previewToken,type,text},'*');
async function tiff(){
    const source=await GeoTIFF.fromUrl(config.files[0].url,{cacheSize:32,blockSize:65536});
    const image=await source.getImage(),width=image.getWidth(),height=image.getHeight();
    const bits=image.fileDirectory.getValue('BitsPerSample')||[8],samples=image.getSamplesPerPixel();
    const rows=image.fileDirectory.getValue('RowsPerStrip')||height;
    const tileWidth=image.fileDirectory.getValue('TileWidth'),tileHeight=image.fileDirectory.getValue('TileLength');
    const blockPixels=tileWidth?tileWidth*tileHeight:width*Math.min(rows,height);
    if(blockPixels*samples*Math.max(...bits)/8>64*1024*1024)throw Error(config.zh?'TIFF 单个解码块超过 64 MB，请转为较小分块的 TIFF 后预览。':'TIFF decoding block exceeds 64 MB; use a TIFF with smaller tiles.');
    pool=new GeoTIFF.Pool(2);
    const maxLevel=Math.ceil(Math.log2(Math.max(width,height))),size=256;
    const tiles=new OpenSeadragon.TileSource({width,height,tileSize:size,minLevel:0,maxLevel});
    tiles.getTileUrl=(level,x,y)=>`${config.files[0].url}?level=${level}&x=${x}&y=${y}`;
    tiles.getTilePostData=(level,x,y)=>({level,x,y});
    tiles.downloadTileStart=async job=>{
        try{
            const {level,x,y}=job.postData,scale=2**(maxLevel-level),x0=x*size*scale,y0=y*size*scale;
            const x1=Math.min(width,x0+size*scale),y1=Math.min(height,y0+size*scale),w=Math.ceil((x1-x0)/scale),h=Math.ceil((y1-y0)/scale);
            const pixels=await image.readRGB({window:[x0,y0,x1,y1],width:w,height:h,pool,interleave:true,enableAlpha:true,resampleMethod:'bilinear'});
            const canvas=document.createElement('canvas');canvas.width=w;canvas.height=h;const context=canvas.getContext('2d'),data=context.createImageData(w,h),channels=pixels.length/(w*h);
            if(!ArrayBuffer.isView(pixels)||![3,4].includes(channels))throw Error('Unsupported TIFF color channels');
            for(let i=0;i<w*h;i++){data.data[i*4]=pixels[i*channels];data.data[i*4+1]=pixels[i*channels+1];data.data[i*4+2]=pixels[i*channels+2];data.data[i*4+3]=channels===4?pixels[i*4+3]:255;}
            context.putImageData(data,0,0);job.finish(context,null,'context2d');
        }catch(error){job.fail(error.message);report('error',error.message);}
    };
    tiles.downloadTileAbort=()=>{};
    viewer=OpenSeadragon({element:surface,tileSources:tiles,showNavigationControl:false,imageLoaderLimit:2,maxImageCacheCount:64,animationTime:0.2,minZoomImageRatio:0.1,maxZoomPixelRatio:10});
    let lastZoom;
    function updateZoom(){const percent=Math.round(viewer.viewport.viewportToImageZoom(viewer.viewport.getZoom())*100);if(percent!==lastZoom){lastZoom=percent;report('zoom',String(percent));}}
    viewer.addHandler('open',updateZoom);viewer.addHandler('viewport-change',updateZoom);
    viewer.addOnceHandler('tile-loaded',()=>report('ready',config.zh?'TIFF 分块预览':'Tiled TIFF preview'));
}
async function structure(){
    const source=config.files[0],format=source.name.split('.').pop().toLowerCase();
    const data=await (await fetch(source.url)).text();
    viewer=$3Dmol.createViewer(surface,{backgroundColor:'white',antialias:true});viewer.addModel(data,format==='mmcif'?'cif':format);
    viewer.setStyle({},['sdf','mol2','xyz'].includes(format)?{stick:{radius:0.15}}:{cartoon:{color:'spectrum'},stick:{radius:0.12}});viewer.zoomTo();viewer.render();
    new ResizeObserver(()=>viewer.resize()).observe(surface);
    report('ready',config.zh?'三维结构预览':'3D structure preview');
}
async function genome(){
    const reference=config.files.find(file=>/\.(fa|fasta|fna)$/i.test(file.name));
    const index=reference&&config.files.find(file=>file.name===reference.name+'.fai');
    if(!reference||!index){report('ready',config.zh?'请选择本地 FASTA 参考基因组和对应 FAI 索引；BAM/CRAM/压缩 VCF 也需要对应索引。':'Select a local reference FASTA and FAI. Alignments and compressed VCF also need their indexes.');return;}
    if(viewer)igv.removeBrowser(viewer);
    const tracks=config.files.filter(file=>/\.(bam|cram|vcf|bcf|bed|gff3?|gtf|bw|bigwig|bb|bigbed)(\.gz)?$/i.test(file.name)).map(file=>{
        if(/\.bcf$/i.test(file.name))throw Error(config.zh?'BCF 请先转换为带索引的 VCF，或使用系统应用打开。':'Convert BCF to indexed VCF or open externally.');
        const type=/\.(bam|cram)$/i.test(file.name)?'alignment':/\.(vcf|bcf)(\.gz)?$/i.test(file.name)?'variant':/\.(bw|bigwig)$/i.test(file.name)?'wig':'annotation';
        const names=[file.name+'.bai',file.name+'.crai',file.name+'.tbi',file.name+'.csi'];
        if(/\.bam$/i.test(file.name))names.push(file.name.replace(/\.bam$/i,'.bai'));
        const index=config.files.find(item=>names.includes(item.name));
        const rangeIndexed=/\.(bw|bigwig|bb|bigbed)$/i.test(file.name);
        if(!index&&!rangeIndexed&&(/\.(bam|cram|gz)$/i.test(file.name)||file.size>32*1024*1024))throw Error(config.zh?'此轨道需要对应索引文件后才能预览：'+file.name:'An index is required to preview '+file.name);
        return {name:file.name,url:file.url,filename:file.name,type,indexURL:index?.url,indexed:!!index};
    });
    viewer=await igv.createBrowser(surface,{reference:{id:'local',name:reference.name,fastaURL:reference.url,indexURL:index.url},tracks,loadDefaultGenomes:false,search:false,showIdeogram:false,showRuler:true});
    report('ready',config.zh?'本地基因组轨道预览':'Local genome tracks');
}
addEventListener('message',async event=>{
    if(event.source!==parent||event.data?.token!==previewToken)return;
    try{
        if(event.data.type==='init'&&!config){config=event.data;await ({tiff,structure,genome}[config.kind])();}
        else if(event.data.type==='files'&&config?.kind==='genome'){config.files=event.data.files;await genome();}
        else if(event.data.type==='control'&&config?.kind==='tiff'&&viewer?.viewport){
            if(event.data.action==='zoomfit')viewer.viewport.goHome();
            else if(['zoomin','zoomout'].includes(event.data.action))viewer.viewport.zoomBy(event.data.action==='zoomin'?1.25:0.8);
            viewer.viewport.applyConstraints();
        }
        else if(event.data.type==='control'&&config?.kind==='structure'&&viewer){
            if(event.data.action==='cartoon')viewer.setStyle({},{cartoon:{color:'spectrum'}});
            else if(event.data.action==='sticks')viewer.setStyle({},{stick:{radius:0.15}});
            else if(event.data.action==='fit')viewer.zoomTo();
            else return;
            viewer.render();
        }
    }catch(error){report('error',error.message);}
});
addEventListener('pagehide',()=>{pool?.destroy();if(config?.kind==='genome'&&viewer)igv.removeBrowser(viewer);else viewer?.destroy?.();});
