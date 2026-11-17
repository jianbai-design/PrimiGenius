'use strict';
const { EventEmitter } = require('events');
const { mountScrollingPreview } = require('./file-preview');

function mountSourcePreview(container, { ipc, number, file, lang, wrapLines, onStatus, session, legacy = false }) {
    const client = new EventEmitter();
    const zh = lang === 'zh';
    let closed = false, requestId;
    client.timeoutMs = legacy ? 150000 : 30000;
    client.timeoutMessage = zh ? '读取超时，请重试或检查网络连接。' : 'Read timed out. Retry or check your network connection.';
    client.onFailure = onStatus;
    function progress(_event, value) {
        if (closed || !value || value.requestId !== requestId) return;
        let text = value.phase === 'metadata' ? (zh ? '正在通过内置加速线路读取附件信息…' : 'Loading attachment details using built-in routes…') :
            value.phase === 'verify' ? (zh ? '正在校验附件 SHA256…' : 'Verifying attachment SHA256…') :
            value.phase === 'read' ? (zh ? '正在读取附件中的文件…' : 'Reading file from attachment…') :
            value.phase === 'cached' ? (zh ? '正在读取已校验的临时附件…' : 'Reading verified temporary attachment…') :
            (zh ? '正在下载历史附件（自动选择线路）…' : 'Downloading legacy attachment (automatic route selection)…');
        if (value.phase === 'download' && value.transferred !== undefined) {
            text += ` ${value.transferred}${Number(value.total) > 0 ? ' / ' + value.total : ''} MiB`;
            if (value.speed !== undefined) text += ` · ${value.speed} MiB/s`;
        }
        onStatus(text); client.emit('progress', { text });
    }
    ipc.on?.('market-preview-progress', progress);
    client.terminate = () => { closed = true; ipc.removeListener?.('market-preview-progress', progress); client.removeAllListeners(); };
    client.postMessage = ({ id, page }) => {
        requestId = require('crypto').randomBytes(16).toString('hex');
        ipc.invoke('market-preview', { number, file, page, session, requestId }).then(result => {
            if (closed) return;
            onStatus('');
            client.emit('message', { ...result, id, page, wrapLines });
        }).catch(error => {
            if (closed) return;
            onStatus(error.message);
            client.emit('error', error);
        });
    };
    return mountScrollingPreview(container, file, { kind: 'text', lang, showExternalOpen: false, client });
}
module.exports = { mountSourcePreview };
