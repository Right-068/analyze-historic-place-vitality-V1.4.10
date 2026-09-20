"""Literal external text and an independently checked release-formula allowlist."""
from __future__ import annotations
import ast
import re
from pathlib import Path
from xml.etree import ElementTree as ET


def decode_spreadsheet_text(value):
    """One-pass OOXML escapes; escaped underscores must not trigger a second decode."""
    return re.sub(r'_x([0-9a-fA-F]{4})_', lambda match: chr(int(match.group(1), 16)), value)


def safe_hyperlink(value):
    from source_identity import trusted_domain
    try:
        trusted_domain(value)
        return True
    except (ValueError, TypeError):
        return False


def _formula_patterns():
    """Read fixed release-source literals, not generated cells or model narratives."""
    tree = ast.parse(Path(__file__).with_name('build_research_workbook.py').read_text(encoding='utf-8-sig'))
    patterns = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith('='):
            patterns.add(re.escape(node.value.lstrip('=')))
        elif isinstance(node, ast.JoinedStr):
            parts = []
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    parts.append(re.escape(part.value))
                elif isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Name) and part.value.id == 'excel_row':
                    parts.append('[1-9][0-9]*')
                else:
                    break
            else:
                pattern = ''.join(parts)
                if pattern.startswith('='):
                    patterns.add(pattern[1:])
    return patterns


def audit_formula_cells(xml, sheet_name):
    namespace = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    patterns = _formula_patterns()
    errors = []
    for cell in ET.fromstring(xml).iter(namespace + 'c'):
        formula = cell.find(namespace + 'f')
        if formula is None:
            continue
        ref = cell.get('r', '')
        allowed_position = ((sheet_name == '主题编码' and re.fullmatch('[F-L][1-9][0-9]*', ref))
            or (sheet_name == '七维评分' and re.fullmatch('[ST][2-8]', ref))
            or (sheet_name == '机器可读汇总' and re.fullmatch('[A-Z]+2', ref)))
        if not allowed_position or not any(re.fullmatch(pattern, formula.text or '') for pattern in patterns):
            errors.append(f'untrusted_spreadsheet_formula:{sheet_name}:{ref}')
    return errors


def audit_external_relationships(archive):
    errors = []
    for name in archive.namelist():
        if name.startswith('xl/externalLinks/') or name.endswith('vbaProject.bin'):
            errors.append('untrusted_spreadsheet_external_part:' + name)
        if not name.endswith('.rels'):
            continue
        for rel in ET.fromstring(archive.read(name)):
            if rel.get('TargetMode') == 'External':
                if not rel.get('Type', '').endswith('/hyperlink') or not safe_hyperlink(rel.get('Target', '')):
                    errors.append('untrusted_spreadsheet_external_relationship:' + name)
    return errors
