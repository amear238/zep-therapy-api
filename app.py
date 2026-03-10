#!/usr/bin/env python3
"""
Flask app for Render deployment — Therapeutic AI Pipeline
Endpoints: /health  /store  /context  /initialize
Python 3.9 compatible.
"""

import os
import logging
import requests as http
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

USER_ID   = "amear-bani-ahmad"
ROOT      = Path(__file__).parent
CACHE_TTL = timedelta(minutes=30)
ZEP_BASE  = "https://api.getzep.com/api/v2"

# In-memory chat_id tracker — per worker, resets on restart.
# Render free tier runs single-worker so this is acceptable.
_chat_cache = {}      # {chat_id: datetime_utc}
_session_calls = 0    # increments each time /initialize triggers Gemini


# ── Env helper ─────────────────────────────────────────────────────────────────

def env(key):
    val = os.environ.get(key, "").strip()
    if not val:
        logging.warning("Environment variable %s not set", key)
    return val


# ── File helpers ───────────────────────────────────────────────────────────────

def read_file(path, label):
    if not path.exists():
        return f"[MISSING - {label}]"
    content = path.read_text().strip()
    return content if content else f"[EMPTY - {label}]"


def load_kb(env_var, rel_path, label):
    """Env var takes priority over filesystem (lets Render override sensitive KB)."""
    from_env = os.environ.get(env_var, "").strip()
    if from_env:
        return from_env
    return read_file(ROOT / rel_path, label)


# ── Zep helpers ────────────────────────────────────────────────────────────────

def _zep_headers():
    return {
        "Authorization": f"Api-Key {env('ZEP_API_KEY')}",
        "Content-Type": "application/json",
    }


def fetch_zep_memory():
    """GET /v2/users/{user_id}/memory — mirrors zep-context.sh exactly."""
    try:
        r = http.get(
            f"{ZEP_BASE}/users/{USER_ID}/memory",
            headers=_zep_headers(),
            timeout=10,
        )
        r.raise_for_status()
        return r.text
    except Exception as e:
        logging.error("Zep memory fetch failed: %s", e)
        return f"[ZEP UNAVAILABLE - {e}]"


def zep_block(content):
    return f"[ZEP MEMORY]\n{content}\n[END ZEP MEMORY]"


def zep_store(session_id, user_id, role_type, role, content):
    """Write a message to a Zep session via zep_cloud SDK."""
    from zep_cloud.client import Zep
    from zep_cloud.types import Message

    client = Zep(api_key=env("ZEP_API_KEY"))

    try:
        client.memory.add(
            session_id=session_id,
            messages=[Message(role_type=role_type, role=role, content=content)],
        )
    except Exception as first_err:
        # Session may not exist yet — create it then retry once.
        if "not found" in str(first_err).lower() or "session" in str(first_err).lower():
            try:
                client.memory.add_session(session_id=session_id, user_id=user_id)
            except Exception as e:
                logging.warning("Session creation warning: %s", e)
            client.memory.add(
                session_id=session_id,
                messages=[Message(role_type=role_type, role=role, content=content)],
            )
        else:
            raise


# ── Gemini helper ──────────────────────────────────────────────────────────────

def run_gemini_initializer(zep_content, session_number):
    from google import genai

    instructions = read_file(ROOT / "agents" / "initializer.md", "initializer.md")
    rolling      = load_kb("KB_ROLLING_SUMMARY", "knowledge-base/rolling-summary.md", "rolling-summary.md")
    intake       = load_kb("KB_INTAKE_SUMMARY",  "knowledge-base/intake-summary.md",  "intake-summary.md")

    prompt = f"""You are the INITIALIZER agent. Follow your instructions exactly.

INSTRUCTIONS:
{instructions}

SESSION NUMBER: {session_number}

ROLLING SUMMARY:
{rolling}

INTAKE SUMMARY:
{intake}

ZEP DYNAMIC CONTEXT:
{zep_content}

Output the SESSION BRIEF now. No preamble."""

    client = genai.Client(api_key=env("GEMINI_API_KEY"))
    response = client.models.generate_content(
        model="gemini-2.5-pro",
        contents=prompt,
        config={"temperature": 0.15, "max_output_tokens": 4096},
    )
    return response.text


# ── Session counter ────────────────────────────────────────────────────────────

def next_session_number():
    global _session_calls
    _session_calls += 1
    base = int(os.environ.get("SESSION_BASE_NUMBER", "1"))
    return base + _session_calls - 1


# ── Chat cache helpers ─────────────────────────────────────────────────────────

def is_chat_new(chat_id):
    """Returns True (and updates cache) if chat_id is new or older than CACHE_TTL."""
    now = datetime.utcnow()
    cached = _chat_cache.get(chat_id)
    is_new = cached is None or (now - cached) > CACHE_TTL

    if is_new:
        _chat_cache[chat_id] = now
        # Prune entries older than 3x TTL to keep memory bounded
        cutoff = now - CACHE_TTL * 3
        for k in list(_chat_cache.keys()):
            if _chat_cache[k] < cutoff:
                del _chat_cache[k]

    return is_new


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "user": USER_ID})


@app.route("/store", methods=["POST"])
def store():
    """
    Accepts: {session_id, user_id, role_type, role, content}
    Always returns 200 — errors are logged but never block TypingMind.
    """
    try:
        data = request.get_json(force=True) or {}
        zep_store(
            session_id=data.get("session_id", "unknown"),
            user_id=data.get("user_id", USER_ID),
            role_type=data.get("role_type", "system"),
            role=data.get("role", "SummaryMaster"),
            content=data.get("content", ""),
        )
        logging.info("Stored session: %s", data.get("session_id"))
    except Exception as e:
        logging.error("/store error: %s", e)
    return jsonify({"status": "success"})


@app.route("/context", methods=["POST"])
def context():
    """Returns Zep user memory formatted as [ZEP MEMORY] block."""
    zep_content = fetch_zep_memory()
    return jsonify({"context": zep_block(zep_content)})


@app.route("/initialize", methods=["POST"])
def initialize():
    """
    Accepts: {chat_id}
    - New or expired chat_id: run Gemini initializer + Zep context, return both.
    - Recent chat_id (< 30 min): return Zep context only.
    - Any failure: fall back to Zep context only, never block.
    """
    try:
        data    = request.get_json(force=True) or {}
        chat_id = str(data.get("chat_id", ""))

        zep_content = fetch_zep_memory()

        if chat_id and is_chat_new(chat_id):
            try:
                session_num = next_session_number()
                brief = run_gemini_initializer(zep_content, session_num)
                combined = f"{brief}\n\n---\n\n{zep_block(zep_content)}"
                logging.info("Initialize: Gemini brief generated for chat_id %s (session %d)", chat_id, session_num)
                return jsonify({"context": combined})
            except Exception as e:
                logging.error("Gemini initializer failed, falling back to Zep only: %s", e)

        return jsonify({"context": zep_block(zep_content)})

    except Exception as e:
        logging.error("/initialize error: %s", e)
        return jsonify({"context": zep_block("[ZEP UNAVAILABLE]")})


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
