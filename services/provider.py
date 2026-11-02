"""A mock cloud provider. Latency and outages are controlled through Redis flags."""
import os, time, random, redis
from flask import Flask, jsonify

NAME, PORT = os.environ["NAME"], int(os.environ["PORT"])
BASE = int(os.environ.get("BASE_MS", "40"))
r = redis.Redis(host=os.environ.get("REDIS_HOST", "redis"), decode_responses=True)
app = Flask(__name__)

def flags():
    try:
        return r.get(f"{NAME}:killed") == "1", int(r.get(f"{NAME}:spike") or 0)
    except redis.RedisError:
        return False, 0

def reply(body):
    resp = jsonify(body)
    resp.headers["X-Provider"] = NAME
    return resp

@app.route("/health")
def health():
    killed, spike = flags()
    if killed:
        return reply({"status": "down"}), 503
    time.sleep((BASE * 0.3 + random.random() * 8 + spike) / 1000)
    return reply({"status": "ok"})

@app.route("/work")
def work():
    killed, spike = flags()
    if killed:
        return reply({"error": "provider offline"}), 503
    time.sleep(max(0, BASE + random.gauss(0, BASE * 0.2) + spike) / 1000)
    return reply({"provider": NAME, "ok": True})

app.run(host="0.0.0.0", port=PORT, threaded=True)
