"""Fixed integration fixture, copied only into disposable generated workspaces."""

import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

BUG_OWNER_FILTER = True
DB = os.environ["DATABASE_URL"].removeprefix("sqlite:///")
with sqlite3.connect(DB) as connection:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS qa_records (id INTEGER PRIMARY KEY, owner TEXT, title TEXT)"
    )

# The untrusted application must not be able to replace QA runner evidence.
try:
    Path("/tmp/evidence/forged.json").write_text('{"status":"passed"}')
except (PermissionError, FileNotFoundError):
    pass
else:
    raise RuntimeError("Application can forge QA evidence")

app = FastAPI()


class Credentials(BaseModel):
    username: str
    password: str


class Record(BaseModel):
    title: str = Field(min_length=1, max_length=100)


def identity(authorization):
    name = (authorization or "").removeprefix("Bearer ")
    if name not in {"alice", "bob"}:
        raise HTTPException(401, "Authentication required")
    return name


@app.get("/api/v1/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/login")
def login(body: Credentials):
    if body.username not in {"alice", "bob"} or body.password != "test-only":
        raise HTTPException(403, "Wrong credentials")
    return {"token": body.username}


@app.post("/api/v1/records", status_code=201)
def create(body: Record, authorization: str | None = Header(default=None)):
    owner = identity(authorization)
    with sqlite3.connect(DB) as connection:
        cursor = connection.execute(
            "INSERT INTO qa_records(owner,title) VALUES (?,?)", (owner, body.title)
        )
        return {"id": cursor.lastrowid, "title": body.title}


@app.get("/api/v1/records")
def records(authorization: str | None = Header(default=None)):
    owner = identity(authorization)
    with sqlite3.connect(DB) as connection:
        rows = connection.execute(
            "SELECT id,title FROM qa_records"
            if BUG_OWNER_FILTER
            else "SELECT id,title FROM qa_records WHERE owner=?",
            () if BUG_OWNER_FILTER else (owner,),
        ).fetchall()
    return {"items": [{"id": row[0], "title": row[1]} for row in rows]}


@app.get("/api/v1/records/{record_id}")
def get_record(record_id: int, authorization: str | None = Header(default=None)):
    owner = identity(authorization)
    with sqlite3.connect(DB) as connection:
        row = connection.execute(
            "SELECT title,owner FROM qa_records WHERE id=?", (record_id,)
        ).fetchone()
    if row is None or (not BUG_OWNER_FILTER and row[1] != owner):
        raise HTTPException(404, "Not found")
    return {"id": record_id, "title": row[0]}
