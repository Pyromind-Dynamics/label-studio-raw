#!/usr/bin/env python3
"""Regenerate the label-studio-plugin ConfigMap in the deploy manifests.

The plugin's sources are ordinary files under plugin/pyromind_ls/ so they can be
read, linted and syntax-checked. Every manifest embeds the same ConfigMap, so the
only way to keep the copies in step is to generate that one block.

Only the `data:` block of the `label-studio-plugin` ConfigMap is rewritten. The
documents are spliced as text rather than round-tripped through a YAML parser
because every manifest carries hand-written comments that PyYAML would drop.

Usage:
    python3 plugin/sync_configmap.py            # rewrite the manifests
    python3 plugin/sync_configmap.py --check    # fail if they are out of date
"""

from __future__ import annotations

import pathlib
import sys


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / 'plugin' / 'pyromind_ls'
CONFIGMAP_NAME = 'label-studio-plugin'
MANIFESTS = (
    REPO_ROOT / 'app-deploy.yaml',
    REPO_ROOT / 'prod-webapp' / '03-configmap-label-studio-plugin.yaml',
)
# Manifest order, so a diff of the generated block reads the same way every time.
# The first eight are the order the ConfigMap already used; the portal modules are
# appended so the block stays in one readable sequence.
FILE_ORDER = (
    '__init__.py',
    'apps.py',
    'export_hook.py',
    'export_ui.py',
    'export_ui.js',
    'settings.py',
    'isolation.py',
    'sso.py',
    'portal_api.py',
    'portal_views.py',
    'urls.py',
)
INDENT = '  '


def render_data_block() -> str:
    """Render the ConfigMap body, from `data:` through the last plugin file."""
    names = sorted(path.name for path in PLUGIN_DIR.iterdir() if path.is_file())
    unknown = [name for name in names if name not in FILE_ORDER]
    if unknown:
        raise SystemExit(f'plugin files missing from FILE_ORDER: {unknown}')

    lines = ['data:']
    for name in FILE_ORDER:
        path = PLUGIN_DIR / name
        if not path.is_file():
            continue
        lines.append(f'{INDENT}{name}: |')
        text = path.read_text(encoding='utf-8')
        for line in text.splitlines():
            lines.append(f'{INDENT}{INDENT}{line}' if line else '')
    return '\n'.join(lines) + '\n'


def document_bounds(text: str) -> tuple[int, int]:
    """Return the [start, end) span of the plugin ConfigMap document."""
    marker = f'name: {CONFIGMAP_NAME}\n'
    name_at = text.index(marker)
    # Walk back to the `---` that opens this document and forward to the next one.
    # A manifest may hold the ConfigMap on its own, with no document separator.
    separator = text.rfind('---\n', 0, name_at)
    start = separator + len('---\n') if separator != -1 else 0
    end = text.find('\n---\n', name_at)
    if end == -1:
        end = len(text)
    else:
        end += 1
    return start, end


def data_block_bounds(document: str) -> tuple[int, int]:
    """Return the [start, end) span of the document's `data:` block.

    A ConfigMap's data block runs until the first line that is not blank and not
    indented, which is where any trailing document comments begin. Splicing only
    that span leaves those comments, and every other document, untouched.
    """
    start = document.index('\ndata:\n') + 1
    body_at = start + len('data:\n')
    consumed = 0
    for line in document[body_at:].splitlines(keepends=True):
        stripped = line.rstrip('\n')
        if stripped and not stripped.startswith(' '):
            break
        consumed += len(line)
    return start, body_at + consumed


def rewrite(text: str) -> str:
    start, end = document_bounds(text)
    document = text[start:end]
    if 'kind: ConfigMap\n' not in document:
        raise SystemExit(f'{CONFIGMAP_NAME} document is not a ConfigMap')
    data_start, data_end = data_block_bounds(document)
    return (
        text[:start]
        + document[:data_start]
        + render_data_block()
        + document[data_end:]
        + text[end:]
    )


def main() -> int:
    check = '--check' in sys.argv[1:]
    stale = []
    for manifest in MANIFESTS:
        original = manifest.read_text(encoding='utf-8')
        updated = rewrite(original)
        if updated == original:
            continue
        stale.append(manifest)
        if not check:
            manifest.write_text(updated, encoding='utf-8')
    if check and stale:
        names = ', '.join(str(path.relative_to(REPO_ROOT)) for path in stale)
        print(f'out of date, run plugin/sync_configmap.py: {names}')
        return 1
    for path in stale:
        print(f'updated {path.relative_to(REPO_ROOT)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
