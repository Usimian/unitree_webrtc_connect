"""
Meta Quest 3 VR control for Unitree Go2.

Setup:
    1. PC and Quest 3 must be on the same WiFi network
    2. python vr_control.py --ip <robot-ip>
    3. On Quest 3 browser: https://<PC-IP>:5000
    4. Accept the certificate warning (Advanced → Proceed)
    5. Click "Enter VR"

Controls (in VR):
    Head tracking    — robot body orientation (yaw / pitch / roll)
    Left stick       — forward / back / strafe
    Right stick X    — rotate
    A button         — stand up
    B button         — lie down
    X button         — toggle head light
    Y button         — stop movement
    Either grip      — recenter head tracking
"""

import argparse
import asyncio
import base64
import logging
import os
import tempfile
import threading
import time

import cv2
from flask import Flask, render_template
from flask_socketio import SocketIO

from unitree_webrtc_connect.webrtc_driver import UnitreeWebRTCConnection, WebRTCConnectionMethod
from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD

logging.basicConfig(level=logging.FATAL)

# ── Tuning constants ──────────────────────────────────────────────────────────
JPEG_QUALITY  = 70      # lower = faster, less bandwidth
VIDEO_FPS     = 30      # max frames sent to Quest per second
HEAD_HZ       = 20      # max head-pose robot commands per second
MOVE_HZ       = 10      # max move robot commands per second

EULER_MAX     = {"roll": 0.3, "pitch": 0.3, "yaw": 0.6}   # radians
MOVE_SPEED    = 0.8     # 0.0–1.0 normalised joystick value
STRAFE_SPEED  = 0.6     # 0.0–1.0 normalised joystick value
ROTATE_SPEED  = 0.8     # 0.0–1.0 normalised joystick value
DEAD_ZONE     = 0.12    # joystick dead zone


# ── Helpers ───────────────────────────────────────────────────────────────────

def build_connection(args) -> UnitreeWebRTCConnection:
    if args.remote:
        return UnitreeWebRTCConnection(
            WebRTCConnectionMethod.Remote,
            serialNumber=args.serial,
            username=args.user,
            password=args.password,
        )
    if args.ip:
        return UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=args.ip)
    if args.serial:
        return UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, serialNumber=args.serial)
    return UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalAP)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def dead_zone(v, dz):
    return 0.0 if abs(v) < dz else v


def generate_ssl_cert():
    """Generate a temporary self-signed certificate for HTTPS (required for WebXR)."""
    from OpenSSL import crypto
    key = crypto.PKey()
    key.generate_key(crypto.TYPE_RSA, 2048)
    cert = crypto.X509()
    cert.get_subject().CN = "go2-vr"
    cert.set_serial_number(1)
    cert.gmtime_adj_notBefore(0)
    cert.gmtime_adj_notAfter(365 * 24 * 60 * 60)
    cert.set_issuer(cert.get_subject())
    cert.set_pubkey(key)
    cert.sign(key, "sha256")
    cf = tempfile.NamedTemporaryFile(delete=False, suffix=".crt")
    kf = tempfile.NamedTemporaryFile(delete=False, suffix=".key")
    cf.write(crypto.dump_certificate(crypto.FILETYPE_PEM, cert))
    kf.write(crypto.dump_privatekey(crypto.FILETYPE_PEM, key))
    cf.close(); kf.close()
    return cf.name, kf.name


# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), "templates"))
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")


