"""Run the real CLI route with disposable outputs and a simulated container."""
import ast
import datetime
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import uuid

from flask import Flask, jsonify, request

ROOT = Path(__file__).resolve().parents[1]


class RunFailureCompatibilityTests(unittest.TestCase):
    def test_success_failures_and_existing_native_outputs(self):
        tree = ast.parse((ROOT / 'backend/app.py').read_text(encoding='utf-8-sig'))
        route = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_plugin')
        route.decorator_list = []
        app = Flask(__name__)
        for stage in ('success', 'create', 'start', 'wait', 'logs', 'nonzero'):
            for existing in (False, True):
                for placeholder in (False, True):
                    with self.subTest(stage=stage, existing=existing, placeholder=placeholder), tempfile.TemporaryDirectory() as folder:
                        root = Path(folder)
                        output = root / 'result_1.txt'
                        if existing:
                            output.write_bytes(b'previous result')
                        unrelated = root / 'unrelated.dat'
                        unrelated.write_bytes(b'keep this file')
                        command = 'tool --out {output_file}' if placeholder else 'tool --out {output_dir}/native.tsv'
                        cfg = dict(id='fixture', type='linux', docker_image='fixture:v1',
                                   plugin_dir=folder, parameters=[], command_template=command)

                        def start():
                            if not existing or stage == 'success':
                                output.write_bytes(b'new result')
                            if stage == 'start':
                                raise RuntimeError('start failed')

                        container = Mock(id='fixture-container', start=start)
                        create = Mock(side_effect=RuntimeError('create failed') if stage == 'create' else None,
                                      return_value=container)
                        wait = Mock(side_effect=RuntimeError('wait failed') if stage == 'wait' else None,
                                    return_value={'StatusCode': 1 if stage == 'nonzero' else 0})
                        final_logs = Mock(side_effect=RuntimeError('logs failed') if stage == 'logs' else None)
                        scope = dict(os=os, json=json, re=re, uuid=uuid, time=time, datetime=datetime,
                                     traceback=traceback, request=request, jsonify=jsonify,
                                     threading=SimpleNamespace(local=threading.local, Event=threading.Event,
                                                               Thread=lambda **kw: Mock()),
                                     _LOG_CONTEXT=threading.local(), R_DOCKER_IMAGE='other-r-image',
                                     load_all_plugins=lambda: {'fixture': cfg}, get_docker_client=lambda: Mock(),
                                     _image_exists_cache={'fixture:v1': True}, log=lambda line: None,
                                     fallback_tool_version=lambda cfg: 'fixture 1', clean_command=lambda cmd: cmd,
                                     _get_plugin_name=lambda cfg: 'Fixture', format_duration=lambda seconds: '0s',
                                     _build_parameter_log_lines=lambda *args: [],
                                     _create_container_with_image_repair=create, _robust_container_wait=wait,
                                     _push_docker_event=lambda event: None, RUNNING_CONTAINERS={},
                                     _collect_final_logs=final_logs, _schedule_container_cleanup=Mock())
                        exec(compile(ast.Module(body=[route], type_ignores=[]), 'backend/app.py', 'exec'), scope)
                        data = dict(pluginId='fixture', outDir=folder, params={}, threads=2, clientRunId='legacy-run')
                        with app.test_request_context('/run-plugin', method='POST', json=data):
                            result = scope['run_plugin']().get_json()
                        self.assertEqual('success' if stage == 'success' else 'error', result['status'], result)
                        if stage == 'success':
                            self.assertEqual(b'new result', output.read_bytes())
                        elif existing:
                            self.assertEqual(b'previous result', output.read_bytes())
                        else:
                            self.assertEqual(stage != 'create' and not placeholder, output.exists())
                        self.assertEqual(b'keep this file', unrelated.read_bytes())
                        logs = list(root.glob('PrimiGenius.*.log'))
                        self.assertEqual(1, len(logs))
                        if stage != 'success':
                            self.assertIn('1/1', result['message'])
                            self.assertIn('FAILED', logs[0].read_text(encoding='utf-8'))
                        kwargs = create.call_args.args[1]
                        self.assertEqual({'bind': '/workspace', 'mode': 'rw'}, kwargs['volumes'][folder])
                        rendered = kwargs['command'][-1]
                        self.assertIn('/workspace/result_1.txt' if placeholder else '/workspace/native.tsv', rendered)
                        self.assertEqual('2', kwargs['environment']['OMP_NUM_THREADS'])
                        self.assertEqual({}, scope['RUNNING_CONTAINERS'])


if __name__ == '__main__':
    unittest.main()
