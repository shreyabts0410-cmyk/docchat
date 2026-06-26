"""Main FastAPI server: auth, document upload/q&a/edit, history."""
import os
import re
import uuid
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from fastapi import FastAPI, APIRouter, UploadFile, File, HTTPException, Header, Cookie, Response, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr, Field
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from starlette.middleware.cors import CORSMiddleware
import io

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

from auth import (
    hash_password, verify_password, create_jwt, decode_jwt,
    fetch_emergent_session, get_current_user, new_user_id,
)
from doc_processor import extract_text, apply_edit
from storage_client import init_storage, put_object, get_object, APP_NAME
from llm_client import classify_intent, answer_question, propose_edit, summarize

mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url, serverSelectionTimeoutMS=2000)
db = client[os.environ["DB_NAME"]]
_guest_docs: dict = {}
_memory_users_by_email: dict = {}
_memory_users_by_id: dict = {}
_memory_messages: dict = {}
_memory_edits: dict = {}
_mongo_available = True
GUEST_USER = {"user_id": "guest", "email": "guest@local", "name": "Guest"}

logger = logging.getLogger("docchat")
logging.basicConfig(level=logging.INFO)


async def save_guest_doc(doc: dict) -> None:
    global _mongo_available
    if _mongo_available:
        try:
            await db.documents.insert_one(doc)
            return
        except Exception as e:
            logger.warning("MongoDB unavailable, storing guest doc in memory: %s", e)
            _mongo_available = False
    _guest_docs[doc["doc_id"]] = doc


async def find_guest_doc(doc_id: str) -> Optional[dict]:
    if _mongo_available:
        try:
            doc = await db.documents.find_one({"doc_id": doc_id}, {"_id": 0})
            if doc:
                return doc
        except Exception as e:
            logger.warning("MongoDB lookup failed, checking in-memory store: %s", e)
    return _guest_docs.get(doc_id)


async def find_user_by_email(email: str) -> Optional[dict]:
    global _mongo_available
    if _mongo_available:
        try:
            return await db.users.find_one({"email": email}, {"_id": 0})
        except Exception as e:
            logger.warning("MongoDB user lookup failed, using in-memory store: %s", e)
            _mongo_available = False
    return _memory_users_by_email.get(email)


async def find_user_by_id(user_id: str) -> Optional[dict]:
    global _mongo_available
    if _mongo_available:
        try:
            return await db.users.find_one({"user_id": user_id}, {"_id": 0})
        except Exception as e:
            logger.warning("MongoDB user lookup failed, using in-memory store: %s", e)
            _mongo_available = False
    return _memory_users_by_id.get(user_id)


async def save_user(user_doc: dict) -> None:
    global _mongo_available
    if _mongo_available:
        try:
            await db.users.insert_one(user_doc)
            return
        except Exception as e:
            logger.warning("MongoDB user insert failed, storing in memory: %s", e)
            _mongo_available = False
    _memory_users_by_email[user_doc["email"]] = user_doc
    _memory_users_by_id[user_doc["user_id"]] = user_doc


async def find_user_doc(doc_id: str, user_id: str) -> Optional[dict]:
    global _mongo_available
    if _mongo_available:
        try:
            d = await db.documents.find_one({"doc_id": doc_id, "user_id": user_id}, {"_id": 0})
            if d:
                return d
        except Exception as e:
            logger.warning("MongoDB doc lookup failed, checking in-memory store: %s", e)
            _mongo_available = False
    d = _guest_docs.get(doc_id)
    if d and d.get("user_id") == user_id:
        return d
    return None


async def list_user_docs(user_id: str) -> list:
    global _mongo_available
    if _mongo_available:
        try:
            cur = db.documents.find({"user_id": user_id}, {"_id": 0, "extracted_text": 0}).sort("created_at", -1)
            return await cur.to_list(200)
        except Exception as e:
            logger.warning("MongoDB doc list failed, using in-memory store: %s", e)
            _mongo_available = False
    docs = [d for d in _guest_docs.values() if d.get("user_id") == user_id]
    docs.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return [{k: v for k, v in d.items() if k != "extracted_text"} for d in docs]


async def update_user_doc(doc_id: str, user_id: str, updates: dict) -> None:
    global _mongo_available
    if _mongo_available:
        try:
            await db.documents.update_one({"doc_id": doc_id, "user_id": user_id}, {"$set": updates})
            return
        except Exception as e:
            logger.warning("MongoDB doc update failed, using in-memory store: %s", e)
            _mongo_available = False
    d = _guest_docs.get(doc_id)
    if d and d.get("user_id") == user_id:
        d.update(updates)


async def get_doc_messages(doc_id: str, user_id: str) -> list:
    global _mongo_available
    if _mongo_available:
        try:
            cur = db.messages.find({"doc_id": doc_id, "user_id": user_id}, {"_id": 0}).sort("created_at", 1)
            return await cur.to_list(500)
        except Exception as e:
            logger.warning("MongoDB messages lookup failed, using in-memory store: %s", e)
            _mongo_available = False
    return [m for m in _memory_messages.get(doc_id, []) if m.get("user_id") == user_id]


async def insert_message(msg: dict) -> None:
    global _mongo_available
    if _mongo_available:
        try:
            await db.messages.insert_one(msg)
            return
        except Exception as e:
            logger.warning("MongoDB message insert failed, using in-memory store: %s", e)
            _mongo_available = False
    _memory_messages.setdefault(msg["doc_id"], []).append(msg)


