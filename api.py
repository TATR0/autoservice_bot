"""
api.py — REST-сервис для Telegram WebApp.

Endpoints:
  GET /api/services?city=<city>   — список сервисов в городе
  GET /api/service/<id>           — детали одного сервиса
  GET /healthz                    — healthcheck для Render
"""

import os

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from database import db

app = FastAPI(title="AutoService API", docs_url=None, redoc_url=None)

# CORS — разрешаем запросы отовсюду.
# Telegram WebApp открывается внутри iframe с origin telegram.org,
# поэтому allow_origins="*" — единственный надёжный вариант.
# allow_credentials=False обязателен при allow_origins="*".
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup() -> None:
    await db.connect()


@app.on_event("shutdown")
async def shutdown() -> None:
    await db.close()


@app.get("/api/services")
async def get_services_by_city(city: str = Query(..., min_length=1)):
    try:
        rows = await db.get_services_by_city(city)
        return [dict(r) for r in rows]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/service/{service_id}")
async def get_service(service_id: str):
    svc = await db.get_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail="Service not found")
    return dict(svc)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(
        "api:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        reload=False,
    )