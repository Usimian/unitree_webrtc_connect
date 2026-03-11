# Go2 WebRTC Command & Sensor Reference

This document covers all commands, sensors, and topics available through the `unitree_webrtc_connect` WebRTC bridge.

## Architecture

The Go2 runs an internal WebRTC bridge (`unitreeWebRTCClientMaster`) that translates WebRTC data channel messages to/from the robot's internal CycloneDDS topics. This is the same protocol used by the Unitree Go app — no jailbreak or ROS2 environment is required on the client side.

```
Your PC  <--WebRTC-->  Go2 WebRTC Bridge  <--CycloneDDS-->  Robot Internals
```

## Connection

```python
from unitree_webrtc_connect.webrtc_driver import UnitreeWebRTCConnection, WebRTCConnectionMethod

# Direct WiFi AP
conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalAP)

# Same LAN (by IP)
conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip="10.0.0.166")

# Same LAN (by serial, uses multicast discovery)
conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, serialNumber="B42D2000XXXXXXXX")

# Remote (via Unitree TURN server)
conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.Remote, serialNumber="...", username="...", password="...")

await conn.connect()
```

## Listing Active Topics

Run the included topic scanner to see which topics are actively publishing:

```bash
python3 examples/go2/list_topics.py
```

---

## Subscribable Sensor Topics

These topics stream data continuously once subscribed. Use `conn.datachannel.pub_sub.subscribe(topic, callback)`.

### rt/lf/sportmodestate (LF_SPORT_MOD_STATE)

High-level robot state at ~50Hz. The most useful general-purpose topic.

| Field | Description |
|-------|-------------|
| `mode` | Current motion mode (0=normal) |
| `progress` | Motion progress |
| `gait_type` | Current gait type |
| `foot_raise_height` | Foot raise height (m) |
| `position` | [x, y, z] position from odometry |
| `body_height` | Body height (m), ~0.33 standing, ~0.07 lying |
| `velocity` | [vx, vy, vz] velocity |
| `yaw_speed` | Yaw angular velocity |
| `range_obstacle` | [front, right, rear, left] obstacle distances |
| `foot_force` | [FL, FR, RL, RR] foot contact forces |
| `foot_position_body` | 12 floats, foot positions in body frame |
| `foot_speed_body` | 12 floats, foot velocities in body frame |
| `imu_state.quaternion` | [w, x, y, z] orientation quaternion |
| `imu_state.gyroscope` | [wx, wy, wz] angular velocity (rad/s) |
| `imu_state.accelerometer` | [ax, ay, az] acceleration (m/s^2) |
| `imu_state.rpy` | [roll, pitch, yaw] (rad) |
| `imu_state.temperature` | IMU temperature (C) |

```python
conn.datachannel.pub_sub.subscribe("rt/lf/sportmodestate", callback)
```

### rt/lf/lowstate (LOW_STATE)

Low-level hardware state — motor positions, battery, foot force.

| Field | Description |
|-------|-------------|
| `imu_state.rpy` | [roll, pitch, yaw] (rad) |
| `motor_state[]` | Array of 12 motors, each with `q` (position), `temperature`, `lost` |
| `bms_state` | Battery: `soc` (%), `current` (mA), `cycle`, `bq_ntc`, `mcu_ntc` |
| `foot_force` | [FL, FR, RL, RR] raw foot forces |
| `temperature_ntc1` | Board temperature (C) |
| `power_v` | Power supply voltage (V) |

```python
conn.datachannel.pub_sub.subscribe("rt/lf/lowstate", callback)
```

### rt/multiplestate (MULTIPLE_STATE)

General settings state.

| Field | Description |
|-------|-------------|
| `bodyHeight` | Current body height |
| `brightness` | LED brightness (0-10) |
| `footRaiseHeight` | Foot raise height |
| `obstaclesAvoidSwitch` | Obstacle avoidance on/off |
| `speedLevel` | Current speed level |
| `uwbSwitch` | UWB on/off |
| `volume` | Speaker volume (0-10) |

```python
conn.datachannel.pub_sub.subscribe("rt/multiplestate", callback)
```

### rt/utlidar/robot_pose (ROBOTODOM)

