"""
rex_client.py - Listens on workstation mic, transcribes speech with Whisper,
sends text to local Ollama LLM, synthesizes speech with Piper TTS on the
workstation, and plays it through the robot's speaker via the Megaphone API.

No rex_server.py needed. No internet required.

Usage:
    python3 examples/go2/rex_client.py
"""

import asyncio
import io
import json
import logging
import re
import sys
import tempfile
import threading
import wave
import numpy as np
import pyaudio
import httpx
from faster_whisper import WhisperModel
from unitree_webrtc_connect.webrtc_driver import UnitreeWebRTCConnection, WebRTCConnectionMethod
from unitree_webrtc_connect.webrtc_audiohub import WebRTCAudioHub
from unitree_webrtc_connect.constants import RTC_TOPIC

# ─────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────

ROBOT_IP         = "10.0.0.166"
WHISPER_MODEL    = "base"
MIC_DEVICE_INDEX = None       # Auto-detect A50 headset
SAMPLERATE       = 48000      # A50 native rate
WHISPER_RATE     = 16000      # Whisper native rate
CHANNELS         = 1
CHUNK_SAMPLES    = 4800       # 100ms chunks at 48kHz
ENERGY_THRESHOLD = 300        # RMS to detect speech
SILENCE_SECONDS  = 1.5

OLLAMA_URL   = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "mistral-small:latest"
PIPER_MODEL  = "/home/marc/.local/share/piper/voices/en_US-ryan-medium.onnx"

SYSTEM_PROMPT = """You are Rex, a robot dog. Playful, loyal, excitable. You know you are a robot but act like a dog.

Rules:
- MAX 1 sentence. Never more.
- Never break character. You are always Rex the dog.
- Plain text only. No asterisks, no markdown, no lists, no special characters.
- No self-references like "As Rex" or "As a robot dog". Just speak naturally."""

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("rex_client")

_event_loop = None
_conn = None
_audiohub = None
_busy = False

# ─────────────────────────────────────────────────
# MOTION COMMANDS
# Keywords → sport API id
# ─────────────────────────────────────────────────

MOTION_COMMANDS = [
    (["stand up", "standup", "get up"],         1004),  # StandUp
    (["lie down", "lay down", "stand down"],    1005),  # StandDown
    (["sit", "sit down"],                       1009),  # Sit
    (["hello", "wave", "hi", "shake", "shake hands"], 1016),  # Hello / ShakeHands
    (["stretch"],                               1017),  # Stretch
    (["dance"],                                 1022),  # Dance1
    (["wallow", "roll over", "roll"],           1021),  # Wallow
    (["jump"],                                  1031),  # FrontJump
    (["pose"],                                  1028),  # Pose
    (["wiggle", "wiggle hips"],                1033),  # WiggleHips
    (["finger heart", "heart"],                1036),  # FingerHeart
    (["moon walk", "moonwalk"],                1305),  # MoonWalk
    (["handstand"],                            1301),  # Handstand
    (["front flip", "flip"],                   1030),  # FrontFlip
]


async def do_motion(api_id: int):
    # Switch to normal mode
    await _conn.datachannel.pub_sub.publish_request_new(
        RTC_TOPIC["MOTION_SWITCHER"],
        {"api_id": 1002, "parameter": {"name": "normal"}}
    )
    await asyncio.sleep(2)
    # Send motion command
    await _conn.datachannel.pub_sub.publish_request_new(
        RTC_TOPIC["SPORT_MOD"],
        {"api_id": api_id}
    )


def detect_motion(text: str):
    """Return sport API id if text matches a motion command, else None."""
    lower = text.lower()
    for keywords, api_id in MOTION_COMMANDS:
        for kw in keywords:
            if kw in lower:
                return api_id
    return None


# ─────────────────────────────────────────────────
# LLM + TTS + PLAY
# ─────────────────────────────────────────────────

