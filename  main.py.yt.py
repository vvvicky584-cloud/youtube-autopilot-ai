"""YouTube Autopilot AI - Phase 1: backend + dashboard + idea agent."""
import os, json, sqlite3, secrets
from pathlib import Path
import httpx
from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

DB = os.getenv("DB_PATH", "autopilot.db")
HERE = Path(__file__).parent
DEFAULTS = {"state": "stopped", "mode": "manual", "niche": "personal finance for beginners",
            "model": "gemini-2.5-flash", "daily_limit": "20", "auto_min_score": "70", "gemini_key": ""}

sec = HTTPBasic(auto_error=False)
def auth(c: HTTPBasicCredentials = Depends(sec)):
    pw = os.getenv("ADMIN_PASSWORD")
    if not pw:
        return
    if not c or not secrets.compare_digest(c.password.encode(), pw.encode()):
        raise HTTPException(401, "Login required", headers={"WWW-Authenticate": "Basic"})

app = FastAPI(title="YouTube Autopilot AI", dependencies=[Depends(auth)])

def q(sql, args=(), one=False, commit=False):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cur = con.execute(sql, args)
    rows = cur.fetchall()
    if commit:
        con.commit()
    con.close()
    return (rows[0] if rows else None) if one else rows

def init():
    for s in ["CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT)",
              "CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, level TEXT, msg TEXT)",
              "CREATE TABLE IF NOT EXISTS ideas(id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, title TEXT, hook TEXT, interest INTEGER, competition INTEGER, score INTEGER, status TEXT)",
              "CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, service TEXT, est_cost REAL)"]:
        q(s, commit=True)
init()

def get(k):
    r = q("SELECT v FROM settings WHERE k=?", (k,), one=True)
    return r["v"] if r else DEFAULTS[k]
def put(k, v):
    q("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)), commit=True)
def log(level, msg):
    q("INSERT INTO activity(level,msg) VALUES(?,?)", (level, msg), commit=True)
def calls_today():
    return q("SELECT COUNT(*) c FROM usage WHERE date(ts)=date('now')", one=True)["c"]

@app.get("/")
def home():
    return FileResponse(HERE / "static" / "index.html")

@app.get("/api/status")
def status():
    counts = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) n FROM ideas GROUP BY status")}
    cost = q("SELECT COALESCE(SUM(est_cost),0) c FROM usage", one=True)["c"]
    return {"state": get("state"), "mode": get("mode"), "niche": get("niche"), "model": get("model"),
            "key_set": bool(get("gemini_key") or os.getenv("GEMINI_API_KEY")),
            "calls_today": calls_today(), "daily_limit": int(get("daily_limit")),
            "auto_min_score": int(get("auto_min_score")), "idea_counts": counts, "est_cost": cost,
            "activity": [dict(r) for r in q("SELECT ts,level,msg FROM activity ORDER BY id DESC LIMIT 30")]}

@app.post("/api/agent/{action}")
def agent(action: str):
    m = {"start": "running", "pause": "paused", "stop": "stopped"}
    if action not in m:
        raise HTTPException(404)
    put("state", m[action])
    log("warn" if action == "stop" else "info", f"Agent {m[action]}" + (" (emergency stop)" if action == "stop" else ""))
    return {"state": m[action]}

class ModeIn(BaseModel):
    mode: str
@app.post("/api/mode")
def set_mode(b: ModeIn):
    if b.mode not in ("manual", "semi", "full"):
        raise HTTPException(400, "Invalid mode")
    put("mode", b.mode)
    log("info", f"Mode set to {b.mode}")
    return {"mode": b.mode}

class SettingsIn(BaseModel):
    niche: str
    model: str
    daily_limit: int
    auto_min_score: int
    gemini_key: str = ""
@app.post("/api/settings")
def save_settings(b: SettingsIn):
    put("niche", b.niche.strip()[:120]); put("model", b.model.strip())
    put("daily_limit", max(1, min(b.daily_limit, 1000))); put("auto_min_score", max(0, min(b.auto_min_score, 100)))
    if b.gemini_key.strip():
        put("gemini_key", b.gemini_key.strip())
        log("info", "Gemini API key saved")
    log("info", "Settings saved")
    return {"ok": True}

@app.delete("/api/settings/key")
def disconnect_key():
    put("gemini_key", "")
    log("warn", "Gemini API key removed (disconnected)")
    return {"ok": True}

@app.post("/api/ideas/generate")
async def generate():
    if get("state") != "running":
        raise HTTPException(409, "Agent is not running. Press Start first.")
    key = get("gemini_key") or os.getenv("GEMINI_API_KEY")
    if not key:
        raise HTTPException(400, "No Gemini API key. Add one in Settings (free at aistudio.google.com/apikey).")
    if calls_today() >= int(get("daily_limit")):
        log("warn", "Daily API limit reached - generation blocked")
        raise HTTPException(429, "Daily limit reached. Raise it in Settings if you want.")
    niche = get("niche")
    prompt = (f"You are a YouTube strategist. Niche: {niche}. Suggest 10 ORIGINAL video ideas that fit current audience "
              "interest and avoid copying any specific creator. Return ONLY a JSON array of objects with keys: "
              "title (string), hook (string, first 5 seconds), interest (integer 1-10), competition (integer 1-10).")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{get('model')}:generateContent"
    log("info", f"Researching ideas for niche: {niche}")
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(url, headers={"x-goog-api-key": key}, json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json"}})
    except httpx.HTTPError as e:
        log("error", f"Network error: {type(e).__name__}")
        raise HTTPException(502, "Could not reach the Gemini API.")
    q("INSERT INTO usage(service,est_cost) VALUES('gemini',0)", commit=True)
    if r.status_code == 429:
        log("error", "Free quota exhausted (HTTP 429). Wait or check your limits.")
        raise HTTPException(429, "Gemini free quota exhausted. Try later.")
    if r.status_code != 200:
        log("error", f"Gemini error HTTP {r.status_code}")
        raise HTTPException(502, f"Gemini returned HTTP {r.status_code}. Check key and model name in Settings.")
    try:
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(text)
        if isinstance(data, dict):
            data = next(iter(data.values()))
        items = list(data)[:10]
    except Exception:
        log("error", "Could not parse AI response")
        raise HTTPException(502, "AI response was not valid JSON. Try again.")
    mode, minscore, added, auto = get("mode"), int(get("auto_min_score")), 0, 0
    for it in items:
        try:
            i = max(1, min(int(it["interest"]), 10)); co = max(1, min(int(it["competition"]), 10))
            score = round(i * 6 + (10 - co) * 4)
            st = "approved" if mode == "full" and score >= minscore else "pending"
            auto += st == "approved"
            q("INSERT INTO ideas(title,hook,interest,competition,score,status) VALUES(?,?,?,?,?,?)",
              (str(it["title"])[:200], str(it.get("hook", ""))[:300], i, co, score, st), commit=True)
            added += 1
        except (KeyError, ValueError, TypeError):
            continue
    log("info", f"Added {added} ideas ({auto} auto-approved by rules)")
    return {"added": added}

@app.get("/api/ideas")
def ideas():
    return [dict(r) for r in q("SELECT * FROM ideas ORDER BY score DESC, id DESC LIMIT 100")]

@app.post("/api/ideas/{iid}/{action}")
def idea_action(iid: int, action: str):
    if action not in ("approve", "reject"):
        raise HTTPException(404)
    st = "approved" if action == "approve" else "rejected"
    q("UPDATE ideas SET status=? WHERE id=?", (st, iid), commit=True)
    log("info", f"Idea #{iid} {st}")
    return {"status": st}
