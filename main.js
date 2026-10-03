const { app, BrowserWindow, ipcMain, screen, Menu, nativeImage, dialog, shell, powerMonitor } = require('electron');
const path = require('path');
const { spawn, execSync, execFileSync } = require('child_process');
const os = require('os');
const fs = require('fs');
const http = require('http');
const https = require('https');
const { autoUpdater } = require('electron-updater');
const crypto = require('crypto');

let mainWindow;
let pyProc = null;
let backendPort = null;
let focusGuardUntil = 0;
let focusGuardTimer = null;
let latestUpdatePayload = null;
let didEmitUpdateAvailable = false;
let manualDownloadedInstallerPath = '';
let manualDownloadedInstallerVersion = '';
let manualDownloadInProgress = false;
let resumeRecoveryTimer = null;

ipcMain.on('get-backend-port-sync', (event) => {
    event.returnValue = backendPort;
});

const GITHUB_RELEASE_DOWNLOAD_ACCELERATORS = [
    { label: 'gh-proxy.com', prefix: 'https://gh-proxy.com/' },
    { label: 'gh-proxy.org', prefix: 'https://gh-proxy.org/' },
    { label: 'v4.gh-proxy.org', prefix: 'https://v4.gh-proxy.org/' },
    { label: 'v6.gh-proxy.org', prefix: 'https://v6.gh-proxy.org/' },
    { label: 'cdn.gh-proxy.org', prefix: 'https://cdn.gh-proxy.org/' },
    { label: 'ghproxy.net', prefix: 'https://ghproxy.net/' },
    { label: 'gh-proxy.cn', prefix: 'https://gh-proxy.cn/' }
];

const GITHUB_RELEASE_LEGACY_DOWNLOAD_ACCELERATORS = [
    { label: 'mirror.ghproxy.com', prefix: 'https://mirror.ghproxy.com/' },
    { label: 'gh.llkk.cc', prefix: 'https://gh.llkk.cc/' },
    { label: 'hubp.llkk.cc', prefix: 'https://hubp.llkk.cc/' }
];

