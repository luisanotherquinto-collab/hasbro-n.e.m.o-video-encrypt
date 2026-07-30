"""
multiplex.py
=================================================================
Codificador / decodificador multi-programa, inspirado en las
Fig. 3(a)/3(b)/4 de la patente US4,849,817: N fuentes de video se
entrelazan campo-a-campo (aqui: frame-a-frame) en una unica senal
compuesta, cada field/frame llevando su propia cabecera de
identificacion (tier id, numero de field, tipo) + su propio audio
oculto, de forma que un decodificador pueda:

  1) Reconstruir el mensaje de texto oculto (payload digital).
  2) Reconstruir la pista de audio oculta de CADA fuente por
     separado (aunque hayan sido entrelazadas en un solo video).
  3) Reconstruir el video original de una fuente elegida (Fig. 3b:
     "video fields re-assembled according to viewer selection").
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import numpy as np

import stego_core as sc
import video_io as vio

ProgressCB = Optional[Callable[[str], None]]


def _report(cb: ProgressCB, msg: str) -> None:
    if cb:
        cb(msg)


@dataclass
class TierResult:
    tier_id: int
    field_count: int
    frames: List[np.ndarray] = field(default_factory=list)
    audio_pcm: bytearray = field(default_factory=bytearray)
    sample_rate: int = 0
    wav_path: Optional[str] = None
    video_path: Optional[str] = None


@dataclass
class DecodeResult:
    hidden_text: Optional[str]
    tiers: Dict[int, TierResult]


def encode(video_paths: List[str], hidden_text: str, output_path: str,
           sample_rate: int = 11025, embed_audio: bool = True,
           progress_cb: ProgressCB = None) -> str:
    """Entrelaza los frames de `video_paths` (en orden ciclico, como la
    Fig. 3(a) de la patente) en un unico video SIN PERDIDA (.mkv/FFV1),
    incrustando en cada frame:
      - una cabecera de identificacion (tier id / field# / tipo)
      - (opcional) un envelope de audio comprimido con el audio propio
        de esa fuente para ese instante
      - en el primer frame de la primera fuente: el texto oculto.
    """
    n = len(video_paths)
    if n == 0:
        raise ValueError("Se necesita al menos un video de entrada.")

    vio.check_ffmpeg_available()

    infos = [vio.probe_video(p) for p in video_paths]
    width = infos[0].width
    height = infos[0].height
    fps = infos[0].fps or 30.0

    min_width = sc.required_width()
    if width < min_width:
        raise ValueError(f"El video es demasiado angosto ({width}px); se necesitan "
                          f"al menos {min_width}px de ancho para la cabecera.")

    payload = hidden_text.encode("utf-8")
    payload_rows_needed = sc.payload_row_count(len(payload)) if payload else 0

    fresh_len = int(round(sample_rate / fps)) if embed_audio else 0
    audio_rows = 0
    if embed_audio:
        total_env_len = sc.REF_PULSE_LEN * 2 + sc.REDUNDANT_LEN + fresh_len
        audio_rows = sc.audio_envelope_rows(total_env_len, width)

    header_end = sc.HEADER_ROWS
    audio_start = header_end
    payload_start = audio_start + audio_rows

    needed_height = payload_start + payload_rows_needed + 1
    if needed_height > height:
        raise ValueError(
            f"El video es demasiado bajo ({height}px de alto) para alojar cabecera "
            f"({sc.HEADER_ROWS} filas) + audio ({audio_rows} filas) + payload "
            f"({payload_rows_needed} filas). Se necesitan al menos {needed_height}px.")

    _report(progress_cb, f"Diseno de filas: cabecera=0-{header_end-1}, "
                          f"audio={audio_start}-{audio_start+audio_rows-1}, "
                          f"payload desde {payload_start}")

    # --- extraer audio propio de cada fuente (si corresponde) ---
    audio_tracks: List[bytes] = []
    if embed_audio:
        for p in video_paths:
            _report(progress_cb, f"Extrayendo audio de {os.path.basename(p)}...")
            pcm = vio.extract_audio_pcm_u8(p, sample_rate)
            audio_tracks.append(pcm)
    else:
        audio_tracks = [b""] * n

    caps = [vio.read_frames(p) for p in video_paths]
    writer = vio.LosslessWriter(output_path, width, height, fps)

    field_counters = [0] * n
    audio_pos = [0] * n
    SILENCE_U8 = 128  # nivel medio = silencio en PCM u8 sin signo
    prev_tail = [bytes([SILENCE_U8]) * sc.REDUNDANT_LEN] * n
    exhausted = [False] * n
    payload_written = False

    try:
        while not all(exhausted):
            for i in range(n):
                if exhausted[i]:
                    continue
                try:
                    frame = next(caps[i])
                except StopIteration:
                    exhausted[i] = True
                    continue

                if frame.shape[1] != width or frame.shape[0] != height:
                    frame = __import__("cv2").resize(frame, (width, height))

                is_first_field = field_counters[i] == 0
                field_type = sc.FIELD_TYPE_VIDEO
                if is_first_field and i == 0 and payload:
                    field_type = sc.FIELD_TYPE_DATA

                header = sc.FrameHeader(
                    tier_id=i,
                    field_number=min(field_counters[i], 254),
                    field_type=field_type,
                    fresh_len=fresh_len,
                    sample_rate=sample_rate if embed_audio else 0,
                )
                sc.embed_header(frame, header, start_row=0)

                if embed_audio and fresh_len > 0:
                    track = audio_tracks[i]
                    start = audio_pos[i]
                    chunk = track[start:start + fresh_len]
                    if len(chunk) < fresh_len:
                        chunk = chunk + bytes([SILENCE_U8]) * (fresh_len - len(chunk))
                    envelope = sc.build_audio_envelope(chunk, prev_tail[i])
                    sc.embed_audio_envelope(frame, envelope, start_row=audio_start)
                    prev_tail[i] = chunk[-sc.REDUNDANT_LEN:] if len(chunk) >= sc.REDUNDANT_LEN \
                        else (bytes([SILENCE_U8]) * (sc.REDUNDANT_LEN - len(chunk)) + chunk)
                    audio_pos[i] += fresh_len

                if field_type == sc.FIELD_TYPE_DATA and not payload_written:
                    sc.embed_payload(frame, payload, start_row=payload_start)
                    payload_written = True

                field_counters[i] += 1
                writer.write(frame)
            _report(progress_cb, f"Procesados ~{sum(field_counters)} fields entrelazados...")
    finally:
        writer.close()

    _report(progress_cb, f"Listo: {writer.path}")
    return writer.path


def decode(input_path: str, output_dir: str,
           reconstruct_video: bool = True,
           progress_cb: ProgressCB = None) -> DecodeResult:
    """Lee el video compuesto y reconstruye:
      - el texto oculto,
      - el audio oculto de cada tier (como .wav),
      - (opcional) el video de cada tier (como .mp4, re-muxado con su audio)
    """
    os.makedirs(output_dir, exist_ok=True)
    vio.check_ffmpeg_available()
    tiers: Dict[int, TierResult] = {}
    hidden_text: Optional[str] = None

    width = None
    for idx, frame in enumerate(vio.read_frames(input_path)):
        if width is None:
            width = frame.shape[1]

        header = sc.extract_header(frame, start_row=0)
        if header is None:
            _report(progress_cb, f"Frame {idx}: cabecera ilegible, se omite.")
            continue

        tier = tiers.setdefault(header.tier_id, TierResult(
            tier_id=header.tier_id, field_count=0,
            sample_rate=header.sample_rate))
        tier.field_count += 1

        if reconstruct_video:
            tier.frames.append(frame.copy())

        if header.sample_rate and header.fresh_len:
            audio_start = sc.HEADER_ROWS
            total_len = sc.REF_PULSE_LEN * 2 + sc.REDUNDANT_LEN + header.fresh_len
            envelope = sc.extract_audio_envelope(frame, audio_start, total_len)
            if sc.check_reference_pulses(envelope):
                fresh = sc.split_envelope(envelope, header.fresh_len)
                tier.audio_pcm.extend(fresh)
            else:
                _report(progress_cb, f"Frame {idx} (tier {header.tier_id}): "
                                      f"pulsos de referencia de audio fuera de rango, "
                                      f"se descarta ese fragmento de audio.")

        if header.field_type == sc.FIELD_TYPE_DATA and hidden_text is None:
            audio_rows = 0
            if header.sample_rate and header.fresh_len:
                total_len = sc.REF_PULSE_LEN * 2 + sc.REDUNDANT_LEN + header.fresh_len
                audio_rows = sc.audio_envelope_rows(total_len, frame.shape[1])
            payload_start = sc.HEADER_ROWS + audio_rows
            payload = sc.extract_payload(frame, start_row=payload_start)
            if payload is not None:
                try:
                    hidden_text = payload.decode("utf-8")
                except UnicodeDecodeError:
                    hidden_text = payload.decode("utf-8", errors="replace")

        if idx % 30 == 0:
            _report(progress_cb, f"Analizados {idx} frames...")

    # --- volcar resultados por tier ---
    for tier_id, tier in tiers.items():
        if tier.audio_pcm:
            wav_path = os.path.join(output_dir, f"tier_{tier_id}_audio.wav")
            vio.write_wav_u8(wav_path, bytes(tier.audio_pcm), tier.sample_rate)
            tier.wav_path = wav_path
            _report(progress_cb, f"Tier {tier_id}: audio guardado en {wav_path} "
                                  f"({len(tier.audio_pcm)} muestras)")

        if reconstruct_video and tier.frames:
            raw_mkv = os.path.join(output_dir, f"tier_{tier_id}_raw.mkv")
            h, w = tier.frames[0].shape[:2]
            writer = vio.LosslessWriter(raw_mkv, w, h, 30.0)
            for f in tier.frames:
                writer.write(f)
            writer.close()

            final_mp4 = os.path.join(output_dir, f"tier_{tier_id}.mp4")
            vio.mux_video_audio_to_mp4(raw_mkv, tier.wav_path, final_mp4)
            tier.video_path = final_mp4
            _report(progress_cb, f"Tier {tier_id}: video reconstruido en {final_mp4}")

        tier.frames = []  # liberar memoria, ya no se necesitan los frames crudos

    return DecodeResult(hidden_text=hidden_text, tiers=tiers)
