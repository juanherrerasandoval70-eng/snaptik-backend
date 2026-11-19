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
    version="1.0.0"
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

# Memoria Caché en RAM con expiración de 1 hora
CACHE_EXPIRY_SECONDS = 3600
cache_store: Dict[str, Dict[str, Any]] = {}

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
    except Exception:
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
                "preview_url": play_url if not images else "",
                "video_url": play_url if not images else "",
                "hd_url": hd_play_url if not images else "",
                "uhd_4k_url": hd_play_url if not images else "",
                "sd_url": play_url if not images else "",
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
    except Exception:
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

            images = re.findall(r"data-splide-lazy=[\"'](https?://[^\"']+)[\"']", html)

            video_url = v_m.group(1) if v_m else (prev_m.group(1) if prev_m else "")
            audio_url = a_m.group(1) if a_m else ""

            if not video_url and not images:
                return None

            name_m = re.search(r"<h2>(.*?)</h2>", html)
            nickname = re.sub(r"<[^>]+>", "", name_m.group(1)).strip() if name_m else "Creador de TikTok"

            desc_m = re.search(r"<p[^>]*class=[\"'][^\"']*(?:ssr-caption|maintext)[^\"']*[\"'][^>]*>(.*?)</p>", html, re.DOTALL)
            desc = re.sub(r"<[^>]+>", "", desc_m.group(1)).strip() if desc_m else f"Vídeo de {nickname}"

            is_photos = len(images) > 0
            return {
                "id": str(int(time.time())),
                "source_url": clean_url,
                "title": desc,
                "preview_url": video_url if not is_photos else "",
                "video_url": video_url if not is_photos else "",
                "hd_url": video_url if not is_photos else "",
                "uhd_4k_url": video_url if not is_photos else "",
                "sd_url": video_url if not is_photos else "",
                "wm_url": "",
                "audio_url": audio_url,
                "music_title": f"Música de {nickname}",
                "cover_url": images[0] if is_photos else "",
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
                    "avatar": ""
                },
                "engine": "ssstik-proxy"
            }
    except Exception:
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

@app.get("/api/health")
def health():
    return {
        "status": "healthy",
        "uptime": "operational",
        "proxy_configured": bool(PROXY_URL),
        "engines": ["yt-dlp", "tikwm", "ssstik"]
    }

@app.post("/api/extract")
def extract_media(req: ExtractRequest):
    clean_url = clean_tiktok_url(req.url)
    if not clean_url:
        raise HTTPException(
            status_code=400,
            detail="Enlace inválido. Por favor proporciona una URL válida de tiktok.com"
        )

    # Revisar memoria caché
    now = time.time()
    cached = cache_store.get(clean_url)
    if cached and (now - cached["timestamp"] < CACHE_EXPIRY_SECONDS):
        return {"success": True, "cached": True, "data": cached["data"]}

    # Cascada de motores: TikWM -> SSSTik -> yt-dlp
    result = resolve_with_tikwm(clean_url)
    if not result:
        result = resolve_with_ssstik(clean_url)
    if not result:
        result = resolve_with_ytdlp(clean_url)

    if result:
        # Guardar en caché
        cache_store[clean_url] = {"timestamp": now, "data": result}
        # Limitar caché a 1000 elementos
        if len(cache_store) > 1000:
            oldest_key = min(cache_store.keys(), key=lambda k: cache_store[k]["timestamp"])
            cache_store.pop(oldest_key, None)

        return {"success": True, "cached": False, "data": result}

    raise HTTPException(
        status_code=404,
        detail="No se pudo procesar el vídeo o carrusel. Puede ser privado o haber sido borrado de TikTok."
    )

@app.get("/api/proxy-download")
async def proxy_download(url: str = Query(...), filename: str = Query("download.mp4")):
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="URL inválida")

    headers = {
        "User-Agent": UA_DESKTOP,
        "Referer": "https://www.tiktok.com/"
    }

    client = httpx.AsyncClient(timeout=60.0)
    req = client.build_request("GET", url, headers=headers)
    resp = await client.send(req, stream=True)

    if resp.status_code >= 400:
        await resp.aclose()
        await client.aclose()
        raise HTTPException(status_code=resp.status_code, detail="Error al conectar con el servidor multimedia")

    media_type = resp.headers.get("content-type", "application/octet-stream")

    async def stream_generator():
        try:
            async for chunk in resp.aiter_bytes(chunk_size=65536):
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
