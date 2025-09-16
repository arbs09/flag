import eventlet
eventlet.monkey_patch()

import json, random, time, os, redis, string
from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_socketio import SocketIO, join_room, leave_room, emit
from engineio import WSGIApp as EngineIOWSGIApp
from flask_minify import minify, decorators
from typing import cast
from threading import Timer

app = Flask(__name__)

app.secret_key = os.environ.get("FLASK_SECRET_KEY", "devkey")

app.config.update(
    SEND_FILE_MAX_AGE_DEFAULT=60 * 60 * 24 * 30 * 6,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE='Lax'
)

r = redis.Redis(host="redis", port=6379, db=0, decode_responses=True)

minify(app=app, passive=True)

socketio = SocketIO(app, message_queue="redis://redis:6379/0")

ROUND_DURATION_SEC = 5
room_timers = {}

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    storage_uri="redis://redis:6379",
    storage_options={"socket_connect_timeout": 30},
    strategy="moving-window",
)

limiter.init_app(app)

with open("flags.json", "r", encoding="utf-8") as f:
    FLAGS = json.load(f)

def random_room_id(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def save_room(room_id, data):
    r.set(f"room:{room_id}", json.dumps(data), ex=600)

def load_room(room_id: str):
    raw = r.get(f"room:{room_id}")
    raw_str = cast(str | None, raw)
    return json.loads(raw_str) if raw_str else None

def ensure_session_id():
    if "sid" not in session:
        session["sid"] = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
    return session["sid"]

def new_round_state():
    flag_id = random.choice(list(FLAGS.keys()))
    wrong_choices = random.sample([k for k in FLAGS if k != flag_id], 3)
    options = wrong_choices + [flag_id]
    random.shuffle(options)
    return {
        "flag_id": flag_id,
        "flag_file": f"{flag_id.lower()}.svg",
        "options": options,
        "option_names": [FLAGS[o] for o in options],
        "start_time": time.time(),
        "state": "playing",
        "answers": {}
    }

def schedule_round_end(room_id):
    # Cancel existing timer
    t = room_timers.get(room_id)
    if t:
        t.cancel()

    def end_round():
        room = load_room(room_id)
        if not room or not room.get("round"):
            return

        rnd = room["round"]
        elapsed = time.time() - rnd["start_time"]
        if elapsed < ROUND_DURATION_SEC - 0.05:
            # Safety: delay to exact end if fired early
            return schedule_round_end(room_id)

        correct_id = rnd["flag_id"]
        # Update scores
        for sid, pdata in room["players"].items():
            ans = rnd["answers"].get(sid)
            if ans and ans.get("correct"):
                pdata["score"] = pdata.get("score", 0) + 100
            elif ans and not ans.get("correct"):
                pdata["score"] = max(0, pdata.get("score", 0) - 1)

        # Prepare round results to broadcast
        scores = {sid: p["score"] for sid, p in room["players"].items()}
        emit("round_result", {
            "correct_flag_id": correct_id,
            "correct_flag_name": FLAGS[correct_id],
            "scores": scores
        }, to=room_id)

        # Start next round shortly
        room["round"] = new_round_state()
        save_room(room_id, room)
        emit("round_start", {
            "flag_file": room["round"]["flag_file"],
            "options": room["round"]["options"],
            "option_names": room["round"]["option_names"],
            "time_limit": ROUND_DURATION_SEC
        }, to=room_id)

        # Reschedule timer
        schedule_round_end(room_id)

    t = Timer(ROUND_DURATION_SEC, end_round)
    room_timers[room_id] = t
    t.start()

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

@app.route("/quiz", methods=["GET"])
@decorators.minify(html=True, js=True, cssless=True)
def quiz():
    if "score" not in session:
        session["score"] = 0
    return render_template("quiz.html")

@app.route("/preload")
@decorators.minify(html=True, js=True, cssless=True)
def preload():
    return render_template("preload.html", FLAGS=FLAGS)

@limiter.limit("10 per second")
@app.route("/solo_quiz_api", methods=["POST"])
def solo_quiz_api():
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

# Multiplayer

@app.route("/create_room", methods=["POST"])
def create_room():
    ensure_session_id()
    # Create unique room id
    for _ in range(5):
        room_id = random_room_id()
        if not load_room(room_id):
            break
    else:
        return jsonify({"error": "Could not create room"}), 500

    room = {
        "room_id": room_id,
        "players": {},
        "round": None,
        "created_at": time.time()
    }
    save_room(room_id, room)
    return jsonify({"room_id": room_id})

@app.route("/room/<room_id>")
def room_page(room_id):
    ensure_session_id()
    # Minimal template; all data via websockets
    return render_template("room.html", room_id=room_id)

@socketio.on("join_room")
def on_join(data):
    room_id = data.get("room_id")
    sid = ensure_session_id()
    room = load_room(room_id)
    if not room:
        emit("error", {"message": "Room not found"})
        return

    join_room(room_id)

    # Add player if not present
    players = room.get("players", {})
    if sid not in players:
        players[sid] = {"score": 0}
    room["players"] = players

    # If no active round, start one
    if not room.get("round"):
        room["round"] = new_round_state()

    save_room(room_id, room)

    # Send current round state only to this user initially
    emit("joined", {
        "room_id": room_id,
        "your_session_id": sid,
        "score": room["players"][sid]["score"]
    })
    emit("round_start", {
        "flag_file": room["round"]["flag_file"],
        "options": room["round"]["options"],
        "option_names": room["round"]["option_names"],
        "time_limit": ROUND_DURATION_SEC
    }, to=room_id)


    schedule_round_end(room_id)

@socketio.on("submit_answer")
def on_submit_answer(data):
    room_id = data.get("room_id")
    choice = data.get("choice")
    sid = ensure_session_id()
    room = load_room(room_id)
    if not room or not room.get("round"):
        emit("error", {"message": "Room not ready"})
        return

    rnd = room["round"]
    # Check time
    elapsed = time.time() - rnd["start_time"]
    in_time = elapsed <= ROUND_DURATION_SEC

    if not in_time:
        emit("answer_ack", {"accepted": False, "reason": "timeout"})
        return

    if choice not in FLAGS:
        emit("answer_ack", {"accepted": False, "reason": "invalid_choice"})
        return

    # Record answer (first answer sticks; ignore later)
    if sid not in rnd["answers"]:
        is_correct = FLAGS[choice] == FLAGS[rnd["flag_id"]]
        rnd["answers"][sid] = {"choice": choice, "correct": is_correct}

    save_room(room_id, room)
    emit("answer_ack", {"accepted": True})

@limiter.limit("5 per second")
@app.route("/reset")
def reset():
    session['score'] = 0
    return redirect(url_for("quiz"))
