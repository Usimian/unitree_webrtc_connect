# Robot Modifications Log

## Modified Files

### /unitree/module/chat_go/const.py
**Backup location on robot:** `/unitree/module/chat_go/const.py.backup`

**Changes made:**
| Setting | Original Value | New Value |
|---|---|---|
| `SSL_VERIFY` | `True` | `False` |
| `GPT_SERVER` | `'gpt-proxy.unitree.com'` | `'10.0.0.221'` |
| `GPT_PORT` | `'6080'` | `'8765'` |

**Purpose:** Redirects the robot's chat_go LLM requests from Unitree's cloud
server to a local WebSocket server running on the workstation (10.0.0.221:8765).

**To revert:**
```bash
ssh root@10.0.0.166
cp /unitree/module/chat_go/const.py.backup /unitree/module/chat_go/const.py
```

### /unitree/module/pet_go/const.py
**Backup location on robot:** `/unitree/module/pet_go/const.py.backup`

**Changes made:**
| Setting | Original Value | New Value |
|---|---|---|
| `GPT_SERVER` | `'gpt-proxy.unitree.com'` | `'10.0.0.221'` |
| `GPT_PORT` | `'6080'` | `'8765'` |

**Purpose:** Redirects pet_go (already running on robot) to local WebSocket server.

**To revert:**
```bash
ssh root@10.0.0.166
cp /unitree/module/pet_go/const.py.backup /unitree/module/pet_go/const.py
kill $(pgrep -f pet_go/service)
```

## No Other Files Modified
All other robot software is untouched. The robot's STT, TTS, motion control,
and all other functions remain exactly as shipped.
