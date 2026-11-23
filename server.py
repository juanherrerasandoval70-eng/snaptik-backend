import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.parse
from collections import defaultdict
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from starlette.staticfiles import StaticFiles
from pydantic import BaseModel
import httpx
import yt_dlp

app = FastAPI(
    title="SnapTik PRO Engine",
    version="3.6.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Accept", "X-App-Client", "X-App-Timestamp", "X-App-Nonce", "X-App-Signature"],
)

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if "server" in response.headers:
        del response.headers["server"]
    return response

UA_DESKTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
PROXY_URL = os.environ.get("PROXY_URL", "").strip() or None
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://snaptik-backend.onrender.com").strip()

# Blindaje Anti-Robo de Servidor + Anti-Bot + Anti-Replay + Auto-Ban
API_HMAC_SECRET = os.environ.get("API_HMAC_SECRET", "STPRO_2026_K9xV#mQ7pL2nR8wZ4jF6b").encode("utf-8")
ENFORCE_HMAC_AUTH = os.environ.get("ENFORCE_HMAC_AUTH", "true").lower() == "true"
RATE_LIMIT_MAX_PER_MIN = 25
rate_limit_buckets: Dict[str, List[float]] = defaultdict(list)
ip_strikes: Dict[str, int] = defaultdict(int)
banned_ips: Dict[str, float] = {}
used_nonces: Dict[str, float] = {}

BOT_UA_SIGNATURES = (
    "curl/", "wget/", "python-requests", "python-httpx", "scrapy", "httpclient",
    "postman", "insomnia", "burp", "sqlmap", "nikto", "nmap", "masscan",
    "headlesschrome", "phantomjs", "selenium", "playwright", "puppeteer",
    "bot", "crawler", "spider", "zgrab", "go-http-client", "libwww-perl"
)

def get_client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

def register_strike(ip: str, weight: int = 1):
    ip_strikes[ip] += weight
    if ip_strikes[ip] >= 5:
        banned_ips[ip] = time.time() + 1800.0  # Ban temporal de 30 minutos
        ip_strikes[ip] = 0
    if len(banned_ips) > 5000:
        now = time.time()
        for k in [k for k, exp in list(banned_ips.items()) if exp < now]:
            banned_ips.pop(k, None)

def check_bot_and_rate_limit(request: Request, strict_bot_check: bool = True):
    ip = get_client_ip(request)
    now = time.time()

    # 1. Comprobar lista de IPs baneadas por comportamiento abusivo / ataques
    ban_until = banned_ips.get(ip, 0.0)
    if ban_until > now:
        raise HTTPException(status_code=403, detail="Acceso bloqueado temporalmente por actividad sospechosa.")

    # 2. Bloqueo activo de bots, scrapers y herramientas de pentesting
    if strict_bot_check:
        ua = (request.headers.get("user-agent") or "").lower().strip()
        if not ua or any(sig in ua for sig in BOT_UA_SIGNATURES):
            register_strike(ip, 2)
            raise HTTPException(status_code=403, detail="Cliente automatizado no permitido.")

    # 3. Rate limiting por ventana deslizante de 60 segundos
    bucket = [t for t in rate_limit_buckets[ip] if (now - t) < 60.0]
    if len(bucket) >= RATE_LIMIT_MAX_PER_MIN:
        rate_limit_buckets[ip] = bucket
        register_strike(ip, 1)
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta en unos segundos.")
    bucket.append(now)
    rate_limit_buckets[ip] = bucket
    if len(rate_limit_buckets) > 5000:
        rate_limit_buckets.clear()

def check_rate_limit(request: Request):
    check_bot_and_rate_limit(request, strict_bot_check=True)

def verify_app_signature(request: Request, target_url: str):
    if not ENFORCE_HMAC_AUTH:
        return
    ip = get_client_ip(request)
    client_pkg = request.headers.get("x-app-client", "")
    ts_str = request.headers.get("x-app-timestamp", "")
    nonce = request.headers.get("x-app-nonce", "").strip()
    sig = request.headers.get("x-app-signature", "").lower().strip()

    ALLOWED_CLIENT_PACKAGES = ("com.snaptik.app", "com.snaptikpro.hd.app")
    if client_pkg not in ALLOWED_CLIENT_PACKAGES or not ts_str or not sig:
        register_strike(ip, 2)
        raise HTTPException(status_code=403, detail="Acceso denegado: cliente no autorizado.")

    try:
        ts_val = int(ts_str)
    except ValueError:
        register_strike(ip, 2)
        raise HTTPException(status_code=403, detail="Firma temporal inválida.")

    now_sec = int(time.time())
    if abs(now_sec - ts_val) > 180:
        register_strike(ip, 1)
        raise HTTPException(status_code=403, detail="Solicitud expirada.")

    # Anti-Replay: Si incluye X-App-Nonce, cada petición firmada es de un solo uso
    if nonce:
        if len(used_nonces) > 10000:
            cutoff = time.time() - 240.0
            for k in [k for k, t in list(used_nonces.items()) if t < cutoff]:
                used_nonces.pop(k, None)
        if nonce in used_nonces:
            register_strike(ip, 3)
            raise HTTPException(status_code=403, detail="Petición duplicada rechazada (Anti-Replay).")
        msg = f"{ts_str}:{nonce}:{target_url}".encode("utf-8")
    else:
        msg = f"{ts_str}:{target_url}".encode("utf-8")

    expected = hmac.new(API_HMAC_SECRET, msg, hashlib.sha256).hexdigest().lower()
    if not hmac.compare_digest(sig, expected):
        register_strike(ip, 3)
        raise HTTPException(status_code=403, detail="Firma criptográfica inválida.")

    if nonce:
        used_nonces[nonce] = time.time()

# Memoria Caché en RAM con expiración de 1 hora
CACHE_EXPIRY_SECONDS = 3600
cache_store: Dict[str, Dict[str, Any]] = {}

# Pool de conexiones optimizado de alta velocidad (reutilización HTTP Keep-Alive)
http_limits = httpx.Limits(max_keepalive_connections=60, max_connections=200)
shared_async_client: Optional[httpx.AsyncClient] = None

# Semáforo anti-OOM para yt-dlp (máximo 2 procesos pesados simultáneos en 512 MB RAM)
ytdlp_semaphore = asyncio.Semaphore(2)

# Deduplicación de peticiones en vuelo (In-Flight Request Coalescing)
in_flight_extractions: Dict[str, asyncio.Task] = {}

