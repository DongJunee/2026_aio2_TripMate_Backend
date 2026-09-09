"""TripMate OpenAPI 문서화 적용 참고 코드.

원본 ``app/main.py``, ``app/schemas.py``, ``app/routers/*``는 수정하지 않는다.
이 파일에는 실습 1~5의 **문서화 설정만** 정리한다.
따라서 CORS, 요청 로그, 라우터 연결, 데이터베이스 처리 같은 기능 코드는 넣지 않는다.
"""

from fastapi import FastAPI


# ---------- 원본 대조 위치: app/main.py 의 app = FastAPI(...) ----------
# ---------- 원본 경로: app/main.py (현재 9행 부근) ----------
# 실습 1. 앱 정보 넣기
APP_DOCUMENTATION = {
    "title": "TripMate 여행 계획 API",
    "description": """
TripMate는 **AI 일정 추천**, **Google 지도·장소 검색**, **여행 일정 관리**를 제공하는 여행 계획 서비스 API입니다.

보호된 요청에는 로그인 후 받은 `Authorization: Bearer <access_token>` 헤더가 필요합니다.
""",
    "version": "1.0.0-documentation-preview",
    "contact": {"name": "TripMate 개발팀"},
}

app = FastAPI(
    title=APP_DOCUMENTATION["title"],
    description=APP_DOCUMENTATION["description"],
    version=APP_DOCUMENTATION["version"],
    contact=APP_DOCUMENTATION["contact"],   
)


# ---------- 원본 대조 위치: app/routers/* 의 router = APIRouter(..., tags=[...]) ----------
# ---------- 대표 위치: auth.py 12행, me.py 9행, trips.py 32행, chat.py 20행 ----------
# 실습 2. 태그로 그룹 나누기
# 원래 라우터에 이미 있는 tags=["auth"], tags=["trips"] 등을 이 설명·순서와 연결한다.
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


# ---------- 원본 대조 위치: auth.py signup/login, me.py update_profile ----------
# ---------- 추가 대조 위치: trips.py create_my_trip/read_trip_dashboard, chat.py chat ----------
# ---------- 아래 URL·HTTP 메서드가 원본 @router 데코레이터와 같은지 확인 ----------
# 실습 3. 엔드포인트 설명 붙이기
# 원본 함수 본문은 건드리지 않고, 문서 설계도(OpenAPI) 안의 설명만 보완한다.
ENDPOINT_DOCUMENTATION = {
    ("/auth/signup", "post"): {
        "summary": "회원가입",
        "description": "새 TripMate 계정을 만들고, 가능하면 최초 로그인 세션 정보를 반환합니다.",
        "response_description": "생성된 계정 ID, 이메일, 액세스 토큰",
    },
    ("/auth/login", "post"): {
        "summary": "로그인",
        "description": "이메일과 비밀번호를 인증하고 보호된 API 요청에 쓸 액세스 토큰을 반환합니다.",
        "response_description": "액세스 토큰과 로그인 사용자 정보",
    },
    ("/me/profile", "patch"): {
        "summary": "내 프로필과 Mate 방식 변경",
        "description": "Bearer 토큰으로 확인한 본인의 표시 이름 또는 AI Mate 방식을 변경합니다.",
        "response_description": "변경 후 현재 사용자의 프로필",
    },
    ("/me/trips", "post"): {
        "summary": "AI 여행 일정 생성",
        "description": "여행 조건을 바탕으로 AI 초안과 Google Places 검증을 거쳐 여행과 DAY별 일정을 생성합니다.",
        "response_description": "생성된 여행과 일차별 일정 정보",
    },
    ("/trips/{trip_id}/dashboard", "get"): {
        "summary": "여행 대시보드 조회",
        "description": "여행 기본 정보, DAY별 일정, 연결된 장소 정보를 한 번에 반환합니다.",
        "response_description": "여행 대시보드 데이터",
    },
    ("/trips/{trip_id}/chat", "post"): {
        "summary": "AI 여행 채팅 보내기",
        "description": "현재 여행의 일정과 Mate 설정을 반영해 AI 답변을 SSE 스트림으로 반환하고 대화를 저장합니다.",
        "response_description": "Server-Sent Events 형식의 AI 답변",
    },
}


