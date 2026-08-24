#!/usr/bin/env python3
"""
caverna-video v1.3
Flujo completo: toma un audio ya generado (caverna-audio), lo transcribe con
Whisper para tener timing exacto, arma escenas, genera/reutiliza imágenes
(stickman + fondo prehistórico) con Pollinations (con fallback a Hugging
Face FLUX si Pollinations falla), arma el video final con ffmpeg (imágenes +
audio + subtítulos quemados) y sube todo a Drive vía rclone.

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

GROQ_API_KEYS = [
    os.environ.get("GROQ_API_KEY_1", ""),
    os.environ.get("GROQ_API_KEY_2", ""),
    os.environ.get("GROQ_API_KEY_3", ""),
    os.environ.get("GROQ_API_KEY_4", ""),
]
GROQ_API_KEYS = [k for k in GROQ_API_KEYS if k]  # descarta vacías
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
POLLINATIONS_TOKEN = os.environ.get("POLLINATIONS_TOKEN", "")  # opcional
HF_API_TOKEN = os.environ.get("HF_API_TOKEN", "")  # fallback si Pollinations falla
LIMITE_DEMO_SEGUNDOS = int(os.environ.get("LIMITE_DEMO_SEGUNDOS", "0")) or None  # ej: 300 = 5 min

DURACION_ESCENA_OBJETIVO = 3  # segundos, objetivo (no fijo)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("caverna-video")


# ---------- utilidad de reintentos ----------

def con_reintentos(func, intentos=3, espera_base=5, nombre=""):
    """Corre func() hasta `intentos` veces, con espera creciente (5s, 10s, 20s...)
    entre cada intento. Devuelve el resultado o None si todos fallan."""
    for intento in range(1, intentos + 1):
        try:
            return func()
        except Exception as e:
            if intento == intentos:
                log.error(f"{nombre}: falló tras {intentos} intentos: {e}")
                return None
            espera = espera_base * (2 ** (intento - 1))
            log.warning(f"{nombre}: intento {intento}/{intentos} falló ({e}), reintenta en {espera}s")
            time.sleep(espera)


# ---------- utilidades rclone ----------

def rclone_lsf(ruta):
    r = subprocess.run(["rclone", "lsf", ruta], capture_output=True, text=True)
    if r.returncode != 0:
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
    # cubre tanto /d/XXXX/view como open?id=XXXX
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", link) or re.search(r"[?&]id=([a-zA-Z0-9_-]+)", link)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    log.warning(f"rclone link con formato no reconocido, se usa tal cual: {link}")
    return link


def recortar_audio_demo(audio_path, segundos):
    """Recorta el audio a los primeros `segundos` segundos, para pruebas."""
    recortado = audio_path.replace(".mp3", "_recorte.mp3")
    subprocess.run([
        "ffmpeg", "-y", "-i", audio_path, "-t", str(segundos),
        "-c", "copy", recortado
    ], check=True, capture_output=True)
    return recortado


# ---------- paso 1: elegir audio pendiente ----------

def elegir_audio_pendiente():
    audios = [a for a in rclone_lsf(CARPETA_AUDIO) if a.lower().endswith(".mp3")]
    listos = set(v[:-4] for v in rclone_lsf(CARPETA_VIDEOS) if v.lower().endswith(".mp4"))
    pendientes = [a for a in audios if a[:-4] not in listos]
    if not pendientes:
        return None
    return pendientes[0]


def elegir_audio_demo():
    """En modo demo siempre usa el mismo audio: lo guarda la primera vez en un
    marcador en Drive y lo reutiliza en corridas futuras, sin importar el
    estado de caverna-videos."""
    marcador_local = os.path.join(WORK_DIR, "demo_audio_elegido.txt")
    if rclone_copyto(f"{CARPETA_AUDIO}/.demo_audio_elegido.txt", marcador_local):
        with open(marcador_local, "r", encoding="utf-8") as f:
            nombre = f.read().strip()
        if nombre:
            log.info(f"Modo demo: reutilizando audio ya elegido antes: {nombre}")
            return nombre

    audios = [a for a in rclone_lsf(CARPETA_AUDIO) if a.lower().endswith(".mp3")]
    if not audios:
        return None
    nombre = audios[0]
    with open(marcador_local, "w", encoding="utf-8") as f:
        f.write(nombre)
    rclone_copyto(marcador_local, f"{CARPETA_AUDIO}/.demo_audio_elegido.txt")
    log.info(f"Modo demo: eligiendo audio por primera vez: {nombre}")
    return nombre


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
    if not GEMINI_API_KEY:
        return ""
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
    def _pedir():
        r = requests.post(url, json=payload, timeout=60)
        if not r.ok:
            log.error(f"Gemini respuesta cruda: status={r.status_code} body={r.text[:500]}")
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

    resultado = con_reintentos(_pedir, nombre="Gemini describir imagen")
    return resultado or ""


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
    """Prueba cada key de Groq en orden; si una da error (límite, etc.) pasa a la
    siguiente. Cada key individual también tiene sus propios reintentos."""
    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = {"model": modelo, "messages": mensajes, "temperature": 0.7}

    if not GROQ_API_KEYS:
        raise RuntimeError("No hay ninguna GROQ_API_KEY configurada")

    for idx, key in enumerate(GROQ_API_KEYS, start=1):
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

        def _pedir():
            r = requests.post(url, headers=headers, json=payload, timeout=60)
            if not r.ok:
                log.error(f"Groq key #{idx} respuesta cruda: status={r.status_code} body={r.text[:500]}")
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()

        resultado = con_reintentos(_pedir, intentos=2, espera_base=5, nombre=f"Groq key #{idx}")
        if resultado is not None:
            return resultado
        log.warning(f"Groq key #{idx} agotada/falló, prueba con la siguiente")

    raise RuntimeError("Groq no respondió con ninguna de las keys configuradas")


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
    modelo = "kontext" if imagen_referencia_path else "flux"
    params = {"model": modelo, "width": 1024, "height": 576}
    if imagen_referencia_path:
        params["image"] = imagen_referencia_path  # debe ser URL pública
    headers = {}
    if POLLINATIONS_TOKEN:
        headers["Authorization"] = f"Bearer {POLLINATIONS_TOKEN}"
    url = f"https://gen.pollinations.ai/image/{quote(prompt)}"

    def _pedir():
        r = requests.get(url, params=params, headers=headers, timeout=180)
        r.raise_for_status()
        with open(salida_path, "wb") as f:
            f.write(r.content)
        return True

    resultado = con_reintentos(_pedir, intentos=3, espera_base=15, nombre="Pollinations generar imagen")
    return bool(resultado)


# ---------- paso 6b: fallback con Hugging Face (FLUX Kontext) si Pollinations falla ----------

HF_MODELO = "black-forest-labs/FLUX.1-Kontext-dev"


def generar_imagen_huggingface(prompt, imagen_referencia_path, salida_path):
    """Fallback cuando Pollinations no responde. Usa FLUX.1-Kontext-dev vía
    huggingface_hub (proveedor fal-ai), que es el único que sirve este modelo
    ahora que HF descontinuó api-inference.huggingface.co."""
    if not HF_API_TOKEN:
        log.warning("HF_API_TOKEN no configurada, no se puede usar el fallback de Hugging Face")
        return False

    from huggingface_hub import InferenceClient
    client = InferenceClient(provider="fal-ai", api_key=HF_API_TOKEN)

    def _pedir():
        if imagen_referencia_path:
            img_r = requests.get(imagen_referencia_path, timeout=60)
            img_r.raise_for_status()
            imagen = client.image_to_image(
                img_r.content, prompt=prompt, model="black-forest-labs/FLUX.1-Kontext-dev"
            )
        else:
            imagen = client.text_to_image(
                prompt, model="black-forest-labs/FLUX.1-schnell"
            )
        imagen.save(salida_path)
        return True

    resultado = con_reintentos(_pedir, intentos=3, espera_base=20, nombre="Hugging Face generar imagen")
    return bool(resultado)


def generar_imagen(prompt, imagen_referencia_path, salida_path):
    """Intenta Pollinations primero; si falla, cae a Hugging Face (mismo
    modelo Kontext, respeta imagen de referencia)."""
    if generar_imagen_pollinations(prompt, imagen_referencia_path, salida_path):
        return True
    if not HF_API_TOKEN:
        return False

    log.warning("Pollinations falló, probando fallback con Hugging Face (Kontext)")
    return generar_imagen_huggingface(prompt, imagen_referencia_path, salida_path)


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

    nombre_audio = elegir_audio_demo() if LIMITE_DEMO_SEGUNDOS else elegir_audio_pendiente()
    if not nombre_audio:
        log.info("No hay audios pendientes. Fin.")
        return
    base = nombre_audio[:-4]
    log.info(f"Procesando: {base}")

    audio_local = os.path.join(WORK_DIR, nombre_audio)
    if not rclone_copyto(f"{CARPETA_AUDIO}/{nombre_audio}", audio_local):
        return

    if LIMITE_DEMO_SEGUNDOS:
        log.info(f"Modo demo: recortando a los primeros {LIMITE_DEMO_SEGUNDOS}s")
        audio_local = recortar_audio_demo(audio_local, LIMITE_DEMO_SEGUNDOS)

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

        ok = generar_imagen(prompt, referencia_url, salida)
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

    sufijo = "_demo" if LIMITE_DEMO_SEGUNDOS else ""
    srt_path = os.path.join(WORK_DIR, f"{base}{sufijo}.srt")
    generar_srt(segmentos, srt_path)

    t4 = time.time()
    video_path = os.path.join(WORK_DIR, f"{base}{sufijo}.mp4")
    armar_video(escenas[:len(imagenes)], imagenes, audio_local, srt_path, video_path)
    log.info(f"Video armado, {time.time()-t4:.1f}s")

    if rclone_copyto(video_path, f"{CARPETA_VIDEOS}/{base}{sufijo}.mp4"):
        log.info(f"Subido: {base}{sufijo}.mp4 -> {CARPETA_VIDEOS}")

    log.info(f"=== Fin caverna-video, total {time.time()-t0:.1f}s ===")


if __name__ == "__main__":
    main()
