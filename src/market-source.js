'use strict';
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const hash = raw => crypto.createHash('sha256').update(raw).digest('hex');
function blobSha(raw) {
    return crypto.createHash('sha1').update(`blob ${raw.length}\0`).update(raw).digest('hex');
}
function decodeBlob(value, sha) {
    if (value.encoding !== 'base64' || typeof value.content !== 'string') throw new Error('源码内容编码不合法');
    const raw = Buffer.from(value.content, 'base64');
    if (blobSha(raw) !== sha) throw new Error('仓库源码内容校验失败');
    return raw;
}

async function commitSource({ api, repository, branch, pluginId, directory, index, version }) {
    if (!/^[A-Za-z0-9_.-]+$/.test(pluginId) || !Object.keys(index.files || {}).length) throw new Error('缺少有效的插件源码');
    const route = `/repos/${repository}`;
    const branchPath = branch.split('/').map(encodeURIComponent).join('/');
    const head = await api(`${route}/git/ref/heads/${branchPath}`);
    const base = await api(`${route}/git/commits/${head.object.sha}`);
    const previous = await api(`${route}/git/trees/${base.tree.sha}?recursive=1`);
    if (previous.truncated) throw new Error('作者仓库文件树过大，无法安全同步源码');
    const known = new Set(previous.tree.filter(item => item.type === 'blob').map(item => item.sha));
    const sourceTree = [], chunkTree = new Map();
    async function upload(raw, sha) {
        if (blobSha(raw) !== sha) throw new Error('本地源码发生变化');
        if (known.has(sha)) return;
        const result = await api(`${route}/git/blobs`, 'POST', { encoding: 'base64', content: raw.toString('base64') });
        if (result.sha !== sha) throw new Error('上传源码摘要不一致');
        known.add(sha);
    }
    // Upload sequentially: only one source file and one bounded fragment are held at a time.
    for (const [name, file] of Object.entries(index.files)) {
        const target = path.resolve(directory, ...name.split('/'));
        if (!target.startsWith(path.resolve(directory) + path.sep)) throw new Error('源码路径不合法');
        const raw = await fs.promises.readFile(target);
        if (raw.length !== file.size || hash(raw) !== file.sha256) throw new Error('本地源码发生变化');
        await upload(raw, file.sha);
        sourceTree.push({ path: name, mode: file.mode, type: 'blob', sha: file.sha });
        for (let n = 0; n < file.chunks.length; n++) {
            const chunk = file.chunks[n], bytes = raw.subarray(chunk.offset, chunk.offset + chunk.size);
            if (hash(bytes) !== chunk.sha256) throw new Error('源码分块摘要不一致');
            await upload(bytes, chunk.sha);
            const chunkPath = `${file.sha256}/${n}`;
            chunkTree.set(chunkPath, { path: chunkPath, mode: '100644', type: 'blob', sha: chunk.sha });
        }
    }
    const indexRaw = Buffer.from(JSON.stringify(index));
    const indexSha = blobSha(indexRaw);
    await upload(indexRaw, indexSha);
    chunkTree.set('index.json', { path: 'index.json', mode: '100644', type: 'blob', sha: indexSha });
    const source = await api(`${route}/git/trees`, 'POST', { tree: sourceTree });
    const chunks = await api(`${route}/git/trees`, 'POST', { tree: [...chunkTree.values()] });
    // Replace only this plugin's two subtrees, preserving other plugins and author files.
    for (let attempt = 0; attempt < 3; attempt++) {
        const ref = await api(`${route}/git/ref/heads/${branchPath}`);
        const parent = await api(`${route}/git/commits/${ref.object.sha}`);
        const tree = await api(`${route}/git/trees`, 'POST', { base_tree: parent.tree.sha, tree: [
            { path: `plugins/${pluginId}`, mode: '040000', type: 'tree', sha: source.sha },
            { path: `.primigenius-preview/${pluginId}`, mode: '040000', type: 'tree', sha: chunks.sha }
        ] });
        const commit = await api(`${route}/git/commits`, 'POST', {
            message: `Update ${pluginId} source to ${version}`, tree: tree.sha, parents: [ref.object.sha] });
        try {
            await api(`${route}/git/refs/heads/${branchPath}`, 'PATCH', { sha: commit.sha, force: false });
            return { commit: commit.sha, path: `plugins/${pluginId}`, index_sha: indexSha,
                preview_path: `.primigenius-preview/${pluginId}` };
        } catch (error) { if (![409, 422].includes(error.status) || attempt === 2) throw error; }
    }
}

async function readSourcePage(api, source, index, file, page) {
    const entry = index.files?.[file];
    if (!entry) throw new Error(index.omitted?.[file] === 'binary' ? '二进制文件保存在安装附件中，不能作为文本预览' : '该文件没有在线源码，请重新提交以启用在线预览');
    if (!Number.isSafeInteger(page) || page < 0 || page >= Math.max(1, entry.chunks.length)) throw new Error('预览位置不合法');
    if (!entry.chunks.length) return { text: '', hasNext: false, startLine: 1 };
    const chunk = entry.chunks[page];
    if (!/^[a-f0-9]{40}$/.test(chunk.sha) || chunk.size > 128 * 1024) throw new Error('源码分块索引不合法');
    const result = await api(`/repos/${source.repository}/git/blobs/${chunk.sha}`);
    const raw = decodeBlob(result, chunk.sha);
    if (raw.length !== chunk.size || hash(raw) !== chunk.sha256 || raw.includes(0)) throw new Error('源码分块内容校验失败');
    return { text: new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(raw), startLine: chunk.startLine,
        continuedLine: chunk.continuedLine, hasNext: page + 1 < entry.chunks.length };
}
module.exports = { commitSource, readSourcePage, decodeBlob, blobSha };
