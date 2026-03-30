"""Enrich API docs with real type/default values using subprocess isolation.

Each class is introspected in a separate subprocess to prevent segfaults
from crashing the main process.

Usage: /usr/bin/python3 scripts/enrich_docs.py
"""

import json
import os
import subprocess
import sys

YADE_PY_PATH = '/home/yusong/yade-src/install/lib/x86_64-linux-gnu/yade-2026-03-12.git-ed38a85/py'
DOCS_BASE = os.path.join(os.path.dirname(__file__), '..', 'src', 'yade_mcp', 'knowledge', 'resources', 'python_api_docs')

SKIP_ATTRS = {
    'dict', 'clone', 'dispatch', 'dumps', 'loads', 'updateAttrs', 'postLoad',
    'dispFunctor', 'dispIndex', 'dead', 'execCount', 'execTime', 'label',
    'ompThreads', 'timingDeltas', 'bases', 'id', 'firstIterRun', 'iterLast',
    'realLast', 'virtLast', 'nDone',
}

INTROSPECT_SCRIPT = '''
import sys, json
sys.path.insert(0, "{yade_path}")
import yade.wrapper

skip = {skip_attrs}

try:
    cls = getattr(yade.wrapper, "{cls_name}")
    obj = cls()
    attrs = {{}}
    for a in dir(obj):
        if a.startswith("_") or a in skip:
            continue
        try:
            val = getattr(obj, a)
            if callable(val):
                continue
            d = repr(val)
            if len(d) > 80:
                d = d[:80]
            attrs[a] = {{"type": type(val).__name__, "default": d}}
        except:
            pass
    print(json.dumps(attrs))
except Exception as e:
    print(json.dumps({{"_error": str(e)}}))
'''


def introspect_class(cls_name):
    """Introspect a single class in a subprocess. Returns attr dict or None."""
    code = INTROSPECT_SCRIPT.format(
        yade_path=YADE_PY_PATH,
        cls_name=cls_name,
        skip_attrs=repr(SKIP_ATTRS),
    )
    try:
        result = subprocess.run(
            ['/usr/bin/python3', '-c', code],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        # Find the JSON line (skip YADE banner)
        for line in result.stdout.strip().split('\n'):
            line = line.strip()
            if line.startswith('{'):
                data = json.loads(line)
                if '_error' in data:
                    return None
                return data
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    return None


def enrich_doc(doc_path, cls_name):
    """Enrich a doc file with real type/default values."""
    with open(doc_path, 'r') as f:
        doc = json.load(f)

    attrs = doc.get('attributes', [])
    needs_enrichment = any(a.get('type') == 'auto' or a.get('default') == '' for a in attrs)
    if not needs_enrichment:
        return False

    real_attrs = introspect_class(cls_name)
    if not real_attrs:
        return False

    updated = False
    for attr in attrs:
        name = attr['name']
        if name in real_attrs:
            real = real_attrs[name]
            if attr.get('type') == 'auto':
                attr['type'] = real['type']
                updated = True
            if attr.get('default') == '':
                attr['default'] = real['default']
                updated = True

    if updated:
        with open(doc_path, 'w') as f:
            json.dump(doc, f, indent=2)

    return updated


def main():
    enriched = 0
    failed = 0
    skipped = 0
    total = 0

    for root, dirs, files in os.walk(DOCS_BASE):
        for f in sorted(files):
            if not f.endswith('.json') or f == 'index.json':
                continue
            total += 1
            cls_name = f[:-5]
            doc_path = os.path.join(root, f)

            result = enrich_doc(doc_path, cls_name)
            if result is True:
                enriched += 1
                print(f'  enriched: {cls_name}')
            elif result is False:
                skipped += 1
            else:
                failed += 1

    print(f'\nDone: {enriched} enriched, {skipped} skipped (already rich), {failed} failed, {total} total')


if __name__ == '__main__':
    main()