function _githubReleaseDownloadUrls(base) {
    const url = String(base || '').trim();
    if (!/^https?:\/\//i.test(url)) return [];
    return GITHUB_RELEASE_DOWNLOAD_ACCELERATORS.map(accel => `${accel.prefix}${url}`);
}

function _githubReleaseLegacyDownloadUrls(base) {
    const url = String(base || '').trim();
    if (!/^https?:\/\//i.test(url)) return [];
    return GITHUB_RELEASE_LEGACY_DOWNLOAD_ACCELERATORS.map(accel => `${accel.prefix}${url}`);
}

function _isGithubReleaseAcceleratorUrl(url) {
    const value = String(url || '');
    return [...GITHUB_RELEASE_DOWNLOAD_ACCELERATORS, ...GITHUB_RELEASE_LEGACY_DOWNLOAD_ACCELERATORS]
        .some(accel => value.startsWith(accel.prefix));
}

function _orderReleaseDownloadUrlsForMode(urls, networkMode = 'auto') {
    const unique = _uniqueUrls(urls || []);
    const regular = unique.filter(url => !_isGithubReleaseAcceleratorUrl(url));
    const accelerated = unique.filter(url => _isGithubReleaseAcceleratorUrl(url));
    return _safeNetworkModeValue(networkMode) === 'cn'
        ? [...accelerated, ...regular]
        : [...regular, ...accelerated];
}

const UPDATE_MIRROR_RELEASE_API_CANDIDATES = [
    'https://api.github.com/repos/jianbai-design/PrimiGenius/releases/latest',
    'https://mirror.ghproxy.com/https://api.github.com/repos/jianbai-design/PrimiGenius/releases/latest',
    'https://gh.llkk.cc/https://api.github.com/repos/jianbai-design/PrimiGenius/releases/latest'
];

// ---- 插件远程更新配置 ----
// 策略1: 通过 API 找最新 Release，从中下载 plugins-registry.json
const PLUGIN_REGISTRY_API_CANDIDATES = [
    'https://api.github.com/repos/jianbai-design/PrimiGenius-plugins/releases/latest',
    'https://mirror.ghproxy.com/https://api.github.com/repos/jianbai-design/PrimiGenius-plugins/releases/latest',
    'https://gh.llkk.cc/https://api.github.com/repos/jianbai-design/PrimiGenius-plugins/releases/latest'
];
const PLUGIN_REGISTRY_RELEASE_ASSET_CANDIDATES = [
    'https://github.com/jianbai-design/PrimiGenius-plugins/releases/latest/download/plugins-registry.json',
    ..._githubReleaseDownloadUrls('https://github.com/jianbai-design/PrimiGenius-plugins/releases/latest/download/plugins-registry.json'),
    'https://mirror.ghproxy.com/https://github.com/jianbai-design/PrimiGenius-plugins/releases/latest/download/plugins-registry.json',
    'https://gh.llkk.cc/https://github.com/jianbai-design/PrimiGenius-plugins/releases/latest/download/plugins-registry.json',
    'https://hubp.llkk.cc/https://github.com/jianbai-design/PrimiGenius-plugins/releases/latest/download/plugins-registry.json'
];
// 策略2: 备用，main 分支 raw URL
const PLUGIN_REGISTRY_RAW_CANDIDATES = [
    'https://raw.githubusercontent.com/jianbai-design/PrimiGenius-plugins/main/plugins-registry.json',
    'https://raw.githubusercontent.com/jianbai-design/PrimiGenius-plugins/master/plugins-registry.json',
    'https://cdn.jsdelivr.net/gh/jianbai-design/PrimiGenius-plugins@main/plugins-registry.json',
    'https://cdn.jsdelivr.net/gh/jianbai-design/PrimiGenius-plugins@master/plugins-registry.json',
    'https://fastly.jsdelivr.net/gh/jianbai-design/PrimiGenius-plugins@main/plugins-registry.json',
    'https://fastly.jsdelivr.net/gh/jianbai-design/PrimiGenius-plugins@master/plugins-registry.json',
    'https://gcore.jsdelivr.net/gh/jianbai-design/PrimiGenius-plugins@main/plugins-registry.json',
    'https://gcore.jsdelivr.net/gh/jianbai-design/PrimiGenius-plugins@master/plugins-registry.json',
    'https://mirror.ghproxy.com/https://raw.githubusercontent.com/jianbai-design/PrimiGenius-plugins/main/plugins-registry.json',
    'https://mirror.ghproxy.com/https://raw.githubusercontent.com/jianbai-design/PrimiGenius-plugins/master/plugins-registry.json',
    'https://gh.llkk.cc/https://raw.githubusercontent.com/jianbai-design/PrimiGenius-plugins/main/plugins-registry.json',
    'https://gh.llkk.cc/https://raw.githubusercontent.com/jianbai-design/PrimiGenius-plugins/master/plugins-registry.json',
    'https://hubp.llkk.cc/https://raw.githubusercontent.com/jianbai-design/PrimiGenius-plugins/main/plugins-registry.json',
    'https://hubp.llkk.cc/https://raw.githubusercontent.com/jianbai-design/PrimiGenius-plugins/master/plugins-registry.json'
];
const PLUGIN_REGISTRY_CONTENTS_CANDIDATES = [
    'https://api.github.com/repos/jianbai-design/PrimiGenius-plugins/contents/plugins-registry.json?ref=main',
    'https://mirror.ghproxy.com/https://api.github.com/repos/jianbai-design/PrimiGenius-plugins/contents/plugins-registry.json?ref=main',
    'https://gh.llkk.cc/https://api.github.com/repos/jianbai-design/PrimiGenius-plugins/contents/plugins-registry.json?ref=main'
];
const PLUGIN_REGISTRY_REPO = 'jianbai-design/PrimiGenius-plugins';
const PLUGIN_SPLIT_REGISTRY_DEFS = [
    {
        kind: 'cli',
        label: 'CLI',
        fileName: 'plugins-registry-cli.json',
        rawPaths: ['plugins-registry-cli.json', 'CLI/plugins-registry-cli.json']
    },
    {
        kind: 'r',
        label: 'R',
        fileName: 'plugins-registry-r.json',
        rawPaths: ['R/plugins-registry-r.json', 'plugins-registry-r.json']
    }
];

function _pluginRegistryPurgeUrls() {
    const urls = [];
    for (const def of PLUGIN_SPLIT_REGISTRY_DEFS) {
        const paths = Array.isArray(def.rawPaths) && def.rawPaths.length
            ? def.rawPaths
            : [def.fileName];
        for (const branch of ['main', 'master']) {
            for (const registryPath of paths) {
                urls.push(`https://purge.jsdelivr.net/gh/${PLUGIN_REGISTRY_REPO}@${branch}/${registryPath}`);
            }
        }
    }
    return _uniqueUrls(urls);
}

async function _purgePluginRegistryCdnCache() {
    const urls = _pluginRegistryPurgeUrls();
    const results = await Promise.all(urls.map(async (url) => {
        try {
            await _fetchJson(url, 10000, { noCache: true });
            return { url, ok: true };
        } catch (e) {
            return { url, ok: false, reason: e.message || String(e) };
        }
    }));
    const succeeded = results.filter(item => item.ok).length;
    const failed = results.length - succeeded;
    console.log(`[PLUGIN-UPDATE] jsDelivr registry cache purge: ${succeeded}/${results.length} succeeded`);
    if (failed) {
        console.log('[PLUGIN-UPDATE] jsDelivr purge failures:', results.filter(item => !item.ok));
    }
    return { requested: results.length, succeeded, failed };
}
// Release API（用于获取 Release 描述/notes）
const PLUGIN_RELEASE_API_CANDIDATES = [
    'https://api.github.com/repos/jianbai-design/PrimiGenius-plugins/releases/latest',
    'https://mirror.ghproxy.com/https://api.github.com/repos/jianbai-design/PrimiGenius-plugins/releases/latest',
    'https://gh.llkk.cc/https://api.github.com/repos/jianbai-design/PrimiGenius-plugins/releases/latest'
];

// 插件签名公钥（与 nodjs_crypto/keys/plugin-sign-public.pem 对应）
const PLUGIN_SIGN_PUBLIC_KEY = `-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA1WP0ZjcmL62LYHjRd40N
Jkm05UqX8hx3mZFBdBiBZluWSeytK5MUFArtUKOtYdF2NSWXS56QTpxuVXx7NbZo
2s+ubcBLEK0GlEVoiGFH/wiMmqMkoZ5lUdETbkmeV3sYdMnomzwesMu43hnfMD5X
3qzia5nhvJfH3249VZV/xzUJvP3gtGHxas4EXU9FcjtpNjhkBhXx/e9TSKK6uxtY
k0FFkkJzYC+R5jMxAKPlGpKLTaypTvvwncbNAg/h1Pu140Qm/c1g+z9F4opxfHLR
li97/2GfGJAgG6T99+v62Cik2pL0Pa7ccbLx8BIjz3JZ2vDNt9U/8stPKMJp5zFQ
SwIDAQAB
-----END PUBLIC KEY-----`;

let pluginUpdateCheckDone = false;
let pluginRegistryCache = { data: null, time: 0 };
let localPluginCache = { data: null, time: 0 };
let pluginReleaseHistoryCache = { data: null, time: 0 };
const PLUGIN_UPDATE_CACHE_TTL = 5 * 60 * 1000;
const PLUGIN_INSTALL_COUNT_CACHE_TTL = 30 * 60 * 1000;
const ANONYMOUS_INSTALLATION_ID_FILE = 'anonymous-installation-id';
const PLUGIN_INSTALL_HISTORY_FILE = 'plugin-install-history.json';

function _anonymousInstallationIdPath() {
    return path.join(app.getPath('userData'), ANONYMOUS_INSTALLATION_ID_FILE);
}

function _pluginInstallHistoryPath() {
    return path.join(app.getPath('userData'), PLUGIN_INSTALL_HISTORY_FILE);
}

function _isAnonymousInstallationId(value) {
    return /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(String(value || '').trim());
}

function _newAnonymousInstallationId() {
    if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
    const hex = crypto.randomBytes(16).toString('hex');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-a${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}

function _getAnonymousInstallationId() {
    const filePath = _anonymousInstallationIdPath();
    try {
        const existing = fs.readFileSync(filePath, 'utf8').trim();
        if (_isAnonymousInstallationId(existing)) return existing;
    } catch (e) {}

    const id = _newAnonymousInstallationId();
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    fs.writeFileSync(filePath, `${id}\n`, 'utf8');
    return id;
}

function _readPluginInstallHistory() {
    const anonymousInstallationId = _getAnonymousInstallationId();
    const filePath = _pluginInstallHistoryPath();
    let history = null;
    try {
        history = JSON.parse(fs.readFileSync(filePath, 'utf8'));
    } catch (e) {}

    if (!history || typeof history !== 'object' ||
        history.anonymous_installation_id !== anonymousInstallationId) {
        history = {
            schema_version: 1,
            anonymous_installation_id: anonymousInstallationId,
            created_at: new Date().toISOString(),
            plugins: {}
        };
    }
    if (!history.plugins || typeof history.plugins !== 'object' || Array.isArray(history.plugins)) {
        history.plugins = {};
    }
    return history;
}

function _writePluginInstallHistory(history) {
    const filePath = _pluginInstallHistoryPath();
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    fs.writeFileSync(filePath, JSON.stringify(history, null, 2), 'utf8');
}

function _recordPluginInstallHistory(pluginId, version, source = 'plugin-store') {
    const id = String(pluginId || '').trim();
    if (!id) return { ok: false, first_install: false, reason: 'Missing plugin ID' };
    try {
        const history = _readPluginInstallHistory();
        const alreadyRecorded = Object.prototype.hasOwnProperty.call(history.plugins, id);
        if (!alreadyRecorded) {
            history.plugins[id] = {
                first_installed_at: new Date().toISOString(),
                first_version: String(version || '').trim(),
                source: String(source || 'plugin-store').trim() || 'plugin-store'
            };
            history.updated_at = new Date().toISOString();
            _writePluginInstallHistory(history);
        }
        return {
            ok: true,
            first_install: !alreadyRecorded,
            anonymous_installation_id: history.anonymous_installation_id,
            recorded_plugin_count: Object.keys(history.plugins).length
        };
    } catch (e) {
        console.log('[PLUGIN-HISTORY] Failed to record install:', id, e.message || e);
        return { ok: false, first_install: false, reason: e.message || String(e) };
    }
}

function _seedPluginInstallHistory(localPlugins) {
    const entries = Object.entries(localPlugins || {}).filter(([id]) => String(id || '').trim());
    if (!entries.length) return { ok: true, added: 0 };
    try {
        const history = _readPluginInstallHistory();
        const observedAt = new Date().toISOString();
        let added = 0;
        for (const [pluginId, plugin] of entries) {
            if (Object.prototype.hasOwnProperty.call(history.plugins, pluginId)) continue;
            history.plugins[pluginId] = {
                first_installed_at: observedAt,
                first_version: String(plugin && plugin.version || '').trim(),
                source: 'existing-installation-migration'
            };
            added += 1;
        }
        if (added > 0) {
            history.updated_at = observedAt;
            _writePluginInstallHistory(history);
            console.log(`[PLUGIN-HISTORY] Migrated ${added} existing plugin install(s)`);
        }
        return { ok: true, added, recorded_plugin_count: Object.keys(history.plugins).length };
    } catch (e) {
        console.log('[PLUGIN-HISTORY] Failed to migrate existing installs:', e.message || e);
        return { ok: false, added: 0, reason: e.message || String(e) };
    }
}

function _normalizeVersion(v) {
    return String(v || '').trim().replace(/^v/i, '');
}

function _isNewerVersion(latest, current) {
    const a = _normalizeVersion(latest).split('.').map(x => parseInt(x, 10) || 0);
    const b = _normalizeVersion(current).split('.').map(x => parseInt(x, 10) || 0);
    const n = Math.max(a.length, b.length);
    for (let i = 0; i < n; i++) {
        const av = a[i] || 0;
        const bv = b[i] || 0;
        if (av > bv) return true;
        if (av < bv) return false;
    }
    return false;
}

function _fetchJson(url, timeoutMs = 6500, options = {}) {
    return new Promise((resolve, reject) => {
        let finished = false;
        let req = null;
        let deadlineTimer = null;
        const finishResolve = (value) => {
            if (finished) return;
            finished = true;
            if (deadlineTimer) clearTimeout(deadlineTimer);
            resolve(value);
        };
        const finishReject = (error) => {
            if (finished) return;
            finished = true;
            if (deadlineTimer) clearTimeout(deadlineTimer);
            reject(error);
        };
        const headers = {
            'User-Agent': 'PrimiGenius-Updater/1.0',
            'Accept': 'application/vnd.github+json'
        };
        if (options && options.noCache) {
            headers['Cache-Control'] = 'no-cache, no-store, max-age=0';
            headers.Pragma = 'no-cache';
        }
        const token = process.env.PRIMIGENIUS_GITHUB_TOKEN || process.env.GITHUB_TOKEN || '';
        if (token && /api\.github\.com/i.test(url)) {
            headers.Authorization = `Bearer ${token}`;
        }
        req = https.get(url, {
            headers
        }, (res) => {
            const status = Number(res.statusCode || 0);
            if (status >= 300 && status < 400 && res.headers.location) {
                if (finished) return;
                finished = true;
                if (deadlineTimer) clearTimeout(deadlineTimer);
                res.resume();
                _fetchJson(res.headers.location, timeoutMs, options).then(resolve).catch(reject);
                return;
            }
            if (status < 200 || status >= 300) {
                const rate = res.headers['x-ratelimit-remaining'];
                const reset = res.headers['x-ratelimit-reset'];
                const detail = (rate !== undefined || reset)
                    ? ` rate_remaining=${rate || 'unknown'} reset=${reset ? new Date(Number(reset) * 1000).toISOString() : 'unknown'}`
                    : '';
                res.resume();
                finishReject(new Error(`HTTP ${status}${detail}`));
                return;
            }
            let raw = '';
            res.on('data', chunk => { raw += chunk.toString('utf8'); });
            res.on('end', () => {
                if (finished) return;
                try {
                    finishResolve(JSON.parse(raw));
                } catch (e) {
                    finishReject(new Error('Invalid JSON'));
                }
            });
        });
        deadlineTimer = setTimeout(() => {
            if (finished) return;
            const error = new Error('timeout');
            finishReject(error);
            if (req) req.destroy(error);
        }, timeoutMs);
        req.setTimeout(timeoutMs, () => {
            if (finished) return;
            const error = new Error('timeout');
            finishReject(error);
            req.destroy(error);
        });
        req.on('error', (e) => {
            finishReject(e);
        });
    });
}

function _uniqueUrls(urls) {
    const seen = new Set();
    const out = [];
    for (const u of urls || []) {
        const s = String(u || '').trim();
        if (!s || seen.has(s)) continue;
        seen.add(s);
        out.push(s);
    }
    return out;
}

function _pluginReleaseHistoryApiCandidates(page, perPage = 100) {
    const api = `https://api.github.com/repos/${PLUGIN_REGISTRY_REPO}/releases?per_page=${perPage}&page=${page}`;
    return _uniqueUrls([
        api,
        `https://mirror.ghproxy.com/${api}`,
        `https://gh.llkk.cc/${api}`,
        `https://hubp.llkk.cc/${api}`
    ]);
}

async function _fetchPluginReleaseHistory(options = {}) {
    const forceRefresh = !!(options && options.forceRefresh);
    const now = Date.now();
    if (!forceRefresh && pluginReleaseHistoryCache.data &&
        now - pluginReleaseHistoryCache.time < PLUGIN_INSTALL_COUNT_CACHE_TTL) {
        return { releases: pluginReleaseHistoryCache.data, stale: false };
    }

    const previous = pluginReleaseHistoryCache.data;
    const perPage = 100;
    const maxPages = 10;
    try {
        const releases = [];
        for (let page = 1; page <= maxPages; page++) {
            let pageItems = null;
            const candidates = _pluginReleaseHistoryApiCandidates(page, perPage);
            try {
                const data = await _fetchJson(candidates[0], 4500, { noCache: forceRefresh });
                if (!Array.isArray(data)) throw new Error('Invalid GitHub releases response');
                pageItems = data;
            } catch (directError) {
                console.log('[PLUGIN-STATS] Direct release history failed:', candidates[0], directError.message);
                try {
                    pageItems = await _firstSuccessful(candidates.slice(1).map(async (url) => {
                        try {
                            const data = await _fetchJson(url, 9000, { noCache: forceRefresh });
                            if (!Array.isArray(data)) throw new Error('Invalid GitHub releases response');
                            return data;
                        } catch (e) {
                            console.log('[PLUGIN-STATS] Release history fallback failed:', url, e.message);
                            throw e;
                        }
                    }));
                } catch (fallbackError) {
                    throw fallbackError || directError;
                }
            }
            releases.push(...pageItems);
            if (pageItems.length < perPage) break;
            if (page === maxPages) throw new Error('GitHub release history exceeds pagination limit');
        }
        pluginReleaseHistoryCache = { data: releases, time: now };
        return { releases, stale: false };
    } catch (e) {
        if (previous) {
            console.log('[PLUGIN-STATS] Using stale release history cache:', e.message);
            return { releases: previous, stale: true, reason: e.message || String(e) };
        }
        throw e;
    }
}

function _pluginIdForRelease(release, pluginIds) {
    const tag = String(release && release.tag_name || '').trim().toLowerCase();
    const sortedIds = [...pluginIds].sort((a, b) => b.length - a.length);
    for (const id of sortedIds) {
        const normalizedId = String(id).toLowerCase();
        if (tag === normalizedId || tag.startsWith(`${normalizedId}-`)) return id;
    }

    const assetNames = ((release && release.assets) || [])
        .map(asset => String(asset && asset.name || '').toLowerCase());
    for (const id of sortedIds) {
        const normalizedId = String(id).toLowerCase();
        if (assetNames.some(name =>
            name === `${normalizedId}.zip` ||
            name.startsWith(`${normalizedId}-`) ||
            name.startsWith(`${normalizedId}.zip.part`)
        )) return id;
    }
    return '';
}

function _pluginPackageDownloadCount(release, pluginId) {
    const normalizedId = String(pluginId || '').toLowerCase();
    const packageAssets = ((release && release.assets) || []).filter(asset => {
        const name = String(asset && asset.name || '').toLowerCase();
        return name.endsWith('.zip') || /\.zip\.part\d+$/i.test(name);
    });
    if (!packageAssets.length) return 0;

    const matchingAssets = packageAssets.filter(asset => {
        const name = String(asset && asset.name || '').toLowerCase();
        return name === `${normalizedId}.zip` ||
            name.startsWith(`${normalizedId}-`) ||
            name.startsWith(`${normalizedId}.zip.part`);
    });
    const scopedAssets = matchingAssets.length ? matchingAssets : packageAssets;
    const fullArchives = scopedAssets.filter(asset => String(asset.name || '').toLowerCase().endsWith('.zip'));
    if (fullArchives.length) {
        return fullArchives.reduce((sum, asset) => sum + Math.max(0, Number(asset.download_count) || 0), 0);
    }

    const parts = scopedAssets.filter(asset => /\.zip\.part\d+$/i.test(String(asset.name || '')));
    if (!parts.length) return 0;
    return Math.min(...parts.map(asset => Math.max(0, Number(asset.download_count) || 0)));
}

function _pluginInstallCountsFromReleases(plugins, releases) {
    const pluginIds = (plugins || []).map(plugin => plugin && plugin.id).filter(Boolean);
    const counts = Object.fromEntries(pluginIds.map(id => [id, 0]));
    for (const release of releases || []) {
        const pluginId = _pluginIdForRelease(release, pluginIds);
        if (!pluginId) continue;
        counts[pluginId] += _pluginPackageDownloadCount(release, pluginId);
    }
    return counts;
}

function _decodePluginRegistryPayload(data) {
    if (data && Array.isArray(data.plugins)) return data;

    if (data && data.encoding === 'base64' && typeof data.content === 'string') {
        try {
            const text = Buffer.from(data.content.replace(/\s+/g, ''), 'base64').toString('utf8');
            const parsed = JSON.parse(text);
            if (parsed && Array.isArray(parsed.plugins)) return parsed;
        } catch (e) {
            throw new Error('Invalid registry content');
        }
    }

    if (typeof data === 'string') {
        try {
            const parsed = JSON.parse(data);
            if (parsed && Array.isArray(parsed.plugins)) return parsed;
        } catch (e) {
            throw new Error('Invalid registry text');
        }
    }

    throw new Error('Invalid registry');
}

function _pluginRegistryFreshness(registry) {
    const generatedAt = Date.parse(
        registry && (registry.generated_at || registry.generatedAt || registry.updated_at || registry.updatedAt) || ''
    );
    return {
        generatedAt: Number.isFinite(generatedAt) ? generatedAt : 0,
        pluginCount: registry && Array.isArray(registry.plugins) ? registry.plugins.length : 0
    };
}

async function _fetchFirstPluginRegistryCandidate(urls, timeoutMs, sourceLabel) {
    const candidates = _uniqueUrls(urls);
    if (!candidates.length) return null;

    try {
        return await _firstSuccessful(candidates.map(async (url) => {
            try {
                const data = await _fetchJson(url, timeoutMs, { noCache: true });
                return { registry: _decodePluginRegistryPayload(data), source: url };
            } catch (e) {
                console.log(`[PLUGIN-UPDATE] ${sourceLabel} failed:`, url, e.message);
                throw e;
            }
        }));
    } catch (e) {
        console.log(`[PLUGIN-UPDATE] ${sourceLabel} unavailable:`, e.message);
        return null;
    }
}

async function _fetchFreshestPluginRegistryCandidate(urls, timeoutMs, sourceLabel) {
    const candidates = _uniqueUrls(urls);
    if (!candidates.length) return null;

    const results = await Promise.all(candidates.map(async (url, sourceOrder) => {
        try {
            const data = await _fetchJson(url, timeoutMs, { noCache: true });
            const registry = _decodePluginRegistryPayload(data);
            return {
                registry,
                source: url,
                sourceOrder,
                ..._pluginRegistryFreshness(registry)
            };
        } catch (e) {
            console.log(`[PLUGIN-UPDATE] ${sourceLabel} failed:`, url, e.message);
            return null;
        }
    }));

    const available = results.filter(Boolean);
    if (!available.length) return null;
    available.sort((a, b) =>
        b.generatedAt - a.generatedAt ||
        b.pluginCount - a.pluginCount ||
        a.sourceOrder - b.sourceOrder
    );

    const selected = available[0];
    console.log(
        `[PLUGIN-UPDATE] ${sourceLabel} selected: ${selected.source}` +
        ` generated_at=${selected.registry.generated_at || '-'} plugins=${selected.pluginCount}`
    );
    return selected;
}

function _splitRegistryCandidateSets(def) {
    const fileName = def.fileName;
    const rawPaths = Array.isArray(def.rawPaths) && def.rawPaths.length ? def.rawPaths : [fileName];
    const releaseAssetBase = `https://github.com/${PLUGIN_REGISTRY_REPO}/releases/latest/download/${fileName}`;
    const releaseAsset = _uniqueUrls([
        releaseAssetBase,
        ..._githubReleaseDownloadUrls(releaseAssetBase),
        `https://mirror.ghproxy.com/${releaseAssetBase}`,
        `https://gh.llkk.cc/${releaseAssetBase}`,
        `https://hubp.llkk.cc/${releaseAssetBase}`
    ]);

    const raw = [];
    const contents = [];
    const rawGroups = [];
    const contentsGroups = [];
    for (const p of rawPaths) {
        const rawGroup = [
            `https://raw.githubusercontent.com/${PLUGIN_REGISTRY_REPO}/main/${p}`,
            `https://raw.githubusercontent.com/${PLUGIN_REGISTRY_REPO}/master/${p}`,
            `https://cdn.jsdelivr.net/gh/${PLUGIN_REGISTRY_REPO}@main/${p}`,
            `https://cdn.jsdelivr.net/gh/${PLUGIN_REGISTRY_REPO}@master/${p}`,
            `https://fastly.jsdelivr.net/gh/${PLUGIN_REGISTRY_REPO}@main/${p}`,
            `https://fastly.jsdelivr.net/gh/${PLUGIN_REGISTRY_REPO}@master/${p}`,
            `https://gcore.jsdelivr.net/gh/${PLUGIN_REGISTRY_REPO}@main/${p}`,
            `https://gcore.jsdelivr.net/gh/${PLUGIN_REGISTRY_REPO}@master/${p}`,
            `https://mirror.ghproxy.com/https://raw.githubusercontent.com/${PLUGIN_REGISTRY_REPO}/main/${p}`,
            `https://mirror.ghproxy.com/https://raw.githubusercontent.com/${PLUGIN_REGISTRY_REPO}/master/${p}`,
            `https://gh.llkk.cc/https://raw.githubusercontent.com/${PLUGIN_REGISTRY_REPO}/main/${p}`,
            `https://gh.llkk.cc/https://raw.githubusercontent.com/${PLUGIN_REGISTRY_REPO}/master/${p}`,
            `https://hubp.llkk.cc/https://raw.githubusercontent.com/${PLUGIN_REGISTRY_REPO}/main/${p}`,
            `https://hubp.llkk.cc/https://raw.githubusercontent.com/${PLUGIN_REGISTRY_REPO}/master/${p}`
        ];
        const contentsGroup = [
            `https://api.github.com/repos/${PLUGIN_REGISTRY_REPO}/contents/${p}?ref=main`,
            `https://mirror.ghproxy.com/https://api.github.com/repos/${PLUGIN_REGISTRY_REPO}/contents/${p}?ref=main`,
            `https://gh.llkk.cc/https://api.github.com/repos/${PLUGIN_REGISTRY_REPO}/contents/${p}?ref=main`
        ];
        raw.push(...rawGroup);
        contents.push(...contentsGroup);
        rawGroups.push(_uniqueUrls(rawGroup));
        contentsGroups.push(_uniqueUrls(contentsGroup));
    }

    return {
        releaseAsset,
        raw: _uniqueUrls(raw),
        rawGroups,
        contents: _uniqueUrls(contents),
        contentsGroups,
        api: PLUGIN_REGISTRY_API_CANDIDATES
    };
}

async function _fetchNamedPluginRegistry(def, timeoutMs = 6500) {
    const sets = _splitRegistryCandidateSets(def);
    const acceptRegistry = (data, source) => {
        const registry = _decodePluginRegistryPayload(data);
        console.log(`[PLUGIN-UPDATE] ${def.label} registry from:`, source);
        return registry;
    };

    let lastError = null;
    const trySequentially = async (urls, sourcePrefix = '') => {
        for (const url of _uniqueUrls(urls)) {
            try {
                const data = await _fetchJson(url, timeoutMs, { noCache: true });
                return acceptRegistry(data, sourcePrefix ? `${sourcePrefix}: ${url}` : url);
            } catch (e) {
                lastError = e;
                console.log(`[PLUGIN-UPDATE] ${def.label} registry source failed:`, url, e.message);
            }
        }
        return null;
    };

    const rawPaths = Array.isArray(def.rawPaths) && def.rawPaths.length
        ? def.rawPaths
        : [def.fileName];

    // Give authoritative GitHub main a short, bounded head start. Raw and Contents
    // represent the same branch, so they may race each other safely. A blocked
    // GitHub connection must not prevent users in restricted networks from
    // reaching the CDN fallbacks.
    const githubMainRaw = rawPaths.map(p =>
        `https://raw.githubusercontent.com/${PLUGIN_REGISTRY_REPO}/main/${p}`
    );
    const githubMainContents = rawPaths.map(p =>
        `https://api.github.com/repos/${PLUGIN_REGISTRY_REPO}/contents/${p}?ref=main`
    );
    const githubCandidate = await _fetchFirstPluginRegistryCandidate(
        [...githubMainRaw, ...githubMainContents],
        Math.min(timeoutMs, 3200),
        `${def.label} GitHub main`
    );
    if (githubCandidate) {
        return acceptRegistry(githubCandidate.registry, `GitHub main: ${githubCandidate.source}`);
    }

    // CDN and proxy sources are fallbacks only, and all point to main. Wait for
    // the bounded CDN group and select the freshest registry instead of allowing
    // the fastest (possibly stale) edge cache to win.
    const cdnFallbacks = [];
    for (const p of rawPaths) {
        cdnFallbacks.push(
            `https://fastly.jsdelivr.net/gh/${PLUGIN_REGISTRY_REPO}@main/${p}`,
            `https://gcore.jsdelivr.net/gh/${PLUGIN_REGISTRY_REPO}@main/${p}`,
            `https://cdn.jsdelivr.net/gh/${PLUGIN_REGISTRY_REPO}@main/${p}`,
            `https://mirror.ghproxy.com/https://raw.githubusercontent.com/${PLUGIN_REGISTRY_REPO}/main/${p}`,
            `https://gh.llkk.cc/https://raw.githubusercontent.com/${PLUGIN_REGISTRY_REPO}/main/${p}`,
            `https://hubp.llkk.cc/https://raw.githubusercontent.com/${PLUGIN_REGISTRY_REPO}/main/${p}`
        );
    }
    const cdnCandidate = await _fetchFreshestPluginRegistryCandidate(
        cdnFallbacks,
        timeoutMs,
        `${def.label} CDN fallback`
    );
    if (cdnCandidate) {
        return acceptRegistry(cdnCandidate.registry, `CDN fallback: ${cdnCandidate.source}`);
    }

    const releaseRegistry = await trySequentially(sets.releaseAsset, 'release latest asset');
    if (releaseRegistry) return releaseRegistry;

    for (const apiUrl of sets.api) {
        try {
            const releaseData = await _fetchJson(apiUrl, timeoutMs, { noCache: true });
            if (!releaseData || !Array.isArray(releaseData.assets)) continue;
            const registryAsset = releaseData.assets.find(a =>
                a && a.name === def.fileName && a.browser_download_url
            );
            if (!registryAsset) continue;
            const assetRegistry = await trySequentially(_uniqueUrls([
                registryAsset.browser_download_url,
                ..._githubReleaseDownloadUrls(registryAsset.browser_download_url),
                `https://mirror.ghproxy.com/${registryAsset.browser_download_url}`,
                `https://gh.llkk.cc/${registryAsset.browser_download_url}`,
                `https://hubp.llkk.cc/${registryAsset.browser_download_url}`
            ]), 'release API asset');
            if (assetRegistry) return assetRegistry;
        } catch (e) {
            lastError = e;
            console.log(`[PLUGIN-UPDATE] ${def.label} release API failed:`, apiUrl, e.message);
        }
    }

    throw lastError || new Error(`No ${def.fileName} registry`);
}

async function _fetchSplitPluginRegistries() {
    const loaded = (await Promise.all(PLUGIN_SPLIT_REGISTRY_DEFS.map(async (def) => {
        try {
            const registry = await _fetchNamedPluginRegistry(def, 6500);
            if (registry && Array.isArray(registry.plugins)) {
                return { def, registry };
            }
        } catch (e) {
            console.log(`[PLUGIN-UPDATE] ${def.label} split registry unavailable:`, e.message);
        }
        return null;
    }))).filter(Boolean);
    if (!loaded.length) return null;

    const plugins = [];
    for (const item of loaded) {
        for (const plugin of item.registry.plugins || []) {
            if (!plugin || !plugin.id) continue;
            plugins.push({
                ...plugin,
                registry_kind: plugin.registry_kind || item.def.kind,
                registry_label: plugin.registry_label || item.def.label
            });
        }
    }

    if (!plugins.length) return null;
    return {
        registry_version: '1.0',
        registries: loaded.map(item => ({
            kind: item.def.kind,
            label: item.def.label,
            registry_version: item.registry.registry_version || '1.0',
            count: Array.isArray(item.registry.plugins) ? item.registry.plugins.length : 0
        })),
        plugins
    };
}

function _firstSuccessful(promises) {
    return new Promise((resolve, reject) => {
        const list = Array.isArray(promises) ? promises : [];
        if (!list.length) {
            reject(new Error('No candidates'));
            return;
        }
        let pending = list.length;
        let lastError = null;
        for (const p of list) {
            Promise.resolve(p).then(resolve).catch((e) => {
                lastError = e;
                pending -= 1;
                if (pending <= 0) reject(lastError || new Error('All candidates failed'));
            });
        }
    });
}

function _pickInstallerAsset(assets) {
    if (!Array.isArray(assets)) return null;
    const list = assets.filter(a => a && typeof a.name === 'string');
    const prefer = list.find(a => /setup-.*\.exe$/i.test(a.name));
    if (prefer) return prefer;
    return list.find(a => /\.exe$/i.test(a.name) && !/\.(blockmap|yml)$/i.test(a.name)) || null;
}

function _buildInstallerCandidates(asset) {
    const base = asset && asset.browser_download_url ? String(asset.browser_download_url) : '';
    if (!/^https?:\/\//i.test(base)) return [];
    return _uniqueUrls([
        base,
        ..._githubReleaseDownloadUrls(base),
        `https://mirror.ghproxy.com/${base}`,
        `https://gh.llkk.cc/${base}`,
        `https://hubp.llkk.cc/${base}`
    ]);
}

function _installerAssetFromUpdateInfo(info) {
    const ver = _normalizeVersion(info && info.version);
    const files = Array.isArray(info && info.files) ? info.files : [];
    const exeFile = files.find(file => {
        const raw = String((file && (file.url || file.path || file.name)) || '');
        return /\.exe(?:$|\?)/i.test(raw) && !/\.(blockmap|yml)(?:$|\?)/i.test(raw);
    });
    if (exeFile) {
        const raw = String(exeFile.url || exeFile.path || exeFile.name || '').trim();
        if (/^https?:\/\//i.test(raw)) {
            return { browser_download_url: raw };
        }
        const fileName = raw.split(/[\\/]/).pop();
        if (fileName && ver) {
            return {
                browser_download_url: `https://github.com/jianbai-design/PrimiGenius/releases/download/v${ver}/${fileName}`
            };
        }
    }
    if (!ver) return null;
    return {
        browser_download_url: `https://github.com/jianbai-design/PrimiGenius/releases/download/v${ver}/PrimiGenius-Setup-${ver}.exe`
    };
}

function _updatesDir() {
    return path.join(app.getPath('userData'), 'updates');
}

function _installerStatePath() {
    return path.join(_updatesDir(), 'pending-installer.json');
}

function _readInstallerState() {
    try {
        const filePath = _installerStatePath();
        if (!fs.existsSync(filePath)) return null;
        const state = JSON.parse(fs.readFileSync(filePath, 'utf8'));
        return state && typeof state === 'object' ? state : null;
    } catch (e) {
        return null;
    }
}

function _writeInstallerState(state) {
    try {
        fs.mkdirSync(_updatesDir(), { recursive: true });
        fs.writeFileSync(_installerStatePath(), JSON.stringify(state, null, 2), 'utf8');
    } catch (e) {
        console.log('[UPDATER] failed to persist installer state:', e.message || e);
    }
}

function _cleanupInstalledUpdateArtifacts(attempt = 0) {
    const state = _readInstallerState();
    const installedVersion = _normalizeVersion(app.getVersion());
    const downloadedVersion = _normalizeVersion(state && state.version);

    // Keep the package when the recorded update is still newer than the
    // running app: the user may have chosen "Later" or restarted before
    // completing the installation.
    if (!state || !downloadedVersion || _isNewerVersion(downloadedVersion, installedVersion)) {
        return;
    }

    const updatesRoot = path.resolve(_updatesDir());
    const installerPath = typeof state.path === 'string' ? path.resolve(state.path) : '';
    const relativePath = installerPath ? path.relative(updatesRoot, installerPath) : '';
    const isSafeInstallerPath = !!installerPath
        && !relativePath.startsWith('..')
        && !path.isAbsolute(relativePath)
        && /\.(exe|msi)$/i.test(installerPath);

    try {
        if (isSafeInstallerPath && fs.existsSync(installerPath)) {
            fs.unlinkSync(installerPath);
        }
        const statePath = _installerStatePath();
        if (fs.existsSync(statePath)) {
            fs.unlinkSync(statePath);
        }
        if (manualDownloadedInstallerPath === installerPath) {
            manualDownloadedInstallerPath = '';
            manualDownloadedInstallerVersion = '';
        }
        console.log(`[UPDATER] Cleaned installed update package for v${downloadedVersion}`);
    } catch (e) {
        // The NSIS process can briefly keep its own executable open while the
        // updated app starts. Retry during this session and retain the state
        // file so a later launch can try again as well.
        const retryDelays = [5000, 15000, 60000];
        if (attempt < retryDelays.length) {
            setTimeout(() => _cleanupInstalledUpdateArtifacts(attempt + 1), retryDelays[attempt]);
        }
        console.log('[UPDATER] Update package cleanup deferred:', e.message || e);
    }
}

function _isInstallerFileUsable(filePath) {
    try {
        if (!filePath || typeof filePath !== 'string') return false;
        const resolved = path.resolve(filePath);
        const updatesRoot = path.resolve(_updatesDir());
        const rel = path.relative(updatesRoot, resolved);
        if (rel.startsWith('..') || path.isAbsolute(rel)) return false;
        if (!/\.(exe|msi)$/i.test(resolved)) return false;
        const st = fs.statSync(resolved);
        return st.isFile() && st.size > 1024 * 1024;
    } catch (e) {
        return false;
    }
}

function _rememberDownloadedInstaller(filePath, versionText, sourceUrl = '') {
    if (!_isInstallerFileUsable(filePath)) return false;
    const st = fs.statSync(filePath);
    const version = _normalizeVersion(versionText || app.getVersion());
    manualDownloadedInstallerPath = filePath;
    manualDownloadedInstallerVersion = version;
    _writeInstallerState({
        version,
        path: filePath,
        fileName: path.basename(filePath),
        size: st.size,
        mtimeMs: st.mtimeMs,
        sourceUrl,
        savedAt: new Date().toISOString()
    });
    return true;
}

function _getCachedInstaller(versionText) {
    const expectedVersion = _normalizeVersion(versionText || (latestUpdatePayload && latestUpdatePayload.version));
    const candidates = [];
    if (manualDownloadedInstallerPath) {
        candidates.push({
            path: manualDownloadedInstallerPath,
            version: manualDownloadedInstallerVersion
        });
    }
    const state = _readInstallerState();
    if (state && state.path) {
        candidates.push(state);
    }

    const updatesDir = _updatesDir();
    const fallbackName = expectedVersion ? `PrimiGenius-Setup-${expectedVersion}.exe` : '';
    if (fallbackName) {
        candidates.push({
            path: path.join(updatesDir, fallbackName),
            version: expectedVersion
        });
    }

    for (const item of candidates) {
        const itemVersion = _normalizeVersion(item && item.version);
        if (expectedVersion && itemVersion && itemVersion !== expectedVersion) continue;
        const filePath = item && item.path;
        if (_isInstallerFileUsable(filePath)) {
            manualDownloadedInstallerPath = filePath;
            manualDownloadedInstallerVersion = itemVersion || expectedVersion;
            return filePath;
        }
    }
    return '';
}

function _emitInstallerReady(installerPath, versionText) {
    if (!installerPath || !mainWindow || mainWindow.isDestroyed()) return;
    mainWindow.webContents.send('update-download-progress', {
        percent: 100,
        speed: '0.0',
        transferred: '?',
        total: '?'
    });
    mainWindow.webContents.send('update-download-complete');
    mainWindow.webContents.send('update-ready-to-install', {
        version: versionText || '?',
        currentVersion: app.getVersion(),
        localInstaller: true,
        cached: true
    });
}

async function _launchDownloadedInstaller(installerPath) {
    if (!_isInstallerFileUsable(installerPath)) {
        throw new Error('Downloaded installer is missing or invalid');
    }
    const ext = path.extname(installerPath).toLowerCase();

    // Windows installers that request elevation must be launched through
    // ShellExecute. child_process.spawn() uses CreateProcess directly, which
    // can fail with ERROR_ELEVATION_REQUIRED and make the Install Now button
    // appear to do nothing on a standard user account.
    if (process.platform === 'win32') {
        const errorMessage = await shell.openPath(installerPath);
        if (errorMessage) {
            throw new Error(errorMessage);
        }
        return;
    }

    const command = ext === '.msi' ? 'msiexec.exe' : installerPath;
    const args = ext === '.msi' ? ['/i', installerPath] : [];
    return new Promise((resolve, reject) => {
        let proc = null;
        let settled = false;
        const done = () => {
            if (settled) return;
            settled = true;
            try { if (proc) proc.unref(); } catch (e) {}
            resolve();
        };
        const fail = (err) => {
            if (settled) return;
            settled = true;
            reject(err);
        };
        try {
            proc = spawn(command, args, {
                detached: true,
                stdio: 'ignore',
                windowsHide: false
            });
            proc.once('spawn', done);
            proc.once('error', fail);
            setTimeout(done, 700);
        } catch (e) {
            fail(e);
        }
    });
}

function _downloadFileWithProgress(url, destinationPath, onProgress, timeoutMs = 15000, redirectLeft = 4) {
    return new Promise((resolve, reject) => {
        const tmpPath = `${destinationPath}.${Date.now()}-${process.pid}-${Math.random().toString(16).slice(2)}.part`;
        try { fs.mkdirSync(path.dirname(destinationPath), { recursive: true }); } catch (e) {}

        const startedAt = Date.now();
        let received = 0;
        let file = null;
        let settled = false;

        const cleanup = () => {
            try { if (file) file.close(); } catch (e) {}
            try { fs.unlinkSync(tmpPath); } catch (e) {}
        };
        const fail = (err) => {
            if (settled) return;
            settled = true;
            cleanup();
            reject(err);
        };
        const done = () => {
            if (settled) return;
            settled = true;
            resolve(destinationPath);
        };

        let parsedUrl = null;
        try {
            parsedUrl = new URL(url);
        } catch (e) {
            fail(e);
            return;
        }
        const client = parsedUrl.protocol === 'http:' ? http : https;

        const req = client.get(url, {
            headers: {
                'User-Agent': 'PrimiGenius-Updater/1.0',
                'Accept': 'application/octet-stream,*/*'
            }
        }, (res) => {
            res.on('error', fail);
            const status = Number(res.statusCode || 0);
            if (status >= 300 && status < 400 && res.headers.location) {
                res.resume();
                if (redirectLeft <= 0) {
                    fail(new Error('Too many redirects'));
                    return;
                }
                const nextUrl = new URL(res.headers.location, parsedUrl).toString();
                _downloadFileWithProgress(nextUrl, destinationPath, onProgress, timeoutMs, redirectLeft - 1)
                    .then((p) => {
                        if (settled) return;
                        settled = true;
                        resolve(p);
                    })
                    .catch(fail);
                return;
            }
            if (status < 200 || status >= 300) {
                res.resume();
                fail(new Error(`HTTP ${status}`));
                return;
            }

            file = fs.createWriteStream(tmpPath);
            file.on('error', (e) => {
                try { res.destroy(); } catch (e2) {}
                fail(e);
            });
            const total = parseInt(String(res.headers['content-length'] || '0'), 10) || 0;
            res.on('data', (chunk) => {
                if (settled) return;
                received += chunk.length;
                file.write(chunk);

                const elapsedSec = Math.max((Date.now() - startedAt) / 1000, 0.001);
                const speed = received / elapsedSec;
                const pct = total > 0 ? Math.min(100, Math.round((received * 100) / total)) : 0;
                if (typeof onProgress === 'function') {
                    onProgress({
                        percent: pct,
                        speed: (speed / 1024 / 1024).toFixed(1),
                        transferred: (received / 1024 / 1024).toFixed(1),
                        total: (total / 1024 / 1024).toFixed(1)
                    });
                }
            });

            res.on('end', () => {
                if (settled) return;
                file.end(() => {
                    if (settled) return;
                    try {
                        try { if (fs.existsSync(destinationPath)) fs.unlinkSync(destinationPath); } catch (e0) {}
                        fs.renameSync(tmpPath, destinationPath);
                        done();
                    } catch (e) {
                        fail(e);
                    }
                });
            });
        });

        req.setTimeout(timeoutMs, () => {
            req.destroy(new Error('timeout'));
        });

        req.on('error', (e) => {
            fail(e);
        });
    });
}

function _bufferLooksLikeZip(buffer) {
    return buffer && buffer.length >= 4 && buffer[0] === 0x50 && buffer[1] === 0x4b;
}

function _fileLooksLikeZip(filePath) {
    const fd = fs.openSync(filePath, 'r');
    try {
        const first = Buffer.alloc(4);
        const read = fs.readSync(fd, first, 0, 4, 0);
        return read >= 4 && _bufferLooksLikeZip(first);
    } finally {
        fs.closeSync(fd);
    }
}

function _computeFileSHA256(filePath) {
    return new Promise((resolve, reject) => {
        const hash = crypto.createHash('sha256');
        const input = fs.createReadStream(filePath);
        input.on('data', chunk => hash.update(chunk));
        input.on('error', reject);
        input.on('end', () => {
            try {
                resolve(hash.digest('hex'));
            } catch (e) {
                reject(e);
            }
        });
    });
}

function _verifyPluginSignatureFile(zipPath, signatureBase64) {
    return new Promise((resolve) => {
        try {
            const verify = crypto.createVerify('SHA256');
            const input = fs.createReadStream(zipPath);
            input.on('data', chunk => verify.update(chunk));
            input.on('error', (e) => {
                console.log('[PLUGIN-UPDATE] signature verification error:', e.message);
                resolve(false);
            });
            input.on('end', () => {
                try {
                    verify.end();
                    resolve(verify.verify(PLUGIN_SIGN_PUBLIC_KEY, signatureBase64, 'base64'));
                } catch (e) {
                    console.log('[PLUGIN-UPDATE] signature verification error:', e.message);
                    resolve(false);
                }
            });
        } catch (e) {
            console.log('[PLUGIN-UPDATE] signature verification error:', e.message);
            resolve(false);
        }
    });
}

function _probeDownloadCandidate(candidate, options = {}, redirectLeft = 4) {
    return new Promise((resolve, reject) => {
        const url = candidate && candidate.url;
        const expectZip = !!options.expectZip;
        const timeoutMs = Number(options.timeoutMs || 4500);
        const startedAt = Date.now();
        let settled = false;
        let req = null;

        const finish = (fn, value) => {
            if (settled) return;
            settled = true;
            try { if (req) req.destroy(); } catch (e) {}
            fn(value);
        };
        const fail = (err) => finish(reject, err);
        const done = (meta) => finish(resolve, {
            candidate,
            latencyMs: Date.now() - startedAt,
            ...(meta || {})
        });

        let parsedUrl = null;
        try {
            parsedUrl = new URL(url);
        } catch (e) {
            fail(e);
            return;
        }
        const client = parsedUrl.protocol === 'http:' ? http : https;
        req = client.request(url, {
            method: 'GET',
            headers: {
                'User-Agent': 'PrimiGenius-Updater/1.0',
                'Accept': 'application/octet-stream,*/*',
                'Range': 'bytes=0-3'
            }
        }, (res) => {
            res.on('error', fail);
            const status = Number(res.statusCode || 0);
            if (status >= 300 && status < 400 && res.headers.location) {
                res.resume();
                if (redirectLeft <= 0) {
                    fail(new Error('Too many redirects'));
                    return;
                }
                const nextUrl = new URL(res.headers.location, parsedUrl).toString();
                _probeDownloadCandidate({ ...candidate, url: nextUrl }, options, redirectLeft - 1)
                    .then(done)
                    .catch(fail);
                return;
            }
            if (status < 200 || status >= 300) {
                res.resume();
                fail(new Error(`HTTP ${status}`));
                return;
            }

            let first = Buffer.alloc(0);
            const contentLength = parseInt(String(res.headers['content-length'] || '0'), 10) || 0;
            res.on('data', (chunk) => {
                if (settled) return;
                first = Buffer.concat([first, chunk]).slice(0, 8);
                if (expectZip && first.length >= 2 && !_bufferLooksLikeZip(first)) {
                    fail(new Error('Downloaded file is not a ZIP package'));
                    return;
                }
                if (!expectZip || first.length >= 4) {
                    done({ status, contentLength });
                }
            });
            res.on('end', () => {
                if (settled) return;
                if (expectZip && !_bufferLooksLikeZip(first)) {
                    fail(new Error('Downloaded file is not a ZIP package'));
                    return;
                }
                done({ status, contentLength });
            });
        });
        req.setTimeout(timeoutMs, () => {
            fail(new Error('timeout'));
        });
        req.on('error', fail);
        req.end();
    });
}

async function _downloadCandidateByRace(candidates, destinationPath, options = {}) {
    const uniqueCandidates = [];
    const seen = new Set();
    for (const candidate of candidates || []) {
        if (!candidate || !candidate.url || seen.has(candidate.url)) continue;
        seen.add(candidate.url);
        uniqueCandidates.push(candidate);
    }
    if (!uniqueCandidates.length) {
        throw new Error('No download candidates');
    }

    const kind = options.kind || 'download';
    const probeTimeoutMs = Number(options.probeTimeoutMs || 4500);
    const downloadTimeoutMs = Number(options.downloadTimeoutMs || 15000);
    const expectZip = !!options.expectZip;
    const onProgress = typeof options.onProgress === 'function' ? options.onProgress : null;
    const verifyBuffer = typeof options.verifyBuffer === 'function' ? options.verifyBuffer : null;
    const verifyFile = typeof options.verifyFile === 'function' ? options.verifyFile : null;
    const log = typeof options.log === 'function' ? options.log : (() => {});
    const orderedFirstCount = Math.max(0, Math.min(uniqueCandidates.length, Number(options.orderedFirstCount || 0)));
    const attemptDownload = async (candidate, prefix) => {
        log(`[PLUGIN-UPDATE] ${prefix || 'Trying'} ${candidate.label}: ${candidate.url}\n`);
        await _downloadFileWithProgress(candidate.url, destinationPath, onProgress, downloadTimeoutMs);
        if (expectZip && !_fileLooksLikeZip(destinationPath)) {
            try { fs.unlinkSync(destinationPath); } catch (e) {}
            throw new Error('Downloaded file is not a ZIP package');
        }
        let value = null;
        if (verifyFile) {
            value = await verifyFile(destinationPath, candidate);
        } else if (verifyBuffer) {
            value = await verifyBuffer(fs.readFileSync(destinationPath), candidate);
        }
        return { candidate, path: destinationPath, value };
    };

    let lastErr = null;
    let workingCandidates = uniqueCandidates;
    if (orderedFirstCount > 0) {
        const orderedFirst = uniqueCandidates.slice(0, orderedFirstCount);
        workingCandidates = uniqueCandidates.slice(orderedFirstCount);
        for (const candidate of orderedFirst) {
            try {
                const result = await attemptDownload(candidate, `Trying primary ${kind} source`);
                log(`[PLUGIN-UPDATE] ${candidate.label} ${kind} OK\n`);
                return result;
            } catch (e) {
                lastErr = new Error(`${candidate.label}: ${e.message || e}`);
                log(`[PLUGIN-UPDATE] ${candidate.label} failed: ${e.message || e}\n`);
            }
        }
    }

    const raceFirstCountRaw = options.raceFirstCount;
    const raceFirstCount = raceFirstCountRaw === undefined
        ? workingCandidates.length
        : Math.max(0, Math.min(workingCandidates.length, Number(raceFirstCountRaw || 0)));
    const raceCandidates = workingCandidates.slice(0, raceFirstCount);
    const fallbackCandidates = workingCandidates.slice(raceFirstCount);

    log(`[PLUGIN-UPDATE] Probing ${raceCandidates.length} ${kind} sources in parallel\n`);
    const probes = raceCandidates.map(candidate => {
        let wrapped;
        wrapped = _probeDownloadCandidate(candidate, { expectZip, timeoutMs: probeTimeoutMs })
            .then(meta => ({ ok: true, candidate, meta, wrapped }))
            .catch(error => ({ ok: false, candidate, error, wrapped }));
        return wrapped;
    });
    const pending = new Set(probes);
    let selectedAny = false;

    while (pending.size > 0) {
        const settled = await Promise.race(Array.from(pending));
        pending.delete(settled.wrapped);
        if (!settled.ok) {
            lastErr = new Error(`${settled.candidate.label}: ${settled.error && settled.error.message ? settled.error.message : settled.error}`);
            continue;
        }
        selectedAny = true;
        const latency = settled.meta && Number.isFinite(settled.meta.latencyMs) ? `${settled.meta.latencyMs} ms` : 'ready';
        try {
            const result = await attemptDownload(settled.candidate, `Fastest ${kind} source (${latency})`);
            log(`[PLUGIN-UPDATE] ${settled.candidate.label} ${kind} OK\n`);
            return result;
        } catch (e) {
            lastErr = new Error(`${settled.candidate.label}: ${e.message || e}`);
            log(`[PLUGIN-UPDATE] ${settled.candidate.label} failed: ${e.message || e}\n`);
        }
    }

    if (!selectedAny) {
        log(`[PLUGIN-UPDATE] Parallel ${kind} probe found no usable source; falling back to ordered download\n`);
    }
    for (const candidate of fallbackCandidates) {
        try {
            const result = await attemptDownload(candidate, 'Trying fallback');
            log(`[PLUGIN-UPDATE] ${candidate.label} ${kind} OK\n`);
            return result;
        } catch (e) {
            lastErr = new Error(`${candidate.label}: ${e.message || e}`);
            log(`[PLUGIN-UPDATE] ${candidate.label} failed: ${e.message || e}\n`);
        }
    }

    throw new Error(lastErr ? lastErr.message : `${kind} download failed from all sources`);
}

function _normalizePluginDownloadParts(updateInfo) {
    const rawParts = updateInfo && (updateInfo.download_parts || updateInfo.downloadParts || updateInfo.parts);
    if (!Array.isArray(rawParts)) return [];
    return rawParts
        .map((part, index) => {
            if (typeof part === 'string') {
                return { index: index + 1, url: part, name: path.basename(part) };
            }
            return {
                index: Number(part.index || index + 1),
                url: part.url || part.download_url || part.downloadUrl,
                name: part.name || (part.url ? path.basename(part.url) : `part${String(index + 1).padStart(3, '0')}`),
                size: part.size,
                sha256: part.sha256
            };
        })
        .filter(part => part.url)
        .sort((a, b) => a.index - b.index);
}

function _appendFileToWriteStream(filePath, output) {
    return new Promise((resolve, reject) => {
        const input = fs.createReadStream(filePath);
        input.on('error', reject);
        output.on('error', reject);
        input.on('end', resolve);
        input.pipe(output, { end: false });
    });
}

async function _combinePluginParts(partPaths, destinationPath) {
    try { fs.unlinkSync(destinationPath); } catch (e) {}
    const output = fs.createWriteStream(destinationPath);
    try {
        for (const partPath of partPaths) {
            await _appendFileToWriteStream(partPath, output);
        }
    } finally {
        await new Promise(resolve => output.end(resolve));
    }
}

async function _downloadMultipartPluginPackage(updateInfo, parts, zipPath, networkMode, log, onProgress) {
    const partPaths = [];
    try {
        for (let i = 0; i < parts.length; i += 1) {
            const part = parts[i];
            const partPath = `${zipPath}.part${String(i + 1).padStart(3, '0')}`;
            const partCandidates = _pluginDownloadCandidates(part.url, networkMode);
            log(`[PLUGIN-UPDATE] Downloading part ${i + 1}/${parts.length}: ${part.name || path.basename(part.url)}\n`);
            const partResult = await _downloadCandidateByRace(partCandidates, partPath, {
                kind: `part ${i + 1}/${parts.length}`,
                expectZip: i === 0,
                probeTimeoutMs: networkMode === 'cn' ? 4200 : 6000,
                downloadTimeoutMs: 120000,
                orderedFirstCount: networkMode === 'cn' ? 0 : 1,
                raceFirstCount: networkMode === 'cn' ? _initialAcceleratedCandidateCount(partCandidates) : undefined,
                log,
                onProgress: (p) => {
                    if (typeof onProgress === 'function') {
                        const partPercent = Number(p.percent || 0);
                        const percent = Math.min(99, Math.round(((i + (partPercent / 100)) / parts.length) * 100));
                        onProgress({ ...p, percent });
                    }
                },
                verifyFile: async (filePath) => {
                    if (part.sha256) {
                        const actualHash = await _computeFileSHA256(filePath);
                        if (actualHash !== part.sha256) {
                            try { fs.unlinkSync(filePath); } catch (e) {}
                            throw new Error(`Part SHA256 mismatch: expected ${part.sha256}, got ${actualHash}`);
                        }
                        return actualHash;
                    }
                    return null;
                }
            });
            partPaths.push(partPath);
            const label = partResult.candidate ? partResult.candidate.label : '';
            log(`[PLUGIN-UPDATE] Part ${i + 1}/${parts.length} OK${label ? ` (${label})` : ''}\n`);
        }

        log(`[PLUGIN-UPDATE] Combining ${parts.length} downloaded parts\n`);
        await _combinePluginParts(partPaths, zipPath);
        if (!_fileLooksLikeZip(zipPath)) {
            try { fs.unlinkSync(zipPath); } catch (e) {}
            throw new Error('Combined file is not a ZIP package');
        }
        if (updateInfo.sha256) {
            const actualHash = await _computeFileSHA256(zipPath);
            if (actualHash !== updateInfo.sha256) {
                try { fs.unlinkSync(zipPath); } catch (e) {}
                throw new Error(`SHA256 mismatch: expected ${updateInfo.sha256}, got ${actualHash}`);
            }
        }
        for (const partPath of partPaths) {
            try { fs.unlinkSync(partPath); } catch (e) {}
        }
        return { candidate: { label: 'multipart' }, path: zipPath };
    } catch (e) {
        for (const partPath of partPaths) {
            try { fs.unlinkSync(partPath); } catch (e2) {}
        }
        throw e;
    }
}

function _candidateUrl(label, url) {
    return { label, url };
}

const PLUGIN_DOWNLOAD_ACCELERATORS = GITHUB_RELEASE_DOWNLOAD_ACCELERATORS;

const PLUGIN_DOWNLOAD_LEGACY_ACCELERATORS = GITHUB_RELEASE_LEGACY_DOWNLOAD_ACCELERATORS;

function _safeNetworkModeValue(mode) {
    const m = String(mode || '').trim().toLowerCase();
    return ['auto', 'cn', 'global'].includes(m) ? m : 'auto';
}

function _readNetworkModeFromFile() {
    const candidates = [
        path.join(__dirname, '.primigenius_network_profile.json'),
        path.join(app.getPath('userData'), '.primigenius_network_profile.json')
    ];
    for (const filePath of candidates) {
        try {
            if (!fs.existsSync(filePath)) continue;
            const data = JSON.parse(fs.readFileSync(filePath, 'utf8'));
            return _safeNetworkModeValue(data && data.mode);
        } catch (e) {}
    }
    return '';
}

function _fetchNetworkModeFromBackend(timeoutMs = 900) {
    return new Promise((resolve) => {
        const req = http.request({
            hostname: '127.0.0.1',
            port: backendPort,
            path: '/podman/network-profile',
            method: 'GET',
            timeout: timeoutMs
        }, (res) => {
            let data = '';
            res.on('data', chunk => data += chunk);
            res.on('end', () => {
                try {
                    const parsed = JSON.parse(data);
                    resolve(_safeNetworkModeValue(parsed && parsed.mode));
                } catch (e) {
                    resolve('auto');
                }
            });
        });
        req.on('error', () => resolve('auto'));
        req.on('timeout', () => { req.destroy(); resolve('auto'); });
        req.end();
    });
}

async function _getPluginDownloadNetworkMode() {
    return _readNetworkModeFromFile() || await _fetchNetworkModeFromBackend();
}

function _pluginDownloadUrlVariants(url) {
    const base = String(url || '').trim();
    if (!base) return [];
    const variants = [];

    try {
        const parsed = new URL(base);
        const parts = parsed.pathname.split('/');
        const downloadIndex = parts.findIndex(part => part === 'download');
        const tagIndex = downloadIndex >= 0 ? downloadIndex + 1 : -1;
        if (tagIndex > 0 && parts[tagIndex]) {
            const tag = decodeURIComponent(parts[tagIndex]);
            const tagWithoutVersionV = tag.replace(/-v(?=\d+(?:\.\d+)+)/i, '-');
            if (tagWithoutVersionV !== tag) {
                parts[tagIndex] = encodeURIComponent(tagWithoutVersionV);
                parsed.pathname = parts.join('/');
                variants.push(parsed.toString());
            }
        }
    } catch (e) {}

    variants.push(base);
    return _uniqueUrls(variants);
}

function _pluginDownloadCandidates(url, networkMode = 'auto') {
    const variants = _pluginDownloadUrlVariants(url);
    const mode = _safeNetworkModeValue(networkMode);
    const candidates = [];
    const pushDirect = () => variants.forEach((base, index) => {
        const suffix = index === 0 ? '' : ` alt${index}`;
        candidates.push(_candidateUrl(`GitHub${suffix}`, base));
    });
    const pushAccelerators = (list) => variants.forEach((base, index) => {
        const suffix = index === 0 ? '' : ` alt${index}`;
        for (const accel of list) {
            candidates.push(_candidateUrl(`${accel.label}${suffix}`, `${accel.prefix}${base}`));
        }
    });

    if (mode === 'cn') {
        pushAccelerators(PLUGIN_DOWNLOAD_ACCELERATORS);
        pushAccelerators(PLUGIN_DOWNLOAD_LEGACY_ACCELERATORS);
        pushDirect();
    } else if (mode === 'global') {
        pushDirect();
        candidates.push(
            ...variants.flatMap((base, index) => {
                const suffix = index === 0 ? '' : ` alt${index}`;
                return PLUGIN_DOWNLOAD_ACCELERATORS.map(accel =>
                    _candidateUrl(`${accel.label}${suffix}`, `${accel.prefix}${base}`)
                );
            })
        );
    } else {
        pushDirect();
        pushAccelerators(PLUGIN_DOWNLOAD_ACCELERATORS);
        pushAccelerators(PLUGIN_DOWNLOAD_LEGACY_ACCELERATORS);
    }

    const seen = new Set();
    return candidates.filter(c => {
        if (!c.url || seen.has(c.url)) return false;
        seen.add(c.url);
        return true;
    });
}

function _initialAcceleratedCandidateCount(candidates) {
    let count = 0;
    for (const candidate of candidates || []) {
        const label = String(candidate && candidate.label || '');
        const url = String(candidate && candidate.url || '');
        if (label.startsWith('GitHub') || !_isGithubReleaseAcceleratorUrl(url)) break;
        count += 1;
    }
    return count;
}

async function _downloadInstallerInApp(candidates, versionText) {
    const urls = _uniqueUrls(candidates || []);
    if (!urls.length) {
        throw new Error('No installer download URL');
    }
    if (manualDownloadInProgress) {
        throw new Error('Download already in progress');
    }

    const cachedInstaller = _getCachedInstaller(versionText);
    if (cachedInstaller) {
        _emitInstallerReady(cachedInstaller, versionText);
        return cachedInstaller;
    }

    manualDownloadInProgress = true;
    if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('update-download-start');
    }

    const updatesDir = _updatesDir();
    const fallbackName = `PrimiGenius-Setup-${_normalizeVersion(versionText || app.getVersion()) || 'latest'}.exe`;

    try {
        let lastErr = null;
        for (const url of urls) {
            try {
                const filename = decodeURIComponent((new URL(url)).pathname.split('/').pop() || fallbackName);
                const target = path.join(updatesDir, filename || fallbackName);
                if (_rememberDownloadedInstaller(target, versionText, url)) {
                    _emitInstallerReady(target, versionText);
                    return target;
                }
                if (mainWindow && !mainWindow.isDestroyed()) {
                    mainWindow.webContents.send('sys-log', `[UPDATE] 应用内下载更新: ${url}\n`);
                }
                await _downloadFileWithProgress(url, target, (p) => {
                    if (mainWindow && !mainWindow.isDestroyed()) {
                        mainWindow.webContents.send('update-download-progress', p);
                    }
                });

                _rememberDownloadedInstaller(target, versionText, url);
                if (mainWindow && !mainWindow.isDestroyed()) {
                    mainWindow.webContents.send('update-download-complete');
                    mainWindow.webContents.send('update-ready-to-install', {
                        version: versionText || '?',
                        currentVersion: app.getVersion(),
                        localInstaller: true
                    });
                }
                return target;
            } catch (e) {
                lastErr = e;
                if (mainWindow && !mainWindow.isDestroyed()) {
                    mainWindow.webContents.send('sys-log', `[UPDATE] 下载源失败，切换下一个: ${e.message || e}\n`);
                }
            }
        }
        throw lastErr || new Error('All download sources failed');
    } finally {
        manualDownloadInProgress = false;
    }
}

function _installerDownloadLabel(url) {
    try {
        const parsed = new URL(url);
        return parsed.hostname || 'source';
    } catch (e) {
        return 'source';
    }
}

async function _downloadInstallerInAppByMode(candidates, versionText, networkMode = 'auto') {
    const urls = _orderReleaseDownloadUrlsForMode(candidates || [], networkMode);
    if (!urls.length) {
        throw new Error('No installer download URL');
    }
    if (manualDownloadInProgress) {
        throw new Error('Download already in progress');
    }

    const cachedInstaller = _getCachedInstaller(versionText);
    if (cachedInstaller) {
        _emitInstallerReady(cachedInstaller, versionText);
        return cachedInstaller;
    }

    manualDownloadInProgress = true;
    if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('update-download-start');
    }

    const updatesDir = _updatesDir();
    const fallbackName = `PrimiGenius-Setup-${_normalizeVersion(versionText || app.getVersion()) || 'latest'}.exe`;

    try {
        let lastErr = null;
        const attempted = new Set();

        const attemptUrl = async (url, reason) => {
            attempted.add(url);
            try {
                const filename = decodeURIComponent((new URL(url)).pathname.split('/').pop() || fallbackName);
                const target = path.join(updatesDir, filename || fallbackName);
                if (_rememberDownloadedInstaller(target, versionText, url)) {
                    _emitInstallerReady(target, versionText);
                    return target;
                }
                if (mainWindow && !mainWindow.isDestroyed()) {
                    mainWindow.webContents.send('sys-log', `[UPDATE] ${reason || 'Trying'}: ${url}\n`);
                }
                await _downloadFileWithProgress(url, target, (p) => {
                    if (mainWindow && !mainWindow.isDestroyed()) {
                        mainWindow.webContents.send('update-download-progress', p);
                    }
                });

                _rememberDownloadedInstaller(target, versionText, url);
                if (mainWindow && !mainWindow.isDestroyed()) {
                    mainWindow.webContents.send('update-download-complete');
                    mainWindow.webContents.send('update-ready-to-install', {
                        version: versionText || '?',
                        currentVersion: app.getVersion(),
                        localInstaller: true
                    });
                }
                return target;
            } catch (e) {
                lastErr = e;
                if (mainWindow && !mainWindow.isDestroyed()) {
                    mainWindow.webContents.send('sys-log', `[UPDATE] Download source failed, trying next: ${e.message || e}\n`);
                }
            }
            return null;
        };

        const trySequential = async (list) => {
            for (const url of list) {
                const result = await attemptUrl(url, 'Trying primary update source');
                if (result) return result;
            }
            return null;
        };

        const tryParallelAccelerators = async (list) => {
            const accelUrls = _uniqueUrls(list || []).filter(url => !attempted.has(url));
            if (!accelUrls.length) return null;
            if (mainWindow && !mainWindow.isDestroyed()) {
                mainWindow.webContents.send('sys-log', `[UPDATE] Probing ${accelUrls.length} accelerated sources in parallel\n`);
            }
            const probes = accelUrls.map(url => {
                const candidate = { label: _installerDownloadLabel(url), url };
                let wrapped;
                wrapped = _probeDownloadCandidate(candidate, { expectZip: false, timeoutMs: 4500 })
                    .then(meta => ({ ok: true, url, meta, wrapped }))
                    .catch(error => ({ ok: false, url, error, wrapped }));
                return wrapped;
            });
            const pending = new Set(probes);
            while (pending.size > 0) {
                const settled = await Promise.race(Array.from(pending));
                pending.delete(settled.wrapped);
                if (!settled.ok) {
                    lastErr = settled.error || lastErr;
                    continue;
                }
                const latency = settled.meta && Number.isFinite(settled.meta.latencyMs) ? `${settled.meta.latencyMs} ms` : 'ready';
                const result = await attemptUrl(settled.url, `Fastest accelerated source (${latency})`);
                if (result) return result;
            }
            for (const url of accelUrls) {
                if (attempted.has(url)) continue;
                const result = await attemptUrl(url, 'Trying accelerated fallback');
                if (result) return result;
            }
            return null;
        };

        const regularUrls = urls.filter(url => !_isGithubReleaseAcceleratorUrl(url));
        const acceleratedUrls = urls.filter(url => _isGithubReleaseAcceleratorUrl(url));
        const mode = _safeNetworkModeValue(networkMode);
        if (mode === 'cn') {
            const accelerated = await tryParallelAccelerators(acceleratedUrls);
            if (accelerated) return accelerated;
            const regular = await trySequential(regularUrls);
            if (regular) return regular;
        } else {
            const regular = await trySequential(regularUrls);
            if (regular) return regular;
            const accelerated = await tryParallelAccelerators(acceleratedUrls);
            if (accelerated) return accelerated;
        }

        throw lastErr || new Error('All download sources failed');
    } finally {
        manualDownloadInProgress = false;
    }
}

async function _checkLatestReleaseViaMirrors() {
    for (const apiUrl of UPDATE_MIRROR_RELEASE_API_CANDIDATES) {
        try {
            const data = await _fetchJson(apiUrl, 7000);
            if (data && (data.tag_name || data.name)) {
                return { data, apiUrl };
            }
        } catch (e) {
            console.log('[UPDATER] mirror check failed:', apiUrl, e.message);
        }
    }
    return null;
}

async function _emitMirrorFallbackUpdateIfNeeded(reason = 'fallback') {
    if (didEmitUpdateAvailable) return;
    const res = await _checkLatestReleaseViaMirrors();
    if (!res || !res.data) return;

    const rel = res.data;
    const tagRaw = String(rel.tag_name || '').trim();
    const latestVer = _normalizeVersion(rel.tag_name || rel.name || '');
    const currentVer = _normalizeVersion(app.getVersion());
    if (!latestVer || !_isNewerVersion(latestVer, currentVer)) return;

    if (latestUpdatePayload && _normalizeVersion(latestUpdatePayload.version) === latestVer) {
        didEmitUpdateAvailable = true;
        return;
    }

    const exeAsset = _pickInstallerAsset(rel.assets || []);
    const synthesizedAsset = {
        browser_download_url: `https://github.com/jianbai-design/PrimiGenius/releases/download/${tagRaw || ('v' + latestVer)}/PrimiGenius-Setup-${latestVer}.exe`
    };
    const downloadCandidates = _buildInstallerCandidates(exeAsset || synthesizedAsset);

    const payload = {
        version: latestVer,
        currentVersion: app.getVersion(),
        releaseNotes: String(rel.body || '').substring(0, 4000),
        releaseName: rel.name || '',
        releaseDate: rel.published_at || rel.created_at || '',
        manualOnly: true,
        downloadCandidates,
        source: 'mirror-fallback'
    };

    latestUpdatePayload = payload;
    didEmitUpdateAvailable = true;

    if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('sys-log', `[UPDATE] Find new version v${latestVer}\n`);
        mainWindow.webContents.send('update-available', payload);
        const cachedInstaller = _getCachedInstaller(latestVer);
        if (cachedInstaller) {
            mainWindow.webContents.send('sys-log', `[UPDATE] Reusing downloaded installer: ${cachedInstaller}\n`);
            setTimeout(() => _emitInstallerReady(cachedInstaller, latestVer), 250);
        }
    }
}

function extractReleaseNotes(info) {
    const raw = info && info.releaseNotes;
    if (!raw) return '';
    if (typeof raw === 'string') return raw;
    if (Array.isArray(raw)) {
        return raw
            .map(item => {
                if (!item) return '';
                if (typeof item === 'string') return item;
                return item.note || item.content || '';
            })
            .filter(Boolean)
            .join('\n\n');
    }
    if (typeof raw === 'object') {
        return raw.note || raw.content || String(raw);
    }
    return String(raw);
}

function focusMainWindowNow() {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    try { if (!mainWindow.isVisible()) mainWindow.show(); } catch (e) {}
    try { if (mainWindow.isMinimized()) mainWindow.restore(); } catch (e) {}
    try { mainWindow.focus(); } catch (e) {}
    try { mainWindow.webContents.focus(); } catch (e) {}
}

function extendFocusGuard(durationMs = 1200) {
    focusGuardUntil = Math.max(focusGuardUntil, Date.now() + durationMs);
    if (focusGuardTimer) return;
    focusGuardTimer = setInterval(() => {
        if (!mainWindow || mainWindow.isDestroyed()) {
            clearInterval(focusGuardTimer);
            focusGuardTimer = null;
            focusGuardUntil = 0;
            return;
        }
        if (Date.now() >= focusGuardUntil) {
            clearInterval(focusGuardTimer);
            focusGuardTimer = null;
            return;
        }
        focusMainWindowNow();
    }, 250);
}

// 读取系统资源和WSL配置
function getSystemInfo() {
    const cpuTotal = os.cpus().length;
    let cpuVal = cpuTotal;
    let memVal = `${Math.round(os.totalmem() / 1024 / 1024 / 1024)} GB`;
    
    try {
        const wslConfigPath = path.join(os.homedir(), '.wslconfig');
        if (fs.existsSync(wslConfigPath)) {
            const txt = fs.readFileSync(wslConfigPath, 'utf-8');
            const cpuMatch = txt.match(/processors\s*=\s*(\d+)/i);
            // 捕获数字和可选单位（B/KB/MB/GB/G/M）
            const memMatch = txt.match(/memory\s*=\s*([\d.]+)\s*(b|kb|k|mb|m|gb|g)?/i);
            console.log('[DEBUG] .wslconfig found, cpuMatch:', cpuMatch, 'memMatch:', memMatch);

            if (cpuMatch) cpuVal = parseInt(cpuMatch[1], 10);
            if (memMatch) {
                const raw = parseFloat(memMatch[1]);
                const unit = memMatch[2] ? memMatch[2].toLowerCase() : null;
                let memGB = 0;
                if (unit === 'gb' || unit === 'g') {
                    memGB = raw;
                } else if (unit === 'mb' || unit === 'm') {
                    memGB = raw / 1024;
                } else if (unit === 'kb' || unit === 'k') {
                    memGB = raw / 1024 / 1024;
                } else if (unit === 'b') {
                    memGB = raw / 1024 / 1024 / 1024;
                } else {
                    // 未注明单位：根据数量级推断
                    if (raw > 1024 * 1024 * 1024) {
                        // 看起来像 bytes
                        memGB = raw / 1024 / 1024 / 1024;
                    } else if (raw > 1024) {
                        // 看起来像 MB
                        memGB = raw / 1024;
                    } else {
                        // 否则当作 GB
                        memGB = raw;
                    }
                }

                // 如果解析结果异常大（>10万 GB），忽略并回退到系统值
                if (!isFinite(memGB) || memGB > 100000) {
                    console.log('[WARN] Parsed memGB is unreasonable:', memGB, 'falling back to system memory');
                } else {
                    memVal = `${Math.round(memGB)} GB`;
                    console.log('[DEBUG] Parsed memory =>', memGB, 'GB');
                }
            }
        } else {
            console.log('[DEBUG] .wslconfig not found at:', wslConfigPath);
        }
    } catch (e) {
        console.log('[DEBUG] Error reading .wslconfig:', e.message);
    }
    
    console.log('[DEBUG] Final memVal:', memVal);
    return `MAX CPU: ${cpuVal} | MAX RAM: ${memVal}`;
}

// ---- Live editor sub-windows ----
const editorWindows = new Map(); // sessionKey -> BrowserWindow

function createWindow() {
    // 使用应用图标（开发环境和打包环境路径不同）
    let winIcon = null;
    const iconCandidates = [
        path.join(__dirname, 'build', 'icon.ico'),
        path.join(__dirname, 'build', 'icons', 'icon.ico'),
        path.join(process.resourcesPath || __dirname, 'icon.ico'),
        path.join(process.resourcesPath || __dirname, 'build', 'icon.ico')
    ];
    for (const p of iconCandidates) {
        try {
            if (fs.existsSync(p)) { winIcon = nativeImage.createFromPath(p); break; }
        } catch (e) {}
    }

    mainWindow = new BrowserWindow({
        width: 1200,
        height: 800,
        icon: winIcon || undefined,
        autoHideMenuBar: true,
        webPreferences: {
            nodeIntegration: true,
            contextIsolation: false, // 允许前端直接使用 ipcRenderer
            webviewTag: true // 允许内置浏览器使用 webview 标签
        }
    });
    mainWindow.loadFile(path.join(__dirname, 'src/index.html'));
    
    mainWindow.webContents.on('did-finish-load', () => {
        mainWindow.setTitle(`PrimiGenius v${app.getVersion()} - ${getSystemInfo()}`);
        mainWindow.focus();
        mainWindow.webContents.focus();
    });

    mainWindow.on('focus', () => {
        try { mainWindow.webContents.focus(); } catch(e) {}
    });

    // 如果关键操作期间被外部窗口/进程短暂抢走焦点，立即抢回主窗口和 webContents。
    mainWindow.on('blur', () => {
        if (Date.now() >= focusGuardUntil) return;
        setTimeout(() => {
            if (Date.now() < focusGuardUntil) focusMainWindowNow();
        }, 40);
        setTimeout(() => {
            if (Date.now() < focusGuardUntil) focusMainWindowNow();
        }, 140);
    });

    // 拦截 Ctrl+R / F5 实现前端软刷新（不重启后台，只刷新UI和数据缓存）
    mainWindow.webContents.on('before-input-event', (event, input) => {
        if ((input.control && input.key.toLowerCase() === 'r' && !input.shift) || input.key === 'F5') {
            event.preventDefault();
            // 发送软刷新信号给前端，前端重新拉取插件配置并刷新菜单
            mainWindow.webContents.send('soft-refresh');
        }
        // Ctrl+Shift+I opens DevTools for debugging
        if (input.control && input.shift && input.key.toLowerCase() === 'i') {
            mainWindow.webContents.toggleDevTools();
        }
    });
    
    // 创建最精简的隐藏菜单，保留 Edit role 使 Ctrl+C/V/X/Z/A 在输入框内正常工作
    // 如果完全移除菜单 (Menu.setApplicationMenu(null))，Electron 在某些版本/平台
    // 下会导致输入框内的剪贴板快捷键及焦点行为异常。
    const minimalMenu = Menu.buildFromTemplate([
        {
            label: 'Edit',
            submenu: [
                { role: 'undo' },
                { role: 'redo' },
                { type: 'separator' },
                { role: 'cut' },
                { role: 'copy' },
                { role: 'paste' },
                { role: 'delete' },
                { type: 'separator' },
                { role: 'selectAll' }
            ]
        }
    ]);
    Menu.setApplicationMenu(minimalMenu);
    mainWindow.setMenuBarVisibility(false);
}

// ---- IPC: Focus webContents ----
ipcMain.on('force-webcontents-focus', () => {
    if (mainWindow && !mainWindow.isDestroyed()) {
        extendFocusGuard();
        focusMainWindowNow();
    }
});

// ---- IPC: Create editor sub-window ----
ipcMain.handle('create-editor-window', (event, { sessionKey, filename, svgText, lang, posIndex, totalCount }) => {
    // If window already exists for this session, just update and focus
    if (editorWindows.has(sessionKey)) {
        const existing = editorWindows.get(sessionKey);
        if (!existing.isDestroyed()) {
            existing.webContents.send('update-svg', svgText);
            existing.focus();
            return { status: 'updated' };
        }
        editorWindows.delete(sessionKey);
    }

    // Calculate tiled position on screen
    const displays = screen.getAllDisplays();
    const primaryDisplay = screen.getPrimaryDisplay();
    const { width: scrW, height: scrH } = primaryDisplay.workAreaSize;
    const cols = Math.min(totalCount || 1, 3);
    const winW = Math.max(700, Math.floor(scrW / cols));
    const winH = Math.max(500, scrH - 50);
    const col = (posIndex || 0) % cols;
    const row = Math.floor((posIndex || 0) / cols);
    const posX = col * winW;
    const posY = row * 60;

    const editorWin = new BrowserWindow({
        width: winW,
        height: winH,
        x: posX,
        y: posY,
        title: `${filename} — Vector Editor (LIVE)`,
        parent: null, // independent window, can be dragged freely
        autoHideMenuBar: true,
        webPreferences: {
            nodeIntegration: true,
            contextIsolation: false
        }
    });

    editorWin.loadFile(path.join(__dirname, 'src/editor.html'));
    editorWin.setMenuBarVisibility(false);

    editorWin.webContents.once('did-finish-load', () => {
        editorWin.webContents.send('init-editor', {
            sessionKey,
            filename,
            svgText,
            lang
        });
    });

    editorWin.on('closed', () => {
        editorWindows.delete(sessionKey);
        // Notify main window that editor was closed
        if (mainWindow && !mainWindow.isDestroyed()) {
            mainWindow.webContents.send('editor-window-closed', sessionKey);
        }
    });

    editorWindows.set(sessionKey, editorWin);
    return { status: 'created' };
});

// ---- IPC: Update SVG in existing editor window ----
ipcMain.handle('update-editor-svg', (event, { sessionKey, svgText }) => {
    if (editorWindows.has(sessionKey)) {
        const win = editorWindows.get(sessionKey);
        if (!win.isDestroyed()) {
            win.webContents.send('update-svg', svgText);
            win.focus();
            return { status: 'updated' };
        }
    }
    return { status: 'not-found' };
});

// ---- IPC: Close editor window ----
ipcMain.handle('close-editor-window', (event, sessionKey) => {
    if (editorWindows.has(sessionKey)) {
        const win = editorWindows.get(sessionKey);
        if (!win.isDestroyed()) win.close();
        editorWindows.delete(sessionKey);
    }
    return { status: 'ok' };
});

// ---- IPC: Broadcast language change to all editor windows ----
ipcMain.on('update-editor-lang', (event, lang) => {
    editorWindows.forEach((win, key) => {
        if (!win.isDestroyed()) {
            win.webContents.send('update-lang', lang);
        }
    });
});

// ---- IPC: Open GitHub OAuth in-app browser window ----
let ghAuthWindow = null;
ipcMain.handle('open-github-auth', (event, { url, userCode }) => {
    if (ghAuthWindow && !ghAuthWindow.isDestroyed()) {
        ghAuthWindow.loadURL(url);
        ghAuthWindow.focus();
        return { status: 'reused' };
    }
    ghAuthWindow = new BrowserWindow({
        width: 520,
        height: 700,
        parent: mainWindow,
        modal: false,
        autoHideMenuBar: true,
        title: 'GitHub Authorization',
        webPreferences: {
            nodeIntegration: false,
            contextIsolation: true
        }
    });
    ghAuthWindow.setMenuBarVisibility(false);
    ghAuthWindow.loadURL(url);
    ghAuthWindow.on('closed', () => { ghAuthWindow = null; });
    return { status: 'opened' };
});
ipcMain.handle('close-github-auth', () => {
    if (ghAuthWindow && !ghAuthWindow.isDestroyed()) {
        ghAuthWindow.close();
        ghAuthWindow = null;
    }
});

// ---- IPC: App update actions from renderer ----
ipcMain.handle('update-download-request', async () => {
    const networkMode = await _getPluginDownloadNetworkMode();
    const acceleratedCandidates = latestUpdatePayload && Array.isArray(latestUpdatePayload.downloadCandidates)
        ? _orderReleaseDownloadUrlsForMode(latestUpdatePayload.downloadCandidates, networkMode)
        : [];
    if (acceleratedCandidates.length) {
        try {
            const installerPath = await _downloadInstallerInAppByMode(
                acceleratedCandidates,
                latestUpdatePayload.version || '?',
                networkMode
            );
            return { ok: true, manual: true, installerPath };
        } catch (e) {
            if (mainWindow && !mainWindow.isDestroyed()) {
                mainWindow.webContents.send('sys-log', `[UPDATE] 应用内下载失败: ${e.message || e}\n`);
            }
            if (latestUpdatePayload && latestUpdatePayload.manualOnly) {
                return { ok: false, manual: true, reason: e.message || String(e) };
            }
        }
    }

    if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('update-download-start');
        mainWindow.webContents.send('sys-log', '[UPDATE] Falling back to default updater download\n');
    }
    autoUpdater.downloadUpdate().catch((err) => {
        console.log('[UPDATER] 下载启动失败:', err && err.message ? err.message : err);
        if (mainWindow && !mainWindow.isDestroyed()) {
            mainWindow.webContents.send('sys-log', `[UPDATE] 下载启动失败: ${err && err.message ? err.message : err}\n`);
        }
    });
    return { ok: true };
});

