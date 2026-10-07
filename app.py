"""Voice assistant inside a Telegram Mini App.

The browser talks to the OpenAI Realtime API directly over WebRTC. This server only:
  * checks Telegram initData (HMAC with the bot token, not older than a day, only OWNER_TG_ID),
  * mints a short-lived (120 s) client secret for the Realtime session with the assistant's instructions,
  * stores finished transcripts that another process can pick up (GET /api/transcripts with X-Api-Token).
The OpenAI key never reaches the browser.
"""
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

app = FastAPI()
DATA = Path(os.environ.get("DATA_DIR", "./data"))
NOTES = DATA / "notes.txt"                 # optional context for the assistant, pushed by your backend
TRANSCRIPTS = DATA / "transcripts.jsonl"
API_TOKEN = os.environ.get("API_TOKEN", "")          # for your backend: push notes, pull transcripts
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER = int(os.environ.get("OWNER_TG_ID", "0"))
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
MODEL = os.environ.get("REALTIME_MODEL", "gpt-realtime-mini")
VOICE = os.environ.get("REALTIME_VOICE", "marin")
LANGUAGE = os.environ.get("LANGUAGE", "en")
NAME = os.environ.get("ASSISTANT_NAME", "Assistant")
DAY = 24 * 3600


def backend_only(token: str):
    if not API_TOKEN or not hmac.compare_digest(token, API_TOKEN):
        raise HTTPException(403)


def check_init(init: str) -> dict:
    """Telegram initData: HMAC with the bot token, at most a day old, only the owner. Otherwise 401/403."""
    if not init or not BOT_TOKEN:
        raise HTTPException(401)
    pairs = dict(parse_qsl(init, keep_blank_values=True))
    signature = pairs.pop("hash", "")
    data_check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    if not signature or not hmac.compare_digest(hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest(), signature):
        raise HTTPException(401)
    if not pairs.get("auth_date", "").isdigit() or time.time() - int(pairs["auth_date"]) > DAY:
        raise HTTPException(401)
    try:
        user = json.loads(pairs.get("user") or "{}")
    except json.JSONDecodeError:
        raise HTTPException(401)
    if not OWNER or int(user.get("id") or 0) != OWNER:
        raise HTTPException(403)
    return user


def init_from(request: Request) -> str:
    a = request.headers.get("Authorization", "")
    return a[4:].strip() if a.startswith("tma ") else ""


def instructions() -> str:
    notes = NOTES.read_text(encoding="utf-8") if NOTES.exists() else "(no notes)"
    base = os.environ.get("ASSISTANT_PROMPT") or (
        f"You are {NAME}, a personal assistant. Speak naturally and warmly, in short sentences, one thought at a time, "
        "no lists. Listen for what the user wants: a task, a question or a decision. Repeat a task back in one sentence "
        "so they know you understood. Do not invent facts that are not in the notes; if you do not know, say you will "
        "check and reply in the chat. Greet once, at the very start. If interrupted, do not repeat yourself. "
        "When the user says goodbye, briefly list what you noted and say goodbye.")
    return base + "\n\nNOTES:\n" + notes


def client_secret() -> dict:
    session = {
        "type": "realtime", "model": MODEL, "output_modalities": ["audio"], "instructions": instructions(),
        "audio": {
            "input": {"transcription": {"model": "gpt-4o-mini-transcribe", "language": LANGUAGE},
                      "turn_detection": {"type": "server_vad", "threshold": 0.5, "prefix_padding_ms": 300,
                                         "silence_duration_ms": 500, "create_response": True, "interrupt_response": True}},
            "output": {"voice": VOICE}}}
    body = json.dumps({"expires_after": {"anchor": "created_at", "seconds": 120}, "session": session}).encode()
    r = urllib.request.Request("https://api.openai.com/v1/realtime/client_secrets", data=body, method="POST",
                               headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(r, timeout=20).read())
    except urllib.error.HTTPError as e:
        print(f"openai {e.code} {e.read()[:300]!r}", flush=True)
        raise HTTPException(502, "OpenAI did not issue a key")


@app.post("/api/token")
async def token(request: Request):
    check_init(init_from(request))
    if not OPENAI_KEY:
        raise HTTPException(503, "no OpenAI key")
    secret = await run_in_threadpool(client_secret)
    return {"value": secret["value"], "expires_at": secret.get("expires_at"), "model": MODEL}


@app.post("/api/notes")
async def put_notes(request: Request, x_api_token: str = Header("")):
    backend_only(x_api_token)
    text = (await request.body()).decode("utf-8")
    DATA.mkdir(parents=True, exist_ok=True)
    NOTES.write_text(text[:20000], encoding="utf-8")
    return {"ok": True, "chars": len(text)}


def read_transcripts() -> list:
    if not TRANSCRIPTS.exists():
        return []
    return [json.loads(s) for s in TRANSCRIPTS.read_text(encoding="utf-8").splitlines() if s.strip()]


@app.post("/api/transcript")
async def save_transcript(request: Request):
    body = await request.json()
    check_init(init_from(request) or body.get("initData", ""))
    turns = [{"t": str(x.get("t", ""))[:40], "who": "bot" if x.get("who") == "bot" else "user", "text": str(x.get("text", ""))[:4000]}
             for x in (body.get("turns") or [])[:500] if str(x.get("text", "")).strip()]
    sid = str(body.get("sid", ""))[:64]
    saved = read_transcripts()
    if sid and any(s["sid"] == sid for s in saved):
        return {"ok": True, "duplicate": True}
    # id in milliseconds: the host may have no persistent disk, a counter from 1 would restart after redeploy
    item = {"id": max([s["id"] + 1 for s in saved] + [int(time.time() * 1000)]), "sid": sid, "received": int(time.time()),
            "started": body.get("started"), "ended": body.get("ended"), "seconds": body.get("seconds"), "turns": turns}
    DATA.mkdir(parents=True, exist_ok=True)
    with TRANSCRIPTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")
    return {"ok": True, "id": item["id"]}


@app.get("/api/transcripts")
def list_transcripts(after: int = 0, x_api_token: str = Header("")):
    backend_only(x_api_token)
    return [s for s in read_transcripts() if s["id"] > after]


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def page():
    html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    return html.replace("{{NAME}}", json.dumps(NAME)[1:-1].replace("<", ""))
