"""Real Electron/WSL check, isolated profile, backend, and log archive."""
import json
import argparse
import os
import pathlib
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--offline-r', action='store_true')
    parser.add_argument('--distro', default='primigenius-runtime-test-20261017')
    args = parser.parse_args()
    if not args.distro.startswith('primigenius-runtime-test-'):
        raise SystemExit('Only a disposable test distribution is allowed')
    with tempfile.TemporaryDirectory(dir=ROOT / 'build', prefix='status-log-gui-') as temp:
        work = pathlib.Path(temp)
        server = work / 'server.py'
        server.write_text(f'''import sys
sys.path.insert(0, {str(ROOT / 'backend')!r})
import app as backend
from podman_manager import PodmanManager
manager = PodmanManager({str(work / 'installation')!r})
manager.wsl_distro_name = {args.distro!r}
manager.runtime.bundle = {str(ROOT / 'runtime/release')!r}
backend._podman_mgr = manager
backend._WSL_DISTRO_NAME = manager.wsl_distro_name
backend._LOG_JOURNAL.append('qa-history', 'R', ['重复日志 🧬'] * 3201)
if not manager._start_wsl_podman_service():
    raise RuntimeError(manager.runtime.last_error)
server = backend.make_server('127.0.0.1', 0, backend.app, threaded=True)
print(f'PRIMIGENIUS_API_PORT={{server.server_port}}', flush=True)
server.serve_forever()
''', encoding='utf-8')
        if args.offline_r:
            library = work / 'empty library'
            library.mkdir()
            code = server.read_text(encoding='utf-8')
            code = code.replace("server = backend.make_server", f"backend.R_LIBS_DIR = {str(library)!r}\nbackend._offline_r_environment().ensure()\nserver = backend.make_server")
            server.write_text(code, encoding='utf-8')
        source = (ROOT / 'main.js').read_text(encoding='utf-8')
        source = source.replace("path.join(__dirname, 'backend', 'app.py')", json.dumps(str(server)))
        source = source.replace('    setupAutoUpdater();', '    // QA: updater disabled.')
        source = source.replace('checkPluginUpdates().catch(', 'Promise.resolve().catch(')
        source = source.replace('let mainWindow;', f"app.setPath('userData', {json.dumps(str(work / 'profile'))});\nlet mainWindow;", 1)
        source += r'''
app.whenReady().then(async () => {
    const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
    const report = {};
    try {
        for (let i=0; i<150 && (!mainWindow || mainWindow.webContents.isLoading()); i++) await wait(200);
        report.renderer = await mainWindow.webContents.executeJavaScript(String.raw`(async () => {
            const result = {};
            try {
            const start = performance.now();
            const status = await fetchRRuntimeStatus();
            result.localStatus = status.status;
            result.localStatusMs = performance.now() - start;
            if (!['success','image_missing'].includes(status.status)) throw new Error(JSON.stringify(status));
            const originalFetch = window.fetch;
            let calls = 0;
            window.fetch = async (...args) => {
                if (String(args[0]).includes('/r/check-runtime')) calls++;
                return originalFetch(...args);
            };
            try { await Promise.all(Array.from({length:20}, () => fetchRRuntimeStatus())); }
            finally { window.fetch = originalFetch; }
            result.coalescedRequests = calls;
            if (calls !== 1) throw new Error('R status requests were not coalesced');
            const holder = document.createElement('div');
            const box = document.createElement('div');
            holder.appendChild(box); document.body.appendChild(holder);
            let after = 0, count = 0, more = true;
            while (more) {
                const page = await (await fetch(auxApiUrl('/logs/history?runId=qa-history&afterSeq='+after))).json();
                for (const event of page.events) {
                    appendArchivedInstallEvent(box, event);
                    appendArchivedInstallEvent(box, event); // repeated delivery, not repeated text
                    count++;
                }
                after = page.nextSeq; more = page.has_more;
            }
            result.historyCount = count;
            result.visibleRows = box.children.length;
            if (count !== 3201 || box.children.length !== 300) throw new Error('Log count mismatch');
            const exported = await (await fetch(auxApiUrl('/logs/download?runId=qa-history'))).text();
            result.exportedLines = exported.trimEnd().split('\n').length;
            if (result.exportedLines !== 3201) throw new Error('Export lost repeated records');
            showLogHistory('qa-history');
            let dialog;
            for (let attempt = 0; attempt < 50; attempt++) {
                dialog = document.querySelector('.log-history-dialog[open]');
                if (dialog?.querySelector('pre')?.textContent.includes('重复日志')) break;
                await new Promise(resolve => setTimeout(resolve, 100));
            }
            result.historyDialog = !!dialog && dialog.querySelector('pre').textContent.includes('重复日志');
            if (!result.historyDialog) throw new Error('History dialog did not render');
            dialog.close(); holder.remove();
            // Native EventSource reconnect must begin after the requested cursor.
            result.resumeSeq = await new Promise((resolve, reject) => {
                const es = new EventSource(auxApiUrl('/logs/stream?runId=qa-history&lastSeq='+(after-1)));
                const timer = setTimeout(() => {es.close(); reject(new Error('SSE timeout'));}, 5000);
                es.onmessage = event => {
                    const data = JSON.parse(event.data);
                    if (data.type === 'log') {clearTimeout(timer); es.close(); resolve(data.seq);}
                };
            });
            if (result.resumeSeq !== after) throw new Error('SSE replay cursor mismatch');
            return result;
            } catch (error) { return {...result, error: String(error.stack || error)}; }
        })()`);
        if (report.renderer.error) throw new Error(report.renderer.error);
        report.shutdown = await prepareRuntimeExit();
        if (!report.shutdown) throw new Error('QA shutdown failed');
    } catch (error) { report.error = String(error.stack || error); process.exitCode = 1; }
    finally {
        fs.writeFileSync(path.join(__dirname, 'runtime/validation-status-logs.json'), JSON.stringify(report, null, 2));
        if (report.error) { try { await postBackendLifecycle('/podman/shutdown', 65000); } catch (_) {} }
        runtimeQuitApproved = true;
        cleanupBackendProcess(); app.quit();
    }
});
'''
        entry = ROOT / 'main.status-log-qa.cjs'
        if args.offline_r:
            source = source.replace("if (!['success','image_missing'].includes(status.status))", "if (status.status !== 'success' || !status.ready)")
            source = source.replace('const originalFetch = window.fetch;', '''
            showRImageInstallDialog('');
            await new Promise(resolve => setTimeout(resolve, 300));
            const rDialog = document.querySelector('dialog[open]');
            result.rReadyDialog = !!rDialog && rDialog.querySelector('.retry').hidden;
            if (!result.rReadyDialog) throw new Error('Ready R environment still requests repair');
            result.rDialogStyle = rDialog.classList.contains('image-install-dialog')
                && getComputedStyle(rDialog).padding === '0px'
                && getComputedStyle(rDialog.querySelector('.image-install-header')).display === 'flex';
            result.rRepairHidden = rDialog.querySelector('.r-environment-repair').hidden;
            result.rFixedVersion = rDialog.querySelector('.r-environment-version').textContent;
            if (!result.rDialogStyle || !result.rRepairHidden || !result.rFixedVersion.startsWith('R ')) {
                throw new Error('R environment dialog style or ready content mismatch');
            }
            rDialog.close();
            openDockerImagesTab();
            let rDelete = null;
            for (let attempt = 0; attempt < 60; attempt++) {
                rDelete = document.querySelector('.docker-del-btn[data-r-environment="1"]');
                if (rDelete) break;
                await new Promise(resolve => setTimeout(resolve, 200));
            }
            if (!rDelete) throw new Error('Built-in R image is missing from image management');
            result.rImageName = rDelete.dataset.repo;
            rDelete.click();
            await new Promise(resolve => setTimeout(resolve, 100));
            const confirmation = document.getElementById('_app-inline-confirm');
            result.rDeleteWarning = !!confirmation && /离线恢复|offline recovery/.test(confirmation.textContent);
            if (!result.rDeleteWarning) throw new Error('Missing dedicated R removal warning');
            confirmation.querySelector('._app-confirm-cancel').click();
            result.rDeleteCancelPreserved = (await fetchRRuntimeStatus()).ready;
            if (!result.rDeleteCancelPreserved) throw new Error('Cancel changed R readiness');
            const originalFetch = window.fetch;''')
            source = source.replace('validation-status-logs.json', 'validation-offline-r-gui.json')
        entry.write_text(source, encoding='utf-8')
        env = dict(os.environ, PRIMIGENIUS_LOG_DIR=str(work / 'logs'), PYTHONDONTWRITEBYTECODE='1')
        try:
            subprocess.run([str(ROOT / 'node_modules/electron/dist/electron.exe'), str(entry)],
                           cwd=ROOT, env=env, timeout=120, check=True)
            report = json.loads((ROOT / ('runtime/validation-offline-r-gui.json' if args.offline_r else 'runtime/validation-status-logs.json')).read_text())
            if report.get('error'):
                raise RuntimeError(report['error'])
            print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            entry.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
