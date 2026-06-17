#!/usr/bin/env python3
"""
build_points.py — Download every Warhammer 40,000 faction from the live
Munitorum Field Manual (mfm.warhammer-community.com) and emit a points.json
matching the CMonnin/list_builder schema:

{
  "lastUpdated": "YYYY-MM-DD",
  "factions": {
    "<Faction Name>": {
      "datasheets":   { "<Unit Name>": { "options": [ {"models": int, "points": int}, ... ] } },
      "enhancements": { "<Detachment Name>": { "<Enhancement Name>": int, ... } }
    }
  }
}

Unit/detachment names arrive ALL-CAPS in the site's RSC payload, while the target
schema uses Games Workshop's idiosyncratic casing ("Arco-flagellants",
"Brother-Captain"). That casing can't be reproduced by rule, so we use an existing
points.json as a casing dictionary (--reference) and fall back to a heuristic
title-case (and warn) for any unit the reference doesn't contain.

Usage:
    python build_points.py --reference points.json --output points.json
    python build_points.py --reference points.json --local-dir ./rsc_txt   # parse saved .txt files instead of downloading
"""
import re, json, sys, gzip, argparse, datetime, time, urllib.request, urllib.error, pathlib

BASE = "https://mfm.warhammer-community.com/en"

# slug -> exact faction display name (must match the reference schema's keys).
# If a faction 404s, fix its slug here (grab the real slug from the site's nav).
FACTIONS = {
    "adepta-sororitas":    "Adepta Sororitas",
    "adeptus-custodes":    "Adeptus Custodes",
    "adeptus-mechanicus":  "Adeptus Mechanicus",
    "aeldari":             "Aeldari",
    "astra-militarum":     "Astra Militarum",
    "chaos-daemons":       "Chaos Daemons",
    "chaos-knights":       "Chaos Knights",
    "chaos-space-marines": "Chaos Space Marines",
    "death-guard":         "Death Guard",
    "deathwatch":          "Deathwatch",
    "drukhari":            "Drukhari",
    "emperors-children":   "Emperor\u2019s Children",
    "genestealer-cults":   "Genestealer Cults",
    "grey-knights":        "Grey Knights",
    "imperial-agents":     "Imperial Agents",
    "imperial-knights":    "Imperial Knights",
    "leagues-of-votann":   "Leagues Of Votann",
    "necrons":             "Necrons",
    "orks":                "Orks",
    "space-marines":       "Space Marines",
    "space-wolves":        "Space Wolves",
    "tau-empire":          "T\u2019au Empire",
    "thousand-sons":       "Thousand Sons",
    "tyranids":            "Tyranids",
    "world-eaters":        "World Eaters",
}

# ---- RSC payload regexes (validated against the live Adepta Sororitas page) ----
RE_PTS_DEF  = re.compile(r'^([0-9a-f]+):\["\$","span",null,\{"children":"(\d+) pts"\}\]', re.M)
RE_UNIT     = re.compile(r'bg-slate-500 dark:bg-slate-800 font-bold text-xl text-white","children":"([^"]+)"')
RE_COST     = re.compile(r'\[false,"([^"]+)"\]\}\],"\$L([0-9a-f]+)"')
RE_DETACH   = re.compile(r'"className":"text-xl break-all","children":"([^"]+)"')
RE_ENH      = re.compile(r'"children":"([^"]+)"\}\],"\$L([0-9a-f]+)"')

SMALL = {"of", "the", "and", "with", "to", "for", "in", "on", "or", "a", "an",
         "from", "into", "at", "by"}


def title_case(name):
    """Heuristic fallback for names not found in the reference dictionary."""
    words = name.split(" ")
    out = []
    for i, w in enumerate(words):
        low = w.lower()
        out.append(low if (low in SMALL and i != 0) else (w[:1].upper() + w[1:].lower()))
    return " ".join(out)


def parse_payload(text):
    """Return {'units': [(NAME, [(models_int, pts_int)..])], 'detachments': [(NAME, [(enh, pts)..])]}."""
    ref_pts = {m.group(1): int(m.group(2)) for m in RE_PTS_DEF.finditer(text)}

    units = []
    um = list(RE_UNIT.finditer(text))
    for i, m in enumerate(um):
        block = text[m.end(): um[i + 1].start() if i + 1 < len(um) else len(text)]
        opts = []
        for c in RE_COST.finditer(block):
            ref = c.group(2)
            if ref in ref_pts:
                mc = re.match(r"(\d+)", c.group(1))
                if mc:
                    opts.append((int(mc.group(1)), ref_pts[ref]))
        units.append((m.group(1), opts))

    detach = []
    dm = list(RE_DETACH.finditer(text))
    for i, m in enumerate(dm):
        block = text[m.end(): dm[i + 1].start() if i + 1 < len(dm) else len(text)]
        enh = []
        for e in RE_ENH.finditer(block):
            ref = e.group(2)
            if ref in ref_pts:
                enh.append((e.group(1), ref_pts[ref]))
        if enh:
            detach.append((m.group(1), enh))
    return {"units": units, "detachments": detach}


