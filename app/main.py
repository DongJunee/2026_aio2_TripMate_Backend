from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import auth, chat, maps, me, trips

# FastAPI 앱은 모든 backend API의 시작점이다.
app = FastAPI(title="TripMate API", version="0.1.0")
# Streamlit 개발 서버(8501)에서 이 API를 호출할 수 있도록 CORS를 허용한다.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8501", "http://127.0.0.1:8501"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(me.router)
app.include_router(trips.router)
app.include_router(chat.router)
app.include_router(maps.router)


@app.get("/health")
def health():
    """서버가 실행 중인지 빠르게 확인하는 상태 점검 API이다."""
    return {"status": "ok"}
