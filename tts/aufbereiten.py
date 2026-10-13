"""Stimmprobe aufbereiten, bevor ein Modell die Stimme daraus klont.

Handyaufnahmen haben Rauschen (Lüfter, Straße, Raum), Brummen und Stille am Anfang und Ende. Qwen3-TTS übernimmt
im Vorlage-Modus nicht nur die Stimme, sondern auch diese Aufnahmebedingungen: dann rauscht jeder Satz und klingt
„wie eine Aufnahme“. Hier, nur mit numpy:

1. Mono, Gleichanteil weg, unter 70 Hz weg (Brummen, Trittschall)
2. Rauschen abziehen (Spektral-Gate mit Rauschprofil aus den leisesten Stellen, weicher Boden gegen Zwitschern)
3. Stille am Anfang und Ende kürzen
4. Lautstärke angleichen (etwa -20 dBFS, Spitzen höchstens -1 dBFS)

Abschalten: TTS_STIMME_AUFBEREITEN=aus.
"""

from __future__ import annotations

import io
import wave

import numpy as np

N_FFT, HOP = 1024, 256


def wav_lesen(daten: bytes) -> tuple[np.ndarray, int]:
    """16-Bit-PCM-WAV (wie die App es schickt) als float32 mono."""
    with wave.open(io.BytesIO(daten)) as w:
        kanaele, breite, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
        roh = w.readframes(w.getnframes())
    if breite != 2:
        raise ValueError("nur 16-Bit-WAV")
    audio = np.frombuffer(roh, dtype="<i2").astype(np.float32) / 32768.0
    return audio.reshape(-1, kanaele).mean(axis=1), sr


def _fenster() -> np.ndarray:
    return np.hanning(N_FFT + 1)[:-1].astype(np.float32)


def _stft(x: np.ndarray) -> np.ndarray:
    x = np.pad(x, (N_FFT // 2, N_FFT // 2 + HOP))
    n = 1 + (len(x) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n)[:, None]
    return np.fft.rfft(x[idx] * _fenster(), axis=1)


def _istft(spek: np.ndarray, laenge: int) -> np.ndarray:
    rahmen = np.fft.irfft(spek, n=N_FFT, axis=1) * _fenster()
    gesamt = N_FFT + HOP * (len(rahmen) - 1)
    aus, norm = np.zeros(gesamt, np.float32), np.zeros(gesamt, np.float32)
    w2 = _fenster() ** 2
    for i, r in enumerate(rahmen):
        aus[i * HOP:i * HOP + N_FFT] += r
        norm[i * HOP:i * HOP + N_FFT] += w2
    aus /= np.maximum(norm, 1e-8)
    return aus[N_FFT // 2:N_FFT // 2 + laenge]


def entrauschen(audio: np.ndarray, sr: int, staerke: float = 1.5, boden: float = 0.08) -> np.ndarray:
    spek = _stft(audio)
    betrag = np.abs(spek)
    frequenzen = np.fft.rfftfreq(N_FFT, 1 / sr)
    # Rauschprofil: die leisesten 15 % der Rahmen (Pausen zwischen den Wörtern)
    energie = (betrag ** 2).sum(axis=1)
    leise = betrag[energie <= np.quantile(energie, 0.15)]
    rauschen = np.median(leise, axis=0) if len(leise) else np.zeros(betrag.shape[1])
    # Wiener-artige Maske, über Zeit und Frequenz geglättet, damit kein „Zwitschern“ entsteht
    maske = np.clip(1.0 - (staerke * rauschen / np.maximum(betrag, 1e-8)) ** 2, 0.0, 1.0)
    kern = np.ones(3) / 3
    maske = np.apply_along_axis(lambda z: np.convolve(z, kern, mode="same"), 0, maske)
    maske = np.apply_along_axis(lambda z: np.convolve(z, kern, mode="same"), 1, maske)
    maske = boden + (1 - boden) * maske
    maske[:, frequenzen < 70] = 0.0  # Gleichanteil und Brummen
    return _istft(spek * maske, len(audio)).astype(np.float32)


def stille_kuerzen(audio: np.ndarray, sr: int, rand: float = 0.15) -> np.ndarray:
    block = max(1, int(0.02 * sr))
    n = len(audio) // block
    if n == 0:
        return audio
    pegel = np.sqrt((audio[:n * block].reshape(n, block) ** 2).mean(axis=1))
    laut = np.nonzero(pegel > max(pegel.max() * 0.05, 1e-4))[0]
    if not len(laut):
        return audio
    anfang = max(0, laut[0] * block - int(rand * sr))
    ende = min(len(audio), (laut[-1] + 1) * block + int(rand * sr))
    return audio[anfang:ende]


def lautstaerke(audio: np.ndarray, ziel_db: float = -20.0, spitze: float = 0.89) -> np.ndarray:
    rms = float(np.sqrt(np.mean(audio ** 2))) if len(audio) else 0.0
    if rms < 1e-6:
        return audio
    audio = audio * (10 ** (ziel_db / 20) / rms)
    hoch = float(np.abs(audio).max())
    return audio * (spitze / hoch) if hoch > spitze else audio


def aufbereiten(daten: bytes) -> tuple[np.ndarray, int]:
    audio, sr = wav_lesen(daten)
    audio = audio - audio.mean()
    audio = entrauschen(audio, sr)
    audio = stille_kuerzen(audio, sr)
    return lautstaerke(audio).astype(np.float32), sr
