DESCRIPTION = '合作商商品规格页面的 HTML 表单观察与授权请求留存。'
DEMO_PATH = '/sample'

import argparse
import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from .archive import Archive
from .explorer import Explorer
from .parser import inspect_document
from .source_server import FixtureServer
from .transport import fetch


def _run_explore(arguments):
    state_path = Path(arguments.state)
    archive = Archive(arguments.db, arguments.objects)
    try:
        # Explorer 自行加载检查点；恢复时重新打开 /product 拿新令牌，
        # 旧令牌绝不从检查点读取（load_state 会拒绝任何令牌字段）。
        explorer = Explorer(arguments.url, archive, arguments.budget,
                            state_path=state_path)
        return explorer.explore()
    finally:
        archive.close()


def _run_explore_demo(arguments):
    """本机门户端到端演练：预算中断 -> 进程重启换新令牌恢复 -> 遗漏材质。"""
    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        state_path = directory / 'explore-state.json'
        db_path = directory / 'captures.sqlite3'
        objects_path = directory / 'objects'
        server = FixtureServer()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_address[1])
        try:
            common = [sys.executable, '-m', 'varianttrail', 'explore', base,
                      '--state', str(state_path), '--db', str(db_path),
                      '--objects', str(objects_path)]
            # 第一次运行：预算只够覆盖棉的六个包装，麻（遗漏材质）整个留在边界上。
            first = subprocess.run(common + ['--budget', '38'], capture_output=True,
                                   text=True, check=True)
            first_report = json.loads(first.stdout)
            # 模拟进程重启：第二个子进程不继承任何内存令牌，只能凭检查点重建路径。
            second = subprocess.run(common + ['--budget', '10'], capture_output=True,
                                    text=True, check=True)
            second_report = json.loads(second.stdout)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()
        checkpoint = json.loads(state_path.read_text(encoding='utf-8'))
        return {
            'narrative': [
                '首次运行预算 38：棉的六种完整组合全部检查（含售罄仍保留 SKU 的大包装），麻材质未探索',
                '进程重启后第二次运行：检查点无令牌，重新打开 /product 用新令牌从清空状态重放选择路径',
                '第二次预算 10：重建到麻-原色-礼装并绑定 SKU；麻-黑条件不成立（无 SKU），麻其余包装仍未探索',
                'single 与 gift 合并到同一 SKU，两条选择路径的证据分别保留；选项按稳定 data-key 排序，展示文案/排序变化不产生新规格',
            ],
            'first_run': first_report,
            'second_run': second_report,
            'checkpoint_contains_token': 'token' in json.dumps(checkpoint),
        }


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    commands = parser.add_subparsers(dest='command', required=True)
    inspect = commands.add_parser('inspect')
    inspect.add_argument('path')
    capture = commands.add_parser('capture')
    capture.add_argument('url')
    capture.add_argument('--db', default='captures.sqlite3')
    capture.add_argument('--objects', default='objects')
    show = commands.add_parser('show')
    show.add_argument('--db', default='captures.sqlite3')
    show.add_argument('--objects', default='objects')
    commands.add_parser('demo')
    explore = commands.add_parser('explore', help='按门户允许的交互探索可达规格组合')
    explore.add_argument('url', help='门户根地址，例如 http://127.0.0.1:8765')
    explore.add_argument('--budget', type=int, default=50,
                         help='本次运行允许的 GET 请求数；用完即停并保留未探索分支')
    explore.add_argument('--state', default='explore-state.json', help='检查点路径（不保存令牌）')
    explore.add_argument('--db', default='captures.sqlite3')
    explore.add_argument('--objects', default='objects')
    commands.add_parser('explore-demo', help='本机门户端到端演练（含预算中断与进程重启恢复）')
    arguments = parser.parse_args()
    if arguments.command == 'inspect':
        result = inspect_document(Path(arguments.path).read_bytes())
    elif arguments.command in {'capture', 'show'}:
        archive = Archive(arguments.db, arguments.objects)
        try:
            if arguments.command == 'capture':
                status, content_type, body = fetch(arguments.url)
                result = {'capture_id': archive.save(arguments.url, status, content_type, body), 'http_status': status}
            else:
                result = archive.rows()
        finally:
            archive.close()
    elif arguments.command == 'explore':
        result = _run_explore(arguments)
    elif arguments.command == 'explore-demo':
        result = _run_explore_demo(arguments)
    else:
        with FixtureServer() as server, tempfile.TemporaryDirectory() as directory:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = 'http://127.0.0.1:' + str(server.server_address[1]) + DEMO_PATH
                status, content_type, body = fetch(url)
                archive = Archive(Path(directory) / 'demo.sqlite3', Path(directory) / 'objects')
                try:
                    capture_id = archive.save(url, status, content_type, body)
                    result = {'capture_id': capture_id, 'http_status': status, 'observation': inspect_document(archive.read(capture_id))}
                finally:
                    archive.close()
            finally:
                server.shutdown()
                thread.join(timeout=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
