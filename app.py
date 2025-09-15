import json
import random
import time
from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import os

app = Flask(__name__)

app.secret_key = os.environ.get("FLASK_SECRET_KEY", "devkey")

app.config.update(
    SEND_FILE_MAX_AGE_DEFAULT=60 * 60 * 24 * 30 * 6,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE='Lax'
)


limiter = Limiter(
    key_func=get_remote_address
)
limiter.init_app(app)

with open("flags.json", "r", encoding="utf-8") as f:
    FLAGS = json.load(f)

def get_new_quiz_state(message=None):
    flag_id = random.choice(list(FLAGS.keys()))
    wrong_choices = random.sample([k for k in FLAGS if k != flag_id], 3)
    options = wrong_choices + [flag_id]
    random.shuffle(options)
    session["flag_id"] = flag_id
    session["start_time"] = time.time()
    return {
        "flag_id": flag_id,
        "flag_file": f"{flag_id.lower()}.svg",
        "options": options,
        "score": session.get("score", 0),
        "FLAGS": FLAGS,
        "message": message
    }

@app.route("/")
def index():
    return redirect(url_for("quiz"))

@app.route("/preload")
def preload():
    return render_template("preload.html", FLAGS=FLAGS)

@limiter.limit("10 per second")
@app.route("/quiz_api", methods=["POST"])
def quiz_api():
    if "score" not in session:
        session["score"] = 0

    message = None
    if request.method == "POST":
        choice = request.form.get("choice")
        start_time = session.get("start_time")
        flag_id = session.get("flag_id")
        elapsed = time.time() - start_time if start_time else 999

        if elapsed > 5:
            pass
        elif flag_id and choice and choice in FLAGS:
            if FLAGS[flag_id] == FLAGS[choice]:
                session["score"] += 100
                message = "Richtig!"
            else:
                session["score"] = max(0, session["score"] - 1)
                message = "Falsch!"

    state = get_new_quiz_state(message)
    return jsonify({
        "flag_file": state["flag_file"],
        "options": state["options"],
        "score": state["score"],
        "message": state["message"],
        "option_names": [state["FLAGS"][opt] for opt in state["options"]]
    })

@app.route("/quiz", methods=["GET"])
def quiz():
    if "score" not in session:
        session["score"] = 0
    state = get_new_quiz_state()
    return render_template(
        "quiz.html",
        flag_file=state["flag_file"],
        options=state["options"],
        score=state["score"],
        FLAGS=FLAGS,
        message=state["message"]
    )

@limiter.limit("5 per second")
@app.route("/reset", methods=["POST"])
def reset():
    session['score'] = 0
    return redirect(url_for("quiz"))

if __name__ == "__main__":
    app.run(debug=True)
