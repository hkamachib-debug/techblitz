"""End-to-end: a real browser plays the whole event against a real server, with time sped up 50x
(the 70-minute event runs in about 85 seconds). Skipped automatically if Playwright isn't installed.

Run:  pytest tests/test_e2e.py -s
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

BACKEND = Path(__file__).resolve().parents[1]
SCALE = 0.02          # 1 event minute = 1.2 real seconds
ADMIN = "e2e-admin"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    port = free_port()
    env = {**os.environ, "TB_DB_PATH": str(tmp_path_factory.mktemp("db") / "e2e.db"),
           "TB_TIME_SCALE": str(SCALE), "TB_ADMIN_KEY": ADMIN}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
                            cwd=BACKEND, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/api/health"); break
        except OSError:
            time.sleep(0.2)
    yield base
    proc.terminate()


def secret_answers():
    data = json.loads((BACKEND / "data" / "answers.json").read_text())
    public = (BACKEND / "data" / "puzzles.public.json").read_text().lower()
    # distinctive answers only (words that are also ordinary puzzle text can't count as a leak)
    return [a for p in data["answers"].values() for a in p["accepted"]
            if len(a) >= 4 and not a.isdigit() and a.lower() not in public]


ANSWERS = {"cho-a": ("9", "43"), "cho-b": "workshop", "rigsby-a": "Radio Room", "rigsby-b": "till ten",
           "van-pelt-a": "nonstop", "van-pelt-b": "ice rink", "lisbon-a": "5", "lisbon-b": "4"}


def test_full_event(server):
    leaks, errors = [], []
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))
        hidden = secret_answers()

        def watch(resp):
            if "/api/" in resp.url and "/api/results" not in resp.url:
                try:
                    data = resp.json()
                except Exception:
                    return
                if isinstance(data, dict):
                    data.pop("submissions", None)   # the player's own typed answers, echoed back for receipts
                body = json.dumps(data).lower()
                leaks.extend(f"{resp.url}: {a}" for a in hidden if a.lower() in body)
        page.on("response", watch)

        # landing + check-in
        page.goto(server + "/")
        assert "TECH" in page.text_content("h1")
        page.click("text=Enter the investigation")
        page.fill("#ci-name", "E2E Detective")
        page.click("#ci-go")
        page.wait_for_selector(".reading strong")                      # reading timer started by the server

        # Cho is locked during reading, then opens on the server's clock
        page.goto(server + "/#/challenge/cho-a")
        assert page.locator(".field-card").count() == 1
        page.wait_for_selector("#ans", timeout=15000)                  # prompt arrived from the server
        assert "Question 01 of 8" in page.inner_text(".ws-center")    # numbering survives the server merge

        # hint unlocks after the (scaled) 5 minutes and shows the server's text
        page.wait_for_selector("#hint:not([disabled])", timeout=15000)
        page.click("#hint"); page.click("#confirm-hint")
        page.wait_for_selector(".hint-text")
        assert page.text_content(".hint-text").strip()

        # submit, then reload: the answer must still be locked in (server-side persistence)
        page.fill("#ans", "9"); page.fill("#ans-m", "43"); page.click("#submit"); page.click("#confirm-sub")
        page.wait_for_selector("#rcpt")
        page.reload(); page.wait_for_selector("#rcpt")
        assert page.input_value("#ans") == "9:43"

        for pid, ans in ANSWERS.items():
            if pid == "cho-a":
                continue
            page.goto(server + "/#/challenge/" + pid)
            page.wait_for_selector("#ans:not([readonly]), .field-card", timeout=15000)
            page.wait_for_selector("#ans:not([readonly])", timeout=40000)   # wait for this detective's window
            page.fill("#ans", ans); page.click("#submit"); page.click("#confirm-sub")
            page.wait_for_selector("#rcpt", timeout=10000)

        # no correctness anywhere before release
        body = page.inner_text("body")          # visible text only, not the page's own script
        assert not any(w in body for w in ["Correct", "Incorrect", "Not quite"])

        # final deduction opens when every case is closed (Lisbon's window must end)
        page.goto(server + "/#/final-deduction")
        page.wait_for_selector(".pick", timeout=40000)
        page.click('.pick label:has-text("Dr. Sam Whitley")'); page.click("#review"); page.click("#confirm")
        page.wait_for_url("**/#/results")
        assert "sealed" in page.text_content(".res-sealed")

        # organizer releases results; the player sees their rank and the epilogue opens
        req = urllib.request.Request(server + "/api/admin/release", data=b'{"released": true}', method="POST",
                                     headers={"Content-Type": "application/json", "X-Admin-Key": ADMIN})
        urllib.request.urlopen(req)
        page.reload()
        page.wait_for_selector(".reveal-line", timeout=10000)
        panel = page.text_content(".res-sealed")
        assert "Dr. Sam Whitley" in panel and "1 of 1" in panel and "4 of 4" in panel
        assert page.locator(".ep-entry a").text_content().strip() == "Continue investigation"
        page.screenshot(path=str(BACKEND / "tests" / "e2e-results.png"))
        browser.close()

    assert errors == [], errors
    assert leaks == [], leaks


def test_sixty_second_demo(server):
    """A visitor clicks the demo button and watches a full round. It must finish in about a minute,
    leave the visitor's own session alone, and stay off the real leaderboard."""
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(server + "/")
        start = time.time()
        page.click(".demo-btn")
        page.wait_for_selector("#db-play", timeout=120000)          # "Your turn?" end screen
        took = time.time() - start
        assert 45 <= took <= 90, f"demo took {took:.0f}s"
        panel = page.inner_text(".res-sealed")
        assert "Dr. Sam Whitley" in panel and "8 of 8" in panel
        assert page.evaluate("localStorage.getItem('tb_token')") is None   # demo session never stored
        page.click("#db-play")
        page.wait_for_selector("#ci-name")                                  # back to a normal check-in
        browser.close()
    assert errors == [], errors
    req = urllib.request.Request(server + "/api/admin/leaderboard", headers={"X-Admin-Key": ADMIN})
    names = [p["name"] for p in json.loads(urllib.request.urlopen(req).read())["players"]]
    assert "Demo Detective" not in names