# Caché del token SSSTik (TTL 10 minutos -> ahorra 1 petición HTTP completa por extracción)
ssstik_token_cache: Dict[str, Any] = {"tt": None, "furl": "abc", "ts": 0.0}
SSSTIK_TOKEN_TTL = 600.0

def get_async_client() -> httpx.AsyncClient:
    global shared_async_client
    if shared_async_client is None or shared_async_client.is_closed:
        kwargs: Dict[str, Any] = {
            "timeout": httpx.Timeout(12.0, connect=6.0),
            "limits": http_limits,
            "headers": {"User-Agent": UA_DESKTOP},
            "follow_redirects": True,
        }
        if PROXY_URL:
            kwargs["proxy"] = PROXY_URL
        shared_async_client = httpx.AsyncClient(**kwargs)
    return shared_async_client

async def background_auto_maintenance():
    """Mantenimiento automático y Keep-Alive 24/7: Evita que Render duerma y purga RAM periódicamente."""
    while True:
        try:
            await asyncio.sleep(600)  # Cada 10 minutos
            if RENDER_EXTERNAL_URL:
                client = get_async_client()
                await client.get(f"{RENDER_EXTERNAL_URL}/api/health", timeout=15.0)

            now = time.time()
            expired_keys = [k for k, v in list(cache_store.items()) if (now - v["timestamp"] > CACHE_EXPIRY_SECONDS)]
            for k in expired_keys:
                cache_store.pop(k, None)
        except Exception:
            pass

@app.on_event("startup")
async def on_startup():
    get_async_client()
    asyncio.create_task(background_auto_maintenance())

@app.on_event("shutdown")
async def on_shutdown():
    global shared_async_client
    if shared_async_client is not None and not shared_async_client.is_closed:
        await shared_async_client.aclose()

class ExtractRequest(BaseModel):
    url: str

def clean_tiktok_url(raw: str) -> Optional[str]:
    if not raw:
        return None
    m = re.search(r"(https?://(?:[a-zA-Z0-9-]+\.)?(?:tiktok|douyin)\.com/[^\s\"']+)", raw)
    if m:
        u = m.group(1)
        q = u.find('?')
        return u[:q] if q > 0 else u
    return None

def format_mb(size_bytes: int) -> str:
    if not size_bytes or size_bytes <= 0:
        return ""
    return f"{size_bytes / (1024 * 1024):.1f} MB"

def resolve_with_ytdlp(clean_url: str) -> Optional[Dict[str, Any]]:
    ydl_opts = {
        'format': 'best',
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'skip_download': True,
        'user_agent': UA_DESKTOP,
    }
    if PROXY_URL:
        ydl_opts['proxy'] = PROXY_URL

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean_url, download=False)
            if not info:
                return None

            video_url = info.get('url') or ''
            audio_url = ''
            formats = info.get('formats', [])
            hd_url = video_url

            for f in formats:
                f_url = f.get('url', '')
                if not f_url:
                    continue
                if f.get('vcodec') == 'none' and f.get('acodec') != 'none':
                    if not audio_url:
                        audio_url = f_url
                height = f.get('height') or 0
                if height >= 1080 and f_url.startswith('http'):
                    hd_url = f_url

            images: List[str] = []
            entries = info.get('entries')
            if entries:
                for entry in entries:
                    t = entry.get('url') or entry.get('thumbnail')
                    if t:
                        images.append(t)

            is_photos = len(images) > 0
            author_name = info.get('uploader') or 'Creador de TikTok'
            unique_id = info.get('uploader_id') or author_name.lower().replace(' ', '_')

            return {
                "id": str(info.get('id') or int(time.time())),
                "source_url": clean_url,
                "title": info.get('title') or f"Vídeo de {author_name}",
                "preview_url": video_url if not is_photos else "",
                "video_url": video_url if not is_photos else "",
                "hd_url": hd_url if not is_photos else "",
                "uhd_4k_url": hd_url if not is_photos else "",
                "sd_url": video_url if not is_photos else "",
                "wm_url": "",
                "audio_url": audio_url or (video_url if not is_photos else ""),
                "music_title": f"Música de {author_name}",
                "cover_url": info.get('thumbnail') or "",
                "size_mb": "",
                "hd_size_mb": "",
                "duration": info.get('duration') or 0,
                "width": 1080,
                "height": 1920,
                "is_4k": False,
                "detected_quality": f"CARRUSEL ({len(images)} FOTOS)" if is_photos else "1080p FULL HD",
                "images": images if is_photos else [],
                "author": {
                    "nickname": author_name,
                    "unique_id": unique_id,
                    "avatar": info.get('thumbnail') or ""
                },
                "engine": "yt-dlp-native"
            }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return None

