'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path'), crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');
const { commitSource, readSourcePage, blobSha, decodeBlob } = require('../src/market-source');
const { register, ADMIN_ID } = require('../src/github-market-main');
const { Writable, EventEmitter } = require('node:stream');

async function checkPreviewIPC(git, repository, snapshot) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'market-online-ipc-'));
    const handlers = {}, sender = { mainFrame: {} };
    const state = { schema: 1, administrator_id: ADMIN_ID, reviewers: [], blocked: [], audit: [], plugins: {}, requests: {
        1: { kind: 'release', source_verified: true, source: { repository, ...snapshot }, files: { 'scripts/large.py': {} } }
    } };
    try {
        register({ app: { getPath: () => directory, on() {} }, ipcMain: { handle: (name, fn) => handlers[name] = fn },
            safeStorage: { isEncryptionAvailable: () => false }, getWindow: () => ({ webContents: sender }),
            download: () => { throw new Error('Online preview must never download a ZIP'); },
            net: { request(options) {
                const req = new Writable({ write(_c, _e, done) { done(); }, final(done) { done(); queueMicrotask(() => {
                    const url = new URL(options.url);
                    const value = url.pathname.includes('/contents/market-state.json') ? { content: Buffer.from(JSON.stringify(state)).toString('base64') } :
                        git.request(url.pathname + url.search);
                    assert(value, 'Unexpected online preview request');
                    const res = new EventEmitter(); res.statusCode = 200; req.emit('response', res); res.emit('data', Buffer.from(JSON.stringify(value))); res.emit('end');
                }); } }); req.setHeader = () => {}; req.abort = () => {}; return req;
            } } });
        const invoke = args => handlers['market-preview']({ sender, senderFrame: sender.mainFrame }, args);
        const before = git.calls.length;
        const result = await invoke({ number: 1, file: 'scripts/large.py', page: 0 });
        assert(result.hasNext);
        assert.equal(git.calls.slice(before).filter(call => call.route.includes('/git/blobs/')).length, 2,
            'Opening online source reads only the immutable index and one bounded fragment');
        const reads = git.calls.length;
        assert.deepEqual(await invoke({ number: 1, file: 'scripts/large.py', page: 0 }), result);
        assert.equal(git.calls.length, reads, 'Repeated visible fragments use bounded memory cache');
        assert((await invoke({ number: 1, file: 'scripts/large.py', page: 1 })).text.length);
        await assert.rejects(invoke({ number: 1, file: '../keep.txt', page: 0 }), /文件不存在/);
        await assert.rejects(invoke({ number: 1, file: 'scripts/large.py', page: -1 }), /不合法/);
    } finally { fs.rmSync(directory, { recursive: true, force: true }); }
}

function snapshotFixture(files) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'primigenius-source-'));
    try {
        for (const [name, text] of Object.entries(files)) {
            const file = path.join(directory, name); fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, text);
        }
        const result = spawnSync('python', ['-B', '-c', 'import sys,json;sys.path.insert(0,"backend");from plugin_source import source_index;print(json.dumps(source_index(sys.argv[1])))', directory],
            { cwd: path.resolve(__dirname, '..'), encoding: 'utf8', windowsHide: true, maxBuffer: 16 * 1024 * 1024 });
        assert.equal(result.status, 0, result.stderr);
        return { status: 'success', directory, index: JSON.parse(result.stdout) };
    } catch (error) { fs.rmSync(directory, { recursive: true, force: true }); throw error; }
}

function gitFixture() {
    const blobs = new Map(), trees = new Map(), commits = new Map();
    const id = object => crypto.createHash('sha1').update(JSON.stringify(object)).digest('hex');
    const storeTree = tree => { const sha = id(tree); trees.set(sha, structuredClone(tree)); return sha; };
    const keep = Buffer.from('author-owned file'); blobs.set(blobSha(keep), keep);
    const first = { tree: { sha: storeTree([{ path: 'keep.txt', mode: '100644', type: 'blob', sha: blobSha(keep) }]) } };
    let head = id(first); commits.set(head, first);
    const flatten = (sha, prefix = '') => trees.get(sha).flatMap(item => [{ ...item, path: prefix + item.path },
        ...(item.type === 'tree' ? flatten(item.sha, prefix + item.path + '/') : [])]);
    const calls = [];
    function request(route, method = 'GET', data) {
        const match = route.match(/\/git\/(.*)/); if (!match) return undefined;
        calls.push({ route, method, data }); const endpoint = match[1];
        if (endpoint.startsWith('ref/heads/')) return { object: { sha: head } };
        if (endpoint.startsWith('commits/')) return commits.get(endpoint.slice(8));
        if (endpoint.startsWith('trees/')) return { tree: flatten(endpoint.slice(6).split('?')[0]), truncated: false };
        if (endpoint.startsWith('blobs/')) {
            const sha = endpoint.slice(6), raw = blobs.get(sha); assert(raw, 'Unknown blob ' + sha);
            return { sha, encoding: 'base64', content: raw.toString('base64') };
        }
        if (endpoint === 'blobs') {
            const raw = Buffer.from(data.content, 'base64'), sha = blobSha(raw); blobs.set(sha, raw); return { sha };
        }
        if (endpoint === 'trees') {
            assert.equal(new Set(data.tree.map(item => item.path)).size, data.tree.length, 'Git trees must not contain duplicate paths');
            const merged = new Map((data.base_tree ? trees.get(data.base_tree) : []).map(item => [item.path, item]));
            for (const item of data.tree) merged.set(item.path, item);
            return { sha: storeTree([...merged.values()]) };
        }
        if (endpoint === 'commits') { const sha = id(data); commits.set(sha, { ...data, tree: { sha: data.tree } }); return { sha }; }
        if (endpoint.startsWith('refs/heads/')) {
            assert.equal(data.force, false); assert.deepEqual(commits.get(data.sha).parents, [head]); head = data.sha; return { object: { sha: head } };
        }
        throw new Error('Unexpected Git endpoint ' + endpoint);
    }
    return { request, blobs, calls, files: commit => flatten(commits.get(commit || head).tree.sha), get head() { return head; } };
}