def main():
    parser = argparse.ArgumentParser(description="Quest 3 VR control for Go2")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--ip",     help="Robot IP address (LocalSTA)")
    group.add_argument("--serial", help="Robot serial number")
    parser.add_argument("--remote",   action="store_true")
    parser.add_argument("--user",     help="Unitree account e-mail (remote only)")
    parser.add_argument("--password", help="Unitree account password (remote only)")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()

    conn = build_connection(args)

    # ── Shared state ──────────────────────────────────────────────────────────
    latest_frame = [None]
    frame_lock   = threading.Lock()
    light_on     = threading.Event()
    lying_down   = threading.Event()

    last_head_t  = [0.0]
    last_move_t  = [0.0]
    stop_sent    = [True]   # start true so we don't send a spurious stop on connect

    # ── Async robot commands ──────────────────────────────────────────────────

    async def send_euler(roll, pitch, yaw):
        await conn.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["SPORT_MOD"],
            {"api_id": SPORT_CMD["Euler"],
             "parameter": {"x": roll, "y": pitch, "z": yaw}}
        )

    async def send_wireless(lx=0.0, ly=0.0, rx=0.0, ry=0.0, keys=0):
        conn.datachannel.pub_sub.publish_without_callback(
            RTC_TOPIC["WIRELESS_CONTROLLER"],
            {"lx": lx, "ly": ly, "rx": rx, "ry": ry, "keys": keys}
        )

    async def send_stand_up():
        await conn.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["SPORT_MOD"], {"api_id": SPORT_CMD["StandUp"]}
        )
        await asyncio.sleep(2.0)
        # Return to BalanceStand so WIRELESS_CONTROLLER movement works again
        await conn.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["SPORT_MOD"], {"api_id": SPORT_CMD["BalanceStand"]}
        )

    async def send_stand_down():
        await conn.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["SPORT_MOD"], {"api_id": SPORT_CMD["StandDown"]}
        )

    async def set_light(brightness):
        await conn.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["VUI"],
            {"api_id": 1005, "parameter": {"brightness": brightness}}
        )

    def fire(coro):
        asyncio.run_coroutine_threadsafe(coro, robot_loop)

    # ── WebRTC / robot thread ─────────────────────────────────────────────────

    async def recv_camera(track):
        while True:
            frame = await track.recv()
            img = frame.to_ndarray(format="bgr24")
            with frame_lock:
                latest_frame[0] = img

    def run_robot_loop(loop):
        asyncio.set_event_loop(loop)
        async def setup():
            try:
                await conn.connect()
                conn.video.switchVideoChannel(True)
                conn.video.add_track_callback(recv_camera)
            except Exception as e:
                logging.error(f"WebRTC error: {e}")
        loop.run_until_complete(setup())
        loop.run_forever()

    robot_loop = asyncio.new_event_loop()
    threading.Thread(target=run_robot_loop, args=(robot_loop,), daemon=True).start()

    # ── Video broadcast thread ────────────────────────────────────────────────

    def video_broadcast():
        interval = 1.0 / VIDEO_FPS
        while True:
            t0 = time.time()
            with frame_lock:
                frame = latest_frame[0]
            if frame is not None:
                _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                b64 = base64.b64encode(buf.tobytes()).decode("ascii")
                socketio.emit("frame", b64)
            elapsed = time.time() - t0
            time.sleep(max(0.0, interval - elapsed))

    threading.Thread(target=video_broadcast, daemon=True).start()

    # ── Flask routes ──────────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return render_template("index.html")

    # ── Socket.IO events ──────────────────────────────────────────────────────

    @socketio.on("head_pose")
    def on_head_pose(data):
        now = time.time()
        if now - last_head_t[0] < 1.0 / HEAD_HZ:
            return
        last_head_t[0] = now
        roll  = clamp(float(data.get("roll",  0)), -EULER_MAX["roll"],  EULER_MAX["roll"])
        pitch = clamp(float(data.get("pitch", 0)), -EULER_MAX["pitch"], EULER_MAX["pitch"])
        yaw   = clamp(float(data.get("yaw",   0)), -EULER_MAX["yaw"],   EULER_MAX["yaw"])
        fire(send_euler(roll, pitch, yaw))

    @socketio.on("controller")
    def on_controller(data):
        now = time.time()
        if now - last_move_t[0] < 1.0 / MOVE_HZ:
            return
        last_move_t[0] = now
        lx = dead_zone(float(data.get("lx", 0)), DEAD_ZONE)   # strafe
        ly = dead_zone(float(data.get("ly", 0)), DEAD_ZONE)   # forward/back
        rx = dead_zone(float(data.get("rx", 0)), DEAD_ZONE)   # rotate
        if lx or ly or rx:
            fire(send_wireless(lx=lx * STRAFE_SPEED, ly=ly * MOVE_SPEED, rx=rx * ROTATE_SPEED))
            stop_sent[0] = False
        elif not stop_sent[0]:
            fire(send_wireless())   # all zeros = stop
            stop_sent[0] = True

    @socketio.on("action")
    def on_action(data):
        action = data.get("type")
        if action == "stand_up":
            lying_down.clear()
            fire(send_stand_up())
        elif action == "lie_down":
            lying_down.set()
            fire(send_stand_down())
        elif action == "stop":
            fire(send_wireless())
        elif action == "toggle_light":
            if light_on.is_set():
                light_on.clear()
                fire(set_light(0))
            else:
                light_on.set()
                fire(set_light(10))

    # ── Start ─────────────────────────────────────────────────────────────────

    import socket as _sock
    try:
        host_ip = _sock.gethostbyname(_sock.gethostname())
    except Exception:
        host_ip = "your-pc-ip"

    cert_path, key_path = generate_ssl_cert()
    print(f"\n  Quest 3 browser → https://{host_ip}:{args.port}")
    print("  Accept the certificate warning: Advanced → Proceed\n")

    socketio.run(app, host="0.0.0.0", port=args.port,
                 ssl_context=(cert_path, key_path),
                 allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
