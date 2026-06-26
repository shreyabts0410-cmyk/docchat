"""Auth helpers: JWT email/password."""
import os
import uuid
import jwt
import bcrypt
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from typing import Optional, Callable, Awaitable

JWT_SECRET = os.environ.get("JWT_SECRET", "change-me")
JWT_ALGO = "HS256"
JWT_TTL_DAYS = 7


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:
        return False


def create_jwt(user_id: str) -> str:
    payload = {
        "user_id": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_TTL_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def decode_jwt(token: str) -> Optional[str]:
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        return data.get("user_id")
    except Exception:
        return None


async def fetch_emergent_session(session_id: str) -> dict:
    raise HTTPException(status_code=400, detail="Google login not available")


async def get_current_user(
    db,
    authorization: Optional[str] = None,
    session_token_cookie: Optional[str] = None,
    find_user_by_id: Optional[Callable[[str], Awaitable[Optional[dict]]]] = None,
) -> dict:
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(None, 1)[1].strip()
        user_id = decode_jwt(token)
        if user_id:
            user = None
            if find_user_by_id:
                user = await find_user_by_id(user_id)
            else:
                user = await db.users.find_one({"user_id": user_id}, {"_id": 0})
            if user:
                return user
    raise HTTPException(status_code=401, detail="Not authenticated")


def new_user_id() -> str:
    return f"user_{uuid.uuid4().hex[:12]}"
