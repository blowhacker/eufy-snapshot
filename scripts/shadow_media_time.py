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

from wanyard.media_time import check_detection_round_trip  # noqa: E402


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
    status: dict[str, int] = {}
    alternates = 0
    worst: list[tuple[float, int]] = []

    for det_id in ids:
        rt = check_detection_round_trip(conn, video_dir, det_id)
        status[rt.status] = status.get(rt.status, 0) + 1
        if rt.alternate:
            alternates += 1
        if rt.status == "world_mismatch" and rt.world_delta is not None:
            worst.append((rt.world_delta, det_id))

    conn.close()

    ok = status.get("ok", 0)
    # Anchored real detections = the population the resolver is responsible for.
    anchored = ok + status.get("world_mismatch", 0) + status.get("gap", 0)
    print(f"detections scanned     : {total}")
    print(f"status breakdown       : {status}")
    print(f"  ok                   : {ok}")
    print(f"  world_mismatch       : {status.get('world_mismatch', 0)}  (resolver faults)")
    print(f"  gap                  : {status.get('gap', 0)}  (resolver faults)")
    print(f"  no_anchor            : {status.get('no_anchor', 0)}  (NULL media_epoch, honest)")
    print(f"  sentinel             : {status.get('sentinel', 0)}  (ts_offset<0 markers, skipped)")
    print(f"alternate-segment hits : {alternates}  (boundary duplicates, valid)")
    if anchored:
        print(f"resolver correctness   : {100*ok/anchored:.3f}% of {anchored} anchored detections")
    if worst:
        worst.sort(reverse=True)
        print("worst world-deltas (delta, det_id):")
        for d, i in worst[:10]:
            print(f"    {d:.3f}  #{i}")

    # Gate fails ONLY on resolver faults: world_mismatch or unexpected gap.
    faults = status.get("world_mismatch", 0) + status.get("gap", 0)
    return 1 if faults else 0


if __name__ == "__main__":
    raise SystemExit(main())
