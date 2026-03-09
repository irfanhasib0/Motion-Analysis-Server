import subprocess
import numpy as np
import math
import sys

RTSP_URL = "rtsp://admin:L2D841A1@192.168.2.131:554/cam/realmonitor?channel=1&subtype=0&unicast=true&proto=Onvif"

SAMPLE_RATE = 16000
CHANNELS = 1
READ_BYTES = 4096


def looks_like_encoded_audio(b: bytes) -> bool:
    # Detect AAC ADTS / MP3 frame sync
    return len(b) >= 2 and b[0] == 0xFF and (b[1] & 0xF0) == 0xF0


cmd = [
    "ffmpeg",
    "-hide_banner",
    "-loglevel", "error",
    "-rtsp_transport", "tcp",
    "-i", RTSP_URL,

    "-map", "0:a:0",          # select first audio stream
    "-vn", "-sn", "-dn",

    "-c:a", "pcm_s16le",      # force decode to PCM
    "-ac", str(CHANNELS),
    "-ar", str(SAMPLE_RATE),

    "-f", "s16le",
    "pipe:1",
]

print("Starting FFmpeg...")

process = subprocess.Popen(
    cmd,
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
    bufsize=0
)

try:
    while True:

        chunk = process.stdout.read(READ_BYTES)
        if not chunk:
            print("Stream ended")
            break
        '''
        if looks_like_encoded_audio(chunk):
            print("ERROR: Received encoded audio frame instead of PCM")
            print("First bytes:", chunk[:16].hex())
            break
        '''
        usable = len(chunk) - (len(chunk) % 2)
        samples = np.frombuffer(chunk[:usable], dtype="<i2").astype(np.float32)
        samples /= 32768.0

        if samples.size == 0:
            continue

        rms = math.sqrt(np.mean(samples ** 2) + 1e-12)
        db = 20 * math.log10(rms)

        print(
            f"samples={samples.size:4d} "
            f"min={samples.min(): .3f} "
            f"max={samples.max(): .3f} "
            f"mean={samples.mean(): .4f} "
            f"loudness={db: .2f} dBFS"
        )

except KeyboardInterrupt:
    print("\nStopping...")

finally:
    process.kill()