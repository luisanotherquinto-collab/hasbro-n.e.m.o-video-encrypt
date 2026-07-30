"""
stego_core.py
=================================================================
Nucleo de codificacion/decodificacion inspirado en la patente
US 4,849,817 "Video System, Method and Apparatus for Incorporating
Audio or Data in Video Scan Intervals" (M. Short / ISIX Inc., 1989).

Este modulo NO reproduce el circuito analogico original (IRE,
subportadora de color, etc.) -- eso no tiene sentido en un pipeline
100% digital -- pero SI reproduce fielmente su *arquitectura logica*:

  * Cada "field" (aqui: cada frame de video) lleva una CABECERA
    (Fig. 2c de la patente) con:
        - Tier identifier number  (de que fuente / video viene)
        - Tier field number       (numero de secuencia dentro de esa fuente)
        - Field type              (0=video normal, 1=frame con datos ocultos)
        - Checksum
        - Copia duplicada de todo lo anterior (redundancia, igual que
          las lineas 17-21 duplican las lineas 12-16 en la patente)

  * Los datos digitales (texto oculto / carga util) se codifican como
    "data cells" pulso-codificados: cada byte = start bit(1) + 8 bits
    (LSB primero) + stop bit(0), agrupados de a 4 "data frames" por
    linea de escaneo -- EXACTAMENTE como describe la patente
    ("four data frames of ten bits per data frame ... per horizontal
    scan line", columna 21-22, reivindicaciones 2 y 11).

  * El audio se codifica como un "envelope" (Fig. 23/24): pulsos de
    referencia al inicio y al final (para permitir verificacion de
    nivel) + una seccion de audio REDUNDANTE (cola del frame anterior)
    + audio FRESCO del frame actual -- tal como reivindican las
    claims 4-9 de la patente.

Todo se codifica como bloques de pixeles en filas reservadas de cada
frame, usando 0/255 (o el valor de muestra de audio) en el canal de
luminancia. Como la codificacion es sensible a compresion con
perdida, el pipeline de E/S usa ffmpeg con un codec SIN PERDIDA
(FFV1 en contenedor .mkv) -- ver video_io.py.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

# -----------------------------------------------------------------
# Parametros de bajo nivel
# -----------------------------------------------------------------

# Ancho (en pixeles) de un "bit cell" digital. La patente no fija un
# ancho concreto (dependia de la duracion en microsegundos sobre la
# senal analogica); aqui elegimos un ancho que sea comodo de leer de
# vuelta con un simple promedio + umbral, tolerante a un poco de
# ruido de escalado si el video se re-muestrea.
BIT_BLOCK_W = 4

BITS_PER_BYTE_FRAME = 10          # start(1) + 8 datos + stop(0)
BYTE_FRAMES_PER_ROW = 4           # "four data frames ... per horizontal scan line"
BITS_PER_ROW = BITS_PER_BYTE_FRAME * BYTE_FRAMES_PER_ROW   # 40
BYTES_PER_ROW = BYTE_FRAMES_PER_ROW                        # 4

HEADER_MAGIC = 0xACDC  # marca para reconocer un frame generado por esta herramienta

# Estructura de la cabecera (ver Fig. 2c de la patente):
#   fila 0: 2 bytes magic + 2 bytes reservados         (utility data)
#   fila 1: 2 bytes tier id + 1 byte tier field# + 1 byte field type
#   fila 2: 1 byte checksum + 3 bytes padding
#   filas 3-5: DUPLICADO de las filas 0-2 (redundancia)
HEADER_ROWS = 6
HEADER_DATA_ROWS = 3  # las primeras 3 (sin contar el duplicado)

FIELD_TYPE_VIDEO = 0
FIELD_TYPE_DATA = 1     # este frame ademas trae datos digitales (texto/payload)
FIELD_TYPE_END = 2      # marca de fin de una "tier" (equivalente al valor 255 de la patente)


# -----------------------------------------------------------------
# Utilidades de bit-cell (Fig. 21 de la patente)
# -----------------------------------------------------------------

def _byte_to_cell_bits(byte_val: int) -> List[int]:
    """1 byte -> 10 bits: start(1) + 8 bits LSB-first + stop(0)."""
    bits = [1]
    for i in range(8):
        bits.append((byte_val >> i) & 1)
    bits.append(0)
    return bits


def _cell_bits_to_byte(bits: List[int]) -> Optional[int]:
    """Inverso de _byte_to_cell_bits. Devuelve None si start/stop no calzan
    (indicador de corrupcion, igual que el checksum de la patente)."""
    if len(bits) != 10 or bits[0] != 1 or bits[-1] != 0:
        return None
    val = 0
    for i in range(8):
        val |= (bits[1 + i] & 1) << i
    return val


def bytes_to_row_bits(data: bytes) -> List[int]:
    """N bytes -> lista de bits lista para escribir en una fila (N debe
    ser BYTES_PER_ROW; se rellena con 0x00 si falta)."""
    data = (data + b"\x00" * BYTES_PER_ROW)[:BYTES_PER_ROW]
    bits: List[int] = []
    for b in data:
        bits.extend(_byte_to_cell_bits(b))
    return bits


def row_bits_to_bytes(bits: List[int]) -> Optional[bytes]:
    """Inverso de bytes_to_row_bits. Devuelve None si algun data-frame
    esta corrupto (start/stop invalidos)."""
    if len(bits) != BITS_PER_ROW:
        return None
    out = bytearray()
    for i in range(BYTE_FRAMES_PER_ROW):
        cell = bits[i * BITS_PER_BYTE_FRAME:(i + 1) * BITS_PER_BYTE_FRAME]
        val = _cell_bits_to_byte(cell)
        if val is None:
            return None
        out.append(val)
    return bytes(out)


# -----------------------------------------------------------------
# Escritura/lectura de una fila de "data cells" sobre un frame numpy
# -----------------------------------------------------------------

def required_width(bits_per_row: int = BITS_PER_ROW, block_w: int = BIT_BLOCK_W) -> int:
    return bits_per_row * block_w


def write_bits_row(frame: np.ndarray, row_y: int, bits: List[int],
                    block_w: int = BIT_BLOCK_W, channel: int = 0) -> None:
    """Escribe `bits` como bloques blancos(255)/negros(0) en la fila row_y,
    usando un unico canal (por defecto: canal 0 = azul en BGR de OpenCV)
    para no interferir con las otras filas/canales."""
    h, w, _ = frame.shape
    need = len(bits) * block_w
    if need > w:
        raise ValueError(f"El frame es demasiado angosto ({w}px) para {len(bits)} bits "
                          f"con block_w={block_w} (se necesitan {need}px).")
    row = frame[row_y]
    for i, bit in enumerate(bits):
        val = 255 if bit else 0
        row[i * block_w:(i + 1) * block_w, channel] = val


def read_bits_row(frame: np.ndarray, row_y: int, num_bits: int,
                   block_w: int = BIT_BLOCK_W, channel: int = 0) -> List[int]:
    """Lee `num_bits` bits de la fila row_y (promedio del bloque + umbral 128,
    equivalente digital al umbral 40%/60% IRE de la patente)."""
    row = frame[row_y]
    bits = []
    for i in range(num_bits):
        block = row[i * block_w:(i + 1) * block_w, channel]
        avg = float(np.mean(block))
        bits.append(1 if avg >= 128 else 0)
    return bits


def write_bytes_row(frame: np.ndarray, row_y: int, data: bytes,
                     block_w: int = BIT_BLOCK_W, channel: int = 0) -> None:
    write_bits_row(frame, row_y, bytes_to_row_bits(data), block_w, channel)


def read_bytes_row(frame: np.ndarray, row_y: int, block_w: int = BIT_BLOCK_W,
                    channel: int = 0) -> Optional[bytes]:
    bits = read_bits_row(frame, row_y, BITS_PER_ROW, block_w, channel)
    return row_bits_to_bytes(bits)


# -----------------------------------------------------------------
# Cabecera por frame (Fig. 2c) -- tier id / field number / field type
# -----------------------------------------------------------------

MAGIC_BYTE = 0xAC


@dataclass
class FrameHeader:
    tier_id: int          # 0..65535 : identifica de que fuente/video viene el field
    field_number: int      # 0..254   : numero de secuencia dentro del tier (255 = fin)
    field_type: int         # FIELD_TYPE_*
    fresh_len: int = 0       # muestras de audio "frescas" incluidas en este frame
    sample_rate: int = 0      # sample rate (Hz) del audio oculto, 0 = sin audio
    valid: bool = True       # False si la cabecera (y su duplicado) no calzan

    def to_rows(self) -> List[bytes]:
        # fila 0 (utility, Fig. 2c lineas 12-13): magic + cantidad de
        # muestras de audio "frescas" que trae este frame.
        utility = struct.pack(">BHB", MAGIC_BYTE, self.fresh_len & 0xFFFF, 0)
        # fila 1 (Fig. 2c linea 14-15): tier identifier + tier field# + field type
        tier = struct.pack(">HBB", self.tier_id & 0xFFFF,
                            self.field_number & 0xFF, self.field_type & 0xFF)
        checksum = (sum(utility) + sum(tier)) & 0xFF
        # fila 2 (Fig. 2c linea 16): checksum + sample_rate del audio oculto
        chk_row = bytes([checksum]) + struct.pack(">H", self.sample_rate & 0xFFFF) + b"\x00"
        primary = [utility, tier, chk_row]
        return primary + primary  # filas 3-5 = duplicado de 0-2 (redundancia)


def embed_header(frame: np.ndarray, header: FrameHeader, start_row: int = 0,
                  block_w: int = BIT_BLOCK_W) -> None:
    for i, row_bytes in enumerate(header.to_rows()):
        write_bytes_row(frame, start_row + i, row_bytes, block_w)


def extract_header(frame: np.ndarray, start_row: int = 0,
                    block_w: int = BIT_BLOCK_W) -> Optional[FrameHeader]:
    rows = []
    for i in range(HEADER_ROWS):
        rows.append(read_bytes_row(frame, start_row + i, block_w))

    def parse(triplet):
        utility, tier, chk_row = triplet
        if utility is None or tier is None or chk_row is None:
            return None
        magic, fresh_len, _ = struct.unpack(">BHB", utility)
        if magic != MAGIC_BYTE:
            return None
        tier_id, field_number, field_type = struct.unpack(">HBB", tier)
        expected_chk = (sum(utility) + sum(tier)) & 0xFF
        if chk_row[0] != expected_chk:
            return None
        sample_rate = struct.unpack(">H", chk_row[1:3])[0]
        return FrameHeader(tier_id, field_number, field_type, fresh_len, sample_rate)

    primary = parse(rows[0:3])
    duplicate = parse(rows[3:6])

    if primary is not None:
        primary.valid = True
        return primary
    if duplicate is not None:
        # Se recupero gracias a la copia duplicada (igual que la patente
        # contempla en columna 5: "lines 17-21 may contain a duplicate").
        duplicate.valid = True
        return duplicate
    return None


# -----------------------------------------------------------------
# Envelope de audio comprimido (Fig. 23 / Fig. 24 y reivindicaciones 4-9)
# -----------------------------------------------------------------

REF_PULSE_LEN = 4        # muestras "pulso de referencia" (nivel fijo conocido)
REF_PULSE_LEVEL = 128    # nivel medio -> equivalente digital a "~50 IRE" (claim 5)
REDUNDANT_LEN = 32       # muestras redundantes (cola del frame anterior)


@dataclass
class AudioEnvelopeLayout:
    fresh_len: int   # muestras "frescas" del frame actual
    rows: int        # filas necesarias para alojar el envelope completo
    width: int

    @property
    def total_len(self) -> int:
        return REF_PULSE_LEN + REDUNDANT_LEN + self.fresh_len + REF_PULSE_LEN

    @classmethod
    def compute(cls, fresh_len: int, width: int) -> "AudioEnvelopeLayout":
        total = REF_PULSE_LEN + REDUNDANT_LEN + fresh_len + REF_PULSE_LEN
        rows = (total + width - 1) // width
        return cls(fresh_len, rows, width)


def build_audio_envelope(fresh_samples: bytes, prev_tail: bytes) -> bytes:
    """Arma el envelope: [pulso-ref inicio][redundante][fresco][pulso-ref fin].
    `prev_tail` son las ultimas REDUNDANT_LEN muestras del frame de audio
    anterior (o silencio si es el primer frame de esa tier)."""
    lead = bytes([REF_PULSE_LEVEL]) * REF_PULSE_LEN
    tail_ref = bytes([REF_PULSE_LEVEL]) * REF_PULSE_LEN
    silence_pad = bytes([REF_PULSE_LEVEL]) * REDUNDANT_LEN
    redundant = (prev_tail + silence_pad)[:REDUNDANT_LEN]
    return lead + redundant + fresh_samples + tail_ref


def audio_envelope_rows(total_len: int, width: int) -> int:
    return (total_len + width - 1) // width


def embed_audio_envelope(frame: np.ndarray, envelope: bytes, start_row: int,
                          channel: int = 1) -> int:
    """Escribe el envelope de audio como muestras crudas de 1 pixel = 1 byte,
    usando un canal separado del de los datos digitales (canal 1 = verde).
    Solo toca las filas estrictamente necesarias (nunca el resto del frame).
    Devuelve la cantidad de filas usadas."""
    h, w, _ = frame.shape
    n_rows = audio_envelope_rows(len(envelope), w)
    if start_row + n_rows > h:
        raise ValueError("No hay suficientes filas reservadas para el audio.")
    block = frame[start_row:start_row + n_rows, :, channel]
    padded = envelope + bytes(n_rows * w - len(envelope))
    block[:, :] = np.frombuffer(padded, dtype=np.uint8).reshape(n_rows, w)
    return n_rows


def extract_audio_envelope(frame: np.ndarray, start_row: int, total_len: int,
                            channel: int = 1) -> bytes:
    h, w, _ = frame.shape
    n_rows = audio_envelope_rows(total_len, w)
    block = frame[start_row:start_row + n_rows, :, channel]
    flat = block.reshape(-1)
    return bytes(flat[:total_len].tolist())


def split_envelope(envelope: bytes, fresh_len: int) -> bytes:
    """Dado un envelope leido, devuelve solo las muestras 'frescas' (el
    audio real de ese frame; descarta pulsos de referencia y redundancia)."""
    start = REF_PULSE_LEN + REDUNDANT_LEN
    return envelope[start:start + fresh_len]


def check_reference_pulses(envelope: bytes, tolerance: int = 40) -> bool:
    """Verifica que los pulsos de referencia inicio/fin sigan cerca del
    nivel esperado -- deteccion de corrupcion, igual que las claims 7-8
    de la patente ('detecting the level of the reference pulses ... for
    producing an output representative of the difference')."""
    if len(envelope) < REF_PULSE_LEN * 2:
        return False
    lead = envelope[:REF_PULSE_LEN]
    tail = envelope[-REF_PULSE_LEN:]
    lead_ok = all(abs(b - REF_PULSE_LEVEL) <= tolerance for b in lead)
    tail_ok = all(abs(b - REF_PULSE_LEVEL) <= tolerance for b in tail)
    return lead_ok and tail_ok


# -----------------------------------------------------------------
# Payload digital (texto / datos arbitrarios) -- solo en frames FIELD_TYPE_DATA
# -----------------------------------------------------------------

def payload_row_count(payload_len: int) -> int:
    """1 fila para el largo (4 bytes) + filas para los datos (BYTES_PER_ROW c/u)."""
    data_rows = (payload_len + BYTES_PER_ROW - 1) // BYTES_PER_ROW
    return 1 + max(data_rows, 0)


def embed_payload(frame: np.ndarray, payload: bytes, start_row: int,
                   block_w: int = BIT_BLOCK_W) -> None:
    length_bytes = struct.pack(">I", len(payload))
    write_bytes_row(frame, start_row, length_bytes, block_w)
    row = start_row + 1
    for i in range(0, len(payload), BYTES_PER_ROW):
        chunk = payload[i:i + BYTES_PER_ROW]
        write_bytes_row(frame, row, chunk, block_w)
        row += 1


def extract_payload(frame: np.ndarray, start_row: int,
                     block_w: int = BIT_BLOCK_W) -> Optional[bytes]:
    length_bytes = read_bytes_row(frame, start_row, block_w)
    if length_bytes is None:
        return None
    length = struct.unpack(">I", length_bytes)[0]
    if length > 10_000_000:  # sanity check
        return None
    out = bytearray()
    row = start_row + 1
    remaining = length
    while remaining > 0:
        chunk = read_bytes_row(frame, row, block_w)
        if chunk is None:
            return None
        take = min(BYTES_PER_ROW, remaining)
        out.extend(chunk[:take])
        remaining -= take
        row += 1
    return bytes(out)
