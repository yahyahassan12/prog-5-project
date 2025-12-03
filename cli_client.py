import asyncio
import json
import requests
import websockets
import os
import sys
import time
from getpass import getpass

USER_BASE = "http://127.0.0.1:8001"
ROOM_BASE = "http://127.0.0.1:8002"
GAME_BASE = "http://127.0.0.1:8003"

token = None
username = None
local_mode = False


# ------------------ AUTH ------------------
def login():
    global token, username
    username = input("Username: ").strip()
    pw = getpass("Password: ")

    if not username:
        print("Username required.")
        return False

    try:
        res = requests.post(USER_BASE + "/login", json={"username": username, "password": pw})
    except Exception as e:
        print("Login failed:", e)
        return False

    if res.status_code == 200:
        token = res.json().get("access_token")
        print("Login successful.")
        return True
    else:
        print("Login failed:", res.status_code, res.text)
        return False


def register():
    u = input("Choose username: ").strip()
    pw = getpass("Choose password: ")

    if not u:
        print("Username required.")
        return

    r = requests.post(USER_BASE + "/register", json={"username": u, "password": pw})

    if r.status_code in (200, 201):
        print("Registered:", u)
    else:
        print("Register failed:", r.status_code, r.text)


# ------------------ HELPERS ------------------
def auth_headers():
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    return h


def pretty_board(b):
    b = [c if c is not None else " " for c in b]
    rows = [
        f"{b[0]} | {b[1]} | {b[2]}",
        "---------",
        f"{b[3]} | {b[4]} | {b[5]}",
        "---------",
        f"{b[6]} | {b[7]} | {b[8]}",
    ]
    return "\n".join(rows)


# ------------------ ROOM SERVICE ACTIONS ------------------
def create_room():
    name = input("Room name (optional): ").strip() or "Room"
    max_p = input("Max players (default 2): ").strip() or "2"

    payload = {"name": name, "max_players": int(max_p), "username": username}

    try:
        r = requests.post(ROOM_BASE + "/create-room", json=payload, headers=auth_headers())
    except Exception as e:
        print("Room service unreachable:", e)
        return None

    if r.status_code in (200, 201):
        room = r.json()
        print("Created room:", room["id"])
        return room["id"]
    else:
        print("Create failed:", r.status_code, r.text)
        return None


def join_room(room_id):
    global username

    try:
        r = requests.post(
            f"{ROOM_BASE}/join-room/{room_id}",
            json={"username": username},
            headers=auth_headers()
        )
    except Exception as e:
        print("Room service unreachable:", e)
        return False

    if r.status_code == 200:
        print("Joined", room_id)
        return True
    else:
        print("Join failed:", r.status_code, r.text)
        return False


def start_game(room_id):
    try:
        r = requests.post(
            f"{ROOM_BASE}/start-game/{room_id}",
            json={"username": username},
            headers=auth_headers()
        )
    except Exception as e:
        print("Start-game error:", e)
        return False

    if r.status_code == 200:
        print("Start request accepted.")
        return True
    else:
        print("Start failed:", r.status_code, r.text)
        return False


def wait_for_game(room_id, timeout=40):
    print("Waiting for Game Rules to create the match...")

    start = time.time()
    dots = 0

    while time.time() - start < timeout:
        try:
            r = requests.get(f"{GAME_BASE}/games/{room_id}", timeout=2)
        except:
            r = None

        if r and r.status_code == 200:
            print("\nGame created!")
            return True

        print(".", end="", flush=True)
        dots += 1
        if dots % 20 == 0:
            print()

        time.sleep(1.2)

    print("\nTimed out.")
    return False


# ------------------ GAMEPLAY (WEBSOCKET) ------------------
async def play_ws(room_id):
    global token, username, local_mode

    ws_url = f"ws://127.0.0.1:8003/ws/games/{room_id}"
    if token:
        ws_url += f"?token={token}"

    print("Connecting:", ws_url)

    backoff = 1

    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                print("Connected to Game WebSocket.")

                while True:
                    msg = await ws.recv()

                    try:
                        obj = json.loads(msg)
                    except:
                        print("Non-JSON:", msg)
                        continue

                    if obj.get("type") == "game_update":
                        g = obj["game"]
                        print("\n=== GAME UPDATE ===")
                        print("Players:", ", ".join(g["players"]))
                        print("Turn:", g["turn"])
                        print(pretty_board(g["board"]))

                        if g["state"] == "finished":
                            if g["winner"] == "draw":
                                print("Game ended: DRAW")
                            else:
                                print("Winner:", g["winner"])
                            return

                        # LOCAL MODE
                        if local_mode:
                            cmd = input("local> ").strip().split()
                            if not cmd:
                                continue

                            if cmd[0] == "apply" and len(cmd) == 2:
                                pos = int(cmd[1])
                                print("(Local simulation only — does not send to server)")
                                continue

                            if cmd[0] == "setturn" and len(cmd) == 2:
                                await ws.send(json.dumps({
                                    "cmd": "set_turn",
                                    "player": cmd[1],
                                    "token": token
                                }))
                                continue

                            print("Local mode commands: apply <pos>, setturn <player>")
                            continue

                        # SERVER MODE
                        if g["turn"] == username:
                            while True:
                                pos = input("Your move (0-8): ").strip()
                                if pos in ("q", "quit", "exit"):
                                    await ws.close()
                                    return
                                try:
                                    pos = int(pos)
                                    if not (0 <= pos <= 8):
                                        raise ValueError
                                    if g["board"][pos] is not None:
                                        print("Cell occupied.")
                                        continue
                                    break
                                except:
                                    print("Enter a number 0–8.")

                            await ws.send(json.dumps({
                                "cmd": "move",
                                "position": pos,
                                "token": token
                            }))

                    elif obj.get("type") == "error":
                        print("Server error:", obj["detail"])

                    elif obj.get("type") == "pong":
                        pass

        except Exception as e:
            print("\nWS ERROR:", e)
            print(f"Reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 20)


# ------------------ MENU LOOP ------------------
def menu():
    print("""
Commands:
  register
  login
  create
  join <room>
  start <room>
  wait <room>
  play <room>
  local on/off
  exit
""")


async def main():
    global local_mode, username

    menu()

    while True:
        cmd = input("> ").strip().split()
        if not cmd:
            continue

        c = cmd[0].lower()

        if c == "register":
            register()

        elif c == "login":
            login()

        elif c == "create":
            if not token: print("Login first."); continue
            create_room()

        elif c == "join":
            if not token: print("Login first."); continue
            if len(cmd) < 2: print("Usage: join <room>"); continue
            join_room(cmd[1])

        elif c == "start":
            if not token: print("Login first."); continue
            if len(cmd) < 2: print("Usage: start <room>"); continue
            start_game(cmd[1])

        elif c == "wait":
            if len(cmd) < 2: print("Usage: wait <room>"); continue
            wait_for_game(cmd[1])

        elif c == "play":
            if len(cmd) < 2: print("Usage: play <room>"); continue
            await play_ws(cmd[1])

        elif c == "local":
            if len(cmd) < 2:
                print("Usage: local on/off")
                continue
            local_mode = (cmd[1].lower() == "on")
            print("Local mode:", local_mode)

        elif c in ("exit", "quit", "q"):
            print("Bye.")
            return

        else:
            print("Unknown command.")
            menu()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted.")
