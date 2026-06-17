#!/usr/bin/env python3
"""Parse the GW Munitorum Field Manual PDF and extract points data as JSON."""

import argparse
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from datetime import date

import pdfplumber
import requests

# Regex patterns — dot leaders may be actual dots or U+FFFD replacement chars
_DOTS = r'[.�\s]+'
DATASHEET_ENTRY_RE = re.compile(
    r'(\d+)\s*models?' + _DOTS + r'(\d+)\s*pts'
)
NAMED_ENTRY_RE = re.compile(
    r'(\d+)\s+(.+?)' + _DOTS + r'(\d+)\s*pts'
)
ENHANCEMENT_ENTRY_RE = re.compile(
    r'(.+?)[.�]{2,}\s*\+?(\d+)\s*pts'
)
FACTION_HEADER_RE = re.compile(
    r'^(CODEX|INDEX|ALPHA ANVIL|OMEGA ANVIL):\s*(.+)',
    re.IGNORECASE,
)
FORGE_WORLD_RE = re.compile(r'FORGE\s*WORLD\s*POINTS\s*VALUES', re.IGNORECASE)
DETACHMENT_HEADER_RE = re.compile(r'DETACHMENT\s*ENHANCEMENTS', re.IGNORECASE)


def download_pdf(url: str) -> str:
    """Download PDF from URL and return path to temp file."""
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    tmp = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
    try:
        tmp.write(resp.content)
        tmp.close()
    except Exception:
        os.unlink(tmp.name)
        raise
    return tmp.name


def title_case(name: str) -> str:
    """Convert all-caps faction name to title case."""
    # Split on spaces and title-case each word
    return ' '.join(word.capitalize() for word in name.split())


COL_BOUNDARIES = [0, 228, 378, 610]

FULL_WIDTH_RE = re.compile(
    r'^(CODEX|INDEX|ALPHA ANVIL|OMEGA ANVIL)\s*:', re.IGNORECASE
)


def extract_page_lines(page) -> list[str]:
    """Extract text from a PDF page, splitting the fixed 2-or-3 column layout
    into sequential lines (left column first, then middle, then right).

    Column gutters in this PDF sit at approximately x=228 and x=378.
    Faction headers span the full page width and are emitted first unsplit.
    """
    words = page.extract_words(keep_blank_chars=False, x_tolerance=3, y_tolerance=3)
    if not words:
        return []

    # Group words by row first to detect full-width header rows
    row_map: dict[int, list] = defaultdict(list)
    for w in words:
        row_map[round(w['top'])].append(w)

    full_width_rows: set[int] = set()
    all_lines: list[str] = []

    # Emit full-width rows (faction headers) in page order first
    for y in sorted(row_map.keys()):
        row_words = sorted(row_map[y], key=lambda w: w['x0'])
        line = ' '.join(w['text'] for w in row_words)
        if FULL_WIDTH_RE.match(line):
            all_lines.append(line)
            full_width_rows.add(y)

    # Now split remaining words into columns
    def word_col(w):
        x = w['x0']
        for i in range(len(COL_BOUNDARIES) - 1):
            if COL_BOUNDARIES[i] <= x < COL_BOUNDARIES[i + 1]:
                return i
        return len(COL_BOUNDARIES) - 2

    col_row_map: dict[tuple[int, int], list] = defaultdict(list)
    for w in words:
        y = round(w['top'])
        if y in full_width_rows:
            continue
        col_row_map[(word_col(w), y)].append(w)

    for col_idx in range(len(COL_BOUNDARIES) - 1):
        col_keys = sorted(
            [k for k in col_row_map if k[0] == col_idx], key=lambda k: k[1]
        )
        for key in col_keys:
            row_words = sorted(col_row_map[key], key=lambda w: w['x0'])
            line = ' '.join(w['text'] for w in row_words)
            all_lines.append(line)

    return all_lines


