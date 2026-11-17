'use strict';
const { createAccountMenu } = require('./github-account');
const { installState } = require('./market-install-state');
let reviewWatch = null;

const uploadStages = new Map([
    ['本地准备安装包', 'Preparing upload package locally'],
    ['验证 GitHub 账号', 'Verifying GitHub account'],
    ['读取市场审核与权限信息', 'Loading marketplace review and permissions'],
    ['检查个人上传仓库', 'Checking personal upload repository'],
    ['同步可浏览的源码与滚动预览索引', 'Syncing browsable source and scrolling preview index'],
    ['准备 GitHub Release', 'Preparing GitHub Release'],
    ['检查已上传附件', 'Checking uploaded attachments'],
    ['上传 GitHub 插件附件', 'Uploading plugin attachment to GitHub'],
    ['提交市场审核申请', 'Submitting marketplace review request'],
    ['记录作者仓库中的插件信息（审核申请已提交，失败可重试补全）', 'Saving plugin details to the author repository (review submitted; retry if saving fails)']
]);
function uploadProgressText(text, lang) {
    const message = String(text || '');
    if (lang === 'zh') return message;
    const stage = message.replace(/(?:…|\.{3})$/, '');
    const translated = uploadStages.get(stage);
    return translated ? translated + message.slice(stage.length) : message;
}

function canReview(account, request, plugin) {
    return !!account && (account.role === 'admin' || account.role === 'reviewer' && request.author?.id !== account.id &&
        plugin?.owner?.id !== account.id && !(plugin?.collaborators || []).some(person => person.id === account.id));
}
function pendingReviewCount(data) {
    return Object.values(data?.state?.requests || {}).filter(request => request.kind === 'release' && request.status === 'pending_review' &&
        canReview(data.user, request, data.state.plugins?.[request.plugin_id])).length;
}
function watchReviews({ ipc, button, language, openReviews, prepareSession }) {
    reviewWatch?.dispose();
    const badge = button.ownerDocument.createElement('button'); badge.type = 'button'; badge.className = 'market-review-badge'; badge.hidden = true;
    button.append(badge);
    let last = null, timer, busy = false, again = false, disposed = false, version = 0;
    function paint() {
        const count = pendingReviewCount(last), zh = language() === 'zh';
        badge.textContent = count > 99 ? '99+' : String(count); badge.hidden = !count;
        badge.title = zh ? `${count} 个插件待你审核，点击查看` : `${count} plugin${count === 1 ? '' : 's'} awaiting your review; click to view`;
        badge.setAttribute('aria-label', badge.title);
        button.title = count ? badge.title : zh ? '自由市场' : 'Community Market';
    }
    function update(data) { version++; last = data; paint(); }
    async function refresh() {
        if (disposed) return;
        clearTimeout(timer);
        if (busy) { again = true; return; }
        busy = true; const current = version;
        try {
            await prepareSession?.();
            const session = await ipc.invoke('market-session');
            if (disposed || current !== version) return;
            if (!session?.token) { update(null); return; }
            const data = await ipc.invoke('market-list', { force: false });
            if (!disposed && current === version && !data.accountUnavailable) update(data);
        } catch (_) { /* Keep the last verified count through a temporary network failure. */ }
        finally {
            busy = false;
            if (!disposed) {
                if (again) { again = false; queueMicrotask(refresh); }
                else timer = setTimeout(refresh, 60000);
            }
        }
    }
    const click = event => { event.stopPropagation(); openReviews(); };
    const keydown = event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); event.stopPropagation(); openReviews(); } };
    badge.addEventListener('click', click);
    badge.addEventListener('keydown', keydown);
    reviewWatch = { update, refresh, localize: paint, clear: () => update(null),
        dispose() { disposed = true; clearTimeout(timer); badge.removeEventListener('click', click); badge.removeEventListener('keydown', keydown); badge.remove(); } };
    refresh();
    return reviewWatch;
}

function errorMessage(error, lang = 'zh') {
    const message = String(error && error.message || error || '')
        .replace(/^Error invoking remote method '[^']+':\s*(?:Error:\s*)?/, '').replace(/^Error:\s*/, '');
    if (/\b(?:API rate limit exceeded|secondary rate limit|rate limit exceeded)\b/i.test(message)) {
        return lang === 'zh' ? 'GitHub API 请求次数已达上限，请稍后重试，或登录 GitHub 后刷新。' :
            'GitHub API rate limit exceeded. Try again later, or sign in to GitHub and refresh.';
    }
    if (/ETIMEDOUT|ECONNRESET|ECONNREFUSED|ENOTFOUND|EAI_AGAIN|net::ERR_|GitHub 连接(?:失败|超时)/.test(message)) {
        return lang === 'zh' ? '连接 GitHub 失败，请检查网络或系统代理后点击刷新' : 'Cannot connect to GitHub. Check your network or system proxy, then refresh.';
    }
    if (message === '插件列表已加载，但暂时无法验证 GitHub 登录状态，请稍后刷新') {
        return lang === 'zh' ? message : 'The plugin list is loaded, but GitHub sign-in could not be verified. Refresh later.';
    }
    if (lang !== 'zh') for (const [stage, translated] of uploadStages) {
        const prefix = stage + '失败：';
        if (message.startsWith(prefix)) return translated + ' failed: ' + message.slice(prefix.length);
    }
    return message;
}

