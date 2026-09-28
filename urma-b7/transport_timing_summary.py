#!/usr/bin/env python3
"""Compare B7 READ and SEND/RECV latency envelopes from result manifests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_run(path: Path) -> dict:
    manifest_path = path / "manifest.json" if path.is_dir() else path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    transfer = ((manifest.get("result") or {}).get("transfer") or {})
    timing = ((transfer.get("taskTimingSummary") or {}).get("distribution") or {})
    summary = transfer.get("summary") or {}
    rate = ((summary.get("throughputMiBps") or {}).get("aggregate"))
    read = transfer.get("urmaReadTimelineSummary") or {}
    send = transfer.get("urmaSendRecvTimelineSummary") or {}
    if send.get("observed"):
        kind = "SEND/RECV"
        envelope = (((send.get("duration") or {}).get("receiveBatchEnvelopeNs") or {}).get("medianNs"))
        pwrite = (((send.get("duration") or {}).get("pwriteEnvelopeNs") or {}).get("medianNs"))
    else:
        kind = "READ"
        envelope = (((read.get("duration") or {}).get("readBatchEnvelopeNs") or {}).get("medianNs"))
        pwrite = (((read.get("duration") or {}).get("pwriteEnvelopeNs") or {}).get("medianNs"))

    def median(*names: str):
        for name in names:
            value = (timing.get(name) or {}).get("medianNs")
            if value is not None:
                return value
        return None

    return {
        "run": manifest.get("runId") or manifest_path.parent.name,
        "kind": kind,
        "throughputMiBps": rate,
        "dfgetNs": median("dfgetElapsedNs"),
        "toTransportNs": median("dfgetToFirstTransportStartNs", "dfgetToFirstReadStartNs"),
        "transportToFirstPieceNs": median(
            "firstTransportStartToFirstPieceNs", "firstReadStartToFirstPieceNs"
        ),
        "firstToLastPieceNs": median("firstToLastPieceNs"),
        "tailNs": median("lastPieceToDfgetEndNs"),
        "transportEnvelopeNs": envelope,
        "pwriteEnvelopeNs": pwrite,
    }


def ms(value):
    return float("nan") if value is None else value / 1_000_000


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    rows = [load_run(path) for path in args.runs]
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    print(
        f"{'run':<28} {'kind':<9} {'MiB/s':>9} {'dfget':>8} {'toXport':>8} "
        f"{'Xport->1':>9} {'pieceSpan':>10} {'tail':>8} {'xportEnv':>9} {'pwrEnv':>8}"
    )
    for row in rows:
        rate = row["throughputMiBps"]
        print(
            f"{str(row['run']):<28} {row['kind']:<9} "
            f"{rate if rate is not None else float('nan'):>9.1f} "
            f"{ms(row['dfgetNs']):>8.2f} {ms(row['toTransportNs']):>8.2f} "
            f"{ms(row['transportToFirstPieceNs']):>9.2f} "
            f"{ms(row['firstToLastPieceNs']):>10.2f} {ms(row['tailNs']):>8.2f} "
            f"{ms(row['transportEnvelopeNs']):>9.2f} {ms(row['pwriteEnvelopeNs']):>8.2f}"
        )
    print("note: stage/envelope columns overlap and must not be added; all envelopes use child-process timestamps only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
