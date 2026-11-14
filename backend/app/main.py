"""TECH BLITZ: The Jane Protocol, API server.

The server is the single source of truth: it owns the case clock, decides what each player may see,
records answers exactly once, and keeps the answer key. The browser never learns whether an answer
is right until an organizer releases results.
"""
import hashlib
import json
import secrets
import sqlite3
import time
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import game
from .config import Settings, load_settings
from .db import connect, init_db


# ---------------------------------------------------------------- request bodies
class SessionIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class SubmissionIn(BaseModel):
    puzzle_id: str
    submitted_answer: str = Field(max_length=200)


class HintIn(BaseModel):
    puzzle_id: str


class AccusationIn(BaseModel):
    suspect_id: str


class ReleaseIn(BaseModel):
    released: bool


def create_app(settings: Settings | None = None, clock=time.time) -> FastAPI:
    cfg = settings or load_settings()
    init_db(cfg.db_path)
    public = json.loads((cfg.data_dir / "puzzles.public.json").read_text())["puzzles"]
    secret = json.loads((cfg.data_dir / "answers.json").read_text())
    by_id = {p["id"]: p for p in public}
    ids = [p["id"] for p in public]

    app = FastAPI(title="TECH BLITZ API", version="1.0.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.clock = clock  # injectable so tests can move time

    def now() -> float:
        return app.state.clock()

    # ------------------------------------------------------------ helpers
    def db():
        with connect(cfg.db_path) as con:
            yield con

    def player(authorization: str = Header(default=""), con: sqlite3.Connection = Depends(db)):
        token = authorization.removeprefix("Bearer ").strip()
        if not token:
            raise HTTPException(401, "Not signed in")
        row = con.execute("SELECT * FROM participants WHERE token_hash=?", (_hash(token),)).fetchone()
        if not row:
            raise HTTPException(401, "Session not recognised")
        return row

    def require_admin(x_admin_key: str = Header(default="")):
        if not cfg.admin_key or not secrets.compare_digest(x_admin_key, cfg.admin_key):
            raise HTTPException(403, "Admin key required")

    def scale(p) -> float:
        return p["time_scale"] or cfg.time_scale

    def wins(p):
        if p["t0"] is None:
            return None
        return game.windows(p["t0"], cfg.reading_min, cfg.section_min, scale(p))

    def submitted_ids(con, pid) -> set[str]:
        return {r[0] for r in con.execute("SELECT puzzle_id FROM submissions WHERE participant_id=?", (pid,))}

    def released(con) -> bool:
        r = con.execute("SELECT value FROM settings WHERE key='results_released'").fetchone()
        return bool(r and r[0] == "1")

    def released_for(p, con) -> bool:
        # A demo reveals its own result as soon as it accuses; real players wait for the organizers.
        if p["is_demo"]:
            return con.execute("SELECT 1 FROM accusations WHERE participant_id=?", (p["id"],)).fetchone() is not None
        return released(con)

    def gate(p, con, puzzle_id):
        """Can this player see / answer this puzzle right now? Returns (window, submitted set)."""
        if puzzle_id not in by_id:
            raise HTTPException(404, "Unknown puzzle")
        ws = wins(p)
        if ws is None:
            raise HTTPException(423, "Your case clock hasn't started. Open the case briefing first.")
        w = game.window_for(ws, game.detective_of(puzzle_id))
        done = submitted_ids(con, p["id"])
        if now() < w.open:
            raise HTTPException(423, "This detective is still in the field.")
        req = by_id[puzzle_id].get("requires")
        if req and req not in done:
            raise HTTPException(423, "Submit Part A first.")
        return w, done

    # ------------------------------------------------------------ session + clock
    @app.post("/api/session", status_code=201)
    def create_session(body: SessionIn, con=Depends(db)):
        token = secrets.token_urlsafe(32)
        pid = "P-" + uuid.uuid4().hex[:8].upper()
        con.execute("INSERT INTO participants (id,name,token_hash,created_at) VALUES (?,?,?,?)",
                    (pid, body.name.strip(), _hash(token), now()))
        return {"participant_id": pid, "name": body.name.strip(), "token": token}

    @app.post("/api/session/demo", status_code=201)
    def create_demo_session(con=Depends(db)):
        """A throwaway player on a fast clock, for the 60-second walkthrough. Its clock starts at once,
        it never appears on the leaderboard, and only it can ask the server for answers (see below)."""
        token = secrets.token_urlsafe(32)
        pid = "DEMO-" + uuid.uuid4().hex[:6].upper()
        con.execute("INSERT INTO participants (id,name,token_hash,created_at,t0,is_demo,time_scale) VALUES (?,?,?,?,?,1,?)",
                    (pid, "Demo Detective", _hash(token), now(), now(), cfg.demo_scale))
        return {"participant_id": pid, "name": "Demo Detective", "token": token}

    @app.get("/api/demo/answer/{puzzle_id}")
    def demo_answer(puzzle_id: str, p=Depends(player), con=Depends(db)):
        """The autopilot's answer for one puzzle. Demo sessions only, and only once that puzzle is unlocked,
        so a real player's browser can never obtain the key this way."""
        if not p["is_demo"]:
            raise HTTPException(403, "Only available in the demo.")
        if puzzle_id == "accusation":
            ws = wins(p)
            if not game.sections_closed(ws, submitted_ids(con, p["id"]), ids, now()):
                raise HTTPException(423, "Not yet.")
            return {"answer": secret["killer"]}
        gate(p, con, puzzle_id)
        return {"answer": secret["answers"][puzzle_id]["accepted"][0]}

    @app.post("/api/clock/start")
    def start_clock(p=Depends(player), con=Depends(db)):
        # Idempotent: the first call wins, so reloading the briefing never restarts the clock.
        con.execute("UPDATE participants SET t0=? WHERE id=? AND t0 IS NULL", (now(), p["id"]))
        return {"status": "started"}

    @app.get("/api/state")
    def state(p=Depends(player), con=Depends(db)):
        ws = wins(p)
        subs = {r["puzzle_id"]: {"answer": r["raw_answer"], "recorded_at": r["recorded_at"]}
                for r in con.execute("SELECT * FROM submissions WHERE participant_id=?", (p["id"],))}
        hints = {r["puzzle_id"]: {"used_at": r["used_at"], "hint": secret["answers"][r["puzzle_id"]]["hint"]}
                 for r in con.execute("SELECT * FROM hints WHERE participant_id=?", (p["id"],))}
        opened = {r["puzzle_id"]: r["opened_at"]
                  for r in con.execute("SELECT * FROM puzzle_opens WHERE participant_id=?", (p["id"],))}
        acc = con.execute("SELECT suspect_id, recorded_at FROM accusations WHERE participant_id=?", (p["id"],)).fetchone()
        return {
            "server_time": now(),
            "participant": {"id": p["id"], "name": p["name"], "demo": bool(p["is_demo"])},
            "clock": None if ws is None else {
                "t0": p["t0"],
                "reading_ends": ws[0].open,
                "sections": [{"id": w.id, "open": w.open, "close": w.close} for w in ws],
            },
            "rules": {"hint_delay_s": cfg.hint_delay_min * 60 * scale(p), "hint_penalty_s": cfg.hint_penalty_s},
            # metadata only: prompts are fetched one at a time, once unlocked
            "puzzles": [{k: q.get(k) for k in ("id", "case", "title", "requires", "unlock_order", "hint_available")} for q in public],
            "submissions": subs, "hints": hints, "opened": opened,
            "accusation": dict(acc) if acc else None,
            "results_released": released_for(p, con),
        }

    # ------------------------------------------------------------ puzzles
    @app.get("/api/puzzles/{puzzle_id}")
    def get_puzzle(puzzle_id: str, p=Depends(player), con=Depends(db)):
        w, done = gate(p, con, puzzle_id)
        if now() >= w.close and puzzle_id not in done:
            raise HTTPException(410, "Time's up for this case.")
        con.execute("INSERT OR IGNORE INTO puzzle_opens VALUES (?,?,?)", (p["id"], puzzle_id, now()))
        return by_id[puzzle_id]

    @app.post("/api/submissions", status_code=201)
    def submit(body: SubmissionIn, p=Depends(player), con=Depends(db)):
        w, _ = gate(p, con, body.puzzle_id)
        if not game.is_open(w, now()):
            raise HTTPException(410, "Time's up for this case.")
        if not body.submitted_answer.strip():
            raise HTTPException(422, "Answer is empty")
        accepted = secret["answers"][body.puzzle_id]["accepted"]
        try:
            con.execute("INSERT INTO submissions VALUES (?,?,?,?,?,?)",
                        (p["id"], body.puzzle_id, body.submitted_answer, game.normalize(body.submitted_answer),
                         int(game.is_correct(body.submitted_answer, accepted)), now()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "An answer is already recorded for this puzzle.")
        # Deliberately no correctness in the response.
        return {"status": "recorded", "recorded_at": now()}

    @app.post("/api/hints", status_code=201)
    def hint(body: HintIn, p=Depends(player), con=Depends(db)):
        w, done = gate(p, con, body.puzzle_id)
        q = by_id[body.puzzle_id]
        if not q.get("hint_available"):
            raise HTTPException(404, "No hint for this puzzle")
        used = con.execute("SELECT used_at FROM hints WHERE participant_id=? AND puzzle_id=?", (p["id"], body.puzzle_id)).fetchone()
        if not used:
            if body.puzzle_id in done or not game.is_open(w, now()):
                raise HTTPException(410, "Hints are only available while the part is open.")
            opened = con.execute("SELECT opened_at FROM puzzle_opens WHERE participant_id=? AND puzzle_id=?", (p["id"], body.puzzle_id)).fetchone()
            if not opened or now() < opened[0] + cfg.hint_delay_min * 60 * scale(p):
                raise HTTPException(425, "The hint isn't available yet.")
            con.execute("INSERT INTO hints VALUES (?,?,?)", (p["id"], body.puzzle_id, now()))
            used = (now(),)
        return {"status": "logged", "recorded_at": used[0], "penalty_s": cfg.hint_penalty_s,
                "hint": secret["answers"][body.puzzle_id]["hint"]}

    # ------------------------------------------------------------ accusation + results
    @app.post("/api/accusation", status_code=201)
    def accuse(body: AccusationIn, p=Depends(player), con=Depends(db)):
        ws = wins(p)
        if ws is None or not game.sections_closed(ws, submitted_ids(con, p["id"]), ids, now()):
            raise HTTPException(423, "The final deduction opens once all four cases are closed.")
        if body.suspect_id not in game.SUSPECTS:
            raise HTTPException(422, "Unknown suspect")
        try:
            con.execute("INSERT INTO accusations VALUES (?,?,?,?)",
                        (p["id"], body.suspect_id, int(body.suspect_id == secret["killer"]), now()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Your accusation is already recorded.")
        return {"status": "recorded", "recorded_at": now()}

    def scoreboard(con, include_id=None) -> list[dict]:
        rows = []
        for p in con.execute("SELECT * FROM participants WHERE t0 IS NOT NULL AND (is_demo=0 OR id=?)", (include_id,)):
            ws = {w.id: w for w in wins(p)}
            subs = con.execute("SELECT puzzle_id, correct, recorded_at FROM submissions WHERE participant_id=?", (p["id"],)).fetchall()
            right = {s["puzzle_id"] for s in subs if s["correct"]}
            n_hints = con.execute("SELECT COUNT(*) FROM hints WHERE participant_id=?", (p["id"],)).fetchone()[0]
            acc = con.execute("SELECT correct FROM accusations WHERE participant_id=?", (p["id"],)).fetchone()
            # time to finish each case: from that case opening to its last answer (each case counted once)
            finished = {}
            for s in subs:
                d = game.detective_of(s["puzzle_id"])
                finished[d] = max(finished.get(d, 0), s["recorded_at"] - ws[d].open)
            elapsed = sum(finished.values())
            rows.append({
                "participant_id": p["id"], "name": p["name"],
                "correct_answers": len(right),
                "cases_cleared": sum(all(q in right for q in ids if game.detective_of(q) == d) for d in game.DETECTIVES),
                "accusation_correct": int(bool(acc and acc["correct"])),
                "hints_used": n_hints,
                "total_time_s": round(elapsed / scale(p) + n_hints * cfg.hint_penalty_s, 1),   # in event minutes, whatever the speed
            })
        rows.sort(key=game.rank_key)
        for i, r in enumerate(rows, 1):
            r["rank"] = i
        return rows

    @app.get("/api/results")
    def results(p=Depends(player), con=Depends(db)):
        if not released_for(p, con):
            raise HTTPException(423, "Results are sealed until the reveal.")
        board = scoreboard(con, include_id=p["id"] if p["is_demo"] else None)
        me = next((r for r in board if r["participant_id"] == p["id"]), None)
        mine = {r["puzzle_id"]: bool(r["correct"]) for r in con.execute(
            "SELECT puzzle_id, correct FROM submissions WHERE participant_id=?", (p["id"],))}
        return {"you": me, "answers_correct": mine, "players": len(board), "killer": secret["killer"]}

    @app.post("/api/admin/release", dependencies=[Depends(require_admin)])
    def release(body: ReleaseIn, con=Depends(db)):
        con.execute("INSERT INTO settings VALUES ('results_released',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("1" if body.released else "0",))
        return {"results_released": body.released}

    @app.get("/api/admin/leaderboard", dependencies=[Depends(require_admin)])
    def leaderboard(con=Depends(db)):
        return {"players": scoreboard(con)}

    @app.get("/api/health")
    def health():
        return {"status": "ok", "time_scale": cfg.time_scale, "demo_scale": cfg.demo_scale}

    # ------------------------------------------------------------ frontend
    # Only the frontend folder is served. backend/data (the answer key) is never reachable over HTTP.
    if cfg.frontend_dir.exists():
        app.mount("/assets", StaticFiles(directory=cfg.frontend_dir / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(cfg.frontend_dir / "index.html")

    return app


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


app = create_app()