def test_demo_survives_a_slow_connection(server):
    """Regression: on a slow link (a hosted server far away) the demo used to stall at Lisbon's Part B,
    because each request ate into a case's ~13-second window. Every API call is delayed here; the demo
    must still lock in all eight answers and finish."""
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})     # a phone, as a LinkedIn visitor would
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        def slow(route):
            time.sleep(0.45)
            route.continue_()
        page.route("**/api/**", slow)
        page.goto(server + "/")
        page.click(".demo-btn")
        page.wait_for_selector("#db-play", timeout=150000)
        panel = page.inner_text(".res-sealed")
        assert "8 of 8" in panel and "4 of 4" in panel, panel
        assert "closed before" not in page.inner_text("#demo-bar")
        browser.close()
    assert errors == [], errors


def test_no_timer_round(server):
    """'Play without a timer': every case is open at once, no countdowns, hints straight away, and the
    result is revealed as soon as the player accuses. It never joins the event leaderboard."""
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(server + "/")
        page.click("text=Play without a timer")
        assert page.is_checked('input[name="ci-mode"][value="practice"]')
        page.fill("#ci-name", "Recruiter")
        page.click("#ci-go")
        page.wait_for_url("**/#/briefing")
        assert "No timer" in page.inner_text(".side")

        # the last case first: nothing waits on a clock
        page.goto(server + "/#/challenge/lisbon-a")
        page.wait_for_selector("#ans:not([readonly])", timeout=5000)
        assert "Case closes in" not in page.inner_text(".ws-center")
        page.click("#hint"); page.click("#confirm-hint"); page.wait_for_selector(".hint-text")

        for pid, ans in ANSWERS.items():
            page.goto(server + "/#/challenge/" + pid)
            page.wait_for_selector("#ans:not([readonly])", timeout=5000)
            if pid == "cho-a":
                page.fill("#ans", ans[0]); page.fill("#ans-m", ans[1])
            else:
                page.fill("#ans", ans)
            page.click("#submit"); page.click("#confirm-sub")
            page.wait_for_selector("#rcpt", timeout=5000)

        page.goto(server + "/#/final-deduction")
        page.wait_for_selector(".pick", timeout=5000)
        page.click('.pick label:has-text("Dr. Sam Whitley")'); page.click("#review"); page.click("#confirm")
        page.wait_for_selector(".reveal-line", timeout=10000)                 # no organizer release needed
        panel = page.inner_text(".res-sealed")
        assert "Practice, no timer" in panel and "8 of 8" in panel and "Correct" in panel
        browser.close()
    assert errors == [], errors
    req = urllib.request.Request(server + "/api/admin/leaderboard", headers={"X-Admin-Key": ADMIN})
    names = [p["name"] for p in json.loads(urllib.request.urlopen(req).read())["players"]]
    assert "Recruiter" not in names
