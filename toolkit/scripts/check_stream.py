#!/usr/bin/env python3
"""Проверка аудиопотока бейджа без железа.

WAV -> Opus с параметрами прошивки -> нарезка на BLE-уведомления -> потери ->
сборка на приёмнике -> декодирование (потерянные кадры через PLC) -> WAV.
Печатает отчёт JSON. Код выхода: 0 — PASS, 1 — FAIL, 2 — ошибка входных данных.
"""

import argparse
import json
import sys

import numpy as np
import opuslib
import opuslib.api.ctl as ctl
import opuslib.api.decoder as opus_dec
import opuslib.api.encoder as opus_enc
import soundfile as sf

import omi_protocol as omi


def read_wav(path: str) -> tuple[np.ndarray, int]:
    """Читает WAV 16 кГц моно, дополняет нулями до целого кадра. Возвращает (pcm, исходная длина)."""
    pcm, rate = sf.read(path, dtype="int16")
    if rate != omi.SAMPLE_RATE or pcm.ndim != 1:
        raise ValueError(f"нужен WAV {omi.SAMPLE_RATE} Гц моно, получено: {rate} Гц, "
                         f"каналов {1 if pcm.ndim == 1 else pcm.shape[1]}")
    if len(pcm) == 0:
        raise ValueError("входной WAV пуст")
    # Прошивка неполный кадр оставляет в буфере до прихода новых данных (codec.c:76);
    # для файла дополняем хвост тишиной.
    padding = -len(pcm) % omi.FRAME_SAMPLES
    return np.concatenate([pcm, np.zeros(padding, dtype=np.int16)]), len(pcm)


def encode(pcm: np.ndarray) -> list[bytes]:
    """Кодирует PCM в кадры Opus с настройками codec_start(), codec.c:96-106."""
    enc = opus_enc.create_state(omi.SAMPLE_RATE, omi.CHANNELS, opuslib.APPLICATION_RESTRICTED_LOWDELAY)
    for request, value in (
        (ctl.set_bitrate, omi.BITRATE),
        (ctl.set_vbr, omi.VBR),
        (ctl.set_vbr_constraint, omi.VBR_CONSTRAINT),
        (ctl.set_complexity, omi.COMPLEXITY),
        (ctl.set_signal, opuslib.SIGNAL_VOICE),
        (ctl.set_lsb_depth, omi.LSB_DEPTH),
        (ctl.set_dtx, omi.DTX),
        (ctl.set_inband_fec, omi.INBAND_FEC),
        (ctl.set_packet_loss_perc, omi.PACKET_LOSS_PERC),
    ):
        opus_enc.encoder_ctl(enc, request, value)

    frames = []
    for start in range(0, len(pcm), omi.FRAME_SAMPLES):
        chunk = pcm[start:start + omi.FRAME_SAMPLES].tobytes()
        # Выходной буфер прошивки — CODEC_OUTPUT_MAX_BYTES, codec.c:134-135
        frames.append(bytes(opus_enc.encode(enc, chunk, omi.FRAME_SAMPLES, omi.MAX_FRAME_BYTES)))
    opus_enc.destroy(enc)
    return frames


def packetize(frames: list[bytes], mtu: int, start_packet: int) -> tuple[list[bytes], list[int]]:
    """Режет кадры на уведомления как push_to_gatt(), transport.c:1066-1131.

    Возвращает уведомления и для каждого — номер кадра, к которому оно относится.
    """
    notifications, frame_of = [], []
    packet_next_index = start_packet
    for frame_no, frame in enumerate(frames):
        offset = 0
        index = 0  # transport.c:1070 — с нуля на каждый кадр
        for size in omi.fragment_sizes(len(frame), mtu):
            # transport.c:1082-1085 — номер растёт на каждое уведомление, uint16 LE
            header = packet_next_index.to_bytes(2, "little") + bytes([index])
            packet_next_index = (packet_next_index + 1) % omi.PACKET_NUMBER_MOD
            notifications.append(header + frame[offset:offset + size])
            frame_of.append(frame_no)
            offset += size
            index += 1  # transport.c:1089
    return notifications, frame_of


