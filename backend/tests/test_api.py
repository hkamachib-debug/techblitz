"""API tests. Time is injected, so every clock rule is tested without waiting."""
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import game
from app.config import load_settings
from app.main import create_app

ADMIN = "test-admin-key"
MIN = 60


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t

    def advance(self, minutes):
        self.t += minutes * MIN


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    cfg = replace(load_settings(), db_path=str(tmp_path / "t.db"), admin_key=ADMIN, time_scale=1.0)
    client = TestClient(create_app(cfg, clock=clock))
    return client, clock


def join(client, name="Ada"):
    r = client.post("/api/session", json={"name": name})
    assert r.status_code == 201
    return {"Authorization": "Bearer " + r.json()["token"]}


def started(client, clock, name="Ada"):
    h = join(client, name)
    client.post("/api/clock/start", headers=h)
    clock.advance(5)  # reading time over: Cho is open
    return h


def submit(client, h, pid, answer):
    return client.post("/api/submissions", headers=h, json={"puzzle_id": pid, "submitted_answer": answer})


# ---------------------------------------------------------------- pure rules
@pytest.mark.parametrize("given,accepted,ok", [
    ("9:43", ["9:43"], True), ("09:43", ["9:43"], True), ("943", ["9:43"], True), ("9.43", ["9:43"], True),
    ("  radio   room ", ["RADIO ROOM"], True), ("till ten", ["TILL TEN"], True),
    ("mine = 5", ["MINE = 5"], True), ("9:44", ["9:43"], False), ("garage", ["ICE RINK", "RINK"], False),
])
def test_normalization(given, accepted, ok):
    assert game.is_correct(given, accepted) is ok


def test_windows_are_back_to_back():
    ws = game.windows(0, 5, {"cho": 15, "rigsby": 15, "van-pelt": 15, "lisbon": 20}, 1.0)
    assert [(w.id, w.open / 60, w.close / 60) for w in ws] == [
        ("cho", 5, 20), ("rigsby", 20, 35), ("van-pelt", 35, 50), ("lisbon", 50, 70)]


# ---------------------------------------------------------------- auth + clock
def test_requires_session(env):
    client, _ = env
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/state", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_clock_start_is_idempotent(env):
    client, clock = env
    h = join(client)
    client.post("/api/clock/start", headers=h)
    t0 = client.get("/api/state", headers=h).json()["clock"]["t0"]
    clock.advance(3)
    client.post("/api/clock/start", headers=h)  # e.g. a reload of the briefing
    assert client.get("/api/state", headers=h).json()["clock"]["t0"] == t0


def test_cases_unlock_by_clock_not_by_correctness(env):
    client, clock = env
    h = join(client)
    client.post("/api/clock/start", headers=h)
    assert client.get("/api/puzzles/cho-a", headers=h).status_code == 423     # still reading
    clock.advance(5)
    assert client.get("/api/puzzles/cho-a", headers=h).status_code == 200
    assert client.get("/api/puzzles/rigsby-a", headers=h).status_code == 423  # Rigsby in the field
    assert client.get("/api/puzzles/cho-b", headers=h).status_code == 423     # Part A not submitted
    submit(client, h, "cho-a", "1:00")                                          # a WRONG answer...
    assert client.get("/api/puzzles/cho-b", headers=h).status_code == 200     # ...still opens Part B
    clock.advance(15)
    assert client.get("/api/puzzles/rigsby-a", headers=h).status_code == 200


# ---------------------------------------------------------------- submissions
def test_submission_is_locked_once_and_reveals_nothing(env):
    client, clock = env
    h = started(client, clock)
    r = submit(client, h, "cho-a", "9:43")
    assert r.status_code == 201 and set(r.json()) == {"status", "recorded_at"}  # no correctness leaked
    assert submit(client, h, "cho-a", "9:44").status_code == 409
    s = client.get("/api/state", headers=h).json()
    assert s["submissions"]["cho-a"]["answer"] == "9:43"  # raw value kept exactly


def test_late_submission_rejected(env):
    client, clock = env
    h = started(client, clock)
    clock.advance(15)  # Cho's window has closed
    assert submit(client, h, "cho-a", "9:43").status_code == 410
    assert client.get("/api/puzzles/cho-a", headers=h).status_code == 410


def test_players_are_isolated(env):
    client, clock = env
    a, b = started(client, clock, "A"), started(client, clock, "B")
    submit(client, a, "cho-a", "9:43")
    assert "cho-a" not in client.get("/api/state", headers=b).json()["submissions"]


