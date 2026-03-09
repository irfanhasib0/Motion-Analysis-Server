import subprocess

# Adjust this depending on OS
# Linux (PulseAudio)
input_device = "default"
input_format = "pulse"

# macOS
# input_device = ":0"
# input_format = "avfoundation"

# Windows
# input_device = "audio=Microphone"
# input_format = "dshow"

cmd = [
    "ffmpeg",
    "-loglevel", "quiet",
    "-f", input_format,
    "-i", input_device,
    "-ac", "1",              # mono
    "-ar", "16000",          # sample rate
    "-f", "s16le",           # raw PCM 16-bit
    "-"
]

process = subprocess.Popen(
    cmd,
    stdout=subprocess.PIPE,
    bufsize=4096
)

print("Reading microphone audio bytes...")

try:
    while True:
        data = process.stdout.read(4096)
        if not data:
            break

        print(len(data), data[:20])  # print first 20 bytes

except KeyboardInterrupt:
    pass

process.terminate()