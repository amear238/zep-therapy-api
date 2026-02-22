import os
import json
from flask import Flask, request, jsonify
from zep_cloud.client import Zep

app = Flask(__name__)

ZEP_API_KEY = os.environ.get("ZEP_API_KEY")
USER_ID = "amear-bani-ahmad"

def get_zep_client():
    return Zep(api_key=ZEP_API_KEY)


# ─────────────────────────────────────────
# GET /context
# Called by TypingMind Dynamic Context
# ─────────────────────────────────────────
@app.route("/context", methods=["GET"])
def get_context():
    try:
        client = get_zep_client()
        last_message = request.args.get("last_message", "")

        # Get memory for the user
        memory = client.memory.get(
            session_id="phase2-session-latest",
            last_n=6
        )

        context_text = memory.context if memory.context else "No prior session memory found."

        return jsonify({
            "context": f"[ZEP MEMORY]\n{context_text}\n[END ZEP MEMORY]"
        })

    except Exception as e:
        # Return empty context on error — don't break the therapy session
        return jsonify({
            "context": f"[ZEP MEMORY]\nMemory retrieval unavailable: {str(e)}\n[END ZEP MEMORY]"
        }), 200


# ─────────────────────────────────────────
# POST /store
# Called by Zapier with ZEP-STORE block
# ─────────────────────────────────────────
@app.route("/store", methods=["POST"])
def store_session():
    try:
        client = get_zep_client()
        data = request.get_json()

        if not data:
            return jsonify({"error": "No JSON body received"}), 400

        session_id = data.get("session_id")
        content = data.get("content", "")
        role_type = data.get("role_type", "system")
        role = data.get("role", "SummaryMaster")

        if not session_id:
            return jsonify({"error": "session_id is required"}), 400

        # Add session memory to Zep
        client.memory.add(
            session_id=session_id,
            messages=[
                {
                    "role_type": role_type,
                    "role": role,
                    "content": content
                }
            ]
        )

        # Also update the "latest" session pointer for context retrieval
        try:
            client.memory.add(
                session_id="phase2-session-latest",
                messages=[
                    {
                        "role_type": role_type,
                        "role": role,
                        "content": content
                    }
                ]
            )
        except Exception:
            pass  # Non-fatal if latest pointer update fails

        return jsonify({
            "status": "success",
            "session_id": session_id
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────
# GET /health
# Deployment health check
# ─────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "user": USER_ID}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