async def resolve_with_tikwm(clean_url: str) -> Optional[Dict[str, Any]]:
    api_url = f"https://www.tikwm.com/api/?url={urllib.parse.quote(clean_url)}&hd=1"
    try:
        client = get_async_client()
        r = await client.get(api_url, headers={"User-Agent": UA_DESKTOP})
        if r.status_code != 200:
            return None
        js = r.json()
        if js.get('code') != 0:
            return None

        d = js.get('data', {})
        play_url = d.get('play', '')
        if play_url.startswith("//"):
            play_url = "https:" + play_url
        elif play_url.startswith("/"):
            play_url = "https://www.tikwm.com" + play_url

        hd_play_url = d.get('hdplay') or play_url
        if hd_play_url.startswith("//"):
            hd_play_url = "https:" + hd_play_url
        elif hd_play_url.startswith("/"):
            hd_play_url = "https://www.tikwm.com" + hd_play_url

        wm_play_url = d.get('wmplay') or ''
        if wm_play_url.startswith("//"):
            wm_play_url = "https:" + wm_play_url
        elif wm_play_url.startswith("/"):
            wm_play_url = "https://www.tikwm.com" + wm_play_url

        if not play_url:
            play_url = hd_play_url

        images = d.get('images') or []
        if images:
            images = [("https:" + img if img.startswith("//") else ("https://www.tikwm.com" + img if img.startswith("/") else img)) for img in images]

        audio_url = d.get('music') or ''
        if audio_url.startswith("//"):
            audio_url = "https:" + audio_url
        elif audio_url.startswith("/"):
            audio_url = "https://www.tikwm.com" + audio_url

        music_info = d.get('music_info') or {}
        m_title = (music_info.get('title') or '').strip()
        m_author = (music_info.get('author') or '').strip()
        if m_title and m_author:
            music_full_title = f"{m_title} • {m_author}"
        elif m_title:
            music_full_title = m_title
        else:
            music_full_title = "Audio Original"

        if not audio_url and images and play_url:
            audio_url = play_url

        # Si es un carrusel de fotos y play_url es en realidad un stream de audio mpeg
        if images and ("audio_mpeg" in play_url or "mime_type=audio" in play_url):
            if not audio_url:
                audio_url = play_url
            play_url = ""
            hd_play_url = ""

        author_obj = d.get('author') or {}
        nickname = author_obj.get('nickname') or 'Creador de TikTok'
        unique_id = author_obj.get('unique_id') or 'tiktok_user'
        avatar = author_obj.get('avatar') or ''
        if avatar.startswith("//"):
            avatar = "https:" + avatar
        elif avatar.startswith("/"):
            avatar = "https://www.tikwm.com" + avatar

        cover = d.get('origin_cover') or d.get('ai_dynamic_cover') or d.get('cover') or avatar
        if cover.startswith("//"):
            cover = "https:" + cover
        elif cover.startswith("/"):
            cover = "https://www.tikwm.com" + cover
        raw_cover = cover if cover != avatar else ""

        size_bytes = d.get('size') or 0
        hd_size_bytes = d.get('hd_size') or size_bytes
        duration = d.get('duration') or 0
        width = d.get('width') or 0
        height = d.get('height') or 0
        max_dim = max(width, height)
        min_dim = min(width, height)

        is_4k = bool(not images and (
            max_dim >= 2560 or min_dim >= 1440 or
            (hd_size_bytes > 0 and duration > 0 and (hd_size_bytes / max(1, duration)) > 550000) or
            hd_size_bytes > 18 * 1024 * 1024
        ))

        if images:
            detected_quality = f"CARRUSEL ({len(images)} FOTOS + MÚSICA)"
        elif is_4k:
            detected_quality = "4K UHD DETECTADO"
        elif hd_size_bytes > size_bytes or max_dim >= 1920:
            detected_quality = "1080p FULL HD"
        else:
            detected_quality = "720p HD"

        return {
            "id": str(d.get('id') or int(time.time())),
            "source_url": clean_url,
            "title": (d.get('title') or (f"Carrusel de {nickname}" if images else f"Vídeo de {nickname}")).strip(),
            "preview_url": play_url,
            "video_url": play_url,
            "hd_url": hd_play_url or play_url,
            "uhd_4k_url": hd_play_url or play_url,
            "sd_url": play_url,
            "wm_url": wm_play_url,
            "audio_url": audio_url,
            "music_title": music_full_title,
            "cover_url": cover,
            "raw_cover_url": raw_cover,
            "size_mb": format_mb(size_bytes),
            "hd_size_mb": format_mb(hd_size_bytes),
            "duration": duration,
            "width": width,
            "height": height,
            "is_4k": is_4k,
            "detected_quality": detected_quality,
            "images": images,
            "author": {
                "nickname": nickname,
                "unique_id": unique_id,
                "avatar": avatar
            },
            "engine": "tikwm-async-pool"
        }
    except Exception:
        import traceback
        traceback.print_exc()
        return None

async def get_ssstik_token(force_refresh: bool = False) -> Optional[tuple]:
    now = time.time()
    if (not force_refresh and ssstik_token_cache["tt"]
            and (now - ssstik_token_cache["ts"] < SSSTIK_TOKEN_TTL)):
        return ssstik_token_cache["tt"], ssstik_token_cache["furl"]

    client = get_async_client()
    home = await client.get("https://ssstik.io/es")
    tt_m = re.search(r"s_tt\s*=\s*'([^']+)'", home.text)
    furl_m = re.search(r"s_furl\s*=\s*'([^']+)'", home.text)
    if not tt_m:
        return None
    s_tt = tt_m.group(1)
    s_furl = furl_m.group(1) if furl_m else "abc"
    ssstik_token_cache["tt"] = s_tt
    ssstik_token_cache["furl"] = s_furl
    ssstik_token_cache["ts"] = now
    return s_tt, s_furl

