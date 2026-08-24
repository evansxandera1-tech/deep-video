#!/usr/bin/env python3
"""
caverna-video v1.1
Flujo completo: toma un audio ya generado (caverna-audio), lo transcribe con
Whisper para tener timing exacto, arma escenas, genera/reutiliza imágenes
(stickman + fondo prehistórico) con Pollinations, arma el video final con
ffmpeg (imágenes + audio + subtítulos quemados) y sube todo a Drive vía rclone.

Carpetas remotas usadas (remoto rclone "gdrive"):
  caverna-audio      -> origen: audios ya generados
  caverna-fondos     -> banco de fondos/imagenes de referencia (input manual)
  caverna-imagenes   -> salida: imagenes generadas por escena
  caverna-videos     -> salida: video final
  caverna-fondos/descripciones.json -> cache de descripciones (banco de fondos)
"""

import subprocess
import json
import os
import re
import sys
import time
import logging
import requests
from urllib.parse import quote

REMOTE = "gdrive"
CARPETA_AUDIO = f"{REMOTE}:caverna-audio"
CARPETA_FONDOS = f"{REMOTE}:caverna-fondos"
CARPETA_IMAGENES = f"{REMOTE}:caverna-imagenes"
CARPETA_VIDEOS = f"{REMOTE}:caverna-videos"

WORK_DIR = "trabajo"
LOG_FILE = "caverna_video.log"
DESCRIPCIONES_LOCAL = os.path.join(WORK_DIR, "descripciones.json")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
POLLINATIONS_TOKEN = os.environ.get("POLLINATIONS_TOKEN", "")  # opcional

DURACION_ESCENA_OBJETIVO = 12  # segundos, objetivo (no fijo)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("caverna-video")


# ---------- utilidades rclone ----------

def rclone_lsf(ruta):
    r = subprocess.run(["rclone", "lsf", ruta], capture_output=True, text=True)
    if r.returncode != 0:
        if "directory not found" in r.stderr:
            log.warning(f"Carpeta no existe, creando: {ruta}")
            subprocess.run(["rclone", "mkdir", ruta], capture_output=True, text=True)
            return []
        log.error(f"rclone lsf falló en {ruta}: {r.stderr.strip()}")
        return []
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]


def rclone_copyto(origen, destino):
    r = subprocess.run(["rclone", "copyto", origen, destino], capture_output=True, text=True)
    if r.returncode != 0:
        log.error(f"rclone copyto falló ({origen} -> {destino}): {r.stderr.strip()}")
        return False
    return True


def rclone_copy(origen, destino):
    r = subprocess.run(["rclone", "copy", origen, destino], capture_output=True, text=True)
    if r.returncode != 0:
        log.error(f"rclone copy falló ({origen} -> {destino}): {r.stderr.strip()}")
        return False
    return True


def rclone_link(ruta_remota_archivo):
    """Genera (o recupera) un link público de descarga directa para un archivo en Drive."""
    r = subprocess.run(["rclone", "link", ruta_remota_archivo], capture_output=True, text=True)
    if r.returncode != 0:
        log.error(f"rclone link falló en {ruta_remota_archivo}: {r.stderr.strip()}")
        return None
    link = r.stdout.strip()
    # convierte el link de "ver" de Drive a un link de descarga directa
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", link)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    return link


# ---------- paso 1: elegir audio pendiente ----------

def elegir_audio_pendiente():
    audios = [a for a in rclone_lsf(CARPETA_AUDIO) if a.lower().endswith(".mp3")]
    listos = set(v[:-4] for v in rclone_lsf(CARPETA_VIDEOS) if v.lower().endswith(".mp4"))
    pendientes = [a for a in audios if a[:-4] not in listos]
    if not pendientes:
        return None
    return pendientes[0]


# ---------- paso 2: transcribir con whisper ----------

def transcribir(audio_path):
    import whisper
    modelo = whisper.load_model("base")
    resultado = modelo.transcribe(audio_path, language="en", word_timestamps=False)
    return resultado["segments"]  # lista de {start, end, text}


# ---------- paso 3: armar escenas agrupando segmentos ----------

def armar_escenas(segmentos, duracion_objetivo=DURACION_ESCENA_OBJETIVO):
    escenas = []
    actual_textos = []
    inicio = None
    fin = None
    for seg in segmentos:
        if inicio is None:
            inicio = seg["start"]
        actual_textos.append(seg["text"].strip())
        fin = seg["end"]
        if fin - inicio >= duracion_objetivo:
            escenas.append({"inicio": inicio, "fin": fin, "texto": " ".join(actual_textos)})
            actual_textos = []
            inicio = None
    if actual_textos:
        escenas.append({"inicio": inicio, "fin": fin, "texto": " ".join(actual_textos)})
    return escenas


# ---------- paso 4: descripciones del banco de fondos (cache con Gemini vision) ----------

def cargar_descripciones():
    if rclone_copyto(f"{CARPETA_FONDOS}/descripciones.json", DESCRIPCIONES_LOCAL):
        with open(DESCRIPCIONES_LOCAL, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def guardar_descripciones(desc):
    with open(DESCRIPCIONES_LOCAL, "w", encoding="utf-8") as f:
        json.dump(desc, f, ensure_ascii=False, indent=2)
    rclone_copyto(DESCRIPCIONES_LOCAL, f"{CARPETA_FONDOS}/descripciones.json")


def describir_imagen_gemini(imagen_path):
    import base64
    with open(imagen_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{
            "parts": [
                {"text": "Describe this background image in one short phrase (max 10 words), focus on setting/environment only."},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}}
            ]
        }]
    }
    try:
        r = requests.post(url, json=payload, timeout=60)
        r.raise_for_status()
        texto = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        return texto
    except Exception as e:
        log.error(f"Error describiendo imagen con Gemini: {e}")
        return ""


def actualizar_banco_fondos():
    """Sincroniza el banco de fondos y genera descripciones para las imágenes nuevas."""
    desc = cargar_descripciones()
    archivos = [a for a in rclone_lsf(CARPETA_FONDOS) if a.lower().endswith((".jpg", ".jpeg", ".png"))]
    nuevos = [a for a in archivos if a not in desc]
    if not nuevos:
        log.info(f"Banco de fondos: {len(archivos)} imágenes, sin nuevas por describir")
        return desc

    log.info(f"Banco de fondos: describiendo {len(nuevos)} imágenes nuevas")
    os.makedirs(os.path.join(WORK_DIR, "fondos"), exist_ok=True)
    for nombre in nuevos:
        local = os.path.join(WORK_DIR, "fondos", nombre)
        if rclone_copyto(f"{CARPETA_FONDOS}/{nombre}", local):
            descripcion = describir_imagen_gemini(local)
            if descripcion:
                desc[nombre] = descripcion
                log.info(f"  {nombre}: {descripcion}")
    guardar_descripciones(desc)
    return desc


# ---------- paso 5: prompt de escena + elegir fondo o generar nuevo (Groq) ----------

def groq_chat(mensajes, modelo="openai/gpt-oss-120b"):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": modelo, "messages": mensajes, "temperature": 0.7}
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def elegir_fondo_o_prompt(texto_escena, descripciones):
    lista = "\n".join(f"- {archivo}: {desc}" for archivo, desc in descripciones.items())
    instruccion = (
        "You choose a background image for an animated scene about prehistoric humans.\n"
        f"Available backgrounds:\n{lista}\n\n"
        f"Scene narration text: \"{texto_escena}\"\n\n"
        "Reply with ONLY a JSON object, no explanation:\n"
        '{"fondo": "<exact filename from the list, or null if none fit well>", '
        '"accion": "<short phrase describing what the stickman character is doing in this scene, in English>", '
        '"fondo_nuevo": "<if fondo is null, a short phrase describing the new background needed, else empty string>"}'
    )
    try:
        respuesta = groq_chat([{"role": "user", "content": instruccion}])
        respuesta = re.sub(r"^```json|```$", "", respuesta.strip(), flags=re.MULTILINE).strip()
        data = json.loads(respuesta)
        return data.get("fondo"), data.get("accion", "standing"), data.get("fondo_nuevo", "")
    except Exception as e:
        log.error(f"Error eligiendo fondo/acción con Groq: {e}")
        return None, "standing", ""


# ---------- paso 6: generar imagen con Pollinations ----------

PROMPT_ESTILO = (
    "2D cartoon illustration, a simple stickman-style prehistoric human wearing "
    "a tattered animal skin tunic, {accion}, {fondo_extra}"
    "The contrast between the simple flat stickman character and the "
    "realistically painted detailed background is key. No text, no watermark."
)
REFERENCIA_STICKMAN_NOMBRE = "referencia_stickman.jpg"  # debe existir en caverna-fondos


def generar_imagen_pollinations(prompt, imagen_referencia_path, salida_path):
    params = {"model": "kontext", "width": 1024, "height": 576}
    if POLLINATIONS_TOKEN:
        params["token"] = POLLINATIONS_TOKEN
    if imagen_referencia_path:
        params["image"] = imagen_referencia_path  # debe ser URL pública
    url = f"https://image.pollinations.ai/prompt/{quote(prompt)}"
    try:
        r = requests.get(url, params=params, timeout=120)
        r.raise_for_status()
        with open(salida_path, "wb") as f:
            f.write(r.content)
        return True
    except Exception as e:
        log.error(f"Error generando imagen con Pollinations: {e}")
        return False


# ---------- paso 7: armar video con ffmpeg ----------

def segundos_a_srt_ts(s):
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = int(s % 60)
    ms = int((s - int(s)) * 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def generar_srt(segmentos, ruta_srt):
    with open(ruta_srt, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segmentos, start=1):
            f.write(f"{i}\n")
            f.write(f"{segundos_a_srt_ts(seg['start'])} --> {segundos_a_srt_ts(seg['end'])}\n")
            f.write(f"{seg['text'].strip()}\n\n")


def armar_video(escenas, imagenes, audio_path, ruta_srt, salida_path):
    lista_path = os.path.join(WORK_DIR, "lista.txt")
    with open(lista_path, "w", encoding="utf-8") as f:
        for escena, img in zip(escenas, imagenes):
            duracion = escena["fin"] - escena["inicio"]
            f.write(f"file '{os.path.abspath(img)}'\n")
            f.write(f"duration {duracion}\n")
        # ffmpeg concat demuxer necesita repetir la última imagen sin duration
        f.write(f"file '{os.path.abspath(imagenes[-1])}'\n")

    video_sin_subs = os.path.join(WORK_DIR, "video_sin_subs.mp4")
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lista_path,
        "-i", audio_path, "-vsync", "vfr", "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-c:a", "aac", "-shortest", video_sin_subs
    ], check=True, capture_output=True)

    subprocess.run([
        "ffmpeg", "-y", "-i", video_sin_subs,
        "-vf", f"subtitles={ruta_srt}:force_style='FontSize=18,PrimaryColour=&HFFFFFF&'",
        "-c:a", "copy", salida_path
    ], check=True, capture_output=True)


# ---------- main ----------

def main():
    t0 = time.time()
    log.info("=== Inicio caverna-video ===")
    os.makedirs(WORK_DIR, exist_ok=True)

    nombre_audio = elegir_audio_pendiente()
    if not nombre_audio:
        log.info("No hay audios pendientes. Fin.")
        return
    base = nombre_audio[:-4]
    log.info(f"Procesando: {base}")

    audio_local = os.path.join(WORK_DIR, nombre_audio)
    if not rclone_copyto(f"{CARPETA_AUDIO}/{nombre_audio}", audio_local):
        return

    t1 = time.time()
    segmentos = transcribir(audio_local)
    log.info(f"Whisper: {len(segmentos)} segmentos, {time.time()-t1:.1f}s")

    escenas = armar_escenas(segmentos)
    log.info(f"Escenas armadas: {len(escenas)}")

    t2 = time.time()
    descripciones = actualizar_banco_fondos()
    log.info(f"Banco de fondos listo, {time.time()-t2:.1f}s")

    os.makedirs(os.path.join(WORK_DIR, "escenas"), exist_ok=True)
    imagenes = []
    t3 = time.time()
    for i, escena in enumerate(escenas, start=1):
        fondo, accion, fondo_nuevo = elegir_fondo_o_prompt(escena["texto"], descripciones)
        salida = os.path.join(WORK_DIR, "escenas", f"escena_{i:02d}.jpg")

        if fondo and fondo in descripciones:
            referencia_url = rclone_link(f"{CARPETA_FONDOS}/{fondo}")
            prompt = PROMPT_ESTILO.format(accion=accion, fondo_extra="")
            log.info(f"Escena {i}: usando fondo existente '{fondo}' | acción: {accion}")
        else:
            referencia_url = rclone_link(f"{CARPETA_FONDOS}/{REFERENCIA_STICKMAN_NOMBRE}")
            fondo_extra = f"background: {fondo_nuevo}. " if fondo_nuevo else ""
            prompt = PROMPT_ESTILO.format(accion=accion, fondo_extra=fondo_extra)
            log.info(f"Escena {i}: sin match, genera fondo nuevo ('{fondo_nuevo}') | acción: {accion}")

        ok = generar_imagen_pollinations(prompt, referencia_url, salida)
        if ok:
            imagenes.append(salida)
        else:
            log.warning(f"Escena {i} falló, se salta")
        time.sleep(5 if POLLINATIONS_TOKEN else 15)  # respeta rate limit

    log.info(f"Imágenes generadas: {len(imagenes)}/{len(escenas)}, {time.time()-t3:.1f}s")

    if not imagenes:
        log.error("No se generó ninguna imagen, no se puede armar el video")
        return

    rclone_copy(os.path.join(WORK_DIR, "escenas"), CARPETA_IMAGENES + f"/{base}")

    srt_path = os.path.join(WORK_DIR, f"{base}.srt")
    generar_srt(segmentos, srt_path)

    t4 = time.time()
    video_path = os.path.join(WORK_DIR, f"{base}.mp4")
    armar_video(escenas[:len(imagenes)], imagenes, audio_local, srt_path, video_path)
    log.info(f"Video armado, {time.time()-t4:.1f}s")

    if rclone_copyto(video_path, f"{CARPETA_VIDEOS}/{base}.mp4"):
        log.info(f"Subido: {base}.mp4 -> {CARPETA_VIDEOS}")

    log.info(f"=== Fin caverna-video, total {time.time()-t0:.1f}s ===")


if __name__ == "__main__":
    main()
