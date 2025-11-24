# game_rules_service/main.py - persistent games with WebSocket move handling + token validation
import os
import time
import json
from uuid import uuid4
from typing import List, Dict, Optional
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, String, Integer, Float, Text
from sqlalchemy.orm import declarative_base, sessionmaker
import requests

# Config (can be overridden with env)
DATABASE_URL = os.getenv("GAME_DB", "sqlite:///./game_store.db")
USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", "http://127.0.0.1:8001")

# DB setup
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
Base = declarative_base()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class GameModel(Base):
    __tablename__ = "games"
    id = Column(String, primary_key=True, index=True)
    board = Column(Text)  # json list
    players = Column(Text)  # json list
    turn = Column(String)
    symbols = Column(Text)  # json dict
    state = Column(String)
    winner = Column(String, nullable=True)
    created_at = Column(Float, default=time.time)

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Game Rules Service (WS moves + persistent)")

# in-memory websocket connections: game_id -> list of websockets
WS_CONNECTIONS: Dict[str, List[WebSocket]] = {}

# --- Pydantic models -----------------------------------------------------
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

class MoveIn(BaseModel):
    player: str
    position: int

# --- game helpers -------------------------------------------------------
WIN_LINES = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
def new_board(): return [None]*9
def check_winner(board):
    for a,b,c in WIN_LINES:
        if board[a] and board[a]==board[b]==board[c]:
            return board[a]
    if all(cell is not None for cell in board): return "draw"
    return None

# --- persistence helpers ------------------------------------------------
def persist_game(session, g: dict):
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
    if not gm: return None
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

# --- broadcast ----------------------------------------------------------
def broadcast_update(game_id: str):
    conns = WS_CONNECTIONS.get(game_id, [])
    session = SessionLocal()
    g = load_game(session, game_id)
    session.close()
    if not g:
        return
    payload = {"type":"game_update","game":g}
    text = json.dumps(payload)
    import asyncio
    # send asynchronously to all conns; prune closed ones
    for ws in list(conns):
        try:
            asyncio.create_task(ws.send_text(text))
        except Exception:
            try:
                conns.remove(ws)
            except ValueError:
                pass
    WS_CONNECTIONS[game_id] = conns

# --- user token validation ----------------------------------------------
def validate_token_and_get_username(token: str) -> str:
    """
    Token expected as raw JWT string. Calls User Service /validate-session.
    Raises HTTPException on failure.
    """
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        r = requests.post(f"{USER_SERVICE_URL}/validate-session", headers=headers, timeout=5)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"User service unreachable: {e}")
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid token")
    j = r.json()
    return j.get("username")

# --- REST endpoints (kept for compatibility) ----------------------------
@app.post("/games", response_model=GameStateOut, status_code=201)
def create_game(payload: CreateGameIn):
    gid = payload.game_id or str(uuid4())
    session = SessionLocal()
    if session.query(GameModel).filter(GameModel.id==gid).first():
        session.close()
        raise HTTPException(status_code=400, detail="Game exists")
    players = payload.players
    starting = payload.starting or players[0]
    symbols = {players[0]:"X", players[1]:"O"}
    game = {"id":gid, "board":new_board(), "players":players.copy(), "turn":starting, "symbols":symbols, "state":"in_progress", "winner":None, "created_at":time.time()}
    persist_game(session, game)
    session.close()
    WS_CONNECTIONS.setdefault(gid, [])
    broadcast_update(gid)
    return GameStateOut(**game)

@app.get("/games/{game_id}", response_model=GameStateOut)
def get_game(game_id: str):
    session = SessionLocal()
    g = load_game(session, game_id)
    session.close()
    if not g: raise HTTPException(status_code=404, detail="Game not found")
    return GameStateOut(**g)

