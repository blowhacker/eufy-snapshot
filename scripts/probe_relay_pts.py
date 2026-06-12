#!/usr/bin/env python3
"""
B0 probe: verify RTP-ts identity across consumers of one mediamtx relay path,
and measure frozen-delta (walltime - rtp_seconds) stability.

Record N consumers for D seconds:
    python probe_relay_pts.py record --url rtsp://host:8554/tapo-garden \
        --consumers 2 --duration 600 --out-dir /tmp/probe

Analyze recorded logs:
    python probe_relay_pts.py analyze --out-dir /tmp/probe

Each consumer writes <out-dir>/consumer_<idx>.csv with columns:
    frame_idx,rtp_ts,walltime

analyze checks:
  1. rtp_ts identity across consumers (same RTP clock -> same per-frame ts).
  2. per-consumer frozen-delta: min(walltime - rtp_ts) over the window is the
     delta candidate; spread = max-min is the wobble it must absorb.
  3. PTS discontinuities (non-monotonic or step >> median interval) within a
     consumer's log -- run record across a relay restart to produce one.

A consumer auto-reconnects on stream error so a forced relay restart shows up
as a discontinuity row rather than killing the run.
"""

import argparse
import csv
import os
import threading
import time

import av


def consumer_loop(url, idx, duration, out_dir, stop_event):
    path = os.path.join(out_dir, f"consumer_{idx}.csv")
    start = time.monotonic()
    frame_idx = 0
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["frame_idx", "rtp_ts", "walltime"])
        while time.monotonic() - start < duration and not stop_event.is_set():
            try:
                container = av.open(url, options={"rtsp_transport": "tcp"})
                stream = next(s for s in container.streams if s.type == "video")
                tb = stream.time_base
                for packet in container.demux(stream):
                    if packet.pts is None:
                        continue
                    rtp_ts = float(packet.pts * tb)
                    walltime = time.time()
                    w.writerow([frame_idx, f"{rtp_ts:.6f}", f"{walltime:.6f}"])
                    f.flush()
                    frame_idx += 1
                    if stop_event.is_set() or time.monotonic() - start > duration:
                        break
                container.close()
            except Exception as e:
                print(f"[consumer {idx}] reconnect after error: {e}")
                time.sleep(1)


def cmd_record(args):
    os.makedirs(args.out_dir, exist_ok=True)
    stop_event = threading.Event()
    threads = [
        threading.Thread(
            target=consumer_loop,
            args=(args.url, i, args.duration, args.out_dir, stop_event),
        )
        for i in range(args.consumers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=args.duration + 30)
    print(f"done: wrote {args.consumers} consumer logs to {args.out_dir}")


def load_csv(path):
    rows = []
    with open(path) as f:
        for row in csv.DictReader(f):
            rows.append((int(row["frame_idx"]), float(row["rtp_ts"]), float(row["walltime"])))
    return rows


def cmd_analyze(args):
    files = sorted(f for f in os.listdir(args.out_dir) if f.startswith("consumer_") and f.endswith(".csv"))
    if not files:
        print("no consumer_*.csv found in", args.out_dir)
        return
    logs = {f: load_csv(os.path.join(args.out_dir, f)) for f in files}

    # 1. rtp_ts identity across consumers
    if len(logs) >= 2:
        names = list(logs)
        ts_sets = [set(round(r[1], 3) for r in logs[n]) for n in names]
        common = set.intersection(*ts_sets)
        total = max(len(s) for s in ts_sets)
        print(
            f"[identity] common rtp_ts across {len(names)} consumers: "
            f"{len(common)}/{total} ({100 * len(common) / total:.1f}%)"
        )

    # 2. frozen-delta stability per consumer
    for name, rows in logs.items():
        deltas = [wt - rtp for _, rtp, wt in rows]
        min_d = min(deltas)
        spread = max(deltas) - min_d
        print(
            f"[delta] {name}: n={len(rows)} min_delta={min_d:.4f}s "
            f"spread(wobble)={spread * 1000:.2f}ms"
        )

    # 3. PTS discontinuity
    for name, rows in logs.items():
        if len(rows) < 3:
            continue
        rtps = [r[1] for r in rows]
        diffs = [b - a for a, b in zip(rtps, rtps[1:])]
        median = sorted(diffs)[len(diffs) // 2]
        jumps = [(i, d) for i, d in enumerate(diffs) if d <= 0 or d > median * 5]
        if jumps:
            print(f"[discontinuity] {name}: {len(jumps)} jump(s), e.g. {jumps[:3]} (median diff={median:.4f}s)")
        else:
            print(f"[discontinuity] {name}: none (median diff={median:.4f}s)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("record")
    pr.add_argument("--url", required=True, help="rtsp://host:8554/<path>")
    pr.add_argument("--consumers", type=int, default=2)
    pr.add_argument("--duration", type=float, default=600, help="seconds")
    pr.add_argument("--out-dir", required=True)
    pr.set_defaults(func=cmd_record)

    pa = sub.add_parser("analyze")
    pa.add_argument("--out-dir", required=True)
    pa.set_defaults(func=cmd_analyze)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
