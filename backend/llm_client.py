"""LLM helpers for Q&A and edit instruction parsing using OpenAI SDK."""
import asyncio
from typing import Optional
import json
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI, RateLimitError, APIError

load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger("docchat.llm")

client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"), max_retries=0)
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
DEFAULT_GEMINI_MODELS = ("gemini-2.5-flash", "gemini-1.5-flash", "gemini-1.5-pro", "gemini-pro")
GEMINI_MODEL_ALIASES = {
    "gemini-1.5-flash": ("gemini-2.5-flash", "gemini-flash-latest"),
    "gemini-1.5-pro": ("gemini-2.5-pro", "gemini-pro-latest"),
    "gemini-pro": ("gemini-pro-latest",),
}


def _gemini_model_attempts() -> list[str]:
    attempts: list[str] = []
    seen: set[str] = set()
    for name in _gemini_models():
        for candidate in (name, *GEMINI_MODEL_ALIASES.get(name, ())):
            if candidate not in seen:
                seen.add(candidate)
                attempts.append(candidate)
    return attempts


def _gemini_models() -> list[str]:
    preferred = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
    if preferred == "gemini-1.5-flash":
        preferred = "gemini-2.5-flash"
    ordered: list[str] = []
    seen: set[str] = set()
    for name in (preferred, *DEFAULT_GEMINI_MODELS):
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _gemini_api_key() -> Optional[str]:
    key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not key or key in ("dummy-key-for-startup", "your_actual_google_api_key_here"):
        return None
    return key


async def _openai_json(system: str, user: str, max_tokens: Optional[int] = None) -> dict:
    kwargs = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    response = await client.chat.completions.create(**kwargs)
    return _safe_json(response.choices[0].message.content, default={})


async def _gemini_json(system: str, user: str, max_tokens: Optional[int] = None) -> dict:
    import google.generativeai as genai
    from google.api_core import exceptions as google_exceptions

    genai.configure(api_key=_gemini_api_key())

    last_error: Optional[Exception] = None
    for model_name in _gemini_model_attempts():
        try:
            def _call(model=model_name):
                model_obj = genai.GenerativeModel(model, system_instruction=system)
                gen_config = {"response_mime_type": "application/json"}
                if max_tokens is not None:
                    gen_config["max_output_tokens"] = max_tokens
                response = model_obj.generate_content(
                    user,
                    generation_config=gen_config,
                )
                return response.text

            text = await asyncio.wait_for(asyncio.to_thread(_call), timeout=30.0)
            result = _safe_json(text, default={})
            if result:
                logger.info("Answer generated via Gemini fallback (%s)", model_name)
                return result
        except (asyncio.TimeoutError, TimeoutError) as e:
            logger.warning("Gemini model %s timed out, trying next.", model_name)
            last_error = e
            continue
        except Exception as e:
            if _is_gemini_quota_error(e):
                logger.warning("Gemini model %s quota error, trying next", model_name)
                last_error = e
            else:
                logger.warning("Gemini model %s failed (%s), trying next", model_name, e)
                last_error = e

    if last_error:
        raise last_error
    return {}


def _is_gemini_quota_error(exc: Exception) -> bool:
    from google.api_core import exceptions as google_exceptions
    if isinstance(exc, google_exceptions.ResourceExhausted):
        return True
    msg = str(exc).lower()
    return "quota" in msg or "resource_exhausted" in msg or "rate limit" in msg


async def _llm_json(system: str, user: str, *, fallback: dict, max_tokens: Optional[int] = None) -> dict:
    try:
        result = await _openai_json(system, user, max_tokens=max_tokens)
        if result:
            return result
    except (RateLimitError, APIError) as e:
        logger.warning("OpenAI request failed (%s), trying Gemini", e)
    except Exception:
        logger.exception("OpenAI request failed unexpectedly")

    gemini_key = _gemini_api_key()
    if gemini_key:
        try:
            result = await _gemini_json(system, user, max_tokens=max_tokens)
            if result:
                return result
        except Exception:
            logger.exception("Gemini fallback failed")

    return fallback


