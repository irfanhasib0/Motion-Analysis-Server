import cv2
import sys
import time
from src.imgproc import optical_flow

VIDEO_PATH = sys.argv[1] if len(sys.argv) > 1 else 'test.mp4'

cap = cv2.VideoCapture(VIDEO_PATH)
if not cap.isOpened():
    print(f"Failed to open video: {VIDEO_PATH}")
    sys.exit(1)

paused = False
restart = False
frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
fps = cap.get(cv2.CAP_PROP_FPS) or 30

while True:
    if restart:
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        restart = False
    if not paused:
        ret, frame = cap.read()
        if not ret:
            break
        # Call your custom detect() function
        out = optical_flow.detect(frame)
        cv2.imshow('Optical Flow Detect', out if out is not None else frame)
    key = cv2.waitKey(30) & 0xFF
    if key == ord('q'):
        break
    elif key == ord(' '):  # Space to pause/resume
        paused = not paused
    elif key == ord('r'):  # R to restart
        restart = True
    elif key == ord('b'):  # B to go back 10 seconds
        pos = cap.get(cv2.CAP_PROP_POS_FRAMES)
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, pos - int(10 * fps)))

cap.release()
cv2.destroyAllWindows()
print("Done.")
