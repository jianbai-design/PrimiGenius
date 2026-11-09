"""Exercise the real Electron renderer/lifecycle against a disposable runtime.

Only dialog choices are automated. No user's active window/backend is reused.
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORK = ROOT / 'build/runtime-work'


def main():
    server = WORK / 'qa_backend.py'
    entry = ROOT / 'main.runtime-qa.cjs'
    server.write_text('''import pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
import app as backend
from podman_manager import PodmanManager
manager = PodmanManager(str(ROOT / 'build/runtime-work/qa-install'))
manager.wsl_distro_name = 'primigenius-runtime-test-20261017'
manager.runtime.bundle = str(ROOT / 'runtime/release')
backend._podman_mgr = manager
backend._WSL_DISTRO_NAME = manager.wsl_distro_name
if not manager._start_wsl_podman_service():
    raise RuntimeError(manager.runtime.last_error)
server = backend.make_server('127.0.0.1', 0, backend.app, threaded=True)
print(f'PRIMIGENIUS_API_PORT={server.server_port}', flush=True)
server.serve_forever()
''', encoding='utf-8')
    source = (ROOT / 'main.js').read_text(encoding='utf-8')
    source = source.replace("path.join(__dirname, 'backend', 'app.py')", "path.join(__dirname, 'build/runtime-work/qa_backend.py')")
    source = source.replace('    setupAutoUpdater();', '    // Disabled only in the disposable QA harness.')
    source = source.replace('checkPluginUpdates().catch(', 'Promise.resolve().catch(')
    source = source.replace('let mainWindow;', "app.setPath('userData', path.join(__dirname, 'build/runtime-work/qa-userdata'));\nlet mainWindow;", 1)
    source += r'''
app.whenReady().then(async () => {
    const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
    const output = path.join(__dirname, 'runtime');
    const result = {};
    try {
        for (let i = 0; i < 150 && (!mainWindow || mainWindow.webContents.isLoading()); i++) await wait(200);
        if (!mainWindow) throw new Error('No QA window');
        await mainWindow.webContents.executeJavaScript("currentLang='zh'; document.getElementById('menu-docker-images').click()");
        for (let i = 0; i < 100; i++) {
            const text = await mainWindow.webContents.executeJavaScript("document.querySelector('.docker-machine-card')?.innerText || ''");
            if (text.includes('就绪')) { result.card = text; break; }
            await wait(200);
        }
        if (!result.card) throw new Error('Runtime card did not become ready');
        await wait(500);
        fs.writeFileSync(path.join(output, 'validation-gui.png'), (await mainWindow.webContents.capturePage()).toPNG());
        result.openLatency = await mainWindow.webContents.executeJavaScript(`(() => {
            const originalFetch = window.fetch;
            window.fetch = async (...args) => {
                if (String(args[0]).includes('/podman/wsl-status')) await new Promise(r => setTimeout(r, 2500));
                return originalFetch(...args);
            };
            window.restoreQAFetch = () => { window.fetch = originalFetch; };
            const started = performance.now();
            document.querySelector('.docker-machine-card').click();
            return { milliseconds: performance.now() - started,
                immediate: !!document.querySelector('dialog[data-docker-panel-dialog=machine][open]') };
        })()`);
        if (!result.openLatency.immediate) throw new Error('Dialog waited for IPC');
        await wait(3000);
        await mainWindow.webContents.executeJavaScript('window.restoreQAFetch()');
        result.details = await mainWindow.webContents.executeJavaScript("document.querySelector('dialog[data-docker-panel-dialog=machine]')?.innerText || ''");
        if (!result.details.includes('5.8.5') || !result.details.includes('本地连接')) throw new Error('Runtime details missing');
        fs.writeFileSync(path.join(output, 'validation-gui-details.png'), (await mainWindow.webContents.capturePage()).toPNG());
        result.brandingRemoved = !(await mainWindow.webContents.executeJavaScript("document.getElementById('docker-images-view')?.innerText || document.body.innerText")).includes('Podman');
        if (!result.brandingRemoved) throw new Error('Old runtime branding visible');
        result.engineConfigRemoved = await mainWindow.webContents.executeJavaScript("!document.querySelector('.docker-config-card')");
        if (!result.engineConfigRemoved) throw new Error('Engine config card remains');
        await mainWindow.webContents.executeJavaScript("document.querySelector('dialog[data-docker-panel-dialog=machine] .dlg-close-x').click()");
        await wait(5500);
        result.mirrorOpen = await mainWindow.webContents.executeJavaScript(`(() => {
            const originalFetch = window.fetch;
            window.qaMirrorRequests = [];
            window.qaMirrorFail = false;
            window.fetch = async (...args) => {
                if (['/podman/config', '/podman/network-profile', '/podman/custom-mirrors'].some(p => String(args[0]).includes(p))) {
                    window.qaMirrorRequests.push(performance.now());
                    await new Promise(r => setTimeout(r, 800));
                    if (window.qaMirrorFail) throw new Error('QA settings unavailable');
                }
                return originalFetch(...args);
            };
            window.restoreQAMirrorFetch = () => { window.fetch = originalFetch; };
            const started = performance.now();
            const realNow = Date.now;
            Date.now = () => realNow() + 60000;
            document.querySelector('.docker-mirror-card').click();
            Date.now = realNow;
            const dlg = document.querySelector('dialog[data-docker-panel-dialog=mirror]');
            return {milliseconds: performance.now() - started, immediate: !!dlg?.open,
                saveDisabled: dlg?.querySelector('.dlg-apply').disabled};
        })()`);
        if (!result.mirrorOpen.immediate || !result.mirrorOpen.saveDisabled) throw new Error('Mirror opening blocked or editing enabled too early');
        await wait(3000);
        result.mirrorLoaded = await mainWindow.webContents.executeJavaScript(`(() => {
            const times = window.qaMirrorRequests;
            return {enabled: !document.querySelector('dialog[data-docker-panel-dialog=mirror] .dlg-apply').disabled,
                status: document.querySelector('dialog[data-docker-panel-dialog=mirror] [role=status]').innerText, requests: times.length, spread: Math.max(...times) - Math.min(...times)};
        })()`);
        if (!result.mirrorLoaded.enabled || result.mirrorLoaded.requests !== 3 || result.mirrorLoaded.spread > 200) throw new Error('Settings did not load in parallel');
        fs.writeFileSync(path.join(output, 'validation-gui-mirrors.png'), (await mainWindow.webContents.capturePage()).toPNG());
        await mainWindow.webContents.executeJavaScript("document.querySelector('dialog[data-docker-panel-dialog=mirror] .dlg-close-x').click(); window.qaMirrorFail = true");
        await wait(5500);
        await mainWindow.webContents.executeJavaScript("(() => { const realNow = Date.now; Date.now = () => realNow() + 120000; document.querySelector('.docker-mirror-card').click(); Date.now = realNow; })()");
        await wait(3000);
        result.mirrorFailure = await mainWindow.webContents.executeJavaScript(`(() => {
            const dlg = document.querySelector('dialog[data-docker-panel-dialog=mirror]');
            return dlg.querySelector('.dlg-apply').disabled && dlg.innerText.includes('QA settings unavailable');
        })()`);
        if (!result.mirrorFailure) throw new Error('Read failure was hidden');
        await mainWindow.webContents.executeJavaScript("window.qaMirrorFail = false; document.querySelector('dialog[data-docker-panel-dialog=mirror] [role=status] button').click()");
        await wait(3000);
        result.mirrorRetry = await mainWindow.webContents.executeJavaScript("!document.querySelector('dialog[data-docker-panel-dialog=mirror] .dlg-apply').disabled");
        if (!result.mirrorRetry) throw new Error('Retry failed');
        await mainWindow.webContents.executeJavaScript("window.restoreQAMirrorFetch(); document.querySelector('dialog[data-docker-panel-dialog=mirror] .dlg-close-x').click()");
        const backendPid = pyProc.pid;
        await mainWindow.webContents.executeJavaScript('activeWorkflowRuns = 1');
        dialog.showMessageBox = async () => ({ response: 0 });
        result.background = !(await prepareRuntimeExit()) && !mainWindow.isVisible() && pyProc.pid === backendPid;
        restoreRuntimeWindow();
        dialog.showMessageBox = async () => ({ response: 2 });
        result.cancel = !(await prepareRuntimeExit()) && mainWindow.isVisible() && pyProc.pid === backendPid;
        dialog.showMessageBox = async () => ({ response: 1 });
        result.stopAndExit = await prepareRuntimeExit();
        if (!result.background || !result.cancel || !result.stopAndExit) throw new Error('Exit scenario failed');
        fs.writeFileSync(path.join(output, 'validation-gui.json'), JSON.stringify(result, null, 2));
    } catch (error) {
        result.error = String(error.stack || error);
        fs.writeFileSync(path.join(output, 'validation-gui.json'), JSON.stringify(result, null, 2));
        process.exitCode = 1;
        try { await postBackendLifecycle('/podman/shutdown', 65000); } catch (_) {}
    } finally {
        runtimeQuitApproved = true;
        if (runtimeTray) runtimeTray.destroy();
        cleanupBackendProcess();
        app.quit();
    }
});
'''
    entry.write_text(source, encoding='utf-8')
    try:
        subprocess.run([str(ROOT / 'node_modules/electron/dist/electron.exe'), str(entry)],
                       cwd=ROOT, check=True, timeout=150)
    finally:
        entry.unlink(missing_ok=True)
        server.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
