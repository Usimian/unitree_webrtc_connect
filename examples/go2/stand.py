import asyncio
import logging
import json
import sys
from unitree_webrtc_connect.webrtc_driver import UnitreeWebRTCConnection, WebRTCConnectionMethod
from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD

logging.basicConfig(level=logging.FATAL)

async def main():
    conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip="10.0.0.166")
    await conn.connect()

    # Check current motion mode
    response = await conn.datachannel.pub_sub.publish_request_new(
        RTC_TOPIC["MOTION_SWITCHER"],
        {"api_id": 1001}
    )

    if response['data']['header']['status']['code'] == 0:
        data = json.loads(response['data']['data'])
        mode = data['name']
        print(f"Current mode: {mode}")
    else:
        print(f"Motion switcher response: {response}")

    # Switch to normal mode first
    print("Switching to normal mode...")
    resp = await conn.datachannel.pub_sub.publish_request_new(
        RTC_TOPIC["MOTION_SWITCHER"],
        {"api_id": 1002, "parameter": {"name": "normal"}}
    )
    print(f"Mode switch response: {resp['data']['header']['status']}")
    await asyncio.sleep(3)

    # Send RecoveryStand
    print("Sending RecoveryStand...")
    resp = await conn.datachannel.pub_sub.publish_request_new(
        RTC_TOPIC["SPORT_MOD"],
        {"api_id": SPORT_CMD["RecoveryStand"]}
    )
    print(f"RecoveryStand response: {resp['data']['header']['status']}")
    await asyncio.sleep(3)

    # Send StandUp
    print("Sending StandUp...")
    resp = await conn.datachannel.pub_sub.publish_request_new(
        RTC_TOPIC["SPORT_MOD"],
        {"api_id": SPORT_CMD["StandUp"]}
    )
    print(f"StandUp response: {resp['data']['header']['status']}")
    await asyncio.sleep(3)

    # Check mode again
    response = await conn.datachannel.pub_sub.publish_request_new(
        RTC_TOPIC["MOTION_SWITCHER"],
        {"api_id": 1001}
    )
    if response['data']['header']['status']['code'] == 0:
        data = json.loads(response['data']['data'])
        print(f"Final mode: {data['name']}")

    print("Done.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(0)
