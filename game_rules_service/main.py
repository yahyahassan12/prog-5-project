import os
import time
import json
from uuid import uuid4
from typing import List, Dict, Optional
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, String, Float, Text
from sqlalchemy.orm import declarative_base, sessionmaker
import requests

DATABASE_URL = os.getenv("GAME_DB", "sqlite:///./game_store.db")
USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", "http://127.0.0.1:8001")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
Base = declarative_base()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class GameModel(Base):
    __tablename__ = "games"
    id = Column(String, primary_key=True)
    board = Column(Text)
    players = Column(Text)
    turn = Column(String)
    symbols = Column(Text)
    state = Column(String)
    winner = Column(String, nullable=True)
    created_at = Column(Float, default=time.time)

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Game Rules Service")

WS_CONNECTIONS: Dict[str, List[WebSocket]] = {}

class CreateGameIn(BaseModel):
    game_id: Optional[str] = None
    players: List[str] = Field(..., min_items=2, max_items=2)
    starting: Optional[str] = None

class GameStateOut(BaseModel):
    id: str
    board: List[Optional[str]]
    players: List[str]
    turn: str
    symbols: Dict[str, str]
    state: str
    winner: Optional[str] = None
    created_at: float

WIN_LINES = [
    (0,1,2),(3,4,5),(6,7,8),
    (0,3,6),(1,4,7),(2,5,8),
    (0,4,8),(2,4,6)
]

def new_board():
    return [None]*9

def check_winner(board):
    for a,b,c in WIN_LINES:
        if board[a] and board[a]==board[b]==board[c]:
            return board[a]
    if all(x is not None for x in board):
        return "draw"
    return None

def persist_game(session, g):
    gm = session.query(GameModel).filter(GameModel.id==g["id"]).first()
    payload = {
        "board": json.dumps(g["board"]),
        "players": json.dumps(g["players"]),
        "symbols": json.dumps(g["symbols"]),
        "turn": g["turn"],
        "state": g["state"],
        "winner": g.get("winner"),
        "created_at": g["created_at"]
    }
    if gm:
        for k,v in payload.items():
            setattr(gm, k, v)
    else:
        gm = GameModel(id=g["id"], **payload)
        session.add(gm)
    session.commit()

def load_game(session, gid):
    gm = session.query(GameModel).filter(GameModel.id==gid).first()
    if not gm:
        return None
    return {
        "id": gm.id,
        "board": json.loads(gm.board),
        "players": json.loads(gm.players),
        "turn": gm.turn,
        "symbols": json.loads(gm.symbols),
        "state": gm.state,
        "winner": gm.winner,
        "created_at": gm.created_at
    }

def broadcast_update(game_id):
    conns = WS_CONNECTIONS.get(game_id, [])
    session = SessionLocal()
    g = load_game(session, game_id)
    session.close()
    if not g:
        return
    payload = json.dumps({"type":"game_update","game":g})
    import asyncio
    for ws in conns.copy():
        try:
            asyncio.create_task(ws.send_text(payload))
        except:
            try:
                conns.remove(ws)
            except:
                pass

def validate_token_and_get_username(token: str) -> str:
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.post(f"{USER_SERVICE_URL}/validate-session", headers=headers, timeout=5)
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid token")
    return r.json().get("username")

@app.post("/games", response_model=GameStateOut, status_code=201)
def create_game(payload: CreateGameIn):
    gid = payload.game_id or str(uuid4())

    session = SessionLocal()
    if session.query(GameModel).filter(GameModel.id == gid).first():
        session.close()
        raise HTTPException(status_code=400, detail="Game exists")

    players = payload.players
    starting = payload.starting or players[0]
    symbols = {players[0]:"X", players[1]:"O"}

    game = {
        "id": gid,
        "board": new_board(),
        "players": players.copy(),
        "turn": starting,
        "symbols": symbols,
        "state": "in_progress",
        "winner": None,
        "created_at": time.time(),
    }

    persist_game(session, game)
    session.close()
    WS_CONNECTIONS.setdefault(gid, [])
    broadcast_update(gid)

    return GameStateOut(**game)

@app.get("/games/{gid}", response_model=GameStateOut)
def get_game(gid: str):
    session = SessionLocal()
    g = load_game(session, gid)
    session.close()
    if not g:
        raise HTTPException(status_code=404, detail="Game not found")
    return GameStateOut(**g)

@app.websocket("/ws/games/{gid}")
async def websocket_game(ws: WebSocket, gid: str):
    await ws.accept()

    WS_CONNECTIONS.setdefault(gid, []).append(ws)

    session = SessionLocal()
    g = load_game(session, gid)
    session.close()

    if not g:
        await ws.send_text(json.dumps({"type":"error","detail":"game not found"}))
        await ws.close()
        WS_CONNECTIONS[gid].remove(ws)
        return

    await ws.send_text(json.dumps({"type":"game_update","game":g}))

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            cmd = msg.get("cmd")

            if cmd == "ping":
                await ws.send_text(json.dumps({"type":"pong"}))
                continue

            if cmd == "set_turn":
                token = msg.get("token") or ws.query_params.get("token")
                username = validate_token_and_get_username(token)
                new_player = msg.get("player")

                session = SessionLocal()
                g = load_game(session, gid)

                if not g:
                    session.close()
                    await ws.send_text(json.dumps({"type":"error","detail":"game not found"}))
                    continue

                if username not in g["players"]:
                    session.close()
                    await ws.send_text(json.dumps({"type":"error","detail":"you are not a player"}))
                    continue

                if new_player not in g["players"]:
                    session.close()
                    await ws.send_text(json.dumps({"type":"error","detail":"invalid player"}))
                    continue

                g["turn"] = new_player
                persist_game(session, g)
                session.close()
                broadcast_update(gid)
                continue

            if cmd == "move":
                token = msg.get("token") or ws.query_params.get("token")
                username = validate_token_and_get_username(token)
                pos = msg.get("position")

                session = SessionLocal()
                g = load_game(session, gid)

                if g["state"] != "in_progress":
                    session.close()
                    await ws.send_text(json.dumps({"type":"error","detail":"not in progress"}))
                    continue

                if username not in g["players"]:
                    session.close()
                    await ws.send_text(json.dumps({"type":"error","detail":"not a player"}))
                    continue

                if g["turn"] != username:
                    session.close()
                    await ws.send_text(json.dumps({"type":"error","detail":"not your turn"}))
                    continue

                if pos < 0 or pos > 8 or g["board"][pos] is not None:
                    session.close()
                    await ws.send_text(json.dumps({"type":"error","detail":"invalid move"}))
                    continue

                symbol = g["symbols"][username]
                g["board"][pos] = symbol

                w = check_winner(g["board"])
                if w == "draw":
                    g["state"] = "finished"
                    g["winner"] = "draw"
                elif w in ("X","O"):
                    g["state"] = "finished"
                    g["winner"] = username
                else:
                    p0, p1 = g["players"]
                    g["turn"] = p1 if g["turn"] == p0 else p0

                persist_game(session, g)
                session.close()
                broadcast_update(gid)
                continue

            await ws.send_text(json.dumps({"type":"error","detail":"unknown cmd"}))

    except WebSocketDisconnect:
        try:
            WS_CONNECTIONS[gid].remove(ws)
        except:
            pass
