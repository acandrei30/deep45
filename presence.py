"""Periodic camera-based presence detection.

Instead of holding the camera open for the entire 45-minute sprint, this
opens the camera every CHECK_INTERVAL seconds, reads a handful of frames,
detects a face, and closes the camera. The LED blinks every interval —
less invasive than continuous, and reads multiple frames each check so a
single dropped frame doesn't false-fail.

All processing local. No images stored or transmitted."""

import threading
import time

import cv2


class PresenceMonitor:
    # Defaults — tuned so a walk-away forfeits within ~50s and a brief
    # look-away (≤10s) is never penalised.
    CHECK_INTERVAL = 15.0    # seconds between camera checks
    FRAMES_PER_CHECK = 6     # frames to grab per check (more frames = more reliable)
    MISS_THRESHOLD = 40.0    # absent_seconds before forfeit
    WARN_THRESHOLD = 25.0    # absent_seconds before red overlay

    def __init__(self, on_status,
                 miss_threshold=None, warn_threshold=None,
                 check_interval=None, frames_per_check=None):
        self.on_status = on_status
        self.miss_threshold = miss_threshold or self.MISS_THRESHOLD
        self.warn_threshold = warn_threshold or self.WARN_THRESHOLD
        self.check_interval = check_interval or self.CHECK_INTERVAL
        self.frames_per_check = frames_per_check or self.FRAMES_PER_CHECK

        self.frontal = None
        self.profile = None
        self._running = False
        self._thread = None
        self._absent_since = None
        self._first_check_ok = False  # only return True from start() once we
                                       # know the camera actually delivers frames

    def start(self):
        """Verify camera works once, then begin periodic monitoring in a thread.
        Returns True if the camera delivered at least one frame on the test."""
        # Load cascades (cheap, no camera needed).
        haar = cv2.data.haarcascades
        self.frontal = cv2.CascadeClassifier(haar + "haarcascade_frontalface_default.xml")
        self.profile = cv2.CascadeClassifier(haar + "haarcascade_profileface.xml")
        if self.frontal.empty():
            return False

        # Try to grab a single frame as a sanity check.
        cap = self._open_camera()
        if cap is None:
            return False
        ok = False
        for _ in range(8):
            ret, frame = cap.read()
            if ret and frame is not None and frame.size > 0:
                ok = True
                break
            time.sleep(0.15)
        try:
            cap.release()
        except Exception:
            pass
        if not ok:
            return False

        self._running = True
        self._absent_since = None
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    @staticmethod
    def _open_camera():
        """Open the default camera, trying backends in order. Returns the
        VideoCapture (caller releases) or None."""
        for backend in (cv2.CAP_DSHOW, 0, cv2.CAP_MSMF):
            try:
                cap = cv2.VideoCapture(0, backend) if backend else cv2.VideoCapture(0)
            except Exception:
                continue
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                return cap
            try:
                cap.release()
            except Exception:
                pass
        return None

    def _detect_face(self, gray):
        # Frontal first (most common).
        faces = self.frontal.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=3, minSize=(50, 50),
            flags=cv2.CASCADE_SCALE_IMAGE,
        )
        if len(faces) > 0:
            return True
        # Profile, then mirrored profile.
        if self.profile is not None and not self.profile.empty():
            faces = self.profile.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=3, minSize=(50, 50),
                flags=cv2.CASCADE_SCALE_IMAGE,
            )
            if len(faces) > 0:
                return True
            flipped = cv2.flip(gray, 1)
            faces = self.profile.detectMultiScale(
                flipped, scaleFactor=1.1, minNeighbors=3, minSize=(50, 50),
                flags=cv2.CASCADE_SCALE_IMAGE,
            )
            if len(faces) > 0:
                return True
        return False

    def _check_once(self):
        """Open the camera, grab N frames, look for a face in any of them.
        Releases camera before returning. Returns True if a face was found."""
        cap = self._open_camera()
        if cap is None:
            return None  # camera unavailable this round — don't penalize the user
        try:
            for _ in range(self.frames_per_check):
                ret, frame = cap.read()
                if not ret or frame is None:
                    time.sleep(0.1)
                    continue
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                gray = cv2.equalizeHist(gray)
                if self._detect_face(gray):
                    return True
                time.sleep(0.05)
            return False
        finally:
            try:
                cap.release()
            except Exception:
                pass

    def _loop(self):
        # First check happens after a short delay so the sprint UI renders first.
        time.sleep(2.0)
        while self._running:
            present = self._check_once()
            now = time.time()

            if present is True:
                self._absent_since = None
                absent = 0.0
            elif present is False:
                if self._absent_since is None:
                    self._absent_since = now
                absent = now - self._absent_since
            else:
                # Camera unavailable — skip this check, don't increment absence.
                time.sleep(self.check_interval)
                continue

            failed = absent >= self.miss_threshold
            try:
                self.on_status(absent_seconds=absent, failed=failed)
            except Exception:
                pass
            if failed:
                break

            # Sleep until next check, in 0.5s slices so stop() can break us out fast.
            slept = 0.0
            while self._running and slept < self.check_interval:
                time.sleep(0.5)
                slept += 0.5