# ---------------------------------------------------------------- hints
def test_hint_timing_and_single_use(env):
    client, clock = env
    h = started(client, clock)
    assert client.post("/api/hints", headers=h, json={"puzzle_id": "cho-a"}).status_code == 425  # never opened
    client.get("/api/puzzles/cho-a", headers=h)
    assert client.post("/api/hints", headers=h, json={"puzzle_id": "cho-a"}).status_code == 425  # too soon
    clock.advance(5)
    r = client.post("/api/hints", headers=h, json={"puzzle_id": "cho-a"})
    assert r.status_code == 201 and r.json()["hint"] and r.json()["penalty_s"] == 60
    again = client.post("/api/hints", headers=h, json={"puzzle_id": "cho-a"})
    assert again.json()["recorded_at"] == r.json()["recorded_at"]  # same hint, no second penalty


# ---------------------------------------------------------------- accusation + results
def play_all(client, clock, h, right=True):
    good = {"cho-a": "9:43", "cho-b": "workshop", "rigsby-a": "radio room", "rigsby-b": "till ten",
            "van-pelt-a": "nonstop", "van-pelt-b": "ice rink", "lisbon-a": "5", "lisbon-b": "4"}
    for d, length in [("cho", 15), ("rigsby", 15), ("van-pelt", 15), ("lisbon", 20)]:
        for part in "ab":
            pid = f"{d}-{part}"
            assert submit(client, h, pid, good[pid] if right else "x").status_code == 201
        clock.advance(length)


def test_accusation_locked_until_cases_close_then_once(env):
    client, clock = env
    h = started(client, clock)
    assert client.post("/api/accusation", headers=h, json={"suspect_id": "sam"}).status_code == 423
    play_all(client, clock, h)
    assert client.post("/api/accusation", headers=h, json={"suspect_id": "nobody"}).status_code == 422
    assert client.post("/api/accusation", headers=h, json={"suspect_id": "sam"}).status_code == 201
    assert client.post("/api/accusation", headers=h, json={"suspect_id": "owen"}).status_code == 409


def test_results_sealed_until_admin_release_and_ranked(env):
    client, clock = env
    good, bad = started(client, clock, "Good"), started(client, clock, "Bad")
    # play both in lockstep so they share windows
    answers = {"cho-a": "9:43", "cho-b": "workshop", "rigsby-a": "radio room", "rigsby-b": "till ten",
               "van-pelt-a": "nonstop", "van-pelt-b": "ice rink", "lisbon-a": "5", "lisbon-b": "4"}
    for d, length in [("cho", 15), ("rigsby", 15), ("van-pelt", 15), ("lisbon", 20)]:
        for part in "ab":
            pid = f"{d}-{part}"
            submit(client, good, pid, answers[pid]); submit(client, bad, pid, "wrong")
        clock.advance(length)
    client.post("/api/accusation", headers=good, json={"suspect_id": "sam"})
    client.post("/api/accusation", headers=bad, json={"suspect_id": "owen"})

    assert client.get("/api/results", headers=good).status_code == 423
    assert client.post("/api/admin/release", json={"released": True}).status_code == 403
    assert client.post("/api/admin/release", headers={"X-Admin-Key": ADMIN}, json={"released": True}).status_code == 200
    r = client.get("/api/results", headers=good).json()
    assert r["you"]["rank"] == 1 and r["you"]["cases_cleared"] == 4 and r["you"]["accusation_correct"] == 1
    assert client.get("/api/results", headers=bad).json()["you"]["rank"] == 2


# ---------------------------------------------------------------- security
def test_answer_key_never_reaches_the_browser(env):
    client, clock = env
    secret = json.loads((load_settings().data_dir / "answers.json").read_text())
    h = started(client, clock)
    bodies = [client.get("/").text, client.get("/api/state", headers=h).text, client.get("/api/puzzles/cho-a", headers=h).text]
    for path in ("/data/answers.json", "/backend/data/answers.json", "/assets/../../backend/data/answers.json"):
        assert client.get(path).status_code == 404
    puzzle_text = (load_settings().data_dir / "puzzles.public.json").read_text().lower()
    # Answers that are ordinary words inside a published prompt (e.g. "workshop") aren't leaks.
    leaks = [a for p in secret["answers"].values() for a in p["accepted"]
             if len(a) >= 4 and not a.isdigit() and a.lower() not in puzzle_text
             and any(a.lower() in b.lower() for b in bodies)]
    assert leaks == []
    assert "killer" not in client.get("/api/state", headers=h).text