def parse_pdf(pdf_path: str) -> dict:
    """Parse the Munitorum Field Manual PDF and return structured data."""
    factions: dict = {}
    current_faction: str | None = None
    current_datasheet: str | None = None
    in_enhancements: bool = False
    current_detachment: str | None = None
    pending_name: str | None = None
    pending_models: int | None = None

    warnings: list[str] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            lines = extract_page_lines(page)

            for line_s in lines:
                line_s = line_s.strip()
                if not line_s:
                    continue

                # --- Faction header ---
                m = FACTION_HEADER_RE.match(line_s)
                if m:
                    current_faction = title_case(m.group(2))
                    current_datasheet = None
                    in_enhancements = False
                    current_detachment = None
                    if current_faction not in factions:
                        factions[current_faction] = {
                            'datasheets': {},
                            'enhancements': {},
                        }
                    continue

                # --- Detachment Enhancements section ---
                if DETACHMENT_HEADER_RE.search(line_s):
                    in_enhancements = True
                    current_datasheet = None
                    current_detachment = None
                    continue

                # --- Forge World header ---
                if FORGE_WORLD_RE.search(line_s):
                    in_enhancements = False
                    current_datasheet = None
                    continue

                # --- Datasheet entry: "N model(s)..........NNN pts" ---
                m = DATASHEET_ENTRY_RE.match(line_s)
                if m and current_faction and current_datasheet and not in_enhancements:
                    models = int(m.group(1))
                    points = int(m.group(2))
                    factions[current_faction]['datasheets'][current_datasheet]['options'].append(
                        {'models': models, 'points': points}
                    )
                    continue

                # --- Named/mixed entry: "1 Spanner and 4 Burna Boyz ...60 pts"
                #     or "3 Wolf Guard Headtakers ......85 pts"
                m = NAMED_ENTRY_RE.match(line_s)
                if m and current_faction and current_datasheet and not in_enhancements:
                    models = int(m.group(1))
                    points = int(m.group(3))
                    factions[current_faction]['datasheets'][current_datasheet]['options'].append(
                        {'models': models, 'points': points}
                    )
                    continue

                # --- Continuation entry: "and 3 Hunting Wolves ..110 pts" ---
                m_enh = ENHANCEMENT_ENTRY_RE.match(line_s)
                if (m_enh and current_faction and current_datasheet
                        and not in_enhancements and line_s.startswith('and ')):
                    pts = int(m_enh.group(2))
                    if pending_models is not None:
                        factions[current_faction]['datasheets'][current_datasheet]['options'].append(
                            {'models': pending_models, 'points': pts}
                        )
                        pending_models = None
                    else:
                        opts = factions[current_faction]['datasheets'][current_datasheet]['options']
                        if opts:
                            opts[-1]['points'] = pts
                    continue

                # --- Enhancement entry: "Name..........NN pts" ---
                if in_enhancements and current_faction and current_detachment:
                    if m_enh:
                        name = m_enh.group(1).strip()
                        pts = int(m_enh.group(2))
                        factions[current_faction]['enhancements'][current_detachment][name] = pts
                        continue

                if not current_faction:
                    continue

                # Skip lines that look like rule/explanatory text
                if any(w in line_s.lower() for w in [
                    'army faction', 'agents of the', 'points values',
                    'imperium,', 'your army', 'doing so',
                ]):
                    continue

                # Skip page numbers and bullet lines
                if (re.match(r'^\d+$', line_s)
                        or re.match(r'^[•\-–—]$', line_s)
                        or line_s.startswith(('•', '•', '–', '—'))):
                    continue

                # --- Multi-line entry prefix: "3 Wolf Guard Headtakers" (digit-prefixed, no pts) ---
                if (
                    not in_enhancements
                    and current_datasheet
                    and re.match(r'^\d+\s+', line_s)
                    and not DATASHEET_ENTRY_RE.match(line_s)
                    and not NAMED_ENTRY_RE.match(line_s)
                ):
                    pending_models = int(line_s.split()[0])
                    continue

                # --- Potential datasheet name ---
                if (
                    not in_enhancements
                    and not DATASHEET_ENTRY_RE.match(line_s)
                    and not ENHANCEMENT_ENTRY_RE.match(line_s)
                    and not NAMED_ENTRY_RE.match(line_s)
                    and len(line_s) < 60
                    and not re.match(r'^\d+\s*\d+\s*\d+', line_s)
                    and not line_s.isupper()
                ):
                    first_word = line_s.split()[0].lower()
                    starts_with_prep = first_word in ('with', 'and', 'in', 'of')

                    # Check if this is a continuation of a multi-line name
                    if starts_with_prep:
                        base = pending_name or current_datasheet
                        if base:
                            merged = f'{base} {line_s}'
                            if pending_name:
                                pending_name = None
                            else:
                                factions[current_faction]['datasheets'].pop(base, None)
                            current_datasheet = merged
                            factions[current_faction]['datasheets'][current_datasheet] = {
                                'options': []
                            }
                            continue

                    if current_datasheet:
                        prev = current_datasheet
                        if prev.endswith((' with', ' of', ' in', ' and', ' the', ' a')):
                            merged = f'{prev} {line_s}'
                            opts = factions[current_faction]['datasheets'].pop(prev, {}).get('options', [])
                            current_datasheet = merged
                            factions[current_faction]['datasheets'][current_datasheet] = {
                                'options': opts
                            }
                            continue

                    pending_name = None

                    # Don't overwrite an existing datasheet that already has options
                    existing = factions[current_faction]['datasheets'].get(line_s)
                    if existing and existing['options']:
                        pending_name = line_s
                        continue
                    current_datasheet = line_s
                    factions[current_faction]['datasheets'][current_datasheet] = {
                        'options': []
                    }

                # --- Detachment name ---
                elif (
                    in_enhancements
                    and not ENHANCEMENT_ENTRY_RE.match(line_s)
                    and not DATASHEET_ENTRY_RE.match(line_s)
                    and len(line_s) < 50
                    and not re.match(r'^\d+$', line_s)
                ):
                    current_detachment = line_s
                    if current_detachment not in factions[current_faction]['enhancements']:
                        factions[current_faction]['enhancements'][current_detachment] = {}

    return factions, warnings


