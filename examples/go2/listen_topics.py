"""
listen_topics.py - Subscribe to voice/AI related topics and print everything received.
Use the robot's built-in voice command mode while this is running to see what comes out.

Usage: python3 examples/go2/listen_topics.py
"""

import asyncio
import json
import sys
from unitree_webrtc_connect.webrtc_driver import UnitreeWebRTCConnection, WebRTCConnectionMethod

ROBOT_IP = "10.0.0.166"

TOPICS = [
    "rt/gptflowfeedback",
    "rt/api/assistant_recorder/request",
    "rt/servicestate",
    "rt/multiplestate",
]

async def main():
    print("Connecting to robot...")
    conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=ROBOT_IP)
    await conn.connect()
    print("Connected. Now use the robot's built-in voice command mode and watch for output.\n")

    conn.audio.switchAudioChannel(True)

    for topic in TOPICS:
        conn.datachannel.pub_sub.subscribe(topic, lambda msg, t=topic: print(f"\n[{t}]\n{json.dumps(msg, indent=2)}"))
        print(f"Subscribed to: {topic}")

    print("\nListening... Press Ctrl+C to quit.\n")
    await asyncio.sleep(86400)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDone.")
        sys.exit(0)