def test_simultaneous_submits_record_exactly_one(env):
    """Two tabs pressing Submit at the same moment: the database key lets exactly one through."""
    from concurrent.futures import ThreadPoolExecutor
    client, clock = env
    h = started(client, clock)
    with ThreadPoolExecutor(8) as pool:
        codes = sorted(pool.map(lambda a: submit(client, h, "cho-a", a).status_code, [f"9:4{i}" for i in range(8)]))
    assert codes.count(201) == 1 and codes.count(409) == 7


# ---------------------------------------------------------------- 60-second demo
def demo(client):
    r = client.post("/api/session/demo")
    assert r.status_code == 201
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_demo_runs_on_its_own_fast_clock(env):
    client, _ = env
    s = client.get("/api/state", headers=demo(client)).json()
    c = s["clock"]
    total = c["sections"][-1]["close"] - c["t0"]
    assert s["participant"]["demo"] is True
    assert 50 <= total <= 70        # the whole 70-minute event in about a minute


def test_demo_hint_opens_sooner_but_real_players_keep_five_minutes(env):
    client, clock = env
    d = demo(client)
    s = client.get("/api/state", headers=d).json()
    assert s["rules"]["hint_delay_s"] < 1.5                     # 1 demo minute, under a real second
    clock.t += 5                                                # past the demo's reading time
    client.get("/api/puzzles/cho-a", headers=d)                 # opening it starts the hint timer
    assert client.post("/api/hints", headers=d, json={"puzzle_id": "cho-a"}).status_code == 425
    clock.t += 1
    assert client.post("/api/hints", headers=d, json={"puzzle_id": "cho-a"}).status_code == 201
    real = started(client, clock)
    assert client.get("/api/state", headers=real).json()["rules"]["hint_delay_s"] == 5 * 60   # unchanged for real rounds


def test_demo_answers_only_for_demo_sessions_and_only_once_unlocked(env):
    client, clock = env
    real = started(client, clock)
    assert client.get("/api/demo/answer/cho-a", headers=real).status_code == 403   # real players: never
    d = demo(client)
    assert client.get("/api/demo/answer/cho-a", headers=d).status_code == 423      # still reading
    clock.t += 5                                                                    # past the demo's reading time
    assert client.get("/api/demo/answer/cho-a", headers=d).json()["answer"] == "9:43"
    assert client.get("/api/demo/answer/rigsby-a", headers=d).status_code == 423   # not unlocked yet
    assert client.get("/api/demo/answer/accusation", headers=d).status_code == 423


def test_demo_reveals_its_own_result_but_stays_off_the_leaderboard(env):
    client, clock = env
    real = started(client, clock, "Real Player")
    d = demo(client)
    clock.t += 70                   # the demo's whole event has elapsed
    assert client.post("/api/accusation", headers=d, json={"suspect_id": "sam"}).status_code == 201
    r = client.get("/api/results", headers=d)
    assert r.status_code == 200 and r.json()["you"]["accusation_correct"] == 1
    assert client.get("/api/results", headers=real).status_code == 423              # real results still sealed
    client.post("/api/admin/release", headers={"X-Admin-Key": ADMIN}, json={"released": True})
    board = client.get("/api/admin/leaderboard", headers={"X-Admin-Key": ADMIN}).json()["players"]
    assert [p["name"] for p in board] == ["Real Player"]


def test_existing_database_is_upgraded_in_place(tmp_path):
    """A database created by the earlier version (no demo columns) keeps its players and gains demo support."""
    import sqlite3
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE participants (id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,"
                    " created_at REAL NOT NULL, t0 REAL)")
        con.execute("INSERT INTO participants VALUES ('P-OLD','Old Player','h',0,NULL)")
    client = TestClient(create_app(replace(load_settings(), db_path=str(path), admin_key=ADMIN), clock=Clock()))
    assert client.post("/api/session/demo").status_code == 201
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT name, is_demo FROM participants WHERE id='P-OLD'").fetchone() == ("Old Player", 0)


def test_total_time_counts_each_case_once(env):
    """Part A at 2 min and Part B at 5 min into Cho's case is 5 minutes, not 2 + 5 = 7."""
    client, clock = env
    h = started(client, clock)
    clock.advance(2); submit(client, h, "cho-a", "9:43")
    clock.advance(3); submit(client, h, "cho-b", "workshop")
    client.post("/api/admin/release", headers={"X-Admin-Key": ADMIN}, json={"released": True})
    board = client.get("/api/admin/leaderboard", headers={"X-Admin-Key": ADMIN}).json()["players"]
    assert board[0]["total_time_s"] == 5 * 60
