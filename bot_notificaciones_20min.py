"""
Bot Automático de Notificaciones SnapTik PRO (cada 20 minutos)
Ejecuta este script en tu PC para rotar los 10 banners oficiales automáticamente cada 20 min
y mantener despierto el servidor de Render 24/7.

Uso:
  python bot_notificaciones_20min.py
  python bot_notificaciones_20min.py --minutos 20
"""
import argparse
import json
import time
import urllib.request
from datetime import datetime

API_URL = "https://snaptik-backend.onrender.com/api/admin/push"
ADMIN_KEY = "Sn@pT1kAdmin#2026"

PRESETS = [
    {"tag": "4K Ultra HD", "title": "Vídeos en 4K Ultra HD Sin Marca de Agua", "body": "Conserva la nitidez original a 60 FPS sin logos molestos directo en tu galería.", "image_url": "https://snaptik-backend.onrender.com/static/banners/banner_01_4k_ultra_hd.jpg"},
    {"tag": "Audio MP3 320k", "title": "Extrae Audio MP3 a 320 kbps Original", "body": "Convierte canciones, audios virales y podcasts en un solo toque.", "image_url": "https://snaptik-backend.onrender.com/static/banners/banner_02_mp3_audio.jpg"},
    {"tag": "Carruseles HD", "title": "Descarga Álbumes Completos en HD", "body": "Guarda todas las fotos de un carrusel en un clic directo en tu galería.", "image_url": "https://snaptik-backend.onrender.com/static/banners/banner_03_photo_albums.jpg"},
    {"tag": "Turbo 24/7", "title": "Descargas Ultrarrápidas Sin Esperas", "body": "Servidores dedicados de alta velocidad activos las 24 horas sin límites.", "image_url": "https://snaptik-backend.onrender.com/static/banners/banner_04_turbo_speed.jpg"},
    {"tag": "Sin Conexión", "title": "Tus Vídeos Siempre Contigo Sin Internet", "body": "Accede a tus descargas offline con el reproductor integrado sin gastar datos.", "image_url": "https://snaptik-backend.onrender.com/static/banners/banner_05_offline_library.jpg"},
    {"tag": "Pegado Auto", "title": "Pegado Automático en 1 Solo Toque", "body": "Copia el enlace en TikTok y SnapTik PRO lo detecta al instante.", "image_url": "https://snaptik-backend.onrender.com/static/banners/banner_06_smart_paste.jpg"},
    {"tag": "Tendencias", "title": "Guárdalos Antes de Que Los Borren", "body": "No pierdas los vídeos en tendencia ni los audios que se vuelven virales.", "image_url": "https://snaptik-backend.onrender.com/static/banners/banner_07_trending_viral.jpg"},
    {"tag": "Diseño PRO", "title": "Experiencia Limpia Obsidian OLED", "body": "Interfaz oscura fluida estilo cristal líquido sin pantallas invasivas.", "image_url": "https://snaptik-backend.onrender.com/static/banners/banner_08_clean_design.jpg"},
    {"tag": "Actualización", "title": "SnapTik PRO Actualizado y Más Rápido", "body": "Mayor velocidad de descarga, alertas con Cha-Ching y máxima estabilidad.", "image_url": "https://snaptik-backend.onrender.com/static/banners/banner_09_new_version.jpg"},
    {"tag": "Comunidad", "title": "¿Te Gusta SnapTik PRO? ¡Compártelo!", "body": "Recomienda el descargador HD sin marcas de agua con tus amigos.", "image_url": "https://snaptik-backend.onrender.com/static/banners/banner_10_share_friends.jpg"},
]

def send_banner(preset):
    payload = json.dumps({
        "admin_key": ADMIN_KEY,
        "title": preset["title"],
        "body": preset["body"],
        "image_url": preset["image_url"],
        "url": "",
        "active": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "SnapTikAutoBot/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))

def main():
    parser = argparse.ArgumentParser(description="Bot Automático de Notificaciones SnapTik PRO")
    parser.add_argument("--minutos", type=int, default=20, help="Intervalo en minutos (por defecto: 20)")
    args = parser.parse_args()
    interval_sec = max(60, args.minutos * 60)

    print(f"=== BOT AUTOMÁTICO SNAPTIK PRO INICIADO (cada {args.minutos} min) ===")
    print("Presiona Ctrl+C para detenerlo.\n")

    idx = 0
    while True:
        preset = PRESETS[idx % len(PRESETS)]
        now_str = datetime.now().strftime("%H:%M:%S")
        try:
            res = send_banner(preset)
            nid = res.get("notification", {}).get("id", "ok")
            print(f"[{now_str}] ENVIADO #{(idx % len(PRESETS)) + 1}/10 ({preset['tag']}) -> ID: {nid}")
        except Exception as e:
            print(f"[{now_str}] ERROR al enviar ({preset['tag']}): {e}")
        idx += 1
        time.sleep(interval_sec)

if __name__ == "__main__":
    main()
