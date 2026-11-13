"""对联动规格门户做有边界的 explore 遍历。

门户不是无状态查询：每次 /select 消费一次性 token、重建下游选项。
本模块只发起到 /product 与 /select 的只读选择请求，绝不触碰 /purchase
一类写入动作；SKU 与价格只在完整选择后的页面上绑定一次。
"""

import json
import os
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from .archive import Archive
from .transport import fetch

SELECT_PATH = '/select'
PRODUCT_PATH = '/product'
# 门户中任何写入动作都不在允许集合内（/purchase 始终拒绝演练客户端）。
ALLOWED_PATHS = frozenset({PRODUCT_PATH, SELECT_PATH})
MAX_TOKEN_CONFLICT_RETRIES = 2


class ExplorerError(RuntimeError):
    """探索过程中的协议级或预算级错误。"""


class RevisionChanged(ExplorerError):
    """运行中检测到商品修订号变化，当前 epoch 的边界必须作废。"""


class BudgetExhausted(ExplorerError):
    """本次运行的请求预算已用完。"""


@dataclass
class Option:
    value: str
    key: str
    label: str
    reason: str
    disabled: bool

    @property
    def selectable(self):
        # soldout 仍允许选择；只有 condition（disabled）不成立、没有 SKU。
        return not self.disabled and self.reason != 'condition'


@dataclass
class FieldForm:
    field: str
    options: list


@dataclass
class Page:
    revision: str
    token: str | None
    forms: list = field(default_factory=list)
    sku: dict | None = None
    write_forms: list = field(default_factory=list)

    def field_form(self, name):
        for form in self.forms:
            if form.field == name:
                return form
        return None


class _PageReader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.revision = ''
        self.token = None
        self.forms = []
        self.write_forms = []
        self.sku = None
        self._form = None
        self._option = None
        self._in_revision = False
        self._revision_text = ''

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'div' and attrs.get('id') == 'revision':
            self._in_revision = True
            self._revision_text = ''
        elif tag == 'div' and attrs.get('id') == 'sku':
            self.sku = {
                'id': attrs.get('data-id'),
                'price': attrs.get('data-price'),
                'stock': attrs.get('data-stock'),
            }
        elif tag == 'form':
            self._form = {'action': attrs.get('action', ''), 'token': None,
                          'field': None, 'options': []}
        elif tag == 'input' and self._form is not None and attrs.get('type') == 'hidden':
            if attrs.get('name') == 'token' and self._form['action'] == SELECT_PATH:
                self._form['token'] = attrs.get('value')
                if self.token is None:
                    self.token = attrs.get('value')
            elif attrs.get('name') == 'field':
                self._form['field'] = attrs.get('value')
        elif tag == 'option' and self._form is not None:
            self._option = {
                'value': attrs.get('value'),
                'key': attrs.get('data-key', ''),
                'reason': attrs.get('data-reason', ''),
                'disabled': 'disabled' in attrs,
                'label': '',
            }
            self._form['options'].append(self._option)

    def handle_data(self, data):
        if self._in_revision:
            self._revision_text += data
        if self._option is not None:
            self._option['label'] += data

    def handle_endtag(self, tag):
        if tag == 'div' and self._in_revision:
            self.revision = self._revision_text.strip()
            self._in_revision = False
        elif tag == 'option':
            self._option = None
        elif tag == 'form' and self._form is not None:
            form = self._form
            self._form = None
            if form['action'] == SELECT_PATH and form['field']:
                self.forms.append(FieldForm(form['field'], [
                    Option(option['value'], option['key'], option['label'].strip(),
                           option['reason'], option['disabled'])
                    for option in form['options']
                ]))
            elif form['action'] != SELECT_PATH:
                self.write_forms.append({'action': form['action']})


def read_page(body):
    reader = _PageReader()
    reader.feed(body.decode('utf-8', errors='strict'))
    reader.close()
    return Page(reader.revision, reader.token, reader.forms, reader.sku, reader.write_forms)