def _local_qa_fallback(question: str, doc_text: str) -> dict:
    stop = {"what", "when", "where", "which", "that", "this", "with", "from", "about",
            "does", "have", "your", "tell", "explain", "the", "and", "for", "are", "was"}
    q_words = [w.lower() for w in re.findall(r"\w+", question) if len(w) > 2 and w.lower() not in stop]
    sentences = re.split(r"(?<=[.!?])\s+|\n+", doc_text)
    scored = []
    for sentence in sentences:
        s = sentence.strip()
        if len(s) < 20:
            continue
        score = sum(1 for w in q_words if w in s.lower())
        if score:
            scored.append((score, s))
    if scored:
        scored.sort(key=lambda x: -x[0])
        # Return top 3 sentences joined for a better fallback answer
        best = " ".join(s for _, s in scored[:3])
        return {"answer": best, "source": ""}
    excerpt = doc_text.strip()[:800]
    if excerpt:
        return {"answer": excerpt, "source": ""}
    return {"answer": "I couldn't find an answer in the document.", "source": ""}


def chunk_and_select(doc_text: str, question: str, top_k: int = 8) -> str:
    """
    Improved chunk selection:
    - Splits into paragraphs AND sentences for better granularity
    - Scores by keyword overlap + partial word matching (handles plurals, verb forms)
    - Boosts chunks that contain multiple question keywords close together
    - Returns top_k chunks in document order
    """
    stop = {
        "what", "when", "where", "which", "that", "this", "with", "from", "about",
        "does", "have", "your", "tell", "explain", "the", "and", "for", "are", "was",
        "how", "why", "who", "can", "will", "did", "its", "any", "all", "more"
    }

    # Extract question keywords and their stems (first 5 chars)
    q_words = [w.lower() for w in re.findall(r"\w+", question) if len(w) > 2 and w.lower() not in stop]
    q_stems = [w[:5] for w in q_words]

    # Try paragraph split first, fall back to line split
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', doc_text) if p.strip()]
    if len(paragraphs) < 4:
        paragraphs = [p.strip() for p in re.split(r'\n+', doc_text) if p.strip()]

    # If still very few chunks, split long paragraphs into sentences
    chunks = []
    for p in paragraphs:
        if len(p) > 600:
            sentences = re.split(r'(?<=[.!?])\s+', p)
            # Group sentences into pairs to keep context
            for i in range(0, len(sentences), 2):
                group = " ".join(sentences[i:i+2]).strip()
                if group:
                    chunks.append(group)
        else:
            chunks.append(p)

    scored = []
    for idx, chunk in enumerate(chunks):
        chunk_lower = chunk.lower()
        chunk_words = set(re.findall(r"\w+", chunk_lower))

        # Exact keyword match score
        exact_score = sum(1 for w in q_words if w in chunk_words)

        # Partial/stem match score (handles plurals, conjugations)
        stem_score = sum(0.5 for stem in q_stems if any(w.startswith(stem) for w in chunk_words))

        # Proximity bonus: if 3+ keywords appear in the same chunk
        keyword_hits = sum(1 for w in q_words if w in chunk_lower)
        proximity_bonus = 1.5 if keyword_hits >= 3 else (0.5 if keyword_hits == 2 else 0)

        total_score = exact_score + stem_score + proximity_bonus
        scored.append((total_score, idx, chunk))

    # Sort by score descending
    scored.sort(key=lambda x: (-x[0], x[1]))

    # Take top_k most relevant
    selected = scored[:top_k]

    # Restore document order
    selected.sort(key=lambda x: x[1])

    result = "\n\n".join(chunk for _, _, chunk in selected)

    # Safety: if result is very short (poor keyword match), return more of the doc
    if len(result) < 300:
        return doc_text[:6000]

    return result


async def classify_intent(session_id: str, history: list, user_msg: str, doc_excerpt: str) -> dict:
    hist_text = "\n".join(
        f"{m['role']}: {m['content'][:500]}" for m in history[-8:]
    )
    system = (
        "You classify user messages about a document into either 'qa' (question/answer) or "
        "'edit' (a request to modify the document). Also resolve pronouns like 'it', 'this', "
        "'that line' using the conversation context. "
        "Reply ONLY with strict JSON: "
        "{\"intent\": \"qa\"|\"edit\", \"resolved_message\": \"<fully resolved instruction>\"}"
    )
    user = (
        f"Conversation so far:\n{hist_text}\n\n"
        f"Document excerpt (first 2000 chars):\n{doc_excerpt[:2000]}\n\n"
        f"New user message: {user_msg}\n\n"
        "Classify and resolve pronouns. JSON only."
    )
    return await _llm_json(
        system,
        user,
        fallback={"intent": "qa", "resolved_message": user_msg},
    )