function open({ ipc, tabs, createTabFrame, switchTab, language, refreshPlugins, githubLogin, githubLogout, prepareSession, onAccount, finishInstallLog }) {
    const id = 'sys-marketplace';
    if (tabs[id]) { switchTab(id); return; }
    const t = (zh, en) => language() === 'zh' ? zh : en;
    createTabFrame(id, t('自由市场', 'Community Market'), root => {
        const model = { view: 'browse', query: '', kind: 'all', data: null, counts: {}, installed: null, installedError: '', loading: false, loadError: '' };
        let installId = '', installPanel = null, installBox = null, installCaption = '';
        const installLines = [];
        const permissionProfiles = new Map();
        const uploadStatusPainters = new Set();
        tabs[id] = { id, type: 'marketplace', renderMarket: () => { render(); for (const paint of uploadStatusPainters) paint(); }, showReviews: () => {
            if (model.data && !['admin', 'reviewer'].includes(model.data.user?.role)) return;
            model.view = 'reviews'; render();
        } };
        tabs[id].onGithubAuthChanged = () => load(true, false);
        root.classList.add('market-page');
        const el = (tag, text, className) => {
            const node = document.createElement(tag);
            if (text !== undefined) node.textContent = text;
            if (className) node.className = className;
            return node;
        };
        const icon = name => {
            const node = el('i', undefined, name);
            node.setAttribute('aria-hidden', 'true');
            return node;
        };
        function emptyState(message) {
            const node = el('div', undefined, 'market-empty');
            node.append(icon('fas fa-box-open'), el('p', message));
            return node;
        }
        const button = (text, action, className = '') => {
            const node = el('button', text, `market-button ${className}`);
            node.type = 'button';
            node.onclick = () => run(async () => {
                node.disabled = true;
                try { await action(); } finally { node.disabled = false; if(node.dataset.marketInstall)updateInstallButtons(); }
            });
            return node;
        };
        let notice = '';
        const noticeText = () => typeof notice === 'function' ? notice() : errorMessage(notice, language());
        const notify = message => {
            notice = message;
            const status = root.querySelector('.market-notice');
            if (status) status.textContent = noticeText();
        };
        async function run(action) {
            try { return await action(); }
            catch (error) { notify(error); }
        }
        const title = value => {
            const candidate = typeof value === 'object' && value ? value[language()] || value.zh || value.en : value;
            return typeof candidate === 'string' ? candidate : '';
        };
        const label = status => ({ awaiting_author: t('待作者处理', 'Awaiting author'), pending_review: t('待平台审核', 'Awaiting platform review'),
            published: t('已发布', 'Published'), rejected: t('已退回', 'Rejected'), withdrawn: t('已撤回', 'Withdrawn'), accepted: t('已加入合作', 'Collaboration accepted'),
            open: t('管理操作', 'Management') }[status] || status);
        function dialogBox(caption) {
            const dialog = el('dialog', undefined, 'market-dialog');
            const header = el('div', undefined, 'market-dialog-header');
            header.append(el('strong', caption), button('×', () => dialog.close(), 'market-close'));
            const body = el('div', undefined, 'market-dialog-body');
            const status = el('div', '', 'market-dialog-status');
            status.setAttribute('role', 'status');
            dialog.append(header, body, status);
            document.body.append(dialog);
            dialog.addEventListener('close', () => dialog.remove(), { once: true });
            dialog.showModal();
            return { dialog, body, status };
        }
        function field(parent, caption, multiline = false) {
            const wrap = el('label', undefined, 'market-field');
            wrap.append(el('span', caption));
            const input = el(multiline ? 'textarea' : 'input');
            if (multiline) input.rows = 4;
            wrap.append(input); parent.append(wrap);
            return input;
        }
        function needUser() {
            if (!model.data || !model.data.user) throw new Error(t('请先登录 GitHub', 'Please sign in to GitHub'));
            return model.data.user;
        }
        function pluginOwner(p) {
            const account = model.data.user;
            return !!account && (account.role === 'admin' || account.id === p?.owner?.id);
        }
        function member(p, uid) {
            return p.owner.id === uid || p.collaborators.some(person => person.id === uid);
        }
        async function command(number, text) {
            const result = await ipc.invoke('market-command', { number, command: '/market ' + text });
            notify(() => t('操作已提交，GitHub Actions 正在验证。请稍后刷新。', 'Submitted. GitHub Actions is verifying the operation; refresh shortly.'));
            return result;
        }
        async function reasonAction(caption, action) {
            const box = dialogBox(caption);
            const reason = field(box.body, t('原因或说明', 'Reason or note'), true);
            box.body.append(button(t('提交', 'Submit'), async () => {
                try { await action(reason.value); box.dialog.close(); }
                catch (error) { box.status.textContent = errorMessage(error, language()); }
            }, 'primary'));
        }
        async function login() {
            if (await githubLogin()) await load(true);
        }
        async function upload(plugin = null) {
            needUser();
            const box = dialogBox(plugin ? t('提交插件更新', 'Submit plugin update') : t('上传新插件', 'Submit new plugin'));
            box.dialog.classList.add('market-upload-dialog');
            let selected = null, uploading = false, latestProgress = null;
            const paintProgress = () => {
                if (latestProgress !== null && box.dialog.open) box.status.textContent = uploadProgressText(latestProgress, language());
            };
            uploadStatusPainters.add(paintProgress);
            box.body.append(el('p', t('可选择插件 ZIP，或包含 config.json、脚本与附带程序的源码目录。文件将公开上传到你自己的 GitHub 仓库。',
                'Select a plugin ZIP or a source folder containing config.json, scripts and bundled programs. Files will be uploaded publicly to your GitHub repository.')));
            const picker = el('div', undefined, 'market-actions');
            const chosen = el('div', t('尚未选择文件', 'No file selected'), 'market-selection');
            async function select(folder) {
                if (uploading) return;
                const next = await ipc.invoke('market-select-source', { folder });
                if (!next) return;
                if (selected) await ipc.invoke('market-discard-source', { id: selected.id });
                selected = next;
                chosen.textContent = `${next.name} · ${(next.size / 1048576).toFixed(2)} MiB\nSHA256: ${next.sha256}`;
            }
            picker.append(button(t('选择 ZIP', 'Choose ZIP'), () => select(false)), button(t('选择源码目录', 'Choose source folder'), () => select(true)));
            box.body.append(picker, chosen);
            if (plugin) box.body.append(el('p', `${title(plugin.metadata.name)} · ${plugin.id}`));
            const versionRow = el('div', undefined, 'market-version-row');
            const version = field(versionRow, t('版本号（必填）', 'Version (required)'));
            version.required = true;
            version.placeholder = '1.0.0';
            version.autocomplete = 'off';
            version.maxLength = 128;
            version.pattern = '(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z]+(?:[.\\-][0-9A-Za-z]+)*)?';
            versionRow.append(el('span', t('填写值将覆盖上传包 config.json 中的版本号，例如 1.0.0。',
                'Overrides the version in the uploaded config.json, e.g. 1.0.0.'), 'market-muted'));
            box.body.append(versionRow);
            const changes = field(box.body, t('更新说明', 'Change notes'), true);
            changes.rows = 3;
            changes.maxLength = 8000;
            box.body.append(el('p', t('插件作者名使用其 GitHub 用户名；已加入合作的用户也会列入作者名单。贡献更新先由主作者接纳，再由平台审核。GitHub 附件上传中断后需要重新上传。',
                'Plugin authors are listed by their GitHub usernames, including accepted collaborators as co-authors. Contributions require acceptance by the lead author and platform review. An interrupted GitHub asset upload must restart.')));
            const submit = button(t('上传并提交审核', 'Upload and submit'), async () => {
                if (!selected) { box.status.textContent = t('请先选择插件', 'Select a plugin first'); return; }
                if (!version.checkValidity()) {
                    box.status.textContent = t('请填写版本号，格式例如 1.0.0 或 1.0.0-beta.1。', 'Enter a version such as 1.0.0 or 1.0.0-beta.1.');
                    version.focus(); return;
                }
                const submittedVersion = version.value;
                uploading = true;
                box.dialog.querySelectorAll('button, input, textarea').forEach(b => { b.disabled = true; });
                box.status.textContent = t('正在上传并提交，请保持窗口打开…', 'Uploading and submitting; keep this window open…');
                try {
                    const result = await ipc.invoke('market-submit', { selection: selected.id, pluginId: plugin ? plugin.id : 'new', changelog: changes.value, version: submittedVersion });
                    selected = null;
                    notify(() => t(`已提交 #${result.issue}，等待 GitHub Actions 检查。`, `Submitted #${result.issue}; awaiting GitHub Actions checks.`));
                    box.dialog.close();
                    model.view = 'inbox'; await load(true);
                } catch (error) { latestProgress = null; box.status.textContent = errorMessage(error, language()); }
                finally { uploading = false; box.dialog.querySelectorAll('button, input, textarea').forEach(b => { b.disabled = false; }); }
            }, 'primary');
            box.body.append(box.status, submit);
            box.dialog.addEventListener('cancel', event => { if (uploading) event.preventDefault(); });
            const progress = (_, update) => { if (selected && update.selection === selected.id) { latestProgress = update.text; paintProgress(); } };
            ipc.on('market-progress', progress);
            box.dialog.addEventListener('close', () => {
                ipc.removeListener('market-progress', progress);
                uploadStatusPainters.delete(paintProgress);
                if (selected && !uploading) ipc.invoke('market-discard-source', { id: selected.id }).catch(() => {});
            }, { once: true });
        }
        async function inspect(r) {
            const box = dialogBox(t('检查提交文件', 'Inspect submission'));
            box.dialog.classList.add('market-inspect-dialog');
            box.dialog.setAttribute('aria-label', t('检查提交文件', 'Inspect submission'));
            box.dialog.querySelector('.market-close').setAttribute('aria-label', t('关闭', 'Close'));
            const files = Object.keys(r.files || {}).sort((a, b) => a.localeCompare(b));
            const changes = Array.isArray(r.changes) ? r.changes : [];
            const changeLabel = value => ({ added: t('新增', 'Added'), changed: t('修改', 'Changed'), removed: t('删除', 'Removed') }[value] || value);
            const changeMap = new Map(changes.map(change => [change.path, change.change]));
            const identity = el('div', undefined, 'market-inspect-identity');
            identity.append(el('strong', title(r.metadata?.name) || r.plugin_id),
                el('span', [r.version && 'v' + r.version, r.author?.login && '@' + r.author.login].filter(Boolean).join(' · '), 'market-muted'));
            const workspace = el('div', undefined, 'market-inspect-workspace');
            const sidebar = el('nav', undefined, 'market-inspect-sidebar');
            sidebar.setAttribute('aria-label', t('提交文件', 'Submission files'));
            const sidebarTitle = el('div', undefined, 'market-inspect-sidebar-title');
            sidebarTitle.append(el('strong', t('提交内容', 'Contents')), el('span', String(files.length), 'market-inspect-count'));
            const overviewButton = el('button', t('提交概览', 'Submission overview'), 'market-inspect-file');
            overviewButton.type = 'button';
            const search = el('input', undefined, 'market-inspect-search');
            search.type = 'search'; search.placeholder = t('搜索文件…', 'Search files…'); search.setAttribute('aria-label', search.placeholder);
            const list = el('div', undefined, 'market-inspect-file-list');
            const empty = el('p', files.length ? t('没有匹配的文件', 'No matching files') : t('没有可预览的文件', 'No files to preview'), 'market-inspect-empty');
            empty.hidden = files.length > 0; search.disabled = !files.length;
            sidebar.append(sidebarTitle, overviewButton, search, list);
            const viewer = el('section', undefined, 'market-inspect-viewer');
            const viewerHeader = el('div', undefined, 'market-inspect-viewer-header');
            const filename = el('strong', '', 'market-inspect-filename');
            const wrapLabel = el('label', undefined, 'market-inspect-wrap');
            const wrap = el('input'); wrap.type = 'checkbox'; wrap.checked = true;
            wrapLabel.append(wrap, document.createTextNode(t('自动换行', 'Wrap lines'))); wrapLabel.hidden = true;
            wrap.onchange = () => { if (selectedFile) showFile(selectedFile.file, selectedFile.entry); };
            const preview = el('div', undefined, 'market-inspect-preview'); preview.tabIndex = 0;
            preview.setAttribute('role', 'region');
            viewerHeader.append(filename, wrapLabel); viewer.append(viewerHeader, preview);
            workspace.append(sidebar, viewer); box.body.append(identity, workspace);
            const entries = [];
            const previewSession = require('crypto').randomBytes(16).toString('hex');
            let disposePreview, selectedFile, serial = 0;
            function stopPreview() { serial++; disposePreview?.(); disposePreview = null; }
            box.dialog.addEventListener('close', () => {
                stopPreview(); ipc.invoke('market-preview-close', { session: previewSession }).catch(() => {});
            }, { once: true });
            function selectEntry(selected) {
                for (const entry of [overviewButton, ...entries]) {
                    const active = entry === selected;
                    entry.classList.toggle('selected', active);
                    if (active) entry.setAttribute('aria-current', 'true'); else entry.removeAttribute('aria-current');
                }
            }
            function showOverview() {
                stopPreview(); selectedFile = null; selectEntry(overviewButton); box.status.textContent = ''; preview.removeAttribute('aria-busy');
                filename.textContent = t('提交概览', 'Submission overview'); filename.title = filename.textContent; wrapLabel.hidden = true;
                preview.setAttribute('aria-label', filename.textContent); preview.replaceChildren(); preview.scrollTop = preview.scrollLeft = 0;
                const overview = el('div', undefined, 'market-inspect-overview');
                const details = el('dl', undefined, 'market-inspect-facts');
                for (const [caption, value] of [[t('插件 ID', 'Plugin ID'), r.plugin_id], [t('版本', 'Version'), r.version],
                    [t('提交作者', 'Submitted by'), r.author?.login], ['SHA-256', r.sha256],
                    ...(r.source?.commit ? [[t('源码提交', 'Source commit'), r.source.commit]] : [])]) {
                    details.append(el('dt', caption), el('dd', value || '—'));
                }
                overview.append(details);
                if (r.changelog) overview.append(el('h4', t('更新说明', 'Release notes')), el('p', r.changelog, 'market-inspect-changelog'));
                overview.append(el('h4', t('文件变更', 'File changes')));
                if (!changes.length) overview.append(el('p', t('没有文件变更记录', 'No file-change records'), 'market-muted'));
                for (const change of changes) {
                    const row = el('div', undefined, 'market-inspect-change');
                    row.append(el('span', changeLabel(change.change), 'market-inspect-badge'), el('code', change.path)); overview.append(row);
                }
                const raw = el('details', undefined, 'market-inspect-raw');
                raw.append(el('summary', t('原始提交信息', 'Original submission details')), el('pre', JSON.stringify({ plugin: r.plugin_id,
                    version: r.version, author: r.author, sha256: r.sha256, changes: r.changes, metadata: r.metadata }, null, 2), 'market-inspect-code'));
                overview.append(raw); preview.append(overview);
            }
            function showFile(file, entry) {
                stopPreview(); const request = serial; selectedFile = { file, entry };
                selectEntry(entry); filename.textContent = file; filename.title = file; wrapLabel.hidden = false;
                preview.setAttribute('aria-label', file); preview.replaceChildren(); preview.scrollTop = preview.scrollLeft = 0;
                const host = el('div', undefined, 'market-inspect-text'); preview.append(host);
                box.status.textContent = r.source_verified ? t('正在在线读取已固定版本的源码…', 'Reading pinned source online…') :
                    t('历史提交未保存在线源码，正在读取原始附件…', 'Legacy submission: reading original attachment…');
                disposePreview = require('./market-source-preview').mountSourcePreview(host, {
                    ipc, number: r.issue, file, lang: language(), wrapLines: wrap.checked,
                    session: previewSession, legacy: !r.source_verified,
                    onStatus(message) { if (request === serial && box.dialog.open) box.status.textContent = message ? errorMessage(new Error(message), language()) : ''; }
                });
            }
            const fileTree = { path: '', directories: new Map(), entries: [] }, folders = [];
            for (const file of files) {
                const parts = file.split('/'); let branch = fileTree;
                for (const name of parts.slice(0, -1)) {
                    if (!branch.directories.has(name)) branch.directories.set(name, {
                        path: branch.path ? branch.path + '/' + name : name, directories: new Map(), entries: []
                    });
                    branch = branch.directories.get(name);
                }
                const entry = el('button', undefined, 'market-inspect-file'); entry.type = 'button'; entry.title = file; entry.dataset.file = file;
                entry.append(el('span', parts[parts.length - 1], 'market-inspect-file-path'));
                if (changeMap.has(file)) entry.append(el('span', changeLabel(changeMap.get(file)), 'market-inspect-badge'));
                entry.onclick = () => showFile(file, entry); entries.push(entry); branch.entries.push(entry);
            }
            function appendBranch(branch, container) {
                for (const [name, folder] of [...branch.directories].sort(([a], [b]) => a.localeCompare(b))) {
                    const details = el('details', undefined, 'market-inspect-folder'); details.open = true; details.dataset.directory = folder.path;
                    const summary = el('summary', name, 'market-inspect-folder-heading'); summary.title = folder.path;
                    const children = el('div', undefined, 'market-inspect-folder-children'); details.append(summary, children); container.append(details);
                    folder.element = details; folders.push(folder); appendBranch(folder, children);
                }
                container.append(...branch.entries);
            }
            appendBranch(fileTree, list);
            list.append(empty);
            let searchExpansion;
            function filterBranch(branch, query) {
                let matches = 0;
                for (const entry of branch.entries) {
                    entry.hidden = !entry.dataset.file.toLowerCase().includes(query); if (!entry.hidden) matches++;
                }
                for (const folder of branch.directories.values()) {
                    const count = filterBranch(folder, query); folder.element.hidden = count === 0;
                    if (query && count) folder.element.open = true;
                    matches += count;
                }
                return matches;
            }
            search.oninput = () => {
                const query = search.value.trim().toLowerCase();
                if (query && !searchExpansion) searchExpansion = new Map(folders.map(folder => [folder.element, folder.element.open]));
                empty.hidden = filterBranch(fileTree, query) > 0;
                if (!query && searchExpansion) {
                    for (const [folder, open] of searchExpansion) folder.open = open;
                    searchExpansion = null;
                }
            };
            overviewButton.onclick = showOverview; showOverview();
        }
        function requestCard(r, key) {
            const card = el('article', undefined, 'market-card');
            const p = model.data.state.plugins[r.plugin_id];
            card.append(el('strong', `#${key} · ${r.plugin_id || t('市场管理', 'Market management')} ${r.version || ''}`),
                el('p', `${r.author.login} · ${label(r.status)}`));
            if (r.changelog) card.append(el('p', r.changelog));
            if (r.message) card.append(el('p', r.message));
            if (r.reason) card.append(el('p', r.reason, 'market-muted'));
            const actions = el('div', undefined, 'market-actions');
            const number = Number(key);
            actions.append(button(t('GitHub 记录', 'GitHub record'), () => ipc.invoke('market-open', { url: `https://github.com/${model.data.repository}/issues/${number}` })));
            if (r.kind === 'release') actions.append(button(t('文件与变更', 'Files and changes'), () => inspect({ ...r, issue: number })));
            const me = model.data.user;
            if (p && pluginOwner(p) && r.status === 'awaiting_author') {
                const prefix = r.kind === 'collaboration' ? 'collaborate-' : 'author-';
                const hash = r.sha256 ? ' ' + r.sha256 : '';
                actions.append(button(t('接纳', 'Accept'), () => command(number, prefix + 'accept' + hash), 'primary'),
                    button(t('退回', 'Reject'), () => reasonAction(t('退回申请', 'Reject request'), reason => command(number, prefix + 'reject' + hash + ' ' + reason))));
            }
            if (r.status === 'pending_review' && me && ['admin', 'reviewer'].includes(me.role)) {
                if (canReview(me, r, p)) actions.append(button(t('审核通过并发布', 'Approve and publish'), () => command(number, 'approve ' + r.sha256), 'primary'),
                    button(t('退回', 'Reject'), () => reasonAction(t('填写退回原因', 'Reason for rejection'), reason => command(number, 'reject ' + r.sha256 + ' ' + reason))));
                else card.append(el('p', t('需由其他审核员审核此插件', 'Another reviewer must review this plugin'), 'market-muted'));
            }
            if (r.kind === 'release' && ['awaiting_author', 'pending_review'].includes(r.status) &&
                me && (r.author.id === me.id || pluginOwner(p))) actions.append(button(t('撤回', 'Withdraw'), () => command(number, 'withdraw ' + r.sha256)));
            card.append(actions); return card;
        }
        function pluginInstallState(plugin){
            if(!model.installed)return 'checking';
            return installState(model.installed[plugin.id],{version:plugin.published.version,source:'github:'+model.data.repository,owner_github_id:plugin.owner.id});
        }
        async function loadInstalled(){
            try{
                const result=await ipc.invoke('market-installed');
                if(!result?.ok||!result.plugins)throw new Error(t('无法读取本地安装状态，请刷新后重试','Cannot read installed plugins; refresh to retry'));
                model.installed=result.plugins;model.installedError='';
            }catch(error){model.installed=null;model.installedError=errorMessage(error,language());}
            updateInstallButtons();
        }
        function paintInstallButton(node,plugin){
                const state=pluginInstallState(plugin);
                node.disabled=!!installId||!['install','update'].includes(state);
                node.textContent=node.dataset.marketInstall===installId?installCaption:({install:t('安装','Install'),update:t('更新','Update'),installed:t('已安装','Installed'),newer:t('本地版本较新','Newer version installed'),conflict:t('来源冲突','Source conflict'),unknown:t('本地版本未知','Unknown installed version'),checking:model.installedError?t('检查失败，请刷新','Check failed; refresh'):t('正在检查…','Checking…')})[state];
                const local=model.installed?.[plugin.id];
                node.title=model.installedError||(local?t('本地版本：','Installed version: ')+String(local.version||'—'):'');
        }
        function updateInstallButtons() {
            root.querySelectorAll('[data-market-install]').forEach(node => {
                const plugin=model.data?.state.plugins[node.dataset.marketInstall];if(plugin)paintInstallButton(node,plugin);
            });
        }
        async function installPlugin(plugin) {
            if (installId) return;
            if(!['install','update'].includes(pluginInstallState(plugin)))return;
            installId = plugin.id;
            installCaption = t('正在验证…', 'Verifying…');
            installLines.length = 0;
            if (installPanel) installPanel.remove();
            installPanel = el('div', undefined, 'local-log-container plugin-store-install-log market-install-log');
            installPanel.dataset.autoCloseLog = 'true';
            const header = el('div', undefined, 'local-log-header plugin-store-install-log-header');
            header.append(el('span', t('安装日志', 'Installation Log') + ' · ' + (title(plugin.metadata?.name) || plugin.id)),
                button('×', () => { installPanel.style.display = 'none'; }, 'plugin-store-install-log-close'));
            installBox = el('div', undefined, 'local-log-box');
            installPanel.append(header, installBox); root.append(installPanel);
            const append = text => {
                if (!text) return;
                installLines.push(String(text));
                installBox.append(el('div', String(text)));
                while (installBox.children.length > 400) installBox.firstChild.remove();
                installBox.scrollTop = installBox.scrollHeight;
            };
            let lastPercent = -1;
            const progress = (_, data) => {
                if (!data || data.pluginId !== installId) return;
                if (data.phase === 'download') {
                    const percent = Number(data.percent);
                    installCaption = Number.isFinite(percent) ? t('下载 ', 'Downloading ') + Math.max(0, Math.min(100, Math.round(percent))) + '%' : t('正在下载…', 'Downloading…');
                    if (Number.isFinite(percent) && Math.floor(percent) !== lastPercent) {
                        lastPercent = Math.floor(percent); append(installCaption);
                    }
                } else if (data.phase !== 'log') {
                    installCaption = ({verify:t('正在验证…','Verifying…'), install:t('正在安装…','Installing…'),
                        done:t('安装完成','Installed'), failed:t('安装失败','Installation failed')})[data.phase] || installCaption;
                }
                append(data.text); updateInstallButtons();
            };
            ipc.on('market-install-progress', progress); updateInstallButtons();
            try {
                const result = await ipc.invoke('market-install', { pluginId: plugin.id });
                if (!result || !result.ok) throw new Error(result?.reason || t('安装失败','Installation failed'));
                await refreshPlugins();
                await loadInstalled();
                notify(() => result.skipped?t('已安装相同或更新版本，已跳过重复安装','Same or newer version already installed; skipped'):t('安装成功', 'Installed successfully'));
                installPanel.remove(); installPanel = null;
            } catch (error) {
                const message = errorMessage(error, language()); append(message);
                if (finishInstallLog) finishInstallLog(installPanel, {state:'failed',message,log:[...installLines]}, installBox);
                throw error;
            } finally {
                ipc.removeListener('market-install-progress', progress);
                installId = ''; updateInstallButtons();
            }
        }
        function updateDownloadCounts(nodes = root.querySelectorAll('.market-download-count')) {
            nodes.forEach(node => {
                const value = model.counts[node.dataset.pluginId];
                const count = Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString(language() === 'zh' ? 'zh-CN' : 'en-US') : '—';
                node.replaceChildren(icon('fas fa-download'), document.createTextNode(t('下载数：', 'Downloads: ') + count));
                node.title = t('GitHub Release 各版本安装包累计下载数', 'Total package downloads across GitHub Releases');
            });
        }
        function pluginCard(p) {
            const card = el('article', undefined, 'market-card plugin-store-card market-plugin-card');
            const metadata = p.metadata || {};
            const isR = String(metadata.type || '').toLowerCase().startsWith('r');
            const top = el('div', undefined, 'market-plugin-top');
            const badge = el('div', undefined, `plugin-store-card-icon${isR ? ' r' : ''}`);
            badge.append(icon(isR ? 'fab fa-r-project' : 'fas fa-terminal'));
            const main = el('div', undefined, 'plugin-store-card-main');
            const name = el('h3', title(metadata.name) || p.id, 'plugin-store-card-title');
            name.title = name.textContent;
            const meta = el('div', undefined, 'plugin-store-card-meta');
            meta.append(el('span', isR ? 'R' : 'CLI', `plugin-store-kind${isR ? ' r' : ''}`),
                el('span', p.published ? `v${p.published.version}` : t('尚未发布', 'Unpublished')),
                el('span', p.listed ? t('社区来源', 'Community source') : t('已下架', 'Unlisted')));
            main.append(name, meta); top.append(badge, main);
            const author = el('div', undefined, 'plugin-store-card-foot market-author-list');
            const authors = el('span', t('作者：', 'Authors: ') + [p.owner, ...p.collaborators].map(person => person.login).join(t('、', ', ')));
            authors.title = authors.textContent; authors.prepend(icon('fab fa-github'));
            const downloads = el('span', undefined, 'market-download-count');
            downloads.dataset.pluginId = p.id;
            updateDownloadCounts([downloads]);
            author.append(authors, downloads);
            card.append(top, el('div', title(metadata.description), 'plugin-store-card-desc'), author);
            const actions = el('div', undefined, 'market-actions');
            if (p.published && p.listed) {
                const install = button('', () => installPlugin(p), 'primary');
                install.dataset.marketInstall = p.id; paintInstallButton(install,p);
                actions.append(install);
            }
            if (model.data.user) {
                actions.append(button(t('提交更新', 'Contribute update'), () => upload(p)));
                if (!member(p, model.data.user.id)) actions.append(button(t('申请合作', 'Request collaboration'), () => reasonAction(t('申请合作', 'Request collaboration'), async message => {
                    await ipc.invoke('market-collaborate', { pluginId: p.id, message }); notify(() => t('合作申请已提交', 'Collaboration request submitted'));
                })));
                if (pluginOwner(p)) actions.append(button(t('管理', 'Manage'), () => manage(p)));
                else if (model.data.user.role === 'reviewer') actions.append(button(t('下架', 'Unlist'), () => reasonAction(t('下架说明', 'Unlisting reason'), reason => command(null, `unlist ${p.id} ${reason}`))));
            }
            card.append(actions); return card;
        }
        function manage(p) {
            const box = dialogBox(title(p.metadata.name) || p.id);
            box.body.append(el('p', `${t('主作者', 'Lead author')}: ${p.owner.login} (${p.owner.id})`));
            for (const person of p.collaborators) {
                const row = el('div', undefined, 'market-actions');
                row.append(el('span', `${t('共同作者', 'Co-author')}: ${person.login} (${person.id})`), button(t('移除合作权限', 'Remove collaboration'), () => command(null, `collaborator-remove ${p.id} ${person.id}`)));
                box.body.append(row);
            }
            box.body.append(button(t('下架插件', 'Unlist plugin'), () => reasonAction(t('下架说明', 'Unlisting reason'), async reason => {
                await command(null, `unlist ${p.id} ${reason}`);
                box.dialog.close();
            })));
            if (model.data.user.role === 'admin' && !p.listed) box.body.append(button(t('恢复上架', 'Restore listing'), () => command(null, `restore ${p.id}`)));
        }
        function permissionAccountList(ids, blocked = false) {
            const list = el('div', undefined, 'market-permission-list');
            if (!ids.length) { list.append(el('p', t('暂无账号', 'No accounts'), 'market-muted')); return list; }
            ids.forEach(id => {
                const card = el('article', undefined, 'market-permission-card');
                const avatar = el('img', undefined, 'market-permission-avatar'); avatar.alt = '';
                const details = el('div', undefined, 'market-permission-details');
                const name = el('strong', t('GitHub 账号', 'GitHub account') + ' #' + id);
                const account = el('div', 'ID: ' + id, 'market-muted');
                const unavailable = blocked || model.data.state.blocked.includes(id);
                const status = el('span', unavailable ? t('已禁用', 'Blocked') : t('审核员 · 已生效', 'Reviewer · Active'), 'community-label');
                details.append(name, account, status);
                const extra = el('div', t('正在读取账号资料…', 'Loading profile…'), 'market-permission-extra');
                const actions = el('div', undefined, 'market-actions');
                details.append(extra, actions); card.append(avatar, details); list.append(card);
                const load = () => {
                    if (!permissionProfiles.has(id)) {
                        const request = ipc.invoke('market-resolve-user', { value: String(id) }).catch(error => { permissionProfiles.delete(id); throw error; });
                        permissionProfiles.set(id, request);
                    }
                    permissionProfiles.get(id).then(person => {
                        if (!card.isConnected) return;
                        name.textContent = person.name ? person.name + ' · @' + person.login : '@' + person.login;
                        avatar.src = person.avatar_url;
                        extra.replaceChildren();
                        const info = [person.company, person.location].filter(Boolean).join(' · ');
                        if (info) extra.append(el('div', info));
                        if (person.bio) extra.append(el('div', person.bio));
                        actions.replaceChildren(button(t('GitHub 主页', 'GitHub profile'), () => ipc.invoke('market-open', { url: 'https://github.com/' + encodeURIComponent(person.login) })));
                    }).catch(() => {
                        if (!card.isConnected) return;
                        extra.textContent = t('账号资料暂不可用，ID 与权限状态仍有效。', 'Profile unavailable; ID and permission status are retained.');
                        actions.replaceChildren(button(t('重试', 'Retry'), load));
                    });
                };
                load();
            });
            return list;
        }
        function adminPanel(parent) {
            parent.append(el('p', t('平台管理员拥有最高管理权限；审核员只有审核与下架权限，不获得仓库写权限。',
                'The administrator controls platform permissions. Reviewers can review and unlist plugins without repository write access.')));
            parent.append(el('p', t('审核员由管理员直接授权，无需对方接受邀请。权限变更经 GitHub Actions 处理生效后，对方刷新自由市场即可使用。',
                'Reviewers are appointed directly; no invitation acceptance is required. Once GitHub Actions applies the change, they can refresh the market to use their permissions.'), 'market-muted'));
            const form = el('div', undefined, 'market-admin-grid');
            const target = field(form, t('GitHub 用户名或数字 ID', 'GitHub username or numeric ID'));
            const action = el('select');
            for (const [value, caption] of [['reviewer-add', t('添加审核员', 'Add reviewer')], ['reviewer-remove', t('移除审核员', 'Remove reviewer')],
                ['ban', t('禁用账号', 'Ban account')], ['unban', t('解除禁用', 'Unban account')]]) {
                const option = el('option', caption); option.value = value; action.append(option);
            }
            form.append(action, button(t('提交权限变更', 'Submit permission change'), async () => {
                const person = await ipc.invoke('market-resolve-user', { value: target.value.trim() });
                await command(null, `${action.value} ${person.id}`);
            }, 'primary'));
            parent.append(form,
                el('h3', t('审核员', 'Reviewers') + ' (' + model.data.state.reviewers.length + ')'), permissionAccountList(model.data.state.reviewers),
                el('h3', t('禁用账号', 'Blocked accounts') + ' (' + model.data.state.blocked.length + ')'), permissionAccountList(model.data.state.blocked, true));
        }
        function render() {
            if (!root.isConnected) return;
            root.replaceChildren();
            if (installPanel) root.append(installPanel);
            const header = el('div', undefined, 'tool-header plugin-store-header market-header');
            const heading = el('div', undefined, 'plugin-store-heading');
            const caption = el('div', undefined, 'tool-title plugin-store-title-el');
            caption.append(icon('fas fa-box-open'), document.createTextNode(' ' + t('自由市场', 'Community Market')));
            const count = el('div', undefined, 'plugin-store-count-card');
            const published = model.data && Object.values(model.data.state.plugins).filter(p => p.listed && p.published && !model.data.state.blocked.includes(p.owner.id)).length;
            count.append(icon('fas fa-puzzle-piece'), el('span', model.data ? String(published) : '—', 'plugin-store-count-value'),
                el('span', t('个插件', 'plugins')));
            count.title = t('市场当前公开上架的插件数量', 'Plugins currently published in the market');
            heading.append(caption, count); header.append(heading);
            const account = model.data && model.data.user;
            const pending = pendingReviewCount(model.data);
            const toolbar = el('div', undefined, 'plugin-store-toolbar market-toolbar');
            const browsing = ['browse', 'mine'].includes(model.view);
            let fill = () => {};
            const searchWrap = el('div', undefined, 'plugin-store-search-wrap');
            const search = el('input', undefined, 'plugin-store-search');
            search.type = 'search'; search.placeholder = t('搜索插件、作者、合作者…', 'Search plugins, authors or collaborators…');
            search.setAttribute('aria-label', search.placeholder); search.value = model.query;
            search.oninput = () => { model.query = search.value; fill(); };
            searchWrap.append(icon('fas fa-search'), search);
            const types = el('div', undefined, 'plugin-store-segments');
            types.setAttribute('aria-label', t('插件类型', 'Plugin type'));
            for (const [value, text] of [['all', t('全部', 'All')], ['cli', t('CLI 插件', 'CLI Plugins')], ['r', t('R 插件', 'R Plugins')]]) {
                const filter = button(text, () => {
                    model.kind = value;
                    for (const child of types.children) {
                        const active = child === filter; child.classList.toggle('active', active); child.setAttribute('aria-pressed', String(active));
                    }
                    fill();
                });
                filter.className = 'plugin-store-filter' + (model.kind === value ? ' active' : '');
                filter.setAttribute('aria-pressed', String(model.kind === value)); types.append(filter);
            }
            if (browsing) toolbar.append(types, searchWrap);
            const refresh = button('', () => load(true));
            refresh.className = 'plugin-store-refresh'; refresh.title = t('刷新', 'Refresh');
            refresh.setAttribute('aria-label', refresh.title); refresh.append(icon('fas fa-sync-alt'));
            refresh.disabled = model.loading;
            const controls = el('div', undefined, 'market-actions market-account-actions');
            if (account) {
                controls.append(createAccountMenu({ account, language,
                    profile: () => run(() => ipc.invoke('market-open', { url: 'https://github.com/' + encodeURIComponent(account.login) })),
                    logout: () => run(async () => { await (githubLogout ? githubLogout() : ipc.invoke('market-logout')); model.view = 'browse'; await load(true); }) }));
            } else if (model.data && model.data.accountUnavailable) {
                controls.append(el('span', t('GitHub 账号暂时无法验证', 'GitHub account verification unavailable'), 'market-account'),
                    button(t('退出登录', 'Sign out'), async () => { await (githubLogout ? githubLogout() : ipc.invoke('market-logout')); await load(true); }));
            } else {
                const signIn = button(t('登录', 'Sign in'), login, 'github-login-button');
                signIn.prepend(icon('fab fa-github'));
                controls.append(signIn);
            }
            const nav = el('select', undefined, 'market-view-select');
            nav.setAttribute('aria-label', t('市场管理', 'Market navigation'));
            const views = [['browse', t('浏览插件', 'Browse')], ['mine', t('我的插件', 'My plugins')], ['inbox', t('合作与更新', 'Contributions')]];
            if (account && ['reviewer', 'admin'].includes(account.role)) views.push(['reviews', t('审核管理', 'Review queue') + (pending ? ` (${pending})` : '')]);
            if (account && account.role === 'admin') views.push(['admin', t('权限管理', 'Permissions')], ['audit', t('操作记录', 'Audit trail')]);
            for (const [view, caption] of views) {
                const option = el('option', caption); option.value = view; nav.append(option);
            }
            nav.value = model.view;
            nav.onchange = () => run(async () => {
                try { if (nav.value !== 'browse') needUser(); model.view = nav.value; render(); root.querySelector('.market-view-select').focus(); }
                catch (error) { nav.value = model.view; throw error; }
            });
            if (account) controls.append(button(t('上传插件', 'Upload plugin'), () => upload(), 'primary'));
            toolbar.prepend(nav); toolbar.append(controls, refresh); header.append(toolbar); root.append(header);
            const content = el('div', undefined, 'market-content'); root.append(content);
            const status = el('div', noticeText() || (model.loading ? t('正在连接 GitHub…', 'Connecting to GitHub…') : ''), 'market-notice');
            status.setAttribute('role', model.loadError ? 'alert' : 'status'); content.append(status);
            if (!model.data) {
                content.append(emptyState(model.loadError ? t('插件列表加载失败，请稍后点击刷新重试。', 'The plugin list could not be loaded. Refresh to retry.') :
                    t('正在从 GitHub 读取插件列表…', 'Loading the plugin list from GitHub…')));
                return;
            }
            if (pending) {
                const caption = t(`${pending} 个插件待你审核`, `${pending} plugin${pending === 1 ? '' : 's'} awaiting your review`);
                const prompt = button(caption + (model.view === 'reviews' ? '' : t(' · 前往审核 →', ' · Review now →')),
                    () => tabs[id].showReviews(), 'market-review-prompt');
                content.append(prompt);
            }
            const state = model.data.state;
            if (model.view === 'admin') {
                const panel = el('section', undefined, 'market-card market-admin-panel');
                panel.append(el('h3', t('权限管理', 'Permissions'))); adminPanel(panel); content.append(panel); return;
            }
            if (model.view === 'audit') {
                const table = el('table', undefined, 'market-table');
                const tableHead = el('thead'), titles = el('tr');
                for (const text of [t('时间', 'Time'), t('操作人', 'User'), t('操作', 'Action'), t('记录', 'Record')]) titles.append(el('th', text));
                tableHead.append(titles); table.append(tableHead);
                for (const item of [...state.audit].reverse()) {
                    const row = el('tr'); for (const value of [item.at, item.actor.login, item.action, '#' + item.issue]) row.append(el('td', value)); table.append(row);
                }
                content.append(state.audit.length ? table : emptyState(t('暂无操作记录', 'No activity yet'))); return;
            }
            const list = el('div', undefined, 'market-list');
            if (['inbox', 'reviews'].includes(model.view)) {
                for (const [key, r] of Object.entries(state.requests).reverse()) {
                    if (model.view === 'reviews' && r.status !== 'pending_review') continue;
                    if (model.view === 'inbox' && r.author.id !== account.id && !(state.plugins[r.plugin_id] && member(state.plugins[r.plugin_id], account.id))) continue;
                    if (r.kind !== 'management') list.append(requestCard(r, key));
                }
            } else {
                fill = () => {
                    list.replaceChildren();
                    const query = model.query.trim().toLowerCase();
                    for (const p of Object.values(state.plugins)) {
                        if (model.view === 'mine' ? !member(p, account.id) : (!p.listed || !p.published || state.blocked.includes(p.owner.id))) continue;
                        const type = String(p.metadata.type || '').toLowerCase().startsWith('r') ? 'r' : 'cli';
                        if (model.kind !== 'all' && model.kind !== type) continue;
                        const text = `${p.id} ${title(p.metadata.name)} ${title(p.metadata.description)} ${p.owner.login} ${p.collaborators.map(x => x.login).join(' ')}`.toLowerCase();
                        if (query && !text.includes(query)) continue;
                        list.append(pluginCard(p));
                    }
                    if (!list.children.length) list.append(emptyState(model.loadError ?
                        t('本次刷新失败，尚未获取最新插件列表', 'Refresh failed; the latest plugin list is unavailable') :
                        t('暂无匹配的插件', 'No matching plugins')));
                };
                fill();
            }
            if (!list.children.length) list.append(emptyState(model.loadError ?
                t('本次刷新失败，尚未获取最新申请', 'Refresh failed; the latest requests are unavailable') :
                t('暂无申请或待审核更新', 'No requests or pending updates')));
            content.append(list);
        }
        async function load(force = false, refreshCounts = force) {
            if (model.loading) return;
            model.loading = true;
            loadInstalled();
            if (!model.data) render();
            try {
                if (prepareSession) await prepareSession();
                const data = await ipc.invoke('market-list', { force });
                if (data.accountUnavailable && model.data && model.data.user) data.user = model.data.user;
                model.data = data;
                if (data.user && onAccount) onAccount(data.user);
                if (!data.user || model.view === 'reviews' && !['admin', 'reviewer'].includes(data.user.role)) model.view = 'browse';
                reviewWatch?.update(data);
                model.loadError = '';
                notice = data.warning || '';
                ipc.invoke('market-download-counts', { force: refreshCounts }).then(result => {
                    if (!result || !result.ok || model.data !== data || !root.isConnected) return;
                    model.counts = result.counts || {};
                    updateDownloadCounts();
                }).catch(() => {});
            } catch (error) {
                model.loadError = error;
                notice = () => errorMessage(error, language()) +
                    (model.data ? t('；正在显示上次成功读取的数据', '; showing the last successfully loaded data') : '');
            }
            finally { model.loading = false; render(); updateDownloadCounts(); updateInstallButtons(); }
        }
        render(); load();
        const poll = async () => {
            if (!root.isConnected) return;
            const editing = root.contains(document.activeElement) && ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName);
            if (model.data && model.data.user && !installId && !editing && !document.querySelector('.market-dialog[open]')) await load(true, false);
            setTimeout(poll, 15000);
        };
        setTimeout(poll, 15000);
    });
}

module.exports = { open, errorMessage, watchReviews, pendingReviewCount };