def drop(count: int, loss: float, seed: int) -> np.ndarray:
    """Маска выброшенных уведомлений. Первое и последнее не выбрасываются:
    потерю на краях потока приёмник по номерам увидеть не может."""
    dropped = np.random.default_rng(seed).random(count) < loss
    dropped[0] = dropped[-1] = False
    return dropped


def receive(notifications: list[bytes], mtu: int) -> tuple[list[bytes | None], int, int]:
    """Приёмник: видит только уведомления и MTU.

    Возвращает кадры (None — потерян), число пропущенных номеров
    и число пропусков, где количество потерянных кадров определить однозначно нельзя.
    """
    full = mtu - omi.HEADER_SIZE
    # Если максимальный кадр влезает в одно уведомление, каждое уведомление — целый кадр
    fragmentation_possible = full < omi.MAX_FRAME_BYTES

    frames: list[bytes | None] = []
    parts: list[bytes] | None = None  # куски текущего кадра; None — кадр испорчен
    started = False
    expected = None
    missing = ambiguous = 0

    def flush() -> None:
        if started:
            frames.append(b"".join(parts) if parts is not None else None)

    for data in notifications:
        number = int.from_bytes(data[:2], "little")
        index = data[2]
        payload = data[omi.HEADER_SIZE:]
        gap = 0 if expected is None else (number - expected) % omi.PACKET_NUMBER_MOD
        expected = (number + 1) % omi.PACKET_NUMBER_MOD
        missing += gap

        if gap == 0:
            if index == 0:
                flush()
                parts = [payload]
            elif parts is not None:
                parts.append(payload)
        elif gap < index:
            # Потеряны куски из середины того же кадра
            parts = None
        else:
            # index потерянных уведомлений — начало текущего кадра, остальные — до него
            before = gap - index
            if fragmentation_possible and before >= 2:
                ambiguous += 1
            # Кусок полного размера перед пропуском: считаем, что у кадра потеряно продолжение
            if before and fragmentation_possible and parts is not None and len(parts[-1]) == full:
                parts = None
                before -= 1
            flush()
            # Заголовок не говорит, сколько кусков было в кадре: считаем по кадру на уведомление
            frames.extend([None] * before)
            parts = [payload] if index == 0 else None
        started = True

    flush()
    return frames, missing, ambiguous


