#!/usr/bin/env python3
"""
Go2 WebRTC Topic Scanner

Connects to the robot via WebRTC and subscribes to all known topics
to show which ones are actively publishing data.

Usage:
    python3 list_topics.py                          # default IP
    python3 list_topics.py --ip 192.168.8.181       # custom IP
    python3 list_topics.py --duration 20            # wait longer
    python3 list_topics.py --ap                     # AP mode
"""

import argparse
import asyncio
import logging
import sys
import json
from unitree_webrtc_connect.webrtc_driver import UnitreeWebRTCConnection, WebRTCConnectionMethod
from unitree_webrtc_connect.constants import RTC_TOPIC

logging.basicConfig(level=logging.FATAL)

received_topics = {}

def make_callback(topic_name, topic_path):
    def callback(message):
        if topic_name not in received_topics:
            data = message.get('data', '')
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except (json.JSONDecodeError, TypeError):
                    pass
            preview = str(data)[:120]
            received_topics[topic_name] = preview
            print(f"  [{len(received_topics):2d}] {topic_path:50s} OK - {preview}")
            sys.stdout.flush()
    return callback

async def main():
    parser = argparse.ArgumentParser(description="Go2 WebRTC Topic Scanner")
    parser.add_argument("--ip", default="10.0.0.166", help="Robot IP address (default: 10.0.0.166)")
    parser.add_argument("--serial", default=None, help="Robot serial number (alternative to IP)")
    parser.add_argument("--ap", action="store_true", help="Use AP connection mode")
    parser.add_argument("--duration", type=int, default=10, help="Seconds to wait for data (default: 10)")
    args = parser.parse_args()

    if args.ap:
        conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalAP)
    elif args.serial:
        conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, serialNumber=args.serial)
    else:
        conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=args.ip)

    await conn.connect()

    print(f"\nSubscribing to {len(RTC_TOPIC)} WebRTC topics...\n")

    for name, path in RTC_TOPIC.items():
        conn.datachannel.pub_sub.subscribe(path, make_callback(name, path))

    print(f"Waiting {args.duration} seconds for topic data...\n")
    await asyncio.sleep(args.duration)

    print(f"\n{'='*60}")
    print(f"Results: {len(received_topics)}/{len(RTC_TOPIC)} topics responded\n")

    print("ACTIVE topics (publishing data):")
    for name, path in RTC_TOPIC.items():
        if name in received_topics:
            print(f"  + {path:50s} ({name})")

    silent = [(name, path) for name, path in RTC_TOPIC.items() if name not in received_topics]
    if silent:
        print(f"\nSILENT topics (request-only or inactive):")
        for name, path in silent:
            print(f"  - {path:50s} ({name})")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(0)
