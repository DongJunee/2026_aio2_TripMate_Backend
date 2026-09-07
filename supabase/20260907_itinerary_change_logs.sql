-- TripMate 일정 변경 기록 + 되돌리기
--
-- 기존 profiles / trips / trip_days / itinerary_items 테이블을 만든 뒤
-- Supabase SQL Editor에서 한 번 실행하세요.
--
-- 여행마다 별도 테이블을 만들지 않습니다. 모든 여행의 변경 기록을 이 테이블에
-- 쌓고 trip_id로 연결합니다. 각 변경에서 영향을 받은 일정 행의 전·후 상태만
-- 기록하므로 최초 AI 일정 전체 JSON은 저장하지 않습니다.

begin;

create extension if not exists pgcrypto;

create table if not exists public.itinerary_change_logs (
    id uuid primary key default gen_random_uuid(),

    trip_id uuid not null
        references public.trips (id)
        on delete cascade,

    action_type text not null check (
        action_type in (
            'time_changed',
            'place_swapped',
            'undo'
        )
    ),

    -- user: 화면 버튼, ai: 채팅 명령, system: 최초 AI 일정 생성
    actor_type text not null default 'user' check (
        actor_type in ('user', 'ai', 'system')
    ),

    -- before_data / after_data에는 영향을 받은 일정 행만 JSONB로 넣습니다.
    -- 예: 시간 변경은 한 행, 위·아래 장소 교환은 두 행의 전·후 값입니다.
    before_data jsonb not null,
    after_data jsonb not null default '{}'::jsonb,

    -- Undo 로그는 되돌린 원본 로그 ID를 가리킵니다.
    undo_of_log_id uuid
        references public.itinerary_change_logs (id)
        on delete set null,

    -- 원본 변경이 이미 되돌려졌는지 표시합니다. 감사 기록은 삭제하지 않습니다.
    is_reverted boolean not null default false,
    reverted_at timestamptz,
    created_at timestamptz not null default now(),

    constraint itinerary_change_logs_before_data_object_check
        check (jsonb_typeof(before_data) = 'object'),

    constraint itinerary_change_logs_after_data_object_check
        check (jsonb_typeof(after_data) = 'object')
);

-- 여행별 최근 변경 상태 카드와 Undo 대상 조회용
create index if not exists itinerary_change_logs_trip_created_at_idx
    on public.itinerary_change_logs (trip_id, created_at desc);

alter table public.itinerary_change_logs enable row level security;

grant select, insert, update on table public.itinerary_change_logs to authenticated;

drop policy if exists itinerary_change_logs_select_own_trip on public.itinerary_change_logs;
create policy itinerary_change_logs_select_own_trip
on public.itinerary_change_logs
for select
to authenticated
using (
    exists (
        select 1
        from public.trips
        where trips.id = itinerary_change_logs.trip_id
          and trips.user_id = auth.uid()
    )
);

drop policy if exists itinerary_change_logs_insert_own_trip on public.itinerary_change_logs;
create policy itinerary_change_logs_insert_own_trip
on public.itinerary_change_logs
for insert
to authenticated
with check (
    exists (
        select 1
        from public.trips
        where trips.id = itinerary_change_logs.trip_id
          and trips.user_id = auth.uid()
    )
);

drop policy if exists itinerary_change_logs_update_own_trip on public.itinerary_change_logs;
create policy itinerary_change_logs_update_own_trip
on public.itinerary_change_logs
for update
to authenticated
using (
    exists (
        select 1
        from public.trips
        where trips.id = itinerary_change_logs.trip_id
          and trips.user_id = auth.uid()
    )
)
with check (
    exists (
        select 1
        from public.trips
        where trips.id = itinerary_change_logs.trip_id
          and trips.user_id = auth.uid()
    )
);

commit;
