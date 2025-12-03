# Tic Tac Toe 


This project implements a distributed Tic Tac Toe game using **three microservices**, a **CLI client**, and a **web UI**.

---

## Architecture Overview

             ┌──────────────────┐
             │     Web Client    │
             │ (HTML/JS browser) │
             └─────────┬────────┘
                       │  HTTP + WebSocket
                ┌──────▼──────────────┐
                │    User Service     │
                │  (Auth, Static UI)  │
                └───┬───────────────┬─┘
                    │               │
                    │HTTP           │HTTP
         ┌──────────▼───┐       ┌──▼─────────────────┐
         │  Room Service │       │  Game Rules Service │
         │ (rooms, join) │       │ (logic + WS)        │
         └──────┬────────┘       └─────────┬──────────┘
                │                          │
                │                          │WS
          ┌─────▼──────────┐          ┌────▼───────────────────┐
          │      CLI       │          │     Web Game Client     │
          │  (terminal)    │          │  (game.html WebSocket)  │
          └────────────────┘          └─────────────────────────┘


### 1. User Service (Port 8001)
Handles:
- Registration
- Login
- Password hashing
- JWT creation & validation
- Serves static web UI (`/static/*.html`)

### 2. Room Service (Port 8002)
Handles:
- Creating rooms
- Joining rooms
- Leaving rooms
- Marking rooms as full
- Notifying Game Rules Service when a room is full or game starts

### 3. Game Rules Service (Port 8003)
Handles:
- Game creation
- Game persistence
- Turn logic
- Move validation
- WebSocket-based real-time updates

### 4. CLI Client
Terminal application for:
- Register/Login
- Create/Join/Start rooms
- Play game via WebSocket
- Local simulation mode

### 5. Web Client (HTML UI)
Pages:
- login.html
- lobby.html
- game.html
- game_client.html

Served by User Service at /static/.

---

## Project File Structure

```
.
├── cli_client.py
├── README.md
├── gamepage.html
├── game_rules_service
│   └── main.py
├── room_service
│   └── main.py
└── user_service
    ├── database.py
    ├── main.py
    ├── models.py
    ├── requirements.txt
    ├── schemas.py
    ├── static
    │   ├── game.html
    │   ├── game_client.html
    │   ├── lobby.html
    │   └── login.html
    └── web-client
        ├── package.json
        ├── index.html
        └── node_modules/...
```

---

## How to Run

### 1. Install dependencies
```
pip install fastapi uvicorn sqlalchemy requests passlib[bcrypt] pyjwt websockets
```

### 2. Start Each Service

**User Service**
```
cd user_service
uvicorn main:app --port 8001 --reload
```

**Room Service**
```
cd room_service
uvicorn main:app --port 8002 --reload
```

**Game Rules Service**
```
cd game_rules_service
uvicorn main:app --port 8003 --reload
```

---


## WebSocket Protocol

### Client → Server
| Command | Payload Example | Description |
|--------|------------------|-------------|
| move | `{"cmd":"move","position":3,"token":"<jwt>"}` | Make a move |
| ping | `{"cmd":"ping"}` | Keepalive |
| set_turn | `{"cmd":"set_turn","player":"bob","token":"<jwt>"}` | Host/local turn select |

### Server → Client
| Type | Example | Description |
|------|----------|-------------|
| game_update | `{...}` | Full game state |
| error | `{"detail":"not your turn"}` | Error |
| pong | `{"type":"pong"}` | Ping reply |

## REST API Summary

### User Service
- POST /register
- POST /login
- POST /validate-session
- GET /static/*.html

### Room Service
- GET /rooms
- POST /create-room
- POST /join-room/{id}
- POST /leave-room/{id}
- POST /start-game/{id}
- GET /room/{id}

### Game Rules Service
- POST /games
- GET /games/{id}
- WS /ws/games/{id}

--------

### User Service
- POST /register
- POST /login
- POST /validate-session
- GET /static/*.html

### Room Service
- GET /rooms
- POST /create-room
- POST /join-room/{id}
- POST /leave-room/{id}
- POST /start-game/{id}
- GET /room/{id}

### Game Rules Service
- POST /games
- GET /games/{id}
- WS /ws/games/{id}

## Environment Variables


----
## CLI Usage

```
python cli_client.py
```

Commands:
- register
- login
- create
- join <room>
- start <room>
- wait <room>
- play <room>
- local on/off
- exit

---

## Web UI

Visit:
```
http://127.0.0.1:8001/static/login.html
```

From there:
- Login/Register
- Go to lobby
- Create/join rooms
- Game auto-starts when ready

---

## Game Flow Summary

1. User logs in → receives JWT  
2. User creates or joins a room in Room Service  
3. When room is full → Room Service notifies Game Rules Service  
4. Game Rules Service creates game state  
5. Players connect to WebSocket:
   ```
   ws://127.0.0.1:8003/ws/games/<GAME_ID>?token=<jwt>
   ```
6. Moves validated & broadcast  
7. Winner detection & game end state  

---

## Notes

- Passwords are hashed securely.
- JWTs validated for every gameplay command.
- Room Service is in-memory for simplicity.
- WebSockets handle real-time updates.

---

