#!/usr/bin/env python3
"""校验 Flow_vis 的源码引用，并更新定位行号；只用 Python 标准库。

教学内容保存在 flowdata.js，不从历史文档或私有覆盖中抽取。此工具只更新
refs 中的 line，保留场景、模块与说明。页面运行不需要 Python 或此脚本。

用法：
    python tools/build_flow_data.py
    python tools/build_flow_data.py --check
    python tools/build_flow_data.py --root . --data frontend/public/flow/flowdata.js
    python tools/build_flow_data.py --root /path/to/source --data /path/to/flowdata.js

--root 指待核对的源码根目录；--data / --out 显式路径相对当前工作目录。
默认依次查找源码树中的 docs/flow 和 frontend/public/flow。
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

HEADER = '/* Flow_vis 教学内容；源码引用可由 tools/build_flow_data.py 校验并刷新行号。 */\n'
ASSIGNMENT = re.compile(r'\s*(?:/\*.*?\*/\s*)?window\.FLOW_DATA\s*=\s*(\{.*\})\s*;\s*', re.S)


def python_symbols(source: str) -> dict[str, int]:
    symbols: dict[str, int] = {}

    def walk(body: list[ast.stmt], prefix: str = '') -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + node.name
                symbols[name] = node.lineno
                walk(node.body, name + '.')
            # Routes defined conditionally inside create_app remain in that scope.
            elif isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)):
                walk(node.body, prefix)
                walk(getattr(node, 'orelse', []), prefix)
                walk(getattr(node, 'finalbody', []), prefix)
                for handler in getattr(node, 'handlers', []):
                    walk(handler.body, prefix)

    walk(ast.parse(source).body)
    return symbols


def source_line(path: Path, symbol: str, cache: dict) -> int:
    if path not in cache:
        source = path.read_text(encoding='utf-8')
        cache[path] = python_symbols(source) if path.suffix == '.py' else source
    source = cache[path]
    if isinstance(source, dict):
        if symbol not in source:
            raise ValueError(f'未找到 Python 符号 {symbol}')
        return source[symbol]
    # Current frontend references are declarations, not inferred call relations.
    pattern = re.compile(r'\b(?:class|function|const|let|var)\s+' + re.escape(symbol) + r'\b')
    matches = list(pattern.finditer(source))
    if len(matches) != 1:
        raise ValueError(f'前端声明 {symbol} 匹配 {len(matches)} 次，应为 1 次')
    return source.count('\n', 0, matches[0].start()) + 1


def validate_structure(data: dict) -> None:
    if data.get('schemaVersion') != 2:
        raise ValueError('需要 schemaVersion=2 的 Flow_vis 数据，未修改任何文件')
    refs = data['refs']
    modules = {item['id']: item for item in data['modules']}
    scenes = {item['id']: item for item in data['scenes']}
    if len(modules) != len(data['modules']) or len(scenes) != len(data['scenes']):
        raise ValueError('模块或场景 ID 重复')
    if not modules or not scenes or not refs:
        raise ValueError('模块、场景与源码引用不能为空')
    used = set()
    for module in modules.values():
        used.update(module['refs'])
    for scene in scenes.values():
        if not scene['steps']:
            raise ValueError(f'场景 {scene["id"]} 没有步骤')
        for step in scene['steps']:
            if step['module'] not in modules:
                raise ValueError(f'未知模块 {step["module"]}')
            used.update(step['refs'])
    if missing := used - refs.keys():
        raise ValueError(f'未知源码引用：{sorted(missing)}')
    for source, target in data['connections']:
        if source not in modules or target not in modules:
            raise ValueError(f'连线引用未知模块：{source}, {target}')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1], help='源码根目录')
    parser.add_argument('--data', type=Path, help='现有 flowdata.js')
    parser.add_argument('--out', type=Path, help='输出路径，默认原地更新')
    parser.add_argument('--check', action='store_true', help='只检查源码引用和行号，不写入')
    args = parser.parse_args()
    root = args.root.resolve()
    source = args.data
    if source is None:
        source = next((root / relative for relative in ('docs/flow/flowdata.js', 'frontend/public/flow/flowdata.js') if (root / relative).is_file()), None)
        if source is None:
            parser.error('找不到 flowdata.js，请用 --data 指定')
    source = source.resolve()
    try:
        match = ASSIGNMENT.fullmatch(source.read_text(encoding='utf-8'))
        if not match:
            raise ValueError('flowdata.js 必须为单一 JSON 数据赋值，未修改任何文件')
        data = json.loads(match[1])
        validate_structure(data)
        errors, stale, cache = [], [], {}
        for key, ref in data['refs'].items():
            path = root / ref['path']
            try:
                if Path(ref['path']).is_absolute() or not path.resolve().is_relative_to(root):
                    raise ValueError('引用必须在源码根目录内')
                line = source_line(path, ref['symbol'], cache)
                if ref.get('line') != line:
                    stale.append(f'{key}: {ref.get("line", "缺失")} -> {line}')
                    ref['line'] = line
            except (OSError, SyntaxError, ValueError) as exc:
                errors.append(f'{ref["path"]} # {ref["symbol"]}: {exc}')
        if errors:
            raise ValueError('\n'.join(errors))
        if args.check and stale:
            print('源码引用存在，但定位行号需要刷新：\n' + '\n'.join(stale), file=sys.stderr)
            return 1
        if not args.check:
            target = (args.out or source).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(HEADER + 'window.FLOW_DATA = ' + json.dumps(data, ensure_ascii=False, indent=2) + ';\n', encoding='utf-8')
        print(f'Flow_vis：{len(data["scenes"])} 个场景、{len(data["modules"])} 个模块、{len(data["refs"])} 个源码入口全部有效；' + ('检查通过。' if args.check else f'刷新 {len(stale)} 处行号。'))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'Flow_vis 数据检查失败：{exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
