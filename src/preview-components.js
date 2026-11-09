'use strict';
const path=require('path');
function stylesheet(document,file){
    if([...document.querySelectorAll('style[data-preview-stylesheet]')].some(style=>style.dataset.previewStylesheet===file))return;
    // Apply the small local stylesheet before Tabulator measures header and row heights.
    const style=document.createElement('style');style.dataset.previewStylesheet=file;
    style.textContent=require('fs').readFileSync(file,'utf8');document.head.append(style);
}
function table(element,result,{frozen=true,viewport=element.closest('.file-preview-scroll'),headerOnly=false,showHeader=true,columnWidths=[],onColumnWidths}={}){
    const {Tabulator:TabulatorFull}=require('./preview-table');
    stylesheet(element.ownerDocument,path.join(__dirname,'vendor/file-preview/tabulator.min.css'));
    const count=Math.max(result.header.length,...result.rows.map(row=>row.length),1);
    const columns=[{title:'#',field:'_row',width:65,frozen},...Array.from({length:count},(_,index)=>({title:String(result.header[index]??''),field:'c'+index,width:160,frozen:frozen && index===0,formatter:'plaintext'}))];
    columns.forEach((column,index)=>{if(columnWidths[index])column.width=columnWidths[index];});
    const data=result.rows.map((values,index)=>Object.assign({_row:(result.startRow||result.page*100+1)+index},Object.fromEntries(values.map((value,i)=>['c'+i,value??'']))));
    element.style.overflow='visible';
    const grid=new TabulatorFull(element,{data,columns,layout:'fitData',height:headerOnly?false:Math.max(1,data.length*29+(showHeader?35:0)),headerVisible:showHeader,rowHeight:29,
        renderHorizontal:'virtual',renderVertical:'virtual',columnDefaults:{resizable:true,titleFormatter:'plaintext',tooltip:(_event,cell)=>{
            const content=element.ownerDocument.createElement('div');content.textContent=String(cell.getValue()??'');content.style.cssText='white-space:pre-wrap;max-width:70vw;max-height:40vh;overflow:auto;';return content;
        }},index:'_row',placeholder:''});
    function syncHeader(){
        const header=element.querySelector('.tabulator-header');
        if(!header || !viewport)return;
        header.style.position=frozen && !headerOnly?'sticky':'relative';header.style.top='0';
        if(headerOnly){element.style.position=frozen?'sticky':'relative';element.style.top='0';element.style.zIndex='30';element.style.height='auto';}
        header.style.zIndex='20'; // Above Tabulator's frozen body cells (z-index 11).
    }
    grid.on('renderComplete',syncHeader);
    grid.on('tableBuilt',syncHeader);
    syncHeader();
    if(onColumnWidths)grid.on('columnResized',()=>onColumnWidths(grid.getColumns().map(column=>column.getWidth())));
    const dispose=()=>grid.destroy();
    dispose.setFrozen=value=>{
        frozen=value;
        // Use the public column API and preserve user-adjusted widths.
        const definitions=grid.getColumnDefinitions().map((column,index)=>({...column,width:grid.getColumn(column.field).getWidth(),frozen:value && index<2}));
        const holder=element.querySelector('.tabulator-tableholder'),left=holder?.scrollLeft||0;
        grid.setColumns(definitions);
        if(holder)holder.scrollLeft=left;
        syncHeader();
    };
    dispose.setColumnWidths=widths=>{
        grid.getColumns().forEach((column,index)=>{if(widths[index] && column.getWidth()!==widths[index])column.setWidth(widths[index]);});
        syncHeader();
    };
    return dispose;
}
function text(element,result,filePath){
    const cm=require('./preview-codemirror');
    const ext=path.extname(filePath).toLowerCase();
    const language=ext==='.json'?cm.json():['.xml','.svg'].includes(ext)?cm.xml():ext==='.py'?cm.python():ext==='.r'?cm.StreamLanguage.define(cm.r):[];
    const view=new cm.EditorView({parent:element,state:cm.EditorState.create({doc:result.text,extensions:[
        cm.EditorState.readOnly.of(true),cm.EditorView.editable.of(false),cm.lineNumbers({formatNumber:number=>String((result.startLine||1)+number-1)}),cm.highlightSpecialChars(),cm.drawSelection(),
        cm.keymap.of([...cm.searchKeymap,...cm.defaultKeymap]),cm.highlightSelectionMatches(),language,
        cm.syntaxHighlighting(cm.defaultHighlightStyle),cm.EditorView.theme({'&':{fontSize:'13px',color:'#333',backgroundColor:'#fff'},'.cm-content':{paddingTop:'0'},'.cm-scroller':{fontFamily:'Consolas,monospace',overflow:'visible',width:'max-content',minWidth:'100%'}})
    ]})});
    // Preserve the widest measured text even when virtualization removes its line or segment.
    let minimumWidth=0;
    function rememberWidth(){
        const width=view.scrollDOM.scrollWidth;
        if(width>minimumWidth){minimumWidth=width;element.style.minWidth=width+'px';}
    }
    rememberWidth();
    const observer=new element.ownerDocument.defaultView.ResizeObserver(rememberWidth);
    observer.observe(view.scrollDOM);
    return ()=>{observer.disconnect();rememberWidth();view.destroy();};
}
module.exports={table,text};
