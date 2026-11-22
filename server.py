import asyncio
import os
import re
import time
import urllib.parse
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
import httpx
import yt_dlp

app = FastAPI(
    title="SnapTik PRO - Motor Privado de Extracción",
    description="Backend API independiente para procesar y descargar vídeos y carruseles de TikTok en máxima calidad.",
    version="1.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UA_DESKTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
PROXY_URL = os.environ.get("PROXY_URL", "").strip() or None
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://snaptik-backend.onrender.com").strip()

# Memoria Caché en RAM con expiración de 1 hora
CACHE_EXPIRY_SECONDS = 3600
cache_store: Dict[str, Dict[str, Any]] = {}

# Pool de conexiones optimizado de alta velocidad
http_limits = httpx.Limits(max_keepalive_connections=50, max_connections=200)

async def background_auto_maintenance():
    """Mantenimiento automático y Keep-Alive 24/7: Evita que Render duerma y purga RAM periódicamente."""
    while True:
        try:
            await asyncio.sleep(600)  # Cada 10 minutos
            # Auto-ping externo para mantener activo el servidor
            if RENDER_EXTERNAL_URL:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    await client.get(f"{RENDER_EXTERNAL_URL}/api/health")

            # Mantenimiento de memoria: limpiar entradas expiradas
            now = time.time()
            expired_keys = [k for k, v in list(cache_store.items()) if (now - v["timestamp"] > CACHE_EXPIRY_SECONDS)]
            for k in expired_keys:
                cache_store.pop(k, None)
        except Exception:
            pass

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(background_auto_maintenance())

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

def resolve_with_tikwm(clean_url: str) -> Optional[Dict[str, Any]]:
    api_url = f"https://www.tikwm.com/api/?url={urllib.parse.quote(clean_url)}&hd=1"
    try:
        client_kwargs = {"timeout": 12.0}
        if PROXY_URL:
            client_kwargs["proxy"] = PROXY_URL

        with httpx.Client(**client_kwargs) as client:
            r = client.get(api_url, headers={"User-Agent": UA_DESKTOP})
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

            cover = d.get('origin_cover') or d.get('cover') or avatar
            if cover.startswith("//"):
                cover = "https:" + cover
            elif cover.startswith("/"):
                cover = "https://www.tikwm.com" + cover

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
                "engine": "tikwm-proxy"
            }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return None

def resolve_with_ssstik(clean_url: str) -> Optional[Dict[str, Any]]:
    try:
        client_kwargs = {"timeout": 12.0, "headers": {"User-Agent": UA_DESKTOP}}
        if PROXY_URL:
            client_kwargs["proxy"] = PROXY_URL

        with httpx.Client(**client_kwargs) as client:
            home = client.get("https://ssstik.io/es")
            tt_m = re.search(r"s_tt\s*=\s*'([^']+)'", home.text)
            furl_m = re.search(r"s_furl\s*=\s*'([^']+)'", home.text)
            if not tt_m:
                return None

            s_tt = tt_m.group(1)
            s_furl = furl_m.group(1) if furl_m else "abc"

            post_data = {
                "id": clean_url,
                "locale": "es",
                "tt": s_tt
            }
            post_headers = {
                "HX-Request": "true",
                "HX-Target": "target",
                "HX-Current-URL": "https://ssstik.io/es",
                "Origin": "https://ssstik.io",
                "Referer": "https://ssstik.io/es",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
            }
            res = client.post(f"https://ssstik.io/{s_furl}?url=dl", data=post_data, headers=post_headers)
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

            ava_m = re.search(r'<img[^>]*class=[\'"][^\'"]*ssr-ava[^\'"]*[\'"][^>]*src=[\'"]([^\'"]+)[\'"]', html)
            avatar_url = ava_m.group(1) if ava_m else ""

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
                "cover_url": avatar_url or (images[0] if is_photos else ""),
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

@app.get("/api/version")
def get_version_info():
    return {
        "versionCode": 6,
        "versionName": "3.1.0",
        "backendUrl": "https://snaptik-backend.onrender.com/api/extract",
        "apkUrl": "https://raw.githubusercontent.com/juanherrerasandoval70-eng/snaptik-app/main/dist/SnapTik_PRO_v1.0.apk",
        "changelog": "• Nuevo ícono oficial 3D Liquid Glass & Obsidian Titanium\n• Reproductor adaptable sin recortes: muestra el 100% del vídeo (vertical, horizontal o cuadrado)\n• Detección en tiempo real de resolución exacta (px), orientación y duración\n• Fondo ambiental difuminado, botón de pantalla completa y pulido visual premium",
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

@app.post("/api/extract")
async def extract_media(req: ExtractRequest):
    clean_url = clean_tiktok_url(req.url)
    if not clean_url:
        raise HTTPException(
            status_code=400,
            detail="Enlace inválido. Por favor proporciona una URL válida de tiktok.com"
        )

    # Si es enlace corto (vt.tiktok.com o vm.tiktok.com), intentar resolver hacia la URL canónica del vídeo
    if "vt.tiktok.com" in clean_url or "vm.tiktok.com" in clean_url or "/t/" in clean_url:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=5.0) as client:
                resp = await client.get(clean_url, headers={"User-Agent": UA_DESKTOP})
                target = str(resp.url)
                if "?" in target:
                    target = target[:target.find("?")]
                if ("/video/" in target or "/photo/" in target) and target != clean_url:
                    clean_url = target
        except Exception:
            pass

    # Revisar memoria caché
    now = time.time()
    cached = cache_store.get(clean_url)
    if cached and (now - cached["timestamp"] < CACHE_EXPIRY_SECONDS):
        return {"success": True, "cached": True, "data": cached["data"]}

    # Fusión paralela sin bloqueo de event-loop (2x más rápida y alta concurrencia)
    twm_res, sss_res = await asyncio.gather(
        asyncio.to_thread(resolve_with_tikwm, clean_url),
        asyncio.to_thread(resolve_with_ssstik, clean_url),
        return_exceptions=True
    )
    if isinstance(twm_res, Exception):
        twm_res = None
    if isinstance(sss_res, Exception):
        sss_res = None

    result = None
    if twm_res and sss_res:
        merged = dict(twm_res)
        if sss_res.get("images") and len(sss_res["images"]) > len(twm_res.get("images") or []):
            merged["images"] = sss_res["images"]
        if sss_res.get("preview_url"):
            merged["preview_url"] = sss_res["preview_url"]
        if not merged.get("video_url") and sss_res.get("video_url"):
            merged["video_url"] = sss_res["video_url"]
        result = merged
    elif twm_res:
        result = twm_res
    elif sss_res:
        result = sss_res
    else:
        result = await asyncio.to_thread(resolve_with_ytdlp, clean_url)

    if result:
        # Guardar en caché tanto la URL canónica como el id del video
        cache_store[clean_url] = {"timestamp": now, "data": result}
        vid_id = result.get("id")
        if vid_id and len(vid_id) > 5:
            cache_store[f"id:{vid_id}"] = {"timestamp": now, "data": result}

        # Limitar caché a 1000 elementos
        if len(cache_store) > 1000:
            oldest_key = min(cache_store.keys(), key=lambda k: cache_store[k]["timestamp"])
            cache_store.pop(oldest_key, None)

        return {"success": True, "cached": False, "data": result}

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
async def proxy_download(url: str = Query(...), filename: str = Query("download.mp4")):
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
