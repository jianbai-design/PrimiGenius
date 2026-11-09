'use strict';

// Public JSON endpoints and Raw files need different proxy capabilities.
// z2.gh-proxy.cn serves week-old API data even with no-cache headers and cache-busting queries.
const API_PREFIXES = [
    'https://gh-proxy.com/',
    'https://v6.gh-proxy.org/',
    'https://cf.gh-proxy.cn/github/'
];
const RAW_PREFIXES = [
    'https://gh-proxy.com/',
    'https://gh-proxy.org/',
    'https://v4.gh-proxy.org/',
    'https://v6.gh-proxy.org/',
    'https://cdn.gh-proxy.org/',
    'https://ghproxy.net/',
    'https://eo.gh-proxy.cn/github/',
    'https://cf.gh-proxy.cn/github/'
];

function publicGithubCandidates(url, options = {}, mode = 'auto') {
    const method = String(options.method || 'GET').toUpperCase();
    const headers = options.headers || {};
    const privateHeaders = ['authorization', 'cookie', 'proxy-authorization'];
    const hasCredentials = Array.isArray(headers)
        ? headers.some(([name, value]) => privateHeaders.includes(String(name).toLowerCase()) && value)
        : typeof headers.get === 'function'
        ? privateHeaders.some(name => headers.get(name))
        : Object.keys(headers).some(name => privateHeaders.includes(name.toLowerCase()) && headers[name]);
    if (method !== 'GET' || options.body != null || options.data != null || options.token ||
        options.credentials === 'include' || hasCredentials) return [url];
    let target;
    try { target = new URL(url); } catch (_) { return [url]; }
    if (target.username || target.password || target.port || target.hash ||
        [...target.searchParams.keys()].some(name => /token|secret|signature|credential/i.test(name))) return [url];
    const publicFile = target.origin === 'https://avatars.githubusercontent.com' ||
        (target.origin === 'https://github.com' && /^\/[^/]+\/[^/]+\/(?:releases\/(?:download|latest\/download)\/|archive\/)/.test(target.pathname));
    const prefixes = target.origin === 'https://api.github.com' && target.pathname.startsWith('/repos/')
        ? API_PREFIXES : target.origin === 'https://raw.githubusercontent.com' ? RAW_PREFIXES
        : publicFile ? ['https://gh-proxy.com/'] : [];
    const proxies = prefixes.map(prefix => prefix + url);
    return mode === 'cn' ? [...proxies, url] : [url, ...proxies];
}

function installGithubAvatarFallback(root) {
    root.addEventListener('error', event => {
        const img = event.target;
        if (!img || img.tagName !== 'IMG') return;
        let url;
        try { url = new URL(img.src); } catch (_) { return; }
        if (url.origin !== 'https://avatars.githubusercontent.com') return;
        const urls = publicGithubCandidates(img.src);
        if (urls.length < 2) return;
        img.referrerPolicy = 'no-referrer';
        img.src = urls[1]; // A failed proxy image has another origin, so it cannot loop.
    }, true);
}

function isPublicGithubProxyUrl(url) {
    const prefix = [...new Set([...API_PREFIXES, ...RAW_PREFIXES])].find(value => url.startsWith(value));
    return !!prefix && publicGithubCandidates(url.slice(prefix.length)).includes(url);
}

async function raceGithubReads(urls, attempt, hedgeMs = 0) {
    const controllers = urls.map(() => new AbortController());
    let winnerIndex = -1;
    try {
        const winner = await Promise.any(urls.map((url, index) => new Promise((resolve, reject) => {
            const controller = controllers[index];
            let timer;
            const cancel = () => {
                clearTimeout(timer);
                reject(Object.assign(new Error('GitHub read cancelled'), { name: 'AbortError' }));
            };
            const start = () => {
                controller.signal.removeEventListener('abort', cancel);
                Promise.resolve().then(() => attempt(url, controller))
                    .then(value => resolve({ value, index }), reject);
            };
            controller.signal.addEventListener('abort', cancel, { once: true });
            timer = setTimeout(start, index === 0 ? 0 : hedgeMs);
        })));
        winnerIndex = winner.index;
        return winner.value;
    } finally {
        controllers.forEach((controller, index) => { if (index !== winnerIndex) controller.abort(); });
    }
}

module.exports = { publicGithubCandidates, isPublicGithubProxyUrl, raceGithubReads, installGithubAvatarFallback };
