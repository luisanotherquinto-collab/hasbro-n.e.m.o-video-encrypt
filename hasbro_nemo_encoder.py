"""
hasbro_nemo_encoder.py
=================================================================
US4849817 - Herramienta GUI (v2)
-----------------------------------------------------------------
Version mejorada del codificador/decodificador multi-programa
inspirado en la patente US 4,849,817 "Video System, Method and
Apparatus for Incorporating Audio or Data in Video Scan Intervals"
(M. Short / ISIX Inc., 1989).

Cambios principales respecto de la version anterior:

1. CORRIGE el problema de fondo: el video de salida ya NO se graba
   con un codec con perdida (mp4v). Ahora se graba sin perdida
   (FFV1 dentro de .mkv), porque cualquier esteganografia por bit
   se destruye con compresion con perdida. Ver video_io.py.

2. El texto oculto y el audio se codifican como "data cells" e
   "audio envelopes" inspirados directamente en las figuras de la
   patente (Fig. 2c, Fig. 21 y Fig. 23), en vez de un LSB simple
   sobre toda la imagen. Ver stego_core.py.

3. SOPORTE DE AUDIO real: cada uno de los 4 videos de entrada
   conserva su propio audio, oculto dentro de la propia senal de
   video (igual que describe la patente), y el decodificador lo
   recupera como archivos .wav independientes -- uno por fuente.

4. El decodificador ahora reconstruye, ademas del texto oculto:
   - el audio oculto de cada una de las 4 fuentes (Fig. 16/20)
   - (opcional) el video de cada fuente por separado, re-muxado
     con su audio recuperado (Fig. 3b: "video fields re-assembled
     according to viewer selection")

Este archivo SOLO contiene la interfaz grafica (tkinter); toda la
logica de codificacion vive en stego_core.py / video_io.py /
multiplex.py para que se pueda probar por separado sin abrir la GUI.
"""

import os
import threading
import traceback

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import multiplex as mx
import video_io as vio

SAMPLE_RATES = [8000, 11025, 22050, 44100]
DEFAULT_SAMPLE_RATE = 11025