LiDAR-based odometry pose.

| Field | Description |
|-------|-------------|
| `header.stamp` | Timestamp |
| `header.frame_id` | "odom" |
| `pose.position` | {x, y, z} |
| `pose.orientation` | {x, y, z, w} quaternion |

```python
conn.datachannel.pub_sub.subscribe("rt/utlidar/robot_pose", callback)
```

### rt/utlidar/lidar_state (ULIDAR_STATE)

LiDAR sensor status.

| Field | Description |
|-------|-------------|
| `firmware_version` | LiDAR firmware |
| `software_version` | LiDAR software version |
| `sys_rotation_speed` | Rotation speed |

```python
conn.datachannel.pub_sub.subscribe("rt/utlidar/lidar_state", callback)
```

### rt/utlidar/voxel_map_compressed (ULIDAR_ARRAY)

Compressed LiDAR point cloud. Requires enabling traffic and turning on the LiDAR first:

```python
await conn.datachannel.disableTrafficSaving(True)
conn.datachannel.pub_sub.publish_without_callback("rt/utlidar/switch", "on")
# Choose decoder: 'libvoxel' (voxel mesh) or 'native' (raw point cloud)
conn.datachannel.set_decoder(decoder_type='native')
conn.datachannel.pub_sub.subscribe("rt/utlidar/voxel_map_compressed", callback)
```

### rt/audiohub/player/state (AUDIO_HUB_PLAY_STATE)

Audio player status.

| Field | Description |
|-------|-------------|
| `play_state` | "not_in_use", "playing", etc. |
| `is_playing` | Boolean |
| `current_audio_unique_id` | Currently playing audio ID |
| `current_audio_custom_name` | Audio name |

### Other subscribable topics (require specific modes)

| Topic | Constant | Description |
|-------|----------|-------------|
| `rt/sportmodestate` | SPORT_MOD_STATE | Sport mode state (alternative frequency) |
| `rt/uwbstate` | UWB_STATE | UWB positioning state |
| `rt/wirelesscontroller` | WIRELESS_CONTROLLER | Wireless controller input |
| `rt/selftest` | SELF_TEST | Self-test results |
| `rt/mapping/grid_map` | GRID_MAP | 2D grid map |
| `rt/servicestate` | SERVICE_STATE | Service state |
| `rt/gptflowfeedback` | GPT_FEEDBACK | GPT flow feedback |
| `rt/gas_sensor` | GAS_SENSOR | Gas sensor data |
| `rt/qt_notice` | SLAM_QT_NOTICE | SLAM notifications |
| `rt/pctoimage_local` | SLAM_PC_TO_IMAGE_LOCAL | Point cloud to image |
| `rt/lio_sam_ros2/mapping/odometry` | SLAM_ODOMETRY | SLAM odometry |
| `rt/arm_Feedback` | ARM_FEEDBACK | Arm feedback (if equipped) |
| `rt/uslam/frontend/cloud_world_ds` | LIDAR_MAPPING_CLOUD_POINT | SLAM cloud points |
| `rt/uslam/frontend/odom` | LIDAR_MAPPING_ODOM | SLAM front-end odometry |
| `rt/uslam/cloud_map` | LIDAR_MAPPING_PCD_FILE | SLAM PCD map |
| `rt/uslam/server_log` | LIDAR_MAPPING_SERVER_LOG | SLAM server log |
| `rt/uslam/localization/odom` | LIDAR_LOCALIZATION_ODOM | Localization odometry |
| `rt/uslam/navigation/global_path` | LIDAR_NAVIGATION_GLOBAL_PATH | Navigation path |
| `rt/uslam/localization/cloud_world` | LIDAR_LOCALIZATION_CLOUD_POINT | Localization cloud |

---

## Commands

Commands are sent via `conn.datachannel.pub_sub.publish_request_new(topic, options)`.

### Motion Switcher (rt/api/motion_switcher/request)

Controls the robot's overall motion mode.

| api_id | Action | Parameter |
|--------|--------|-----------|
| 1001 | Get current mode | (none) |
| 1002 | Set mode | `{"name": "<mode>"}` |

Available modes: `"normal"`, `"ai"`, `"advanced"`

