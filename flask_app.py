

import logging

from flask import Flask, jsonify, request

from core import process_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

web_sessions: dict[str, dict] = {}


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(force=True, silent=True) or {}
    session_id = data.get("session_id")
    user_text = data.get("message", "")

    if not session_id:
        return jsonify({"error": "session_id is required"}), 400
    if not user_text:
        return jsonify({"error": "message is required"}), 400

    session = web_sessions.get(session_id, {})

    result = process_message(session, user_text)  # <-- same function bot.py calls
    web_sessions[session_id] = result["session"]

    return jsonify(
        {
            "reply": result["reply"],
            "complete": result["complete"],
            "search_input": result["search_input"],
            "scraper_result": result["scraper_result"],
        }
    )


@app.route("/reset", methods=["POST"])
def reset():
    data = request.get_json(force=True, silent=True) or {}
    session_id = data.get("session_id")
    if not session_id:
        return jsonify({"error": "session_id is required"}), 400
    web_sessions.pop(session_id, None)
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True)
