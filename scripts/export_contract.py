"""Writes the ems-edge/1.0 JSON Schema files (contracts/ems-edge/1.0/<message>.schema.json) from the contract's pydantic models (backend/gridlytics/
contract.py), so an edge team builds against a published schema (plan DAT-02, U3 5.8). With --check it writes nothing and exits 1 when a file differs
from the models (the test backend/tests/test_fix_r7_control_edge.py does the same). Run from anywhere: python scripts/export_contract.py [--check]
[--out FOLDER]."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from gridlytics import contract  # noqa: E402

FOLDER = ROOT / "contracts" / "ems-edge" / "1.0"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="compare only: exit 1 when a file is missing or differs")
    ap.add_argument("--out", type=Path, default=FOLDER, help="the folder of the schema files")
    args = ap.parse_args(argv)
    drift = []
    for name, text in contract.schema_files().items():
        path = args.out / name
        if path.exists() and path.read_text(encoding="utf-8") == text:
            continue
        drift.append(name)
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
    if args.check and drift:
        print(f"The schema files differ from the models: {', '.join(drift)}. Run python scripts/export_contract.py", file=sys.stderr)
        return 1
    print(("Up to date" if not drift else f"Wrote {', '.join(drift)}") + f" in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
