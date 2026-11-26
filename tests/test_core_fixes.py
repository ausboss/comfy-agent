"""Exercise the runner through the CLI and MCP with an in-memory ComfyUI."""
import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

import test_comfy_agent as fixtures

ca = fixtures.ca
real_download = ca.download


class FakeComfy:
    def __init__(self):
        self.nodes = copy.deepcopy(fixtures.OBJECT_INFO)
        self.queue = {}
        self.history = {}
        self.submitted = []
        self.uploads = []
        self.on_submit = None

    def __call__(self, path, body=None, **kw):
        if path.startswith('/object_info/'):
            name = path.split('/')[-1]
            return {name: self.nodes[name]} if name in self.nodes else {}
        if path == '/system_stats':
            return {}
        if path == '/models':
            return []
        if path == '/upload/image':
            self.uploads.append(body if isinstance(body, bytes) else b"".join(body))
            return {'subfolder': 'inputs', 'name': 'upload.dat'}
        if path == '/prompt':
            self.submitted.append(body)
            pid = str(len(self.submitted))  # Also exercise servers that assign their own IDs.
            self.queue[pid] = [0, pid, body['prompt'], {'client_id': body['client_id']}, []]
            if self.on_submit:
                return self.on_submit(pid)
            return {'prompt_id': pid}
        if path == '/queue':
            return {'queue_running': [], 'queue_pending': list(self.queue.values())}
        if path == '/history':
            return self.history
        if path.startswith('/history/'):
            pid = path.rsplit('/', 1)[-1]
            return {pid: self.history[pid]} if pid in self.history else {}
        if path.startswith('/view?') and kw.get('raw'):
            return fixtures.PNG
        raise AssertionError(path)

    def finish(self, pid, names=('a.png',), **status):
        self.history[pid] = {'prompt': self.queue.pop(pid), 'status': {'status_str': 'success', **status},
                             'outputs': {'9': {'images': [{'filename': name, 'type': 'output'} for name in names]}}}

    def download(self, url, dest):
        name = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)['filename'][0]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(name.encode())


