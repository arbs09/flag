import eventlet
eventlet.monkey_patch()

from engineio import WSGIApp as EngineIOWSGIApp
from app import app, socketio

sio_app = EngineIOWSGIApp(socketio, app)