// ---- IPC: Open file dialog for workflow background ----
ipcMain.handle('open-file-dialog', async (event, options) => {
    const result = await dialog.showOpenDialog(mainWindow, options);
    return result;
});

ipcMain.handle('save-file-dialog', async (event, options) => {
    return await dialog.showSaveDialog(mainWindow, options);
});

ipcMain.handle('update-install-request', async () => {
    const installerPath = _isInstallerFileUsable(manualDownloadedInstallerPath)
        ? manualDownloadedInstallerPath
        : _getCachedInstaller(latestUpdatePayload && latestUpdatePayload.version);
    if (installerPath) {
        try {
            await _launchDownloadedInstaller(installerPath);
            // Return to the renderer first so the click gets immediate visual
            // feedback. Cleanup may synchronously wait for the backend process.
            setTimeout(() => {
                cleanupBackendProcess();
                app.quit();
            }, 250);
            return { ok: true, manual: true };
        } catch (e) {
            if (mainWindow && !mainWindow.isDestroyed()) {
                mainWindow.webContents.send('sys-log', `[UPDATE] 启动安装程序失败: ${e.message || e}\n`);
            }
            return { ok: false, manual: true, reason: e.message || String(e) };
        }
    }

    cleanupBackendProcess();
    autoUpdater.quitAndInstall(false, true);
    return { ok: true };
});