```python
# Get current mode
resp = await conn.datachannel.pub_sub.publish_request_new(
    "rt/api/motion_switcher/request", {"api_id": 1001})

# Switch to normal mode (robot stands up)
await conn.datachannel.pub_sub.publish_request_new(
    "rt/api/motion_switcher/request",
    {"api_id": 1002, "parameter": {"name": "normal"}})
```

### Sport Commands (rt/api/sport/request)

All sport mode movement commands. Robot must be in "normal" or "ai" mode.

#### Basic Posture

| api_id | Name | Description | Parameter |
|--------|------|-------------|-----------|
| 1001 | Damp | Disable all motors (robot collapses) | — |
| 1002 | BalanceStand | Stand in balanced position | — |
| 1003 | StopMove | Stop all movement | — |
| 1004 | StandUp | Stand up from sitting | — |
| 1005 | StandDown | Sit down / lower body | — |
| 1006 | RecoveryStand | Recover to standing from any position | — |
| 1009 | Sit | Sit down | — |
| 1010 | RiseSit | Rise from sitting | — |
| 1050 | Standup | Stand up (alternate) | — |

#### Movement

| api_id | Name | Description | Parameter |
|--------|------|-------------|-----------|
| 1008 | Move | Move in direction | `{"x": fwd, "y": left, "z": yaw}` (m/s, rad/s) |
| 1007 | Euler | Set body orientation | `{"x": roll, "y": pitch, "z": yaw}` (rad) |
| 1013 | BodyHeight | Set body height | height value |
| 1014 | FootRaiseHeight | Set foot raise height | height value |
| 1015 | SpeedLevel | Set speed level | level value |
| 1028 | Pose | Set body pose | pose parameters |

#### Gait

| api_id | Name | Description | Parameter |
|--------|------|-------------|-----------|
| 1011 | SwitchGait | Change gait type | gait ID |
| 1019 | ContinuousGait | Continuous gait mode | — |
| 1035 | EconomicGait | Economy gait (lower power) | — |
| 1051 | CrossWalk | Cross-step walking | — |

#### Tricks & Animations

| api_id | Name | Description |
|--------|------|-------------|
| 1016 | Hello | Wave hello |
| 1017 | Stretch | Stretch body |
| 1020 | Content | Happy gesture |
| 1021 | Wallow | Lie down and roll |
| 1022 | Dance1 | Dance routine 1 |
| 1023 | Dance2 | Dance routine 2 |
| 1029 | Scrape | Scrape ground |
| 1033 | WiggleHips | Wiggle hips |
| 1036 | FingerHeart | Finger heart gesture |
| 1305 | MoonWalk | Moonwalk |

#### Acrobatics (use with caution)

| api_id | Name | Description |
|--------|------|-------------|
| 1030 | FrontFlip | Front flip |
| 1031 | FrontJump | Front jump |
| 1032 | FrontPounce | Front pounce |
| 1042 | LeftFlip | Left flip |
| 1043 | RightFlip | Right flip |
| 1044 | BackFlip | Backflip |
| 1301 | Handstand | Handstand |
| 1302 | CrossStep | Cross step |
| 1303 | OnesidedStep | One-sided step |
| 1304 | Bound | Bounding gait |

#### AI Mode Commands

| api_id | Name | Description | Parameter |
|--------|------|-------------|-----------|
| 1039 | StandOut | Toggle handstand in AI mode | `{"data": true/false}` |
| 1045 | FreeWalk / LeadFollow | Free walk / lead follow mode | — |

#### Query

| api_id | Name | Description |
|--------|------|-------------|
| 1024 | GetBodyHeight | Get current body height |
| 1025 | GetFootRaiseHeight | Get foot raise height |
| 1026 | GetSpeedLevel | Get speed level |
| 1034 | GetState | Get current state |

```python
from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD

# Wave hello
await conn.datachannel.pub_sub.publish_request_new(
    RTC_TOPIC["SPORT_MOD"], {"api_id": SPORT_CMD["Hello"]})

# Move forward at 0.5 m/s
await conn.datachannel.pub_sub.publish_request_new(
    RTC_TOPIC["SPORT_MOD"],
    {"api_id": SPORT_CMD["Move"], "parameter": {"x": 0.5, "y": 0, "z": 0}})

# Stop
await conn.datachannel.pub_sub.publish_request_new(
    RTC_TOPIC["SPORT_MOD"], {"api_id": SPORT_CMD["StopMove"]})
```

