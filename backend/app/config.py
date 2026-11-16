"""Runtime configuration, read once from environment variables."""
import os
from dataclasses import dataclass
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BACKEND_DIR.parent


@dataclass(frozen=True)
class Settings:
    db_path: str
    admin_key: str | None
    time_scale: float          # 1.0 = real event timing; 0.1 = a 70-minute event plays in 7 minutes
    demo_scale: float          # clock speed for the 60-second demo (0.014 = 70 minutes in ~59 seconds)
    reading_min: float
    section_min: dict
    hint_delay_min: float
    demo_hint_delay_min: float  # the demo's hint opens sooner so the whole round fits in a minute
    hint_penalty_s: int
    frontend_dir: Path
    data_dir: Path


def load_settings() -> Settings:
    return Settings(
        db_path=os.getenv("TB_DB_PATH", str(BACKEND_DIR / "tech_blitz.db")),
        admin_key=os.getenv("TB_ADMIN_KEY") or None,
        time_scale=float(os.getenv("TB_TIME_SCALE", "1.0")),
        demo_scale=float(os.getenv("TB_DEMO_SCALE", "0.014")),
        reading_min=5,
        # Cases open in this order, back to back, after the reading period.
        section_min={"cho": 15, "rigsby": 15, "van-pelt": 15, "lisbon": 20},
        hint_delay_min=5,
        demo_hint_delay_min=1,
        hint_penalty_s=60,
        frontend_dir=Path(os.getenv("TB_FRONTEND_DIR", str(ROOT_DIR / "frontend"))),
        data_dir=BACKEND_DIR / "data",
    )
