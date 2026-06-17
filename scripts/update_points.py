#!/usr/bin/env python3
"""Parse the GW Munitorum Field Manual PDF and extract points data as JSON."""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import date

import pdfplumber
import requests

# Regex patterns
DATASHEET_ENTRY_RE = re.compile(
    r'(\d+)\s*model[s]?\.{2,}\s*(\d+)\s*pts'
)
ENHANCEMENT_ENTRY_RE = re.compile(
    r'(.+?)\.{2,}\s*(\d+)\s*pts'
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


def parse_pdf(pdf_path: str) -> dict:
    """Parse the Munitorum Field Manual PDF and return structured data."""
    factions: dict = {}
    current_faction: str | None = None
    current_datasheet: str | None = None
    in_enhancements: bool = False
    current_detachment: str | None = None

    warnings: list[str] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue

            lines = text.strip().splitlines()

            for line in lines:
                line_s = line.strip()
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
                    # Forge World datasheets go in the same faction
                    in_enhancements = False
                    current_datasheet = None
                    continue

                # --- Datasheet entry (N model(s)..........NNN pts) ---
                m = DATASHEET_ENTRY_RE.match(line_s)
                if m and current_faction and current_datasheet and not in_enhancements:
                    models = int(m.group(1))
                    points = int(m.group(2))
                    factions[current_faction]['datasheets'][current_datasheet]['options'].append(
                        {'models': models, 'points': points}
                    )
                    continue

                # --- Enhancement entry (Name..........NN pts) ---
                if in_enhancements and current_faction and current_detachment:
                    m = ENHANCEMENT_ENTRY_RE.match(line_s)
                    if m:
                        name = m.group(1).strip()
                        pts = int(m.group(2))
                        factions[current_faction]['enhancements'][current_detachment][name] = pts
                        continue

                # --- Potential datasheet name (dark banner bar) ---
                # Heuristic: a line that doesn't match any pattern and is followed by
                # DATASHEET_ENTRY_RE lines. We detect this by checking if the line
                # looks like a proper noun phrase (title-cased or all-caps, no dots).
                if (
                    current_faction
                    and not in_enhancements
                    and current_datasheet is not None
                    and not DATASHEET_ENTRY_RE.match(line_s)
                    and not ENHANCEMENT_ENTRY_RE.match(line_s)
                    and not line_s.startswith(('•', '•', '–', '—'))
                    and len(line_s) < 60
                    # Must not be a number-only line or a single character
                    and not re.match(r'^\d+$', line_s)
                    and not re.match(r'^[•\-–—]$', line_s)
                ):
                    # This line is between a datasheet and its entry lines,
                    # so it's likely a new datasheet name.
                    # Only treat it as a new datasheet if it looks like a name
                    # (not a rule text line, not a stat line).
                    if (
                        not re.match(r'^\d+\s*\d+\s*\d+', line_s)  # not a stat line
                        and ' ' in line_s  # multi-word
                    ):
                        current_datasheet = line_s
                        factions[current_faction]['datasheets'][current_datasheet] = {
                            'options': []
                        }

                # --- Detachment name (dark banner in enhancements section) ---
                if (
                    in_enhancements
                    and current_faction
                    and not ENHANCEMENT_ENTRY_RE.match(line_s)
                    and current_datasheet is None
                    and len(line_s) < 50
                    and ' ' in line_s
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