async def speak(text: str):
    """Send text to Ollama, synthesize with Piper, play on robot speaker."""
    global _busy
    if _busy:
        print("  [busy, ignoring]")
        return
    _busy = True
    print(f"\nYou: {text}")
    try:
        # Check for motion commands first
        motion_id = detect_motion(text)
        if motion_id is not None:
            print(f"  [motion: {motion_id}]")
            await do_motion(motion_id)

        # 1. Get LLM response
        reply = ""
        async with httpx.AsyncClient(timeout=30) as client:
            async with client.stream("POST", OLLAMA_URL, json={
                "model": OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                "stream": True,
            }) as resp:
                print("Rex: ", end="", flush=True)
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        token = json.loads(line).get("message", {}).get("content", "")
                        if token:
                            reply += token
                            print(token, end="", flush=True)
                    except Exception:
                        pass
        print()

        if not reply:
            return

        # Strip markdown characters that TTS would speak aloud
        reply = re.sub(r'[*_`#]', '', reply).strip()

        # 2. Synthesize with Piper → WAV bytes
        proc = await asyncio.create_subprocess_exec(
            "piper", "-m", PIPER_MODEL, "--output-raw",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        raw_audio, _ = await proc.communicate(input=reply.encode())

        # Wrap raw PCM (16kHz, 16-bit mono) in a WAV container
        wav_buf = io.BytesIO()
        with wave.open(wav_buf, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)        # 16-bit
            wf.setframerate(22050)    # piper default
            wf.writeframes(raw_audio)
        wav_bytes = wav_buf.getvalue()

        # 3. Write WAV to temp file and send via Megaphone API
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(wav_bytes)
            tmp_path = tmp.name

        await _audiohub.enter_megaphone()
        await _audiohub.upload_megaphone(tmp_path)
        await asyncio.sleep(len(raw_audio) / (22050 * 2) + 0.5)  # wait for playback
        await _audiohub.exit_megaphone()

    except Exception as e:
        print(f"\n  [ERROR: {e}]")
    finally:
        _busy = False

# ─────────────────────────────────────────────────
# SPEECH DETECTOR
# ─────────────────────────────────────────────────

class SpeechDetector:
    def __init__(self, model: WhisperModel):
        self.model = model
        self.chunks = []
        self.speaking = False
        self.sil = 0
        self.silence_limit = int(SILENCE_SECONDS * SAMPLERATE)
        self._resample_ratio = WHISPER_RATE / SAMPLERATE
        self._processing = False

    def feed_chunk(self, audio: np.ndarray):
        rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))

        if rms > ENERGY_THRESHOLD:
            if not self.speaking:
                print("\n  [listening...]")
            self.speaking = True
            self.sil = 0
            self.chunks.append(audio.copy())

        elif self.speaking:
            self.sil += len(audio)
            self.chunks.append(audio.copy())
            if self.sil >= self.silence_limit:
                if not self._processing:
                    audio_data = np.concatenate(self.chunks)
                    self.chunks = []
                    self.speaking = False
                    self.sil = 0
                    self._processing = True
                    threading.Thread(
                        target=self._transcribe,
                        args=(audio_data,),
                        daemon=True
                    ).start()
                else:
                    self.chunks = []
                    self.speaking = False
                    self.sil = 0
        else:
            pass

    def _transcribe(self, audio_data: np.ndarray):
        print("  [transcribing...]")
        try:
            new_len = int(len(audio_data) * self._resample_ratio)
            indices = np.linspace(0, len(audio_data) - 1, new_len)
            audio_16k = np.interp(indices, np.arange(len(audio_data)), audio_data).astype(np.int16)
            audio_float = audio_16k.astype(np.float32) / 32768.0
            segments, info = self.model.transcribe(
                audio_float,
                language="en",
                beam_size=5,
            )
            segments = list(segments)
            text = " ".join(s.text for s in segments).strip()
            if text:
                asyncio.run_coroutine_threadsafe(speak(text), _event_loop)
            else:
                print("  [no speech detected]")
        except Exception as e:
            logger.error(f"Transcription error: {e}")
        finally:
            self._processing = False

# ─────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────

async def main():
    global _event_loop, _conn, _audiohub

    _event_loop = asyncio.get_running_loop()

    print(f"Loading Whisper ({WHISPER_MODEL})...")
    whisper_model = await _event_loop.run_in_executor(
        None, lambda: WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    )
    print("Whisper ready.")

    print(f"Connecting to robot at {ROBOT_IP}...")
    _conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=ROBOT_IP)
    await _conn.connect()
    _audiohub = WebRTCAudioHub(_conn)

    # Set robot volume to 50%
    await _conn.datachannel.pub_sub.publish_request_new(
        "rt/api/vui/request",
        {"api_id": 1003, "parameter": json.dumps({"volume": 2})}
    )

    print("Connected.\n")

    detector = SpeechDetector(whisper_model)

    pa = pyaudio.PyAudio()
    mic_index = MIC_DEVICE_INDEX
    if mic_index is None:
        for i in range(pa.get_device_count()):
            d = pa.get_device_info_by_index(i)
            if d['maxInputChannels'] > 0 and 'A50' in d['name']:
                mic_index = i
                break
        if mic_index is None:
            raise RuntimeError("A50 headset mic not found")
        print(f"Found A50 mic at device index {mic_index}")
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=CHANNELS,
        rate=SAMPLERATE,
        input=True,
        input_device_index=mic_index,
        frames_per_buffer=CHUNK_SAMPLES,
    )

    print("Rex is listening. Speak into your headset mic!\n")
    print("Press Ctrl+C to quit.\n")

    def mic_reader():
        while True:
            try:
                data = stream.read(CHUNK_SAMPLES, exception_on_overflow=False)
                audio = np.frombuffer(data, dtype=np.int16)
                detector.feed_chunk(audio)
            except Exception as e:
                logger.error(f"Mic read error: {e}")
                break

    mic_thread = threading.Thread(target=mic_reader, daemon=True)
    mic_thread.start()

    try:
        await asyncio.sleep(86400)
    except asyncio.CancelledError:
        pass
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()
        await _conn.disconnect()
        print("\nGoodbye!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)
