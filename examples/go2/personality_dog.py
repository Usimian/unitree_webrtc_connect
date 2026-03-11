"""
personality_dog.py - Interactive LLM personality for Unitree Go2

Runs entirely on the workstation (no robot modification needed).
- Receives mic audio from robot via WebRTC
- STT via faster-whisper (local)
- LLM via Ollama (local)
- TTS via piper (local)
- Sends TTS audio back to robot speaker via WebRTC megaphone
- Triggers motion commands based on LLM response

Usage:
    python3 personality_dog.py

Requirements:
    pip install faster-whisper httpx pydub numpy aiortc
    pip install unitree-webrtc-connect  (or run from this repo)
    ollama pull mistral-small
    piper TTS installed with a voice model
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading

import httpx
import numpy as np
from scipy.signal import butter, sosfilt
from faster_whisper import WhisperModel
from unitree_webrtc_connect.webrtc_driver import UnitreeWebRTCConnection, WebRTCConnectionMethod
from unitree_webrtc_connect.webrtc_audiohub import WebRTCAudioHub
from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD

# ─────────────────────────────────────────────────
# CONFIGURATION — edit these before running
# ─────────────────────────────────────────────────

ROBOT_IP = "10.0.0.166"

OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "mistral-small:latest"

PIPER_BIN = "/home/marc/.local/bin/piper"
PIPER_VOICE = "/home/marc/.local/share/piper/voices/en_US-ryan-medium.onnx"

# Whisper model size: "tiny", "base", "small", "medium"
WHISPER_MODEL_SIZE = "base"

# Silence detection: seconds of quiet before treating audio as a complete utterance
SILENCE_THRESHOLD_SECONDS = 1.5
AUDIO_SAMPLERATE = 48000
AUDIO_CHANNELS = 2

# ─────────────────────────────────────────────────
# PERSONALITY PROMPT
# ─────────────────────────────────────────────────

SYSTEM_PROMPT = """You are Rex, an enthusiastic and loyal robot dog. You have a playful, curious
personality — like a real dog but with awareness that you're a robot. You get excited easily,
love compliments, and respond with short punchy sentences. You express emotions naturally.

When you want to perform a physical action, embed it in your response like this:
  <action>hello</action>
  <action>sit</action>
  <action>dance1</action>
  <action>dance2</action>
  <action>stretch</action>
  <action>wiggle</action>
  <action>stand</action>
  <action>finger_heart</action>

Rules:
- Keep responses SHORT — 1 to 3 sentences max.
- Use actions naturally and sparingly (max 1 per response).
- Do not explain what actions you are doing, just do them.
- Never break character.
- If asked to do something physically impossible for a dog, be funny about it."""

# ─────────────────────────────────────────────────
# SPORT COMMANDS MAPPING
# ─────────────────────────────────────────────────

ACTION_MAP = {
    "hello":        SPORT_CMD["Hello"],
    "sit":          SPORT_CMD["Sit"],
    "stand":        SPORT_CMD["StandUp"],
    "dance1":       SPORT_CMD["Dance1"],
    "dance2":       SPORT_CMD["Dance2"],
    "stretch":      SPORT_CMD["Stretch"],
    "wiggle":       SPORT_CMD["WiggleHips"],
    "finger_heart": SPORT_CMD["FingerHeart"],
}

# ─────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("personality_dog")

# ─────────────────────────────────────────────────
# STT — faster-whisper
# ─────────────────────────────────────────────────

class SpeechDetector:
    """
    Buffers incoming WebRTC audio frames (48kHz stereo int16 PCM).
    Records for a fixed window once speech is detected, then transcribes.
    """

    ENERGY_THRESHOLD = 1100      # RMS of filtered audio to consider as speech

    def __init__(self, model: WhisperModel, on_speech_callback):
        self.model = model
        self.on_speech = on_speech_callback
        self.chunks = []
        self.is_speaking = False
        self.silence_samples = 0
        self.speech_samples = 0
        self.silence_limit = int(SILENCE_THRESHOLD_SECONDS * AUDIO_SAMPLERATE)
        self.min_speech_samples = int(0.5 * AUDIO_SAMPLERATE)
        self._processing = False

    def feed_frame(self, frame):
        audio = np.frombuffer(frame.to_ndarray(), dtype=np.int16)
        mono = audio.reshape(-1, AUDIO_CHANNELS).mean(axis=1).astype(np.int16)

        rms_raw = float(np.sqrt(np.mean(mono.astype(np.float32) ** 2)))
        # Filter before RMS so threshold is applied to clean audio
        mono = _filter_audio(mono, AUDIO_SAMPLERATE)
        rms = float(np.sqrt(np.mean(mono.astype(np.float32) ** 2)))

        print(f"\r  [raw: {rms_raw:.0f} filtered: {rms:.0f}]  ", end="", flush=True)

        if rms > self.ENERGY_THRESHOLD:
            if not self.is_speaking:
                print(f"\n  [speech detected]")
            self.is_speaking = True
            self.silence_samples = 0
            self.speech_samples += len(mono)
            self.chunks.append(mono)

        elif self.is_speaking:
            self.silence_samples += len(mono)
            self.chunks.append(mono)

            if self.silence_samples >= self.silence_limit:
                if self.speech_samples >= self.min_speech_samples and not self._processing:
                    audio_data = np.concatenate(self.chunks)
                    self.chunks = []
                    self.is_speaking = False
                    self.speech_samples = 0
                    self.silence_samples = 0
                    self._processing = True
                    threading.Thread(
                        target=self._transcribe,
                        args=(audio_data,),
                        daemon=True
                    ).start()
                else:
                    self.chunks = []
                    self.is_speaking = False
                    self.speech_samples = 0
                    self.silence_samples = 0

    def _transcribe(self, audio_data: np.ndarray):
        print("  [transcribing...]")
        try:
            audio_16k = _resample(audio_data, AUDIO_SAMPLERATE, 16000)
            audio_float = audio_16k.astype(np.float32) / 32768.0

            segments, _ = self.model.transcribe(
                audio_float,
                language="en",
                beam_size=5,
                vad_filter=True,
            )
            text = " ".join(s.text for s in segments).strip()
            if text:
                print(f"\n  [heard: {text}]")
                asyncio.run_coroutine_threadsafe(
                    self.on_speech(text),
                    _event_loop
                )
        except Exception as e:
            logger.error(f"Transcription error: {e}")
        finally:
            self._processing = False


def _resample(audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    """Simple linear resampling."""
    ratio = target_sr / orig_sr
    new_len = int(len(audio) * ratio)
    indices = np.linspace(0, len(audio) - 1, new_len)
    return np.interp(indices, np.arange(len(audio)), audio).astype(np.int16)


def _filter_audio(audio: np.ndarray, samplerate: int) -> np.ndarray:
    """High-pass filter to remove low-frequency hum and rumble."""
    float_audio = audio.astype(np.float32) / 32768.0
    sos = butter(4, 100, btype='high', fs=samplerate, output='sos')
    filtered = sosfilt(sos, float_audio)
    return (filtered * 32768.0).astype(np.int16)


# ─────────────────────────────────────────────────
# TTS — piper
# ─────────────────────────────────────────────────

def synthesize_speech(text: str) -> str:
    """
    Run piper TTS, return path to a temporary WAV file.
    Caller is responsible for deleting the file.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()

    try:
        result = subprocess.run(
            [PIPER_BIN, "--model", PIPER_VOICE, "--output_file", tmp.name],
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"piper failed: {result.stderr.decode()}")
    except Exception as e:
        os.unlink(tmp.name)
        raise

    return tmp.name