async function run() {
    const text = '\ufeff' + ('中文😀\tsequence\r\n' + 'A'.repeat(260000) + '\n').repeat(5) + 'last-line-without-newline';
    const snapshots = [], git = gitFixture(), repository = 'author/PrimiGenius-market-uploads';
    try {
        const first = snapshotFixture({ 'config.json': '{"id":"demo"}', 'scripts/large.py': text, 'obsolete.py': 'old', 'duplicate.py': 'old', 'empty.txt': '', 'runtime/rootfs.tar.gz': Buffer.from([0, 255, 2]) }); snapshots.push(first);
        const args = { api: async (...args) => git.request(...args), repository, branch: 'main', pluginId: 'community_10_demo', version: '1.0.0' };
        const frozen = await commitSource({ ...args, ...first });
        await checkPreviewIPC(git, repository, frozen);
        assert(git.files().some(file => file.path === 'keep.txt'));
        assert(!git.files().some(file => file.path.endsWith('rootfs.tar.gz')));
        assert.equal(first.index.omitted['runtime/rootfs.tar.gz'], 'binary');
        const index = JSON.parse(decodeBlob(await args.api(`/repos/${repository}/git/blobs/${frozen.index_sha}`), frozen.index_sha));
        let complete = '', page = 0;
        do {
            const result = await readSourcePage(args.api, { repository }, index, 'scripts/large.py', page++);
            assert(Buffer.byteLength(result.text) <= 128 * 1024); complete += result.text;
            if (!result.hasNext) break;
        } while (page < 100);
        assert.equal(complete, text, 'Every UTF-8 byte survives chunk boundaries and no final newline is invented');
        assert(Buffer.byteLength(complete) > 512 * 1024);
        const second = snapshotFixture({ 'config.json': '{"id":"demo","version":"2.0.0"}', 'scripts/new.py': 'replacement' }); snapshots.push(second);
        const update = await commitSource({ ...args, ...second, version: '2.0.0' });
        assert.notEqual(update.commit, frozen.commit);
        assert(!git.files().some(file => file.path.endsWith('obsolete.py')), 'Deleted source files disappear from the current version');
        assert(git.files(frozen.commit).some(file => file.path.endsWith('obsolete.py')), 'Prior source remains available in commit history');
        assert(git.files().some(file => file.path === 'keep.txt'));
        assert.equal((await readSourcePage(args.api, { repository }, index, 'scripts/large.py', 0)).text,
            Buffer.from(text).subarray(0, index.files['scripts/large.py'].chunks[0].size).toString('utf8'));
        // The immutable index still reads the old version after the branch has advanced.
        const chunk = index.files['scripts/large.py'].chunks[1], original = git.blobs.get(chunk.sha);
        git.blobs.set(chunk.sha, Buffer.from('tampered'));
        await assert.rejects(readSourcePage(args.api, { repository }, index, 'scripts/large.py', 1), /校验/); git.blobs.set(chunk.sha, original);
        await assert.rejects(readSourcePage(args.api, { repository }, index, 'runtime/rootfs.tar.gz', 0), /二进制/);
        console.log('PASS: source commits/history/deletions, author files preserved, binary assets excluded, full >512KiB UTF-8/CRLF/long-line preview, pinned versions and tamper rejection');
    } finally { for (const snapshot of snapshots) fs.rmSync(snapshot.directory, { recursive: true, force: true }); }
}
module.exports = { snapshotFixture, gitFixture };
if (require.main === module) run().catch(error => { console.error(error); process.exitCode = 1; });
