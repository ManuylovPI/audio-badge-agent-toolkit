"""Параметры аудиопротокола Omi. Единственный источник — docs/omi-audio-protocol.md.

В комментариях: раздел документа и файл:строка прошивки (пути от корня репозитория omi,
transport.c / codec.c / config.h лежат в omi/firmware/omi/src/lib/core/).
"""

# --- Раздел 1: кодек ---
SAMPLE_RATE = 16000        # codec.c:97
CHANNELS = 1               # codec.c:97
FRAME_SAMPLES = 320        # config.h:28 (160 * 2) -> 20 мс
BITRATE = 32000            # config.h:31, codec.c:98
VBR = 1                    # config.h:32, codec.c:99
VBR_CONSTRAINT = 0         # codec.c:100
COMPLEXITY = 3             # config.h:33, codec.c:101
LSB_DEPTH = 16             # codec.c:103
DTX = 0                    # codec.c:104
INBAND_FEC = 0             # codec.c:105
PACKET_LOSS_PERC = 0       # codec.c:106
MAX_FRAME_BYTES = 160      # config.h:29, буфер codec.c:52
AVG_FRAME_BYTES = 80       # [вывод] в документе: 32 кбит/с * 20 мс, в коде не записано

FRAMES_PER_SECOND = SAMPLE_RATE // FRAME_SAMPLES  # 50
FRAME_MS = 1000 * FRAME_SAMPLES // SAMPLE_RATE    # 20

# --- Раздел 2: заголовок уведомления ---
HEADER_SIZE = 3            # NET_BUFFER_HEADER_SIZE, transport.c:973
PACKET_NUMBER_MOD = 1 << 16  # packet_next_index — uint16_t, transport.c:1060

# --- Раздел 3: MTU, очередь ---
MIN_MTU = 100              # MINIMAL_PACKET_SIZE, config.h:9, проверка transport.c:1229
CONFIG_TX_MTU = 498        # CONFIG_BT_L2CAP_TX_MTU, omi.conf:123 (верхняя граница, не проверено)
LL_DATA_LENGTH_MAX = 251   # CONFIG_BT_CTLR_DATA_LENGTH_MAX, omi.conf:125
TX_QUEUE_FRAMES = 32       # NETWORK_RING_BUF_SIZE, config.h:8
CONN_INTERVAL_MAX_MS = 15  # запрошенный интервал 7,5–15 мс, transport.c:783-788

# --- Раздел 5: запись на SD ---
SD_LENGTH_PREFIX = 1       # OPUS_PREFIX_LENGTH, transport.c:1133
SD_BLOCK_BYTES = 440       # MAX_WRITE_SIZE, sd_card.h:10


def fragment_sizes(frame_bytes: int, mtu: int) -> list[int]:
    """Размеры нагрузки уведомлений для одного кадра: MIN(mtu - 3, остаток), transport.c:1075."""
    sizes = []
    offset = 0
    while offset < frame_bytes:
        size = min(mtu - HEADER_SIZE, frame_bytes - offset)
        sizes.append(size)
        offset += size
    return sizes
