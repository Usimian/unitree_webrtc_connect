"""Quick test: headset mic -> Whisper STT -> Ollama LLM. No robot needed."""

import numpy as np, pyaudio, threading, httpx, json
from faster_whisper import WhisperModel

MIC_DEVICE_INDEX = 4
SR = 48000; WR = 16000; CHUNK = 4800; THRESH = 300
SIL = 1.5; MIN = 0.5; RATIO = WR / SR

print("Loading Whisper...")
model = WhisperModel("base", device="cpu", compute_type="int8")
print("Ready. Speak into your headset! Ctrl+C to quit.\n")

class D:
    def __init__(self): self.chunks=[]; self.speaking=False; self.sil=0; self.speech=0; self._p=False
    def feed(self, a):
        rms = float(np.sqrt(np.mean(a.astype(np.float32)**2)))
        if rms > THRESH:
            if not self.speaking: print("\n  [listening...]")
            self.speaking=True; self.sil=0; self.speech+=len(a); self.chunks.append(a.copy())
        elif self.speaking:
            self.sil += len(a); self.chunks.append(a.copy())
            if self.sil >= int(SIL*SR):
                if self.speech >= int(MIN*SR) and not self._p:
                    d=np.concatenate(self.chunks); self.chunks=[]; self.speaking=False; self.speech=0; self.sil=0; self._p=True
                    threading.Thread(target=self.tx, args=(d,), daemon=True).start()
                else:
                    self.chunks=[]; self.speaking=False; self.speech=0; self.sil=0
        else:
            print(f"\r  [RMS: {rms:.0f}]  ", end="", flush=True)

    def tx(self, d):
        try:
            print("  [transcribing...]")
            n = int(len(d)*RATIO); idx = np.linspace(0, len(d)-1, n)
            a16 = np.interp(idx, np.arange(len(d)), d).astype(np.int16)
            segs, _ = model.transcribe(a16.astype(np.float32)/32768.0, language="en", beam_size=5, vad_filter=True)
            text = " ".join(s.text for s in segs).strip()
            if text:
                print(f"\nYou: {text}\nRex: ", end="", flush=True)
                with httpx.Client(timeout=30) as c:
                    with c.stream("POST", "http://localhost:11434/api/chat", json={
                        "model": "mistral-small:latest",
                        "messages": [
                            {"role": "system", "content": "You are Rex, an enthusiastic robot dog. 2-3 sentences max."},
                            {"role": "user", "content": text}
                        ], "stream": True
                    }) as r:
                        for line in r.iter_lines():
                            if line:
                                tok = json.loads(line).get("message", {}).get("content", "")
                                if tok: print(tok, end="", flush=True)
                print("\n")
            else:
                print("  [no speech]")
        except Exception as e:
            print(f"Error: {e}")
        finally:
            self._p = False

d = D()
pa = pyaudio.PyAudio()
s = pa.open(format=pyaudio.paInt16, channels=1, rate=SR, input=True,
            input_device_index=MIC_DEVICE_INDEX, frames_per_buffer=CHUNK)
try:
    while True:
        d.feed(np.frombuffer(s.read(CHUNK, exception_on_overflow=False), dtype=np.int16))
except KeyboardInterrupt:
    print("\nDone.")
finally:
    s.stop_stream(); s.close(); pa.terminate()
