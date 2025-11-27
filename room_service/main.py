# main.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict
from fastapi.middleware.cors import CORSMiddleware
import random, time, requests, logging, threading

# logging
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
    username: str  # creator's username

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
    """Generate a random 5-digit numeric room code as string (e.g. '48239')"""
    while True:
        code = f"{random.randint(10000, 99999)}"
        if code not in ROOMS:
            return code

# Configure your game service URL (change if necessary)
GAME_SERVICE_URL = "http://127.0.0.1:8003"

def notify_game_service_of_full_room(room: dict, max_retries: int = 3, retry_delay: float = 0.6):
    """
    Notify Game Rules service to create a game using room.id as game id.
    Retries a few times if the game service is temporarily unavailable.
    This function is safe to call from a background thread.
    """
    payload = {
        "game_id": room["id"],
        "players": room["players"],
        "starting": room["players"][0] if room["players"] else None
    }
    url = f"{GAME_SERVICE_URL}/games"
    for attempt in range(1, max_retries + 1):
        try:
            logger.info("Notify attempt %d -> %s (room=%s)", attempt, url, room["id"])
            r = requests.post(url, json=payload, timeout=5)
            if r.status_code in (200, 201):
                logger.info("Game created for room %s (status %s)", room["id"], r.status_code)
                return True
            else:
                logger.warning("Game service returned %s for room %s: %s", r.status_code, room["id"], r.text[:200])
        except Exception as e:
            logger.warning("Notify attempt %d failed for room %s: %s", attempt, room["id"], e)
        time.sleep(retry_delay * attempt)
    logger.error("Failed to notify game service for room %s after %d attempts", room["id"], max_retries)
    return False

def notify_in_background(room: dict):
    """
    Start a background thread to notify the game service so the HTTP handler
    doesn't block. Thread is daemon so it won't prevent process exit.
    """
    t = threading.Thread(target=notify_game_service_of_full_room, args=(room,), daemon=True)
    t.start()
    logger.info("Spawned background notifier thread for room %s", room["id"])

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
        logger.info("Room %s is now full (players: %s). Notifying Game Rules service...", room_id, room["players"])
        # notify in background so join returns quickly
        notify_in_background(room)
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

@app.post("/start-game/{room_id}", response_model=RoomOut)
def start_game(room_id: str, body: JoinIn):
    """
    Host-only endpoint to force-start the game for a room.
    Body must include {"username":"<host>"} for verification.
    """
    if room_id not in ROOMS:
        raise HTTPException(status_code=404, detail="Room not found")
    room = ROOMS[room_id]

    # verify caller is host
    if not body.username or body.username != room["host"]:
        raise HTTPException(status_code=403, detail="Only the host may start the game")

    # ensure enough players
    if len(room["players"]) < 2:
        raise HTTPException(status_code=400, detail="Not enough players to start the game")

    # mark full and notify game service
    room["state"] = "full"
    logger.info("Host %s started game for room %s (players=%s)", body.username, room_id, room["players"])
    notify_in_background(room)   # uses your existing background notifier

    return room

@app.post("/notify-game/{room_id}")
def manual_notify_game(room_id: str):
    """
    Manual endpoint to trigger notification to game service for a specific room.
    Useful for retrying when automatic notify failed.
    """
    if room_id not in ROOMS:
        raise HTTPException(status_code=404, detail="Room not found")
    room = ROOMS[room_id]
    if room["state"] != "full":
        raise HTTPException(status_code=400, detail="Room is not full")
    ok = notify_game_service_of_full_room(room)
    if ok:
        return {"detail": "Notified game service"}
    raise HTTPException(status_code=502, detail="Failed to notify game service")