def _allowed(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise ExplorerError('只允许对本机门户发起探索请求')
    if parsed.path not in ALLOWED_PATHS:
        raise ExplorerError('写入或非观察路径被禁止: ' + parsed.path)
    return parsed


def load_state(path):
    """恢复检查点。检查点里永远不允许出现令牌字段。"""
    text = Path(path).read_text(encoding='utf-8')
    state = json.loads(text)
    if not isinstance(state, dict) or 'epochs' not in state:
        raise ExplorerError('检查点结构无法识别')

    def contains_token(node):
        if isinstance(node, dict):
            return any(key == 'token' or contains_token(value) for key, value in node.items())
        if isinstance(node, list):
            return any(contains_token(item) for item in node)
        return False

    if contains_token(state):
        raise ExplorerError('检查点包含旧表单令牌，拒绝恢复；令牌只在单次进程内存活')
    return state


def save_state_atomic(path, state):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.state-', suffix='.tmp', dir=target.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _empty_epoch(revision):
    return {
        'revision': revision,
        'status': 'active',
        'frontier': [],          # 尚未走到的完整路径（稳定 option key 元组）
        'leaves': [],            # 已完整检查的组合
        'blocked': [],           # 条件不成立、无 SKU 的边
        'conflicts': [],         # 因 409 重试仍失败而未完成的路径
        'requests': 0,
    }


class Explorer:
    def __init__(self, base_url, archive: Archive, budget, state_path=None, state=None):
        _allowed(base_url + PRODUCT_PATH)
        self.base_url = base_url.rstrip('/')
        self.archive = archive
        self.budget = budget          # 本次运行允许的 GET 次数；None 表示不限
        self.requests_used = 0
        self.state_path = Path(state_path) if state_path else None
        if state is not None:
            self.state = state
        elif self.state_path is not None and self.state_path.exists():
            # 恢复检查点；其中不含令牌，令牌只能在本次进程内重新获取。
            self.state = load_state(self.state_path)
        else:
            self.state = {'product_url': self.base_url + PRODUCT_PATH,
                          'epochs': [], 'created_at': _now()}
        self.epoch = None

    # ---- 传输与预算 -------------------------------------------------

    def _get(self, url):
        _allowed(url)
        if self.budget is not None and self.requests_used >= self.budget:
            raise BudgetExhausted(url)
        status, content_type, body = fetch(url)
        self.requests_used += 1
        capture_id = self.archive.save(url, status, content_type, body)
        return status, body, capture_id

    def _open_product(self):
        status, body, capture_id = self._get(self.base_url + PRODUCT_PATH)
        if status != 200:
            raise ExplorerError('打开 /product 失败，HTTP %s' % status)
        page = read_page(body)
        return page, capture_id

    def _select(self, token, field, value):
        query = urllib.parse.urlencode({'token': token, 'field': field, 'value': value})
        return self._get(self.base_url + SELECT_PATH + '?' + query)

    # ---- 检查点 -----------------------------------------------------

    def _persist(self):
        if self.state_path is not None:
            save_state_atomic(self.state_path, self.state)

    # ---- 路径重放 ---------------------------------------------------

    def _walk(self, path_keys):
        """从 /product 重新走选择路径，返回完整选择后的页面。

        每个页面的 token 只在当前表单上下文有效且一次性消费；因此每深入
        一层都使用上一步响应里的新 token，从不复用旧 token。
        """
        page, root_capture = self._open_product()
        if self.epoch is not None and page.revision != self.epoch['revision']:
            raise RevisionChanged(page.revision)
        steps = [{'field': None, 'key': None, 'label': None, 'capture_id': root_capture,
                  'http_status': 200, 'token_scope': 'fresh /product'}]
        for depth, key in enumerate(path_keys):
            # 上游选择会重建下游表单，字段顺序始终以当前页面实际呈现为准。
            field_order = [form.field for form in page.forms]
            field = field_order[depth]
            form = page.field_form(field)
            if form is None:
                raise ExplorerError('页面缺少字段 %s，门户规则可能已变化' % field)
            option = next((item for item in form.options if item.key == key), None)
            if option is None:
                raise ExplorerError('当前表单上下文中找不到选项 %s' % key)
            if not option.selectable:
                raise ExplorerError('选项 %s 在当前上下文不可选择（%s）' % (key, option.reason))
            if page.token is None:
                raise ExplorerError('页面未提供表单令牌，无法继续选择')
            attempts = 0
            while True:
                status, body, capture_id = self._select(page.token, field, option.value)
                if status == 409:
                    # 同修订下令牌竞争/消费冲突：不假装成功，重新打开页面再走一遍。
                    attempts += 1
                    if attempts > MAX_TOKEN_CONFLICT_RETRIES:
                        raise ExplorerError('令牌冲突重试上限: ' + '/'.join(path_keys))
                    page, root_capture = self._open_product()
                    if page.revision != self.epoch['revision']:
                        raise RevisionChanged(page.revision)
                    for replay_field, replay_key in path_keys[:depth]:
                        replay_form = page.field_form(replay_field)
                        replay_option = next(item for item in replay_form.options
                                             if item.key == replay_key)
                        status, body, capture_id = self._select(
                            page.token, replay_field, replay_option.value)
                        if status == 409:
                            raise ExplorerError('重放期间再次收到 409: ' + '/'.join(path_keys))
                        page = read_page(body)
                    continue
                if status != 200:
                    raise ExplorerError('选择 %s 返回 HTTP %s' % (key, status))
                next_page = read_page(body)
                if next_page.revision != self.epoch['revision']:
                    raise RevisionChanged(next_page.revision)
                steps.append({'field': field, 'key': key, 'label': option.label,
                              'capture_id': capture_id, 'http_status': status,
                              'state': option.reason})
                page = next_page
                break
        return page, steps

    # ---- 探索主循环 -------------------------------------------------

    def explore(self):
        try:
            root, root_capture = self._open_product()
        except BudgetExhausted:
            return self.build_report(stopped='budget_exhausted')
        active = next((epoch for epoch in self.state['epochs']
                       if epoch['status'] == 'active' and epoch['revision'] == root.revision), None)
        if active is None:
            # 修订号不同：先封存仍处于 active 的旧 epoch，两版规则不混用。
            for stale in self.state['epochs']:
                if stale['status'] == 'active':
                    stale['status'] = 'closed_revision_changed'
                    stale['closed_reason'] = (
                        'product reopened under revision %s; old frontier stays unmerged'
                        % root.revision)
            active = _empty_epoch(root.revision)
            self.state['epochs'].append(active)
        self.epoch = active
        self._persisted_requests = active['requests']
        active['requests'] += self.requests_used
        if not active['frontier'] and not active['leaves'] and not active['blocked']:
            # 按稳定 option key 排序，展示顺序/文案变化不会改变遍历边界。
            active['frontier'] = ['/'.join([option.key]) for option in
                                  sorted((o for o in root.forms[0].options if o.selectable),
                                         key=lambda item: item.key)]
        # 根字段上条件不成立的选项（若有）也要解释为“无 SKU 的边”。
        for option in root.forms[0].options:
            if not option.selectable:
                self._record_blocked([], option, root_capture)
        self._persist()

        try:
            while active['frontier']:
                path = active['frontier'].pop(0)
                path_keys = path.split('/')
                try:
                    page, steps = self._walk(path_keys)
                except RevisionChanged as change:
                    # 修订边界：封存旧 epoch，不混用两版规则；新边界下次运行重划。
                    active['frontier'].insert(0, path)
                    active['status'] = 'closed_revision_changed'
                    active['closed_reason'] = 'revision changed to %s mid-run' % change
                    self._persist()
                    self.epoch = None
                    return self.build_report(stopped='revision_changed')
                except BudgetExhausted:
                    active['frontier'].insert(0, path)
                    self._persist()
                    return self.build_report(stopped='budget_exhausted')
                except ExplorerError as error:
                    active['conflicts'].append({'path': path_keys, 'reason': str(error)})
                    self._persist()
                    continue

                selected_fields = {step['field'] for step in steps[1:]}
                remaining = [form.field for form in page.forms if form.field not in selected_fields]
                if not remaining:
                    try:
                        self._record_leaf(path_keys, page, steps)
                    except ExplorerError as error:
                        # 完整选择却拿不到 SKU 属于门户协议异常：记账并继续，不伪造结果。
                        active['conflicts'].append({'path': path_keys, 'reason': str(error)})
                        self._persist()
                        continue
                else:
                    next_field = remaining[0]
                    form = page.field_form(next_field)
                    # 稳定 key 排序入队，避免门户展示排序变化制造新规格。
                    for option in sorted(form.options, key=lambda item: item.key):
                        child = '/'.join(path_keys + [option.key])
                        if option.selectable:
                            if child not in active['frontier']:
                                active['frontier'].append(child)
                        else:
                            self._record_blocked(path_keys, option, steps[-1]['capture_id'])
                active['requests'] = self._persisted_requests + self.requests_used
                self._persist()
        except BudgetExhausted:
            self._persist()
            return self.build_report(stopped='budget_exhausted')

        stopped = None if not active['frontier'] else 'budget_exhausted'
        return self.build_report(stopped=stopped)

    # ---- 记录 -------------------------------------------------------

    def _record_blocked(self, parent_keys, option: Option, observed_on_capture):
        entry = {'path': parent_keys + [option.key], 'reason': option.reason,
                 'label': option.label, 'observed_on_capture': observed_on_capture}
        if entry['path'] not in [item['path'] for item in self.epoch['blocked']]:
            self.epoch['blocked'].append(entry)

    def _record_leaf(self, path_keys, page: Page, steps):
        if page.sku is None or not page.sku.get('id'):
            raise ExplorerError('完整选择后页面未提供 SKU: ' + '/'.join(path_keys))
        # 只绑定最终完整选择页面上的详情，杜绝中间状态串号。
        entry = {
            'path': path_keys,
            'labels': [step['label'] for step in steps[1:]],
            'selection': {step['field']: step['key'] for step in steps[1:]},
            'sku': page.sku['id'],
            'price': page.sku['price'],
            'stock': page.sku['stock'],
            'evidence': [
                {'step': step['field'] or 'product', 'capture_id': step['capture_id'],
                 'http_status': step['http_status'],
                 **({'option_state': step['state']} if 'state' in step else {})}
                for step in steps
            ],
            'observed_at': _now(),
            'revision': page.revision,
        }
        self.epoch['leaves'].append(entry)

    # ---- 报告 -------------------------------------------------------

    def build_report(self, stopped=None):
        epochs_report = []
        for epoch in self.state['epochs']:
            sku_groups = {}
            for leaf in epoch['leaves']:
                sku_groups.setdefault(leaf['sku'], []).append({
                    'path': leaf['path'],
                    'labels': leaf['labels'],
                    'stock': leaf['stock'],
                    'price': leaf['price'],
                    'evidence_capture_ids': [item['capture_id'] for item in leaf['evidence']],
                })
            epochs_report.append({
                'revision': epoch['revision'],
                'status': epoch['status'],
                'checked_combinations': len(epoch['leaves']),
                'blocked_edges': sorted(epoch['blocked'], key=lambda item: item['path']),
                'sku_groups': [
                    {'sku': sku, 'paths': paths}
                    for sku, paths in sorted(sku_groups.items())
                ],
                'unexplored': sorted(epoch['frontier']),
                'unfinished_conflicts': epoch['conflicts'],
                'requests_spent': epoch['requests'],
            })
        report = {
            'product_url': self.state['product_url'],
            'stopped_reason': stopped,
            'complete': stopped is None and all(
                epoch['status'] != 'active' or not epoch['frontier']
                for epoch in self.state['epochs']),
            'write_actions_touched': [],
            'epochs': epochs_report,
            'state_file': str(self.state_path) if self.state_path else None,
        }
        if stopped == 'budget_exhausted':
            report['budget'] = {'limit_per_run': self.budget, 'used_this_run': self.requests_used}
        return report


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