def load_reference(path):
    """Build UPPER(name) -> canonical-name maps from an existing points.json."""
    data = json.load(open(path, encoding="utf-8"))
    ds_by_faction, det_by_faction = {}, {}
    ds_global, det_global = {}, {}
    for fname, fac in data.get("factions", {}).items():
        dmap = {k.upper(): k for k in fac.get("datasheets", {})}
        emap = {k.upper(): k for k in fac.get("enhancements", {})}
        ds_by_faction[fname] = dmap
        det_by_faction[fname] = emap
        ds_global.update(dmap)
        det_global.update(emap)
    return ds_by_faction, det_by_faction, ds_global, det_global


def fetch(slug, retries=3):
    url = f"{BASE}/{slug}?_rsc=build"
    req = urllib.request.Request(url, headers={
        "RSC": "1",
        "User-Agent": "Mozilla/5.0",
        "Accept-Encoding": "gzip",          # avoid brotli; stdlib has no decoder
    })
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            last = e
        except Exception as e:                # noqa
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise last


def build_faction(fname, payload, refs):
    ds_byf, det_byf, ds_g, det_g = refs
    dmap, emap = ds_byf.get(fname, {}), det_byf.get(fname, {})
    warnings = []

    datasheets = {}
    for raw_name, opts in payload["units"]:
        canon = dmap.get(raw_name.upper()) or ds_g.get(raw_name.upper())
        if not canon:
            canon = title_case(raw_name)
            warnings.append(f"  [new datasheet] {fname}: '{raw_name}' -> '{canon}'")
        datasheets[canon] = {"options": [{"models": m, "points": p} for m, p in opts]}

    enhancements = {}
    for raw_det, enh in payload["detachments"]:
        canon = emap.get(raw_det.upper()) or det_g.get(raw_det.upper())
        if not canon:
            canon = title_case(raw_det)
            warnings.append(f"  [new detachment] {fname}: '{raw_det}' -> '{canon}'")
        # enhancement names already arrive in proper case from the RSC payload
        enhancements[canon] = {ename: pts for ename, pts in enh}

    return {"datasheets": datasheets, "enhancements": enhancements}, warnings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reference", help="existing points.json used for exact name casing")
    ap.add_argument("--output", default="points.json")
    ap.add_argument("--local-dir", help="parse <slug>.txt files from this dir instead of downloading")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    args = ap.parse_args()

    refs = load_reference(args.reference) if args.reference else ({}, {}, {}, {})
    if not args.reference:
        print("WARNING: no --reference given; falling back to heuristic casing for ALL names.", file=sys.stderr)

    factions_out, all_warnings = {}, []
    for slug, fname in FACTIONS.items():
        try:
            if args.local_dir:
                text = pathlib.Path(args.local_dir, f"{slug}.txt").read_text(encoding="utf-8", errors="replace")
            else:
                text = fetch(slug)
                time.sleep(args.delay)
        except urllib.error.HTTPError as e:
            print(f"SKIP {slug}: HTTP {e.code} (check the slug)", file=sys.stderr)
            continue
        except FileNotFoundError:
            print(f"SKIP {slug}: no local file {slug}.txt", file=sys.stderr)
            continue
        except Exception as e:                # noqa
            print(f"SKIP {slug}: {e}", file=sys.stderr)
            continue

        payload = parse_payload(text)
        built, warns = build_faction(fname, payload, refs)
        factions_out[fname] = built
        all_warnings += warns
        print(f"OK  {fname:22s} units={len(built['datasheets']):3d} "
              f"detachments={len(built['enhancements']):2d}", file=sys.stderr)

    out = {"lastUpdated": datetime.date.today().isoformat(), "factions": factions_out}
    json.dump(out, open(args.output, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    print(f"\nWrote {args.output}: {len(factions_out)} factions", file=sys.stderr)
    if all_warnings:
        print(f"\n{len(all_warnings)} name(s) not in reference (heuristic casing applied):", file=sys.stderr)
        print("\n".join(all_warnings), file=sys.stderr)


if __name__ == "__main__":
    main()