### VUI — LED & Volume Control (rt/api/vui/request)

| api_id | Action | Parameter |
|--------|--------|-----------|
| 1003 | Set volume | `{"volume": 0-10}` |
| 1004 | Get volume | — |
| 1005 | Set brightness | `{"brightness": 0-10}` |
| 1006 | Get brightness | — |
| 1007 | Set LED color | `{"color": "<color>", "time": seconds}` |
| 1007 | Flash LED | `{"color": "<color>", "time": seconds, "flash_cycle": ms}` |

LED colors: `white`, `red`, `yellow`, `blue`, `green`, `cyan`, `purple`

```python
from unitree_webrtc_connect.constants import RTC_TOPIC, VUI_COLOR

# Set LED to purple for 5 seconds
await conn.datachannel.pub_sub.publish_request_new(
    RTC_TOPIC["VUI"],
    {"api_id": 1007, "parameter": {"color": VUI_COLOR.PURPLE, "time": 5}})
```

### Obstacle Avoidance (rt/api/obstacles_avoid/request)

```python
await conn.datachannel.pub_sub.publish_request_new(
    RTC_TOPIC["OBSTACLES_AVOID"], {"api_id": ..., "parameter": ...})
```

### Audio Hub (rt/api/audiohub/request)

| api_id | Action | Parameter |
|--------|--------|-----------|
| 1001 | Get audio list | — |
| 1002 | Play selected audio | audio selection |
| 1003 | Pause | — |
| 1004 | Resume | — |
| 1005 | Play previous | — |
| 1006 | Play next | — |
| 1007 | Set play mode | mode |
| 1008 | Rename audio | — |
| 1009 | Delete audio | — |
| 1010 | Get play mode | — |
| 2001 | Upload audio file | — |
| 3001 | Play start obstacle avoidance sound | — |
| 3002 | Play exit obstacle avoidance sound | — |
| 3003 | Play start companion mode sound | — |
| 3004 | Play exit companion mode sound | — |
| 4001 | Enter megaphone mode | — |
| 4002 | Exit megaphone mode | — |
| 4003 | Upload megaphone audio | — |
| 5001 | Play long corpus | — |
| 5002 | Long corpus playback completed | — |
| 5003 | Stop long corpus | — |

### LiDAR Mapping (rt/uslam/client_command)

SLAM mapping commands for the built-in LiDAR.

### Wireless Controller Emulation (rt/wirelesscontroller)

Send joystick-like commands directly (no api_id needed):

```python
conn.datachannel.pub_sub.publish_without_callback(
    "rt/wirelesscontroller",
    {"lx": 0.0, "ly": 0.5, "rx": 0.0, "ry": 0.0, "keys": 0})
```

| Field | Description | Range |
|-------|-------------|-------|
| `lx` | Left stick X (strafe) | -1.0 to 1.0 |
| `ly` | Left stick Y (forward/back) | -1.0 to 1.0 |
| `rx` | Right stick X (yaw) | -1.0 to 1.0 |
| `ry` | Right stick Y (pitch) | -1.0 to 1.0 |
| `keys` | Button bitmask | integer |

---

## Video & Audio Channels

### Video (receive only)

```python
from aiortc import MediaStreamTrack

conn.video.switchVideoChannel(True)

async def on_track(track: MediaStreamTrack):
    while True:
        frame = await track.recv()
        img = frame.to_ndarray(format="bgr24")  # 1280x720 BGR numpy array

conn.video.add_track_callback(on_track)
```

### Audio (send and receive)

```python
# Enable audio channel
conn.datachannel.switchAudioChannel(True)

# Send audio (e.g., play MP3)
from aiortc.contrib.media import MediaPlayer
player = MediaPlayer("file.mp3")
conn.pc.addTrack(player.audio)
```

---

## Data Channel Controls

