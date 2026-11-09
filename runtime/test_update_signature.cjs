'use strict';
// Exercise the actual update handler and cryptographic verification; no network/install.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const { EventEmitter } = require('node:events');
const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'main.js'), 'utf8');
const section = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));

async function main() {
    const directory = fs.mkdtempSync(path.join(root, '.tmp-signature-test-'));
    try {
        const {publicKey, privateKey} = crypto.generateKeyPairSync('rsa', {modulusLength: 2048});
        const payload = Buffer.from('isolated package fixture');
        const signature = crypto.sign('sha256', payload, privateKey).toString('base64');
        const digest = crypto.createHash('sha256').update(payload).digest('hex');
        for (const scenario of ['valid', 'invalid', 'malformed', 'missing', 'timeout', 'no-candidates', 'legacy', 'hash-mismatch', 'multipart-invalid']) {
            let handler, installations = 0;
            const signals = [];
            const http = {request(options, callback) {
                installations++;
                assert.equal(options.path, '/install-plugin-panel');
                const req = new EventEmitter();
                req.write = () => {};
                req.destroy = () => {};
                req.end = () => {
                    const response = new EventEmitter(); callback(response);
                    response.emit('data', JSON.stringify({status: 'success'})); response.emit('end');
                };
                return req;
            }};
            const scope = {fs, path, crypto, Buffer, console: {log() {}}, backendPort: 1,
                PLUGIN_SIGN_PUBLIC_KEY: publicKey, app: {getPath: () => directory},
                mainWindow: {isDestroyed: () => false, webContents: {send: name => signals.push(name)}},
                ipcMain: {handle: (name, callback) => {handler = callback;}},
                require: name => {assert.equal(name, 'http'); return http;},
                _normalizeVersion: String, _getPluginDownloadNetworkMode: async () => 'auto',
                _normalizePluginDownloadParts: () => scenario === 'multipart-invalid' ? [{}] : [],
                _pluginDownloadCandidates: url => scenario === 'no-candidates' && url.endsWith('.sig') ? [] : [{label: 'fixture'}],
                _initialAcceleratedCandidateCount: () => 1,
                _recordPluginInstallHistory: () => ({ok: true, first_install: false}),
                _downloadMultipartPluginPackage: async (info, parts, destination) => {
                    fs.writeFileSync(destination, payload); return {candidate: {label: 'fixture'}};
                },
                _downloadCandidateByRace: async (candidates, destination, options) => {
                    if (!candidates.length) throw Error('No download candidates');
                    if (options.kind === 'signature' && ['missing', 'timeout'].includes(scenario)) throw Error(scenario);
                    fs.writeFileSync(destination, options.kind === 'signature'
                        ? scenario === 'malformed' ? '<html>not a signature</html>'
                        : ['invalid', 'multipart-invalid'].includes(scenario) ? Buffer.alloc(256).toString('base64') : signature
                        : payload);
                    return {candidate: {label: 'fixture'}, value: await options.verifyFile(destination)};
                }};
            vm.createContext(scope);
            vm.runInContext(section('function _computeFileSHA256(', 'function _probeDownloadCandidate(')
                + section("ipcMain.handle('plugin-update-download'", "ipcMain.handle('check-plugin-updates'"), scope);
            const info = {id: 'fixture', version: '1.0.0', download_url: 'fixture.zip',
                sha256: scenario === 'hash-mismatch' ? '0'.repeat(64) : digest};
            if (scenario !== 'legacy') info.signature_url = 'fixture.sig';
            const result = await handler({}, info);
            const accepted = ['valid', 'legacy'].includes(scenario);
            assert.equal(result.ok, accepted, scenario + ': ' + result.reason);
            assert.equal(installations, accepted ? 1 : 0, scenario + ': installation gate');
            assert.equal(signals.includes('plugin-update-download-complete'), accepted, scenario + ': completion event');
            assert.equal(signals.includes('plugin-update-installed'), accepted, scenario + ': installed event');
        }
        console.log('PASS: valid signatures/legacy unsigned entries install; invalid, malformed, missing, timed-out, unusable and multipart signatures block installation; hash mismatch still blocks.');
    } finally {
        const target = path.resolve(directory);
        if (path.dirname(target) !== root || !path.basename(target).startsWith('.tmp-signature-test-')) throw Error('Unsafe cleanup target');
        fs.rmSync(target, {recursive: true, force: true, maxRetries: 6, retryDelay: 200});
    }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