class StegoApp:
    def __init__(self, root):
        self.root = root
        self.root.title("US4849817 - Herramienta GUI (v2, con audio)")
        self.root.geometry("700x640")

        self.ffmpeg_status = tk.StringVar()
        self.ffmpeg_status_label = ttk.Label(root, textvariable=self.ffmpeg_status,
                                              foreground="red", wraplength=680, justify="left")
        self.ffmpeg_status_label.pack(fill="x", padx=10, pady=(8, 0))
        self._check_ffmpeg_at_startup()

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(expand=True, fill="both", padx=10, pady=10)

        self.tab_encoder = ttk.Frame(self.notebook)
        self.tab_decoder = ttk.Frame(self.notebook)

        self.notebook.add(self.tab_encoder, text="Codificador / Multiplexor")
        self.notebook.add(self.tab_decoder, text="Decodificador")

        self.setup_encoder_tab()
        self.setup_decoder_tab()

    def _check_ffmpeg_at_startup(self):
        try:
            path = vio.find_ffmpeg()
            self.ffmpeg_status.set("")  # todo OK, no mostramos nada
        except vio.FFmpegNotFoundError as e:
            self.ffmpeg_status.set(
                "⚠ No se encontro ffmpeg (necesario para leer/escribir video y "
                "audio). Los botones de codificar/decodificar van a fallar hasta "
                "que lo instales y lo tengas en el PATH. Detalle: " + str(e).split("\n")[0])

    # -----------------------------------------------------------
    # Pestaña codificador
    # -----------------------------------------------------------
    def setup_encoder_tab(self):
        self.v1_path = tk.StringVar()
        self.v2_path = tk.StringVar()
        self.v3_path = tk.StringVar()
        self.v4_path = tk.StringVar()
        self.out_path = tk.StringVar()
        self.sample_rate_var = tk.IntVar(value=DEFAULT_SAMPLE_RATE)
        self.embed_audio_var = tk.BooleanVar(value=True)

        ttk.Label(self.tab_encoder,
                  text="Selecciona 4 videos (mp4, avi, etc.) - se entrelazan como "
                       "'tiers' segun Fig. 3(a) de la patente:").grid(
            row=0, column=0, columnspan=3, pady=(10, 5), sticky="w")

        self.create_file_selector(self.tab_encoder, "Video 1 (tier 0):", self.v1_path, 1)
        self.create_file_selector(self.tab_encoder, "Video 2 (tier 1):", self.v2_path, 2)
        self.create_file_selector(self.tab_encoder, "Video 3 (tier 2):", self.v3_path, 3)
        self.create_file_selector(self.tab_encoder, "Video 4 (tier 3):", self.v4_path, 4)

        # --- opciones de audio ---
        audio_frame = ttk.LabelFrame(self.tab_encoder, text="Audio oculto")
        audio_frame.grid(row=5, column=0, columnspan=3, padx=10, pady=(10, 5), sticky="we")

        ttk.Checkbutton(audio_frame, text="Ocultar el audio propio de cada video",
                         variable=self.embed_audio_var).grid(
            row=0, column=0, padx=5, pady=5, sticky="w")

        ttk.Label(audio_frame, text="Frecuencia de muestreo:").grid(
            row=0, column=1, padx=(20, 5), pady=5, sticky="e")
        ttk.OptionMenu(audio_frame, self.sample_rate_var, DEFAULT_SAMPLE_RATE,
                        *SAMPLE_RATES).grid(row=0, column=2, padx=5, pady=5, sticky="w")

        ttk.Label(self.tab_encoder,
                  text="Texto oculto (se incrusta en el primer field del Video 1):").grid(
            row=6, column=0, columnspan=3, pady=(10, 0), sticky="w")
        self.text_data = tk.Text(self.tab_encoder, height=5, width=60)
        self.text_data.grid(row=7, column=0, columnspan=3, padx=10, pady=5)

        self.create_save_selector(self.tab_encoder, "Archivo Salida (.mkv, sin perdida):",
                                   self.out_path, 8)

        self.btn_encode = ttk.Button(self.tab_encoder, text="Iniciar Codificacion",
                                      command=self.start_encoding)
        self.btn_encode.grid(row=9, column=0, columnspan=3, pady=15)

        self.progress_enc = ttk.Progressbar(self.tab_encoder, mode="indeterminate")
        self.progress_enc.grid(row=10, column=0, columnspan=3, sticky="we", padx=10)

        ttk.Label(self.tab_encoder, text="Registro:").grid(
            row=11, column=0, columnspan=3, sticky="w", padx=10, pady=(10, 0))
        self.log_enc = tk.Text(self.tab_encoder, height=8, width=70, state="disabled")
        self.log_enc.grid(row=12, column=0, columnspan=3, padx=10, pady=5)

    def setup_decoder_tab(self):
        self.dec_video_path = tk.StringVar()
        self.dec_outdir = tk.StringVar()
        self.dec_reconstruct_video = tk.BooleanVar(value=True)

        ttk.Label(self.tab_decoder, text="Selecciona el video compuesto (.mkv):").grid(
            row=0, column=0, columnspan=3, pady=(20, 5), sticky="w")
        self.create_file_selector(self.tab_decoder, "Video:", self.dec_video_path, 1)

        ttk.Label(self.tab_decoder, text="Carpeta de salida (audio/video recuperados):").grid(
            row=2, column=0, columnspan=3, pady=(10, 0), sticky="w")
        self.create_folder_selector(self.tab_decoder, "Carpeta:", self.dec_outdir, 3)

        ttk.Checkbutton(self.tab_decoder,
                         text="Reconstruir tambien el video de cada fuente (mas lento)",
                         variable=self.dec_reconstruct_video).grid(
            row=4, column=0, columnspan=3, padx=10, pady=(5, 0), sticky="w")

        self.btn_decode = ttk.Button(self.tab_decoder, text="Decodificar Datos",
                                      command=self.start_decoding)
        self.btn_decode.grid(row=5, column=0, columnspan=3, pady=15)

        self.progress_dec = ttk.Progressbar(self.tab_decoder, mode="indeterminate")
        self.progress_dec.grid(row=6, column=0, columnspan=3, sticky="we", padx=10)

        ttk.Label(self.tab_decoder, text="Texto oculto extraido:").grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(10, 0), padx=10)
        self.text_output = tk.Text(self.tab_decoder, height=4, width=70)
        self.text_output.grid(row=8, column=0, columnspan=3, padx=10, pady=5)

        ttk.Label(self.tab_decoder, text="Resultado por fuente (tier):").grid(
            row=9, column=0, columnspan=3, sticky="w", padx=10, pady=(10, 0))
        self.log_dec = tk.Text(self.tab_decoder, height=8, width=70, state="disabled")
        self.log_dec.grid(row=10, column=0, columnspan=3, padx=10, pady=5)

    # -----------------------------------------------------------
    # Widgets auxiliares
    # -----------------------------------------------------------
    def create_file_selector(self, parent, label_text, var, row):
        ttk.Label(parent, text=label_text).grid(row=row, column=0, padx=10, pady=5, sticky="e")
        ttk.Entry(parent, textvariable=var, width=50).grid(row=row, column=1, padx=5, pady=5)
        ttk.Button(parent, text="Buscar",
                   command=lambda: var.set(filedialog.askopenfilename())).grid(
            row=row, column=2, padx=5, pady=5)

    def create_save_selector(self, parent, label_text, var, row):
        ttk.Label(parent, text=label_text).grid(row=row, column=0, padx=10, pady=5, sticky="e")
        ttk.Entry(parent, textvariable=var, width=50).grid(row=row, column=1, padx=5, pady=5)
        ttk.Button(parent, text="Guardar como",
                   command=lambda: var.set(
                       filedialog.asksaveasfilename(defaultextension=".mkv",
                                                     filetypes=[("Video sin perdida", "*.mkv")]))
                   ).grid(row=row, column=2, padx=5, pady=5)

    def create_folder_selector(self, parent, label_text, var, row):
        ttk.Label(parent, text=label_text).grid(row=row, column=0, padx=10, pady=5, sticky="e")
        ttk.Entry(parent, textvariable=var, width=50).grid(row=row, column=1, padx=5, pady=5)
        ttk.Button(parent, text="Elegir carpeta",
                   command=lambda: var.set(filedialog.askdirectory())).grid(
            row=row, column=2, padx=5, pady=5)

    def _log(self, widget, msg):
        def _do():
            widget.config(state="normal")
            widget.insert(tk.END, msg + "\n")
            widget.see(tk.END)
            widget.config(state="disabled")
        self.root.after(0, _do)

    # -----------------------------------------------------------
    # Codificacion
    # -----------------------------------------------------------
    def start_encoding(self):
        v1, v2, v3, v4 = (self.v1_path.get(), self.v2_path.get(),
                          self.v3_path.get(), self.v4_path.get())
        out = self.out_path.get()
        data = self.text_data.get("1.0", tk.END).strip()

        if not all([v1, v2, v3, v4, out]):
            messagebox.showerror("Error", "Selecciona los 4 videos y el archivo de salida.")
            return

        self.btn_encode.config(state="disabled")
        self.progress_enc.start(10)
        self.log_enc.config(state="normal")
        self.log_enc.delete("1.0", tk.END)
        self.log_enc.config(state="disabled")

        threading.Thread(target=self.run_encoding_task,
                          args=(v1, v2, v3, v4, out, data), daemon=True).start()

    def run_encoding_task(self, v1, v2, v3, v4, out, data):
        try:
            result_path = mx.encode(
                [v1, v2, v3, v4],
                hidden_text=data,
                output_path=out,
                sample_rate=self.sample_rate_var.get(),
                embed_audio=self.embed_audio_var.get(),
                progress_cb=lambda m: self._log(self.log_enc, m),
            )
            self._log(self.log_enc, "¡Completado!")
            self.root.after(0, lambda: messagebox.showinfo(
                "Exito", f"Video multiplexado guardado en:\n{result_path}"))
        except Exception as e:
            err_msg = str(e)
            traceback.print_exc()
            self._log(self.log_enc, f"ERROR: {err_msg}")
            self.root.after(0, lambda msg=err_msg: messagebox.showerror("Error", msg))
        finally:
            self.root.after(0, lambda: self.btn_encode.config(state="normal"))
            self.root.after(0, self.progress_enc.stop)

    # -----------------------------------------------------------
    # Decodificacion
    # -----------------------------------------------------------
    def start_decoding(self):
        video_path = self.dec_video_path.get()
        outdir = self.dec_outdir.get()
        if not video_path:
            messagebox.showerror("Error", "Selecciona un archivo de video.")
            return
        if not outdir:
            outdir = os.path.join(os.path.dirname(video_path) or ".", "decodificado")

        self.text_output.delete("1.0", tk.END)
        self.text_output.insert("1.0", "Decodificando...")
        self.log_dec.config(state="normal")
        self.log_dec.delete("1.0", tk.END)
        self.log_dec.config(state="disabled")
        self.btn_decode.config(state="disabled")
        self.progress_dec.start(10)

        threading.Thread(target=self.run_decoding_task,
                          args=(video_path, outdir), daemon=True).start()

    def run_decoding_task(self, video_path, outdir):
        try:
            result = mx.decode(
                video_path, outdir,
                reconstruct_video=self.dec_reconstruct_video.get(),
                progress_cb=lambda m: self._log(self.log_dec, m),
            )

            def _show_text():
                self.text_output.delete("1.0", tk.END)
                self.text_output.insert("1.0", result.hidden_text or "(sin texto oculto)")
            self.root.after(0, _show_text)

            for tid, tier in sorted(result.tiers.items()):
                msg = f"Tier {tid}: {tier.field_count} fields"
                if tier.wav_path:
                    msg += f" | audio: {tier.wav_path}"
                if tier.video_path:
                    msg += f" | video: {tier.video_path}"
                self._log(self.log_dec, msg)

        except Exception as e:
            err_msg = str(e)
            traceback.print_exc()

            def _show_err(msg=err_msg):
                self.text_output.delete("1.0", tk.END)
                self.text_output.insert("1.0", f"Error: {msg}")
            self.root.after(0, _show_err)
        finally:
            self.root.after(0, lambda: self.btn_decode.config(state="normal"))
            self.root.after(0, self.progress_dec.stop)


if __name__ == '__main__':
    root = tk.Tk()
    app = StegoApp(root)
    root.mainloop()
