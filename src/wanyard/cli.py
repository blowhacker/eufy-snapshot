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
    if args.command == "gen-go2rtc":
        return cmd_gen_go2rtc(args, config)
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
    gen_go2rtc = sub.add_parser("gen-go2rtc", help="generate go2rtc config from configured sources")
    gen_go2rtc.add_argument("--out", default="/run/go2rtc/go2rtc.yaml",
                            help="output path for go2rtc config (default: /run/go2rtc/go2rtc.yaml)")
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
    from .native_hls import hls_port

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
    config_lines.append(f"hlsAddress: :{hls_port()}")
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

    go2rtc_host = (os.environ.get("WANYARD_GO2RTC_HOST", "").strip() or "go2rtc")
    for source in enabled_sources:
        if resolve_rtsp_url(source, direct=True) is None:
            logging.warning("skipping source %s: resolve_rtsp_url returned None", source.id)
            continue
        # Source from go2rtc, not the camera: go2rtc is the single ingest (it
        # pulls the camera once and also serves the instant WebRTC wall). mediamtx
        # reads go2rtc's RTSP for recording/BITC — no extra camera pull.
        config_lines.append(f"  {source.id}:")
        config_lines.append(f"    source: rtsp://{go2rtc_host}:8554/{source.id}")
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


def cmd_gen_go2rtc(args, config: AppConfig) -> int:
    # go2rtc is the single camera ingest: it reads each camera directly, serves
    # RTSP downstream to mediamtx (recording/BITC) and WebRTC to the wall. Reading
    # direct + a warm producer means new WebRTC viewers paint near-instantly.
    source_db = SourceDB(config.db_path) if config.db_path else None
    if not source_db:
        print("error: db_path not configured", file=sys.stderr)
        return 1

    sources = [s for s in source_db.to_source_configs() if s.type == "rtsp" and s.enabled]
    port = (os.environ.get("WANYARD_GO2RTC_WEBRTC_PORT", "").strip() or "8555")
    hosts = [h.strip() for h in os.environ.get("WANYARD_WEBRTC_ADDITIONAL_HOSTS", "").split(",") if h.strip()]

    lines = []
    lines.append("api:")
    lines.append('  listen: ":1984"')          # internal; WHEP is proxied by the app
    lines.append("log:")
    lines.append("  level: error")
    lines.append("webrtc:")
    lines.append(f'  listen: ":{port}"')
    # ICE candidates the browser will try. go2rtc runs on the docker bridge so it
    # can't see the host's LAN IP — advertise reachable hosts explicitly via
    # WANYARD_WEBRTC_ADDITIONAL_HOSTS (e.g. the LAN IP for same-network browsers),
    # PLUS stun for the public IP (external / properly-forwarded access). With no
    # hosts set, stun alone works for forwarded public access.
    lines.append("  candidates:")
    for h in hosts:
        lines.append(f"    - {h}:{port}")
    lines.append(f"    - stun:{port}")
    # Preload keeps go2rtc connected to each source on startup (warm producer),
    # so the last keyframe is always buffered and new WebRTC viewers paint
    # instantly instead of waiting for the next IDR. video-only (wall is muted).
    # This is what makes Frigate's go2rtc live instant. (go2rtc >= 1.9.11.)
    lines.append("preload:")
    for s in sources:
        lines.append(f"  {s.id}: video")
    from .capture import resolve_rtsp_url
    lines.append("streams:")
    for s in sources:
        # go2rtc is the SOLE camera ingest: it reads the camera directly (the
        # configured URL, whatever it is — no stream-name assumptions), serves
        # RTSP downstream to mediamtx (recording/BITC) AND WebRTC to the wall.
        # Direct read = go2rtc's RTCP keyframe request (PLI) reaches the camera →
        # instant first frame for new viewers, at near-zero latency (Frigate's
        # model). One pull per camera, same budget as the old mediamtx ingest.
        url = resolve_rtsp_url(s, direct=True)
        if not url:
            continue
        lines.append(f"  {s.id}: {url}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logging.info("wrote go2rtc config to %s", args.out)
    return 0


def _serve(app, config: AppConfig) -> None:
    import os
    from hypercorn.asyncio import serve
    from hypercorn.config import Config as HConfig

    # Bind port: WANYARD_WEB_PORT env wins over config.yaml. The compose port
    # mapping is already driven by WANYARD_WEB_PORT, so per-deploy ports (e.g.
    # staging 8092) live entirely in the gitignored .env — no local edit to the
    # tracked config.yaml that a `git reset --hard` would clobber.
    port = config.web.port
    raw = (os.environ.get("WANYARD_WEB_PORT") or "").strip()
    if raw:
        try:
            port = int(raw)
        except ValueError:
            pass

    hcfg = HConfig()
    hcfg.loglevel = "WARNING"
    hcfg.accesslog = None
    hcfg.bind = [f"{config.web.host}:{port}"]

    print(f"Serving on http://{config.web.host}:{port}")
    try:
        import uvloop
        uvloop.install()
    except ImportError:
        pass
    asyncio.run(serve(app, hcfg))
