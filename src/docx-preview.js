'use strict';

const fs = require('fs').promises;
const crypto = require('crypto');
let assets;

function loadAssets() {
    if (!assets) {
        assets = Promise.all([
            fs.readFile(require.resolve('jszip/dist/jszip.min.js'), 'utf8'),
            fs.readFile(require('path').join(require('path').dirname(require.resolve('docx-preview')), 'docx-preview.min.js'), 'utf8')
        ]).catch(error => { assets = undefined; throw error; });
    }
    return assets;
}

function mountDocxPreview(container, filePath, {lang = 'zh', onDimensions = () => {}, showExternalOpen = true} = {}) {
    const doc = container.ownerDocument, win = doc.defaultView, zh = lang === 'zh';
    const token = crypto.randomBytes(16).toString('hex');
    const status = doc.createElement('div'), label = doc.createElement('span'), frame = doc.createElement('iframe');
    status.style.cssText = 'padding:10px 14px;background:#f5f5f5;color:#555;font-size:12px;flex-shrink:0;margin-top:auto;';
    label.textContent = zh ? '正在加载 Word 文档…' : 'Loading Word document…';
    status.append(label);
    frame.title = zh ? 'Word 文档预览' : 'Word document preview';
    frame.setAttribute('sandbox', 'allow-scripts');
    frame.style.cssText = 'width:100%;border:0;flex:1;min-height:0;background:#eee;';
    container.style.cssText += ';display:flex;flex-direction:column;min-height:0;overflow:hidden;flex:1;';
    container.replaceChildren(frame, status);
    if (showExternalOpen) {
        const open = doc.createElement('button');
        open.className = 'r-viewer-action-btn';
        status.className = 'r-viewer-footer';
        open.textContent = zh ? '使用系统应用打开' : 'Open with system application';
        open.onclick = () => require('electron').shell.openPath(filePath);
        status.append(' ', open);
    }
    let disposed = false, timer;
    const dispose = () => {
        if (disposed) return;
        disposed = true;
        clearTimeout(timer);
        observer.disconnect();
        win.removeEventListener('message', onMessage);
        frame.remove();
    };
    const fail = message => {
        if (disposed) return;
        status.hidden = false;
        label.textContent = message;
        dispose();
    };
    function onMessage(event) {
        if (event.source !== frame.contentWindow || event.data?.token !== token || disposed) return;
        if (event.data.type === 'rendered') {
            clearTimeout(timer);
            label.textContent = '';
            status.hidden = !showExternalOpen;
            onDimensions('DOCX');
            frame.dataset.rendered = 'true';
        } else if (event.data.type === 'error') {
            fail(zh ? '无法预览此 DOCX，请使用系统应用打开。' : 'Unable to preview this DOCX. Open it with the system application.');
        }
    }
    const observer = new win.MutationObserver(() => { if (!container.isConnected) dispose(); });
    observer.observe(doc.body, {childList: true, subtree: true});
    win.addEventListener('message', onMessage);
    timer = setTimeout(() => fail(zh ? 'DOCX 预览超时，请使用系统应用打开。' : 'DOCX preview timed out. Open with the system application.'), 30000);
    (async () => {
        try {
            const stat = await fs.stat(filePath);
            if (!stat.isFile()) throw Error(zh ? '请选择 DOCX 文件。' : 'Select a DOCX file.');
            if (stat.size > 32 * 1024 * 1024) throw Error(zh
                ? 'DOCX 超过 32 MB 快速预览上限，请使用系统应用打开。'
                : 'DOCX exceeds the 32 MB quick-preview limit. Open with the system application.');
            const [data, scripts] = await Promise.all([fs.readFile(filePath), loadAssets()]);
            if (disposed) return;
            const buffer = Uint8Array.from(data).buffer;
            frame.onload = () => {
                if (!disposed) frame.contentWindow.postMessage({token, buffer}, '*', [buffer]);
            };
            const script = text => `<script nonce="${token}">${text.replace(/<\/script/gi, '<\\/script')}<\/script>`;
            // All scripts are bundled locally. The opaque sandbox has no Node API,
            // and document links, external images/fonts and network access are blocked.
            frame.srcdoc = `<!doctype html><meta charset="utf-8">
                <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'nonce-${token}'; style-src 'unsafe-inline'; img-src data: blob:; font-src data: blob:;">
                <style>body{margin:0;background:#eee;color:#222} .docx-wrapper{padding:16px!important} section.docx{max-width:100%;box-sizing:border-box} img{max-width:100%}</style>
                <main id="document"></main>${scripts.map(script).join('')}${script(`
                    let received = false;
                    document.addEventListener('click', event => { if (event.target.closest('a')) event.preventDefault(); });
                    addEventListener('message', async event => {
                        if (received || event.source !== parent || event.data?.token !== '${token}') return;
                        received = true;
                        try {
                            await docx.renderAsync(event.data.buffer, document.getElementById('document'), null, {renderAltChunks:false});
                            parent.postMessage({token:'${token}', type:'rendered'}, '*');
                        } catch (_) { parent.postMessage({token:'${token}', type:'error'}, '*'); }
                    });
                `)}`;
        } catch (error) { fail(error.message); }
    })();
    return dispose;
}

module.exports = {mountDocxPreview};
