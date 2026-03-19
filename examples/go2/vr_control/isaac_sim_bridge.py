"""
Isaac Sim VR bridge for Unitree Go2.

Runs the trained Go2 locomotion policy inside Isaac Sim and streams the
simulated front camera to a Meta Quest 3 via the same Flask-SocketIO
server used by vr_control.py. No real robot required.

IMPORTANT: Must be launched with the IsaacLab conda environment from the
unitree_rl_lab directory so that unitree_rl_lab tasks are importable:

    cd ~/unitree_rl_lab
    conda run -n env_isaaclab python \\
        ../unitree_webrtc_connect/examples/go2/vr_control/isaac_sim_bridge.py \\
        --checkpoint logs/rsl_rl/unitree_go2_velocity/2026-02-26_09-39-03/exported/policy.pt \\
        --port 5000

On Quest 3 browser: https://<PC-IP>:5000  →  Advanced → Proceed  →  Enter VR

VR Controls (same HTML frontend as vr_control.py):
    Left stick       forward / back / strafe
    Right stick X    rotate
    A button         reset simulation
    B / Y button     stop movement
    Grip             recenter head (cosmetic — no effect on sim pose)
"""

# ── AppLauncher MUST be created before any other isaaclab imports ──────────────
import argparse
import os
import sys

from isaaclab.app import AppLauncher

parser = argparse.ArgumentParser(description="Isaac Sim VR bridge for Go2")
parser.add_argument(
    "--checkpoint",
    required=True,
    help="Path to exported TorchScript policy: .../exported/policy.pt",
)
parser.add_argument("--port", type=int, default=5000, help="Flask server port")
AppLauncher.add_app_launcher_args(parser)
args_cli = parser.parse_args()
args_cli.enable_cameras = True          # required for camera sensors to render

app_launcher = AppLauncher(args_cli)
simulation_app = app_launcher.app

# AppLauncher prepends its own paths to sys.path; restore conda site-packages
import sys as _sys, site as _site
for _sp in _site.getsitepackages():
    if _sp not in _sys.path:
        _sys.path.append(_sp)

# ── All other imports after app launch ───────────────────────────────────────
import base64
import tempfile
import threading
import time

import cv2
import gymnasium as gym
import numpy as np
import torch

from flask import Flask, render_template
from flask_socketio import SocketIO

from isaaclab.utils import configclass
from isaaclab.sensors import TiledCameraCfg
import isaaclab.sim as sim_utils
from isaaclab_rl.rsl_rl import RslRlVecEnvWrapper

import unitree_rl_lab.tasks  # noqa: F401 — registers gym environments
from unitree_rl_lab.tasks.locomotion.robots.go2.velocity_env_cfg import (
    RobotPlayEnvCfg,
    RobotSceneCfg,
)

# ── Tuning ───────────────────────────────────────────────────────────────────
JPEG_QUALITY = 70
VIDEO_FPS    = 30
CTRL_HZ      = 10       # max velocity command updates per second
DEAD_ZONE    = 0.12

# Command limits from deploy.yaml
VEL_X_MAX   =  1.0      # m/s  forward / back
VEL_Y_MAX   =  0.4      # m/s  strafe
ANG_Z_MAX   =  1.0      # rad/s  yaw

# Camera resolution (lower = faster streaming)
CAM_W, CAM_H = 640, 360


# ── Scene config: add a forward-facing camera to the Go2 base ────────────────

@configclass
class VRSceneCfg(RobotSceneCfg):
    """Robot scene with a forward-facing TiledCamera on the base link."""

    front_camera = TiledCameraCfg(
        prim_path="{ENV_REGEX_NS}/Robot/base/front_cam",
        offset=TiledCameraCfg.OffsetCfg(
            pos=(0.30, 0.0, 0.05),         # slightly in front of and above base origin
            rot=(0.5, -0.5, 0.5, -0.5),    # (w,x,y,z) forward-facing in ROS convention
            convention="ros",               # camera Z = optical axis
        ),
        data_types=["rgb"],
        spawn=sim_utils.PinholeCameraCfg(
            focal_length=24.0,
            focus_distance=400.0,
            horizontal_aperture=20.955,
            clipping_range=(0.1, 200.0),
        ),
        width=CAM_W,
        height=CAM_H,
    )


@configclass
class VREnvCfg(RobotPlayEnvCfg):
    """Single-env play config with camera, intended for VR teleoperation."""

    scene: VRSceneCfg = VRSceneCfg(num_envs=1, env_spacing=2.5)

    def __post_init__(self):
        super().__post_init__()
        self.scene.num_envs = 1
        # Disable terrain curriculum — not needed for single VR session
        if self.scene.terrain.terrain_generator is not None:
            self.scene.terrain.terrain_generator.curriculum = False
        # Disable terrain height variation for a flat floor experience
        self.scene.terrain.terrain_generator.sub_terrains = {
            "flat": self.scene.terrain.terrain_generator.sub_terrains.get(
                "flat", list(self.scene.terrain.terrain_generator.sub_terrains.values())[0]
            )
        }
        # Disable all termination conditions so the env never auto-resets
        self.terminations.time_out       = None
        self.terminations.base_contact   = None
        self.terminations.bad_orientation = None