function cleanupBackendProcess() {
    stopPodmanOnQuit();

    if (!pyProc) return;

    const pid = pyProc.pid;
    try {
        pyProc.kill();
    } catch (e) {}

    // On Windows, ensure the whole process tree is terminated to release file locks.
    if (process.platform === 'win32' && pid) {
        try {
            execSync(`taskkill /PID ${pid} /T /F`, { stdio: 'ignore', timeout: 5000 });
        } catch (e) {}
    }

    pyProc = null;
}

function getProjectRoot() {
    return app.isPackaged
        ? path.dirname(process.execPath)
        : path.join(__dirname, '..');
}

function resolveWindowsProfileForGui() {
    const candidates = [];
    try {
        const desktopDir = app.getPath('desktop');
        if (desktopDir) candidates.push(path.dirname(desktopDir));
    } catch (e) {}
    try {
        const homeDir = app.getPath('home');
        if (homeDir) candidates.push(homeDir);
    } catch (e) {}
    ['USERPROFILE', 'HOME'].forEach((key) => {
        if (process.env[key]) candidates.push(process.env[key]);
    });
    if (process.env.HOMEDRIVE && process.env.HOMEPATH) {
        candidates.push(process.env.HOMEDRIVE + process.env.HOMEPATH);
    }

    const projectRoot = path.resolve(getProjectRoot());
    for (const candidate of candidates) {
        if (!candidate) continue;
        const normalized = path.resolve(candidate);
        if (normalized === projectRoot || normalized.startsWith(projectRoot + path.sep)) continue;
        if (fs.existsSync(normalized)) return normalized;
    }
    return os.homedir();
}

