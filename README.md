# SnapTik PRO - Backend Privado de Extracción

API REST de alto rendimiento para extraer vídeos sin marca de agua, fotos en carrusel y audios MP3 de TikTok.

## Estructura
- **`server.py`**: Servidor FastAPI con soporte asíncrono, CORS, motor híbrido (yt-dlp + TikWM fallback) y proxy de descarga.
- **`Dockerfile` / `docker-compose.yml`**: Listo para producción en cualquier VPS (Hetzner, DigitalOcean, etc.).
- **`start_server.ps1`**: Lanzador rápido para desarrollo local.

## Ejecución Local
```bash
# 1. Instalar dependencias
pip install -r requirements.txt

# 2. Iniciar servidor
python -m uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```
Acceso a la documentación Swagger interactiva: `http://localhost:8000/docs`

## Endpoints

### 1. `POST /api/extract`
Cuerpo (JSON):
```json
{
  "url": "https://www.tiktok.com/@tiktok/video/123456789"
}
```
Respuesta:
```json
{
  "success": true,
  "data": {
    "id": "123456789",
    "title": "Descripción del video",
    "video_url": "https://...",
    "hd_url": "https://...",
    "audio_url": "https://...",
    "cover_url": "https://...",
    "images": []
  }
}
```

### 2. `GET /api/health`
Estado del servicio y motores disponibles.

### 3. `GET /api/proxy-download?url=...&filename=...`
Túnel de descarga directa para eludir restricciones de cabeceras Referer o CORS de CDNs.
