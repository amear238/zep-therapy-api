import os
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

        # V3 SDK: thread.get_user_context
        context = client.thread.get_user_context(
            thread_id="phase2-session-latest"
        )

        context_text = context.context if context.context else "No prior session memory found."

        return jsonify({
            "context": f"[ZEP MEMORY]\n{context_text}\n[END ZEP MEMORY]"
        })

    except Exception as e:
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
        role = data.get("role", "SummaryMaster")

        if not session_id:
            return jsonify({"error": "session_id is required"}), 400

        # V3 SDK: thread.create
        try:
            client.thread.create(
                thread_id=session_id,
                user_id=USER_ID
            )
        except Exception:
            pass  # Thread may already exist

        # V3 SDK: thread.add_messages
        # role_type is now "role", role is now "name"
        client.thread.add_messages(
            thread_id=session_id,
            messages=[
                {
                    "role": "system",
                    "name": role,
                    "content": content
                }
            ]
        )

        # Mirror to latest thread
        try:
            client.thread.create(
                thread_id="phase2-session-latest",
                user_id=USER_ID
            )
        except Exception:
            pass

        try:
            client.thread.add_messages(
                thread_id="phase2-session-latest",
                messages=[
                    {
                        "role": "system",
                        "name": role,
                        "content": content
                    }
                ]
            )
        except Exception:
            pass

        return jsonify({
            "status": "success",
            "session_id": session_id
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────
# GET /health
# ─────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "user": USER_ID}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