async def get_doc_edits(doc_id: str, user_id: str) -> list:
    global _mongo_available
    if _mongo_available:
        try:
            cur = db.edits.find({"doc_id": doc_id, "user_id": user_id}, {"_id": 0}).sort("created_at", -1)
            return await cur.to_list(200)
        except Exception as e:
            logger.warning("MongoDB edits lookup failed, using in-memory store: %s", e)
            _mongo_available = False
    edits = [e for e in _memory_edits.get(doc_id, []) if e.get("user_id") == user_id]
    edits.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    return edits


async def find_edit(edit_id: str, doc_id: str, user_id: str) -> Optional[dict]:
    global _mongo_available
    if _mongo_available:
        try:
            return await db.edits.find_one({"edit_id": edit_id, "user_id": user_id, "doc_id": doc_id}, {"_id": 0})
        except Exception as e:
            logger.warning("MongoDB edit lookup failed, using in-memory store: %s", e)
            _mongo_available = False
    for e in _memory_edits.get(doc_id, []):
        if e.get("edit_id") == edit_id and e.get("user_id") == user_id:
            return e
    return None


async def insert_edit(edit: dict) -> None:
    global _mongo_available
    if _mongo_available:
        try:
            await db.edits.insert_one(edit)
            return
        except Exception as e:
            logger.warning("MongoDB edit insert failed, using in-memory store: %s", e)
            _mongo_available = False
    _memory_edits.setdefault(edit["doc_id"], []).append(edit)


async def update_edit_record(edit_id: str, doc_id: str, user_id: str, updates: dict) -> bool:
    global _mongo_available
    if _mongo_available:
        try:
            res = await db.edits.update_one(
                {"edit_id": edit_id, "user_id": user_id, "doc_id": doc_id},
                {"$set": updates},
            )
            return res.matched_count > 0
        except Exception as e:
            logger.warning("MongoDB edit update failed, using in-memory store: %s", e)
            _mongo_available = False
    for e in _memory_edits.get(doc_id, []):
        if e.get("edit_id") == edit_id and e.get("user_id") == user_id:
            e.update(updates)
            return True
    return False

app = FastAPI()
api = APIRouter(prefix="/api")

ALLOWED_EXT = {"docx", "pdf", "pptx", "xlsx"}
MIME_MAP = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

class RegisterIn(BaseModel):
    email: EmailStr
    password: str
    name: Optional[str] = None

class LoginIn(BaseModel):
    email: EmailStr
    password: str

class SessionIn(BaseModel):
    session_id: str

class ChatIn(BaseModel):
    message: str

class ConfirmEditIn(BaseModel):
    edit_id: str

async def _user_from_request(authorization: Optional[str], session_token: Optional[str]) -> dict:
    if authorization and authorization.lower().startswith("bearer "):
        try:
            return await get_current_user(
                db,
                authorization=authorization,
                session_token_cookie=session_token,
                find_user_by_id=find_user_by_id,
            )
        except HTTPException:
            pass
    return GUEST_USER

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

@api.post("/auth/register")
async def register(payload: RegisterIn):
    existing = await find_user_by_email(payload.email)
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    user_id = new_user_id()
    user_doc = {
        "user_id": user_id,
        "email": payload.email,
        "name": payload.name or payload.email.split("@")[0],
        "password_hash": hash_password(payload.password),
        "auth_provider": "jwt",
        "created_at": _now_iso(),
    }
    await save_user(user_doc)
    token = create_jwt(user_id)
    return {"token": token, "user": {"user_id": user_id, "email": payload.email, "name": user_doc["name"]}}

@api.post("/auth/login")
async def login(payload: LoginIn):
    user = await find_user_by_email(payload.email)
    if not user or not user.get("password_hash") or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_jwt(user["user_id"])
    return {"token": token, "user": {"user_id": user["user_id"], "email": user["email"], "name": user.get("name")}}

@api.post("/auth/session")
async def emergent_session(payload: SessionIn, response: Response):
    raise HTTPException(status_code=400, detail="Google login not available in this version")

@api.get("/auth/me")
async def me(authorization: Optional[str] = Header(None), session_token: Optional[str] = Cookie(None)):
    user = await _user_from_request(authorization, session_token)
    return {"user_id": user["user_id"], "email": user["email"], "name": user.get("name")}

@api.post("/auth/logout")
async def logout(response: Response, session_token: Optional[str] = Cookie(None)):
    if session_token:
        await db.user_sessions.delete_one({"session_token": session_token})
    response.delete_cookie("session_token", path="/")
    return {"ok": True}

@api.post("/documents/upload")
async def upload_document(
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
    session_token: Optional[str] = Cookie(None),
):
    user = await _user_from_request(authorization, session_token)
    filename = file.filename or "document"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"Unsupported file type. Allowed: {', '.join(ALLOWED_EXT)}")
    data = await file.read()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (25MB max)")
    try:
        text = extract_text(data, ext)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to read document: {e}")

    doc_id = str(uuid.uuid4())
    storage_path = f"{APP_NAME}/uploads/{user['user_id']}/{doc_id}.{ext}"
    mime = MIME_MAP.get(ext, "application/octet-stream")
    put_object(storage_path, data, mime)

    try:
        summary = await summarize(doc_id, text)
    except Exception:
        summary = ""

    doc = {
        "doc_id": doc_id,
        "user_id": user["user_id"],
        "filename": filename,
        "ext": ext,
        "storage_path": storage_path,
        "size": len(data),
        "extracted_text": text,
        "summary": summary,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    await save_guest_doc(doc)
    return _doc_public(doc)

def _doc_public(d: dict) -> dict:
    return {
        "doc_id": d["doc_id"],
        "filename": d["filename"],
        "ext": d["ext"],
        "size": d["size"],
        "summary": d.get("summary", ""),
        "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"),
        "preview": (d.get("extracted_text") or "")[:5000],
    }