# ── Flask / Socket.IO ─────────────────────────────────────────────────────────

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "templates"),
)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Shared state (read/written from both Isaac loop and SocketIO handlers)
latest_frame    = [None]
frame_lock      = threading.Lock()
vel_cmd         = [0.0, 0.0, 0.0]   # [lin_x, lin_y, ang_z]
vel_lock        = threading.Lock()
reset_flag      = [False]
last_ctrl_t     = [0.0]


def _dead_zone(v):
    return 0.0 if abs(v) < DEAD_ZONE else float(v)


@app.route("/")
def index():
    return render_template("index.html")


head_euler = [0.0, 0.0, 0.0]   # [roll, pitch, yaw] in radians
head_lock  = threading.Lock()

EULER_MAX = {"roll": 0.3, "pitch": 0.3, "yaw": 0.6}


def _clamp(v, lo, hi):
    return max(lo, min(hi, float(v)))


@socketio.on("head_pose")
def on_head_pose(data):
    with head_lock:
        head_euler[0] = _clamp(data.get("roll",  0), -EULER_MAX["roll"],  EULER_MAX["roll"])
        head_euler[1] = _clamp(data.get("pitch", 0), -EULER_MAX["pitch"], EULER_MAX["pitch"])
        head_euler[2] = _clamp(data.get("yaw",   0), -EULER_MAX["yaw"],   EULER_MAX["yaw"])


@socketio.on("controller")
def on_controller(data):
    now = time.time()
    if now - last_ctrl_t[0] < 1.0 / CTRL_HZ:
        return
    last_ctrl_t[0] = now
    lx = _dead_zone(data.get("lx", 0))   # strafe
    ly = _dead_zone(data.get("ly", 0))   # forward/back
    rx = _dead_zone(data.get("rx", 0))   # rotate
    with vel_lock:
        vel_cmd[0] =  ly * VEL_X_MAX
        vel_cmd[1] = -lx * VEL_Y_MAX
        vel_cmd[2] = -rx * ANG_Z_MAX


@socketio.on("action")
def on_action(data):
    action = data.get("type")
    if action == "stand_up":          # A button → reset sim
        reset_flag[0] = True
    elif action in ("lie_down", "stop", None):
        with vel_lock:
            vel_cmd[0] = vel_cmd[1] = vel_cmd[2] = 0.0


def _video_broadcast():
    """Background thread: encode latest frame as JPEG and broadcast at VIDEO_FPS."""
    interval = 1.0 / VIDEO_FPS
    while True:
        t0 = time.time()
        with frame_lock:
            frame = latest_frame[0]
        if frame is not None:
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if ok:
                b64 = base64.b64encode(buf.tobytes()).decode("ascii")
                socketio.emit("frame", b64)
        time.sleep(max(0.0, interval - (time.time() - t0)))


def _generate_ssl_cert():
    """Self-signed cert via openssl CLI — no pyopenssl dependency."""
    import subprocess
    cf = tempfile.NamedTemporaryFile(delete=False, suffix=".crt")
    kf = tempfile.NamedTemporaryFile(delete=False, suffix=".key")
    cf.close(); kf.close()
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", kf.name, "-out", cf.name,
        "-days", "365", "-nodes", "-subj", "/CN=go2-isaac-vr",
    ], check=True, capture_output=True)
    return cf.name, kf.name


# ── Static scenery ────────────────────────────────────────────────────────────

def _spawn_scenery():
    """Spawn a handful of coloured boxes and cylinders around the robot start."""
    props = [
        # (path, size_xyz, pos_xyz, color_rgb)
        # Crates near the robot
        ("/World/Scenery/CrateA",  (0.6, 0.6, 0.6),  ( 3.0,  0.0, 0.30), (0.55, 0.35, 0.15)),
        ("/World/Scenery/CrateB",  (0.6, 0.6, 1.2),  ( 3.0,  1.0, 0.60), (0.40, 0.25, 0.10)),
        ("/World/Scenery/CrateC",  (0.6, 0.6, 0.6),  ( 3.5,  0.6, 0.30), (0.55, 0.35, 0.15)),
        # Wall sections
        ("/World/Scenery/WallL",   (3.0, 0.3, 1.5),  ( 0.0,  4.0, 0.75), (0.70, 0.65, 0.60)),
        ("/World/Scenery/WallR",   (3.0, 0.3, 1.5),  ( 0.0, -4.0, 0.75), (0.70, 0.65, 0.60)),
        ("/World/Scenery/WallF",   (0.3, 3.0, 1.5),  ( 5.0,  0.0, 0.75), (0.70, 0.65, 0.60)),
        # Pillars
        ("/World/Scenery/PillarFL", (0.3, 0.3, 2.0), ( 4.5,  3.5, 1.00), (0.80, 0.80, 0.80)),
        ("/World/Scenery/PillarFR", (0.3, 0.3, 2.0), ( 4.5, -3.5, 1.00), (0.80, 0.80, 0.80)),
        # Low platform
        ("/World/Scenery/Platform", (2.0, 2.0, 0.2), (-3.0,  0.0, 0.10), (0.60, 0.60, 0.55)),
    ]
    for path, size, pos, color in props:
        cfg = sim_utils.CuboidCfg(
            size=size,
            collision_props=sim_utils.CollisionPropertiesCfg(),
            visual_material=sim_utils.PreviewSurfaceCfg(diffuse_color=color, roughness=0.8),
        )
        cfg.func(path, cfg, translation=pos)


