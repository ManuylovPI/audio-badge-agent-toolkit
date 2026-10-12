#!/usr/bin/env python3
"""Генерация тестового WAV в формате микрофона бейджа: 16 кГц, моно, 16 бит."""

import argparse

import numpy as np
import soundfile as sf

from omi_protocol import SAMPLE_RATE


def sine(n: int, freq: float) -> np.ndarray:
    return np.sin(2 * np.pi * freq * np.arange(n) / SAMPLE_RATE)


def speech_like(n: int, rng: np.random.Generator) -> np.ndarray:
    """Шум в речевой полосе с огибающей: слоги ~4 Гц и случайные паузы между фразами."""
    # Полоса 300–3400 Гц со спадом к высоким частотам
    spectrum = np.fft.rfft(rng.standard_normal(n))
    freqs = np.fft.rfftfreq(n, 1 / SAMPLE_RATE)
    spectrum[(freqs < 300) | (freqs > 3400)] = 0
    spectrum /= np.maximum(freqs, 300) / 300
    noise = np.fft.irfft(spectrum, n)

    t = np.arange(n) / SAMPLE_RATE
    syllables = 0.5 * (1 - np.cos(2 * np.pi * 4 * t + rng.uniform(0, 2 * np.pi)))

    # Чередование фраз (0,5–2 с) и пауз (0,2–0,6 с)
    phrases = np.zeros(n)
    pos = 0
    while pos < n:
        phrase = int(rng.uniform(0.5, 2.0) * SAMPLE_RATE)
        phrases[pos:pos + phrase] = 1
        pos += phrase + int(rng.uniform(0.2, 0.6) * SAMPLE_RATE)
    # Сглаживание границ фраз окном 20 мс, чтобы не было щелчков
    window = np.ones(SAMPLE_RATE // 50) / (SAMPLE_RATE // 50)
    phrases = np.convolve(phrases, window, mode="same")

    return noise * syllables * phrases


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("type", choices=["sine", "speech", "silence"],
                        help="sine — синус, speech — шум с речевой огибающей, silence — тишина")
    parser.add_argument("--duration", type=float, required=True, help="длительность, с")
    parser.add_argument("--out", required=True, help="путь к выходному WAV")
    parser.add_argument("--freq", type=float, default=440.0, help="частота синуса, Гц (по умолчанию 440)")
    parser.add_argument("--amplitude", type=float, default=0.5,
                        help="пиковая амплитуда 0..1 (по умолчанию 0.5)")
    parser.add_argument("--seed", type=int, default=0, help="seed для speech (по умолчанию 0)")
    args = parser.parse_args()

    if args.duration <= 0:
        parser.error("--duration должна быть больше 0")
    if not 0 <= args.amplitude <= 1:
        parser.error("--amplitude должна быть в диапазоне 0..1")

    n = round(args.duration * SAMPLE_RATE)
    if args.type == "sine":
        signal = sine(n, args.freq)
    elif args.type == "speech":
        signal = speech_like(n, np.random.default_rng(args.seed))
    else:
        signal = np.zeros(n)

    peak = np.max(np.abs(signal))
    if peak > 0:
        signal = signal / peak * args.amplitude

    sf.write(args.out, signal, SAMPLE_RATE, subtype="PCM_16")
    print(f"{args.out}: {args.type}, {n / SAMPLE_RATE:.3f} с, {SAMPLE_RATE} Гц, моно, 16 бит")


if __name__ == "__main__":
    main()
