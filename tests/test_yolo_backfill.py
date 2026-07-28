from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wanyard.video import VideoSegmentDB
from wanyard.yolo_server import (
    _backfill_batch,
    _backfill_max_age_seconds,
)


class BackfillAgeTests(unittest.TestCase):
    def make_segment(
        self,
        db: VideoSegmentDB,
        *,
        source: str,
        start: float,
        end: float,
    ) -> int:
        segment_id = db.open_segment(
            source,
            f"{source}/{start:.0f}.mp4",
            start,
        )
        db.set_segment_media_start(segment_id, start)
        db.close_segment(segment_id, end, None, None)
        return segment_id

    def test_skips_old_segments_and_returns_only_the_recent_hour(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = VideoSegmentDB(Path(tmpdir) / "video.db")
            old_id = self.make_segment(
                db, source="front", start=1000.0, end=1100.0
            )
            recent_id = self.make_segment(
                db, source="front", start=4500.0, end=4600.0
            )

            skipped, rows = _backfill_batch(
                db,
                now=5000.0,
                max_age_seconds=3600.0,
            )

            self.assertEqual(skipped, 1)
            self.assertEqual([row["id"] for row in rows], [recent_id])
            with db._connect() as conn:
                old = conn.execute(
                    "SELECT scanned_at FROM segments WHERE id=?",
                    (old_id,),
                ).fetchone()
            self.assertEqual(old["scanned_at"], 5000.0)

    def test_invalid_age_setting_uses_one_hour_default(self) -> None:
        with patch.dict(
            os.environ,
            {"WANYARD_BACKFILL_MAX_AGE_SECONDS": "not-a-number"},
        ):
            self.assertEqual(_backfill_max_age_seconds(), 3600.0)


if __name__ == "__main__":
    unittest.main()
