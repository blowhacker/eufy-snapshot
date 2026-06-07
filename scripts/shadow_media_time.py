#!/usr/bin/env python3
"""Shadow validation for the media-time resolver (docs step 3).

Read-only. Runs the round-trip invariant over stored detections and reports the
delta distribution, no_anchor count, and provider breakdown. This is the gate:
no caller is moved onto the resolver until the deltas here are within EPS.

Usage:
    python3 scripts/shadow_media_time.py [--db PATH] [--video-dir DIR] [--limit N]

Defaults match the container layout (/app/video/video.db, /app/video).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Allow running from a source checkout without install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wanyard.media_time import EPS, check_detection_round_trip  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/app/video/video.db")
    ap.add_argument("--video-dir", default="/app/video")
    ap.add_argument("--limit", type=int, default=0,
                    help="sample at most N detections (0 = all)")
    args = ap.parse_args()

    video_dir = Path(args.video_dir)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    sql = "SELECT id FROM video_detections ORDER BY id"
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    ids = [r["id"] for r in conn.execute(sql).fetchall()]

    total = len(ids)
    ok = 0
    providers: dict[str, int] = {}
    reasons: dict[str, int] = {}
    buckets = {"<0.01": 0, "0.01-0.05": 0, "0.05-0.25": 0,
               "0.25-1": 0, ">1": 0, "n/a": 0}
    worst: list[tuple[float, int]] = []

    for det_id in ids:
        rt = check_detection_round_trip(conn, video_dir, det_id)
        if rt.ok:
            ok += 1
        providers[rt.provider] = providers.get(rt.provider, 0) + 1
        reasons[rt.reason] = reasons.get(rt.reason, 0) + 1
        d = rt.delta
        if d is None:
            buckets["n/a"] += 1
        elif d < 0.01:
            buckets["<0.01"] += 1
        elif d < 0.05:
            buckets["0.01-0.05"] += 1
        elif d < 0.25:
            buckets["0.05-0.25"] += 1
        elif d < 1.0:
            buckets["0.25-1"] += 1
        else:
            buckets[">1"] += 1
            worst.append((d, det_id))

    conn.close()

    print(f"detections checked : {total}")
    print(f"round-trip ok (<{EPS}) : {ok}  ({100*ok/total:.1f}%)" if total else "no detections")
    print(f"providers          : {providers}")
    print(f"reasons            : {reasons}")
    print("delta buckets      :")
    for k, v in buckets.items():
        print(f"    {k:>10} : {v}")
    if worst:
        worst.sort(reverse=True)
        print("worst deltas (delta, det_id):")
        for d, i in worst[:10]:
            print(f"    {d:.3f}  #{i}")

    # Gate: fail if any usable detection exceeds EPS.
    failures = buckets["0.05-0.25"] + buckets["0.25-1"] + buckets[">1"]
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
