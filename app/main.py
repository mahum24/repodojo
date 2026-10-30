import json
import traceback
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import dojo, llm, repo as repolib

STATIC = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="RepoDojo")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.on_event("startup")
def startup():
    llm.discover_models()
    dojo.get_runner()


def sse(gen):
    def stream():
        try:
            for ev in gen:
                yield f"data: {json.dumps(ev)}\n\n"
        except Exception as e:
            traceback.print_exc()
            yield f"data: {json.dumps({'step': 'error', 'status': 'error', 'message': str(e)})}\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def session(sid: str) -> dojo.Session:
    s = dojo.SESSIONS.get(sid)
    if not s:
        raise HTTPException(404, "Session expired. Start a new one.")
    return s


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/meta")
def meta():
    return {"curated": repolib.CURATED, "runner": dojo.get_runner().name, "models": llm.MODELS}


class Start(BaseModel):
    repo: str


@app.post("/api/start")
def start(body: Start):
    return sse(dojo.start(body.repo))


@app.get("/api/file")
def file(sid: str, path: str):
    s = session(sid)
    if path not in s.repo.files:
        raise HTTPException(404, "Only Python files can be opened.")
    return {"path": path, "content": s.source(path)}


class Chat(BaseModel):
    sid: str
    message: str
    path: str | None = None


@app.post("/api/chat")
def chat(body: Chat):
    return dojo.assistant(session(body.sid), body.message, body.path)


class Flag(BaseModel):
    sid: str
    id: str


@app.post("/api/flag")
def flag(body: Flag):
    dojo.flag(session(body.sid), body.id)
    return {"ok": True}


class Run(BaseModel):
    sid: str
    edits: dict[str, str] = {}


@app.post("/api/run")
def run(body: Run):
    return dojo.run_tests(session(body.sid), body.edits)


class Submit(BaseModel):
    sid: str
    edits: dict[str, str] = {}
    events: list = []
    quiz: dict = {}


@app.post("/api/submit")
def submit(body: Submit):
    return sse(dojo.submit(session(body.sid), body.edits, body.events, body.quiz))
