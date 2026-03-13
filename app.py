#!/usr/bin/env python3
"""
Flask app for Render deployment — Therapeutic AI Pipeline
Endpoints: /health  /store  /context  /initialize  /daily-insights  /kb-update
Python 3.9 compatible.
"""

import os
import re
import logging
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

USER_ID   = "amear-bani-ahmad"
ROOT      = Path(__file__).parent
CACHE_TTL = timedelta(minutes=30)

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

def _zep_client():
    from zep_cloud.client import Zep
    return Zep(api_key=env("ZEP_API_KEY"))


def fetch_zep_memory():
    """Fetch user context from the most recent Zep thread via SDK."""
    try:
        client = _zep_client()
        threads = client.user.get_threads(USER_ID)
        if not threads:
            return "[NO ZEP THREADS]"
        # Pick the most recently created thread
        latest = max(threads, key=lambda t: t.created_at)
        ctx = client.thread.get_user_context(latest.thread_id)
        return ctx.context or "[EMPTY ZEP CONTEXT]"
    except Exception as e:
        logging.error("Zep memory fetch failed: %s", e)
        return f"[ZEP UNAVAILABLE - {e}]"


def zep_block(content):
    return f"[ZEP MEMORY]\n{content}\n[END ZEP MEMORY]"


def zep_store(session_id, user_id, role_type, role, content):
    """Write a message to a Zep thread via zep_cloud SDK."""
    from zep_cloud.types import Message

    # Map role_type to valid SDK role values
    role_map = {"system": "system", "user": "user", "assistant": "assistant"}
    sdk_role = role_map.get(role_type, "system")

    client = _zep_client()
    try:
        client.thread.add_messages(
            thread_id=session_id,
            messages=[Message(role=sdk_role, content=content)],
        )
    except Exception as first_err:
        # Thread may not exist yet — create it then retry once.
        if "not found" in str(first_err).lower() or "thread" in str(first_err).lower():
            try:
                client.thread.create(thread_id=session_id, user_id=user_id)
            except Exception as e:
                logging.warning("Thread creation warning: %s", e)
            client.thread.add_messages(
                thread_id=session_id,
                messages=[Message(role=sdk_role, content=content)],
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


# ── Daily Insights helpers ─────────────────────────────────────────────────────

INSIGHTS_LOG = ROOT / "daily-insights" / "insights-log.md"
INSIGHTS_START = "---DAILY-INSIGHTS-START---"
INSIGHTS_END = "---DAILY-INSIGHTS-END---"

KB_TARGETS = {
    "rolling-summary": ROOT / "knowledge-base" / "rolling-summary.md",
    "intake-summary": ROOT / "knowledge-base" / "intake-summary.md",
    "kb-reference": ROOT / "knowledge-base" / "knowledge-base-reference.md",
}


def parse_insights_block(raw):
    """Parse a DAILY-INSIGHTS block into structured dict."""
    result = {"raw": raw}

    def field(name):
        m = re.search(rf"^{name}:\s*(.+)$", raw, re.MULTILINE)
        return m.group(1).strip() if m else None

    result["session_date"] = field("SESSION_DATE")
    result["session_number"] = field("SESSION_NUMBER")

    # Mantras: pipe-separated
    mantras = field("MANTRAS")
    result["mantras"] = [m.strip() for m in mantras.split("|")] if mantras else []

    # Patterns: pipe-separated
    patterns = field("PATTERNS")
    result["patterns"] = [p.strip() for p in patterns.split("|")] if patterns else []

    # Risk flags: key:level pairs, pipe-separated
    rf_raw = field("RISK_FLAGS")
    risk_flags = {}
    if rf_raw:
        for entry in rf_raw.split("|"):
            parts = entry.strip().split(":", 1)
            if len(parts) == 2:
                risk_flags[parts[0].strip()] = parts[1].strip()
    result["risk_flags"] = risk_flags

    # Priority daily: framework | directive
    pd_raw = field("PRIORITY_DAILY")
    if pd_raw and "|" in pd_raw:
        parts = pd_raw.split("|", 1)
        result["priority_daily"] = {"framework": parts[0].strip(), "directive": parts[1].strip()}
    elif pd_raw:
        result["priority_daily"] = {"framework": pd_raw, "directive": ""}
    else:
        result["priority_daily"] = None

    # Habits: name|trigger|purpose
    habits_raw = field("HABITS")
    if habits_raw:
        parts = [h.strip() for h in habits_raw.split("|")]
        if len(parts) >= 3:
            result["habits"] = [{"name": parts[0], "trigger": parts[1], "purpose": parts[2]}]
        else:
            result["habits"] = [{"name": habits_raw, "trigger": "", "purpose": ""}]
    else:
        result["habits"] = []

    # Planner flags: flag|detail
    pf_raw = field("PLANNER_FLAGS")
    if pf_raw:
        parts = [p.strip() for p in pf_raw.split("|")]
        if len(parts) >= 2:
            result["planner_flags"] = [{"flag": parts[0], "detail": parts[1]}]
        else:
            result["planner_flags"] = [{"flag": pf_raw, "detail": ""}]
    else:
        result["planner_flags"] = []

    return result


def sanitize_ascii(text):
    """Strip curly quotes, em dashes, and smart apostrophes."""
    replacements = {
        "\u201c": '"', "\u201d": '"',  # curly double quotes
        "\u2018": "'", "\u2019": "'",  # curly single quotes
        "\u2014": "--", "\u2013": "-",  # em/en dashes
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


@app.route("/daily-insights", methods=["GET", "POST"])
def daily_insights():
    """Return the most recent DAILY-INSIGHTS block as structured JSON."""
    try:
        if not INSIGHTS_LOG.exists():
            return jsonify({"error": "no insights available", "raw": ""})

        text = INSIGHTS_LOG.read_text(encoding="utf-8")

        # Find the LAST insights block
        last_start = text.rfind(INSIGHTS_START)
        last_end = text.rfind(INSIGHTS_END)

        if last_start == -1 or last_end == -1 or last_end <= last_start:
            return jsonify({"error": "no insights available", "raw": ""})

        raw = text[last_start + len(INSIGHTS_START):last_end].strip()
        result = parse_insights_block(raw)
        logging.info("/daily-insights: returning session %s", result.get("session_number"))
        return jsonify(result)

    except Exception as e:
        logging.error("/daily-insights error: %s", e)
        return jsonify({"error": str(e), "raw": ""})


@app.route("/kb-update", methods=["POST"])
def kb_update():
    """Append a planner update to a knowledge-base file."""
    try:
        data = request.get_json(force=True) or {}

        target = data.get("target", "")
        content = data.get("content", "")
        source = data.get("source", "")
        action = data.get("action", "append")
        date = data.get("date", datetime.utcnow().strftime("%Y-%m-%d"))

        # Validation
        if target not in KB_TARGETS:
            return jsonify({"status": "error", "message": f"invalid target: {target}"}), 400
        if not content:
            return jsonify({"status": "error", "message": "content is required"}), 400
        if not source:
            return jsonify({"status": "error", "message": "source is required"}), 400
        if len(content) > 5000:
            return jsonify({"status": "error", "message": "content exceeds 5000 char limit"}), 400

        content = sanitize_ascii(content)
        target_path = KB_TARGETS[target]

        if action == "append":
            entry = f"\n\n---\n## PLANNER UPDATE -- {date}\n{content}\n---\n"
            with open(target_path, "a", encoding="utf-8") as f:
                f.write(entry)
            bytes_written = len(entry.encode("utf-8"))
            logging.info("/kb-update: appended %d bytes to %s from %s", bytes_written, target, source)
            return jsonify({"status": "success", "target": target, "action": action, "bytes_written": bytes_written})
        else:
            return jsonify({"status": "error", "message": f"unsupported action: {action}"}), 400

    except Exception as e:
        logging.error("/kb-update error: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
