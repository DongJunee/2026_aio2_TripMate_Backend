from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.request_logging import ApiRequestLoggingMiddleware
from app.routers import console, dashboard
from app.routers import auth, chat, maps, me, trips

OPENAPI_TAGS = [
    {"name": "system", "description": "서버 실행 상태를 빠르게 확인합니다."},
    {"name": "auth", "description": "회원가입, 로그인, 비밀번호 재설정 API입니다."},
    {"name": "me", "description": "로그인한 사용자의 프로필과 계정 설정을 관리합니다."},
    {"name": "trips", "description": "여행, 일차별 일정, 일정 변경 이력을 관리합니다."},
    {"name": "chat", "description": "여행별 AI 채팅과 대화 기록을 관리합니다."},
    {"name": "maps", "description": "Google Places 장소 검색, 지도, 경로 정보를 제공합니다."},
    {"name": "admin-dashboard", "description": "관리자용 운영 지표와 오류 로그를 조회합니다."},
    {"name": "admin-console", "description": "관리자 콘솔의 사용자·피드백·시스템 상태 API입니다."},
]

# FastAPI 앱은 모든 backend API의 시작점이다.
app = FastAPI(
    title="TripMate 여행 계획 API",
    description="""
TripMate는 **AI 일정 추천**, **Google 지도·장소 검색**, **여행 일정 관리**를 제공하는 여행 계획 서비스 API입니다.
""",
    version="1.0.0",
    contact={"name": "TripMate 개발팀"},
    openapi_tags=OPENAPI_TAGS,
)
# Streamlit 개발 서버(8501)에서 이 API를 호출할 수 있도록 CORS를 허용한다.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8501", "http://127.0.0.1:8501"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(ApiRequestLoggingMiddleware)

app.include_router(auth.router)
app.include_router(me.router)
app.include_router(trips.router)
app.include_router(chat.router)
app.include_router(maps.router)
app.include_router(dashboard.router)
app.include_router(console.router)


@app.get(
    "/health",
    tags=["system"],
    summary="서버 상태 확인",
    response_description="현재 API 서버의 동작 상태",
)
def health():
    """서버가 실행 중인지 빠르게 확인하는 상태 점검 API이다."""
    return {"status": "ok"}