def decode(frames: list[bytes | None]) -> np.ndarray:
    """Декодирует кадры; для потерянных — маскировка потерь декодера (PLC)."""
    dec = opus_dec.create_state(omi.SAMPLE_RATE, omi.CHANNELS)
    pcm = bytearray()
    for frame in frames:
        # Пустой пакет (NULL, 0) включает PLC в opus_decode
        length = len(frame) if frame is not None else 0
        pcm += opus_dec.decode(dec, frame, length, omi.FRAME_SAMPLES, False, channels=omi.CHANNELS)
    opus_dec.destroy(dec)
    return np.frombuffer(bytes(pcm), dtype=np.int16)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="входной WAV, 16 кГц моно")
    parser.add_argument("output", help="выходной WAV после приёмника")
    parser.add_argument("--mtu", type=int, default=omi.CONFIG_TX_MTU,
                        help=f"ATT MTU, байт (по умолчанию {omi.CONFIG_TX_MTU}; "
                             f"фрагментация кадров — при {omi.MIN_MTU}..{omi.MAX_FRAME_BYTES + omi.HEADER_SIZE - 1})")
    parser.add_argument("--loss", type=float, default=0.0,
                        help="вероятность потери уведомления, 0..1 (по умолчанию 0)")
    parser.add_argument("--seed", type=int, default=0, help="seed генератора потерь (по умолчанию 0)")
    parser.add_argument("--max-frame-loss", type=float, default=0.05,
                        help="порог доли потерянных кадров для PASS (по умолчанию 0.05)")
    parser.add_argument("--start-packet", type=int, default=0,
                        help="начальный номер пакета 0..65535, например 65530 для проверки переполнения")
    parser.add_argument("--report", help="дополнительно записать отчёт JSON в файл")
    args = parser.parse_args()

    if args.mtu < omi.MIN_MTU:
        parser.error(f"MTU < {omi.MIN_MTU}: прошивка звук по BLE не отправляет (transport.c:1229)")
    if not 0 <= args.loss <= 1 or not 0 <= args.max_frame_loss <= 1:
        parser.error("--loss и --max-frame-loss должны быть в диапазоне 0..1")
    if not 0 <= args.start_packet < omi.PACKET_NUMBER_MOD:
        parser.error("--start-packet должен быть в диапазоне 0..65535")

    try:
        pcm, input_samples = read_wav(args.input)
    except (ValueError, sf.LibsndfileError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2

    frames = encode(pcm)
    notifications, frame_of = packetize(frames, args.mtu, args.start_packet)
    dropped = drop(len(notifications), args.loss, args.seed)
    received = [n for n, lost in zip(notifications, dropped) if not lost]
    out_frames, missing, ambiguous = receive(received, args.mtu)
    out_pcm = decode(out_frames)
    sf.write(args.output, out_pcm, omi.SAMPLE_RATE, subtype="PCM_16")

    frames_lost = len({frame_of[i] for i in np.flatnonzero(dropped)})
    frame_sizes = [len(f) for f in frames]
    seconds = len(pcm) / omi.SAMPLE_RATE
    frame_loss = frames_lost / len(frames)
    checks = {
        "gaps_match_dropped": missing == int(dropped.sum()),
        "duration_matches": len(out_pcm) == len(pcm),
        "frame_loss_within_limit": frame_loss <= args.max_frame_loss,
    }
    report = {
        "params": {"input": args.input, "output": args.output, "mtu": args.mtu, "loss": args.loss,
                   "seed": args.seed, "max_frame_loss": args.max_frame_loss,
                   "start_packet": args.start_packet},
        "notifications": {"sent": len(notifications), "dropped": int(dropped.sum()),
                          "gaps_found_by_receiver": missing, "ambiguous_gaps": ambiguous},
        "frames": {"total": len(frames), "lost": frames_lost,
                   "lost_by_receiver": sum(f is None for f in out_frames),
                   "loss_ratio": round(frame_loss, 6),
                   "avg_bytes": round(float(np.mean(frame_sizes)), 2), "max_bytes": max(frame_sizes)},
        "bitrate_bps": {"opus": round(sum(frame_sizes) * 8 / seconds),
                        "with_headers": round(sum(map(len, notifications)) * 8 / seconds)},
        "duration_s": {"input": input_samples / omi.SAMPLE_RATE, "input_padded": seconds,
                       "output": len(out_pcm) / omi.SAMPLE_RATE},
        "assumptions": [
            "Кодер — системный libopus, а не сборка прошивки (1.2.1, только CELT): "
            "размеры кадров близки, но не побитово те же.",
            "Хвост входа дополнен тишиной до целого кадра; прошивка неполный кадр не кодирует.",
            "Первое и последнее уведомление не выбрасываются: потерю на краях потока "
            "по номерам пакетов обнаружить нельзя.",
            "Потери независимые, с одинаковой вероятностью для каждого уведомления.",
            "Пропуск 65536 и более уведомлений подряд по uint16-счётчику не обнаруживается.",
            "При фрагментации (MTU 100..162) число потерянных кадров в пропуске из двух и более "
            "уведомлений по заголовку однозначно не определяется; приёмник считает кадр на уведомление.",
        ],
        "checks": checks,
        "verdict": "PASS" if all(checks.values()) else "FAIL",
    }

    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as file:
            file.write(text + "\n")
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
