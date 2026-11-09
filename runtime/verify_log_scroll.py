"""Exercise the production log viewer in Electron with paginated journal responses."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT / 'src/renderer.js').read_text(encoding='utf-8')
    viewer = source[source.index('function showLogHistory('):
                    source.index('\nfunction appendArchivedInstallEvent(')]
    css = (ROOT / 'src/styles.css').read_text(encoding='utf-8')
    test = r'''
        const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
        const until = async check => {
            for (let i = 0; i < 100; i++) {
                if (check()) return;
                await wait(20);
            }
            throw new Error('Timed out waiting for viewer');
        };
        let failNext = false;
        window.fetch = async url => {
            const after = Number(new URL(url, 'http://localhost').searchParams.get('afterSeq'));
            await wait(10);
            if (failNext) { failNext = false; throw new Error('Injected connection failure'); }
            const last = Math.min(7201, after + 1000);
            return {ok: true, json: async () => ({
                events: Array.from({length: last-after}, (_, i) => ({seq: after+i+1, line: `line ${after+i+1} 重复 🧬`})),
                nextSeq: last, has_more: last < 7201
            })};
        };
        showLogHistory('qa');
        const dialog = document.querySelector('.log-history-dialog');
        const text = dialog.querySelector('pre');
        await until(() => text.children.length === 1 && text.getAttribute('aria-busy') === 'false');
        if (dialog.querySelector('.log-history-navigation')) throw new Error('Page controls remain');
        const checkLines = () => {
            const lines = [...text.children].flatMap(node => node.textContent.split('\n'));
            const nums = lines.map(line => Number(line.match(/^line (\d+)/)[1]));
            if (nums.some((n, i) => i && n !== nums[i-1]+1)) throw new Error('Skipped or duplicated lines');
            if (text.children.length > 3) throw new Error('Unbounded visible chunks');
            return nums;
        };
        for (let i=0; i<7; i++) {
            text.scrollTop = text.scrollHeight;
            text.dispatchEvent(new Event('scroll'));
            await until(() => checkLines().at(-1) === Math.min((i+2)*1000,7201)
                && text.getAttribute('aria-busy') === 'false');
        }
        for (let first = 4001; first >= 1; first -= 1000) {
            text.scrollTop = 0;
            text.dispatchEvent(new Event('scroll'));
            await until(() => checkLines()[0] === first && text.getAttribute('aria-busy') === 'false');
        }
        const before = text.textContent;
        failNext = true;
        text.scrollTop = text.scrollHeight;
        text.dispatchEvent(new Event('scroll'));
        await until(() => !dialog.querySelector('.log-history-retry').hidden);
        if (text.textContent !== before) throw new Error('Failure erased loaded logs');
        dialog.querySelector('.log-history-retry').click();
        await until(() => checkLines().at(-1) === 4000 && text.getAttribute('aria-busy') === 'false');
        if (!dialog.querySelector('a').href.includes('/logs/download?runId=qa')) throw new Error('Export missing');
        dialog.close();
        await until(() => !document.querySelector('.log-history-dialog'));
        return {records: 7201, forwardBackward: true, boundedChunks: 3, retryPreservesLogs: true};
    '''
    with tempfile.TemporaryDirectory(dir=ROOT / 'build', prefix='log-scroll-qa-') as directory:
        work = Path(directory)
        script = work / 'main.cjs'
        injected = "let currentLang='zh'; const auxApiUrl=p=>'http://localhost'+p;\n" + viewer
        script.write_text(f'''
const {{app, BrowserWindow}} = require('electron');
app.setPath('userData', {json.dumps(str(work/'profile'))});
app.whenReady().then(async () => {{
  const window = new BrowserWindow({{show:false,width:1100,height:800}});
  try {{
    await window.loadURL('data:text/html,<html><body></body></html>');
    await window.webContents.insertCSS({json.dumps(css)});
    await window.webContents.executeJavaScript({json.dumps(injected)});
    const result = await window.webContents.executeJavaScript({json.dumps('(async()=>{'+test+'})()')});
    console.log(JSON.stringify(result));
    app.exit(0);
  }} catch (error) {{ console.error(error); app.exit(1); }}
}});
''', encoding='utf-8')
        env = dict(os.environ)
        env.pop('ELECTRON_RUN_AS_NODE', None)
        result = subprocess.run([str(ROOT/'node_modules/electron/dist/electron.exe'), str(script)],
                                capture_output=True, timeout=45, env=env)
        print(result.stdout.decode('utf-8', errors='replace'))
        if result.returncode:
            raise RuntimeError(result.stderr.decode('utf-8', errors='replace'))


if __name__ == '__main__':
    main()
