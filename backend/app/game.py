"""Game rules: the case clock, unlocking, answer normalization and ranking.

Pure functions with no web or database code, so every rule is unit-testable on its own.
"""
import re
from dataclasses import dataclass

DETECTIVES = ["cho", "rigsby", "van-pelt", "lisbon"]
SUSPECTS = ["ingrid", "mateo", "lena", "owen", "sam"]


def detective_of(puzzle_id: str) -> str:
    return next(d for d in DETECTIVES if puzzle_id.startswith(d + "-"))


# ---------------------------------------------------------------- the case clock
@dataclass(frozen=True)
class Window:
    id: str
    open: float   # epoch seconds
    close: float


def windows(t0: float, reading_min: float, section_min: dict, scale: float) -> list[Window]:
    """Reading period, then each detective's case back to back. Times scale for demos."""
    t = t0 + reading_min * 60 * scale
    out = []
    for d in DETECTIVES:
        length = section_min[d] * 60 * scale
        out.append(Window(d, t, t + length))
        t += length
    return out


def window_for(ws: list[Window], detective: str) -> Window:
    return next(w for w in ws if w.id == detective)


def is_open(w: Window, now: float) -> bool:
    return w.open <= now < w.close


def sections_closed(ws: list[Window], submitted: set[str], puzzle_ids: list[str], now: float) -> bool:
    """A case is closed once both parts are submitted or its time has run out."""
    for w in ws:
        parts = [p for p in puzzle_ids if detective_of(p) == w.id]
        if not (all(p in submitted for p in parts) or now >= w.close):
            return False
    return True


# ---------------------------------------------------------------- answers
_TIME = re.compile(r"^0?(\d{1,2})[:.]?(\d{2})(\s*[AP]M)?$")


def normalize(answer: str) -> str:
    """Case-insensitive, whitespace-tolerant form used for comparison (organizer rule)."""
    s = re.sub(r"\s+", " ", answer.strip().upper())
    m = _TIME.match(s)
    if m:  # 09:43 / 9:43 / 9.43 / 943 / 9:43 PM all compare as times
        h, mm, ampm = m.groups()
        s = f"{int(h)}:{mm}" + (ampm.replace(" ", "") if ampm else "")
    return s


def is_correct(answer: str, accepted: list[str]) -> bool:
    n = normalize(answer)
    return any(n == normalize(a) for a in accepted)


# ---------------------------------------------------------------- ranking
def rank_key(row: dict):
    """Organizer-configurable ordering. Default: correct accusation, then cases cleared
    (both parts right), then correct answers, then lowest total time (incl. hint penalties)."""
    return (-row["accusation_correct"], -row["cases_cleared"], -row["correct_answers"], row["total_time_s"])
