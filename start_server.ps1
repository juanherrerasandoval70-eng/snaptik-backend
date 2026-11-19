Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "    SNAPTIK PRO - INICIANDO BACKEND PRIVADO       " -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

$port = 8000
Write-Host "Iniciando servidor FastAPI en http://127.0.0.1:$port..." -ForegroundColor Green
Write-Host "Documentacion interactiva Swagger: http://127.0.0.1:$port/docs" -ForegroundColor Yellow

python -m uvicorn server:app --host 0.0.0.0 --port $port --reload