function createBackendEnv() {
    const env = { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' };
    env.PRIMIGENIUS_CHANNEL = app.isPackaged ? 'stable' : 'dev';
    if (process.platform === 'win32') {
        const userHome = resolveWindowsProfileForGui();
        env.USERPROFILE = userHome;
        env.HOME = userHome;
        if (/^[a-zA-Z]:/.test(userHome)) {
            env.HOMEDRIVE = userHome.slice(0, 2);
            env.HOMEPATH = userHome.slice(2) || '\\';
        }
        delete env.DISPLAY;
        delete env.WAYLAND_DISPLAY;
        delete env.XDG_SESSION_TYPE;
    }
    return env;
}

function createPyProc() {
    return new Promise((resolve, reject) => {
    const isPackaged = app.isPackaged;
    let script;
    
    if (isPackaged) {
        const fs = require('fs');
        const exePath = path.join(process.resourcesPath, 'backend', 'app.exe');
        if (fs.existsSync(exePath)) {
            pyProc = spawn(exePath, [], { env: createBackendEnv(), detached: true });
        } else {
            script = path.join(process.resourcesPath, 'app.asar', 'backend', 'app.py');
            pyProc = spawn('python', [script], {
                env: createBackendEnv(),
                detached: true
            });
        }
    } else {
        script = path.join(__dirname, 'backend', 'app.py');
        pyProc = spawn('python', [script], {
            env: createBackendEnv(),
            detached: true
        });
    }

    if (pyProc != null) {
        console.log('Python backend started.');

        let startupSettled = false;
        const startupTimeout = setTimeout(() => {
            if (startupSettled) return;
            startupSettled = true;
            reject(new Error('Backend did not report its API port within 30 seconds.'));
        }, 30000);

        const settleStartup = (error, port) => {
            if (startupSettled) return;
            startupSettled = true;
            clearTimeout(startupTimeout);
            if (error) reject(error);
            else resolve(port);
        };

        // Forward complete backend log lines to the renderer.
        let stdoutBuf = '';
        let stderrBuf = '';
        const forwardBackendChunk = (kind, msg) => {
            let buf = (kind === 'stderr' ? stderrBuf : stdoutBuf) + String(msg || '');
            const parts = buf.split(/\r?\n/);
            buf = parts.pop() || '';
            if (kind === 'stderr') stderrBuf = buf;
            else stdoutBuf = buf;
            for (const line of parts) {
                if (kind === 'stdout') {
                    const match = line.match(/^PRIMIGENIUS_API_PORT=(\d+)$/);
                    if (match) {
                        const reportedPort = Number(match[1]);
                        if (Number.isInteger(reportedPort) && reportedPort > 0 && reportedPort <= 65535) {
                            backendPort = reportedPort;
                            console.log(`[PrimiGenius] Backend API ready on port ${backendPort}.`);
                            settleStartup(null, backendPort);
                        }
                    }
                }
                if (mainWindow && line) mainWindow.webContents.send('sys-log', line + '\n');
            }
        };
        const flushBackendChunks = () => {
            if (mainWindow && stdoutBuf) mainWindow.webContents.send('sys-log', stdoutBuf + '\n');
            if (mainWindow && stderrBuf) mainWindow.webContents.send('sys-log', stderrBuf + '\n');
            stdoutBuf = '';
            stderrBuf = '';
        };

        pyProc.stdout.on('data', function (data) {
            const msg = data.toString();
            console.log('PY_LOG:', msg); // 控制台留底
            forwardBackendChunk('stdout', msg);
        });

        pyProc.stderr.on('data', function (data) {
            const msg = data.toString();
            console.log('PY_ERR:', msg);
            forwardBackendChunk('stderr', msg);
        });
        pyProc.on('error', error => settleStartup(error));
        pyProc.on('exit', (code, signal) => {
            if (!startupSettled) {
                settleStartup(new Error(`Backend exited before startup (code=${code}, signal=${signal || 'none'}).`));
            }
        });
        pyProc.on('close', flushBackendChunks);
    } else {
        reject(new Error('Failed to create the backend process.'));
    }
    });
}

// ---- Podman Machine 深度集成 (后台异步，不阻塞 UI) ----
function getPodmanExePath() {
    const projectRoot = app.isPackaged
        ? path.dirname(process.execPath)
        : path.join(__dirname, '..');

    const candidates = [
        path.join(projectRoot, 'podman', 'podman.exe'),
        path.join(process.resourcesPath || '', 'podman', 'podman.exe'),
        path.join(path.dirname(process.execPath), 'podman', 'podman.exe'),
    ];
    for (const p of candidates) {
        try { if (fs.existsSync(p)) return p; } catch(e) {}
    }
    return 'podman';
}

function getPodmanDataDir() {
    const projectRoot = app.isPackaged
        ? path.dirname(process.execPath)
        : path.join(__dirname, '..');
    return path.join(projectRoot, 'podman-data');
}

function getPodmanConfigDir() {
    const projectRoot = app.isPackaged
        ? path.dirname(process.execPath)
        : path.join(__dirname, '..');
    return path.join(projectRoot, 'podman-config');
}

function prependPathOnce(pathValue, dir) {
    if (!dir) return pathValue || '';
    const delimiter = path.delimiter;
    const normalizedDir = path.normalize(dir).toLowerCase();
    const entries = String(pathValue || '').split(delimiter).filter(Boolean);
    if (entries.some(entry => path.normalize(entry).toLowerCase() === normalizedDir)) {
        return pathValue || '';
    }
    return dir + (pathValue ? delimiter + pathValue : '');
}

function createPodmanEnv() {
    const env = Object.assign({}, process.env);
    const configDir = getPodmanConfigDir();
    const dataDir = getPodmanDataDir();
    env.XDG_CONFIG_HOME = configDir;
    env.XDG_DATA_HOME = dataDir;
    env.PODMAN_CONFIG_DIR = configDir;
    env.CONTAINERS_CONF_DIR = configDir;
    env.CONTAINERS_STORAGE_CONF = path.join(configDir, 'storage.conf');
    env.CONTAINERS_MACHINE_PROVIDER_DIR = path.join(dataDir, 'machine');
    if (process.platform === 'win32') {
        env.APPDATA = configDir;
        env.USERPROFILE = configDir;
        env.LOCALAPPDATA = dataDir;
    }
    const podmanExe = getPodmanExePath();
    if (path.isAbsolute(podmanExe)) {
        const podmanBinDir = path.dirname(podmanExe);
        if (fs.existsSync(podmanBinDir)) {
            env.PATH = prependPathOnce(env.PATH || '', podmanBinDir);
        }
    }
    delete env.DOCKER_HOST;
    return env;
}

function runPodmanCommand(args, timeout = 60000) {
    const podmanExe = getPodmanExePath();
    try {
        return execFileSync(podmanExe, args, {
            timeout,
            encoding: 'utf-8',
            env: createPodmanEnv(),
            windowsHide: true,
            stdio: ['pipe', 'pipe', 'pipe']
        });
    } catch (e) {
        return null;
    }
}

function ensurePodmanRunning() {
    const { exec, spawn: spawnProc } = require('child_process');
    let podmanReadyAnnounced = false;
    let podmanReadyCheckTimer = null;

    function sendPodmanLog(msg) {
        console.log(msg);
        if (mainWindow && !mainWindow.isDestroyed()) {
            mainWindow.webContents.send('sys-log', msg + '\n');
        }
    }

    function scheduleBackendReadyCheck(retries, delay = 3000) {
        if (podmanReadyAnnounced || retries <= 0 || podmanReadyCheckTimer) return;
        podmanReadyCheckTimer = setTimeout(() => {
            podmanReadyCheckTimer = null;
            checkBackendReady(retries);
        }, delay);
    }

    sendPodmanLog('[PODMAN] PrimiGenius deep-integrated Podman starting...');
    sendPodmanLog('[PODMAN] Podman binary: ' + getPodmanExePath());
    sendPodmanLog('[PODMAN] Data dir: ' + getPodmanDataDir());

    const versionResult = runPodmanCommand(['--version'], 5000);
    if (versionResult) {
        sendPodmanLog('[PODMAN] ' + versionResult.trim());
    } else {
        sendPodmanLog('[PODMAN] Podman binary not found. Deep integration requires bundled Podman.');
    }

    sendPodmanLog('[PODMAN] Podman service will be managed by backend via WSL direct management.');
    const podmanApiPort = app.isPackaged ? 8888 : 8889;
    sendPodmanLog(`[PODMAN] Backend will auto-start Podman API service on port ${podmanApiPort}.`);

    function checkBackendReady(retries) {
        if (podmanReadyAnnounced) return;
        if (retries <= 0) {
            sendPodmanLog('[PODMAN] Backend Podman service did not become ready in time.');
            return;
        }
        try {
            const http = require('http');
            let settled = false;
            const retryOnce = () => {
                if (settled || podmanReadyAnnounced) return;
                settled = true;
                scheduleBackendReadyCheck(retries - 1);
            };
            const req = http.request({
                hostname: '127.0.0.1',
                port: backendPort,
                path: '/podman/info',
                method: 'GET',
                timeout: 5000
            }, (res) => {
                let data = '';
                res.on('data', chunk => data += chunk);
                res.on('end', () => {
                    if (settled || podmanReadyAnnounced) return;
                    settled = true;
                    try {
                        const json = JSON.parse(data);
                        if (json.status === 'success' && !json.loading) {
                            if (podmanReadyCheckTimer) {
                                clearTimeout(podmanReadyCheckTimer);
                                podmanReadyCheckTimer = null;
                            }
                            sendPodmanLog('[PODMAN] Podman engine is ready!');
                            podmanReadyAnnounced = true;
                        } else {
                            scheduleBackendReadyCheck(retries - 1);
                        }
                    } catch(e) {
                        scheduleBackendReadyCheck(retries - 1);
                    }
                });
            });
            req.on('error', retryOnce);
            req.on('timeout', () => {
                req.destroy();
                retryOnce();
            });
            req.end();
        } catch(e) {
            scheduleBackendReadyCheck(retries - 1);
        }
    }

    setTimeout(() => checkBackendReady(60), 2000);
}

function stopPodmanOnQuit() {
    try {
        const http = require('http');
        const postData = JSON.stringify({ stop_machine: true });
        const req = http.request({
            hostname: '127.0.0.1',
            port: backendPort,
            path: '/podman/shutdown',
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(postData)
            },
            timeout: 8000
        }, () => {});
        req.on('error', () => {});
        req.write(postData);
        req.end();
    } catch(e) {}
}

