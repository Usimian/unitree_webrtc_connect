"""
test_mic.py - Records audio from the robot mic and prints RMS values.
Prompts you when to be silent and when to speak so we can set the right threshold.

Usage: python3 examples/go2/test_mic.py
"""

import asyncio
import sys
import numpy as np
from unitree_webrtc_connect.webrtc_driver import UnitreeWebRTCConnection, WebRTCConnectionMethod

ROBOT_IP = "10.0.0.166"
SAMPLERATE = 48000
CHANNELS = 2

rms_values = []
recording = False

async def on_frame(frame):
    if not recording:
        return
    audio = np.frombuffer(frame.to_ndarray(), dtype=np.int16)
    mono = audio.reshape(-1, CHANNELS).mean(axis=1).astype(np.int16)
    rms = float(np.sqrt(np.mean(mono.astype(np.float32) ** 2)))
    rms_values.append(rms)
    print(f"\r  RMS: {rms:.0f}    ", end="", flush=True)

async def main():
    global recording

    print("Connecting to robot...")
    conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=ROBOT_IP)
    await conn.connect()
    print("Connected.\n")

    conn.audio.switchAudioChannel(True)
    conn.audio.add_track_callback(on_frame)

    # Wait for audio to stabilize
    await asyncio.sleep(2)

    # --- SILENT phase ---
    print("=== PHASE 1: Stay SILENT for 5 seconds ===")
    rms_values.clear()
    recording = True
    await asyncio.sleep(5)
    recording = False
    silent_values = rms_values.copy()
    print(f"\n  Silent  — min: {min(silent_values):.0f}  max: {max(silent_values):.0f}  avg: {sum(silent_values)/len(silent_values):.0f}")

    await asyncio.sleep(1)

    # --- SPEAKING phase ---
    print("\n=== PHASE 2: Speak LOUDLY for 5 seconds (say 'hello Rex, how are you') ===")
    rms_values.clear()
    recording = True
    await asyncio.sleep(5)
    recording = False
    speech_values = rms_values.copy()
    print(f"\n  Speaking — min: {min(speech_values):.0f}  max: {max(speech_values):.0f}  avg: {sum(speech_values)/len(speech_values):.0f}")

    # Recommend threshold
    recommended = int((max(silent_values) + min(speech_values[len(speech_values)//4:])) / 2)
    print(f"\n=== RECOMMENDED THRESHOLD: {recommended} ===")

    await conn.disconnect()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
