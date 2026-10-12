#!/usr/bin/env python3
"""Оценка: хватает ли пропускной способности BLE для аудиопотока бейджа.

Сравнивает поток Opus с заголовками уведомлений и оценку пропускной способности
соединения при заданных MTU, интервале соединения и числе пакетов за интервал.
Код выхода: 0 — хватает, 1 — не хватает (по худшему размеру кадра).
"""

import argparse
import math

import omi_protocol as omi

ATT_HEADER = 3    # упомянут в документе (раздел 3), transport.c:504-506
L2CAP_HEADER = 4  # из спецификации BLE, в документе его нет


def frame_load(frame_bytes: int, mtu: int, ll_payload: int) -> tuple[int, int, int]:
    """Для одного кадра: число уведомлений, канальных пакетов и байт в эфире."""
    notifications = ll_packets = air_bytes = 0
    for payload in omi.fragment_sizes(frame_bytes, mtu):
        pdu = payload + omi.HEADER_SIZE + ATT_HEADER + L2CAP_HEADER
        notifications += 1
        ll_packets += math.ceil(pdu / ll_payload)
        air_bytes += pdu
    return notifications, ll_packets, air_bytes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mtu", type=int, required=True, help="согласованный ATT MTU, байт")
    parser.add_argument("--packets-per-interval", type=int, required=True,
                        help="сколько канальных пакетов проходит за один интервал соединения")
    parser.add_argument("--interval-ms", type=float, default=omi.CONN_INTERVAL_MAX_MS,
                        help=f"интервал соединения, мс (по умолчанию {omi.CONN_INTERVAL_MAX_MS} — "
                             "верхняя граница запроса прошивки)")
    parser.add_argument("--frame-bytes", type=int, default=omi.AVG_FRAME_BYTES,
                        help=f"средний размер кадра Opus, байт (по умолчанию {omi.AVG_FRAME_BYTES}, расчётный)")
    parser.add_argument("--ll-payload", type=int, default=omi.LL_DATA_LENGTH_MAX,
                        help=f"нагрузка канального пакета, байт (по умолчанию {omi.LL_DATA_LENGTH_MAX})")
    parser.add_argument("--buffer-bytes", type=int,
                        help="размер буфера/хранилища, байт: посчитать, сколько минут звука влезет")
    args = parser.parse_args()

    if args.mtu < omi.MIN_MTU:
        parser.error(f"MTU < {omi.MIN_MTU}: прошивка звук по BLE не отправляет (transport.c:1229)")
    if not 1 <= args.frame_bytes <= omi.MAX_FRAME_BYTES:
        parser.error(f"--frame-bytes должен быть в диапазоне 1..{omi.MAX_FRAME_BYTES}")
    if args.packets_per_interval < 1 or args.interval_ms <= 0 or args.ll_payload < 1:
        parser.error("--packets-per-interval, --interval-ms и --ll-payload должны быть больше 0")

    fps = omi.FRAMES_PER_SECOND
    capacity_packets = args.packets_per_interval * 1000 / args.interval_ms
    capacity_bps = capacity_packets * args.ll_payload * 8

    print("Параметры")
    print(f"  кодек:        Opus {omi.BITRATE} бит/с, кадр {omi.FRAME_MS} мс, {fps} кадров/с")
    print(f"  MTU:          {args.mtu} (нагрузка уведомления до {args.mtu - omi.HEADER_SIZE} байт)")
    print(f"  соединение:   интервал {args.interval_ms:g} мс, {args.packets_per_interval} пакет(ов) за интервал, "
          f"до {args.ll_payload} байт в пакете")
    print(f"  пропускная способность: {capacity_packets:.1f} пакетов/с, {capacity_bps / 1000:.1f} кбит/с")
    print()
    print("Нагрузка")
    utilization = {}
    for label, frame_bytes in (("средний", args.frame_bytes), ("худший", omi.MAX_FRAME_BYTES)):
        notifications, ll_packets, air_bytes = frame_load(frame_bytes, args.mtu, args.ll_payload)
        utilization[label] = fps * ll_packets / capacity_packets
        print(f"  {label} кадр {frame_bytes} байт: Opus {fps * frame_bytes * 8 / 1000:.1f} кбит/с, "
              f"с заголовками {fps * air_bytes * 8 / 1000:.1f} кбит/с")
        print(f"    {notifications} уведомл./кадр, {fps * ll_packets} пакетов/с, "
              f"загрузка {utilization[label]:.0%}")

    if args.buffer_bytes is not None:
        print()
        print(f"Буфер {args.buffer_bytes} байт (запись: {omi.SD_LENGTH_PREFIX} байт длины + кадр)")
        for label, frame_bytes in (("средний", args.frame_bytes), ("худший", omi.MAX_FRAME_BYTES)):
            seconds = args.buffer_bytes / ((frame_bytes + omi.SD_LENGTH_PREFIX) * fps)
            print(f"  {label} кадр {frame_bytes} байт: {seconds / 60:.1f} мин")
        print(f"  справочно: очередь прошивки перед BLE — {omi.TX_QUEUE_FRAMES} кадра = "
              f"{omi.TX_QUEUE_FRAMES * omi.FRAME_MS} мс")

    print()
    print("Допущения модели")
    assumptions = [
        f"Средний размер кадра {omi.AVG_FRAME_BYTES} байт — расчёт (32 кбит/с × 20 мс), VBR не ограничен; "
        f"худший случай — потолок {omi.MAX_FRAME_BYTES} байт на кадр.",
        "Кадр режется на уведомления по формуле прошивки min(MTU − 3, остаток); "
        "склейки кадров в одно уведомление нет.",
        f"На уведомление: {omi.HEADER_SIZE} байта заголовка приложения + {ATT_HEADER} ATT + {L2CAP_HEADER} L2CAP. "
        "Размер L2CAP взят из спецификации BLE, в документе по протоколу его нет.",
        "Уведомление занимает ceil(длина / нагрузка канального пакета) пакетов; "
        "заголовки канального уровня, преамбула и межпакетные паузы не считаются — "
        "они должны быть учтены в числе пакетов за интервал.",
        "Интервал соединения и число пакетов за интервал задаёт телефон; прошивка только просит 7,5–15 мс.",
        "Нет повторных передач, помех и другого трафика (батарея, синхронизация SD).",
        "Поток равномерный: 50 кадров/с без пауз (DTX в прошивке выключен).",
        "Оценка буфера не учитывает неполное заполнение блоков записи по "
        f"{omi.SD_BLOCK_BYTES} байт и служебные данные файловой системы.",
    ]
    if omi.MAX_FRAME_BYTES > args.mtu - omi.HEADER_SIZE:
        assumptions.append(
            f"При MTU {args.mtu} кадры длиннее {args.mtu - omi.HEADER_SIZE} байт фрагментируются. "
            "Полный фрагмент по формуле прошивки имеет длину MTU, что больше допустимой нагрузки ATT (MTU − 3); "
            "поведение стека в этом случае не проверено.")
    for i, text in enumerate(assumptions, 1):
        print(f"  {i}. {text}")

    ok = utilization["худший"] <= 1
    print()
    print(f"Вердикт: {'ХВАТАЕТ' if ok else 'НЕ ХВАТАЕТ'} "
          f"(загрузка: средний кадр {utilization['средний']:.0%}, худший {utilization['худший']:.0%})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
