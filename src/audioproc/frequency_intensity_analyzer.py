from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np


@dataclass
class AnalyzerConfig:
    sample_rate: int = 16000
    channels: int = 1
    epsilon: float = 1e-8
    baseline_alpha: float = 0.1
    intensity_spike_ratio: float = 2.5
    intensity_drop_ratio: float = 0.35
    band_spike_ratio: float = 3.0
    series_max_points: int = 180
    pcm_smoothing_window: int = 5  # moving-mean kernel size applied to decoded samples; 0 or 1 = disabled


class FrequencyIntensityAnalyzer:
    """Analyze incoming audio chunks and report average intensity per frequency band.

    Notes:
    - Expects chunk bytes that can be interpreted as 16-bit little-endian PCM.
    - Keeps rolling baselines to detect intensity/frequency anomalies.
    """

    def __init__(self, sample_rate: int = 16000, channels: int = 1):
        self.config = AnalyzerConfig(sample_rate=max(8000, int(sample_rate)), channels=max(1, int(channels)))
        self._leftover = b""
        self._overall_baseline = 1e-6
        self._band_baseline: Dict[str, float] = {}
        self._series_index = 0
        self._loudness_series: deque[Tuple[int, float]] = deque(maxlen=self.config.series_max_points)
        self._peak_frequency_mean_series: deque[Tuple[int, float]] = deque(maxlen=self.config.series_max_points)

    @staticmethod
    def _default_bands(sample_rate: int) -> List[Tuple[float, float]]:
        nyquist = max(1000.0, sample_rate / 2.0)
        candidate = [
            (0.0, 125.0),
            (125.0, 250.0),
            (250.0, 500.0),
            (500.0, 1000.0),
            (1000.0, 2000.0),
            (2000.0, 4000.0),
            (4000.0, 8000.0),
            (8000.0, 16000.0),
        ]
        bands = []
        for low, high in candidate:
            if low >= nyquist:
                break
            bands.append((low, min(high, nyquist)))
        if not bands:
            bands = [(0.0, nyquist)]
        return bands

    @staticmethod
    def _band_label(low: float, high: float) -> str:
        return f"{int(low)}-{int(high)}Hz"

    @staticmethod
    def _band_mean_frequency(label: str) -> float:
        text = str(label or '').replace('Hz', '')
        if '-' not in text:
            try:
                return float(text)
            except Exception:
                return 0.0
        left, right = text.split('-', 1)
        try:
            low = float(left)
            high = float(right)
            return 0.5 * (low + high)
        except Exception:
            return 0.0

    def _append_series(self, loudness: float, peak_frequency_mean: float):
        self._series_index += 1
        x = int(self._series_index)
        self._loudness_series.append((x, float(loudness)))
        self._peak_frequency_mean_series.append((x, float(peak_frequency_mean)))

    def _to_pcm16(self, chunk: bytes) -> np.ndarray:
        #data = self._leftover + (chunk or b"")
        data = chunk or b""
        if not data:
            return np.array([], dtype=np.float32)
        '''
        if data.startswith(b"RIFF") and b"data" in data[:128]:
            idx = data.find(b"data")
            if idx >= 0 and len(data) >= idx + 8:
                data = data[idx + 8 :]
        '''
        usable = len(data) - (len(data) % 2)
        self._leftover = data[usable:]
        if usable <= 0:
            return np.array([], dtype=np.float32)

        pcm = np.frombuffer(data[:usable], dtype="<i2").astype(np.float32)
        if self.config.channels > 1 and pcm.size >= self.config.channels:
            pcm = pcm.reshape(-1, self.config.channels).mean(axis=1)
        return pcm / 32768.0

    def _smooth_pcm(self, pcm: np.ndarray) -> np.ndarray:
        """Apply moving-mean smoothing for FFT input only. Does not affect RMS."""
        w = int(self.config.pcm_smoothing_window)
        if w > 1 and pcm.size >= w:
            kernel = np.ones(w, dtype=np.float32) / w
            return np.convolve(pcm, kernel, mode="same")
        return pcm

    def process_chunk(self, samples: np.ndarray) -> Dict[str, object]:
        #samples = self._to_pcm16(chunk)
        samples = self._smooth_pcm(samples)

        if samples.size == 0:
            self._append_series(0.0, 0.0)
            return {
                "sample_rate": self.config.sample_rate,
                "num_samples": 0,
                "average_intensity_per_frequency": {},
                "overall_intensity": 0.0,
                "peak_frequency_mean": 0.0,
                "loudness_series": [[x, y] for x, y in self._loudness_series],
                "peak_frequency_mean_series": [[x, y] for x, y in self._peak_frequency_mean_series],
                "anomaly": {
                    "intensity": False,
                    "frequency": False,
                    "reason": "empty_chunk",
                },
            }

        window = np.hanning(samples.size)
        fft_values = np.fft.rfft(samples * window)
        power = np.abs(fft_values) ** 2
        freqs = np.fft.rfftfreq(samples.size, d=1.0 / self.config.sample_rate)

        eps = self.config.epsilon
        overall_rms = float(np.sqrt(np.mean(np.square(samples)) + eps))
        self._overall_baseline = (
            (1.0 - self.config.baseline_alpha) * self._overall_baseline
            + self.config.baseline_alpha * overall_rms
        )

        avg_by_band: Dict[str, float] = {}
        frequency_spike = False
        top_band = None
        top_ratio = 1.0

        for low, high in self._default_bands(self.config.sample_rate):
            mask = (freqs >= low) & (freqs < high)
            if not np.any(mask):
                continue

            band_power = float(np.mean(power[mask]))
            band_intensity = float(10.0 * np.log10(band_power + eps))
            label = self._band_label(low, high)
            avg_by_band[label] = band_intensity

            prev = self._band_baseline.get(label, band_intensity)
            ratio = max((band_intensity + 120.0) / (prev + 120.0 + eps), 0.0)
            if ratio > top_ratio:
                top_ratio = ratio
                top_band = label
            if ratio >= self.config.band_spike_ratio:
                frequency_spike = True

            self._band_baseline[label] = (1.0 - self.config.baseline_alpha) * prev + self.config.baseline_alpha * band_intensity

        intensity_spike = overall_rms > (self._overall_baseline * self.config.intensity_spike_ratio)
        intensity_drop = overall_rms < (self._overall_baseline * self.config.intensity_drop_ratio)

        reason = "normal"
        if intensity_spike:
            reason = "intensity_spike"
        elif intensity_drop:
            reason = "intensity_drop"
        elif frequency_spike:
            reason = "frequency_spike"

        peak_frequency_mean = self._band_mean_frequency(top_band or "0-0Hz")
        self._append_series(overall_rms, peak_frequency_mean)

        return {
            "sample_rate": self.config.sample_rate,
            "num_samples": int(samples.size),
            "average_intensity_per_frequency": avg_by_band,
            "overall_intensity": overall_rms,
            "peak_frequency_mean": float(peak_frequency_mean),
            "loudness_series": [[x, y] for x, y in self._loudness_series],
            "peak_frequency_mean_series": [[x, y] for x, y in self._peak_frequency_mean_series],
            "anomaly": {
                "intensity": bool(intensity_spike or intensity_drop),
                "frequency": bool(frequency_spike),
                "reason": reason,
                "top_band": top_band,
                "top_ratio": float(top_ratio),
            },
        }
