"""OpenCV projective reconstruction worker for Spatial scenes.

This first visible reconstruction is deliberately relative rather than metric.
It uses matched structure to rectify a camera pair, dense stereo for candidate
correspondences, and an approximate pinhole pose to triangulate a coloured
point cloud. Camera calibration can replace the approximate intrinsics later
without changing the queue, manifest, artifact, or browser-viewer contracts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import fcntl
import gc
import json
import math
import os
from pathlib import Path
import uuid

from . import stereo
from .spatial import SpatialStore


class SpatialReconstructionError(RuntimeError):
    pass


@dataclass
class ProjectiveCloud:
    points: object
    colors: object
    faces: object
    rectified_left: object
    sample_x: object
    sample_y: object
    sample_depth: object
    timestamp: float
    left_camera_id: str
    right_camera_id: str
    metrics: dict
    pose_rotation: object
    pose_translation: object
    left_intrinsic: object
    right_intrinsic: object
    disparity_range: tuple[int, int]


@dataclass
class NeuralCloud:
    points: object
    colors: object
    faces: object
    depth: object
    confidence: object
    timestamp: float
    camera_ids: list[str]
    intrinsic: object
    extrinsic: object
    input_shape: tuple[int, ...]
    gpu_peak_mb: float


def process_next_run(store: SpatialStore, video_db, video_dir: Path) -> bool:
    """Consume at most one persisted job. Returns whether work was found."""
    jobs = store.pending_runs()
    if not jobs:
        return False
    job = jobs[0]
    reconstruct_run(
        store,
        video_db,
        Path(video_dir),
        job["scene_id"],
        job["run_id"],
        job["camera_ids"],
        job.get("feasibility", {}),
    )
    return True


def reconstruct_run(
    store: SpatialStore,
    video_db,
    video_dir: Path,
    scene_id: str,
    run_id: str,
    camera_ids: list[str],
    feasibility: dict | None = None,
) -> dict:
    """Build one run while holding an inter-process ownership lock."""
    lock_path = store.run_directory(scene_id, run_id) / ".reconstruction.lock"
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SpatialReconstructionError(
                f"spatial run {run_id} is already being reconstructed"
            ) from exc
        return _reconstruct_run_locked(
            store, video_db, video_dir, scene_id, run_id, camera_ids, feasibility
        )


def _reconstruct_run_locked(
    store: SpatialStore,
    video_db,
    video_dir: Path,
    scene_id: str,
    run_id: str,
    camera_ids: list[str],
    feasibility: dict | None = None,
) -> dict:
    """Build and publish one queued projective point-cloud preview."""
    engine = os.environ.get("WANYARD_SPATIAL_ENGINE", "vggt").strip().lower()
    kind = "vggt_neural" if engine == "vggt" else "opencv_projective"
    store.update_run(scene_id, run_id, status="running", kind=kind)
    try:
        left_id, right_id = _choose_pair(camera_ids, feasibility or {})
        timestamp = stereo.latest_common_timestamp(video_db, left_id, right_id)
        frames = []
        used_camera_ids = []
        unavailable = []
        for camera_id in camera_ids:
            result = stereo._read_frame(video_db, video_dir, camera_id, timestamp)
            if result.frame is None:
                unavailable.append(f"{camera_id}: {result.status}")
                continue
            used_camera_ids.append(camera_id)
            frames.append(result.frame)
        if left_id not in used_camera_ids or right_id not in used_camera_ids:
            raise SpatialReconstructionError("frame unavailable (" + "; ".join(unavailable) + ")")
        run_dir = store.run_directory(scene_id, run_id)
        if engine == "vggt":
            cloud = build_vggt_cloud(
                frames,
                timestamp=timestamp,
                camera_ids=used_camera_ids,
                model_path=Path(os.environ.get(
                    "WANYARD_SPATIAL_MODEL", "/app/models/vggt/model.pt"
                )),
            )
            artifacts = _publish_vggt_artifacts(run_dir, cloud, camera_ids)
            warnings = [
                "VGGT neural geometry has relative scale; measurements wait for camera calibration."
            ]
        else:
            left_frame = frames[used_camera_ids.index(left_id)]
            right_frame = frames[used_camera_ids.index(right_id)]
            cloud = build_projective_cloud(
                left_frame,
                right_frame,
                timestamp=timestamp,
                left_camera_id=left_id,
                right_camera_id=right_id,
            )
            artifacts = _publish_artifacts(run_dir, cloud, camera_ids)
            warnings = [
                "OpenCV projective geometry: shape is relative; measurements wait for camera calibration."
            ]
        if unavailable:
            warnings.append(
                "Some selected cameras had no readable synchronized frame: "
                + "; ".join(unavailable)
            )
        return store.update_run(
            scene_id,
            run_id,
            status="ready",
            kind=kind,
            artifacts=artifacts,
            stats={
                "points": int(len(cloud.points)),
                "faces": int(len(cloud.faces)),
                "camera_count": len(camera_ids),
                "reconstructed_camera_count": len(used_camera_ids),
                "timestamp": cloud.timestamp,
            },
            warnings=warnings,
        )
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        try:
            store.update_run(
                scene_id, run_id, status="failed", kind=kind, error=message
            )
        except Exception:
            pass
        raise


def build_vggt_cloud(
    frames,
    *,
    timestamp: float,
    camera_ids: list[str],
    model_path: Path,
    max_points: int = 120_000,
) -> NeuralCloud:
    """Infer coherent cameras and dense relative geometry with official VGGT."""
    if len(frames) < 2 or len(frames) != len(camera_ids):
        raise SpatialReconstructionError("VGGT requires at least two named camera frames")
    model_path = Path(model_path)
    if not model_path.is_file():
        raise SpatialReconstructionError(
            f"VGGT model is not installed at {model_path}"
        )
    try:
        import torch
        from vggt.models.vggt import VGGT
        from vggt.utils.pose_enc import pose_encoding_to_extri_intri
    except (ImportError, ModuleNotFoundError) as exc:
        raise SpatialReconstructionError("VGGT runtime is not installed") from exc
    if not torch.cuda.is_available():
        raise SpatialReconstructionError("VGGT requires the CUDA reconstruction worker")

    cv2 = stereo._load_cv2()
    np = stereo._load_numpy()
    images = _preprocess_vggt_frames(cv2, np, torch, frames).to("cuda")
    model = None
    predictions = None
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()
    try:
        model = VGGT(
            enable_camera=True,
            enable_point=True,
            enable_depth=True,
            enable_track=False,
        )
        state = torch.load(model_path, map_location="cpu", weights_only=True)
        model.load_state_dict(state, strict=False)
        del state
        dtype = (
            torch.bfloat16
            if torch.cuda.get_device_capability()[0] >= 8
            else torch.float16
        )
        # Match the official inference path: parameters remain float32 while
        # autocast reduces the large transformer activations. The run lock
        # prevents a second model copy from racing this one onto the GPU.
        model = model.eval().to("cuda")
        with torch.inference_mode(), torch.autocast(device_type="cuda", dtype=dtype):
            predictions = model(images)
        extrinsic, intrinsic = pose_encoding_to_extri_intri(
            predictions["pose_enc"], images.shape[-2:]
        )
        depth = predictions["depth"].squeeze(0).float().cpu().numpy()
        depth_confidence = predictions["depth_conf"].squeeze(0).float().cpu().numpy()
        world_points = predictions["world_points"].squeeze(0).float().cpu().numpy()
        point_confidence = predictions["world_points_conf"].squeeze(0).float().cpu().numpy()
        extrinsic_np = extrinsic.squeeze(0).float().cpu().numpy()
        intrinsic_np = intrinsic.squeeze(0).float().cpu().numpy()
        rgb = (
            images.detach().float().cpu().permute(0, 2, 3, 1).numpy() * 255.0
        ).clip(0, 255).astype(np.uint8)
        points, colors = _select_vggt_points(
            np, world_points, point_confidence, rgb, max_points
        )
        if len(points) < 10_000:
            raise SpatialReconstructionError(
                f"VGGT retained only {len(points)} confident scene points"
            )
        peak = float(torch.cuda.max_memory_allocated()) / (1024.0 * 1024.0)
        return NeuralCloud(
            points=points.astype(np.float32),
            colors=colors.astype(np.uint8),
            faces=np.empty((0, 3), dtype=np.int32),
            depth=depth,
            confidence=depth_confidence,
            timestamp=float(timestamp),
            camera_ids=list(camera_ids),
            intrinsic=intrinsic_np,
            extrinsic=extrinsic_np,
            input_shape=tuple(int(value) for value in images.shape),
            gpu_peak_mb=peak,
        )
    except torch.cuda.OutOfMemoryError as exc:
        raise SpatialReconstructionError(
            "VGGT ran out of GPU memory while reconstructing these cameras"
        ) from exc
    finally:
        del predictions
        del model
        del images
        gc.collect()
        torch.cuda.empty_cache()


def _preprocess_vggt_frames(cv2, np, torch, frames):
    processed = []
    shapes = []
    for frame in frames:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        height, width = rgb.shape[:2]
        new_width = 518
        new_height = round(height * (new_width / width) / 14) * 14
        resized = cv2.resize(
            rgb,
            (new_width, new_height),
            interpolation=cv2.INTER_AREA if new_width < width else cv2.INTER_CUBIC,
        )
        if new_height > 518:
            start = (new_height - 518) // 2
            resized = resized[start:start + 518]
        processed.append(resized)
        shapes.append(resized.shape[:2])
    max_height = max(shape[0] for shape in shapes)
    max_width = max(shape[1] for shape in shapes)
    tensors = []
    for image in processed:
        height, width = image.shape[:2]
        top = (max_height - height) // 2
        bottom = max_height - height - top
        left = (max_width - width) // 2
        right = max_width - width - left
        if top or bottom or left or right:
            image = cv2.copyMakeBorder(
                image, top, bottom, left, right,
                cv2.BORDER_CONSTANT, value=(255, 255, 255),
            )
        tensors.append(
            torch.from_numpy(np.ascontiguousarray(image))
            .permute(2, 0, 1).float().div_(255.0)
        )
    return torch.stack(tensors)


def _select_vggt_points(np, world_points, confidence, rgb, max_points: int):
    points = world_points.reshape(-1, 3)
    scores = confidence.reshape(-1)
    colors = rgb.reshape(-1, 3)
    valid = np.isfinite(points).all(axis=1) & np.isfinite(scores) & (scores > 1e-5)
    if not bool(valid.any()):
        return points[:0], colors[:0]
    threshold = np.percentile(scores[valid], 45)
    selected = np.flatnonzero(valid & (scores >= threshold))
    if len(selected) > max_points:
        selected = selected[
            np.linspace(0, len(selected) - 1, max_points, dtype=int)
        ]
    return points[selected], colors[selected]


def build_projective_cloud(
    left_native,
    right_native,
    *,
    timestamp: float,
    left_camera_id: str,
    right_camera_id: str,
    max_dimension: int = 1280,
    max_points: int = 120_000,
) -> ProjectiveCloud:
    cv2 = stereo._load_cv2()
    np = stereo._load_numpy()
    left, _ = stereo._resize_for_analysis(cv2, left_native, max_dimension)
    right, _ = stereo._resize_for_analysis(cv2, right_native, max_dimension)
    analysis = stereo._analyze_images(cv2, np, left, right)
    if (
        analysis.fundamental is None
        or analysis.inlier_mask is None
        or analysis.left_points is None
        or int(analysis.inlier_mask.sum()) < 12
    ):
        raise SpatialReconstructionError("not enough shared structure to reconstruct this frame")

    feature_left = analysis.left_points[analysis.inlier_mask].astype(np.float64)
    feature_right = analysis.right_points[analysis.inlier_mask].astype(np.float64)
    output_size = (
        max(left.shape[1], right.shape[1]),
        max(left.shape[0], right.shape[0]),
    )
    rectified, left_h, right_h = cv2.stereoRectifyUncalibrated(
        feature_left,
        feature_right,
        analysis.fundamental,
        output_size,
        threshold=3.0,
    )
    if not rectified or left_h is None or right_h is None:
        raise SpatialReconstructionError("OpenCV could not rectify the shared camera view")

    warped_left = cv2.warpPerspective(left, left_h, output_size)
    warped_right = cv2.warpPerspective(right, right_h, output_size)
    left_valid = cv2.warpPerspective(
        np.full(left.shape[:2], 255, dtype=np.uint8),
        left_h,
        output_size,
        flags=cv2.INTER_NEAREST,
    )
    right_valid = cv2.warpPerspective(
        np.full(right.shape[:2], 255, dtype=np.uint8),
        right_h,
        output_size,
        flags=cv2.INTER_NEAREST,
    )
    rectified_left_points = cv2.perspectiveTransform(
        feature_left.reshape(-1, 1, 2), left_h
    ).reshape(-1, 2)
    rectified_right_points = cv2.perspectiveTransform(
        feature_right.reshape(-1, 1, 2), right_h
    ).reshape(-1, 2)
    vertically_aligned = (
        np.abs(rectified_left_points[:, 1] - rectified_right_points[:, 1]) < 3.0
    )
    disparity_hints = (
        rectified_left_points[:, 0] - rectified_right_points[:, 0]
    )[vertically_aligned]
    if len(disparity_hints) < 8:
        raise SpatialReconstructionError("rectified matches are not horizontally aligned")

    minimum_disparity, disparity_count = _disparity_window(np, disparity_hints)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    left_gray = clahe.apply(cv2.cvtColor(warped_left, cv2.COLOR_BGR2GRAY))
    right_gray = clahe.apply(cv2.cvtColor(warped_right, cv2.COLOR_BGR2GRAY))
    matcher = cv2.StereoSGBM_create(
        minDisparity=minimum_disparity,
        numDisparities=disparity_count,
        blockSize=5,
        P1=8 * 25,
        P2=32 * 25,
        disp12MaxDiff=2,
        uniquenessRatio=7,
        speckleWindowSize=80,
        speckleRange=2,
        preFilterCap=31,
        mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY,
    )
    disparity = matcher.compute(left_gray, right_gray).astype(np.float32) / 16.0
    sample_y, sample_x = _valid_dense_samples(
        cv2,
        np,
        disparity,
        left_gray,
        left_valid,
        right_valid,
        minimum_disparity,
        max_points,
    )
    if len(sample_x) < 500:
        raise SpatialReconstructionError(
            f"dense stereo produced only {len(sample_x)} usable correspondences"
        )
    sample_disparity = disparity[sample_y, sample_x]
    rectified_dense_left = np.column_stack([sample_x, sample_y]).astype(np.float64)
    rectified_dense_right = np.column_stack(
        [sample_x - sample_disparity, sample_y]
    ).astype(np.float64)
    dense_left = cv2.perspectiveTransform(
        rectified_dense_left.reshape(-1, 1, 2), np.linalg.inv(left_h)
    ).reshape(-1, 2)
    dense_right = cv2.perspectiveTransform(
        rectified_dense_right.reshape(-1, 1, 2), np.linalg.inv(right_h)
    ).reshape(-1, 2)

    left_intrinsic = _approximate_intrinsic(np, left)
    right_intrinsic = _approximate_intrinsic(np, right)
    rotation, translation = _recover_pose(
        cv2,
        np,
        feature_left,
        feature_right,
        left_intrinsic,
        right_intrinsic,
    )
    points, keep = _triangulate(
        cv2,
        np,
        dense_left,
        dense_right,
        left_intrinsic,
        right_intrinsic,
        rotation,
        translation,
    )
    sample_x = sample_x[keep]
    sample_y = sample_y[keep]
    colors = warped_left[sample_y, sample_x, ::-1].copy()
    if len(points) < 250:
        raise SpatialReconstructionError(
            f"triangulation retained only {len(points)} stable points"
        )
    faces = _build_faces(
        np,
        points,
        sample_x,
        sample_y,
        warped_left.shape[1],
        warped_left.shape[0],
    )

    return ProjectiveCloud(
        points=points.astype(np.float32),
        colors=colors.astype(np.uint8),
        faces=faces,
        rectified_left=warped_left,
        sample_x=sample_x,
        sample_y=sample_y,
        sample_depth=points[:, 2].astype(np.float32),
        timestamp=float(timestamp),
        left_camera_id=left_camera_id,
        right_camera_id=right_camera_id,
        metrics=asdict(analysis.metrics),
        pose_rotation=rotation,
        pose_translation=translation,
        left_intrinsic=left_intrinsic,
        right_intrinsic=right_intrinsic,
        disparity_range=(minimum_disparity, minimum_disparity + disparity_count),
    )


def _choose_pair(camera_ids: list[str], feasibility: dict) -> tuple[str, str]:
    if len(camera_ids) < 2:
        raise SpatialReconstructionError("at least two cameras are required")
    selected = set(camera_ids)
    candidates = []
    for pair in feasibility.get("pairs", []):
        if not isinstance(pair, dict) or pair.get("status") not in {"promising", "borderline"}:
            continue
        left = pair.get("left_camera_id")
        right = pair.get("right_camera_id")
        if left not in selected or right not in selected:
            continue
        metrics = pair.get("metrics") or {}
        candidates.append((float(metrics.get("score") or 0.0), left, right))
    if candidates:
        _, left, right = max(candidates)
        return left, right
    return camera_ids[0], camera_ids[1]


def _disparity_window(np, hints) -> tuple[int, int]:
    low = int(math.floor(float(np.percentile(hints, 2)))) - 24
    high = int(math.ceil(float(np.percentile(hints, 98)))) + 24
    span = min(384, max(64, high - low))
    if high - low > span:
        low = int(float(np.median(hints)) - span / 2)
    count = int(math.ceil(span / 16.0)) * 16
    return low, count


def _valid_dense_samples(
    cv2,
    np,
    disparity,
    left_gray,
    left_valid,
    right_valid,
    minimum_disparity: int,
    max_points: int,
):
    grid_y, grid_x = np.indices(disparity.shape)
    right_x = np.rint(grid_x - disparity).astype(np.int32)
    clipped_right_x = np.clip(right_x, 0, disparity.shape[1] - 1)
    valid = (
        (disparity > minimum_disparity + 0.25)
        & (right_x >= 0)
        & (right_x < disparity.shape[1])
        & (left_valid > 0)
        & (right_valid[grid_y, clipped_right_x] > 0)
    )
    gradient = cv2.magnitude(
        cv2.Sobel(left_gray, cv2.CV_32F, 1, 0, ksize=3),
        cv2.Sobel(left_gray, cv2.CV_32F, 0, 1, ksize=3),
    )
    valid &= gradient > 8.0
    valid[:max(1, round(disparity.shape[0] * 0.10)), :] = False
    sample_y, sample_x = np.nonzero(valid)
    if len(sample_x) > max_points:
        chosen = np.linspace(0, len(sample_x) - 1, max_points, dtype=int)
        sample_y, sample_x = sample_y[chosen], sample_x[chosen]
    return sample_y, sample_x


def _approximate_intrinsic(np, image):
    height, width = image.shape[:2]
    focal = 0.9 * max(width, height)
    return np.asarray(
        [[focal, 0.0, width / 2.0], [0.0, focal, height / 2.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )


def _normalized_points(cv2, points, intrinsic):
    return cv2.undistortPoints(
        points.reshape(-1, 1, 2), intrinsic, None
    ).reshape(-1, 2)


def _recover_pose(cv2, np, left_points, right_points, left_intrinsic, right_intrinsic):
    normalized_left = _normalized_points(cv2, left_points, left_intrinsic)
    normalized_right = _normalized_points(cv2, right_points, right_intrinsic)
    essential, _ = cv2.findEssentialMat(
        normalized_left,
        normalized_right,
        np.eye(3),
        method=cv2.RANSAC,
        prob=0.999,
        threshold=0.003,
    )
    if essential is None:
        raise SpatialReconstructionError("could not estimate a relative camera pose")
    essential = np.asarray(essential, dtype=np.float64)
    if essential.shape[0] > 3:
        essential = essential[:3, :3]
    retained, rotation, translation, _ = cv2.recoverPose(
        essential, normalized_left, normalized_right, np.eye(3)
    )
    if retained < 8:
        raise SpatialReconstructionError("relative camera pose is geometrically ambiguous")
    return rotation, translation


def _triangulate(
    cv2,
    np,
    left_points,
    right_points,
    left_intrinsic,
    right_intrinsic,
    rotation,
    translation,
):
    normalized_left = _normalized_points(cv2, left_points, left_intrinsic)
    normalized_right = _normalized_points(cv2, right_points, right_intrinsic)
    first_projection = np.hstack([np.eye(3), np.zeros((3, 1))])
    second_projection = np.hstack([rotation, translation])
    homogeneous = cv2.triangulatePoints(
        first_projection,
        second_projection,
        normalized_left.T,
        normalized_right.T,
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        points = (homogeneous[:3] / homogeneous[3]).T
        second_points = (rotation @ points.T + translation).T
        first_reprojection = points[:, :2] / points[:, 2:3]
        second_reprojection = second_points[:, :2] / second_points[:, 2:3]
    error = np.maximum(
        np.linalg.norm(first_reprojection - normalized_left, axis=1),
        np.linalg.norm(second_reprojection - normalized_right, axis=1),
    )
    keep = (
        np.isfinite(points).all(axis=1)
        & (points[:, 2] > 0)
        & (second_points[:, 2] > 0)
        & (error < 0.025)
    )
    if bool(keep.any()):
        depths = points[keep, 2]
        low, high = np.percentile(depths, [1, 99])
        keep &= (points[:, 2] >= low) & (points[:, 2] <= high)
    return points[keep], keep


def _build_faces(np, points, sample_x, sample_y, width: int, height: int):
    """Connect adjacent trustworthy samples without bridging depth edges."""
    vertex_at = np.full((height, width), -1, dtype=np.int32)
    vertex_at[sample_y, sample_x] = np.arange(len(points), dtype=np.int32)
    top_left = vertex_at[:-1, :-1]
    top_right = vertex_at[:-1, 1:]
    bottom_left = vertex_at[1:, :-1]
    bottom_right = vertex_at[1:, 1:]
    candidates = []
    for first, second, third in (
        (top_left, top_right, bottom_left),
        (top_right, bottom_right, bottom_left),
    ):
        present = (first >= 0) & (second >= 0) & (third >= 0)
        if not bool(present.any()):
            continue
        faces = np.column_stack([first[present], second[present], third[present]])
        depths = points[faces, 2]
        # Adjacent pixels on one physical surface change depth gradually. This
        # rejects triangles across occlusion borders and stereo mismatches.
        continuous = depths.max(axis=1) <= depths.min(axis=1) * 1.12
        candidates.append(faces[continuous])
    if not candidates:
        return np.empty((0, 3), dtype=np.int32)
    return np.vstack(candidates).astype(np.int32, copy=False)


def _publish_artifacts(run_dir: Path, cloud: ProjectiveCloud, camera_ids: list[str]) -> dict:
    cv2 = stereo._load_cv2()
    np = stereo._load_numpy()
    point_cloud = run_dir / "scene.ply"
    depth_preview = run_dir / "depth-preview.jpg"
    cloud_preview = run_dir / "pointcloud-preview.jpg"
    summary_path = run_dir / "model-summary.json"
    _write_binary_ply(point_cloud, cloud.points, cloud.colors, cloud.faces, np)
    _write_image_atomic(cv2, depth_preview, _render_depth_preview(cv2, np, cloud))
    _write_image_atomic(cv2, cloud_preview, _render_cloud_preview(cv2, np, cloud))
    summary = {
        "method": "opencv-projective-sgbm",
        "metric": False,
        "timestamp": cloud.timestamp,
        "camera_ids": camera_ids,
        "reconstructed_pair": [cloud.left_camera_id, cloud.right_camera_id],
        "points": int(len(cloud.points)),
        "faces": int(len(cloud.faces)),
        "match_metrics": cloud.metrics,
        "disparity_range": list(cloud.disparity_range),
        "intrinsic": [cloud.left_intrinsic.tolist(), cloud.right_intrinsic.tolist()],
        "relative_pose": {
            "rotation": cloud.pose_rotation.tolist(),
            "translation": cloud.pose_translation.reshape(-1).tolist(),
        },
        "limitations": [
            "Intrinsics are approximate until the cameras are calibrated.",
            "Translation and depth have relative, not metric, scale.",
            "Dense matches are restricted to textured shared regions.",
        ],
    }
    _write_json_atomic(summary_path, summary)
    return {
        "point_cloud": point_cloud.name,
        "depth_preview": depth_preview.name,
        "pointcloud_preview": cloud_preview.name,
        "model_summary": summary_path.name,
    }


def _publish_vggt_artifacts(run_dir: Path, cloud: NeuralCloud, camera_ids: list[str]) -> dict:
    cv2 = stereo._load_cv2()
    np = stereo._load_numpy()
    point_cloud = run_dir / "scene.ply"
    depth_preview = run_dir / "depth-preview.jpg"
    cloud_preview = run_dir / "pointcloud-preview.jpg"
    summary_path = run_dir / "model-summary.json"
    _write_binary_ply(point_cloud, cloud.points, cloud.colors, cloud.faces, np)
    _write_image_atomic(
        cv2, depth_preview,
        _render_vggt_depth_preview(cv2, np, cloud.depth, cloud.confidence),
    )
    _write_image_atomic(cv2, cloud_preview, _render_cloud_preview(cv2, np, cloud))
    summary = {
        "method": "vggt-1b-world-point-head",
        "metric": False,
        "timestamp": cloud.timestamp,
        "camera_ids": camera_ids,
        "reconstructed_camera_ids": cloud.camera_ids,
        "points": int(len(cloud.points)),
        "input_shape": list(cloud.input_shape),
        "intrinsic": cloud.intrinsic.tolist(),
        "extrinsic": cloud.extrinsic.tolist(),
        "gpu_peak_mb": round(cloud.gpu_peak_mb, 2),
        "limitations": [
            "Depth and translation have relative, not metric, scale.",
            "Metric measurements require camera calibration and a measured baseline.",
        ],
    }
    _write_json_atomic(summary_path, summary)
    return {
        "point_cloud": point_cloud.name,
        "depth_preview": depth_preview.name,
        "pointcloud_preview": cloud_preview.name,
        "model_summary": summary_path.name,
    }


def _write_binary_ply(path: Path, points, colors, faces, np) -> None:
    header = (
        "ply\nformat binary_little_endian 1.0\n"
        f"element vertex {len(points)}\n"
        "property float x\nproperty float y\nproperty float z\n"
        "property uchar red\nproperty uchar green\nproperty uchar blue\n"
        f"element face {len(faces)}\n"
        "property list uchar int vertex_indices\n"
        "end_header\n"
    ).encode("ascii")
    vertices = np.empty(
        len(points),
        dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
               ("r", "u1"), ("g", "u1"), ("b", "u1")],
    )
    for index, name in enumerate(("x", "y", "z")):
        vertices[name] = points[:, index]
    for index, name in enumerate(("r", "g", "b")):
        vertices[name] = colors[:, index]
    triangles = np.empty(
        len(faces), dtype=[("count", "u1"), ("indices", "<i4", (3,))]
    )
    triangles["count"] = 3
    triangles["indices"] = faces
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(header)
            handle.write(vertices.tobytes())
            handle.write(triangles.tobytes())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _render_depth_preview(cv2, np, cloud: ProjectiveCloud):
    height, width = cloud.rectified_left.shape[:2]
    depth = np.full((height, width), np.nan, dtype=np.float32)
    depth[cloud.sample_y, cloud.sample_x] = cloud.sample_depth
    low, high = np.percentile(cloud.sample_depth, [2, 98])
    normalized = np.zeros_like(depth, dtype=np.uint8)
    finite = np.isfinite(depth)
    if high > low:
        normalized[finite] = np.clip(
            (high - depth[finite]) / (high - low) * 255.0, 0, 255
        ).astype(np.uint8)
    colour = cv2.applyColorMap(normalized, cv2.COLORMAP_TURBO)
    background = (cloud.rectified_left.astype(np.float32) * 0.18).astype(np.uint8)
    output = background
    output[finite] = colour[finite]
    return output


def _render_vggt_depth_preview(cv2, np, depth, confidence):
    panels = []
    for frame_depth, frame_confidence in zip(depth, confidence):
        values = frame_depth.squeeze(-1)
        valid = np.isfinite(values) & np.isfinite(frame_confidence) & (values > 0)
        if bool(valid.any()):
            confidence_cutoff = np.percentile(frame_confidence[valid], 35)
            valid &= frame_confidence >= confidence_cutoff
        normalized = np.zeros(values.shape, dtype=np.uint8)
        if bool(valid.any()):
            low, high = np.percentile(values[valid], [2, 98])
            if high > low:
                normalized[valid] = np.clip(
                    (high - values[valid]) / (high - low) * 255.0, 0, 255
                ).astype(np.uint8)
        panel = cv2.applyColorMap(normalized, cv2.COLORMAP_TURBO)
        panel[~valid] = (10, 14, 19)
        panels.append(panel)
    return np.hstack(panels)


def _render_cloud_preview(cv2, np, cloud: ProjectiveCloud):
    canvas_height, canvas_width = 640, 1920
    panel_width = canvas_width // 3
    canvas = np.full((canvas_height, canvas_width, 3), (7, 10, 13), dtype=np.uint8)
    points = cloud.points.astype(np.float64).copy()
    points[:, 0] *= -1.0
    points[:, 1] *= -1.0
    low = np.percentile(points, 1, axis=0)
    high = np.percentile(points, 99, axis=0)
    inlier = np.all((points >= low) & (points <= high), axis=1)
    points = points[inlier]
    colors = cloud.colors[inlier]
    low = np.percentile(points, 1, axis=0)
    high = np.percentile(points, 99, axis=0)
    center = (low + high) / 2.0
    scale = max(float((high - low).max()), 1e-9)
    view = (points - center) / scale * 2.0
    views = [
        ("FRONT", -0.35, 0.12),
        ("TOP", -0.35, 1.18),
        ("OBLIQUE", 0.55, 0.45),
    ]
    for panel_index, (label, yaw, pitch) in enumerate(views):
        panel = canvas[:, panel_index * panel_width:(panel_index + 1) * panel_width]
        _render_cloud_panel(cv2, np, panel, view, colors, yaw, pitch)
        cv2.putText(
            panel, label, (24, 38), cv2.FONT_HERSHEY_SIMPLEX, 0.68,
            (205, 215, 224), 1, cv2.LINE_AA,
        )
        if panel_index:
            cv2.line(
                canvas, (panel_index * panel_width, 24),
                (panel_index * panel_width, canvas_height - 24),
                (35, 43, 51), 1, cv2.LINE_AA,
            )
    cv2.putText(
        canvas, "VGGT OVERVIEW  /  RELATIVE SCALE", (24, canvas_height - 22),
        cv2.FONT_HERSHEY_SIMPLEX, 0.52, (116, 132, 145), 1, cv2.LINE_AA,
    )
    return canvas


def _render_cloud_panel(cv2, np, canvas, view, colors, yaw, pitch):
    canvas_height, canvas_width = canvas.shape[:2]
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    rotated = np.column_stack([
        cy * view[:, 0] + sy * view[:, 2],
        view[:, 1],
        -sy * view[:, 0] + cy * view[:, 2],
    ])
    rotated = np.column_stack([
        rotated[:, 0],
        cp * rotated[:, 1] - sp * rotated[:, 2],
        sp * rotated[:, 1] + cp * rotated[:, 2] - 3.2,
    ])
    focal = canvas_height * 0.43
    screen_x = (rotated[:, 0] * 1.92 / -rotated[:, 2] * focal + canvas_width / 2).astype(int)
    screen_y = (-rotated[:, 1] * 1.92 / -rotated[:, 2] * focal + canvas_height / 2).astype(int)
    for index in np.argsort(rotated[:, 2]):
        x, y = int(screen_x[index]), int(screen_y[index])
        if 0 <= x < canvas_width and 0 <= y < canvas_height:
            colour = tuple(int(value) for value in colors[index, ::-1])
            cv2.circle(canvas, (x, y), 1, colour, -1, cv2.LINE_AA)


def _write_image_atomic(cv2, path: Path, image) -> None:
    temporary = path.with_name(f".{path.stem}-{uuid.uuid4().hex}{path.suffix}")
    try:
        if not cv2.imwrite(str(temporary), image):
            raise SpatialReconstructionError(f"could not write {path.name}")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
