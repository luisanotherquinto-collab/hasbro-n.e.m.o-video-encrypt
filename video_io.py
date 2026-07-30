"""
video_io.py
=================================================================
Entrada/salida de video y audio para el pipeline de esteganografia.

PUNTO CRITICO (bug del script original que este proyecto corrige):
--------------------------------------------------------------
El script original grababa el video de salida con el codec 'mp4v'
(MPEG-4 con perdida). Cualquier esteganografia por bit-menos-
significativo (o por umbral de bloques como aqui) se destruye en
cuanto el video pasa por compresion con perdida, asi que el
decodificador original en la practica solo funcionaba si el video
de salida no volvia a comprimirse.

Aqui la escritura de salida se hace con FFmpeg usando el codec
FFV1 (video sin perdida) en contenedor .mkv. Es el equivalente
digital a lo que la patente asume implicitamente: una senal de
video que llega al decodificador sin alteraciones "opacas" (en la
patente, una VHS/Betamax de calidad, no una recompresion con
perdida agresiva).

Si el usuario necesita compartir el video final en MP4 para
reproduccion normal (por ejemplo, luego de decodificar y
reconstruir un programa elegido), sí se re-codifica a H.264 en esa
etapa -- porque ya no necesita conservar los datos ocultos.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import numpy as np
import cv2
import wave
from dataclasses import dataclass
from typing import Iterator, Optional


class FFmpegNotFoundError(RuntimeError):
    pass


_FFMPEG_EXE: Optional[str] = None


def find_ffmpeg() -> str:
    """Ubica el ejecutable de ffmpeg. Cachea el resultado. Si no lo
    encuentra, lanza un error con instrucciones concretas en vez de un
    FileNotFoundError crudo de subprocess."""
    global _FFMPEG_EXE
    if _FFMPEG_EXE:
        return _FFMPEG_EXE

    # 1) Un ffmpeg "portable" (ffmpeg.exe + DLLs) sentado en la misma
    #    carpeta que este archivo o que el script principal -- muy comun
    #    en Windows cuando no se quiere instalar nada globalmente.
    exe_names = ["ffmpeg.exe", "ffmpeg"] if os.name == "nt" else ["ffmpeg"]
    local_dirs = []
    try:
        local_dirs.append(os.path.dirname(os.path.abspath(__file__)))
    except NameError:
        pass
    local_dirs.append(os.path.abspath(os.getcwd()))
    try:
        import sys
        if getattr(sys, "argv", None) and sys.argv[0]:
            local_dirs.append(os.path.dirname(os.path.abspath(sys.argv[0])))
    except Exception:
        pass
    for d in dict.fromkeys(local_dirs):  # dedupe preservando orden
        for name in exe_names:
            candidate = os.path.join(d, name)
            if os.path.isfile(candidate):
                _FFMPEG_EXE = candidate
                return candidate

    # 2) El PATH del sistema.
    exe = shutil.which("ffmpeg")
    if exe:
        _FFMPEG_EXE = exe
        return exe

    # 3) Ubicaciones tipicas de instalacion en Windows.
    candidates = []
    if os.name == "nt":
        for env_var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env_var)
            if not base:
                continue
            candidates.append(os.path.join(base, "ffmpeg", "bin", "ffmpeg.exe"))
        # Ruta tipica cuando se instala con winget:
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            winget_dir = os.path.join(local, "Microsoft", "WinGet", "Packages")
            if os.path.isdir(winget_dir):
                for root, _dirs, files in os.walk(winget_dir):
                    if "ffmpeg.exe" in files:
                        candidates.append(os.path.join(root, "ffmpeg.exe"))
    for c in candidates:
        if c and os.path.isfile(c):
            _FFMPEG_EXE = c
            return c

    raise FFmpegNotFoundError(
        "No se encontro 'ffmpeg' (necesario para leer/escribir video y audio).\n\n"
        "Esto puede pasar aunque ya lo hayas instalado, si:\n"
        "  - lo agregaste al PATH pero no reiniciaste la terminal / el editor "
        "desde el que corres este script (el PATH se lee una sola vez al "
        "abrir la terminal - hay que abrir una NUEVA);\n"
        "  - lo instalaste solo para tu usuario y este script corre con otro "
        "entorno (por ejemplo, doble click desde el Explorador de Windows en "
        "vez de una terminal);\n"
        "  - estas usando el Python de la Microsoft Store (WindowsApps), que "
        "a veces no hereda bien el PATH del sistema - si es tu caso, probá "
        "instalar Python desde https://python.org en su lugar.\n\n"
        "Para confirmar: abrí una terminal NUEVA y corré:  ffmpeg -version\n"
        "Si eso funciona ahi pero el script sigue sin encontrarlo, es "
        "justamente el problema de la terminal/entorno viejo de arriba.\n\n"
        "Alternativa sin tocar el PATH: poné ffmpeg.exe (y sus .dll) en la "
        "misma carpeta que estos scripts .py -- se detecta automaticamente."
    )


def check_ffmpeg_available() -> None:
    """Falla rapido y claro (antes de procesar nada) si ffmpeg no esta."""
    find_ffmpeg()


@dataclass
class VideoInfo:
    width: int
    height: int
    fps: float
    frame_count: int


def probe_video(path: str) -> VideoInfo:
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise IOError(f"No se pudo abrir el video: {path}")
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return VideoInfo(w, h, fps, n)


def read_frames(path: str) -> Iterator[np.ndarray]:
    """Generador de frames BGR (uint8, HxWx3) leyendo con OpenCV."""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise IOError(f"No se pudo abrir el video: {path}")
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            yield frame
    finally:
        cap.release()


class LosslessWriter:
    """Escribe frames BGR a un .mkv sin perdida (FFV1) via ffmpeg."""

    def __init__(self, path: str, width: int, height: int, fps: float):
        if not path.lower().endswith(".mkv"):
            path = path.rsplit(".", 1)[0] + ".mkv"
        self.path = path
        cmd = [
            find_ffmpeg(), "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}",
            "-r", str(fps),
            "-i", "-",
            "-an",
            "-c:v", "ffv1",
            "-loglevel", "error",
            self.path,
        ]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    def write(self, frame: np.ndarray) -> None:
        self.proc.stdin.write(frame.astype(np.uint8).tobytes())

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.wait()


def extract_audio_pcm_u8(path: str, sample_rate: int) -> bytes:
    """Extrae el audio de `path` (video o archivo de audio) como PCM
    de 8 bits sin signo, mono, a `sample_rate` Hz, usando ffmpeg.
    Devuelve b"" si el archivo no tiene pista de audio."""
    cmd = [
        find_ffmpeg(), "-y", "-i", path,
        "-vn",
        "-ac", "1",
        "-ar", str(sample_rate),
        "-f", "u8",
        "-loglevel", "error",
        "-",
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout or b""


def write_wav_u8(path: str, pcm: bytes, sample_rate: int) -> None:
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)  # 8-bit unsigned
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)


def mux_video_audio_to_mp4(video_path: str, wav_path: Optional[str], out_path: str) -> None:
    """Recodifica un .mkv/.avi (posiblemente sin audio) a .mp4 H.264,
    agregando `wav_path` como pista de audio si se provee. Se usa solo
    para el archivo FINAL reconstruido (ya no necesita ser sin perdida)."""
    if wav_path:
        cmd = [
            find_ffmpeg(), "-y",
            "-i", video_path,
            "-i", wav_path,
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-shortest",
            "-loglevel", "error",
            out_path,
        ]
    else:
        cmd = [
            find_ffmpeg(), "-y",
            "-i", video_path,
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-an",
            "-loglevel", "error",
            out_path,
        ]
    subprocess.run(cmd, check=True)