```python
# Enable/disable traffic saving (required before LiDAR streaming)
await conn.datachannel.disableTrafficSaving(True)

# Switch video on/off
conn.datachannel.switchVideoChannel(True)

# Switch audio on/off
conn.datachannel.switchAudioChannel(True)

# Set LiDAR decoder
conn.datachannel.set_decoder(decoder_type='native')   # raw point cloud
conn.datachannel.set_decoder(decoder_type='libvoxel')  # voxel mesh
```

---

## All RTC Topics Quick Reference

| Constant | Topic Path | Type |
|----------|-----------|------|
| LOW_STATE | rt/lf/lowstate | Subscribe |
| LF_SPORT_MOD_STATE | rt/lf/sportmodestate | Subscribe |
| SPORT_MOD_STATE | rt/sportmodestate | Subscribe |
| MULTIPLE_STATE | rt/multiplestate | Subscribe |
| ROBOTODOM | rt/utlidar/robot_pose | Subscribe |
| ULIDAR_STATE | rt/utlidar/lidar_state | Subscribe |
| ULIDAR | rt/utlidar/voxel_map | Subscribe |
| ULIDAR_ARRAY | rt/utlidar/voxel_map_compressed | Subscribe |
| ULIDAR_SWITCH | rt/utlidar/switch | Publish |
| UWB_STATE | rt/uwbstate | Subscribe |
| UWB_REQ | rt/api/uwbswitch/request | Request |
| WIRELESS_CONTROLLER | rt/wirelesscontroller | Publish |
| SPORT_MOD | rt/api/sport/request | Request |
| MOTION_SWITCHER | rt/api/motion_switcher/request | Request |
| VUI | rt/api/vui/request | Request |
| OBSTACLES_AVOID | rt/api/obstacles_avoid/request | Request |
| AUDIO_HUB_REQ | rt/api/audiohub/request | Request |
| AUDIO_HUB_PLAY_STATE | rt/audiohub/player/state | Subscribe |
| BASH_REQ | rt/api/bashrunner/request | Request |
| SELF_TEST | rt/selftest | Subscribe |
| GRID_MAP | rt/mapping/grid_map | Subscribe |
| SERVICE_STATE | rt/servicestate | Subscribe |
| GPT_FEEDBACK | rt/gptflowfeedback | Subscribe |
| LOW_CMD | rt/lowcmd | Publish |
| FRONT_PHOTO_REQ | rt/api/videohub/request | Request |
| GAS_SENSOR | rt/gas_sensor | Subscribe |
| GAS_SENSOR_REQ | rt/api/gas_sensor/request | Request |
| SLAM_QT_COMMAND | rt/qt_command | Publish |
| SLAM_ADD_NODE | rt/qt_add_node | Subscribe |
| SLAM_ADD_EDGE | rt/qt_add_edge | Subscribe |
| SLAM_QT_NOTICE | rt/qt_notice | Subscribe |
| SLAM_PC_TO_IMAGE_LOCAL | rt/pctoimage_local | Subscribe |
| SLAM_ODOMETRY | rt/lio_sam_ros2/mapping/odometry | Subscribe |
| ARM_COMMAND | rt/arm_Command | Publish |
| ARM_FEEDBACK | rt/arm_Feedback | Subscribe |
| LIDAR_MAPPING_CMD | rt/uslam/client_command | Request |
| LIDAR_MAPPING_CLOUD_POINT | rt/uslam/frontend/cloud_world_ds | Subscribe |
| LIDAR_MAPPING_ODOM | rt/uslam/frontend/odom | Subscribe |
| LIDAR_MAPPING_PCD_FILE | rt/uslam/cloud_map | Subscribe |
| LIDAR_MAPPING_SERVER_LOG | rt/uslam/server_log | Subscribe |
| LIDAR_LOCALIZATION_ODOM | rt/uslam/localization/odom | Subscribe |
| LIDAR_NAVIGATION_GLOBAL_PATH | rt/uslam/navigation/global_path | Subscribe |
| LIDAR_LOCALIZATION_CLOUD_POINT | rt/uslam/localization/cloud_world | Subscribe |
| PROGRAMMING_ACTUATOR_CMD | rt/programming_actuator/command | Publish |
| ASSISTANT_RECORDER | rt/api/assistant_recorder/request | Request |
