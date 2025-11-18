#!/usr/bin/env python3
"""
rename-release-confirmations.py

Usage:
  python rename-release-confirmations.py <file1.pdf> [<file2.pdf> ...]
  python rename-release-confirmations.py --dry-run <file1.pdf> ...

Renames each input PDF to: "Award Date"-"Award Number"-"Date".pdf
Fields are read via parse_pdf from the local parse_pdf module.

Options:
  --dry-run       Show what would be renamed without changing files
  --overwrite     Allow overwriting if the destination file already exists
"""

import argparse
import sys
import re
from pathlib import Path
from typing import Optional, Dict

from parse_pdf import parse_pdf

SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")

def safe_part(s: Optional[str], fallback: str) -> str:
    """
    Make a string safe for filenames:
    - Replace spaces & unsafe chars with '-'
    - Trim repeated dashes/underscores
    - Provide a fallback if empty/None
    """
    if s is None:
        return fallback
    s = s.strip()
    if not s:
        return fallback
    s = s.replace("/", "-").replace("\\", "-")  # prevent directory traversal
    s = SAFE_CHARS_RE.sub("-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-._")
    return s or fallback

def build_target_name(meta: Dict[str, object]) -> str:
    # parse_pdf returns dates as YYYY-MM-DD
    award_date = safe_part(meta.get("Award Date"), "unknown-awarddate")
    award_num  = safe_part(str(meta.get("Award Number") or ""), "unknown-awardnum")
    release_dt = safe_part(meta.get("Release Date"), "unknown-releasedate")
    return f'{award_date}-{award_num}-{release_dt}.pdf'

def unique_path(target: Path) -> Path:
    """
    If target exists, append a numeric suffix before the extension: name-2.pdf, name-3.pdf, ...
    """
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    i = 2
    while True:
        candidate = target.with_name(f"{stem}-{i}{suffix}")
        if not candidate.exists():
            return candidate
        i += 1

def rename_file(src: Path, dry_run: bool = False, overwrite: bool = False) -> Optional[Path]:
    if not src.exists():
        print(f"[WARN] Skipping (not found): {src}", file=sys.stderr)
        return None
    if src.suffix.lower() != ".pdf":
        print(f"[WARN] Skipping (not a PDF): {src}", file=sys.stderr)
        return None

    try:
        meta = parse_pdf(src)
    except Exception as e:
        print(f"[ERROR] Failed to parse {src}: {e}", file=sys.stderr)
        return None

    new_name = build_target_name(meta)
    dst = src.with_name(new_name)

    if not overwrite and src != dst:
        dst = unique_path(dst)

    if src == dst:
        print(f"No need to rename: {src.name}")
    else:
        action = "Would rename" if dry_run else "Renamed"
        print(f'{action}: "{src.name}" -> "{dst.name}"')

    if not dry_run:
        try:
            src.rename(dst)
        except Exception as e:
            print(f"[ERROR] Failed to rename {src} -> {dst}: {e}", file=sys.stderr)
            return None

    return dst

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Rename release confirmation PDFs using parse_pdf metadata.")
    p.add_argument("files", nargs="+", help="PDF files to rename")
    p.add_argument("--dry-run", action="store_true", help="Show planned renames without changing files")
    p.add_argument("--overwrite", action="store_true", help="Allow overwriting if destination exists")
    args = p.parse_args(argv)

    exit_code = 0
    for f in args.files:
        out = rename_file(Path(f), dry_run=args.dry_run, overwrite=args.overwrite)
        if out is None:
            exit_code = 1
    return exit_code

if __name__ == "__main__":
    raise SystemExit(main())
