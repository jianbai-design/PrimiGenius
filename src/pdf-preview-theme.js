'use strict';

function applyPdfPreviewTheme() {
    const viewer = document.querySelector('pdf-viewer');
    if (!viewer || viewer.dataset.primigeniusTheme) return;
    viewer.dataset.primigeniusTheme = 'gray';
    const css = `
        :host, :root { color-scheme:light; color:#374151; --viewer-pdf-toolbar-background-color:#e5e7eb; --viewer-pdf-sidenav-background-color:#e5e7eb; }
        #pagelength, #page-length { color:#374151 !important; }
        viewer-toolbar, viewer-pdf-sidenav { --pdf-toolbar-text-color:#374151 !important; color:#374151; }
        #toolbar, #sidenav, #sidenav-container, #main, #scroller { background:#e5e7eb !important; color:#374151 !important; }
        cr-icon-button { --cr-icon-button-fill-color:#4b5563 !important; --cr-icon-button-focus-outline-color:#2563eb; }
        cr-icon-button:hover { background:rgba(55,65,81,.10) !important; }
        input { background:#f9fafb !important; color:#374151 !important; border:1px solid #c4c9d0 !important; border-radius:4px; }
        input:focus { outline:2px solid #93b4e8 !important; }
        .vertical-separator { background:#b8bec7 !important; }
        #pageNumber, #pageselector, #pageLength, viewer-page-selector { color:#374151 !important; }
        cr-action-menu, viewer-bookmark { --cr-menu-background-color:#f9fafb !important; --cr-primary-text-color:#374151 !important; --cr-menu-background-focus-color:#e5e7eb !important; }
    `;
    const styled = new WeakSet();
    function styleTree(root) {
        if (!styled.has(root)) {
            styled.add(root);
            const style = document.createElement('style');
            style.textContent = css;
            (root === document ? document.head : root).append(style);
            observer.observe(root, {childList:true, subtree:true});
        }
        root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) styleTree(el.shadowRoot); });
    }
    const observer = new MutationObserver(() => styleTree(document));
    styleTree(document);
    // The native PDF canvas background is independent of CSS.
    const setCanvasBackground = () => viewer.pluginController_?.setBackgroundColor(0xffe5e7eb);
    viewer.getBackgroundColor = () => 0xffe5e7eb;
    setCanvasBackground();
    // Loading the document can reset the native canvas after dom-ready.
    let attempts = 0;
    const loading = setInterval(() => {
        if (viewer.loadProgress_ >= 100 || ++attempts >= 300) {
            setCanvasBackground();
            clearInterval(loading);
        }
    }, 100);
    window.addEventListener('unload', () => { clearInterval(loading); observer.disconnect(); }, {once:true});
}

function installPdfPreviewTheme(app) {
    app.on('web-contents-created', (_event, contents) => {
        contents.on('dom-ready', () => {
            if (contents.getURL() !== 'chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai/index.html') return;
            contents.executeJavaScript(`(${applyPdfPreviewTheme.toString()})()`).catch(error => console.warn('PDF theme:', error.message));
        });
    });
}
module.exports = {installPdfPreviewTheme, applyPdfPreviewTheme};
