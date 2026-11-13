import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from varianttrail.archive import Archive
from varianttrail.explorer import (
    Explorer,
    ExplorerError,
    read_page,
)
from varianttrail.source_server import FixtureServer


class ExplorerServerTests(unittest.TestCase):
    def setUp(self):
        self.server = FixtureServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        if hasattr(self, '_directory'):
            self._directory.cleanup()

    def _explorer(self, directory, budget):
        archive = Archive(directory / 'captures.sqlite3', directory / 'objects')
        self.addCleanup(archive.close)
        return Explorer(self.base, archive, budget, state_path=directory / 'state.json')

    def test_full_exploration_groups_merged_sku_paths_and_keeps_soldout(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            report = self._explorer(directory, 50).explore()
            self.assertTrue(report['complete'])
            epoch = report['epochs'][0]
            self.assertEqual(epoch['revision'], 'initial')
            self.assertEqual(epoch['checked_combinations'], 9)
            # 条件不成立的边没有 SKU，但必须可解释。
            self.assertEqual(epoch['blocked_edges'][0]['path'],
                             ['material:linen', 'color:black'])
            self.assertEqual(epoch['blocked_edges'][0]['reason'], 'condition')
            groups = {group['sku']: group['paths'] for group in epoch['sku_groups']}
            # single 与 gift 合并到同一 SKU，两条路径证据都保留。
            small = groups['cotton-white-small']
            self.assertEqual(len(small), 2)
            self.assertEqual({path['labels'][-1] for path in small}, {'单件', '礼装'})
            for path in small:
                self.assertEqual(len(path['evidence_capture_ids']), 4)
            # 售罄仍保留 SKU，并标记 soldout。
            large = groups['cotton-white-large'][0]
            self.assertEqual(large['stock'], 'soldout')
            self.assertEqual(large['price'], '12.30')
            self.assertEqual(epoch['unexplored'], [])
            # SKU 详情只绑定完整选择：每条叶子路径恰好 4 个证据（product + 3 次选择）。
            for group in epoch['sku_groups']:
                for path in group['paths']:
                    self.assertEqual(len(path['evidence_capture_ids']), 4)

    def test_budget_stops_with_unexplored_frontier_instead_of_claiming_done(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            first = self._explorer(directory, 18).explore()
            self.assertEqual(first['stopped_reason'], 'budget_exhausted')
            self.assertFalse(first['complete'])
            self.assertEqual(first['epochs'][0]['checked_combinations'], 1)
            self.assertTrue(first['epochs'][0]['unexplored'])
            # 预算信息与未探索原因都在报告里。
            self.assertEqual(first['budget']['used_this_run'], 18)
            state_text = (directory / 'state.json').read_text(encoding='utf-8')
            self.assertNotIn('token', state_text)

    def test_restart_rebuilds_paths_with_new_token_and_resumes_frontier(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            self._explorer(directory, 38).explore()
            state_path = directory / 'state.json'
            before = json.loads(state_path.read_text(encoding='utf-8'))
            self.assertEqual(len(before['epochs'][0]['leaves']), 6)
            # 模拟进程重启：新 Explorer 不持有任何内存令牌，只凭检查点恢复。
            report = self._explorer(directory, 50).explore()
            epoch = report['epochs'][0]
            self.assertEqual(epoch['checked_combinations'], 9)
            self.assertTrue(report['complete'])
            self.assertEqual(epoch['unexplored'], [])
            # 恢复后拿到的麻材质 SKU，其证据链来自重启后的新请求。
            linen = next(group for group in epoch['sku_groups']
                         if group['sku'] == 'linen-natural-small')
            self.assertEqual(len(linen['paths']), 2)

    def test_revision_change_rebounds_exploration_without_mixing_rules(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            first = self._explorer(directory, 18).explore()
            self.assertEqual(first['epochs'][0]['revision'], 'initial')
            urllib.request.urlopen(self.base + '/control/phase?value=updated').read()
            report = self._explorer(directory, 50).explore()
            self.assertEqual(len(report['epochs']), 2)
            old, new = report['epochs']
            self.assertEqual(old['revision'], 'initial')
            self.assertEqual(old['status'], 'closed_revision_changed')
            self.assertTrue(old['unexplored'])  # 旧边界原样保留
            self.assertEqual(new['revision'], 'updated')
            self.assertTrue(report['complete'])
            self.assertEqual(new['unexplored'], [])
            # 新规则移除了 gift 包装，且选项重排不产生新规格。
            for leaf in new['sku_groups']:
                for path in leaf['paths']:
                    self.assertFalse(any('pack:gift' in key for key in path['path']))
            self.assertEqual(new['checked_combinations'], 6)

    def test_only_read_requests_are_archived_and_tokens_never_persisted(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            explorer = self._explorer(directory, 50)
            explorer.explore()
            rows = explorer.archive.rows()
            paths = {row['url'].split('?')[0].replace(self.base, '') for row in rows}
            self.assertEqual(paths, {'/product', '/select'})
            self.assertFalse(any('/purchase' in row['url'] for row in rows))
            # 令牌只活在归档的请求事实里，检查点绝不保存。
            self.assertTrue(any('token=' in row['url'] for row in rows))
            self.assertNotIn('token', (directory / 'state.json').read_text(encoding='utf-8'))

    def test_stale_token_checkpoint_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            state_path = Path(raw) / 'state.json'
            state_path.write_text(json.dumps({'epochs': [], 'token': 'old-one-time-token'}))
            archive = Archive(Path(raw) / 'c.sqlite3', Path(raw) / 'objects')
            try:
                with self.assertRaises(ExplorerError):
                    Explorer(self.base, archive, 10, state_path=state_path)
            finally:
                archive.close()

    def test_write_and_nonlocal_urls_are_rejected_before_any_request(self):
        with tempfile.TemporaryDirectory() as raw:
            archive = Archive(Path(raw) / 'c.sqlite3', Path(raw) / 'objects')
            try:
                with self.assertRaises(ExplorerError):
                    Explorer('https://example.com', archive, 10)
                with self.assertRaises(ExplorerError):
                    Explorer('http://127.0.0.1:1/purchase', archive, 10)
            finally:
                archive.close()

    def test_option_order_and_label_changes_do_not_create_specs(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            initial = self._explorer(directory, 50).explore()
            initial_keys = sorted(
                tuple(leaf['path'])
                for group in initial['epochs'][0]['sku_groups']
                for leaf in group['paths']
            )
            urllib.request.urlopen(self.base + '/control/phase?value=updated').read()
            updated = self._explorer(directory, 50).explore()
            updated_keys = sorted(
                tuple(leaf['path'])
                for group in updated['epochs'][1]['sku_groups']
                for leaf in group['paths']
            )
            # 更新版只是 initial 的子集（gift 被移除），没有任何新增规格键。
            self.assertTrue(set(updated_keys).issubset(set(initial_keys)))


class PageReaderTests(unittest.TestCase):
    def test_reads_revision_options_reason_and_sku_without_purchase_form(self):
        html = (
            '<html><body><div id="revision">initial</div>'
            '<form action="/select"><input type="hidden" name="token" value="t1">'
            '<input type="hidden" name="field" value="material">'
            '<select name="value">'
            '<option value="cotton" data-key="material:cotton" data-reason="available">棉</option>'
            '<option value="x" data-key="material:x" data-reason="condition" disabled>禁</option>'
            '</select></form>'
            '<form action="/purchase"><button>订货</button></form>'
            '<div id="sku" data-id="sku-9" data-price="3.21" data-stock="soldout"></div>'
            '</body></html>'
        ).encode()
        page = read_page(html)
        self.assertEqual(page.revision, 'initial')
        self.assertEqual(page.token, 't1')
        options = page.forms[0].options
        self.assertTrue(options[0].selectable)
        self.assertFalse(options[1].selectable)
        self.assertEqual(options[1].reason, 'condition')
        self.assertEqual(page.write_forms, [{'action': '/purchase'}])
        self.assertEqual(page.sku, {'id': 'sku-9', 'price': '3.21', 'stock': 'soldout'})


class CliExplorerTests(unittest.TestCase):
    def test_explore_demo_restarts_and_reports_unexplored_reason(self):
        result = subprocess.run([sys.executable, '-m', 'varianttrail', 'explore-demo'],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report['checkpoint_contains_token'])
        first, second = report['first_run'], report['second_run']
        self.assertEqual(first['stopped_reason'], 'budget_exhausted')
        self.assertEqual(second['stopped_reason'], 'budget_exhausted')
        self.assertTrue(second['epochs'][0]['unexplored'])
        self.assertTrue(any(edge['reason'] == 'condition'
                            for edge in second['epochs'][0]['blocked_edges']))


if __name__ == '__main__':
    unittest.main()
