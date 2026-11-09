"""Run core regression suites with logs and SQLite handles isolated in a child."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SUITES = ['runtime.test_status_and_logs', 'runtime.test_offline_r', 'runtime.test_installer_data',
          'runtime.test_installer_scope', 'backend.tests.test_r_runtime_contract',
          'backend.tests.test_r_package_installation', 'runtime.test_runtime',
          'runtime.test_core_resources', 'runtime.test_delivery_cache', 'runtime.test_library_compat',
          'backend.tests.test_image_pull_resilience', 'backend.tests.test_wsl_self_recovery',
          'backend.tests.test_runtime_selection']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    began = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='core-regression-', dir=ROOT / 'build') as folder:
        environment = dict(os.environ, PRIMIGENIUS_LOG_DIR=folder, PYTHONIOENCODING='utf-8')
        result = subprocess.run([sys.executable, '-B', '-m', 'unittest', *SUITES, '-q'], cwd=ROOT,
                                capture_output=True, encoding='utf-8', errors='replace', env=environment, timeout=180)
    match = re.search(r'Ran (\d+) tests', result.stderr)
    report = dict(result='PASS' if result.returncode == 0 else 'FAIL', count=int(match[1]) if match else None,
                  seconds=time.perf_counter()-began, suites=SUITES, stdout=result.stdout, stderr=result.stderr)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('result','count','seconds')}), flush=True)
    if result.returncode: print(result.stderr, file=sys.stderr)
    raise SystemExit(result.returncode)


if __name__ == '__main__': main()