function postBackendLifecycle(pathname, timeout) {
    return new Promise((resolve, reject) => {
        if (!backendPort) return reject(new Error('Backend API is unavailable'));
        const req = http.request({
            hostname: '127.0.0.1',
            port: backendPort,
            path: pathname,
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Content-Length': 2 },
            timeout
        }, (res) => {
            let data = '';
            res.on('data', chunk => data += chunk);
            res.on('end', () => {
                try { resolve(JSON.parse(data || '{}')); }
                catch (error) { reject(error); }
            });
        });
        req.on('error', reject);
        req.on('timeout', () => req.destroy(new Error(`${pathname} timed out`)));
        req.end('{}');
    });
}

function emitPowerLifecycleLog(message) {
    console.log(message);
    if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('sys-log', message + '\n');
    }
}

function setupPowerLifecycle() {
    if (process.platform !== 'win32') return;
    powerMonitor.on('suspend', () => {
        if (resumeRecoveryTimer) {
            clearTimeout(resumeRecoveryTimer);
            resumeRecoveryTimer = null;
        }
        emitPowerLifecycleLog('[PODMAN] Windows is suspending; preparing the container environment...');
        postBackendLifecycle('/podman/suspend', 15000).then(result => {
            if (result.status === 'busy') {
                emitPowerLifecycleLog('[PODMAN] Active work detected; the container environment was kept running.');
            } else if (result.status === 'success') {
                emitPowerLifecycleLog('[PODMAN] Container environment stopped safely before suspend.');
            } else {
                emitPowerLifecycleLog(`[PODMAN] Suspend preparation failed: ${result.message || 'unknown error'}`);
            }
        }).catch(error => emitPowerLifecycleLog(`[PODMAN] Suspend preparation did not complete: ${error.message}`));
    });

    powerMonitor.on('resume', () => {
        if (resumeRecoveryTimer) clearTimeout(resumeRecoveryTimer);
        emitPowerLifecycleLog('[PODMAN] Windows resumed; waiting for networking and virtualization...');
        resumeRecoveryTimer = setTimeout(() => {
            resumeRecoveryTimer = null;
            postBackendLifecycle('/podman/resume', 120000).then(result => {
                if (result.status === 'busy') {
                    emitPowerLifecycleLog('[PODMAN] Active work detected; automatic recovery was skipped.');
                } else if (result.status === 'success') {
                    emitPowerLifecycleLog('[PODMAN] Container environment recovered after resume.');
                } else {
                    emitPowerLifecycleLog(`[PODMAN] Resume recovery failed: ${result.message || 'restart Windows and try again'}`);
                }
            }).catch(error => emitPowerLifecycleLog(`[PODMAN] Resume recovery did not complete: ${error.message}`));
        }, 15000);
    });
}