# ── Main simulation loop ──────────────────────────────────────────────────────

def main():
    device = getattr(args_cli, "device", "cuda:0")

    # ── Build environment ────────────────────────────────────────────────────
    print("[bridge] Building VR environment…")
    env_cfg = VREnvCfg()
    env_cfg.sim.device = device

    raw_env = gym.make("Unitree-Go2-Velocity", cfg=env_cfg)
    env     = RslRlVecEnvWrapper(raw_env)

    # ── Spawn static scenery props ────────────────────────────────────────────
    _spawn_scenery()

    # ── Load TorchScript policy ──────────────────────────────────────────────
    print(f"[bridge] Loading policy: {args_cli.checkpoint}")
    policy = torch.jit.load(args_cli.checkpoint, map_location=device)
    policy.eval()

    # ── Disable command auto-resampling so VR commands aren't overwritten ───
    cmd_term = raw_env.unwrapped.command_manager.get_term("base_velocity")
    cmd_term.time_left[:] = 1e9   # push resample time far into the future

    # ── Robot articulation reference ─────────────────────────────────────────
    robot  = raw_env.unwrapped.scene["robot"]

    # ── Camera sensor reference ──────────────────────────────────────────────
    camera = raw_env.unwrapped.scene.sensors.get("front_camera")
    if camera is None:
        print("[bridge] WARNING: front_camera sensor not found — no video will stream")

    # ── Start Flask server in background ────────────────────────────────────
    cert_path, key_path = _generate_ssl_cert()
    threading.Thread(target=_video_broadcast, daemon=True).start()

    import socket as _sock
    try:
        host_ip = _sock.gethostbyname(_sock.gethostname())
    except Exception:
        host_ip = "your-pc-ip"

    print(f"\n  Quest 3 browser  →  https://{host_ip}:{args_cli.port}")
    print("  Accept cert warning: Advanced → Proceed\n")

    threading.Thread(
        target=lambda: socketio.run(
            app, host="0.0.0.0", port=args_cli.port,
            ssl_context=(cert_path, key_path),
            allow_unsafe_werkzeug=True,
        ),
        daemon=True,
    ).start()

    # ── Initial observations ─────────────────────────────────────────────────
    # get_observations() returns a TensorDict; extract the "policy" tensor for jit policy
    def _get_obs():
        td = env.get_observations()
        if hasattr(td, "__getitem__"):
            try:
                return td["policy"]
            except (KeyError, TypeError):
                pass
        return td

    obs = _get_obs()

    dt = raw_env.unwrapped.step_dt   # 0.02 s at 50 Hz

    # ── Simulation loop ──────────────────────────────────────────────────────
    print("[bridge] Running. Press Ctrl+C to exit.")
    while simulation_app.is_running():
        t0 = time.time()

        # Reset if A button pressed in VR
        if reset_flag[0]:
            reset_flag[0] = False
            td = env.reset()
            if isinstance(td, tuple):
                td = td[0]
            try:
                obs = td["policy"]
            except (KeyError, TypeError):
                obs = td
            cmd_term.time_left[:] = 1e9  # re-disable resampling after reset

        # Inject velocity command from VR directly into the command term
        with vel_lock:
            cmd = list(vel_cmd)
        cmd_term.vel_command_b[0, 0] = cmd[0]   # forward/back
        cmd_term.vel_command_b[0, 1] = cmd[1]   # strafe
        cmd_term.vel_command_b[0, 2] = cmd[2]   # yaw

        # Policy inference + sim step
        with torch.inference_mode():
            actions = policy(obs)
        step_out = env.step(actions)
        # step returns (TensorDict, rew, dones, extras) or (obs, rew, dones, trunc, extras)
        td_obs = step_out[0]
        dones  = step_out[2]
        try:
            obs = td_obs["policy"]
        except (KeyError, TypeError):
            obs = td_obs

        # Head tracking: not applied via root pose override (breaks locomotion physics).
        # VR head orientation is available in head_euler[] for future use.

        # Grab camera frame from env 0
        if camera is not None:
            rgb = camera.data.output.get("rgb")
            if rgb is not None and rgb.shape[0] > 0:
                # TiledCamera output: [N_envs, H, W, 4] RGBA uint8
                frame_np = rgb[0, :, :, :3].cpu().numpy().astype(np.uint8)
                frame_bgr = cv2.cvtColor(frame_np, cv2.COLOR_RGB2BGR)
                with frame_lock:
                    latest_frame[0] = frame_bgr

        # Real-time pacing
        sleep_t = dt - (time.time() - t0)
        if sleep_t > 0:
            time.sleep(sleep_t)

    env.close()


if __name__ == "__main__":
    main()
    simulation_app.close()
