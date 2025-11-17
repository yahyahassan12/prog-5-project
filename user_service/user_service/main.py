# main.py - User Service with JWT auth
import os
import time
from datetime import datetime, timedelta
from fastapi import FastAPI, Depends, HTTPException, status, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from passlib.context import CryptContext
from sqlalchemy.orm import Session
import jwt  # PyJWT

# local imports (you must have database.py, models.py, schemas.py as before)
from database import SessionLocal, engine, Base
from models import User
from schemas import UserCreate, UserOut, Token

# create tables
Base.metadata.create_all(bind=engine)

app = FastAPI(title="User Service (auth+jwts)")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
)

# mount static UI
app.mount("/static", StaticFiles(directory="static"), name="static")

# hashing
pwd_ctx = CryptContext(schemes=["argon2", "bcrypt"], deprecated="auto")
BCRYPT_MAX_BYTES = 72

def _truncate_for_bcrypt(password: str) -> str:
    if password is None:
        return ""
    b = password.encode("utf-8")
    if len(b) <= BCRYPT_MAX_BYTES:
        return password
    return b[:BCRYPT_MAX_BYTES].decode("utf-8", errors="ignore")

def get_password_hash(password: str) -> str:
    safe = _truncate_for_bcrypt(password)
    return pwd_ctx.hash(safe)

def verify_password(plain: str, hashed: str) -> bool:
    safe = _truncate_for_bcrypt(plain)
    return pwd_ctx.verify(safe, hashed)

# JWT config (from env or defaults)
JWT_SECRET = os.getenv("JWT_SECRET", "dev_jwt_secret_change_me")
JWT_ALGO = os.getenv("JWT_ALGO", "HS256")
JWT_EXP_MIN = int(os.getenv("JWT_EXP_MIN", "120"))  # token lifetime in minutes

def create_jwt_for_user(username: str):
    now = datetime.utcnow()
    payload = {
        "sub": username,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=JWT_EXP_MIN)).timestamp())
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)
    return token

def decode_jwt(token: str):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

# DB dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.get("/", response_class=HTMLResponse)
async def root():
    path = os.path.join("static", "login.html")
    if not os.path.exists(path):
        return HTMLResponse("<h2>login.html not found in static/</h2>", status_code=404)
    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.post("/register", response_model=UserOut)
def register(user: UserCreate, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.username == user.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists")
    user_obj = User(username=user.username, password_hash=get_password_hash(user.password))
    db.add(user_obj)
    db.commit()
    db.refresh(user_obj)
    return user_obj

@app.post("/login", response_model=Token)
def login(user: UserCreate, db: Session = Depends(get_db)):
    db_user = db.query(User).filter(User.username == user.username).first()
    if not db_user or not verify_password(user.password, db_user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    token = create_jwt_for_user(db_user.username)
    return Token(access_token=token)

@app.post("/validate-session")
def validate_session(authorization: str = Header(None)):
    """
    Accepts Authorization: Bearer <token>. Returns {username: "..."} on success.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization format")
    token = parts[1]
    payload = decode_jwt(token)
    return {"username": payload.get("sub")}