// ---- Auto-Update: electron-updater (GitHub Releases) ----
function setupAutoUpdater() {
    // 日志输出到控制台
    autoUpdater.logger = {
        info: (...args) => console.log('[UPDATER]', ...args),
        warn: (...args) => console.warn('[UPDATER]', ...args),
        error: (...args) => console.error('[UPDATER]', ...args),
        debug: (...args) => console.log('[UPDATER:DEBUG]', ...args),
    };
    autoUpdater.autoDownload = false;       // 先询问用户再下载
    // 仅在用户点击“立即安装”后安装，避免退出时静默触发安装。
    autoUpdater.autoInstallOnAppQuit = false;

    // --- 检查到有新版本 ---
    autoUpdater.on('update-available', (info) => {
        const ver = info.version || '?';
        if (
            didEmitUpdateAvailable &&
            latestUpdatePayload &&
            _normalizeVersion(latestUpdatePayload.version) === _normalizeVersion(ver)
        ) {
            return;
        }
        const notes = extractReleaseNotes(info).substring(0, 4000);
        const downloadCandidates = _buildInstallerCandidates(_installerAssetFromUpdateInfo(info));
        const payload = {
            version: ver,
            currentVersion: app.getVersion(),
            releaseNotes: notes,
            releaseName: info.releaseName || '',
            releaseDate: info.releaseDate || '',
            manualOnly: false,
            downloadCandidates
        };
        latestUpdatePayload = payload;
        didEmitUpdateAvailable = true;
        if (mainWindow && !mainWindow.isDestroyed()) {
            mainWindow.webContents.send('sys-log', `[UPDATE] Find new version v${ver}\n`);
            mainWindow.webContents.send('update-available', payload);
            const cachedInstaller = _getCachedInstaller(ver);
            if (cachedInstaller) {
                mainWindow.webContents.send('sys-log', `[UPDATE] Reusing downloaded installer: ${cachedInstaller}\n`);
                setTimeout(() => _emitInstallerReady(cachedInstaller, ver), 250);
            }
        }
    });

    // --- 没有更新 ---
    autoUpdater.on('update-not-available', () => {
        console.log('[UPDATER] 当前已是最新版本。');
    });

    // --- 下载进度 ---
    autoUpdater.on('download-progress', (progress) => {
        const pct = Math.round(progress.percent || 0);
        const speed = ((progress.bytesPerSecond || 0) / 1024 / 1024).toFixed(1);
        const transferred = ((progress.transferred || 0) / 1024 / 1024).toFixed(1);
        const total = ((progress.total || 0) / 1024 / 1024).toFixed(1);
        if (mainWindow && !mainWindow.isDestroyed()) {
            mainWindow.webContents.send('update-download-progress', {
                percent: pct, speed, transferred, total
            });
        }
    });

    // --- 下载完成 ---
    autoUpdater.on('update-downloaded', (info) => {
        const ver = info.version || '?';
        if (mainWindow && !mainWindow.isDestroyed()) {
            mainWindow.webContents.send('update-download-complete');
            mainWindow.webContents.send('update-ready-to-install', {
                version: ver,
                currentVersion: app.getVersion()
            });
        }
    });

    // --- 更新出错 ---
    autoUpdater.on('error', (err) => {
        console.log('[UPDATER] 更新检查失败(已静默到UI):', err && err.message ? err.message : err);
        _emitMirrorFallbackUpdateIfNeeded('autoUpdater-error').catch(() => {});
    });

    // 延迟 5 秒后检查更新
    setTimeout(() => {
        autoUpdater.checkForUpdates().catch(e => {
            console.log('[UPDATER] checkForUpdates error:', e.message);
            _emitMirrorFallbackUpdateIfNeeded('checkForUpdates-catch').catch(() => {});
        });
    }, 5000);

    // 国内网络场景兜底：若主检测没有事件，主动用镜像源做一次 latest release 检测。
    setTimeout(() => {
        _emitMirrorFallbackUpdateIfNeeded('scheduled-fallback').catch(() => {});
    }, 9000);
}

// ---- 插件远程更新 ----

// 获取本地已安装插件列表（通过后端 API）
async function _getLocalPlugins() {
    const cacheNow = Date.now();
    if (localPluginCache.data && cacheNow - localPluginCache.time < PLUGIN_UPDATE_CACHE_TTL) {
        return localPluginCache.data;
    }
    return new Promise((resolve) => {
        const http = require('http');
        const req = http.request({
            hostname: '127.0.0.1',
            port: backendPort,
            path: '/get-plugins',
            method: 'GET',
            timeout: 5000
        }, (res) => {
            let data = '';
            res.on('data', chunk => data += chunk);
            res.on('end', () => {
                try {
                    const json = JSON.parse(data);
                    // /get-plugins 返回 {"status":"success","plugins":[...]} 数组
                    // 需要转为 {id: pluginObj} 的映射
                    const arr = json.plugins || (Array.isArray(json) ? json : []);
                    const map = {};
                    for (const p of arr) {
                        if (p && p.id) map[p.id] = p;
                    }
                    _seedPluginInstallHistory(map);
                    localPluginCache = { data: map, time: Date.now() };
                    resolve(map);
                } catch (e) {
                    resolve({});
                }
            });
        });
        req.on('error', () => resolve({}));
        req.on('timeout', () => { req.destroy(); resolve({}); });
        req.end();
    });
}

// 拉取远程插件清单：短时尝试 GitHub main；不可达时立即选择最新的 CDN/代理结果。
async function _fetchPluginRegistry() {
    const cacheNow = Date.now();
    if (pluginRegistryCache.data && cacheNow - pluginRegistryCache.time < PLUGIN_UPDATE_CACHE_TTL) {
        return pluginRegistryCache.data;
    }

    try {
        const splitRegistry = await _fetchSplitPluginRegistries();
        if (splitRegistry && Array.isArray(splitRegistry.plugins) && splitRegistry.plugins.length) {
            console.log(`[PLUGIN-UPDATE] split registry loaded: ${splitRegistry.plugins.length} total`);
            pluginRegistryCache = { data: splitRegistry, time: Date.now() };
            return splitRegistry;
        }
    } catch (e) {
        console.log('[PLUGIN-UPDATE] split registry fetch failed:', e.message);
    }

    const acceptRegistry = (data, source) => {
        const registry = _decodePluginRegistryPayload(data);
        console.log('[PLUGIN-UPDATE] registry from:', source);
        pluginRegistryCache = { data: registry, time: Date.now() };
        return registry;
    };

    const trySequentially = async (urls, sourcePrefix) => {
        for (const url of _uniqueUrls(urls)) {
            try {
                return acceptRegistry(
                    await _fetchJson(url, 12000, { noCache: true }),
                    `${sourcePrefix}: ${url}`
                );
            } catch (e) {
                console.log(`[PLUGIN-UPDATE] ${sourcePrefix} failed:`, url, e.message);
            }
        }
        return null;
    };

    const githubCandidate = await _fetchFirstPluginRegistryCandidate([
        `https://raw.githubusercontent.com/${PLUGIN_REGISTRY_REPO}/main/plugins-registry.json`,
        `https://api.github.com/repos/${PLUGIN_REGISTRY_REPO}/contents/plugins-registry.json?ref=main`
    ], 3200, 'combined registry GitHub main');
    if (githubCandidate) {
        return acceptRegistry(githubCandidate.registry, `GitHub main: ${githubCandidate.source}`);
    }

    const cdnFallbacks = [
        `https://fastly.jsdelivr.net/gh/${PLUGIN_REGISTRY_REPO}@main/plugins-registry.json`,
        `https://gcore.jsdelivr.net/gh/${PLUGIN_REGISTRY_REPO}@main/plugins-registry.json`,
        `https://cdn.jsdelivr.net/gh/${PLUGIN_REGISTRY_REPO}@main/plugins-registry.json`,
        `https://mirror.ghproxy.com/https://raw.githubusercontent.com/${PLUGIN_REGISTRY_REPO}/main/plugins-registry.json`,
        `https://gh.llkk.cc/https://raw.githubusercontent.com/${PLUGIN_REGISTRY_REPO}/main/plugins-registry.json`,
        `https://hubp.llkk.cc/https://raw.githubusercontent.com/${PLUGIN_REGISTRY_REPO}/main/plugins-registry.json`
    ];
    const cdnCandidate = await _fetchFreshestPluginRegistryCandidate(
        cdnFallbacks,
        6500,
        'combined registry CDN fallback'
    );
    if (cdnCandidate) {
        return acceptRegistry(cdnCandidate.registry, `CDN fallback: ${cdnCandidate.source}`);
    }

    const releaseRegistry = await trySequentially(
        PLUGIN_REGISTRY_RELEASE_ASSET_CANDIDATES,
        'release latest asset'
    );
    if (releaseRegistry) return releaseRegistry;

    for (const apiUrl of PLUGIN_REGISTRY_API_CANDIDATES) {
        try {
            const releaseData = await _fetchJson(apiUrl, 12000, { noCache: true });
            if (!releaseData || !Array.isArray(releaseData.assets)) continue;
            const registryAsset = releaseData.assets.find(a =>
                a && a.name === 'plugins-registry.json' && a.browser_download_url
            );
            if (!registryAsset) continue;
            const assetRegistry = await trySequentially(_uniqueUrls([
                registryAsset.browser_download_url,
                ..._githubReleaseDownloadUrls(registryAsset.browser_download_url),
                `https://mirror.ghproxy.com/${registryAsset.browser_download_url}`,
                `https://gh.llkk.cc/${registryAsset.browser_download_url}`,
                `https://hubp.llkk.cc/${registryAsset.browser_download_url}`
            ]), 'release API asset');
            if (assetRegistry) return assetRegistry;
        } catch (e) {
            console.log('[PLUGIN-UPDATE] release API failed:', apiUrl, e.message);
        }
    }

    return null;
}

// 验证插件 ZIP 签名
function _verifyPluginSignature(zipBuffer, signatureBase64) {
    try {
        const verify = crypto.createVerify('SHA256');
        verify.update(zipBuffer);
        verify.end();
        return verify.verify(PLUGIN_SIGN_PUBLIC_KEY, signatureBase64, 'base64');
    } catch (e) {
        console.log('[PLUGIN-UPDATE] signature verification error:', e.message);
        return false;
    }
}

// 计算文件 SHA256
function _computeSHA256(buffer) {
    return crypto.createHash('sha256').update(buffer).digest('hex');
}

// 从 GitHub Release API 获取 Release 描述（best-effort，限流时跳过）
async function _fetchReleaseNotes() {
    for (const url of PLUGIN_RELEASE_API_CANDIDATES) {
        try {
            const data = await _fetchJson(url, 8000);
            if (data && typeof data.body === 'string') {
                console.log('[PLUGIN-UPDATE] Release notes fetched from API');
                return data.body;
            }
        } catch (e) {
            console.log('[PLUGIN-UPDATE] Release API failed:', url, e.message);
        }
    }
    return '';
}

// 检查插件更新
function _textFromLocalizedValue(raw) {
    if (!raw) return '';
    if (typeof raw === 'string') return raw;
    if (typeof raw === 'object') {
        return raw.zh || raw.en || raw.default || Object.values(raw).find(v => typeof v === 'string') || '';
    }
    return '';
}

function _releaseNotesFromRegistry(registry) {
    if (!registry || typeof registry !== 'object') return '';
    return _textFromLocalizedValue(registry.release_notes || registry.releaseNotes || registry.notes || registry.changelog);
}

function _releaseNotesFromRemote(remote) {
    if (!remote || typeof remote !== 'object') return '';
    return _textFromLocalizedValue(
        remote.release_notes ||
        remote.releaseNotes ||
        remote.notes ||
        remote.changelog_notes ||
        remote.changelog
    );
}

function _pluginDisplayNameForNotes(update) {
    const raw = update && update.name;
    if (typeof raw === 'string') return raw;
    if (raw && typeof raw === 'object') return raw.zh || raw.en || raw.default || update.id || '';
    return (update && update.id) || '';
}

function _pluginReleaseTagCandidates(remote, remoteVersion) {
    const ids = _uniqueUrls([
        remote && remote.id,
        remote && remote.plugin_id,
        remote && remote.pluginId,
        remote && remote.name && typeof remote.name === 'string' ? remote.name : ''
    ]);
    const explicit = _uniqueUrls([
        remote && remote.release_tag,
        remote && remote.releaseTag,
        remote && remote.github_release_tag,
        remote && remote.githubReleaseTag,
        remote && remote.tag_name,
        remote && remote.tag
    ]);
    const version = _normalizeVersion(remoteVersion || (remote && remote.version));
    const inferred = [];
    for (const id of ids) {
        if (version) inferred.push(`${id}-v${version}`, `${id}-${version}`);
    }
    if (version) inferred.push(`v${version}`, version);
    return _uniqueUrls([...explicit, ...inferred]);
}

function _pluginReleaseApiCandidates(tag) {
    const encodedTag = encodeURIComponent(String(tag || '').trim());
    if (!encodedTag) return [];
    const api = `https://api.github.com/repos/jianbai-design/PrimiGenius-plugins/releases/tags/${encodedTag}`;
    return _uniqueUrls([
        api,
        `https://mirror.ghproxy.com/${api}`,
        `https://gh.llkk.cc/${api}`,
        `https://hubp.llkk.cc/${api}`
    ]);
}

async function _fetchReleaseNotesForTag(tag) {
    for (const url of _pluginReleaseApiCandidates(tag)) {
        try {
            const data = await _fetchJson(url, 8000);
            if (data && typeof data.body === 'string' && data.body.trim()) {
                console.log('[PLUGIN-UPDATE] Release notes fetched for tag:', tag);
                return data.body;
            }
        } catch (e) {
            console.log('[PLUGIN-UPDATE] Release tag API failed:', tag, url, e.message);
        }
    }
    return '';
}

function _releaseNotesFromUpdates(updates) {
    const parts = [];
    for (const update of updates || []) {
        const notes = _textFromLocalizedValue(update && (update.release_notes || update.releaseNotes));
        if (!notes.trim()) continue;
        const name = _pluginDisplayNameForNotes(update);
        const version = update && update.remoteVersion ? ` v${update.remoteVersion}` : '';
        parts.push(`## ${name}${version}\n\n${notes.trim()}`);
    }
    return parts.join('\n\n---\n\n');
}

async function _fetchReleaseNotesForUpdates(updates) {
    const enriched = [];
    for (const update of updates || []) {
        if (_textFromLocalizedValue(update.release_notes || update.releaseNotes).trim()) {
            enriched.push(update);
            continue;
        }
        let notes = '';
        for (const tag of update.releaseTagCandidates || []) {
            notes = await _fetchReleaseNotesForTag(tag);
            if (notes) break;
        }
        if (!notes) {
            console.log('[PLUGIN-UPDATE] Release notes unavailable for plugin:', update.id, 'tags=', (update.releaseTagCandidates || []).join(',') || '-');
        }
        enriched.push(notes ? { ...update, release_notes: notes } : update);
    }
    return enriched;
}