# --- WebSocket endpoint: accepts commands (move, ping) -------------------
@app.websocket("/ws/games/{game_id}")
async def websocket_game(websocket: WebSocket, game_id: str):
    """
    WebSocket message protocol (JSON):
      - client -> server:
         {"cmd":"move","position":<0..8>, "token":"<jwt>"}   # move request (or token in query param)
         {"cmd":"ping"}                                      # keepalive
      - server -> client:
         {"type":"game_update","game":{...}}                 # full game state
         {"type":"error","detail":"..."}                     # error message
         {"type":"pong"}                                     # respond to ping
    Token: can be sent per-message or as ?token=... query parameter.
    """
    await websocket.accept()

    # register connection
    WS_CONNECTIONS.setdefault(game_id, []).append(websocket)

    # send current state immediately (if exists)
    session = SessionLocal()
    g = load_game(session, game_id)
    session.close()
    if not g:
        await websocket.send_text(json.dumps({"type":"error","detail":"game not found"}))
        await websocket.close()
        try:
            WS_CONNECTIONS[game_id].remove(websocket)
        except Exception:
            pass
        return

    # send initial state
    await websocket.send_text(json.dumps({"type":"game_update","game":g}))

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                await websocket.send_text(json.dumps({"type":"error","detail":"invalid json"}))
                continue

            cmd = msg.get("cmd")
            if not cmd:
                await websocket.send_text(json.dumps({"type":"error","detail":"missing cmd"}))
                continue

            # ping / heartbeat
            if cmd == "ping":
                await websocket.send_text(json.dumps({"type":"pong"}))
                continue

            # MOVE command: validate token + apply move
            if cmd == "move":
                # token can be per-message or in query params; check message first
                token = msg.get("token")
                # if not provided in message, try to get from initial websocket query (if any)
                if not token:
                    # fastapi stores query params in websocket.query_params
                    token = websocket.query_params.get("token")
                if not token:
                    await websocket.send_text(json.dumps({"type":"error","detail":"missing token"}))
                    continue

                # validate token via User Service
                try:
                    username = validate_token_and_get_username(token)
                except HTTPException as he:
                    await websocket.send_text(json.dumps({"type":"error","detail":f"auth failed: {he.detail}"}))
                    continue

                position = msg.get("position")
                if position is None or not isinstance(position, int):
                    await websocket.send_text(json.dumps({"type":"error","detail":"invalid position"}))
                    continue

                # load latest game state
                session = SessionLocal()
                g = load_game(session, game_id)
                if not g:
                    session.close()
                    await websocket.send_text(json.dumps({"type":"error","detail":"game not found"}))
                    continue

                # validate game status & player & turn
                if g["state"] != "in_progress":
                    session.close()
                    await websocket.send_text(json.dumps({"type":"error","detail":"game not in progress"}))
                    continue

                if username not in g["players"]:
                    session.close()
                    await websocket.send_text(json.dumps({"type":"error","detail":"you are not a player in this game"}))
                    continue

                if g["turn"] != username:
                    session.close()
                    await websocket.send_text(json.dumps({"type":"error","detail":"not your turn"}))
                    continue

                if position < 0 or position > 8:
                    session.close()
                    await websocket.send_text(json.dumps({"type":"error","detail":"position must be 0..8"}))
                    continue

                if g["board"][position] is not None:
                    session.close()
                    await websocket.send_text(json.dumps({"type":"error","detail":"cell already taken"}))
                    continue

                # apply move
                symbol = g["symbols"].get(username)
                if symbol is None:
                    session.close()
                    await websocket.send_text(json.dumps({"type":"error","detail":"no symbol assigned"}))
                    continue

                g["board"][position] = symbol
                winner_symbol = check_winner(g["board"])
                if winner_symbol == "draw":
                    g["state"] = "finished"
                    g["winner"] = "draw"
                elif winner_symbol in ("X","O"):
                    g["state"] = "finished"
                    for p,s in g["symbols"].items():
                        if s == winner_symbol:
                            g["winner"] = p
                            break
                else:
                    p0,p1 = g["players"][0], g["players"][1]
                    g["turn"] = p1 if g["turn"] == p0 else p0

                # persist and broadcast updated state
                persist_game(session, g)
                session.close()
                broadcast_update(game_id)
                # done - server broadcasts update to everyone (including move origin)
                continue

            # unknown command
            await websocket.send_text(json.dumps({"type":"error","detail":"unknown cmd"}))

    except WebSocketDisconnect:
        # remove connection
        try:
            WS_CONNECTIONS[game_id].remove(websocket)
        except Exception:
            pass
    except Exception:
        # on any error, try to remove and close
        try:
            WS_CONNECTIONS[game_id].remove(websocket)
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass
