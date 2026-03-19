"""
Object detection on the Unitree Go2 front RGB camera using RT-DETR-X.

Usage:
    python object_detection.py --ip 10.0.0.166
    python object_detection.py --serial B42D2000XXXXXXXX
    python object_detection.py --serial B42D2000XXXXXXXX --remote --user email@gmail.com --password pass

Controls:
    Arrow keys  — move / rotate robot (release to stop)
    q           — quit
"""

import argparse
import asyncio
import logging
import threading
import time
from queue import Queue

import cv2
import numpy as np
from ultralytics import YOLO

from unitree_webrtc_connect.webrtc_driver import UnitreeWebRTCConnection, WebRTCConnectionMethod
from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD

logging.basicConfig(level=logging.FATAL)

MODEL_NAME = "rtdetr-x.pt"
CONFIDENCE = 0.4

# Arrow key codes returned by cv2.waitKeyEx() on Linux
KEY_UP    = 65362
KEY_DOWN  = 65364
KEY_LEFT  = 65361
KEY_RIGHT = 65363
KEY_Q     = ord('q')

MOVE_SPEED   = 0.5   # 0.0–1.0 normalised joystick value
ROTATE_SPEED = 0.5   # 0.0–1.0 normalised joystick value
STOP_AFTER   = 0.15  # seconds after last keypress before sending stop


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


# ── Button ───────────────────────────────────────────────────────────────────

