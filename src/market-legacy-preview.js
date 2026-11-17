'use strict';
const fs = require('fs'), os = require('os'), path = require('path'), crypto = require('crypto');

function removeTemporary(directory) {
    const absolute = path.resolve(directory);
    if (path.dirname(absolute) !== path.resolve(os.tmpdir()) || !path.basename(absolute).startsWith('primigenius-market-preview-')) {
        throw new Error('附件临时目录不合法');
    }
    fs.rmSync(absolute, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
}

// One verified temporary attachment per inspect window, shared by concurrent file reads.
class LegacyPreviewStore {
    constructor({ readPublic, download, hash, maxSize, idleMs = 0 }) {
        Object.assign(this, { readPublic, download, hash, maxSize, idleMs });
        this.sessions = new Map();
        this.closed = new Set();
    }
    close(id, retire = true) {
        if (retire) {
            this.closed.add(id);
            if (this.closed.size > 128) this.closed.delete(this.closed.values().next().value);
        }
        const item = this.sessions.get(id);
        if (!item) return;
        this.sessions.delete(id); clearTimeout(item.timer); item.closed = true;
        if (item.settled && !item.readers) removeTemporary(item.directory);
    }
    closeAll() { for (const id of this.sessions.keys()) this.close(id); }
    async read({ id, source, author, progress, readFile }) {
        const compatibility = !id;
        const key = `${source.repository}:${source.asset_id}:${source.sha256}:${author.id}`;
        // Older preview callers omit the dialog ID. Reuse their immutable attachment too.
        id ||= crypto.createHash('sha256').update('legacy:' + key).digest('hex').slice(0, 32);
        if (!/^[a-f0-9]{32}$/.test(id)) throw new Error('预览会话不合法');
        if (this.closed.has(id)) throw new Error('文件审核窗口已关闭');
        let item = this.sessions.get(id);
        if (item && item.key !== key) throw new Error('预览会话与提交不一致');
        if (!item) {
            if (this.sessions.size >= 4) throw new Error('请先关闭其他文件审核窗口');
            item = { key, directory: fs.mkdtempSync(path.join(os.tmpdir(), 'primigenius-market-preview-')),
                listeners: new Set(), readers: 0, closed: false, settled: false };
            this.sessions.set(id, item);
            const emit = update => { item.progress = update; for (const listener of item.listeners) listener(update); };
            item.ready = Promise.resolve().then(async () => {
                emit({ phase: 'metadata' });
                const route = `/repos/${source.repository}`;
                const [, asset] = await Promise.all([
                    this.readPublic(route, repository => repository.owner?.id === author.id && repository.private === false),
                    this.readPublic(`${route}/releases/assets/${source.asset_id}`, asset =>
                        asset.browser_download_url?.startsWith(`https://github.com/${source.repository}/releases/download/`) &&
                        Number.isSafeInteger(asset.size) && asset.size > 0 && asset.size <= this.maxSize)
                ]);
                if (item.closed) throw new Error('文件审核窗口已关闭');
                const zip = path.join(item.directory, 'source.zip');
                emit({ phase: 'download' });
                await this.download(asset.browser_download_url, zip, async candidate => {
                    emit({ phase: 'verify' });
                    if (fs.statSync(candidate).size !== asset.size || await this.hash(candidate) !== source.sha256) throw new Error('原始附件摘要不一致');
                    return true;
                }, { onProgress: value => emit({ ...value, phase: 'download' }), log: () => emit({ phase: 'download' }) });
                emit({ phase: 'cached' });
                return zip;
            }).finally(() => {
                item.settled = true;
                if (item.closed && !item.readers) removeTemporary(item.directory);
            });
        }
        clearTimeout(item.timer); item.readers++; item.listeners.add(progress);
        if (item.progress) progress(item.progress);
        let verified = false;
        try {
            const zip = await item.ready;
            verified = true;
            if (item.closed) throw new Error('文件审核窗口已关闭');
            progress({ phase: 'read' });
            return await readFile(zip);
        } catch (error) { if (!verified && this.sessions.get(id) === item) this.close(id, false); throw error; }
        finally {
            item.listeners.delete(progress); item.readers--;
            if (item.closed && item.settled && !item.readers) removeTemporary(item.directory);
            else if (!item.closed && !item.readers && (compatibility || this.idleMs > 0)) {
                // Dialog-owned attachments live until close; only unidentified legacy callers expire.
                item.timer = setTimeout(() => this.close(id, false), this.idleMs || 10 * 60 * 1000); item.timer.unref?.();
            }
        }
    }
}
module.exports = { LegacyPreviewStore };
