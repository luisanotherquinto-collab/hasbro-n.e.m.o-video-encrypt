# US4849817 - Herramienta GUI (v2)

Version mejorada del codificador/decodificador multi-programa inspirado en
la patente **US 4,849,817** — *"Video System, Method and Apparatus for
Incorporating Audio or Data in Video Scan Intervals"* (Michael P. Short /
ISIX Inc., 1989).

## Que cambio respecto de la version anterior

1. **Bug critico corregido — codec con perdida.**
   El script original grababa el video de salida con `mp4v` (con perdida).
   Cualquier dato oculto por LSB se destruye apenas el video se comprime asi,
   asi que en la practica el decodificador original solo funcionaba si nadie
   tocaba el archivo de salida. Ahora la salida se graba **sin perdida**
   (codec **FFV1** dentro de un contenedor **.mkv**) via `ffmpeg`.
   *(Si necesitas compartir el resultado final en MP4 normal, hace eso
   recien DESPUES de decodificar — ver "Reconstruccion" mas abajo.)*

2. **Codificacion mas fiel a la patente.**
   En vez de un LSB plano sobre toda la imagen, ahora cada frame lleva:
   - Una **cabecera** (Fig. 2c de la patente): tier identifier, tier field
     number, field type, checksum, y una **copia duplicada completa** para
     redundancia — igual que las lineas 17-21 duplican las 12-16 en la
     patente.
   - Los datos digitales (texto oculto) se codifican como **"data cells"**
     pulso-codificados: cada byte = start bit(1) + 8 bits + stop bit(0),
     agrupados de a 4 "data frames" por linea de escaneo — tal cual describe
     la patente (reivindicaciones 2 y 11).
   - El audio se codifica como un **"envelope"** con pulsos de referencia al
     inicio/fin + audio redundante (cola del frame anterior) + audio fresco
     — igual que las Fig. 23/24 y las reivindicaciones 4-9.

3. **Soporte de audio real (nuevo).**
   Cada uno de los 4 videos de entrada conserva **su propio audio**, oculto
   dentro de la señal de video (no como pista de audio normal del
   contenedor). El decodificador lo recupera como un `.wav` independiente
   por cada fuente.

4. **El decodificador ahora reconstruye "programas" completos**, no solo el
   texto oculto — replicando la idea central de la patente (Fig. 3b:
   *"video fields re-assembled according to viewer selection"*):
   - el texto oculto,
   - el audio oculto de cada una de las 4 fuentes,
   - opcionalmente, el video de cada fuente por separado (re-muxado con su
     audio recuperado) como `.mp4` normal.

## Archivos

| Archivo | Que hace |
|---|---|
| `stego_core.py` | Nucleo puro (sin GUI, sin video): bits, cabecera, envelope de audio, payload. Testeable por separado. |
| `video_io.py` | Lectura de frames, escritura sin perdida (ffmpeg/FFV1), extraccion de audio y muxado final. |
| `multiplex.py` | Logica de alto nivel: entrelaza los 4 videos, arma cada frame, decodifica y reconstruye. |
| `hasbro_nemo_encoder.py` | La GUI (tkinter) — es el archivo que se ejecuta. |

## Requisitos

- Python 3 con `opencv-python`, `numpy`.
- **`ffmpeg` instalado y en el PATH** (se usa para grabar sin perdida, extraer
  audio y muxar el resultado final). En Debian/Ubuntu: `apt install ffmpeg`.

### "No se encontro ffmpeg" en Windows aunque ya lo instalaste

Es un problema clasico de PATH, no del script. La causa mas comun:

- **Instalaste/agregaste ffmpeg al PATH y no abriste una terminal nueva.**
  El PATH se lee una sola vez cuando se abre la terminal/editor — si lo
  agregaste con esa ventana ya abierta, no lo va a ver. Cerra todo y abri
  una terminal nueva, corre `ffmpeg -version` ahi mismo para confirmar, y
  reci\u00e9n despues corre el script desde esa misma terminal.
- Si usas el **Python de la Microsoft Store** (se ve como
  `...WindowsApps\PythonSoftwareFoundation.Python...` en el traceback), a
  veces no hereda bien el PATH del sistema. Si el problema persiste, probá
  instalar Python desde [python.org](https://python.org) en su lugar.
- Si lo instalaste solo para tu usuario y corres el script con doble click
  desde el Explorador (en vez de una terminal), puede que ese entorno no
  tenga el mismo PATH.

La v2 de esta herramienta ya detecta esto apenas abrís la ventana (te avisa
arriba de todo, en rojo, si no encuentra ffmpeg) en vez de fallar recien
cuando le diste a "Iniciar Codificacion".

## Uso

```
python3 hasbro_nemo_encoder.py
```

**Pestaña Codificador:** elegis los 4 videos, opcionalmente el texto a
ocultar, activas o no "ocultar audio" y elegis la frecuencia de muestreo
(11025 Hz por defecto — similar al ancho de banda de ~11kHz que menciona la
patente para su modo de audio comprimido). El resultado es un `.mkv`.

**Pestaña Decodificador:** eleg\u00eds ese `.mkv`, una carpeta de salida, y
si queres que ademas reconstruya el video de cada fuente (mas lento). Vas a
obtener el texto oculto, un `tier_N_audio.wav` por fuente, y (si lo pediste)
un `tier_N.mp4` por fuente ya con su audio.

## Limitaciones a tener en cuenta

- El archivo intermedio (`.mkv`/FFV1) **no debe volver a comprimirse con
  perdida** (ni resubirse a WhatsApp/YouTube/etc.) antes de decodificarlo, o
  se pierde lo oculto — igual que en la patente original, el "decoder" solo
  funciona sobre la señal tal cual salio del "encoder".
- El ancho minimo de video es 160px (lo que ocupa la cabecera); la altura
  minima depende de cuanto audio/texto quieras ocultar (la herramienta te
  avisa si no alcanza, en vez de fallar en silencio).
- La calidad de audio recuperado es deliberadamente "lo-fi" (8 bits, mono,
  ~11kHz) — es fiel al espiritu de la patente (que hablaba de compresion de

  ##warning
  this was created with claude only for fun and for posible utilty




  audio con perdida de fidelidad para caber en una linea de escaneo), no un
  codec de audio moderno.