class Button:
    def __init__(self, x, y, w, h, label, tooltip, color_idle, color_active):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.label   = label
        self.tooltip = tooltip
        self.color_idle   = color_idle
        self.color_active = color_active

    def hit(self, mx, my):
        return self.x <= mx <= self.x + self.w and self.y <= my <= self.y + self.h

    def draw(self, frame, active=False, hovered=False):
        color = self.color_active if active else self.color_idle
        if hovered and not active:
            # Lighten slightly on hover
            color = tuple(min(255, c + 40) for c in color)
        cv2.rectangle(frame, (self.x, self.y), (self.x + self.w, self.y + self.h), color, -1)
        border = (255, 255, 100) if hovered else (255, 255, 255)
        cv2.rectangle(frame, (self.x, self.y), (self.x + self.w, self.y + self.h), border, 2 if hovered else 1)
        (tw, th), _ = cv2.getTextSize(self.label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        tx = self.x + (self.w - tw) // 2
        ty = self.y + (self.h + th) // 2
        cv2.putText(frame, self.label, (tx, ty),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)


def draw_tooltip(frame, text, mx, my):
    pad = 6
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
    tx = min(mx + 12, frame.shape[1] - tw - pad * 2 - 2)
    ty = max(my - 12, th + pad)
    cv2.rectangle(frame, (tx - pad, ty - th - pad), (tx + tw + pad, ty + pad), (30, 30, 30), -1)
    cv2.rectangle(frame, (tx - pad, ty - th - pad), (tx + tw + pad, ty + pad), (200, 200, 200), 1)
    cv2.putText(frame, text, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)


def draw_move_hud(frame, direction):
    """Draw a small arrow-key HUD in the bottom-left."""
    cx, cy = 60, frame.shape[0] - 60
    size = 18
    arrows = {
        "up":    ((cx, cy - size - 4), (cx - size, cy), (cx + size, cy)),
        "down":  ((cx, cy + size + 4), (cx - size, cy), (cx + size, cy)),
        "left":  ((cx - size - 4, cy), (cx, cy - size), (cx, cy + size)),
        "right": ((cx + size + 4, cy), (cx, cy - size), (cx, cy + size)),
    }
    labels = {"up": "fwd", "down": "back", "left": "left", "right": "right"}
    for name, pts in arrows.items():
        active = (name == direction)
        color  = (0, 220, 255) if active else (100, 100, 100)
        pts_np = np.array(pts, np.int32)
        cv2.fillPoly(frame, [pts_np], color)
    # label
    if direction:
        cv2.putText(frame, labels[direction], (cx - 20, cy + size + 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 255), 1, cv2.LINE_AA)


def main():
    parser = argparse.ArgumentParser(description="Go2 front-camera object detection")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--ip", help="Robot IP address (LocalSTA)")
    group.add_argument("--serial", help="Robot serial number")
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--user")
    parser.add_argument("--password")
    parser.add_argument("--model", default=MODEL_NAME)
    parser.add_argument("--conf", type=float, default=CONFIDENCE)
    args = parser.parse_args()

    print(f"Loading {args.model} ...")
    model = YOLO(args.model)
    print("Model ready.")

    frame_queue: Queue = Queue(maxsize=2)
    conn = build_connection(args)

    # ── Shared state ─────────────────────────────────────────────────────────
    light_on     = threading.Event()
    lying_down   = threading.Event()
    cmd_cooldown = threading.Event()
    cmd_cooldown.set()

    mouse_pos    = [0, 0]           # updated by mouse callback
    last_arrow   = [0.0]            # time of last arrow key press
    moving       = [False]          # whether robot is currently moving
    current_dir  = [None]           # "up" / "down" / "left" / "right" / None

    # ── Async commands ────────────────────────────────────────────────────────

    async def set_light(brightness: int):
        await conn.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["VUI"],
            {"api_id": 1005, "parameter": {"brightness": brightness}}
        )

    async def stand_down():
        await conn.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["SPORT_MOD"], {"api_id": SPORT_CMD["StandDown"]}
        )

    async def stand_up():
        await conn.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["SPORT_MOD"], {"api_id": SPORT_CMD["StandUp"]}
        )

    async def send_wireless(lx=0.0, ly=0.0, rx=0.0, ry=0.0, keys=0):
        conn.datachannel.pub_sub.publish_without_callback(
            RTC_TOPIC["WIRELESS_CONTROLLER"],
            {"lx": lx, "ly": ly, "rx": rx, "ry": ry, "keys": keys}
        )

    def fire(coro):
        asyncio.run_coroutine_threadsafe(coro, loop)

    def dispatch_btn(coro):
        """Debounced dispatch for button presses."""
        if cmd_cooldown.is_set():
            cmd_cooldown.clear()
            fire(coro)
            def reset():
                time.sleep(1.0)
                cmd_cooldown.set()
            threading.Thread(target=reset, daemon=True).start()

    # ── Movement watchdog — stops robot after key is released ─────────────────
    def movement_watchdog():
        while True:
            time.sleep(0.05)
            if moving[0] and (time.time() - last_arrow[0]) > STOP_AFTER:
                moving[0] = False
                current_dir[0] = None
                fire(send_wireless())   # all zeros = stop

    threading.Thread(target=movement_watchdog, daemon=True).start()

    def handle_arrow(key):
        last_arrow[0] = time.time()
        moving[0] = True
        if key == KEY_UP:
            current_dir[0] = "up"
            fire(send_wireless(ly=MOVE_SPEED))
        elif key == KEY_DOWN:
            current_dir[0] = "down"
            fire(send_wireless(ly=-MOVE_SPEED))
        elif key == KEY_LEFT:
            current_dir[0] = "left"
            fire(send_wireless(rx=-ROTATE_SPEED))
        elif key == KEY_RIGHT:
            current_dir[0] = "right"
            fire(send_wireless(rx=ROTATE_SPEED))

    # ── WebRTC ────────────────────────────────────────────────────────────────

    async def recv_camera_stream(track):
        while True:
            frame = await track.recv()
            img = frame.to_ndarray(format="bgr24")
            if frame_queue.full():
                try:
                    frame_queue.get_nowait()
                except Exception:
                    pass
            frame_queue.put(img)

    def run_asyncio_loop(loop):
        asyncio.set_event_loop(loop)
        async def setup():
            try:
                await conn.connect()
                conn.video.switchVideoChannel(True)
                conn.video.add_track_callback(recv_camera_stream)
            except Exception as e:
                logging.error(f"WebRTC connection error: {e}")
        loop.run_until_complete(setup())
        loop.run_forever()

    loop = asyncio.new_event_loop()
    threading.Thread(target=run_asyncio_loop, args=(loop,), daemon=True).start()

    # ── Buttons ───────────────────────────────────────────────────────────────
    BTN_W, BTN_H, BTN_PAD = 160, 45, 10
    btn_light   = Button(0, 0, BTN_W, BTN_H,
                         "Light ON/OFF", "Toggle head light",
                         (60, 60, 60), (30, 180, 30))
    btn_liedown = Button(0, 0, BTN_W, BTN_H,
                         "Lie Down", "Toggle lie down / stand up",
                         (60, 60, 150), (180, 60, 60))

    def position_buttons(fw, fh):
        x = fw - BTN_W - BTN_PAD
        btn_light.x   = x;  btn_light.y   = fh - (BTN_H + BTN_PAD) * 2
        btn_liedown.x = x;  btn_liedown.y = fh - (BTN_H + BTN_PAD)

    # ── Mouse callback ────────────────────────────────────────────────────────

    def on_mouse(event, mx, my, flags, param):
        mouse_pos[0], mouse_pos[1] = mx, my
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        if btn_light.hit(mx, my):
            if light_on.is_set():
                light_on.clear()
                dispatch_btn(set_light(0))
            else:
                light_on.set()
                dispatch_btn(set_light(10))
        elif btn_liedown.hit(mx, my):
            if lying_down.is_set():
                lying_down.clear()
                dispatch_btn(stand_up())
            else:
                lying_down.set()
                dispatch_btn(stand_down())

    win = "Go2 Object Detection"
    cv2.namedWindow(win)
    cv2.setMouseCallback(win, on_mouse)
    cv2.imshow(win, np.zeros((720, 1280, 3), dtype=np.uint8))
    cv2.waitKey(1)

    fps_timer   = time.time()
    frame_count = 0
    fps         = 0.0

    print("Waiting for camera frames … press 'q' to quit.")

    try:
        while True:
            if frame_queue.empty():
                time.sleep(0.005)
                key = cv2.waitKeyEx(1)
                if key == KEY_Q:
                    break
                if key in (KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT):
                    handle_arrow(key)
                continue

            img = frame_queue.get()
            results = model(img, conf=args.conf, verbose=False)[0]

            annotated = img.copy()

            # Detections
            for box in results.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                cls_id = int(box.cls[0])
                conf   = float(box.conf[0])
                label  = f"{model.names[cls_id]} {conf:.2f}"
                cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 2)
                (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 1)
                cv2.rectangle(annotated, (x1, y1 - th - 6), (x1 + tw + 2, y1), (0, 255, 0), -1)
                cv2.putText(annotated, label, (x1 + 1, y1 - 4),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 1, cv2.LINE_AA)

            # FPS
            frame_count += 1
            elapsed = time.time() - fps_timer
            if elapsed >= 1.0:
                fps = frame_count / elapsed
                fps_timer = time.time()
                frame_count = 0
            cv2.putText(annotated, f"FPS: {fps:.1f}", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 200, 255), 2, cv2.LINE_AA)

            # Object summary
            detected = [model.names[int(b.cls[0])] for b in results.boxes]
            counts = {}
            for n in detected:
                counts[n] = counts.get(n, 0) + 1
            summary = "  ".join(f"{n}:{c}" for n, c in counts.items())
            cv2.putText(annotated, summary, (10, annotated.shape[0] - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)

            # Arrow-key movement HUD
            draw_move_hud(annotated, current_dir[0])

            # Buttons
            h, w = annotated.shape[:2]
            position_buttons(w, h)
            mx, my = mouse_pos
            btn_liedown.label = "Stand Up" if lying_down.is_set() else "Lie Down"
            btn_liedown.tooltip = "Click to stand up" if lying_down.is_set() else "Click to lie down"

            for btn in (btn_light, btn_liedown):
                hovered = btn.hit(mx, my)
                active  = (btn is btn_light and light_on.is_set()) or \
                          (btn is btn_liedown and lying_down.is_set())
                btn.draw(annotated, active=active, hovered=hovered)
                if hovered:
                    draw_tooltip(annotated, btn.tooltip, mx, my)

            cv2.imshow(win, annotated)
            key = cv2.waitKeyEx(1)
            if key == KEY_Q:
                break
            if key in (KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT):
                handle_arrow(key)

    finally:
        cv2.destroyAllWindows()
        loop.call_soon_threadsafe(loop.stop)

if __name__ == "__main__":
    main()