class RunnerTest(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        folder = self.root / 'recipes' / 'test'
        folder.mkdir(parents=True)
        (folder / 'card.json').write_text(json.dumps(fixtures.CARD))
        (folder / 'prompt.json').write_text(json.dumps(fixtures.PROMPT))
        self.recipe = ca.Recipe(folder)
        self.fake = FakeComfy()
        stack.enter_context(mock.patch.dict(os.environ, COMFY_AGENT_MCP_WAIT="0"))
        self.out = self.root / 'out'
        stack.enter_context(mock.patch.dict(ca.S, url='http://localhost', out=str(self.out), send={}, recipes=[str(folder.parent)], unload_llm=None))
        stack.enter_context(mock.patch.dict(ca._node_info, clear=True))
        stack.enter_context(mock.patch.object(ca, 'comfy', side_effect=self.fake))
        stack.enter_context(mock.patch.object(ca, 'download', side_effect=self.fake.download))
        self.stdout = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stderr = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def queue(self, *pairs, count=1, sets=()):
        return ca.queue_jobs(self.recipe, [('prompt', 'fox'), *pairs], count, sets)

    def call(self, name, args):
        return ca.mcp_handle({'method': 'tools/call', 'id': 1, 'params': {'name': name, 'arguments': args}})['result']

    def test_accepted_jobs_recover_after_client_dies_during_submit(self):
        snapshot = self.root / 'fake-server.json'
        script = """
import json, os, sys
from pathlib import Path
from unittest import mock
from test_core_fixes import FakeComfy, ca
folder, output, snapshot = sys.argv[1:]
fake = FakeComfy()
def accept(pid):
    if pid == '1':
        fake.finish(pid)
        return {'prompt_id': pid}
    Path(snapshot).write_text(json.dumps([fake.history, fake.queue, fake.submitted]))
    os._exit(23)
fake.on_submit = accept
with mock.patch.dict(ca.S, out=output, unload_llm=None), mock.patch.object(ca, 'comfy', fake):
    ca.queue_jobs(ca.Recipe(folder), [('prompt', 'fox')], count=2)
"""
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent))
        child = subprocess.run([sys.executable, '-c', script, str(self.recipe.folder), str(self.out), str(snapshot)],
                               env=env, capture_output=True, timeout=10)
        self.assertEqual(child.returncode, 23, child.stderr)
        self.fake.history, self.fake.queue, self.fake.submitted = json.loads(snapshot.read_bytes())
        self.assertEqual(ca.main(['wait', '--timeout', '0']), 0)
        self.fake.finish('2', ['b.mp4'])
        self.assertEqual(ca.main(['wait']), 0)
        self.assertCountEqual([m['receipt']['execution']['prompt_id'] for m in ca.load_state().get('made', {}).values()], ['1', '2'])
        self.assertEqual(len(self.fake.submitted), 2)

    def test_failed_cancelled_and_empty_jobs_return_errors_and_keep_good_outputs(self):
        self.queue(count=4)
        self.fake.finish('1')
        self.fake.finish('2', [], status_str='error', messages=[['execution_error', {'node_id': '9', 'node_type': 'SaveImage', 'exception_message': 'disk full'}]])
        self.fake.finish('3', [], messages=[['execution_interrupted', {}]])
        self.fake.finish('4', [])
        self.assertEqual(ca.main(['wait']), 1)
        report = self.stdout.getvalue() + self.stderr.getvalue()
        for phrase in ('disk full', 'cancelled', 'no files'):
            self.assertIn(phrase, report.lower())
        self.assertEqual(len(ca.load_state()['made']), 1)
        self.assertFalse(ca.load_state()['pending'])

    def test_partial_submit_validation_reported_even_with_saved_output(self):
        def partial(pid):
            self.fake.finish(pid)
            return {'prompt_id': pid, 'node_errors': {'8': {'class_type': 'LoadImage', 'errors': [{'message': 'missing photo'}]}}}
        self.fake.on_submit = partial
        reply = self.call('generate', {'recipe': 'test', 'settings': {'prompt': 'fox'}})
        self.assertTrue(reply.get('isError'))
        self.assertIn('missing photo', reply['content'][0]['text'])
        self.assertIn('.png', reply['content'][0]['text'])
        self.assertEqual(len(ca.load_state()['made']), 1)

    def test_redo_overrides_raw_targets_and_refreshes_fixed_default_seed(self):
        self.recipe.inputs['seed']['default'] = 9
        (self.recipe.folder / 'card.json').write_text(json.dumps(self.recipe.card))
        jobs = self.queue(sets=[('4.text', 'old'), ('7.steps', '20')])
        self.fake.finish('1')
        ca.wait_jobs(jobs)
        with mock.patch.object(ca.random, 'randint', return_value=77):
            redo = ca.queue_redo(ca.load_state()['last'][0], [('prompt', 'new')])[0]
        graph = self.fake.submitted[-1]['prompt']
        self.assertEqual((graph['4']['inputs']['text'], graph['7']['inputs']['steps'], redo['seed']), ('new', 20, 77))

    def test_short_transfer_is_never_published(self):
        jobs = self.queue()
        self.fake.finish('1')
        def response(data, length, status):
            body = io.BytesIO(data)
            body.headers = {'Content-Length': str(length), 'Content-Range': 'bytes 2-3/4'}
            body.status = status
            return body
        calls = [response(b'ab', 4, 200), response(b'cd', 2, 206)]
        with mock.patch.object(ca, 'download', real_download), mock.patch.object(ca, 'open_url', side_effect=calls) as get:
            self.assertEqual(ca.main(['wait']), 1)
            self.assertFalse(ca.load_state().get('made'))
            self.assertFalse(list((self.out / 'test').glob('*.png')))
            self.assertEqual(ca.main(['wait']), 0)
            self.assertEqual(get.call_args.kwargs['headers']['Range'], 'bytes=2-')
        self.assertEqual(Path(ca.load_state()['last'][0]).read_bytes(), b'abcd')

    def test_unconfirmed_submit_can_arrive_after_the_first_recovery_check(self):
        def disconnected(pid):
            raise ca.Fail('lost connection before reply')
        self.fake.on_submit = disconnected
        with self.assertRaises(ca.Fail):
            self.queue()
        incoming = self.fake.queue.pop('1')
        ca.main(['wait', '--timeout', '0'])
        self.assertEqual(len(ca.load_state()['pending']), 1)
        self.fake.queue['1'] = incoming
        self.fake.finish('1')
        self.assertEqual(ca.main(['wait']), 0)
        self.assertEqual(len(ca.load_state()['made']), 1)

    def test_no_wait_reports_partial_rejection_and_preserves_accepted_work(self):
        self.fake.on_submit = lambda pid: {'prompt_id': pid, 'node_errors': {'9': {'errors': [{'message': 'bad node'}]}}}
        self.assertEqual(ca.main(['run', 'test', 'prompt=fox', '--no-wait']), 1)
        self.assertEqual([j['id'] for j in ca.load_state()['pending']], ['1'])
        self.assertIn('bad node', self.stdout.getvalue())

    def test_wrong_download_range_never_joins_unrelated_bytes(self):
        (self.root / 'image.png.part').write_bytes(b'ab')
        body = io.BytesIO(b'ab')
        body.headers = {'Content-Length': '2', 'Content-Range': 'bytes 0-1/4'}
        body.status = 206
        with mock.patch.object(ca, 'open_url', return_value=body), self.assertRaises(ca.Fail):
            real_download('fake', self.root / 'image.png')
        self.assertFalse((self.root / 'image.png').exists())

    def test_short_resumed_response_is_not_published(self):
        dest = self.root / 'image.png'
        dest.with_suffix('.png.part').write_bytes(b'ab')
        body = io.BytesIO(b'cd')
        body.status = 206
        body.headers = {'Content-Length': '4', 'Content-Range': 'bytes 2-3/4'}
        with mock.patch.object(ca, 'open_url', return_value=body), self.assertRaises(ca.Fail):
            real_download('fake', dest)
        self.assertFalse(dest.exists())

    def test_cli_and_mcp_report_empty_jobs(self):
        def finish_empty(pid):
            self.fake.finish(pid, [])
            return {'prompt_id': pid}
        self.fake.on_submit = finish_empty
        self.assertEqual(ca.main(['run', 'test', 'prompt=fox']), 1)
        reply = self.call('generate', {'recipe': 'test', 'settings': {'prompt': 'fox'}})
        self.assertTrue(reply.get('isError'))
        self.assertIn('no files', reply['content'][0]['text'])

    def test_cannot_save_result_is_a_cli_and_tool_error(self):
        self.fake.on_submit = lambda pid: (self.fake.finish(pid) or {'prompt_id': pid})
        with mock.patch.object(ca, 'download', side_effect=OSError('disk full')):
            with self.subTest(caller='cli'):
                self.assertEqual(ca.main(['run', 'test', 'prompt=fox']), 1)
                self.assertIn('disk full', self.stderr.getvalue())
            with self.subTest(caller='mcp'):
                result = self.call('generate', {'recipe': 'test', 'settings': {'prompt': 'fox'}})
                self.assertTrue(result.get('isError'))
                self.assertIn('disk full', result['content'][0]['text'])
        self.assertFalse(ca.load_state().get('made'))

    def test_input_reference_alone_is_not_a_saved_result(self):
        self.queue()
        self.fake.finish('1')
        self.fake.history['1']['outputs']['9']['images'][0]['type'] = 'input'
        self.assertEqual(ca.main(['wait']), 1)
        self.assertFalse(ca.load_state().get('made'))

    def test_enabled_lora_rows_and_empty_model_choices(self):
        rows = [{'name': 'a.pt'}, {'name': 'b.pt', 'enabled': False}, {'name': 'c.pt', 'group_off': True}]
        graph = {'1': {'class_type': 'AUSBOSS_NODES_LoraLoader', 'inputs': {'loras': json.dumps(rows)}},
                 '2': {'class_type': 'UNETLoader', 'inputs': {'unet_name': 'missing.pt'}}}
        self.fake.nodes['UNETLoader']['input']['required']['unet_name'] = [[]]
        with mock.patch.object(ca, 'models_in', return_value=[]) as models:
            self.assertEqual(ca.preflight(graph), (['AUSBOSS_NODES_LoraLoader'], ['a.pt', 'missing.pt']))
            models.return_value = ['extras/a.pt']
            self.assertEqual(ca.preflight(graph)[1], ['missing.pt'])
            compact = json.dumps(rows, separators=(',', ':'))
            graph['1']['inputs']['loras'] = compact
            models.return_value = ['a.pt']
            ca.preflight(graph)
            self.assertEqual(graph['1']['inputs']['loras'], compact)
            models.return_value = ['extras/a.pt']
            ca.preflight(graph)
        result = json.loads(graph['1']['inputs']['loras'])
        self.assertEqual(result, [{**rows[0], 'name': 'extras/a.pt'}, *rows[1:]])

    def test_install_downloads_klein_lora(self):
        with mock.patch.object(ca, 'node_info', return_value={}), mock.patch.object(ca, 'models_in', return_value=[]), \
                mock.patch.object(ca, 'remote_size', return_value=1), mock.patch.object(ca, 'folder_paths', return_value={
                    'custom_nodes': [str(self.root)], 'loras': [str(self.root / 'loras')]}), \
                mock.patch.object(ca, 'download') as fetch, mock.patch.object(ca, 'auth_headers', return_value={}):
            self.assertEqual(ca.main(['install', 'klein-outpaint', '--yes']), 0)
        name = 'pixaoutpaint.safetensors'
        fetch.assert_called_once_with('https://huggingface.co/Pixaroma/experimental_loras/resolve/main/' + name,
                                      self.root / 'loras' / name, {}, quiet=False)

    def test_mcp_number_and_boolean_values(self):
        self.recipe.inputs['fast'] = {'type': 'bool', 'set': '7.steps'}
        (self.recipe.folder / 'card.json').write_text(json.dumps(self.recipe.card))
        def finish(pid):
            self.fake.finish(pid)
            return {'prompt_id': pid}
        self.fake.on_submit = finish
        for value, expected in [('OFF', False), (True, True), ('yes', True), (0, False)]:
            reply = self.call('generate', {'recipe': 'test', 'settings': {'prompt': 'fox', 'width': 2.5, 'fast': value}})
            self.assertFalse(reply.get('isError'))
            graph = self.fake.submitted[-1]['prompt']
            with self.subTest(setting='width', fast=value):
                self.assertEqual(graph['6']['inputs']['width'], 2.5)
            self.assertIs(graph['7']['inputs']['steps'], expected)
        for value in ('maybe', '', 2):
            with self.subTest(value=value):
                reply = self.call('generate', {'recipe': 'test', 'settings': {'prompt': 'fox', 'fast': value}})
                self.assertTrue(reply.get('isError'))
                self.assertIn('fast', reply['content'][0]['text'])

    def test_bad_settings_give_plain_named_errors(self):
        for setting in ('seed=bad', 'width=nope', 'prompt=  '):
            with self.subTest(setting=setting):
                self.stderr.seek(0)
                self.stderr.truncate()
                self.assertEqual(ca.main(['run', 'test', 'prompt=fox', setting, '--no-wait']), 1)
                self.assertIn(setting.split('=')[0], self.stderr.getvalue().lower())
        self.assertFalse(self.fake.submitted)

    def test_media_type_and_streamed_upload(self):
        self.recipe.inputs['clip'] = {'type': 'video', 'set': '8.image'}
        clip = self.root / 'attachments' / 'my clip.mp4'
        clip.parent.mkdir()
        data = b'frame' * 450000
        clip.write_bytes(data)
        def receive(path, body=None, **kw):
            if path != '/upload/image':
                return self.fake(path, body, **kw)
            self.assertNotIsInstance(body, bytes)
            chunks = list(body)
            self.assertLessEqual(max(map(len, chunks)), 1 << 20)
            self.assertEqual(sum(map(len, chunks)), int(kw['headers']['Content-Length']))
            self.assertEqual(b''.join(chunks[1:-1]), data)
            self.fake.uploads.append(path)
            return {'subfolder': 'inputs', 'name': 'clip.mp4'}
        with mock.patch.object(ca, 'comfy', side_effect=receive):
            self.queue(('clip', str(clip)), count=2)
        self.assertEqual(self.fake.uploads, ['/upload/image'])
        self.assertEqual([j['prompt']['8']['inputs']['image'] for j in self.fake.submitted], ['inputs/clip.mp4'] * 2)
        text = self.root / 'notes.txt'
        text.touch()
        for kind, file in [('image', clip), ('video', text), ('audio', clip), ('file', text)]:
            with self.subTest(kind=kind), self.assertRaises(ca.Fail):
                self.recipe.inputs['clip']['type'] = kind
                self.queue(('clip', str(file)))
        self.assertEqual(self.fake.uploads, ['/upload/image'])

    def test_mcp_uses_names_and_cli_can_use_recipe_paths(self):
        path = str(self.recipe.folder)
        self.assertEqual(ca.main(['show', path]), 0)
        for name, args in [('recipes', {'name': path}), ('generate', {'recipe': path, 'settings': {'prompt': 'fox'}})]:
            with self.subTest(name=name):
                self.assertTrue(self.call(name, args).get('isError'))
        self.assertFalse(self.fake.submitted)

    def test_provider_tokens_only_go_to_the_exact_https_host(self):
        with mock.patch.dict(os.environ, HF_TOKEN='sample', CIVITAI_API_KEY='example'):
            for host in ('huggingface.co', 'civitai.com'):
                for url in (f'https://other{host}/m', f'http://{host}/m', f'https://{host}:444/m'):
                    with self.subTest(url=url):
                        self.assertEqual(ca.auth_headers(url), {})
                self.assertIn('Authorization', ca.auth_headers(f'https://{host}/m'))

    def test_provider_names_in_a_model_path_do_not_change_its_source(self):
        url = 'https://civitai.com/files/huggingface.co/models/a.pt'
        with mock.patch.dict(os.environ, CIVITAI_API_KEY='example'), \
                mock.patch.object(ca, 'open_url', return_value=io.BytesIO(b'{"siblings": []}')) as lookup:
            self.assertEqual(ca.resolve_model_url({'file': 'a.pt', 'url': url}), url)
            lookup.assert_not_called()
        url = 'https://example.org/civitai.com/models/7?modelVersionId=3'
        self.assertEqual(ca.resolve_model_url({'file': 'a.pt', 'url': url}), url)

    def test_redirect_drops_token_when_host_changes(self):
        request = urllib.request
        captured = []
        class Capture(request.BaseHandler):
            handler_order = 100
            def https_open(self, req):
                captured.append((req.full_url, req.get_header('Authorization')))
                response = io.BytesIO()
                response.code, response.msg = (302, 'Found') if len(captured) < 3 else (200, 'OK')
                response.info = lambda: response.headers
                response.geturl = lambda: req.full_url
                response.headers = {'location': 'https://huggingface.co/redirect' if len(captured) == 1
                                    else 'https://assets.example/file'}
                return response
        build = request.build_opener
        # Real urllib redirect processing, with an in-memory transport.
        with mock.patch.dict(os.environ, HF_TOKEN='sample', CIVITAI_API_KEY='example'), \
                mock.patch.object(request, '_opener', build(Capture())), \
                mock.patch.object(request, 'build_opener', side_effect=lambda *h: build(Capture(), *h)):
            url = 'https://huggingface.co/file'
            with ca.open_url(url, headers=ca.auth_headers(url)):
                pass
        self.assertEqual([token for _, token in captured], ['Bearer sample', 'Bearer sample', None])

    def test_install_uses_declared_source_and_checks_existing_folder(self):
        repo = 'https://example.org/nodes/sample'
        pack = {'id': 'sample-id', 'repo': repo, 'classes': ['Missing']}
        self.recipe.card['needs'] = {'nodes': [pack]}
        with mock.patch.object(ca, 'get_recipe', return_value=self.recipe), \
                mock.patch.object(ca, 'preflight', return_value=(['Missing'], [])), \
                mock.patch.object(ca, 'folder_paths', return_value={'custom_nodes': [str(self.root)]}), \
                mock.patch.object(ca.shutil, 'which', return_value='comfy'), \
                mock.patch.object(ca.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout='other')) as run:
            self.assertEqual(ca.main(['install', 'test', '--yes']), 0)
            self.assertIn(repo, self.stdout.getvalue())
            with self.subTest(source=repo):
                self.assertEqual(run.call_args.args[0][-2:], [repo, str(self.root / 'sample')])
            (self.root / 'sample').mkdir()
            (self.root / 'sample' / 'install.py').touch()
            with self.subTest(existing='other folder'):
                self.assertEqual(ca.main(['install', 'test', '--yes']), 1)
                self.assertFalse(any('install.py' in call.args[0] for call in run.call_args_list))
            pack['id'] = '--option'
            run.reset_mock()
            self.assertEqual(ca.main(['install', 'test']), 1)
            run.assert_not_called()

    def test_registry_install_uses_only_the_planned_source(self):
        pack = {'id': 'sample-id', 'classes': ['Missing']}
        self.recipe.card['needs'] = {'nodes': [pack]}
        repo = 'https://example.org/nodes/sample'
        with mock.patch.object(ca, 'get_recipe', return_value=self.recipe), \
                mock.patch.object(ca, 'preflight', return_value=(['Missing'], [])), \
                mock.patch.object(ca, 'folder_paths', return_value={'custom_nodes': [str(self.root)]}), \
                mock.patch.object(ca.shutil, 'which', return_value=None) as which, \
                mock.patch.object(ca.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run, \
                mock.patch.object(ca, 'open_url', return_value=io.BytesIO(json.dumps({'repository': repo}).encode())) as lookup:
            self.assertEqual(ca.main(['install', 'test', '--yes']), 0)
            self.assertIn(repo, self.stdout.getvalue())
            self.assertEqual(run.call_args.args[0][-2:], [repo, str(self.root / 'sample')])
            which.return_value = 'comfy'
            run.return_value.returncode = 1
            run.reset_mock()
            lookup.reset_mock()
            self.assertEqual(ca.main(['install', 'test', '--yes']), 1)
            lookup.assert_not_called()
            run.assert_called_once()

    def test_model_destination_stays_inside_its_folder(self):
        root = self.root / 'weights'
        root.mkdir()
        subfolders = ['../other', str(self.root)]
        try:
            (root / 'link').symlink_to(self.root, target_is_directory=True)
            subfolders.append('link')
        except OSError:
            pass
        model = {'folder': 'weights', 'file': 'a.pt', 'url': 'https://example.org/a.pt', 'size': 1}
        self.recipe.card['needs'] = {'models': [model]}
        with mock.patch.object(ca, 'get_recipe', return_value=self.recipe), \
                mock.patch.object(ca, 'preflight', return_value=([], ['a.pt'])), \
                mock.patch.object(ca, 'folder_paths', return_value={'custom_nodes': [str(self.root)], 'weights': [str(root)]}):
            for subfolder in subfolders:
                model['subfolder'] = subfolder
                with self.subTest(subfolder=subfolder):
                    self.assertEqual(ca.main(['install', 'test']), 1)

    def test_remote_install_refused_even_if_folder_exists(self):
        with mock.patch.dict(ca.S, url='http://render.example'), \
                mock.patch.object(ca, 'preflight', return_value=(['Missing'], [])), \
                mock.patch.object(ca, 'folder_paths', return_value={'custom_nodes': [str(self.root)]}):
            self.assertEqual(ca.main(['install', 'test', '--yes']), 1)
            self.assertIn('computer that runs ComfyUI', self.stderr.getvalue())

    def test_venv_symlink_stays_in_comfy_environment(self):
        python = self.root / '.venv' / 'bin' / 'python'
        python.parent.mkdir(parents=True)
        other = self.root / 'system-python'
        other.touch()
        try:
            python.symlink_to(other)
        except OSError:
            self.skipTest('symlinks unavailable')
        with mock.patch.dict(os.environ, COMFY_PYTHON=''):
            self.assertEqual(ca.comfy_python(self.root), str(python))

    def test_mcp_recovers_after_bad_stdio_frames_on_legacy_console(self):
        msg = {'jsonrpc': '2.0', 'method': 'ping', 'id': '雪'}
        data = b'{\n[]\n"\xff"\n' + json.dumps(msg, ensure_ascii=False).encode('utf-8') + b'\n'
        with io.TextIOWrapper(io.BytesIO(data), encoding='cp1252') as stdin, \
                io.TextIOWrapper(io.BytesIO(), encoding='cp1252') as stdout, \
                mock.patch.object(sys, 'stdin', stdin), mock.patch.object(sys, 'stdout', stdout):
            self.assertEqual(ca.main(['mcp']), 0)
            replies = [json.loads(line) for line in stdout.buffer.getvalue().decode('utf-8').splitlines()]
        self.assertEqual([r['error']['code'] for r in replies[:-1]], [-32700, -32600, -32700])
        self.assertEqual(replies[-1]['id'], '雪')

    def test_mcp_negotiates_only_supported_versions(self):
        for offer, expected in [('2025-06-18', '2025-06-18'), ('2999-01-01', '2025-11-25')]:
            reply = ca.mcp_handle({'method': 'initialize', 'id': 1, 'params': {'protocolVersion': offer}})
            self.assertEqual(reply['result']['protocolVersion'], expected)

    def test_unload_errors_keep_the_command_and_its_output_private(self):
        command = 'unload example-private-command'
        cases = [mock.Mock(returncode=1, stdout='example-private-output', stderr=''), ca.subprocess.TimeoutExpired(command, 1)]
        for result in cases:
            with self.subTest(result=result), mock.patch.dict(ca.S, unload_llm=command), \
                    mock.patch.object(ca.subprocess, 'run', side_effect=[result]):
                self.assertEqual(ca.main(['run', 'test', 'prompt=fox']), 1)
                self.assertNotIn('example-private', self.stderr.getvalue())
        self.assertFalse(self.fake.submitted)

    def doctor(self, gpu):
        stats = {'system': {}, 'devices': [{'name': gpu}]}
        with mock.patch.object(ca, 'all_recipes', return_value={}), \
                mock.patch.object(ca, 'comfy', side_effect=[stats, self.fake('/queue')]):
            self.assertEqual(ca.main(['doctor']), 0)
        return self.stdout.getvalue()

    def test_ntfy_topic_private_on_success_and_failure(self):
        ca.S['send']['phone'] = 'ntfy:private-test-topic'
        self.assertNotIn('private-test-topic', self.doctor('GPU'))
        photo = self.root / 'photo.png'
        photo.touch()
        with mock.patch.object(ca, 'open_url', return_value=io.BytesIO()):
            self.assertNotIn('private-test-topic', ca.send_files([str(photo)], 'phone'))
        with mock.patch.object(ca, 'open_url', side_effect=OSError('private-test-topic')), self.assertRaises(ca.Fail) as e:
            ca.send_files([str(photo)], 'phone')
        self.assertNotIn('private-test-topic', str(e.exception))

    def test_unknown_send_target_refused_before_generation(self):
        self.fake.on_submit = lambda pid: (self.fake.finish(pid) or {'prompt_id': pid})
        with mock.patch.object(ca, 'queue_redo') as redo:
            self.assertEqual(ca.main(['run', 'test', 'prompt=fox', '--send', 'missing']), 1)
            self.assertEqual(ca.main(['redo', 'unknown.png', '--send', 'missing']), 1)
            reply = self.call('generate', {'recipe': 'test', 'settings': {'prompt': 'fox'}, 'send': 'missing'})
            self.assertTrue(reply.get('isError'))
            redo.assert_not_called()
        self.assertFalse(self.fake.submitted)

    def test_wan_graph_and_editor_use_fast_default(self):
        r = ca.get_recipe('wan-i2v')
        wf = json.loads((r.folder / 'workflow.json').read_bytes())
        spec = r.inputs['fast']
        self.assertIs(spec['default'], True)
        outer, inner = spec['set'].split('.')[0].split(':')
        self.assertIs(r.prompt()[f'{outer}:{inner}']['inputs']['value'], True)
        self.assertIs(next(n for n in wf['nodes'] if n['id'] == int(outer))['widgets_values'][-1], True)
        self.assertIs(next(n for n in ca.ui_nodes(wf) if n['id'] == int(inner))['widgets_values'][0], True)

    def test_add_refresh_from_own_source(self):
        src = self.recipe.folder / 'workflow.json'
        data = b'{"nodes":[]}'
        src.write_bytes(data)
        with mock.patch.object(ca, 'draft_card', return_value=fixtures.CARD | {'needs': {'models': [], 'nodes': []}}):
            self.assertEqual(ca.main(['add', str(src), '--api', str(self.recipe.folder / 'prompt.json'),
                                      '--name', 'test', '--into', str(self.recipe.folder.parent), '--force']), 0)
        self.assertEqual(src.read_bytes(), data)

    def test_add_rejects_unreadable_and_oversized_json(self):
        path = self.root / 'source.json'
        ui = self.root / 'ui.json'
        ui.write_text('{"nodes":[]}')
        for payload in (b'{', b'\xff', b'[]', b'{"x":{"class_type":"X","inputs":[]}}',
                        b'{"nodes":[]}' + b' ' * (16 << 20)):
            path.write_bytes(payload)
            for args in ([str(path)], [str(ui), '--api', str(path)]):
                with self.subTest(data=payload[:12], api=len(args) > 1), mock.patch.object(ca, 'convert_with_comfy_cli',
                        return_value=fixtures.PROMPT) as convert:
                    self.assertEqual(ca.main(['add', *args, '--into', str(self.root / 'new')]), 1)
                    convert.assert_not_called()
                    self.assertFalse((self.root / 'new').exists())

    def test_mcp_add_makes_a_recipe_and_runs_node_inputs(self):
        src = self.root / 'My Flow.json'
        src.write_text('{"nodes":[{"id":1,"type":"Note"}]}')
        with mock.patch.object(ca, 'KIT', self.root), \
                mock.patch.object(ca, 'convert_with_comfy_cli', return_value=fixtures.PROMPT) as convert:
            reply = self.call('recipes', {'add': str(src), 'name': 'flow'})
            self.assertFalse(reply.get('isError'), reply)
            text = reply['content'][0]['text']
            self.assertIn('Recipe flow is ready.', text)
            self.assertIn('7.steps=8', text)          # a plain node input the card has no setting for
            self.assertNotIn('model-a', text)         # model names are not offered as values
            # the same file again reuses the recipe instead of failing
            self.assertFalse(self.call('recipes', {'add': str(src), 'name': 'flow'}).get('isError'))
            self.assertEqual(convert.call_count, 1)  # reused, not converted again
            other = self.root / 'other.json'
            other.write_text('{"nodes":[]}')
            self.assertTrue(self.call('recipes', {'add': str(other), 'name': 'flow'}).get('isError'))
            self.fake.on_submit = lambda pid: (self.fake.finish(pid) or {'prompt_id': pid})
            reply = self.call('generate', {'recipe': 'flow', 'settings': {'prompt': 'a pear', '7.steps': 4}})
            self.assertFalse(reply.get('isError'), reply)
        sent = self.fake.submitted[-1]['prompt']
        self.assertEqual(sent['7']['inputs']['steps'], 4)

    def test_mcp_add_refused_over_http(self):
        reply = ca.mcp_handle({'method': 'tools/call', 'id': 1, 'params': {'name': 'recipes', 'arguments': {
            'add': str(self.root / 'x.json')}}}, transport='mcp-http')['result']
        self.assertTrue(reply.get('isError'))
        self.assertIn('this computer only', reply['content'][0]['text'])

    def test_mcp_node_inputs_keep_types_and_card_names_win(self):
        self.recipe.card['inputs']['sampler.steps'] = {'set': '7.steps', 'default': 8}
        (self.recipe.folder / 'card.json').write_text(json.dumps(self.recipe.card))
        self.fake.on_submit = lambda pid: (self.fake.finish(pid) or {'prompt_id': pid})
        reply = self.call('generate', {'recipe': 'test', 'settings': {'prompt': 'fox', '8.image': '123', 'sampler.steps': 3}})
        self.assertFalse(reply.get('isError'), reply)
        sent = self.fake.submitted[-1]['prompt']
        self.assertEqual(sent['8']['inputs']['image'], '123')   # text stays text
        self.assertEqual(sent['7']['inputs']['steps'], 3)        # the card's own dotted name, not node "sampler"
        bad = self.call('generate', {'recipe': 'test', 'settings': {'prompt': 'fox', '7.seed': ['2', 0]}})
        self.assertTrue(bad.get('isError'))                      # no links or lists through a node input

    def test_connector_neither_sees_add_nor_sets_node_inputs(self):
        tools = ca.mcp_handle({'method': 'tools/list', 'id': 1}, transport='mcp-http')['result']['tools']
        recipes = next(t for t in tools if t['name'] == 'recipes')
        self.assertNotIn('add', recipes['inputSchema']['properties'])
        self.assertIn('add', next(t for t in ca.MCP_TOOLS if t['name'] == 'recipes')['inputSchema']['properties'])
        reply = ca.mcp_handle({'method': 'tools/call', 'id': 1, 'params': {'name': 'generate', 'arguments': {
            'recipe': 'test', 'settings': {'prompt': 'fox', '7.steps': 2}}}}, transport='mcp-http')['result']
        self.assertTrue(reply.get('isError'))
        self.assertFalse(self.fake.submitted)

    def test_failed_add_leaves_nothing_behind(self):
        src = self.root / 'flow.json'
        src.write_text('{"nodes":[]}')
        into = self.root / 'new'
        with mock.patch.object(ca, 'convert_with_comfy_cli', return_value=fixtures.PROMPT), \
                mock.patch.object(ca.shutil, 'copyfile', side_effect=OSError(28, 'No space left')):
            self.assertEqual(ca.main(['add', str(src), '--into', str(into)]), 1)
        self.assertEqual(list(into.iterdir()), [])
        with mock.patch.object(ca, 'convert_with_comfy_cli', return_value=fixtures.PROMPT):
            self.assertEqual(ca.main(['add', str(src), '--into', str(into)]), 0)
        self.assertEqual(sorted(x.name for x in (into / 'flow').iterdir()), ['card.json', 'prompt.json', 'workflow.json'])

    def test_initialize_tells_the_agent_how_to_use_the_tools(self):
        local = ca.mcp_handle({'method': 'initialize', 'id': 1, 'params': {}})['result']['instructions']
        remote = ca.mcp_handle({'method': 'initialize', 'id': 1, 'params': {}}, transport='mcp-http')['result']['instructions']
        self.assertIn('generate', local)
        self.assertIn('add=PATH', local)
        self.assertNotIn('add=PATH', remote)

    def test_set_without_node_id_explains(self):
        self.assertEqual(ca.main(['run', 'test', 'prompt=fox', '--set', 'seed=424242']), 1)
        self.assertIn('seed=VALUE without --set', self.stderr.getvalue())
        self.assertFalse(self.fake.submitted)

    def test_windows_command_hint(self):
        with mock.patch.object(ca.os, 'name', 'nt'):
            msg = ca.not_ready_message(self.recipe, ['Missing'], [])
        self.assertIn('python comfy_agent.py install test', msg)

    def test_cp1252_prints_non_ascii_errors(self):
        with io.TextIOWrapper(io.BytesIO(), encoding='cp1252', errors='strict') as stream, \
                mock.patch.object(sys, 'stderr', stream):
            self.assertEqual(ca.main(['show', '雪']), 1)
            stream.flush()
            self.assertIn(br'\u96ea', stream.buffer.getvalue())

    def test_doctor_gpu_name(self):
        for gpu, expected in [('cuda:0 Example GPU : cudaMallocAsync', 'Example GPU'), ('Example: Pro', 'Example: Pro')]:
            with self.subTest(gpu=gpu):
                self.assertIn('GPU: ' + expected + ',', self.doctor(gpu))
