from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from .config import AppConfig, load_config
from .db import SourceDB
from .runner import CaptureWorker
from .video import VideoSegmentDB


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)s %(message)s")
    config = load_config(args.config)
    if args.command == "serve":
        return cmd_serve(config)
    if args.command == "yolo-serve":
        return cmd_yolo_serve()
    if args.command == "rebuild-events":
        return cmd_rebuild_events(args)
    if args.command == "derive-episodes":
        return cmd_derive_episodes(args)
    if args.command == "gen-mediamtx":
        return cmd_gen_mediamtx(args, config)
    if args.command == "stamp":
        from . import stamper
        return stamper.run()
    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wanyard")
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve",      help="web server + RTSP recording")
    sub.add_parser("yolo-serve", help="YOLO inference + backfill (separate process/container)")
    rebuild = sub.add_parser("rebuild-events", help="rebuild video events from stored detections")
    rebuild.add_argument("--source", default=None, help="source id to rebuild, for example tapo-garden")
    rebuild.add_argument("--since", type=float, default=None, help="Unix timestamp lower bound")
    rebuild.add_argument("--until", type=float, default=None, help="Unix timestamp upper bound")
    rebuild.add_argument("--keep-object-tracks", dest="keep_object_tracks", action="store_true",
                         help="do not clear persisted object tracking state before rebuilding")
    rebuild.add_argument("--keep-vehicle-tracks", dest="keep_object_tracks",
                         action="store_true", help=argparse.SUPPRESS)
    derive = sub.add_parser("derive-episodes", help="derive object episodes from stored detections")
    derive.add_argument("--source", default=None, help="source id to derive, for example tapo-front")
    derive.add_argument("--since", type=float, default=None, help="Unix timestamp lower bound")
    derive.add_argument("--until", type=float, default=None, help="Unix timestamp upper bound")
    derive.add_argument("--keep-tracks", action="store_true",
                        help="append to existing object tracking state instead of clearing it first")
    gen_mediamtx = sub.add_parser("gen-mediamtx", help="generate mediamtx config from configured sources")
    gen_mediamtx.add_argument("--out", default="/run/mediamtx/mediamtx.yml",
                               help="output path for mediamtx config (default: /run/mediamtx/mediamtx.yml)")
    sub.add_parser("stamp", help="BITC stamper: burn world-time into frames, republish <src>-stamped")
    return parser


def cmd_serve(config: AppConfig) -> int:
    from .web import make_app

    source_db      = SourceDB(config.db_path) if config.db_path else None
    video_dir      = Path(os.environ.get("VIDEO_DIR", "video"))
    video_db       = VideoSegmentDB(video_dir / "video.db")
    capture_worker = CaptureWorker(source_db, video_dir, video_db)

    app = make_app(config, source_db=source_db,
                   video_dir=video_dir, video_db=video_db,
                   capture_worker=capture_worker)
    _serve(app, config)
    return 0


def cmd_yolo_serve() -> int:
    from . import yolo_server
    video_dir = Path(os.environ.get("VIDEO_DIR", "video"))
    yolo_server.run(video_dir / "video.db", video_dir)
    return 0


def cmd_rebuild_events(args) -> int:
    from .video import VideoSegmentDB, rebuild_events

    video_dir = Path(os.environ.get("VIDEO_DIR", "video"))
    db = VideoSegmentDB(video_dir / "video.db")
    stats = rebuild_events(
        db,
        source_id=args.source,
        since=args.since,
        until=args.until,
        reset_object_tracks=not args.keep_object_tracks,
    )
    print(
        "rebuilt events:"
        f" segments={stats['segments']}"
        f" with_detections={stats['segments_with_detections']}"
        f" events={stats['events']}"
    )
    return 0


def cmd_derive_episodes(args) -> int:
    from .video import VideoSegmentDB, derive_object_events

    video_dir = Path(os.environ.get("VIDEO_DIR", "video"))
    db = VideoSegmentDB(video_dir / "video.db")
    stats = derive_object_events(
        db,
        source_id=args.source,
        since=args.since,
        until=args.until,
        reset_tracks=not args.keep_tracks,
    )
    sources = ",".join(stats["sources"]) if stats["sources"] else "none"
    print(
        "derived episodes:"
        f" segments={stats['segments']}"
        f" with_detections={stats['segments_with_detections']}"
        f" events={stats['events']}"
        f" sources={sources}"
    )
    return 0


def cmd_gen_mediamtx(args, config: AppConfig) -> int:
    from .capture import resolve_rtsp_url

    source_db = SourceDB(config.db_path) if config.db_path else None
    if not source_db:
        print("error: db_path not configured", file=sys.stderr)
        return 1

    sources = source_db.to_source_configs()
    enabled_sources = [s for s in sources if s.type == "rtsp" and s.enabled]

    # Build mediamtx config YAML
    config_lines = []
    config_lines.append("api: yes")
    config_lines.append("apiAddress: :9997")
    config_lines.append("metrics: yes")
    config_lines.append("metricsAddress: :9998")
    config_lines.append("")
    # Low-latency HLS (fMP4 partial segments) for live view — sub-2s vs the
    # recorder's ~1-4s .ts HLS, and HEVC plays more robustly in fMP4 than TS.
    config_lines.append("hls: yes")
    config_lines.append("hlsAddress: :8888")
    config_lines.append("hlsVariant: lowLatency")
    config_lines.append("hlsAlwaysRemux: yes")
    config_lines.append("hlsAllowOrigin: '*'")
    config_lines.append("")
    config_lines.append("authInternalUsers:")
    config_lines.append("  - user: any")
    config_lines.append("    pass:")
    config_lines.append("    ips: []")
    config_lines.append("    permissions:")
    config_lines.append("      - action: api")
    config_lines.append("      - action: metrics")
    config_lines.append("      - action: read")
    config_lines.append("      - action: publish")
    config_lines.append("      - action: playback")
    config_lines.append("")
    config_lines.append("paths:")

    for source in enabled_sources:
        rtsp_url = resolve_rtsp_url(source, direct=True)
        if rtsp_url is None:
            logging.warning("skipping source %s: resolve_rtsp_url returned None", source.id)
            continue
        config_lines.append(f"  {source.id}:")
        config_lines.append(f"    source: {rtsp_url}")
        config_lines.append("    sourceProtocol: tcp")
        config_lines.append("    sourceOnDemand: yes")
        # BITC stamper republishes the marked stream here (no source = accepts
        # a publisher). Consumers read <id>-stamped post-cutover.
        config_lines.append(f"  {source.id}-stamped: {{}}")

    config_text = "\n".join(config_lines) + "\n"

    # Write to output path
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(config_text, encoding="utf-8")
    logging.info("wrote mediamtx config to %s", args.out)
    return 0


def _serve(app, config: AppConfig) -> None:
    from hypercorn.asyncio import serve
    from hypercorn.config import Config as HConfig

    hcfg = HConfig()
    hcfg.loglevel = "WARNING"
    hcfg.accesslog = None
    hcfg.bind = [f"{config.web.host}:{config.web.port}"]

    print(f"Serving on http://{config.web.host}:{config.web.port}")
    try:
        import uvloop
        uvloop.install()
    except ImportError:
        pass
    asyncio.run(serve(app, hcfg))
