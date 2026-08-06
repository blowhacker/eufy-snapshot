from __future__ import annotations

import json
import mimetypes
import re
from pathlib import Path


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ARTIFACT_MEDIA_TYPES = {
    ".ply": "application/octet-stream",
    ".json": "application/json",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


class SpatialStoreError(ValueError):
    pass


class SpatialStore:
    """Read reconstruction manifests and their explicitly named artifacts.

    The directory shape is ``<scene>/<run>/manifest.json``. A scene owns an
    arbitrary camera set; a two-camera reconstruction is not special here.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def list_scenes(self) -> list[dict]:
        scenes: dict[str, dict] = {}
        if not self.root.is_dir():
            return []

        for manifest_path in sorted(self.root.glob("*/*/manifest.json")):
            try:
                manifest = self._read_manifest(manifest_path)
            except (OSError, json.JSONDecodeError, SpatialStoreError):
                continue
            scene = manifest["scene"]
            run = manifest["run"]
            entry = scenes.setdefault(
                scene["id"],
                {
                    "id": scene["id"],
                    "name": scene["name"],
                    "camera_ids": scene["camera_ids"],
                    "runs": [],
                },
            )
            entry["runs"].append({
                **run,
                "artifacts": manifest["artifacts"],
                "stats": manifest.get("stats", {}),
                "warnings": manifest.get("warnings", []),
            })

        result = list(scenes.values())
        for scene in result:
            scene["runs"].sort(
                key=lambda run: (str(run.get("created_at", "")), run["id"]),
                reverse=True,
            )
        result.sort(key=lambda scene: scene["name"].casefold())
        return result

    def artifact(self, scene_id: str, run_id: str, artifact_name: str) -> tuple[Path, str]:
        for value, label in (
            (scene_id, "scene"), (run_id, "run"), (artifact_name, "artifact")
        ):
            if not _IDENTIFIER.fullmatch(value or ""):
                raise SpatialStoreError(f"invalid {label} identifier")

        run_dir = self.root / scene_id / run_id
        manifest = self._read_manifest(run_dir / "manifest.json")
        if manifest["scene"]["id"] != scene_id or manifest["run"]["id"] != run_id:
            raise SpatialStoreError("manifest location does not match its identifiers")

        relative = manifest["artifacts"].get(artifact_name)
        if not isinstance(relative, str) or not relative:
            raise SpatialStoreError("artifact is not present in this run")
        path = (run_dir / relative).resolve()
        try:
            path.relative_to(run_dir.resolve())
        except ValueError:
            raise SpatialStoreError("artifact escapes its run directory") from None
        if not path.is_file():
            raise FileNotFoundError(path)
        media_type = _ARTIFACT_MEDIA_TYPES.get(
            path.suffix.lower(), mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )
        return path, media_type

    def _read_manifest(self, path: Path) -> dict:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise SpatialStoreError("manifest must be an object")
        scene = manifest.get("scene")
        run = manifest.get("run")
        artifacts = manifest.get("artifacts")
        if not isinstance(scene, dict) or not isinstance(run, dict) or not isinstance(artifacts, dict):
            raise SpatialStoreError("manifest is missing scene, run, or artifacts")

        scene_id = scene.get("id")
        run_id = run.get("id")
        camera_ids = scene.get("camera_ids")
        if not _IDENTIFIER.fullmatch(str(scene_id or "")):
            raise SpatialStoreError("invalid scene id")
        if not _IDENTIFIER.fullmatch(str(run_id or "")):
            raise SpatialStoreError("invalid run id")
        if not isinstance(scene.get("name"), str) or not scene["name"].strip():
            raise SpatialStoreError("scene name is required")
        if not isinstance(camera_ids, list) or not camera_ids:
            raise SpatialStoreError("scene camera_ids must be a non-empty list")
        if any(not _IDENTIFIER.fullmatch(str(camera_id or "")) for camera_id in camera_ids):
            raise SpatialStoreError("invalid camera id")
        if any(not _IDENTIFIER.fullmatch(str(name or "")) for name in artifacts):
            raise SpatialStoreError("invalid artifact name")
        return manifest