async def resolve_with_ssstik(clean_url: str) -> Optional[Dict[str, Any]]:
    try:
        client = get_async_client()
        tok = await get_ssstik_token(force_refresh=False)
        if not tok:
            return None
        s_tt, s_furl = tok

        post_headers = {
            "HX-Request": "true",
            "HX-Target": "target",
            "HX-Current-URL": "https://ssstik.io/es",
            "Origin": "https://ssstik.io",
            "Referer": "https://ssstik.io/es",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
        }
        res = await client.post(
            f"https://ssstik.io/{s_furl}?url=dl",
            data={"id": clean_url, "locale": "es", "tt": s_tt},
            headers=post_headers
        )
        html = res.text

        # Si el token expiró, refrescar 1 vez automáticamente
        if "tikcdn.io" not in html and "slides_data" not in html and "data-splide-lazy" not in html:
            tok = await get_ssstik_token(force_refresh=True)
            if not tok:
                return None
            s_tt, s_furl = tok
            res = await client.post(
                f"https://ssstik.io/{s_furl}?url=dl",
                data={"id": clean_url, "locale": "es", "tt": s_tt},
                headers=post_headers
            )
            html = res.text

        v_m = re.search(r"href=[\"'](https?://tikcdn\.io/ssstik/(?!m/)[^\"']+)[\"']", html)
        prev_m = re.search(r"ssrPreview\(['\"](https?://tikcdn\.io/ssstik/v/[^'\"]+)['\"]\)", html)
        a_m = re.search(r"href=[\"'](https?://tikcdn\.io/ssstik/m/[^\"']+)[\"']", html)

        video_url = v_m.group(1) if v_m else ""
        preview_url = prev_m.group(1) if prev_m else video_url
        audio_url = a_m.group(1) if a_m else ""

        images = re.findall(r"data-splide-lazy=[\"'](https?://[^\"']+)[\"']", html)

        # Extraer carrusel de fotos completo desde slides_data si está presente
        m_slides = re.search(r'name=["\']?slides_data["\']?[^>]*value=["\'](.*?)["\']', html)
        if m_slides:
            try:
                raw_b64 = m_slides.group(1)
                slides_dict = json.loads(base64.b64decode(raw_b64).decode('utf-8'))
                extracted_imgs = []
                for k in sorted(slides_dict.keys(), key=lambda x: int(x) if x.isdigit() else 9999):
                    if k.isdigit() and isinstance(slides_dict[k], dict):
                        slide_img = slides_dict[k].get("url")
                        if slide_img:
                            if not slide_img.startswith("https://tikcdn.io/"):
                                b64_img = base64.b64encode(slide_img.encode('utf-8')).decode('utf-8')
                                extracted_imgs.append(f"https://tikcdn.io/ssstik/{b64_img}")
                            else:
                                extracted_imgs.append(slide_img)
                if extracted_imgs:
                    images = extracted_imgs
                if not audio_url and slides_dict.get("music"):
                    raw_music = slides_dict.get("music")
                    if raw_music and not raw_music.startswith("https://tikcdn.io/"):
                        b64_music = base64.b64encode(raw_music.encode('utf-8')).decode('utf-8')
                        audio_url = f"https://tikcdn.io/ssstik/m/{b64_music}"
                    else:
                        audio_url = raw_music
            except Exception:
                pass

        # También buscar enlaces directos de descarga de diapositivas
        slide_anchors = re.findall(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*slide[^"\']*["\']', html)
        for sa in slide_anchors:
            if sa not in images:
                images.append(sa)

        # Verificar si video_url es en realidad una imagen de diapositiva o photomode
        is_video_actually_image = False
        if video_url:
            lower_v = video_url.lower()
            if "photomode" in lower_v or lower_v.endswith(".webp") or lower_v.endswith(".jpg") or lower_v.endswith(".png") or lower_v.endswith(".jpeg"):
                is_video_actually_image = True
            elif "/ssstik/ahr0" in lower_v or "/ssstik/a/" in lower_v:
                try:
                    b64_part = video_url.split("/ssstik/")[-1].split("?")[0]
                    if b64_part.startswith("a/"):
                        b64_part = b64_part[2:]
                    pad = len(b64_part) % 4
                    if pad:
                        b64_part += "=" * (4 - pad)
                    dec = base64.b64decode(b64_part).decode('utf-8', errors='ignore').lower()
                    if "photomode" in dec or ".webp" in dec or ".jpg" in dec or ".png" in dec or ".jpeg" in dec:
                        is_video_actually_image = True
                except Exception:
                    pass

        if is_video_actually_image:
            if not images:
                images.append(video_url)
            video_url = ""

        video_url = video_url if not is_video_actually_image else ""

        if not video_url and not images:
            return None

        name_m = re.search(r"<h2>(.*?)</h2>", html)
        nickname = re.sub(r"<[^>]+>", "", name_m.group(1)).strip() if name_m else "Creador de TikTok"

        desc_m = re.search(r"<p[^>]*class=[\"'][^\"']*(?:ssr-caption|maintext)[^\"']*[\"'][^>]*>(.*?)</p>", html, re.DOTALL)
        desc = re.sub(r"<[^>]+>", "", desc_m.group(1)).strip() if desc_m else f"Vídeo de {nickname}"

        ava_m = re.search(r'<img[^>]*class=[\'"][^\'"]*(?:ssr-ava|result_author)[^\'"]*[\'"][^>]*src=[\'"]([^\'"]+)[\'"]', html)
        avatar_url = ava_m.group(1) if ava_m else ""

        cov_m = re.search(r"url\(['\"]?(https?://tikcdn\.io/ssstik/p/[^'\"\)]+)['\"]?\)", html)
        raw_cover_url = cov_m.group(1) if cov_m else (images[0] if len(images) > 0 else "")

        is_photos = len(images) > 0
        return {
            "id": str(int(time.time())),
            "source_url": clean_url,
            "title": desc,
            "preview_url": (preview_url or video_url) if not is_photos else "",
            "video_url": video_url if not is_photos else "",
            "hd_url": video_url if not is_photos else "",
            "uhd_4k_url": video_url if not is_photos else "",
            "sd_url": video_url if not is_photos else "",
            "wm_url": "",
            "audio_url": audio_url,
            "music_title": f"Música de {nickname}",
            "cover_url": raw_cover_url or avatar_url,
            "raw_cover_url": raw_cover_url,
            "size_mb": "",
            "hd_size_mb": "",
            "duration": 0,
            "width": 1080,
            "height": 1920,
            "is_4k": False,
            "detected_quality": f"CARRUSEL ({len(images)} FOTOS)" if is_photos else "1080p FULL HD",
            "images": images,
            "author": {
                "nickname": nickname,
                "unique_id": nickname.lower().replace(" ", "_"),
                "avatar": avatar_url
            },
            "engine": "ssstik-proxy"
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return None

@app.get("/")
def home():
    return {
        "status": "online",
        "service": "SnapTik PRO Private Backend Engine",
        "version": "1.0.0",
        "cached_items": len(cache_store),
        "docs": "/docs",
        "timestamp": int(time.time())
    }

@app.get("/app-ads.txt", response_class=PlainTextResponse)
def serve_app_ads_txt():
    ads_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app-ads.txt")
    if os.path.exists(ads_path):
        with open(ads_path, "r", encoding="utf-8") as f:
            return PlainTextResponse(content=f.read(), media_type="text/plain; charset=utf-8")
    raise HTTPException(status_code=404, detail="app-ads.txt not found")

@app.get("/privacy", response_class=HTMLResponse)
def privacy_policy():
    return """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Política de Privacidad — SnapTik PRO</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;background:#08080c;color:#e8e8f0;max-width:740px;margin:0 auto;padding:32px 20px;line-height:1.65}h1,h2{color:#fff}a{color:#00e5ff}</style></head>
<body><h1>Política de Privacidad / Privacy Policy — SnapTik PRO</h1>
<p><strong>Última actualización:</strong> Noviembre 2026</p>
<h2>1. Datos que recopilamos / Data We Collect</h2>
<p>SnapTik PRO (<code>com.snaptik.app</code>) no solicita registro ni recopila nombres, correos, contraseñas ni archivos personales. El historial de descargas se guarda exclusivamente de forma local en tu dispositivo.</p>
<h2>2. Publicidad / Advertising</h2>
<p>Utilizamos el SDK de <strong>Start.io</strong> para mostrar anuncios que financian el servidor gratuito. Start.io puede utilizar el Identificador de Publicidad de Android (GAID) conforme a su propia <a href="https://www.start.io/policy/privacy-policy/">Política de Privacidad</a>. Puedes restablecer o limitar el seguimiento en Ajustes de Google de tu dispositivo.</p>
<h2>3. Permisos / Permissions</h2>
<ul><li><strong>Internet / Red:</strong> para resolver enlaces multimedia y mostrar anuncios.</li><li><strong>Almacenamiento / Galería:</strong> únicamente para guardar los archivos que tú solicitas descargar.</li></ul>
<h2>4. Derechos de Autor / Copyright</h2>
<p>El usuario es responsable de respetar los derechos de autor y descargar únicamente contenido propio o autorizado.</p>
<h2>5. Contacto / Contact</h2>
<p>Para soporte o solicitudes de privacidad, escríbenos a través del repositorio oficial de soporte.</p></body></html>"""

# Panel Online de Notificaciones Push (protegido por contraseña de administrador)
NOTIF_ADMIN_KEY = os.environ.get("NOTIF_ADMIN_KEY", "Sn@pT1kAdmin#2026").strip()
NOTIF_STORE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "active_notification.json")

active_notification_state: Dict[str, Any] = {
    "active": False,
    "id": "init_0",
    "title": "",
    "body": "",
    "image_url": "",
    "url": "",
    "updated_at": 0
}

def _load_saved_notification():
    global active_notification_state
    try:
        if os.path.exists(NOTIF_STORE_FILE):
            with open(NOTIF_STORE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    active_notification_state.update(data)
    except Exception:
        pass

_load_saved_notification()

notif_subscribers: List[asyncio.Queue] = []

def _broadcast_notification_now(payload: Dict[str, Any]):
    dead = []
    for q in list(notif_subscribers):
        try:
            q.put_nowait(dict(payload))
        except Exception:
            dead.append(q)
    for q in dead:
        try:
            notif_subscribers.remove(q)
        except ValueError:
            pass

class PushBroadcastRequest(BaseModel):
    admin_key: str
    title: str
    body: str
    image_url: Optional[str] = ""
    url: Optional[str] = ""
    active: bool = True

@app.get("/api/notifications")
def get_remote_notifications():
    """Devuelve la notificación global activa para todos los teléfonos con SnapTik PRO."""
    return JSONResponse(
        content=active_notification_state,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
    )

@app.get("/api/notifications/stream")
async def stream_remote_notifications(request: Request):
    """Stream SSE en tiempo real: entrega notificaciones en <1 segundo al presionar Enviar."""
    q: asyncio.Queue = asyncio.Queue(maxsize=16)
    notif_subscribers.append(q)

    async def event_gen():
        try:
            if active_notification_state.get("active"):
                yield f"data: {json.dumps(active_notification_state, ensure_ascii=False)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(q.get(), timeout=25.0)
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            try:
                notif_subscribers.remove(q)
            except ValueError:
                pass

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

@app.post("/api/admin/push")
def publish_remote_notification(req: PushBroadcastRequest, request: Request):
    ip = get_client_ip(request)
    if not hmac.compare_digest(req.admin_key.strip(), NOTIF_ADMIN_KEY):
        register_strike(ip, 3)
        raise HTTPException(status_code=403, detail="Clave de administrador incorrecta.")

    new_id = f"push_{int(time.time())}"
    active_notification_state.update({
        "active": bool(req.active),
        "id": new_id,
        "title": req.title.strip()[:100],
        "body": req.body.strip()[:300],
        "image_url": (req.image_url or "").strip()[:500],
        "url": (req.url or "").strip()[:300],
        "updated_at": int(time.time())
    })
    try:
        with open(NOTIF_STORE_FILE, "w", encoding="utf-8") as f:
            json.dump(active_notification_state, f, ensure_ascii=False)
    except Exception:
        pass
    if active_notification_state["active"]:
        _broadcast_notification_now(active_notification_state)
    return {"success": True, "notification": active_notification_state, "live_listeners": len(notif_subscribers)}

# ==================== BOT AUTOMÁTICO DE NOTIFICACIONES (CADA 20 MIN) ====================
AUTO_BOT_PRESETS = [
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

auto_bot_state: Dict[str, Any] = {
    "enabled": False,
    "interval_minutes": 20,
    "current_index": 0,
    "last_sent_at": 0,
    "next_send_at": 0,
    "total_sent": 0,
}

def _publish_next_bot_banner():
    idx = auto_bot_state["current_index"] % len(AUTO_BOT_PRESETS)
    preset = AUTO_BOT_PRESETS[idx]
    now = int(time.time())
    active_notification_state.update({
        "active": True,
        "id": f"bot_{now}_{idx}",
        "title": preset["title"],
        "body": preset["body"],
        "image_url": preset["image_url"],
        "url": "",
        "updated_at": now,
    })
    try:
        with open(NOTIF_STORE_FILE, "w", encoding="utf-8") as f:
            json.dump(active_notification_state, f, ensure_ascii=False)
    except Exception:
        pass
    auto_bot_state["current_index"] = (idx + 1) % len(AUTO_BOT_PRESETS)
    auto_bot_state["last_sent_at"] = now
    auto_bot_state["next_send_at"] = now + int(auto_bot_state["interval_minutes"]) * 60
    auto_bot_state["total_sent"] += 1
    _broadcast_notification_now(active_notification_state)
    return active_notification_state

async def _auto_bot_loop():
    while True:
        try:
            await asyncio.sleep(15)
            if auto_bot_state["enabled"]:
                now = int(time.time())
                if auto_bot_state["next_send_at"] <= now:
                    _publish_next_bot_banner()
        except Exception:
            pass

@app.on_event("startup")
async def _start_auto_bot_task():
    asyncio.create_task(_auto_bot_loop())

class BotConfigRequest(BaseModel):
    admin_key: str
    enabled: bool = True
    interval_minutes: int = 20
    send_now: bool = False

@app.get("/api/admin/bot/status")
def get_bot_status():
    now = int(time.time())
    rem = max(0, auto_bot_state["next_send_at"] - now) if auto_bot_state["enabled"] else 0
    return {
        **auto_bot_state,
        "seconds_until_next": rem,
        "total_presets": len(AUTO_BOT_PRESETS),
        "next_preset_tag": AUTO_BOT_PRESETS[auto_bot_state["current_index"] % len(AUTO_BOT_PRESETS)]["tag"],
    }

@app.post("/api/admin/bot")
def configure_auto_bot(req: BotConfigRequest, request: Request):
    ip = get_client_ip(request)
    if not hmac.compare_digest(req.admin_key.strip(), NOTIF_ADMIN_KEY):
        register_strike(ip, 3)
        raise HTTPException(status_code=403, detail="Clave de administrador incorrecta.")
    interval = max(1, min(1440, int(req.interval_minutes)))
    auto_bot_state["interval_minutes"] = interval
    auto_bot_state["enabled"] = bool(req.enabled)
    now = int(time.time())
    if req.send_now or (req.enabled and auto_bot_state["last_sent_at"] == 0):
        _publish_next_bot_banner()
    elif req.enabled:
        auto_bot_state["next_send_at"] = now + interval * 60
    else:
        auto_bot_state["next_send_at"] = 0
    return {"success": True, "bot": get_bot_status(), "notification": active_notification_state}

@app.get("/admin/notificaciones", response_class=HTMLResponse)
def admin_notifications_panel():
    return """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Panel de Notificaciones — SnapTik PRO</title>
<style>
*{box-sizing:border-box;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
body{margin:0;min-height:100vh;background:#050608;color:#fff;display:flex;align-items:center;justify-content:center;padding:24px 14px;
background-image:radial-gradient(circle at 10% 25%,rgba(36,212,238,.18),transparent 45%),radial-gradient(circle at 90% 75%,rgba(255,108,64,.16),transparent 45%)}
.card{width:100%;max-width:540px;background:rgba(24,28,38,.9);border:1px solid rgba(255,255,255,.15);border-radius:24px;padding:26px;backdrop-filter:blur(22px);box-shadow:0 24px 60px rgba(0,0,0,.7)}
h1{margin:0 0 6px;font-size:1.35rem;letter-spacing:-.3px}p{margin:0 0 16px;color:rgba(255,255,255,.6);font-size:.85rem;line-height:1.4}
label{display:block;font-size:.78rem;font-weight:700;color:rgba(255,255,255,.84);margin:14px 0 6px}
input,textarea,select{width:100%;padding:11px 14px;border-radius:14px;border:1px solid rgba(255,255,255,.14);background:rgba(10,11,15,.88);color:#fff;font-size:.88rem;outline:none}
input:focus,textarea:focus,select:focus{border-color:#38EFEE}
textarea{min-height:72px;resize:vertical}
.preset-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-top:6px;max-height:220px;overflow-y:auto;padding-right:4px}
.preset-btn{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.12);border-radius:12px;padding:8px 10px;text-align:left;color:#fff;cursor:pointer;display:flex;align-items:center;gap:8px;transition:background .2s}
.preset-btn:hover{background:rgba(56,239,238,.15);border-color:#38EFEE}
.preset-btn img{width:40px;height:24px;object-fit:cover;border-radius:5px}
.preset-info{overflow:hidden}
.preset-info b{display:block;font-size:.74rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#E2E8F0}
.preset-info span{display:block;font-size:.65rem;color:#38EFEE}
.preview-box{margin-top:12px;padding:10px;border-radius:16px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.1);display:none}
.preview-box img{width:100%;max-height:180px;object-fit:cover;border-radius:10px;display:block}
button.submit-btn{width:100%;margin-top:16px;padding:14px;border:none;border-radius:999px;background:linear-gradient(180deg,#fff,#E4FAFF);color:#06080B;font-size:.95rem;font-weight:800;cursor:pointer}
.btn-off{width:100%;margin-top:10px;padding:12px;background:rgba(255,255,255,.08);color:#fff;border:1px solid rgba(255,255,255,.14);border-radius:999px;font-weight:600;cursor:pointer}
.msg{margin-top:14px;padding:11px 14px;border-radius:12px;font-size:.82rem;display:none}
.ok{background:rgba(16,185,129,.16);border:1px solid rgba(16,185,129,.4);color:#6EE7B7}
.err{background:rgba(255,46,99,.16);border:1px solid rgba(255,46,99,.4);color:#FDA4AF}
.status-bar{margin-top:14px;padding:10px 12px;border-radius:12px;background:rgba(56,239,238,.08);border:1px solid rgba(56,239,238,.22);font-size:.76rem;color:#BAE6FD}
</style></head><body><div class="card">
<h1>Enviar Notificación con Banner</h1>
<p>Envía un anuncio o banner oficial a todos los celulares con SnapTik PRO.</p>
<label>Elige una plantilla lista (10 Banners Oficiales)</label>
<div class="preset-grid" id="presets"></div>
<label>Clave de Administrador</label>
<input id="key" type="password" placeholder="Ingresa tu clave privada">
<label>Título de la notificación</label>
<input id="title" type="text" maxlength="100" placeholder="Ej: ¡Vídeos en 4K Ultra HD!">
<label>Mensaje</label>
<textarea id="body" maxlength="300" placeholder="Escribe el mensaje..."></textarea>
<label>URL de Imagen / Banner grande (JPG o PNG)</label>
<input id="img" type="url" placeholder="https://..." oninput="updatePreview()">
<div id="pbox" class="preview-box"><img id="pimg" alt="Vista previa del banner"></div>
<label>Enlace al tocar (opcional, déjalo vacío para abrir SnapTik PRO)</label>
<input id="url" type="url" placeholder="https://... (o vacío)">
<button class="submit-btn" onclick="sendPush(true)">Enviar a Todos Ahora</button>
<button class="btn-off" onclick="sendPush(false)">Desactivar Notificación Actual</button>
<div style="margin-top:18px;padding:14px;border-radius:16px;background:rgba(56,239,238,.06);border:1px solid rgba(56,239,238,.25)">
  <div style="font-weight:800;font-size:.88rem;color:#38EFEE;margin-bottom:6px">Bot Automático (Rotación de 10 Banners)</div>
  <div style="display:flex;gap:8px;align-items:center">
    <input id="botMin" type="number" min="1" max="1440" value="20" style="width:90px;padding:9px 10px">
    <span style="font-size:.8rem;color:rgba(255,255,255,.75)">minutos</span>
    <button onclick="setBot(true,false)" style="flex:1;padding:10px;border:none;border-radius:999px;background:#38EFEE;color:#050608;font-weight:800;cursor:pointer">Activar Bot</button>
    <button onclick="setBot(false,false)" style="padding:10px 14px;border-radius:999px;background:rgba(255,255,255,.1);color:#fff;border:1px solid rgba(255,255,255,.2);cursor:pointer">Pausar</button>
  </div>
  <div id="botStatus" style="margin-top:8px;font-size:.74rem;color:#BAE6FD">Cargando estado del bot...</div>
</div>
<div id="msg" class="msg"></div>
<div id="cur" class="status-bar">Cargando estado actual...</div>
</div><script>
const PRESETS = [
  {id:"01",tag:"4K Ultra HD",title:"⚡ ¡Vídeos en 4K Ultra HD Sin Marca de Agua!",body:"Conserva la nitidez original a 60 FPS sin logos molestos en tus descargas.",img:"https://snaptik-backend.onrender.com/static/banners/banner_01_4k_ultra_hd.jpg"},
  {id:"02",tag:"Audio MP3 320k",title:"🎵 Extrae Audio MP3 a 320 kbps Original",body:"Convierte canciones, audios virales y podcasts en un solo toque.",img:"https://snaptik-backend.onrender.com/static/banners/banner_02_mp3_audio.jpg"},
  {id:"03",tag:"Carruseles",title:"📸 Descarga Álbumes Completos en HD",body:"Guarda todas las fotos de un carrusel en un clic directo en tu galería.",img:"https://snaptik-backend.onrender.com/static/banners/banner_03_photo_albums.jpg"},
  {id:"04",tag:"Turbo 24/7",title:"🚀 Descargas Ultrarrápidas Sin Esperas",body:"Servidores dedicados de alta velocidad activos las 24 horas sin límites.",img:"https://snaptik-backend.onrender.com/static/banners/banner_04_turbo_speed.jpg"},
  {id:"05",tag:"Sin Conexión",title:"📱 Tus Vídeos Siempre Contigo Sin Internet",body:"Accede a tus descargas offline con el reproductor integrado sin gastar datos.",img:"https://snaptik-backend.onrender.com/static/banners/banner_05_offline_library.jpg"},
  {id:"06",tag:"Pegado Auto",title:"📋 Pegado Automático en 1 Solo Toque",body:"Copia el enlace en TikTok y SnapTik PRO lo detecta al instante.",img:"https://snaptik-backend.onrender.com/static/banners/banner_06_smart_paste.jpg"},
  {id:"07",tag:"Tendencias",title:"🔥 ¡Guárdalos Antes de Que Los Borren!",body:"No pierdas los vídeos en tendencia ni los audios que se vuelven virales.",img:"https://snaptik-backend.onrender.com/static/banners/banner_07_trending_viral.jpg"},
  {id:"08",tag:"Diseño Limpio",title:"✨ Experiencia Limpia Estilo iOS 18 Glass",body:"Interfaz fluida, acabado de cristal esmerilado y modo oscuro OLED.",img:"https://snaptik-backend.onrender.com/static/banners/banner_08_clean_design.jpg"},
  {id:"09",tag:"Actualización",title:"🎉 ¡Nueva Versión v3.9.3 Disponible!",body:"Mayor velocidad de descarga, compatibilidad mejorada y máxima estabilidad.",img:"https://snaptik-backend.onrender.com/static/banners/banner_09_new_version.jpg"},
  {id:"10",tag:"Comunidad",title:"❤️ ¿Te Gusta SnapTik PRO? ¡Compártelo!",body:"Recomienda la mejor app de descarga con amigos y creadores de contenido.",img:"https://snaptik-backend.onrender.com/static/banners/banner_10_share_friends.jpg"}
];
function renderPresets(){
  const container = document.getElementById('presets');
  container.innerHTML = PRESETS.map((p, idx) => `
    <div class="preset-btn" onclick="applyPreset(${idx})">
      <img src="${p.img}" alt="${p.tag}">
      <div class="preset-info">
        <b>${p.tag}</b>
        <span>Banner #${p.id}</span>
      </div>
    </div>
  `).join('');
}
function applyPreset(idx){
  const p = PRESETS[idx];
  document.getElementById('title').value = p.title;
  document.getElementById('body').value = p.body;
  document.getElementById('img').value = p.img;
  updatePreview();
}
function updatePreview(){
  const u=document.getElementById('img').value.trim();
  const b=document.getElementById('pbox'),i=document.getElementById('pimg');
  if(u.startsWith('http')){i.src=u;b.style.display='block';}else{b.style.display='none';}
}
async function loadCurrent(){
  try{
    const r=await fetch('/api/notifications');const j=await r.json();
    document.getElementById('cur').textContent=j.active?('ACTIVA AHORA: "'+j.title+'"'+(j.image_url?' [CON BANNER]':'')):'Sin notificación activa.';
  }catch(e){}
}
async function sendPush(active){
  const m=document.getElementById('msg');m.style.display='none';
  const payload={admin_key:document.getElementById('key').value,title:document.getElementById('title').value,body:document.getElementById('body').value,image_url:document.getElementById('img').value,url:document.getElementById('url').value,active};
  if(active && (!payload.title.trim()||!payload.body.trim())){m.className='msg err';m.textContent='Escribe título y mensaje.';m.style.display='block';return;}
  try{
    const r=await fetch('/api/admin/push',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const j=await r.json();
    if(r.ok){m.className='msg ok';m.textContent=active?('✓ Publicada en vivo (ID: '+j.notification.id+')'):'✓ Notificación desactivada.';loadCurrent();}
    else{m.className='msg err';m.textContent=j.detail||'Error al enviar.';}
  }catch(e){m.className='msg err';m.textContent='Error de conexión.';}
  m.style.display='block';
}
async function loadBot(){
  try{
    const r=await fetch('/api/admin/bot/status');const b=await r.json();
    const el=document.getElementById('botStatus');
    if(b.enabled){
      const mins=Math.ceil(b.seconds_until_next/60);
      el.textContent='BOT ACTIVO cada '+b.interval_minutes+' min • Enviados: '+b.total_sent+' • Siguiente: '+b.next_preset_tag+' (en ~'+mins+' min)';
    }else{
      el.textContent='Bot en pausa • Próximo banner: '+b.next_preset_tag;
    }
  }catch(e){}
}
async function setBot(enabled, sendNow){
  const m=document.getElementById('msg');m.style.display='none';
  const payload={admin_key:document.getElementById('key').value,enabled,interval_minutes:parseInt(document.getElementById('botMin').value||'20',10),send_now:sendNow};
  try{
    const r=await fetch('/api/admin/bot',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const j=await r.json();
    if(r.ok){m.className='msg ok';m.textContent=enabled?'✓ Bot Automático ACTIVADO (cada '+payload.interval_minutes+' min)':'✓ Bot Automático PAUSADO.';loadBot();loadCurrent();}
    else{m.className='msg err';m.textContent=j.detail||'Error en el bot.';}
  }catch(e){m.className='msg err';m.textContent='Error de conexión.';}
  m.style.display='block';
}
renderPresets();
loadCurrent();
loadBot();
setInterval(loadBot, 15000);
</script></body></html>"""

@app.get("/api/version")
def get_version_info():
    return {
        "versionCode": 27,
        "versionName": "3.9.8",
        "backendUrl": "https://snaptik-backend.onrender.com/api/extract",
        "apkUrl": "https://raw.githubusercontent.com/juanherrerasandoval70-eng/snaptik-app/main/dist/SnapTik_PRO_v1.0.apk",
        "changelog": "• Entrega INSTANTÁNEA (<1 segundo) por stream SSE en tiempo real mientras la app está abierta\n• Alarma en segundo plano cada 5 minutos + caché local de banners",
        "forceUpdate": False
    }

@app.get("/api/health")
def health():
    return {
        "status": "healthy",
        "uptime": "operational",
        "proxy_configured": bool(PROXY_URL),
        "engines": ["yt-dlp", "tikwm", "ssstik"]
    }

async def _run_extraction_pipeline(clean_url: str) -> Optional[Dict[str, Any]]:
    # Fusión paralela 100% asíncrona con pool HTTP compartido (cero threads bloqueantes)
    twm_res, sss_res = await asyncio.gather(
        resolve_with_tikwm(clean_url),
        resolve_with_ssstik(clean_url),
        return_exceptions=True
    )
    if isinstance(twm_res, Exception):
        twm_res = None
    if isinstance(sss_res, Exception):
        sss_res = None

    if twm_res and sss_res:
        merged = dict(twm_res)
        if sss_res.get("images") and len(sss_res["images"]) > len(twm_res.get("images") or []):
            merged["images"] = sss_res["images"]
        if sss_res.get("preview_url"):
            merged["preview_url"] = sss_res["preview_url"]
        if not merged.get("video_url") and sss_res.get("video_url"):
            merged["video_url"] = sss_res["video_url"]
        if not merged.get("raw_cover_url") and sss_res.get("raw_cover_url"):
            merged["raw_cover_url"] = sss_res["raw_cover_url"]
            merged["cover_url"] = sss_res["raw_cover_url"]
        return merged
    if twm_res:
        return twm_res
    if sss_res:
        return sss_res

    # Fallback yt-dlp protegido por semáforo anti-OOM (máximo 2 procesos simultáneos en 512 MB RAM)
    async with ytdlp_semaphore:
        return await asyncio.to_thread(resolve_with_ytdlp, clean_url)

@app.post("/api/extract")
async def extract_media(req: ExtractRequest, request: Request):
    check_rate_limit(request)
    verify_app_signature(request, req.url)

    clean_url = clean_tiktok_url(req.url)
    if not clean_url:
        raise HTTPException(
            status_code=400,
            detail="Enlace inválido. Por favor proporciona una URL válida de tiktok.com"
        )

    # Si es enlace corto (vt.tiktok.com o vm.tiktok.com), resolver con el pool HTTP compartido
    if "vt.tiktok.com" in clean_url or "vm.tiktok.com" in clean_url or "/t/" in clean_url:
        try:
            client = get_async_client()
            resp = await client.get(clean_url, timeout=5.0)
            target = str(resp.url)
            if "?" in target:
                target = target[:target.find("?")]
            if ("/video/" in target or "/photo/" in target) and target != clean_url:
                clean_url = target
        except Exception:
            pass

    # 1. Revisar memoria caché RAM
    now = time.time()
    cached = cache_store.get(clean_url)
    if cached and (now - cached["timestamp"] < CACHE_EXPIRY_SECONDS):
        return {"success": True, "cached": True, "data": cached["data"]}

    # 2. Deduplicación en vuelo: si N personas piden la misma URL al mismo segundo, comparten 1 sola extracción
    task = in_flight_extractions.get(clean_url)
    is_owner = False
    if task is None or task.done():
        task = asyncio.create_task(_run_extraction_pipeline(clean_url))
        in_flight_extractions[clean_url] = task
        is_owner = True

    try:
        result = await task
    finally:
        if is_owner and in_flight_extractions.get(clean_url) is task:
            in_flight_extractions.pop(clean_url, None)

    if result:
        cache_store[clean_url] = {"timestamp": now, "data": result}
        vid_id = result.get("id")
        if vid_id and len(vid_id) > 5:
            cache_store[f"id:{vid_id}"] = {"timestamp": now, "data": result}

        if len(cache_store) > 1000:
            oldest_key = min(cache_store.keys(), key=lambda k: cache_store[k]["timestamp"])
            cache_store.pop(oldest_key, None)

        return {"success": True, "cached": not is_owner, "data": result}

    raise HTTPException(
        status_code=404,
        detail="No se pudo procesar el vídeo o carrusel. Puede ser privado o haber sido borrado de TikTok."
    )

ALLOWED_PROXY_DOMAINS = (
    "tiktokcdn.com", "tiktokcdn-us.com", "tiktokcdn-eu.com",
    "tikcdn.io", "tikwm.com", "muscdn.com", "tiktokv.com",
    "byteoversea.com", "ibytedtos.com", "ttwstatic.com",
    "snaptik.app", "ssstik.io", "tiktok.com"
)

def is_allowed_media_url(target_url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(target_url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").lower()
        return any(host == d or host.endswith("." + d) for d in ALLOWED_PROXY_DOMAINS)
    except Exception:
        return False

@app.get("/api/proxy-download")
async def proxy_download(request: Request, url: str = Query(...), filename: str = Query("download.mp4")):
    check_rate_limit(request)
    if not is_allowed_media_url(url):
        raise HTTPException(status_code=403, detail="Dominio multimedia no autorizado")

    headers = {
        "User-Agent": UA_DESKTOP,
        "Referer": "https://www.tiktok.com/"
    }

    client = httpx.AsyncClient(timeout=60.0, limits=http_limits)
    req = client.build_request("GET", url, headers=headers)
    resp = await client.send(req, stream=True)

    if resp.status_code >= 400:
        await resp.aclose()
        await client.aclose()
        raise HTTPException(status_code=resp.status_code, detail="Error al conectar con el servidor multimedia")

    media_type = resp.headers.get("content-type", "application/octet-stream")

    async def stream_generator():
        try:
            # Buffer de 256KB para máxima velocidad de descarga y menor uso de CPU
            async for chunk in resp.aiter_bytes(chunk_size=262144):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    headers_out = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": media_type
    }
    if "content-length" in resp.headers:
        headers_out["Content-Length"] = resp.headers["content-length"]

    return StreamingResponse(stream_generator(), headers=headers_out)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
