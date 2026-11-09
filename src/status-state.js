'use strict';

// Share reads across views; invalidation also fences responses already in flight.
function createStatusResource(load, ttl = 1500) {
    let generation = 0, cached, expires = 0, pending;
    const read = () => {
        if (cached !== undefined && Date.now() < expires) return Promise.resolve(cached);
        if (pending) return pending;
        const version = generation;
        const request = Promise.resolve().then(load).then(value => {
            if (version !== generation) return read();
            cached = value;
            expires = Date.now() + ttl;
            return value;
        }, error => {
            if (version !== generation) return read();
            throw error;
        }).finally(() => { if (pending === request) pending = undefined; });
        pending = request;
        return request;
    };
    return {
        read,
        invalidate() { generation++; cached = undefined; expires = 0; pending = undefined; }
    };
}

// One registry per window, rather than one global listener/timer per plugin.
function createViewStatusRegistry(isActive, onError = console.warn) {
    const entries = new Set();
    const dispose = entry => { entries.delete(entry); entry.disposed = true; entry.cleanup?.(); };
    const refresh = entry => {
        if (entry.disposed) return;
        if (!entry.element.isConnected) { dispose(entry); return; }
        if (entry.pending) { if (entry.running) entry.again = true; return; }
        entry.pending = true;
        Promise.resolve().then(() => {
            entry.running = true;
            if (!entry.disposed && entry.element.isConnected) return entry.refresh();
        }).catch(onError).finally(() => {
            entry.pending = false;
            entry.running = false;
            if (entry.again && !entry.disposed) { entry.again = false; refresh(entry); }
        });
    };
    return {
        bind(element, keys, callback, cleanup) {
            const entry = {element, keys, refresh: callback, cleanup};
            entries.add(entry);
            return () => { if (!entry.disposed) dispose(entry); };
        },
        refresh(keys) {
            for (const entry of entries) {
                if (!entry.element.isConnected) { dispose(entry); continue; }
                if (keys ? entry.keys.some(key => keys.includes(key)) : isActive(entry.element)) refresh(entry);
            }
        },
        dispose(root) {
            for (const entry of entries) {
                if (!root || root === entry.element || root.contains(entry.element)) dispose(entry);
            }
        }
    };
}

module.exports = {createStatusResource, createViewStatusRegistry};
