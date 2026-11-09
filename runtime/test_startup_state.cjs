'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {EventEmitter} = require('node:events');
const source = fs.readFileSync(path.join(__dirname, '../main.js'), 'utf8');

async function main() {
    const messages = [], runtimeStates = new Map(), timers = new Map();
    const child = new EventEmitter();
    child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
    const scope = vm.createContext({
        runtimeStates, console: {log() {}, warn() {}}, path, __dirname,
        app: {isPackaged: false}, createBackendEnv: () => ({}), spawn: () => child,
        mainWindow: {isDestroyed: () => false, webContents: {send: (...args) => messages.push(args)}},
        setTimeout: (fn, delay) => { timers.set(fn, delay); return fn; },
        clearTimeout: fn => timers.delete(fn)
    });
    vm.runInContext(source.slice(source.indexOf('function handleRuntimeStateLine('), source.indexOf('let focusGuardUntil')), scope);
    vm.runInContext(source.slice(source.indexOf('function createPyProc('), source.indexOf('function postBackendLifecycle(')), scope);
    const startup = scope.createPyProc();
    const wire = state => 'PRIMIGENIUS_RUNTIME_STATE=' + JSON.stringify(state) + '\n';
    const ready = {kind: 'engine', state: {state: 'ready', engine_ready: true}};
    const text = wire(ready);
    child.stdout.emit('data', Buffer.from(text.slice(0, 20)));
    assert.equal(messages.length, 0);
    child.stdout.emit('data', Buffer.from(text.slice(20) + 'PRIMIGENIUS_API_PORT=12345\nordinary log\n'));
    assert.equal(await startup, 12345);
    assert.equal(timers.size, 0);
    assert.deepEqual(JSON.parse(JSON.stringify(runtimeStates.get('engine'))), ready);
    assert.equal(messages.filter(item => item[0] === 'runtime-state').length, 1);
    assert.equal(messages.some(item => item[0] === 'sys-log' && item[1].includes('PRIMIGENIUS_RUNTIME_STATE')), false);
    assert(messages.some(item => item[0] === 'sys-log' && item[1] === 'ordinary log\n'));
    const recovered = {kind: 'engine', state: {state: 'recovering', engine_ready: false}};
    child.stdout.emit('data', Buffer.from(wire(recovered) + wire(ready)));
    assert.equal(messages.filter(item => item[0] === 'runtime-state').length, 3);
    const before = messages.length;
    scope.mainWindow = null;
    child.stdout.emit('data', Buffer.from(wire({kind: 'r', state: {ready: true}})));
    assert.equal(messages.length, before);
    scope.mainWindow = {webContents: {send: (...args) => messages.push(args)}};
    const replay = source.indexOf('for (const update of runtimeStates.values())');
    vm.runInContext(source.slice(replay, source.indexOf('\n', replay)), scope);
    assert.equal(messages.length, before + 2);
    const notifications = [];
    const card = {textContent: ''};
    const renderer = fs.readFileSync(path.join(__dirname, '../src/renderer.js'), 'utf8');
    let listener;
    const start = renderer.indexOf("ipcRenderer.on('runtime-state'");
    vm.runInNewContext(renderer.slice(start, renderer.indexOf('const fetchRRuntimeStatus', start)), {
        ipcRenderer: {on: (_name, callback) => { listener = callback; }},
        document: {querySelectorAll: () => [card]},
        desktopRuntimeState: null, runtimeStateLabel: state => state.state,
        notifyStatusChanged: (...keys) => notifications.push(keys)
    });
    listener(null, ready);
    assert.equal(card.textContent, 'ready');
    listener(null, recovered);
    assert.equal(card.textContent, 'recovering');
    listener(null, {kind: 'r', state: {ready: true}});
    assert.deepEqual(notifications, [['runtime'], ['runtime'], ['r-runtime']]);
    scope.mainWindow.isDestroyed = () => false;
    child.emit('exit', 1, null);
    assert.equal(runtimeStates.get('engine').state.state, 'attention');
    assert(!source.includes('function ensurePodmanRunning('));
    console.log('PASS: fragmented startup messages, immediate IPC, recovery, late renderer replay, status invalidation');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