async def answer_question(session_id: str, history: list, question: str, doc_text: str) -> dict:
    hist_text = "\n".join(
        f"{m['role']}: {m['content'][:600]}" for m in history[-8:]
    )

    # Select the most relevant chunks from the document
    relevant_doc_text = chunk_and_select(doc_text, question, top_k=8)

    system = (
        "You are a precise document assistant. Answer the user's question using ONLY the document content provided.\n"
        "Rules:\n"
        "1. Be thorough and detailed — give a complete answer, not a one-liner.\n"
        "2. If the question asks for a list, return a proper list.\n"
        "3. If the question asks for a number, date, name, or fact — state it directly and clearly.\n"
        "4. Use the conversation history to understand follow-up questions and pronouns.\n"
        "5. If the answer genuinely cannot be found in the document, say: 'This information is not in the document.'\n"
        "6. Do NOT add inline citations like [Page 1] or [Block 2] inside the answer.\n"
        "7. Write in clean, readable prose or bullet points depending on what fits best.\n"
        "Reply ONLY with strict JSON: "
        "{\"answer\": \"<full detailed answer>\", \"source\": \"<very short reference, or empty>\"}"
    )

    user = (
        f"Conversation history:\n{hist_text}\n\n"
        f"Relevant document content:\n{relevant_doc_text}\n\n"
        f"Question: {question}\n\n"
        "Answer thoroughly and accurately. JSON only."
    )

    fallback = _local_qa_fallback(question, doc_text)
    result = await _llm_json(system, user, fallback=fallback, max_tokens=1200)
    if result.get("answer"):
        return result
    return fallback


async def propose_edit(session_id: str, instruction: str, doc_text: str) -> dict:
    system = (
        "You are a precise document editor. Given an edit instruction and the full document:\n"
        "1. Find the EXACT text that needs to change — copy it character-for-character from the document.\n"
        "2. Write the replacement text in new_text.\n"
        "3. Keep old_text as short as possible while uniquely identifying the region to change.\n"
        "4. Preserve the document's tone, style, and formatting in the replacement.\n"
        "5. Output ONLY the replacement text in new_text — no quotes, no preamble.\n"
        "Reply ONLY with strict JSON: "
        "{\"old_text\": \"<verbatim from document>\", \"new_text\": \"<replacement>\", \"explanation\": \"<one short sentence>\"}"
    )
    user = f"Edit instruction: {instruction}\n\nDocument:\n{doc_text[:60000]}\n\nJSON only."
    return await _llm_json(
        system,
        user,
        fallback={"old_text": "", "new_text": "", "explanation": ""},
    )


async def summarize(session_id: str, doc_text: str) -> str:
    system = (
        "Summarize the document in 2-3 clear sentences. "
        "Mention what type of document it is, its main topic, and any key details. "
        "Plain prose only — no bullets, no citations."
    )
    try:
        response = await client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"Document:\n{doc_text[:30000]}\n\nSummary:"},
            ],
            max_tokens=200,
        )
        return response.choices[0].message.content.strip()
    except (RateLimitError, APIError) as e:
        logger.warning("OpenAI summarize failed (%s), trying Gemini", e)
    except Exception:
        logger.exception("OpenAI summarize failed unexpectedly")

    # Gemini fallback for summarize
    gemini_key = _gemini_api_key()
    if gemini_key:
        try:
            import google.generativeai as genai
            genai.configure(api_key=gemini_key)

            def _gemini_summarize():
                for model_name in _gemini_model_attempts():
                    try:
                        model_obj = genai.GenerativeModel(model_name, system_instruction=system)
                        response = model_obj.generate_content(
                            f"Document:\n{doc_text[:30000]}\n\nSummary:",
                            generation_config={"max_output_tokens": 200},
                        )
                        return response.text.strip()
                    except Exception:
                        continue
                return None

            text = await asyncio.wait_for(asyncio.to_thread(_gemini_summarize), timeout=20.0)
            if text:
                return text
        except Exception:
            logger.exception("Gemini summarize fallback failed")

    excerpt = doc_text.strip()[:400]
    return excerpt or "Summary unavailable."


def _safe_json(text: str, default: dict) -> dict:
    if not text:
        return default
    try:
        val = json.loads(text)
        if isinstance(val, dict):
            return val
        if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
            return val[0]
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            val = json.loads(m.group(0))
            if isinstance(val, dict):
                return val
            if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                return val[0]
        except Exception:
            pass
    return default
