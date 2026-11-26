"""The optional usage log: off by default, one line per call, nothing typed by anyone, never fails a command."""
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import test_comfy_agent as fixtures
from test_core_fixes import FakeComfy

ca = fixtures.ca
real_comfy = ca.comfy


class StatsTest(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.recipes = self.root / 'recipes'
        self.make_recipe('test')
        self.fake = FakeComfy()

        def finish(pid):
            self.fake.finish(pid)
            return {'prompt_id': pid}

        self.fake.on_submit = finish
        stack.enter_context(mock.patch.dict(os.environ, COMFY_AGENT_MCP_WAIT='0'))
        self.out = self.root / 'out'
        self.log = self.out / 'stats.jsonl'
        self.settings = self.root / 'settings.json'
        self.settings.write_text(json.dumps({'stats': 'on'}))
        stack.enter_context(mock.patch.dict(ca.S, url='http://localhost', out=str(self.out), send={},
                                            recipes=[str(self.recipes)], unload_llm=None))
        stack.enter_context(mock.patch.object(ca, 'CONFIG', self.settings))
        stack.enter_context(mock.patch.dict(ca._node_info, clear=True))
        stack.enter_context(mock.patch.dict(ca.MCP_CLIENT, agent=None))
        stack.enter_context(mock.patch.object(ca, 'comfy', side_effect=self.fake))
        stack.enter_context(mock.patch.object(ca, 'download', side_effect=self.fake.download))
        self.stdout = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stderr = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def make_recipe(self, name):
        folder = self.recipes / name
        folder.mkdir(parents=True)
        (folder / 'card.json').write_text(json.dumps(fixtures.CARD))
        (folder / 'prompt.json').write_text(json.dumps(fixtures.PROMPT))
        return folder

    def lines(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []

    def clear_output(self):
        self.stdout.seek(0)
        self.stdout.truncate()

    def mcp(self, name, args, transport='mcp-stdio'):
        msg = {'method': 'tools/call', 'id': 1, 'params': {'name': name, 'arguments': args}}
        return ca.mcp_handle(msg, transport=transport)['result']

    def write_log(self, *rows):
        self.out.mkdir(exist_ok=True)
        self.log.write_text(''.join((r if isinstance(r, str) else json.dumps(r)) + '\n' for r in rows))

    def test_off_by_default_writes_nothing(self):
        with mock.patch.object(ca, 'CONFIG', self.root / 'missing.json'):
            self.assertNotIn('stats', ca.load_settings())
            self.assertFalse(ca.stats_enabled())
        self.settings.write_text('{}')
        self.assertEqual(ca.main(['list']), 0)
        self.assertEqual(ca.main(['run', 'test', 'prompt=fox']), 0)
        self.assertEqual(ca.main(['run', 'nope']), 1)
        self.mcp('generate', {'recipe': 'test', 'settings': {'prompt': 'fox'}})
        self.mcp('recipes', {})
        self.assertTrue((self.out / 'test').is_dir())  # the jobs ran and saved into the same folder
        self.assertFalse(self.log.exists())
        self.assertEqual(ca.main(['stats']), 0)
        self.assertIn('Usage stats are off. Nothing is kept yet.', self.stdout.getvalue())
        self.assertFalse(self.log.exists())

    def test_one_line_per_cli_command(self):
        self.assertEqual(ca.main(['list']), 0)
        self.clear_output()
        self.assertEqual(ca.main(['show', 'test']), 0)
        shown = len(self.stdout.getvalue())
        self.assertEqual(ca.main(['run', 'test', 'prompt=fox', '--count', '2', '--by', 'codex']), 0)
        self.assertEqual(ca.main(['run', 'test']), 1)  # prompt is required
        self.assertEqual(ca.main(['run', 'nope', 'prompt=fox']), 1)
        with self.assertRaises(SystemExit):
            ca.main(['run'])  # argparse: no recipe given
        self.assertEqual(ca.main(['stats']), 0)  # the person's own look is not logged
        rows = self.lines()
        self.assertEqual([r['command'] for r in rows], ['list', 'show', 'run', 'run', 'run', 'run'])
        self.assertEqual({r['via'] for r in rows}, {'cli'})
        self.assertEqual([r['recipe'] for r in rows], [None, 'own', 'own', 'own', None, None])
        self.assertEqual([r['ok'] for r in rows], [True, True, True, False, False, False])
        self.assertEqual([r['error'] for r in rows], [None, None, None, 'bad setting', 'bad setting', 'bad setting'])
        self.assertEqual(rows[2]['agent'], 'codex')
        self.assertEqual(rows[1]['reply_chars'], shown)
        self.assertEqual(rows[1]['reply_tokens_est'], (shown + 3) // 4)
        for row in rows:
            self.assertEqual(set(row), {'time', 'kit', 'command', 'via', 'recipe', 'agent', 'reply_chars',
                                        'reply_tokens_est', 'images', 'image_bytes', 'seconds', 'ok', 'error'})
            self.assertEqual(row['kit'], ca.VERSION)
            self.assertGreater(row['reply_chars'], 0)

    def test_failure_kinds(self):
        del self.fake.nodes['KSampler']
        self.assertEqual(ca.main(['run', 'test', 'prompt=fox']), 1)
        self.fake.nodes['KSampler'] = fixtures.OBJECT_INFO['KSampler']
        self.fake.nodes['UNETLoader'] = {'input': {'required': {'unet_name': [['other.safetensors']]}}}
        ca._node_info.clear()
        self.assertEqual(ca.main(['run', 'test', 'prompt=fox']), 1)
        with mock.patch.dict(ca.S, url='http://127.0.0.1:9'), mock.patch.object(ca, 'comfy', real_comfy):
            ca._node_info.clear()
            self.assertEqual(ca.main(['run', 'test', 'prompt=fox']), 1)
        self.assertEqual([r['error'] for r in self.lines()], ['node missing', 'model missing', 'ComfyUI not answering'])
        refused = {'error': {'type': 'prompt_outputs_failed_validation'}, 'node_errors': {'1': {'errors': [
            {'type': 'value_not_in_list', 'details': "unet_name: 'x.safetensors' not in ['y.safetensors']"}]}}}
        self.assertEqual(ca.refusal_kind(refused), 'model missing')
        self.assertEqual(ca.refusal_kind({'error': {'type': 'missing_node_type', 'message': 'Node Foo not found'}}),
                         'node missing')
        self.assertEqual(ca.refusal_kind({'error': {'type': 'invalid_prompt', 'message': 'Image file does not exist'}}),
                         'other')
        self.assertEqual(ca.refusal_kind({'node_errors': {'7': {'errors': [{'type': 'value_bigger_than_max'}]}}}),
                         'bad setting')
        self.assertEqual(ca.refusal_kind('a plain text error'), 'other')

    def test_one_line_per_mcp_call(self):
        ca.mcp_handle({'method': 'initialize', 'id': 0, 'params': {'clientInfo': {'name': 'codex-mcp-client'}}})
        self.mcp('recipes', {})
        self.mcp('recipes', {'name': 'test'})
        made = self.mcp('generate', {'recipe': 'test', 'settings': {'prompt': 'fox'}})
        self.mcp('generate', {'recipe': 'test', 'settings': {'prompt': 'fox'}, 'by': 'claude-code'}, transport='mcp-http')
        failed = self.mcp('generate', {'recipe': 'nope'})
        self.mcp('results', {})
        self.mcp('no-such-tool', {})
        ca.mcp_handle({'method': 'tools/list', 'id': 2})  # not a tool call
        rows = self.lines()
        self.assertEqual([r['command'] for r in rows],
                         ['recipes', 'recipes show', 'generate', 'generate', 'generate', 'results', 'unknown'])
        self.assertEqual([r['via'] for r in rows], ['mcp-stdio'] * 3 + ['mcp-http'] + ['mcp-stdio'] * 3)
        self.assertEqual([r['agent'] for r in rows], ['codex', 'codex', 'codex', 'claude code', 'codex', 'codex', 'codex'])
        self.assertEqual([r['ok'] for r in rows], [True, True, True, True, False, True, False])
        self.assertEqual(rows[4]['error'], 'bad setting')
        self.assertTrue(failed['isError'])
        self.assertEqual(rows[2]['recipe'], 'own')
        self.assertEqual(rows[2]['reply_chars'], len(made['content'][0]['text']))
        self.assertEqual((rows[2]['images'], rows[2]['image_bytes']), (1, len(fixtures.PNG)))
        self.assertEqual(rows[0]['images'], 0)

    def test_turning_it_off_stops_a_running_server(self):
        self.mcp('recipes', {})
        self.settings.write_text(json.dumps({'stats': 'off'}))  # `config stats=off` from another terminal
        self.mcp('recipes', {})
        self.log.unlink()
        self.mcp('results', {})
        self.assertFalse(self.log.exists())  # a deleted log is not made again
        self.settings.write_text('not json')  # a setting that can't be read counts as off
        self.mcp('recipes', {})
        self.assertFalse(self.log.exists())

    def test_agent_names_are_known_names_only(self):
        for given, label in (('claude-code', 'claude code'), ('Claude Code', 'claude code'), ('claude-ai', 'claude'),
                             ('codex-mcp-client', 'codex'), ('pi', 'pi'), ('cursor-vscode', 'cursor'),
                             ('Claude Smith', 'other'), ('C:\\Users\\Zed\\client', 'other'),
                             ('https://pi.example', 'other'), ('Jane', 'other'), ('', None), (None, None), ([], None)):
            self.assertEqual(ca.agent_label(given), label, given)
        # Many clients can share the HTTP server, so a client's name from initialize is used on stdio only.
        ca.mcp_handle({'method': 'initialize', 'id': 0, 'params': {'clientInfo': {'name': 'codex'}}},
                      transport='mcp-http')
        self.mcp('recipes', {}, transport='mcp-http')
        self.assertIsNone(self.lines()[-1]['agent'])

    def test_bundled_recipe_is_named_and_others_are_own(self):
        bundled = ca.KIT / 'recipes' / 'zimage'
        self.assertEqual(ca.stats_recipe(ca.Recipe(bundled)).name, 'zimage')
        self.assertEqual(ca.STATS_CALL.recipe, 'zimage')
        copy = self.make_recipe('zimage-copy')
        copy.rename(copy.with_name('zimage'))  # an own recipe that happens to share a bundled name
        ca.stats_recipe(ca.Recipe(self.recipes / 'zimage'))
        self.assertEqual(ca.STATS_CALL.recipe, 'own')
        for name in ca.BUNDLED_RECIPES:
            self.assertTrue((ca.KIT / 'recipes' / name / 'card.json').exists(), name)
        commands = {n[4:] for n in dir(ca) if n.startswith('cmd_')} - {'mcp', 'stats'}
        self.assertEqual(commands, set(ca.STATS_CLI))

    def test_no_prompt_path_url_or_name_in_the_log(self):
        secret_dir = self.root / 'SECRET-DIR-plugh'
        secret_dir.mkdir()
        picture = secret_dir / 'holiday.png'
        picture.write_bytes(fixtures.PNG)
        self.make_recipe('my-private-recipe-zork')
        ca.S['url'] = 'http://secret-host-quux:8188'
        prompt, request, person = 'SECRET-PROMPT-xyzzy', 'SECRET-REQUEST-frob', 'Jane Secretname'
        self.assertEqual(ca.main(['run', 'my-private-recipe-zork', f'prompt={prompt}', f'image={picture}',
                                  '--by', person, '--request', request]), 0)
        self.assertEqual(ca.main(['run', 'my-private-recipe-zork', f'prompt={prompt}',
                                  f'image={secret_dir / "missing.png"}']), 1)  # error text names the path
        self.assertIn('SECRET-DIR-plugh', self.stderr.getvalue())
        self.assertEqual(ca.main(['run', 'SECRET-TYPO-recipe', f'prompt={prompt}']), 1)
        self.assertEqual(ca.main(['show', str(self.recipes / 'my-private-recipe-zork')]), 0)
        ca.mcp_handle({'method': 'initialize', 'id': 0, 'params': {'clientInfo': {'name': 'Jane Secretname laptop'}}})
        self.mcp('generate', {'recipe': 'my-private-recipe-zork', 'by': person, 'request': request,
                              'settings': {'prompt': prompt, 'image': str(picture)}})
        self.mcp('recipes', {'add': str(secret_dir / 'SECRET-WORKFLOW.json'), 'name': 'SECRET-NAME-zork'})
        self.mcp('SECRET-TOOL', {'prompt': prompt})
        self.clear_output()
        self.assertEqual(ca.main(['stats']), 0)
        self.assertEqual(ca.main(['stats', '--share']), 0)
        rows = self.lines()
        self.assertEqual(len(rows), 7)
        self.assertEqual({r['agent'] for r in rows}, {None, 'other'})
        printed = self.stdout.getvalue()  # also safe to copy from the terminal
        for text in (self.log.read_text(), (self.out / 'stats-share.txt').read_text(), printed):
            for planted in ('SECRET', 'plugh', 'xyzzy', 'quux', 'frob', 'Jane', 'Secretname', 'zork', 'holiday',
                            str(self.root), 'localhost', '\\'):
                self.assertNotIn(planted, text)
        for text in (self.log.read_text(), (self.out / 'stats-share.txt').read_text()):
            self.assertNotIn('/', text)
            self.assertNotIn('http', text)

    def test_a_log_write_error_does_not_fail_the_command(self):
        self.log.mkdir(parents=True)  # the log's name is taken by a folder, so it can't be written
        self.assertEqual(ca.main(['list']), 0)
        self.assertIn('test:', self.stdout.getvalue())
        self.assertEqual(ca.main(['run', 'test', 'prompt=fox']), 0)
        self.assertNotIn('isError', self.mcp('recipes', {}))
        with mock.patch.object(ca, 'stats_file', side_effect=RuntimeError('boom')), \
                mock.patch.object(ca, 'agent_label', side_effect=RuntimeError('boom')):
            self.assertEqual(ca.main(['run', 'test', 'prompt=fox', '--by', 'codex']), 0)
            self.assertNotIn('isError', self.mcp('recipes', {'name': 'test'}))
        self.assertTrue(self.log.is_dir())
        self.assertEqual(self.stderr.getvalue(), '')
        self.assertEqual(ca.main(['stats']), 1)
        self.assertIn("Can't read the usage log", self.stderr.getvalue())

    @unittest.skipUnless(hasattr(os, 'mkfifo') and hasattr(os, 'symlink'), 'POSIX pipes and links')
    def test_the_log_is_only_ever_a_plain_file(self):
        self.out.mkdir()
        os.mkfifo(self.log)  # a pipe nobody reads: opening it for writing would wait forever
        done = []
        worker = threading.Thread(target=lambda: done.append(ca.main(['list'])), daemon=True)
        worker.start()
        worker.join(5)
        self.assertEqual(done, [0])
        self.log.unlink()
        elsewhere = self.root / 'elsewhere.txt'
        elsewhere.write_text('keep me\n')
        os.symlink(elsewhere, self.log)
        self.assertEqual(ca.main(['list']), 0)
        self.assertEqual(elsewhere.read_text(), 'keep me\n')
        self.log.unlink()
        self.write_log({'command': 'list', 'ok': True, 'reply_tokens_est': 5})
        os.symlink(elsewhere, self.out / 'stats-share.txt')
        self.assertEqual(ca.main(['stats', '--share']), 0)
        self.assertEqual(elsewhere.read_text(), 'keep me\n')
        self.assertFalse((self.out / 'stats-share.txt').is_symlink())
        self.assertEqual([p.name for p in self.out.iterdir() if p.name.endswith('.tmp')], [])

    def test_config_turns_it_on_and_off_without_logging_either_switch(self):
        self.settings.write_text(json.dumps({'phone': 'ntfy:SECRET-TOPIC'}))
        ca.S['send'] = {'phone': 'ntfy:SECRET-TOPIC'}
        self.assertEqual(ca.main(['config', 'stats=on']), 0)
        self.assertEqual(json.loads(self.settings.read_text())['stats'], 'on')
        self.assertIn('The kit never sends it anywhere', self.stdout.getvalue())
        self.assertNotIn('SECRET-TOPIC', self.stdout.getvalue())  # only the stats line, not every setting
        self.assertEqual(ca.main(['list']), 0)
        self.assertEqual(ca.main(['config', 'stats=maybe']), 1)
        self.assertEqual(ca.main(['config', 'stats=off']), 0)
        self.assertEqual(ca.main(['list']), 0)
        self.assertEqual(json.loads(self.settings.read_text())['stats'], 'off')
        self.assertEqual([(r['command'], r['error']) for r in self.lines()], [('list', None), ('config', 'bad setting')])

    def test_damaged_lines_are_skipped_or_read_as_other(self):
        big = int('9' * 400)
        self.write_log({}, {'command': 'list'}, {'command': 'run', 'ok': 'yes'}, '[1]', 'not json', '[' * 100000,
                       {'command': 'run', 'ok': True, 'agent': [], 'recipe': {'a': 1}, 'reply_tokens_est': big,
                        'kit': '192.0.2.42', 'via': ['cli'], 'seconds': float('nan'), 'time': 12},
                       {'command': 'run', 'ok': False, 'error': ['x'], 'images': -3, 'reply_tokens_est': 40})
        rows = ca.stats_rows()
        self.assertEqual([(r['agent'], r['recipe'], r['via'], r['tokens'], r['error']) for r in rows],
                         [('other', 'other', 'other', 0, None), (None, None, 'other', 40, 'other')])
        share = ca.stats_summary(rows)
        self.assertTrue(share.startswith(f'comfy-agent {ca.VERSION} · '))
        self.assertNotIn('192.0.2.42', share)

    def test_stats_and_share_summaries(self):
        self.write_log(
            {'time': '2026-11-10T09:00:00Z', 'kit': '1.0', 'command': 'run', 'via': 'cli', 'recipe': 'zimage',
             'agent': 'codex', 'reply_tokens_est': 100, 'seconds': 3.0, 'ok': True, 'error': None},
            {'time': '2026-11-11T09:00:00Z', 'kit': '1.0', 'command': 'run', 'via': 'cli', 'recipe': 'zimage',
             'agent': 'codex', 'reply_tokens_est': 300, 'seconds': 5.0, 'ok': True, 'error': None},
            {'time': '2026-11-12T09:00:00Z', 'kit': '1.0', 'command': 'recipes add', 'via': 'mcp-stdio',
             'recipe': 'own', 'agent': 'claude code', 'reply_tokens_est': 1900, 'seconds': 1.0, 'ok': True},
            {'time': '2026-11-12T10:00:00Z', 'kit': '1.0', 'command': 'generate', 'via': 'mcp-http', 'recipe': None,
             'agent': None, 'reply_tokens_est': 40, 'images': 2, 'image_bytes': 300000, 'seconds': 0.5, 'ok': False,
             'error': 'model missing'},
            # An edited line: anything outside the fixed lists becomes "other" and never reaches a summary.
            {'time': 'yesterday', 'kit': 'SECRET', 'command': 'SECRET-CMD', 'via': 'carrier pigeon',
             'recipe': 'my-secret', 'agent': 'Jane', 'reply_tokens_est': 'lots', 'ok': False, 'error': 'SECRET text'},
        )
        system = {'Darwin': 'macOS'}.get(ca.platform.system(), ca.platform.system())
        self.assertEqual(ca.main(['stats']), 0)
        mine = self.stdout.getvalue()
        self.assertIn('Usage stats are on. The log is stats.jsonl in your outputs folder', mine)
        self.assertNotIn('never uploads', mine)
        self.assertFalse((self.out / 'stats-share.txt').exists())
        self.clear_output()
        self.assertEqual(ca.main(['stats', '--share']), 0)
        share = (self.out / 'stats-share.txt').read_text()
        self.assertEqual(share.splitlines(), [
            f'comfy-agent {ca.VERSION} · {system} · agent: codex, claude code, other (as reported) · 3 days',
            'calls 5 · worked 3 · failed 2 · cli 2, mcp 2, other 1',
            'reply tokens (estimated): median 100, largest 1,900 (recipes add)',
            '  run: 2 calls, median 200, largest 300, about 4.0 s each',
            '  recipes add: 1 call, median 1,900, largest 1,900, about 1.0 s each',
            '  generate: 1 call, median 40, largest 40, about 0.5 s each',
            '  other: 1 call, median 0, largest 0, about 0.0 s each',
            'pictures in MCP replies: 2, 0.3 MB',
            'failures: model missing 1, other 1',
            'recipes: zimage 2, own 1, other 1',
        ])
        printed = self.stdout.getvalue()
        self.assertIn(share, printed)
        self.assertIn('The kit never uploads it', printed)
        self.assertIn('post it yourself', printed)
        self.write_log({'time': '2026-11-01T09:00:00Z', 'command': 'list', 'ok': True},
                       {'time': '2026-11-30T09:00:00Z', 'command': 'list', 'ok': True})
        self.assertIn(' · 30 days, used on 2\n', ca.stats_summary(ca.stats_rows()))
        self.write_log()
        self.clear_output()
        self.assertEqual(ca.main(['stats', '--share']), 0)
        self.assertIn('Nothing is kept yet. No share file was written.', self.stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
