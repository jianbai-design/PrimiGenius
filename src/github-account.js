'use strict';
const documents = new WeakSet();
function createAccountMenu({ account, language, logout, profile }) {
    const doc = document, zh = language() === 'zh';
    const role = ['admin', 'reviewer'].includes(account.role) ? account.role : 'user';
    const label = account.login + ' · ' + ({ admin: zh ? '平台管理员' : 'Administrator', reviewer: zh ? '审核员' : 'Reviewer', user: zh ? '用户' : 'User' })[role];
    const menu = doc.createElement('details'); menu.className = 'github-account-menu';
    const avatar = doc.createElement('summary'); avatar.className = 'github-account-avatar'; avatar.dataset.role = role;
    avatar.title = label; avatar.setAttribute('aria-label', label);
    const image = doc.createElement('img'); image.alt = label;
    image.src = account.id ? `https://avatars.githubusercontent.com/u/${encodeURIComponent(account.id)}?v=4` : account.avatar_url || '';
    avatar.append(image);
    const options = doc.createElement('div'); options.className = 'github-account-options';
    const heading = doc.createElement('div'); heading.className = 'github-account-label'; heading.textContent = label; options.append(heading);
    for (const [caption, action] of [[zh ? '访问主页' : 'Visit profile', profile], [zh ? '退出登录' : 'Sign out', logout]]) {
        const button = doc.createElement('button'); button.type = 'button'; button.textContent = caption;
        button.onclick = async () => { menu.open = false; button.disabled = true; try { await action(); } finally { button.disabled = false; } };
        options.append(button);
    }
    menu.append(avatar, options);
    if (!documents.has(doc)) {
        documents.add(doc);
        doc.addEventListener('click', event => doc.querySelectorAll('.github-account-menu[open]').forEach(item => { if (!item.contains(event.target)) item.open = false; }));
        doc.addEventListener('keydown', event => { if (event.key === 'Escape') doc.querySelectorAll('.github-account-menu[open]').forEach(item => { item.open = false; item.querySelector('summary').focus(); }); });
    }
    return menu;
}
module.exports = { createAccountMenu };