async function checkPluginUpdates() {
    if (pluginUpdateCheckDone) return;
    pluginUpdateCheckDone = true;

    try {
        const releaseNotesPromise = _fetchReleaseNotes().catch(() => '');
        const [registry, localPlugins] = await Promise.all([
            _fetchPluginRegistry(),
            _getLocalPlugins()
        ]);

        if (!registry || !registry.plugins) {
            console.log('[PLUGIN-UPDATE] Remote registry unavailable; skipped this check');
            return;
        }

        const localCount = Object.keys(localPlugins).length;
        console.log(`[PLUGIN-UPDATE] Registry loaded: ${registry.plugins.length} remote, ${localCount} local`);

        const currentAppVersion = _normalizeVersion(app.getVersion());
        const updates = [];

        for (const remote of registry.plugins) {
            // 检查最低主程序版本要求
            if (remote.min_app_version && _isNewerVersion(remote.min_app_version, currentAppVersion)) {
                continue; // 主程序版本太低，跳过
            }

            const local = localPlugins[remote.id];
            if (!local) continue; // 未安装该插件，跳过

            const localVersion = _normalizeVersion(local.version || '0.0.0');
            const remoteVersion = _normalizeVersion(remote.version || '0.0.0');

            console.log(`[PLUGIN-UPDATE] ${remote.id}: local=${localVersion} remote=${remoteVersion}`);

            if (_isNewerVersion(remoteVersion, localVersion)) {
                const releaseTagCandidates = _pluginReleaseTagCandidates(remote, remoteVersion);
                const releaseNotes = _releaseNotesFromRemote(remote);
                console.log(`[PLUGIN-UPDATE] ${remote.id}: release note tags=${releaseTagCandidates.join(',') || '-'} registry_notes=${releaseNotes ? 'yes' : 'no'}`);
                updates.push({
                    id: remote.id,
                    name: local.name || { zh: remote.id, en: remote.id },
                    localVersion: localVersion,
                    remoteVersion: remoteVersion,
                    changelog: remote.changelog || {},
                    release_notes: releaseNotes,
                    releaseTagCandidates: releaseTagCandidates,
                    download_url: remote.download_url,
                    signature_url: remote.signature_url,
                    sha256: remote.sha256,
                    multipart: remote.multipart,
                    download_parts: remote.download_parts || remote.downloadParts || remote.parts
                });
            }
        }

        if (updates.length > 0 && mainWindow && !mainWindow.isDestroyed()) {
            // 获取 Release 描述（best-effort，限流时为空）
            const taggedReleaseNotesPromise = _fetchReleaseNotesForUpdates(updates).catch(() => updates);
            const initialUpdateNotes = _releaseNotesFromUpdates(updates);
            let latestPluginReleaseNotes = initialUpdateNotes;
            const taggedNotesPromise = taggedReleaseNotesPromise.then((enrichedUpdates) => _releaseNotesFromUpdates(enrichedUpdates)).catch(() => '');
            const [taggedNotesBeforeDialog, fallbackReleaseNotes] = await Promise.all([
                initialUpdateNotes ? Promise.resolve('') : Promise.race([
                    taggedNotesPromise,
                    new Promise(resolve => setTimeout(() => resolve(''), 2500))
                ]),
                Promise.race([
                    releaseNotesPromise,
                    new Promise(resolve => setTimeout(() => resolve(''), 1800))
                ])
            ]);
            latestPluginReleaseNotes = initialUpdateNotes || taggedNotesBeforeDialog || '';
            const releaseNotes = latestPluginReleaseNotes || fallbackReleaseNotes || _releaseNotesFromRegistry(registry);
            mainWindow.webContents.send('sys-log', `[PLUGIN-UPDATE] ${updates.length} plugin update(s) available\n`);
            mainWindow.webContents.send('plugin-updates-available', {
                updates: updates,
                release_notes: releaseNotes
            });
            taggedReleaseNotesPromise.then((enrichedUpdates) => {
                const notes = _releaseNotesFromUpdates(enrichedUpdates);
                latestPluginReleaseNotes = notes || latestPluginReleaseNotes;
                const finalNotes = latestPluginReleaseNotes || fallbackReleaseNotes || _releaseNotesFromRegistry(registry);
                if (finalNotes && mainWindow && !mainWindow.isDestroyed()) {
                    mainWindow.webContents.send('plugin-update-release-notes', { release_notes: finalNotes });
                }
            }).catch(() => {});
            releaseNotesPromise.then((notes) => {
                const finalNotes = latestPluginReleaseNotes || notes || _releaseNotesFromRegistry(registry);
                if (finalNotes && mainWindow && !mainWindow.isDestroyed()) {
                    mainWindow.webContents.send('plugin-update-release-notes', { release_notes: finalNotes });
                }
            }).catch(() => {});
        } else {
            console.log('[PLUGIN-UPDATE] All plugins are up to date');
        }
    } catch (e) {
        console.log('[PLUGIN-UPDATE] Check failed:', e.message);
    }
}

// IPC: 下载并安装插件更新
ipcMain.handle('plugin-update-download', async (event, updateInfo) => {
    const normalizedParts = _normalizePluginDownloadParts(updateInfo);
    if (!updateInfo || (!updateInfo.download_url && !normalizedParts.length)) {
        return { ok: false, reason: 'Missing download URL' };
    }

    const updatesDir = path.join(app.getPath('userData'), 'plugin-updates');
    try { fs.mkdirSync(updatesDir, { recursive: true }); } catch (e) {}

    const pluginId = updateInfo.id;
    const remoteVersion = _normalizeVersion(updateInfo.remoteVersion || updateInfo.version || '0.0.0');
    const downloadToken = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const zipFileName = `${pluginId}-${remoteVersion}-${downloadToken}.zip`;
    const sigFileName = `${pluginId}-${remoteVersion}-${downloadToken}.zip.sig`;
    const zipPath = path.join(updatesDir, zipFileName);
    const sigPath = path.join(updatesDir, sigFileName);

    try {
        // 通知前端开始下载
        if (mainWindow && !mainWindow.isDestroyed()) {
            mainWindow.webContents.send('plugin-update-download-start', { id: pluginId });
            mainWindow.webContents.send('sys-log', `[PLUGIN-UPDATE] Downloading plugin: ${pluginId} v${remoteVersion}\n`);
        }

        // 构建 ZIP 下载候选 URL（原始 + 镜像）
        const networkMode = await _getPluginDownloadNetworkMode();
        if (mainWindow && !mainWindow.isDestroyed()) {
            mainWindow.webContents.send('sys-log', `[PLUGIN-UPDATE] Network mode: ${networkMode}\n`);
        }
        const zipCandidates = updateInfo.download_url ? _pluginDownloadCandidates(updateInfo.download_url, networkMode) : [];

        // 构建 SIG 下载候选 URL
        const sigCandidates = updateInfo.signature_url ? _pluginDownloadCandidates(updateInfo.signature_url, networkMode) : [];

        const sendPluginUpdateLog = (msg) => {
            if (mainWindow && !mainWindow.isDestroyed()) {
                mainWindow.webContents.send('sys-log', msg);
            }
        };

        const progressForPlugin = (p) => {
            if (mainWindow && !mainWindow.isDestroyed()) {
                mainWindow.webContents.send('plugin-update-download-progress', {
                    id: pluginId, ...p
                });
            }
        };
        const zipResult = normalizedParts.length
            ? await _downloadMultipartPluginPackage(updateInfo, normalizedParts, zipPath, networkMode, sendPluginUpdateLog, progressForPlugin)
            : await _downloadCandidateByRace(zipCandidates, zipPath, {
                kind: 'package',
                expectZip: true,
                probeTimeoutMs: networkMode === 'cn' ? 4200 : 6000,
                downloadTimeoutMs: 30000,
                orderedFirstCount: networkMode === 'cn' ? 0 : 1,
                raceFirstCount: networkMode === 'cn' ? _initialAcceleratedCandidateCount(zipCandidates) : undefined,
                log: sendPluginUpdateLog,
                onProgress: progressForPlugin,
                verifyFile: async (filePath) => {
                    if (updateInfo.sha256) {
                        const actualHash = await _computeFileSHA256(filePath);
                        if (actualHash !== updateInfo.sha256) {
                            try { fs.unlinkSync(zipPath); } catch (e) {}
                            throw new Error(`SHA256 mismatch: expected ${updateInfo.sha256}, got ${actualHash}`);
                        }
                        return actualHash;
                    }
                    return null;
                }
            });
        const zipSourceLabel = zipResult.candidate ? zipResult.candidate.label : '';

        let signatureBase64 = null;
        let signatureSourceLabel = '';
        if (sigCandidates.length) {
            try {
                const sigResult = await _downloadCandidateByRace(sigCandidates, sigPath, {
                    kind: 'signature',
                    expectZip: false,
                    probeTimeoutMs: networkMode === 'cn' ? 3000 : 5000,
                    downloadTimeoutMs: 10000,
                    orderedFirstCount: networkMode === 'cn' ? 0 : 1,
                    raceFirstCount: networkMode === 'cn' ? _initialAcceleratedCandidateCount(sigCandidates) : undefined,
                    log: sendPluginUpdateLog,
                    verifyFile: async (filePath) => {
                        const sigText = fs.readFileSync(filePath, 'utf8').trim();
                        if (!sigText || sigText.length < 64 || !/^[A-Za-z0-9+/=\s]+$/.test(sigText)) {
                            throw new Error('Invalid signature file');
                        }
                        if (!await _verifyPluginSignatureFile(zipPath, sigText)) {
                            throw new Error('Plugin signature verification failed! File may be tampered.');
                        }
                        return sigText;
                    }
                });
                signatureBase64 = sigResult.value;
                signatureSourceLabel = sigResult.candidate ? sigResult.candidate.label : '';
            } catch (e) {
                if (mainWindow && !mainWindow.isDestroyed()) {
                    mainWindow.webContents.send('sys-log', `[PLUGIN-UPDATE] Warning: signature download/verification failed: ${e.message || e}\n`);
                }
            }
        }

        // SHA256 校验
        if (updateInfo.sha256) {
            if (mainWindow && !mainWindow.isDestroyed()) {
                mainWindow.webContents.send('sys-log', `[PLUGIN-UPDATE] SHA256 verified${zipSourceLabel ? ` (${zipSourceLabel})` : ''}\n`);
            }
        }

        // 签名验证
        if (signatureBase64) {
            if (mainWindow && !mainWindow.isDestroyed()) {
                mainWindow.webContents.send('sys-log', `[PLUGIN-UPDATE] Signature verified${signatureSourceLabel ? ` (${signatureSourceLabel})` : ''}\n`);
            }
        } else {
            if (mainWindow && !mainWindow.isDestroyed()) {
                mainWindow.webContents.send('sys-log', `[PLUGIN-UPDATE] Warning: signature file not found, skipped\n`);
            }
        }

        // 通知前端下载完成
        if (mainWindow && !mainWindow.isDestroyed()) {
            mainWindow.webContents.send('plugin-update-download-complete', { id: pluginId });
        }

        // 调用后端 API 安装插件
        const http = require('http');
        const postData = JSON.stringify({ zipPath: zipPath });

        return new Promise((resolve) => {
            const req = http.request({
                hostname: '127.0.0.1',
                port: backendPort,
                path: '/install-plugin-panel',
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Content-Length': Buffer.byteLength(postData)
                },
                timeout: 30000
            }, (res) => {
                let data = '';
                res.on('data', chunk => data += chunk);
                res.on('end', () => {
                    try {
                        const result = JSON.parse(data);
                        if (result.status === 'success') {
                            localPluginCache = { data: null, time: 0 };
                            const installHistory = _recordPluginInstallHistory(pluginId, remoteVersion, 'plugin-store');
                            if (mainWindow && !mainWindow.isDestroyed()) {
                                mainWindow.webContents.send('sys-log', `[PLUGIN-UPDATE] Plugin ${pluginId} updated successfully!\n`);
                                if (installHistory.first_install) {
                                    mainWindow.webContents.send('sys-log', `[PLUGIN-HISTORY] First install recorded for this device: ${pluginId}\n`);
                                }
                                mainWindow.webContents.send('plugin-update-installed', { id: pluginId, version: remoteVersion });
                            }
                            // 清理临时文件
                            try { fs.unlinkSync(zipPath); } catch (e) {}
                            try { if (fs.existsSync(sigPath)) fs.unlinkSync(sigPath); } catch (e) {}
                            resolve({
                                ok: true,
                                firstInstallOnDevice: !!installHistory.first_install,
                                installHistoryRecorded: !!installHistory.ok
                            });
                        } else {
                            resolve({ ok: false, reason: result.message || 'Install failed' });
                        }
                    } catch (e) {
                        resolve({ ok: false, reason: e.message });
                    }
                });
            });
            req.on('error', (e) => resolve({ ok: false, reason: e.message }));
            req.on('timeout', () => { req.destroy(); resolve({ ok: false, reason: 'Install request timeout' }); });
            req.write(postData);
            req.end();
        });

    } catch (e) {
        if (mainWindow && !mainWindow.isDestroyed()) {
            mainWindow.webContents.send('sys-log', `[PLUGIN-UPDATE] Update failed: ${e.message}\n`);
        }
        return { ok: false, reason: e.message };
    }
});

// IPC: 手动触发插件更新检查
ipcMain.handle('check-plugin-updates', async () => {
    pluginUpdateCheckDone = false;
    pluginRegistryCache = { data: null, time: 0 };
    localPluginCache = { data: null, time: 0 };
    await checkPluginUpdates();
    return { ok: true };
});

ipcMain.handle('record-plugin-install-history', (event, payload = {}) => {
    return _recordPluginInstallHistory(
        payload && (payload.pluginId || payload.id),
        payload && payload.version,
        payload && payload.source
    );
});

ipcMain.handle('plugin-store-list', async (event, options = {}) => {
    try {
        let cachePurge = null;
        if (options && options.forceRefresh) {
            pluginRegistryCache = { data: null, time: 0 };
            localPluginCache = { data: null, time: 0 };
            cachePurge = await _purgePluginRegistryCdnCache();
        }
        const releaseHistoryPromise = _fetchPluginReleaseHistory({
            forceRefresh: !!(options && options.forceRefresh)
        }).then(value => ({ ok: true, ...value })).catch(error => ({
            ok: false,
            releases: [],
            stale: false,
            reason: error.message || String(error)
        }));
        const [registry, localPlugins, releaseHistory] = await Promise.all([
            _fetchPluginRegistry(),
            _getLocalPlugins(),
            releaseHistoryPromise
        ]);
        const installCounts = releaseHistory.ok
            ? _pluginInstallCountsFromReleases((registry && registry.plugins) || [], releaseHistory.releases)
            : {};
        const currentAppVersion = _normalizeVersion(app.getVersion());
        const plugins = [];

        for (const remote of (registry && registry.plugins) || []) {
            if (!remote || !remote.id) continue;
            const local = localPlugins[remote.id] || null;
            const localVersion = _normalizeVersion(local && local.version ? local.version : '0.0.0');
            const remoteVersion = _normalizeVersion(remote.version || remote.remoteVersion || '0.0.0');
            const incompatible = !!(remote.min_app_version && _isNewerVersion(remote.min_app_version, currentAppVersion));
            let status = 'not_installed';
            if (local) {
                status = _isNewerVersion(remoteVersion, localVersion) ? 'update_available' : 'installed';
            }

            const rawKind = String(remote.registry_kind || remote.type || (local && local.type) || '').toLowerCase();
            const registryKind = rawKind.startsWith('r') ? 'r' : 'cli';
            plugins.push({
                ...remote,
                registry_kind: registryKind,
                registry_label: registryKind === 'r' ? 'R' : 'CLI',
                localVersion,
                remoteVersion,
                status,
                installed: !!local,
                incompatible,
                install_count: releaseHistory.ok ? (installCounts[remote.id] || 0) : null,
                install_count_available: !!releaseHistory.ok,
                install_count_source: 'github_release_downloads',
                current_app_version: currentAppVersion
            });
        }

        return {
            ok: true,
            registry_version: registry && registry.registry_version,
            registries: registry && registry.registries,
            cache_purge: cachePurge,
            install_count_meta: {
                available: !!releaseHistory.ok,
                source: 'github_release_downloads',
                stale: !!releaseHistory.stale,
                release_count: releaseHistory.releases.length,
                reason: releaseHistory.ok ? '' : releaseHistory.reason
            },
            plugins
        };
    } catch (e) {
        return { ok: false, reason: e.message || String(e), plugins: [] };
    }
});

app.whenReady().then(async () => {
    // 在 Windows 上设置 AppUserModelID，有助于任务栏图标与通知正确显示
    try { app.setAppUserModelId('com.bioapp.framework'); } catch (e) {}
    try {
        _getAnonymousInstallationId();
        console.log('[PLUGIN-HISTORY] Anonymous installation identity ready');
    } catch (e) {
        console.log('[PLUGIN-HISTORY] Failed to initialize anonymous identity:', e.message || e);
    }
    // Wait for the backend's dynamically assigned port before loading renderer.js.
    // This guarantees that every API URL is built from a ready, bindable endpoint.
    try {
        await createPyProc();
    } catch (error) {
        const message = error && error.message ? error.message : String(error);
        console.error('[PrimiGenius] Backend startup failed:', message);
        dialog.showErrorBox('PrimiGenius backend failed to start', message);
        app.quit();
        return;
    }
    createWindow();
    setupPowerLifecycle();
    // Once the newly installed version has started successfully, remove the
    // installer downloaded by the previous version and its pending-state file.
    setTimeout(() => _cleanupInstalledUpdateArtifacts(), 3000);
    // Podman 异步启动（窗口创建后日志可直接推送到前端）
    ensurePodmanRunning();
    // electron-updater 自动更新
    setupAutoUpdater();

    // 插件远程更新检查（延迟12秒，等后端就绪后再检查）
    setTimeout(() => {
        checkPluginUpdates().catch(e => console.log('[PLUGIN-UPDATE] auto check failed:', e.message));
    }, 2500);

    // ---- 拦截 webview 内所有新窗口弹出（如 B 站、爱奇艺等） ----
    // 对所有 webview 的 guest webContents 设置 setWindowOpenHandler，阻止弹窗
    app.on('web-contents-created', (event, contents) => {
        // 只拦截 webview 类型的 guest webContents
        if (contents.getType() === 'webview') {
            contents.setWindowOpenHandler(({ url }) => {
                // Send URL to renderer to open in a new inner browser tab
                if (url && url !== 'about:blank' && /^https?:\/\//i.test(url)) {
                    if (mainWindow && mainWindow.webContents) {
                        mainWindow.webContents.send('webview-new-tab', url);
                    }
                }
                return { action: 'deny' };
            });
        }
    });
});

app.on('before-quit', cleanupBackendProcess);
app.on('before-quit-for-update', cleanupBackendProcess);
app.on('will-quit', cleanupBackendProcess);

app.on('window-all-closed', () => {
    cleanupBackendProcess();
    app.quit();
});