@api.get("/documents")
async def list_docs(authorization: Optional[str] = Header(None), session_token: Optional[str] = Cookie(None)):
    user = await _user_from_request(authorization, session_token)
    return await list_user_docs(user["user_id"])

@api.get("/documents/{doc_id}")
async def get_doc(doc_id: str, authorization: Optional[str] = Header(None), session_token: Optional[str] = Cookie(None)):
    user = await _user_from_request(authorization, session_token)
    d = await find_user_doc(doc_id, user["user_id"])
    if not d:
        raise HTTPException(status_code=404, detail="Not found")
    return _doc_public(d)

@api.get("/documents/{doc_id}/download")
async def download_doc(doc_id: str, authorization: Optional[str] = Header(None), session_token: Optional[str] = Cookie(None)):
    user = await _user_from_request(authorization, session_token)
    d = await find_user_doc(doc_id, user["user_id"])
    if not d:
        raise HTTPException(status_code=404, detail="Not found")
    data, ctype = get_object(d["storage_path"])
    headers = {"Content-Disposition": f'attachment; filename="{d["filename"]}"'}
    return Response(content=data, media_type=MIME_MAP.get(d["ext"], ctype), headers=headers)

@api.get("/documents/{doc_id}/messages")
async def get_messages(doc_id: str, authorization: Optional[str] = Header(None), session_token: Optional[str] = Cookie(None)):
    user = await _user_from_request(authorization, session_token)
    return await get_doc_messages(doc_id, user["user_id"])

@api.post("/documents/{doc_id}/messages")
async def send_message(doc_id: str, payload: ChatIn, authorization: Optional[str] = Header(None), session_token: Optional[str] = Cookie(None)):
    user = await _user_from_request(authorization, session_token)
    d = await find_user_doc(doc_id, user["user_id"])
    if not d:
        raise HTTPException(status_code=404, detail="Document not found")
    text = d.get("extracted_text") or ""

    user_msg = {
        "msg_id": str(uuid.uuid4()),
        "doc_id": doc_id,
        "user_id": user["user_id"],
        "role": "user",
        "content": payload.message,
        "created_at": _now_iso(),
    }
    await insert_message(user_msg)

    history = await get_doc_messages(doc_id, user["user_id"])

    keywords = {"edit", "change", "replace", "make", "remove", "add", "capitalize", "rewrite", "delete", "rename", "bold", "update"}
    message_words = set(re.findall(r"\w+", payload.message.lower()))
    if any(kw in message_words for kw in keywords):
        cls = {"intent": "edit", "resolved_message": payload.message}
    else:
        try:
            cls = await classify_intent(doc_id, history[:-1], payload.message, text)
        except Exception:
            cls = {"intent": "qa", "resolved_message": payload.message}

    intent = cls.get("intent", "qa")
    resolved = cls.get("resolved_message") or payload.message

    if intent == "edit":
        try:
            proposal = await propose_edit(doc_id, resolved, text)
        except Exception:
            proposal = {"old_text": "", "new_text": "", "explanation": ""}
        old_t = (proposal.get("old_text") or "").strip()
        new_t = proposal.get("new_text") or ""
        if not old_t or old_t not in text:
            ai = {
                "msg_id": str(uuid.uuid4()),
                "doc_id": doc_id,
                "user_id": user["user_id"],
                "role": "assistant",
                "content": "I couldn't pinpoint the exact text to edit. Could you quote the part of the document you want changed, or describe it more specifically?",
                "kind": "text",
                "created_at": _now_iso(),
            }
            await insert_message(ai)
            return ai

        edit_id = str(uuid.uuid4())
        edit_doc = {
            "edit_id": edit_id,
            "doc_id": doc_id,
            "user_id": user["user_id"],
            "instruction": resolved,
            "old_text": old_t,
            "new_text": new_t,
            "explanation": proposal.get("explanation", ""),
            "status": "pending",
            "created_at": _now_iso(),
        }
        await insert_edit(edit_doc)

        ai = {
            "msg_id": str(uuid.uuid4()),
            "doc_id": doc_id,
            "user_id": user["user_id"],
            "role": "assistant",
            "content": proposal.get("explanation") or "Here's the proposed edit. Review and confirm to apply.",
            "kind": "edit_proposal",
            "edit_id": edit_id,
            "old_text": old_t,
            "new_text": new_t,
            "created_at": _now_iso(),
        }
        await insert_message(ai)
        return ai

    try:
        ans = await answer_question(doc_id, history[:-1], resolved, text)
    except Exception as e:
        logger.exception("answer_question failed: %s", e)
        ans = {"answer": "Something went wrong generating an answer.", "source": ""}

    ai = {
        "msg_id": str(uuid.uuid4()),
        "doc_id": doc_id,
        "user_id": user["user_id"],
        "role": "assistant",
        "content": ans.get("answer", ""),
        "source": ans.get("source", ""),
        "kind": "text",
        "created_at": _now_iso(),
    }
    await insert_message(ai)
    return ai