# ---------- 원본 대조 위치: app/schemas.py 의 SignupRequest, LoginRequest, ProfileUpdate ----------
# ---------- 추가 대조 위치: TripCreate, ChatRequest 클래스와 각 Field 제약 조건 ----------
# ---------- 아래 예시는 설명용이며 원본 모델의 길이·날짜·값 검증 규칙을 바꾸지 않음 ----------
# 실습 4. 모델 예시 채우기
# Swagger의 Try it out에 의미 있는 값을 제시하기 위한 설명·예시다. 검증 규칙은 바꾸지 않는다.
MODEL_DOCUMENTATION = {
    "SignupRequest": {
        "example": {"username": "여행메이트", "email": "traveler@example.com", "password": "tripmate123"},
        "properties": {
            "username": ("TripMate에 표시할 사용자 이름", "여행메이트"),
            "email": ("로그인에 사용할 이메일 주소", "traveler@example.com"),
            "password": ("6자 이상 비밀번호", "tripmate123"),
        },
    },
    "LoginRequest": {
        "example": {"email": "traveler@example.com", "password": "tripmate123"},
        "properties": {
            "email": ("가입한 이메일 주소", "traveler@example.com"),
            "password": ("가입 시 설정한 비밀번호", "tripmate123"),
        },
    },
    "ProfileUpdate": {
        "example": {"username": "오사카여행자", "mate_type": "guide"},
        "properties": {
            "username": ("변경할 표시 이름", "오사카여행자"),
            "mate_type": ("AI Mate 답변 방식: assistant, guide, senior", "guide"),
        },
    },
    "TripCreate": {
        "example": {
            "title": "오사카 3박 4일",
            "destination": "오사카, 일본",
            "start_date": "2026-09-07",
            "end_date": "2026-09-10",
            "travel_party": "couple",
            "travel_intensity": 3,
            "budget_level": 3,
            "must_visit": [{"name": "오사카성"}],
        },
        "properties": {
            "title": ("여행 목록에 표시할 제목", "오사카 3박 4일"),
            "destination": ("여행 도시", "오사카, 일본"),
            "travel_party": ("여행 인원 구성", "couple"),
            "travel_intensity": ("여행 일정의 여유·밀도 수준 (1~5)", 3),
            "budget_level": ("상대적인 여행 경비 수준 (1~5)", 3),
        },
    },
    "ChatRequest": {
        "example": {"content": "오전 여유 시간에 갈 만한 카페를 추천해줘"},
        "properties": {
            "content": ("현재 여행 일정에 관해 AI에게 보낼 질문 또는 변경 요청", "오전 여유 시간에 갈 만한 카페를 추천해줘"),
        },
    },
}


# ---------- 원본 대조 위치: 각 라우터 함수 안의 raise HTTPException(status_code=...) ----------
# ---------- 대표 위치: auth.py signup/login, me.py update_profile, trips.py create_my_trip ----------
# ---------- 아래 상태 코드는 원본에 실제 raise가 있는 경우만 문서에 표시 ----------
# 실습 5. 오류 응답 문서화
# 아래 상태 코드는 각 기존 라우터가 이미 HTTPException으로 반환하는 경우만 적었다.
# 문서에 표시하는 작업일 뿐, 오류를 새로 만들거나 실제 동작을 변경하지 않는다.
ERROR_RESPONSES = {
    ("/auth/signup", "post"): {
        "400": "이미 사용 중인 이메일, 잘못된 가입 정보 또는 가입 처리 실패",
        "503": "Supabase 인증 서비스 설정 또는 연결 문제",
    },
    ("/auth/login", "post"): {
        "401": "이메일 또는 비밀번호가 일치하지 않음",
        "503": "Supabase 인증 서비스 설정 또는 연결 문제",
    },
    ("/me/profile", "patch"): {
        "401": "Bearer 토큰이 없거나 유효하지 않음",
        "404": "현재 사용자의 프로필을 찾을 수 없음",
    },
    ("/me/trips", "post"): {
        "401": "로그인이 필요함",
        "502": "AI 일정 초안 또는 Google Places 검증 실패",
        "500": "여행 또는 일정 저장 실패",
    },
    ("/trips/{trip_id}/dashboard", "get"): {
        "401": "로그인이 필요함",
        "404": "여행을 찾을 수 없거나 접근 권한이 없음",
    },
    ("/trips/{trip_id}/chat", "post"): {
        "401": "로그인이 필요함",
        "404": "여행을 찾을 수 없거나 접근 권한이 없음",
    },
}
