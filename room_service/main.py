from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict
from fastapi.middleware.cors import CORSMiddleware
import random, time, requests, logging, threading

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("room_service")

app = FastAPI(title="Room Service (short code)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ROOMS: Dict[str, dict] = {}

class CreateRoomIn(BaseModel):
    name: Optional[str] = "Room"
    max_players: int = 2
    username: str

class RoomOut(BaseModel):
    id: str
    name: str
    host: str
    players: List[str]
    state: str
    created_at: float
    max_players: int

class JoinIn(BaseModel):
    username: str

def generate_room_code() -> str:
    while True:
        code = f"{random.randint(10000, 99999)}"
        if code not in ROOMS:
            return code

GAME_SERVICE_URL = "http://127.0.0.1:8003"

def notify_game_service_of_full_room(room: dict, max_retries: int = 5, retry_delay: float = 0.6):
    payload = {
        "game_id": room["id"],
        "players": room["players"],
        "starting": room["players"][0] if room["players"] else None
    }
    url = f"{GAME_SERVICE_URL}/games"
    for attempt in range(1, max_retries + 1):
        try:
            logger.info("Notify attempt %d -> %s", attempt, url)
            r = requests.post(url, json=payload, timeout=5)
            if r.status_code in (200, 201):
                try:
                    return {"ok": True, "resp": r.json()}
                except Exception:
                    return {"ok": True, "resp_raw": r.text}
            else:
                logger.warning("Game service returned %s: %s", r.status_code, r.text)
        except Exception as e:
            logger.warning("Notify attempt %d failed: %s", attempt, e)
        time.sleep(retry_delay * attempt)
    logger.error("Failed to notify game service for room %s after %d attempts", room["id"], max_retries)
    return {"ok": False}

def notify_game_service_background(room: dict, max_retries: int = 10, base_delay: float = 0.5):
    def worker():
        res = notify_game_service_of_full_room(room, max_retries=max_retries, retry_delay=base_delay)
        if not res.get("ok"):
            logger.error("Background notify failed for room %s", room["id"])
        else:
            logger.info("Background notify succeeded for room %s", room["id"])
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return True

@app.get("/rooms", response_model=List[RoomOut])
def list_rooms():
    return list(ROOMS.values())

@app.post("/create-room", response_model=RoomOut, status_code=201)
def create_room(req: CreateRoomIn):
    if not req.username:
        raise HTTPException(status_code=400, detail="Username is required to create a room")
    room_id = generate_room_code()
    room = {
        "id": room_id,
        "name": req.name or "Room",
        "host": req.username,
        "players": [req.username],
        "state": "waiting",
        "created_at": time.time(),
        "max_players": req.max_players,
    }
    ROOMS[room_id] = room
    logger.info("Room created: %s by %s", room_id, req.username)
    return room

@app.post("/join-room/{room_id}", response_model=RoomOut)
def join_room(room_id: str, body: JoinIn):
    if room_id not in ROOMS:
        raise HTTPException(status_code=404, detail="Room not found")
    room = ROOMS[room_id]
    if body.username in room["players"]:
        return room
    if len(room["players"]) >= room["max_players"]:
        raise HTTPException(status_code=400, detail="Room is full")
    room["players"].append(body.username)
    logger.info("User %s joined room %s", body.username, room_id)
    if len(room["players"]) >= room["max_players"]:
        room["state"] = "full"
        logger.info("Room %s is now full (players: %s). Scheduling background notify to Game Rules service...", room_id, room["players"])
        notify_game_service_background(room)
    return room

@app.post("/start-game/{room_id}", response_model=RoomOut)
def start_game(room_id: str, body: JoinIn):
    if room_id not in ROOMS:
        raise HTTPException(status_code=404, detail="Room not found")
    room = ROOMS[room_id]
    if body.username != room.get("host"):
        raise HTTPException(status_code=403, detail="Only host can start the game")
    if len(room["players"]) < 2:
        raise HTTPException(status_code=400, detail="Need at least 2 players to start")
    if room["state"] == "in_progress":
        return room
    room["state"] = "in_progress"
    logger.info("Host %s started game for room %s; scheduling background notify...", body.username, room_id)
    notify_game_service_background(room)
    return room

@app.get("/room/{room_id}", response_model=RoomOut)
def get_room(room_id: str):
    if room_id not in ROOMS:
        raise HTTPException(status_code=404, detail="Room not found")
    return ROOMS[room_id]

@app.post("/leave-room/{room_id}")
def leave_room(room_id: str, body: JoinIn):
    if room_id not in ROOMS:
        raise HTTPException(status_code=404, detail="Room not found")
    room = ROOMS[room_id]
    if body.username in room["players"]:
        room["players"].remove(body.username)
    if not room["players"]:
        ROOMS.pop(room_id, None)
        logger.info("Room %s deleted (no players left)", room_id)
        return {"detail": "room deleted"}
    if room["host"] not in room["players"]:
        room["host"] = room["players"][0]
    room["state"] = "waiting"
    logger.info("User %s left room %s. State set to waiting.", body.username, room_id)
    return room