def validate_data(factions: dict) -> list[str]:
    """Validate the extracted data. Return list of warning messages."""
    issues: list[str] = []

    for faction_name, faction_data in factions.items():
        # Must have at least one datasheet
        if not faction_data.get('datasheets'):
            issues.append(f'{faction_name}: has no datasheets')

        for ds_name, ds_data in faction_data.get('datasheets', {}).items():
            options = ds_data.get('options', [])
            if not options:
                issues.append(f'{faction_name} / {ds_name}: has no options')
                continue
            for i, opt in enumerate(options):
                if not isinstance(opt.get('models'), int) or opt['models'] <= 0:
                    issues.append(
                        f'{faction_name} / {ds_name} option {i}: invalid models value {opt.get("models")}'
                    )
                if not isinstance(opt.get('points'), int) or opt['points'] <= 0:
                    issues.append(
                        f'{faction_name} / {ds_name} option {i}: invalid points value {opt.get("points")}'
                    )

        for det_name, enh_data in faction_data.get('enhancements', {}).items():
            for enh_name, pts in enh_data.items():
                if not isinstance(pts, int) or pts <= 0:
                    issues.append(
                        f'{faction_name} / {det_name} / {enh_name}: invalid points value {pts}'
                    )

    return issues


def main():
    parser = argparse.ArgumentParser(description='Parse GW Munitorum Field Manual PDF and extract points data.')
    parser.add_argument('--url', required=True, help='URL to the Munitorum Field Manual PDF')
    parser.add_argument('--output', required=True, help='Output path for points.json')
    args = parser.parse_args()

    # Download
    print(f'Downloading PDF from {args.url}...')
    pdf_path = download_pdf(args.url)

    try:
        # Parse
        print('Parsing PDF...')
        factions, warnings = parse_pdf(pdf_path)

        # Validate
        issues = validate_data(factions)
        if issues:
            print('Validation issues found:')
            for issue in issues:
                print(f'  - {issue}')
            print('Aborting — fix the issues before proceeding.')
            sys.exit(1)

        if warnings:
            print('Parsing warnings:')
            for w in warnings:
                print(f'  - {w}')

        # Build output
        output = {
            'lastUpdated': date.today().isoformat(),
            'factions': factions,
        }

        # Write
        os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
            f.write('\n')

        # Summary stats
        total_factions = len(factions)
        total_datasheets = sum(len(f['datasheets']) for f in factions.values())
        total_enhancements = sum(
            len(det) for f in factions.values() for det in f.get('enhancements', {}).values()
        )
        print(f'✓ Success: {total_factions} factions, {total_datasheets} datasheets, {total_enhancements} enhancements')

    finally:
        os.unlink(pdf_path)


if __name__ == '__main__':
    main()