@api.post("/documents/{doc_id}/edits/confirm")
async def confirm_edit(doc_id: str, payload: ConfirmEditIn, authorization: Optional[str] = Header(None), session_token: Optional[str] = Cookie(None)):
    user = await _user_from_request(authorization, session_token)
    edit = await find_edit(payload.edit_id, doc_id, user["user_id"])
    if not edit:
        raise HTTPException(status_code=404, detail="Edit not found")
    if edit["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Edit already {edit['status']}")
    d = await find_user_doc(doc_id, user["user_id"])
    if not d:
        raise HTTPException(status_code=404, detail="Document not found")

    file_bytes, _ = get_object(d["storage_path"])
    try:
        new_bytes, ok = apply_edit(file_bytes, d["ext"], edit["old_text"], edit["new_text"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Edit failed: {e}")

    if not ok:
        await update_edit_record(payload.edit_id, doc_id, user["user_id"], {"status": "failed"})
        raise HTTPException(status_code=400, detail="Could not locate text in document for replacement.")

    new_path = f"{APP_NAME}/uploads/{user['user_id']}/{doc_id}-v{int(datetime.now(timezone.utc).timestamp())}.{d['ext']}"
    mime = MIME_MAP.get(d["ext"], "application/octet-stream")
    put_object(new_path, new_bytes, mime)

    new_text = extract_text(new_bytes, d["ext"])
    await update_user_doc(doc_id, user["user_id"], {
        "storage_path": new_path,
        "extracted_text": new_text,
        "updated_at": _now_iso(),
    })
    await update_edit_record(
        payload.edit_id, doc_id, user["user_id"],
        {"status": "applied", "applied_at": _now_iso()},
    )
    return {"ok": True, "edit_id": payload.edit_id}

@api.post("/documents/{doc_id}/edits/cancel")
async def cancel_edit(doc_id: str, payload: ConfirmEditIn, authorization: Optional[str] = Header(None), session_token: Optional[str] = Cookie(None)):
    user = await _user_from_request(authorization, session_token)
    edit = await find_edit(payload.edit_id, doc_id, user["user_id"])
    if not edit or edit.get("status") != "pending":
        raise HTTPException(status_code=404, detail="Pending edit not found")
    await update_edit_record(payload.edit_id, doc_id, user["user_id"], {"status": "cancelled"})
    return {"ok": True}

@api.get("/documents/{doc_id}/edits")
async def list_edits(doc_id: str, authorization: Optional[str] = Header(None), session_token: Optional[str] = Cookie(None)):
    user = await _user_from_request(authorization, session_token)
    return await get_doc_edits(doc_id, user["user_id"])

@app.on_event("startup")
async def startup():
    global _mongo_available
    try:
        init_storage()
        logger.info("Storage initialized")
    except Exception as e:
        logger.warning(f"Storage init failed: {e}")
    try:
        await client.admin.command("ping")
        _mongo_available = True
        logger.info("MongoDB connected")
    except Exception as e:
        _mongo_available = False
        logger.warning("MongoDB unavailable, using in-memory guest doc store: %s", e)

@app.on_event("shutdown")
async def shutdown():
    client.close()

@api.get("/")
async def root():
    return {"service": "docchat", "ok": True}

# --- ROOT-LEVEL ENDPOINTS TO SUPPORT THE OLD FRONTEND ---
import json
import re

class ChatMessage(BaseModel):
    role: str
    content: str

class OldChatRequest(BaseModel):
    messages: List[ChatMessage]
    file_id: Optional[str] = None
    conversation_history: List[ChatMessage] = []

class OldEditRequest(BaseModel):
    selected_text: str
    full_context: Optional[str] = ""
    instruction: str

class LocateRequest(BaseModel):
    file_id: str
    instruction: str
    conversation_history: Optional[List[ChatMessage]] = None

class DraftRequest(BaseModel):
    file_id: str
    reference: str
    instruction: str
    conversation_history: Optional[List[ChatMessage]] = None

class ApplyRequest(BaseModel):
    file_id: str
    location: str
    text: str

def _make_content_map(text: str) -> dict:
    paragraphs = []
    for idx, p in enumerate(text.split("\n\n")):
        if p.strip():
            paragraphs.append({
                "index": idx,
                "text": p.strip(),
                "runs": []
            })
    return {"paragraphs": paragraphs}

def _flatten_content_map(content_map: dict) -> list:
    items = []
    if not content_map or "paragraphs" not in content_map:
        return items
    for p in content_map["paragraphs"]:
        items.append({
            "reference": f"Paragraph {p['index'] + 1}",
            "text": p["text"]
        })
    return items

@app.post("/upload")
async def root_upload(file: UploadFile = File(...)):
    filename = file.filename or "document"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"Unsupported file type. Allowed: {', '.join(ALLOWED_EXT)}")
    data = await file.read()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (25MB max)")
    try:
        text = extract_text(data, ext)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to read document: {e}")

    doc_id = str(uuid.uuid4())
    storage_path = f"{APP_NAME}/uploads/guest/{doc_id}.{ext}"
    mime = MIME_MAP.get(ext, "application/octet-stream")
    put_object(storage_path, data, mime)

    try:
        summary_prompt = (
            "You are an expert document analyst. Please analyze the following document. "
            "Identify 1-3 specific sentences or phrases in the text that are weak, unclear, wordy, passive, grammatically incorrect, or low-quality and would benefit from proactive improvement.\n\n"
            "Return a raw JSON object (without markdown code blocks) with EXACTLY these four keys:\n"
            '  "summary": "A concise 2-3 sentence summary.",\n'
            '  "risks": "Any major risks, gaps, or contradictions you detect.",\n'
            '  "recommendations": "A brief expert recommendation based on the content.",\n'
            '  "proactive_suggestions": [\n'
            '    {\n'
            '      "original_text": "The exact weak/unclear text from the document (must match character-for-character, including spacing, to allow search-and-replace).",\n'
            '      "suggested_replacement": "The polished, high-quality replacement text. Must NOT be wrapped in markdown code blocks or quotes.",\n'
            '      "explanation": "Why the original was weak and what was improved."\n'
            '    }\n'
            '  ]\n\n'
            f"Document text:\n{text[:40000]}"
        )
        from llm_client import client as openai_client, MODEL as openai_model
        response = await openai_client.chat.completions.create(
            model=openai_model,
            messages=[
                {"role": "user", "content": summary_prompt}
            ]
        )
        raw_content = response.choices[0].message.content.strip()
        if raw_content.startswith("```json"):
            raw_content = raw_content[7:]
        if raw_content.startswith("```"):
            raw_content = raw_content[3:]
        if raw_content.endswith("```"):
            raw_content = raw_content[:-3]
        insights = json.loads(raw_content.strip())
    except Exception as e:
        print(f"Summary generation failed: {e}")
        insights = {
            "summary": "Summary generation failed or timed out.",
            "risks": "N/A",
            "recommendations": "N/A",
            "proactive_suggestions": []
        }

    # Classify style
    detected_style = "casual"
    text_lower = text[:10000].lower()
    scores = {"legal": 0, "technical": 0, "medical": 0, "academic": 0, "financial": 0, "marketing": 0, "casual": 0}
    legal_terms = ["whereas", "hereinafter", "indemnif", "arbitrat", "jurisdiction", "clause", "statute", "liability", "contract"]
    scores["legal"] += sum(3 for t in legal_terms if t in text_lower)
    tech_terms = ["api", "algorithm", "architecture", "deploy", "infrastructure", "latency", "throughput", "database"]
    scores["technical"] += sum(3 for t in tech_terms if t in text_lower)
    detected_style = max(scores, key=scores.get)
    if scores[detected_style] == 0:
        detected_style = "casual"

    style_labels = {
        "legal": "Legal / Regulatory",
        "technical": "Technical / Engineering",
        "medical": "Medical / Clinical",
        "academic": "Academic / Research",
        "financial": "Financial / Business",
        "marketing": "Marketing / Creative",
        "casual": "General / Informal"
    }

    word_count = len(text.split())
    short_summary = {
        "file_type": ext,
        "word_count": word_count
    }

    content_map = _make_content_map(text)

    # Insert doc into db so it can be referenced in `/chat`
    doc_doc = {
        "doc_id": doc_id,
        "user_id": "guest",
        "filename": filename,
        "ext": ext,
        "storage_path": storage_path,
        "size": len(data),
        "extracted_text": text,
        "summary": insights.get("summary", ""),
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    await save_guest_doc(doc_doc)

    return {
        "message": f"Successfully uploaded {filename}",
        "file_id": doc_id,
        "short_summary": short_summary,
        "insights": insights,
        "filename": filename,
        "url": f"http://localhost:8000/files/{doc_id}.{ext}",
        "content": text,
        "content_map": content_map,
        "document_style": detected_style,
        "document_style_label": style_labels.get(detected_style, "General / Informal")
    }

@app.get("/files/{doc_id}.{ext}")
async def get_uploaded_file(doc_id: str, ext: str):
    storage_path = f"{APP_NAME}/uploads/guest/{doc_id}.{ext}"
    try:
        data, ctype = get_object(storage_path)
    except Exception:
        raise HTTPException(status_code=404, detail="File not found")
    headers = {"Content-Disposition": f'inline; filename="{doc_id}.{ext}"'}
    return Response(content=data, media_type=MIME_MAP.get(ext, ctype), headers=headers)

@app.post("/submit-text")
async def old_submit_text(payload: dict):
    text = payload.get("text", "").strip()
    title = payload.get("title", "").strip() or "Pasted Text"
    if not text:
        raise HTTPException(status_code=400, detail="Text content is empty.")
    
    doc_id = str(uuid.uuid4())
    storage_path = f"{APP_NAME}/uploads/guest/{doc_id}.txt"
    put_object(storage_path, text.encode("utf-8"), "text/plain")

    try:
        summary_prompt = (
            "You are an expert document analyst. Please analyze the following document. "
            "Identify 1-3 specific sentences or phrases in the text that are weak, unclear, wordy, passive, grammatically incorrect, or low-quality and would benefit from proactive improvement.\n\n"
            "Return a raw JSON object (without markdown code blocks) with EXACTLY these four keys:\n"
            '  "summary": "A concise 2-3 sentence summary.",\n'
            '  "risks": "Any major risks, gaps, or contradictions you detect.",\n'
            '  "recommendations": "A brief expert recommendation based on the content.",\n'
            '  "proactive_suggestions": [\n'
            '    {\n'
            '      "original_text": "The exact weak/unclear text from the document (must match character-for-character, including spacing, to allow search-and-replace).",\n'
            '      "suggested_replacement": "The polished, high-quality replacement text. Must NOT be wrapped in markdown code blocks or quotes.",\n'
            '      "explanation": "Why the original was weak and what was improved."\n'
            '    }\n'
            '  ]\n\n'
            f"Document text:\n{text[:40000]}"
        )
        from llm_client import client as openai_client, MODEL as openai_model
        response = await openai_client.chat.completions.create(
            model=openai_model,
            messages=[
                {"role": "user", "content": summary_prompt}
            ]
        )
        raw_content = response.choices[0].message.content.strip()
        if raw_content.startswith("```json"):
            raw_content = raw_content[7:]
        if raw_content.startswith("```"):
            raw_content = raw_content[3:]
        if raw_content.endswith("```"):
            raw_content = raw_content[:-3]
        insights = json.loads(raw_content.strip())
    except Exception as e:
        print(f"Summary generation failed: {e}")
        insights = {
            "summary": "Summary generation failed or timed out.",
            "risks": "N/A",
            "recommendations": "N/A",
            "proactive_suggestions": []
        }

    # Classify style
    detected_style = "casual"
    text_lower = text[:10000].lower()
    scores = {"legal": 0, "technical": 0, "medical": 0, "academic": 0, "financial": 0, "marketing": 0, "casual": 0}
    legal_terms = ["whereas", "hereinafter", "indemnif", "arbitrat", "jurisdiction", "clause", "statute", "liability", "contract"]
    scores["legal"] += sum(3 for t in legal_terms if t in text_lower)
    tech_terms = ["api", "algorithm", "architecture", "deploy", "infrastructure", "latency", "throughput", "database"]
    scores["technical"] += sum(3 for t in tech_terms if t in text_lower)
    detected_style = max(scores, key=scores.get)
    if scores[detected_style] == 0:
        detected_style = "casual"

    style_labels = {
        "legal": "Legal / Regulatory",
        "technical": "Technical / Engineering",
        "medical": "Medical / Clinical",
        "academic": "Academic / Research",
        "financial": "Financial / Business",
        "marketing": "Marketing / Creative",
        "casual": "General / Informal"
    }

    content_map = _make_content_map(text)

    # Insert doc into db so it can be referenced in `/chat`
    doc_doc = {
        "doc_id": doc_id,
        "user_id": "guest",
        "filename": title,
        "ext": "txt",
        "storage_path": storage_path,
        "size": len(text.encode("utf-8")),
        "extracted_text": text,
        "summary": insights.get("summary", ""),
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    await save_guest_doc(doc_doc)

    return {
        "message": f"Successfully processed: {title}",
        "file_id": doc_id,
        "title": title,
        "content": text,
        "content_map": content_map,
        "insights": insights,
        "document_style": detected_style,
        "document_style_label": style_labels.get(detected_style, "General / Informal")
    }

@app.post("/edit-section")
async def old_edit_section(request: OldEditRequest):
    if not request.selected_text.strip():
        raise HTTPException(status_code=400, detail="No text selected for editing.")
    
    context_style = "casual"
    text_lower = (request.full_context or "").lower()
    scores = {"legal": 0, "technical": 0, "medical": 0, "academic": 0, "financial": 0, "marketing": 0, "casual": 0}
    legal_terms = ["whereas", "hereinafter", "indemnif", "arbitrat", "jurisdiction", "clause", "statute", "liability", "contract"]
    scores["legal"] += sum(3 for t in legal_terms if t in text_lower)
    tech_terms = ["api", "algorithm", "architecture", "deploy", "infrastructure", "latency", "throughput", "database"]
    scores["technical"] += sum(3 for t in tech_terms if t in text_lower)
    context_style = max(scores, key=scores.get)
    if scores[context_style] == 0:
        context_style = "casual"

    style_labels = {
        "legal": "Legal / Regulatory",
        "technical": "Technical / Engineering",
        "medical": "Medical / Clinical",
        "academic": "Academic / Research",
        "financial": "Financial / Business",
        "marketing": "Marketing / Creative",
        "casual": "General / Informal"
    }
    
    style_label = style_labels.get(context_style, "General / Informal")
    
    edit_prompt = (
        f"You are an elite professional editor specializing in {style_label} documents.\n\n"
    )
    if request.full_context:
        edit_prompt += f"SURROUNDING DOCUMENT CONTEXT (for style matching):\n{request.full_context[:5000]}\n\n"
    
    edit_prompt += (
        f"SELECTED TEXT TO EDIT:\n\"{request.selected_text}\"\n\n"
        f"INSTRUCTION FROM USER:\n\"{request.instruction}\"\n\n"
        "Analyze the user's instruction. If the instruction is unclear, ambiguous, too vague to perform the edit, or contains contradictory commands, set \"needs_clarification\" to true and write a clarifying question under \"clarification_question\" asking the user to explain their intent.\n"
        "Otherwise, set \"needs_clarification\" to false and provide the edited text.\n\n"
        "Return a raw JSON object (without markdown code blocks) with EXACTLY these four keys:\n"
        '  "edited_text": "The final polished replacement text. Must be a direct drop-in replacement. Set to empty string if needs_clarification is true.",\n'
        '  "explanation": "A brief 1-sentence explanation of what was improved (or why clarification is needed).",\n'
        '  "needs_clarification": true/false,\n'
        '  "clarification_question": "If needs_clarification is true, specify a clarifying question asking the user for details. Otherwise, set this to empty string."\n\n'
        "Rules for the edited text:\n"
        "- Return the final polished replacement text ONLY. Do NOT include any introduction, conversational preamble, explanation, or notes in the edited_text value.\n"
        "- Do NOT wrap the edited_text value in markdown code blocks (like ```) or quotes. The text must be returned as plain text inside the JSON string, completely clean and ready to paste directly into the document.\n"
        "- Preserve the original meaning and intent unless the user explicitly instructs otherwise.\n"
        "- Match the tone and style of the surrounding document.\n"
        "- Fix grammar, strengthen phrasing, improve clarity.\n"
        "- Return ONLY the replacement text for the selected section, not the entire document.\n"
    )
    
    from llm_client import client as openai_client, MODEL as openai_model
    response = await openai_client.chat.completions.create(
        model=openai_model,
        messages=[
            {"role": "user", "content": edit_prompt}
        ]
    )
    
    raw_content = response.choices[0].message.content.strip()
    if raw_content.startswith("```json"):
        raw_content = raw_content[7:]
    if raw_content.startswith("```"):
        raw_content = raw_content[3:]
    if raw_content.endswith("```"):
        raw_content = raw_content[:-3]
    
    return json.loads(raw_content.strip())

@app.post("/edit/locate")
async def old_edit_locate(request: LocateRequest):
    system_prompt = (
        "You identify exactly which part of a document an edit instruction refers to, given a structured map of paragraphs/slides/cells/pages.\n"
        "1. Return the single best-matching location with its exact reference.\n"
        "2. If multiple locations plausibly match, don't guess — say it's ambiguous and list candidates with ~10 words of context each.\n"
        "3. If nothing matches, say so rather than inventing a location.\n"
        "4. Don't draft replacement text here — only locate.\n\n"
        "You MUST return your response as a raw JSON object (do not wrap in markdown code blocks or any other text) with the following structure:\n"
        "{\n"
        "  \"matched_locations\": [\n"
        "    {\n"
        "      \"reference\": \"The exact reference string of the item (e.g. 'Paragraph 5')\",\n"
        "      \"text\": \"The exact original text of that item\"\n"
        "    }\n"
        "  ],\n"
        "  \"needs_clarification\": true/false,\n"
        "  \"clarification_question\": \"Your clarifying question here if needs_clarification is true, listing candidates with ~10 words of context each, otherwise empty string.\"\n"
        "}"
    )
    doc = await find_guest_doc(request.file_id)
    if not doc:
        raise HTTPException(status_code=404, detail="File not found")
    text = doc.get("extracted_text", "")
    content_map = _make_content_map(text)
    items = _flatten_content_map(content_map)
    
    items_str_parts = []
    for item in items:
        items_str_parts.append(
            f"--- ITEM REFERENCE: {item['reference']} ---\n"
            f"{item['text']}\n"
            f"--- END ITEM REFERENCE: {item['reference']} ---"
        )
    items_str = "\n\n".join(items_str_parts)

    human_content = (
        f"Here is the list of document items with their reference names and original text:\n\n"
        f"{items_str[:30000]}\n\n"
        f"USER EDIT INSTRUCTION:\n\"{request.instruction}\""
    )
    
    from llm_client import client as openai_client, MODEL as openai_model
    response = await openai_client.chat.completions.create(
        model=openai_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": human_content}
        ]
    )
    raw_content = response.choices[0].message.content.strip()
    if raw_content.startswith("```json"):
        raw_content = raw_content[7:]
    if raw_content.startswith("```"):
        raw_content = raw_content[3:]
    if raw_content.endswith("```"):
        raw_content = raw_content[:-3]
        
    try:
        result = json.loads(raw_content.strip())
        return {
            "matched_locations": result.get("matched_locations", []),
            "needs_clarification": bool(result.get("needs_clarification", False)),
            "clarification_question": str(result.get("clarification_question", ""))
        }
    except Exception:
        return {
            "matched_locations": [],
            "needs_clarification": True,
            "clarification_question": f"Could not parse locate results: {raw_content}"
        }

@app.post("/edit/draft")
async def old_edit_draft(request: DraftRequest):
    doc = await find_guest_doc(request.file_id)
    if not doc:
        raise HTTPException(status_code=404, detail="File not found")
    text = doc.get("extracted_text", "")
    content_map = _make_content_map(text)
    items = _flatten_content_map(content_map)
    
    target_item = None
    for item in items:
        if item["reference"] == request.reference:
            target_item = item
            break
    if not target_item:
        raise HTTPException(status_code=404, detail=f"Target reference '{request.reference}' not found")
        
    system_prompt = (
        "You draft a replacement for one specific, already-located piece of a document, per a user's edit instruction.\n"
        "1. Match the tone, formality, and rhythm of the surrounding document — write as the original author would, not as generic new content.\n"
        "2. Keep length close to the original unless told to expand/shorten significantly.\n"
        "3. Change only what was asked — don't \"improve\" unrelated wording or fix unrelated issues.\n"
        "4. Output ONLY the replacement text — no preamble, no quotes around it, nothing else.\n"
        "5. If the instruction is too vague to draft confidently, ask one clarifying question instead of guessing.\n\n"
        "Special Output Rule:\n"
        "If you need to ask a clarifying question, prefix it with 'CLARIFICATION_REQUIRED: '. Otherwise, output only the raw replacement text."
    )
    
    human_content = (
        f"DOCUMENT SURROUNDING CONTEXT:\n{text[:10000]}\n\n"
        f"TARGET SECTION REFERENCE: {request.reference}\n"
        f"ORIGINAL TEXT OF SECTION:\n\"{target_item['text']}\"\n\n"
        f"EDIT INSTRUCTION:\n\"{request.instruction}\""
    )
    
    from llm_client import client as openai_client, MODEL as openai_model
    response = await openai_client.chat.completions.create(
        model=openai_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": human_content}
        ]
    )
    content = response.choices[0].message.content.strip()
    if content.startswith("CLARIFICATION_REQUIRED:"):
        clar_q = content.replace("CLARIFICATION_REQUIRED:", "").strip()
        return {
            "reference": request.reference,
            "original_text": target_item["text"],
            "draft_text": "",
            "needs_clarification": True,
            "clarification_question": clar_q
        }
    else:
        return {
            "reference": request.reference,
            "original_text": target_item["text"],
            "draft_text": content,
            "needs_clarification": False,
            "clarification_question": ""
        }

@app.post("/edit/apply")
async def old_edit_apply(request: ApplyRequest):
    d = await find_guest_doc(request.file_id)
    if not d:
        raise HTTPException(status_code=404, detail="Document not found")
        
    file_bytes, _ = get_object(d["storage_path"])
    
    match = re.match(r"Paragraph\s+(\d+)", request.location)
    if match:
        para_idx = int(match.group(1)) - 1
        content_map = _make_content_map(d["extracted_text"])
        items = _flatten_content_map(content_map)
        if 0 <= para_idx < len(items):
            old_text = items[para_idx]["text"]
        else:
            raise HTTPException(status_code=400, detail="Paragraph out of bounds")
    else:
        old_text = request.location
        
    try:
        new_bytes, ok = apply_edit(file_bytes, d["ext"], old_text, request.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Edit failed: {e}")
        
    if not ok:
        raise HTTPException(status_code=400, detail="Could not locate text in document for replacement.")
        
    new_file_id = str(uuid.uuid4())
    new_filename = f"{d['filename'].rsplit('.', 1)[0]}_v_{int(datetime.now(timezone.utc).timestamp())}.{d['ext']}"
    new_path = f"{APP_NAME}/uploads/guest/{new_file_id}.{d['ext']}"
    mime = MIME_MAP.get(d["ext"], "application/octet-stream")
    put_object(new_path, new_bytes, mime)
    
    new_text = extract_text(new_bytes, d["ext"])
    new_content_map = _make_content_map(new_text)
    
    detected_style = "casual"
    text_lower = new_text[:10000].lower()
    scores = {"legal": 0, "technical": 0, "medical": 0, "academic": 0, "financial": 0, "marketing": 0, "casual": 0}
    legal_terms = ["whereas", "hereinafter", "indemnif", "arbitrat", "jurisdiction", "clause", "statute", "liability", "contract"]
    scores["legal"] += sum(3 for t in legal_terms if t in text_lower)
    tech_terms = ["api", "algorithm", "architecture", "deploy", "infrastructure", "latency", "throughput", "database"]
    scores["technical"] += sum(3 for t in tech_terms if t in text_lower)
    detected_style = max(scores, key=scores.get)
    if scores[detected_style] == 0:
        detected_style = "casual"

    style_labels = {
        "legal": "Legal / Regulatory",
        "technical": "Technical / Engineering",
        "medical": "Medical / Clinical",
        "academic": "Academic / Research",
        "financial": "Financial / Business",
        "marketing": "Marketing / Creative",
        "casual": "General / Informal"
    }
    
    new_doc_doc = {
        "doc_id": new_file_id,
        "user_id": "guest",
        "filename": new_filename,
        "ext": d["ext"],
        "storage_path": new_path,
        "size": len(new_bytes),
        "extracted_text": new_text,
        "summary": d.get("summary", ""),
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    await save_guest_doc(new_doc_doc)
    
    download_link = f"http://localhost:8000/files/{new_file_id}.{d['ext']}"
    
    return {
        "message": "Successfully applied edit",
        "file_id": new_file_id,
        "download_link": download_link,
        "filename": new_filename,
        "short_summary": {
            "file_type": d["ext"],
            "word_count": len(new_text.split())
        },
        "insights": d.get("insights", {
            "summary": d.get("summary", ""),
            "risks": "N/A",
            "recommendations": "N/A",
            "proactive_suggestions": []
        }),
        "url": download_link,
        "content": new_text,
        "content_map": new_content_map,
        "document_style": detected_style,
        "document_style_label": style_labels.get(detected_style, "General / Informal")
    }

@app.post("/chat")
async def old_chat(request: OldChatRequest):
    doc = await find_guest_doc(request.file_id) if request.file_id else None
    doc_text = doc.get("extracted_text", "") if doc else ""

    history = []
    for m in request.conversation_history:
        history.append({"role": m.role, "content": m.content})
    for m in request.messages[:-1]:
        history.append({"role": m.role, "content": m.content})

    query = request.messages[-1].content if request.messages else ""

    try:
        from llm_client import answer_question
        ans = await answer_question(request.file_id or "guest", history, query, doc_text)
    except Exception as e:
        ans = {"answer": f"Error: {e}", "source": ""}

    return {
        "answer": ans.get("answer", ""),
        "sources": [ans.get("source")] if ans.get("source") else []
    }

app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