# ─────────────────────────────────────────────────
# LLM — Ollama
# ─────────────────────────────────────────────────

async def chat(user_text: str, history: list) -> str:
    """Send message to Ollama, return assistant reply."""
    history.append({"role": "user", "content": user_text})
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(OLLAMA_URL, json={
            "model": OLLAMA_MODEL,
            "messages": messages,
            "stream": False,
        })
        resp.raise_for_status()

    reply = resp.json()["message"]["content"]
    history.append({"role": "assistant", "content": reply})
    return reply


# ─────────────────────────────────────────────────
# ACTION PARSER
# ─────────────────────────────────────────────────

def parse_response(reply: str):
    """
    Returns (spoken_text, list_of_action_names).
    Strips <action> tags from the spoken text.
    """
    actions = re.findall(r'<action>(.*?)</action>', reply, re.IGNORECASE)
    spoken = re.sub(r'<action>.*?</action>', '', reply, flags=re.IGNORECASE).strip()
    return spoken, actions


# ─────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────

_event_loop: asyncio.AbstractEventLoop = None
_conn: UnitreeWebRTCConnection = None
_audio_hub: WebRTCAudioHub = None
_busy = False  # Prevent overlapping responses


async def handle_speech(text: str):
    """Called from STT thread when a complete utterance is detected."""
    global _busy
    if _busy:
        logger.info("Busy, ignoring input.")
        return
    _busy = True

    print(f"\nYou: {text}")

    try:
        reply = await chat(text, conversation_history)
        spoken, actions = parse_response(reply)

        print(f"Rex: {spoken}")
        if actions:
            print(f"     [actions: {', '.join(actions)}]")

        # Execute motion commands first (non-blocking feel)
        for action_name in actions:
            api_id = ACTION_MAP.get(action_name.lower().strip())
            if api_id:
                await _conn.datachannel.pub_sub.publish_request_new(
                    RTC_TOPIC["SPORT_MOD"],
                    {"api_id": api_id}
                )
                await asyncio.sleep(0.3)
            else:
                logger.warning(f"Unknown action: {action_name}")

        # Synthesize and play speech
        if spoken:
            wav_path = synthesize_speech(spoken)
            try:
                await _audio_hub.enter_megaphone()
                await _audio_hub.upload_megaphone(wav_path)
                await _audio_hub.exit_megaphone()
            finally:
                os.unlink(wav_path)

    except Exception as e:
        logger.error(f"Error handling speech: {e}")
    finally:
        _busy = False


conversation_history = []


async def main():
    global _event_loop, _conn, _audio_hub

    _event_loop = asyncio.get_running_loop()

    # Load Whisper in a thread BEFORE connecting so it doesn't block the event loop
    print(f"Loading Whisper ({WHISPER_MODEL_SIZE})...")
    whisper_model = await _event_loop.run_in_executor(
        None, lambda: WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    )
    print("Whisper ready.")

    print(f"Connecting to robot at {ROBOT_IP}...")
    _conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=ROBOT_IP)
    await _conn.connect()
    print("Connected.")

    _audio_hub = WebRTCAudioHub(_conn, logger)

    # Set up STT pipeline on incoming audio
    detector = SpeechDetector(whisper_model, handle_speech)

    async def audio_callback(frame):
        detector.feed_frame(frame)

    _conn.audio.switchAudioChannel(True)
    _conn.audio.add_track_callback(audio_callback)

    print("\nRex is listening. Speak to the robot!\n")
    print("Press Ctrl+C to quit.\n")

    try:
        await asyncio.sleep(86400)  # Run for up to 24 hours
    except asyncio.CancelledError:
        pass
    finally:
        await _conn.disconnect()
        print("\nGoodbye!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")
        sys.exit(0)
