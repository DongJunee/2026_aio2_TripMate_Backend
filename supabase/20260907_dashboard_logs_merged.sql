-- TripMate dashboard and logging schema - consolidated SQL
--
-- Execution order:
--   1) base schema
--   2) Google Places cache
--   3) trip preferences
--   4) activity logs
--   5) API request logs
--   6) itinerary change logs
--   7) operations dashboard queries
--
-- This file consolidates the original SQL files without changing their statements.

-- ============================================================================
-- BEGIN 20260901_base_schema.sql
-- ============================================================================
-- TripMate 기본 데이터베이스 구조
--
-- 새 Supabase 프로젝트에서 가장 먼저 SQL Editor로 전체 실행합니다.
-- 실행 순서:
--   1) 이 파일  2) 20260904_google_places_cache.sql
--   3) 20260906_trip_preferences.sql  4) 20260907_activity_logs.sql

begin;

create extension if not exists pgcrypto;

create table if not exists public.profiles (
    id uuid primary key references auth.users(id) on delete cascade,
    username text not null check (char_length(btrim(username)) between 1 and 30),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.trips (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    title text not null check (char_length(btrim(title)) between 1 and 100),
    destination text,
    timezone text not null default 'Asia/Seoul',
    start_date date,
    end_date date,
    status text not null default 'planning' check (status in ('planning', 'ongoing', 'completed')),
    pinned_order integer check (pinned_order >= 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint trips_date_range_check check (
        (start_date is null and end_date is null)
        or (start_date is not null and end_date is not null and start_date <= end_date)
    )
);

create table if not exists public.trip_days (
    id uuid primary key default gen_random_uuid(),
    trip_id uuid not null references public.trips(id) on delete cascade,
    day_number integer not null check (day_number > 0),
    travel_date date not null,
    title text,
    area text,
    memo text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (trip_id, day_number)
);

create table if not exists public.itinerary_items (
    id uuid primary key default gen_random_uuid(),
    trip_id uuid not null references public.trips(id) on delete cascade,
    trip_day_id uuid references public.trip_days(id) on delete set null,
    place_id uuid,
    item_type text not null default 'place' check (
        item_type in ('place', 'cafe', 'restaurant', 'hotel', 'flight', 'train', 'transit', 'activity', 'note')
    ),
    source text not null default 'manual_entry' check (
        source in ('manual_entry', 'google_search', 'ai_recommendation')
    ),
    title text not null check (char_length(btrim(title)) between 1 and 150),
    start_at timestamptz,
    end_at timestamptz,
    estimated_stay_minutes integer check (estimated_stay_minutes >= 0),
    is_fixed boolean not null default false,
    travel_mode text check (travel_mode in ('walk', 'transit', 'drive', 'bicycle', 'flight')),
    notes text,
    sort_order integer not null default 0 check (sort_order >= 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint itinerary_items_time_range_check check (
        (start_at is null and end_at is null) or (start_at is not null and end_at is not null and start_at <= end_at)
    )
);

create table if not exists public.messages (
    id uuid primary key default gen_random_uuid(),
    trip_id uuid not null references public.trips(id) on delete cascade,
    role text not null check (role in ('user', 'assistant')),
    content text not null check (char_length(content) between 1 and 4000),
    created_at timestamptz not null default now()
);

create index if not exists trips_user_updated_at_idx
    on public.trips (user_id, updated_at desc);
create index if not exists trip_days_trip_day_number_idx
    on public.trip_days (trip_id, day_number);
create index if not exists itinerary_items_trip_sort_order_idx
    on public.itinerary_items (trip_id, sort_order);
create index if not exists messages_trip_created_at_idx
    on public.messages (trip_id, created_at);

-- 회원가입 때 auth.users에 저장된 username으로 profiles 행을 자동 생성한다.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer set search_path = public
as $$
begin
    insert into public.profiles (id, username)
    values (
        new.id,
        coalesce(nullif(btrim(new.raw_user_meta_data ->> 'username'), ''), split_part(new.email, '@', 1))
    )
    on conflict (id) do nothing;
    return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
    after insert on auth.users
    for each row execute procedure public.handle_new_user();

-- 트리거를 만들기 전에 생성된 실습 계정도 프로필을 갖도록 한 번 보완한다.
insert into public.profiles (id, username)
select
    users.id,
    coalesce(nullif(btrim(users.raw_user_meta_data ->> 'username'), ''), split_part(users.email, '@', 1))
from auth.users as users
on conflict (id) do nothing;

alter table public.profiles enable row level security;
alter table public.trips enable row level security;
alter table public.trip_days enable row level security;
alter table public.itinerary_items enable row level security;
alter table public.messages enable row level security;

drop policy if exists profiles_select_own on public.profiles;
create policy profiles_select_own on public.profiles for select to authenticated using (auth.uid() = id);
drop policy if exists profiles_update_own on public.profiles;
create policy profiles_update_own on public.profiles for update to authenticated using (auth.uid() = id) with check (auth.uid() = id);

drop policy if exists trips_own_all on public.trips;
create policy trips_own_all on public.trips for all to authenticated using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists trip_days_own_all on public.trip_days;
create policy trip_days_own_all on public.trip_days for all to authenticated
using (exists (select 1 from public.trips where trips.id = trip_days.trip_id and trips.user_id = auth.uid()))
with check (exists (select 1 from public.trips where trips.id = trip_days.trip_id and trips.user_id = auth.uid()));

drop policy if exists itinerary_items_own_all on public.itinerary_items;
create policy itinerary_items_own_all on public.itinerary_items for all to authenticated
using (exists (select 1 from public.trips where trips.id = itinerary_items.trip_id and trips.user_id = auth.uid()))
with check (exists (select 1 from public.trips where trips.id = itinerary_items.trip_id and trips.user_id = auth.uid()));

drop policy if exists messages_own_all on public.messages;
create policy messages_own_all on public.messages for all to authenticated
using (exists (select 1 from public.trips where trips.id = messages.trip_id and trips.user_id = auth.uid()))
with check (exists (select 1 from public.trips where trips.id = messages.trip_id and trips.user_id = auth.uid()));

commit;
-- ============================================================================
-- END 20260901_base_schema.sql
-- ============================================================================

-- ============================================================================
-- BEGIN 20260904_google_places_cache.sql
-- ============================================================================
-- TripMate Google Places 캐시 + 일정 연결
--
-- 기본 profiles / trips / trip_days / itinerary_items 테이블을 만든 *후*에
-- Supabase SQL Editor에서 실행하세요.
--
-- 설계 메모
-- - `places`는 사용자의 저장 목록이 아니라 Google Place 정보를 함께 쓰는 캐시입니다.
-- - 개인 일정은 `itinerary_items.place_id`를 통해 캐시된 장소 하나를 가리킵니다.
-- - Google Maps API 키는 backend/.env에만 두며, 이곳에 저장하지 않습니다.
-- - 이 스크립트는 반복 실행할 수 있습니다. 기존 `id` 또는 `place_id`가 UUID가 아닌
--   형식이면, 기존 데이터를 강제 변환하거나 잃기 전에 실행을 중단합니다.

begin;

create extension if not exists pgcrypto;

-- 새 프로젝트에는 완전한 캐시 테이블을 만듭니다. 이전 프로젝트에서는 이 구문이
-- 기존 테이블을 그대로 두고, 아래 ALTER 구문이 빠진 Google 관련 필드만 추가합니다.
create table if not exists public.places (
    id uuid primary key default gen_random_uuid(),
    provider_place_id text,
    google_place_id text,
    display_name text,
    formatted_address text,
    latitude double precision,
    longitude double precision,
    google_rating double precision,
    google_rating_count integer,
    primary_type text,
    types text[] not null default '{}'::text[],
    google_maps_uri text,
    google_fetched_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- name, address, category 같은 기존 열을 지우지 않고, 최소 구성의 기존 `places`
-- 테이블도 지원합니다.
alter table public.places
    add column if not exists id uuid,
    add column if not exists provider_place_id text,
    add column if not exists google_place_id text,
    add column if not exists display_name text,
    add column if not exists formatted_address text,
    add column if not exists latitude double precision,
    add column if not exists longitude double precision,
    add column if not exists google_rating double precision,
    add column if not exists google_rating_count integer,
    add column if not exists primary_type text,
    add column if not exists types text[] not null default '{}'::text[],
    add column if not exists google_maps_uri text,
    add column if not exists google_fetched_at timestamptz,
    add column if not exists created_at timestamptz not null default now(),
    add column if not exists updated_at timestamptz not null default now();

-- 기존 정수/텍스트 식별자를 조용히 변환하지 않습니다. API 스키마는 의도적으로 UUID를
-- 사용하며, 키를 조용히 바꾸면 저장된 데이터가 깨질 수 있습니다.
do $$
declare
    id_data_type text;
begin
    select c.data_type
      into id_data_type
      from information_schema.columns c
     where c.table_schema = 'public'
       and c.table_name = 'places'
       and c.column_name = 'id';

    if id_data_type is distinct from 'uuid' then
        raise exception using message = format(
            'public.places.id must be uuid before this migration (currently %s).',
            coalesce(id_data_type, 'missing')
        ),
        hint = 'Create a new UUID id column and migrate references explicitly; do not cast existing keys blindly.';
    end if;
end
$$;

alter table public.places
    alter column id set default gen_random_uuid(),
    alter column types set default '{}'::text[],
    alter column created_at set default now(),
    alter column updated_at set default now();

-- 최소 구성의 기존 테이블 행에는 NOT NULL을 적용하기 전에 안전한 기본값을 넣습니다.
-- 비어 있는 Google ID는 키가 아니라 "아직 Google 장소와 연결되지 않음"을 뜻합니다.
update public.places
   set id = gen_random_uuid()
 where id is null;

update public.places
   set types = '{}'::text[]
 where types is null;

update public.places
   set created_at = now()
 where created_at is null;

update public.places
   set updated_at = now()
 where updated_at is null;

update public.places
   set google_place_id = nullif(btrim(google_place_id), '')
 where google_place_id is not null;

alter table public.places
    alter column id set not null,
    alter column types set not null,
    alter column created_at set not null,
    alter column updated_at set not null;

-- 외래 키 대상은 고유해야 합니다. 새 테이블에는 이미 기본 키가 있고, 이전의 최소
-- 테이블에는 이 추가 고유 제약 조건만 필요할 수 있습니다.
do $$
declare
    id_attnum smallint;
begin
    select a.attnum
      into id_attnum
      from pg_attribute a
     where a.attrelid = 'public.places'::regclass
       and a.attname = 'id'
       and not a.attisdropped;

    if exists (
        select 1
          from public.places
         group by id
        having count(*) > 1
    ) then
        raise exception using message = 'public.places.id contains duplicate values.',
        hint = 'Resolve duplicate IDs before adding the itinerary_items.place_id foreign key.';
    end if;

    if not exists (
        select 1
          from pg_constraint c
         where c.conrelid = 'public.places'::regclass
           and c.contype in ('p', 'u')
           and array_length(c.conkey, 1) = 1
           and c.conkey[1] = id_attnum
    ) then
        alter table public.places
            add constraint tripmate_places_id_key unique (id);
    end if;
end
$$;

-- Google Place ID는 전역적으로 고유하므로, 고유값을 두면 백엔드는 같은 Google 검색
-- 결과를 새 행으로 만들지 않고 기존 캐시 행을 갱신할 수 있습니다.
-- 이전에 수동으로 만든 테이블에 중복 Google ID가 있다면, 해당 행을 삭제하거나 합치지
-- 않고 그대로 둔 뒤 안내 메시지만 출력합니다.
do $$
declare
    google_id_attnum smallint;
begin
    select a.attnum
      into google_id_attnum
      from pg_attribute a
     where a.attrelid = 'public.places'::regclass
       and a.attname = 'google_place_id'
       and not a.attisdropped;

    if not exists (
        select 1
          from pg_constraint c
         where c.conrelid = 'public.places'::regclass
           and c.contype in ('p', 'u')
           and array_length(c.conkey, 1) = 1
           and c.conkey[1] = google_id_attnum
    ) then
        if exists (
            select 1
              from public.places
             where google_place_id is not null
             group by google_place_id
            having count(*) > 1
        ) then
            raise notice 'Duplicate google_place_id values found; skipped the unique cache constraint.';
        else
            alter table public.places
                add constraint tripmate_places_google_place_id_key unique (google_place_id);
        end if;
    end if;
end
$$;

create index if not exists tripmate_places_coordinates_idx
    on public.places (latitude, longitude);

-- NOT VALID는 기존 테이블을 계속 사용할 수 있게 하면서도, 새로 추가하거나 갱신하는
-- 모든 캐시 행은 검증합니다. 기존 데이터를 정리한 뒤 나중에 전체 검증할 수 있습니다.
do $$
begin
    if not exists (
        select 1 from pg_constraint
         where conrelid = 'public.places'::regclass
           and conname = 'tripmate_places_latitude_range_check'
    ) then
        alter table public.places
            add constraint tripmate_places_latitude_range_check
            check (latitude is null or latitude between -90 and 90) not valid;
    end if;

    if not exists (
        select 1 from pg_constraint
         where conrelid = 'public.places'::regclass
           and conname = 'tripmate_places_longitude_range_check'
    ) then
        alter table public.places
            add constraint tripmate_places_longitude_range_check
            check (longitude is null or longitude between -180 and 180) not valid;
    end if;

    if not exists (
        select 1 from pg_constraint
         where conrelid = 'public.places'::regclass
           and conname = 'tripmate_places_google_rating_range_check'
    ) then
        alter table public.places
            add constraint tripmate_places_google_rating_range_check
            check (google_rating is null or google_rating between 0 and 5) not valid;
    end if;

    if not exists (
        select 1 from pg_constraint
         where conrelid = 'public.places'::regclass
           and conname = 'tripmate_places_google_rating_count_check'
    ) then
        alter table public.places
            add constraint tripmate_places_google_rating_count_check
            check (google_rating_count is null or google_rating_count >= 0) not valid;
    end if;
end
$$;

-- 캐시를 갱신할 때는 원래 created_at을 유지하고, 행이 마지막으로 변경된 시점만 기록해야
-- 합니다. 전용 이름을 사용해 trips나 itinerary_items가 이미 쓰는 일반적인
-- 수정 시각 트리거와 충돌하지 않게 합니다.
create or replace function public.tripmate_touch_places_updated_at()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists tripmate_places_set_updated_at on public.places;
create trigger tripmate_places_set_updated_at
before update on public.places
for each row execute function public.tripmate_touch_places_updated_at();

-- `places`는 공유 참조/캐시 테이블입니다. 로그인한 사용자는 읽을 수 있고, 보호된
-- 백엔드가 추가하거나 갱신합니다. 의도적으로 DELETE 정책은 두지 않습니다.
-- `service_role`은 향후 서버 캐시 작업자를 위해 계속 RLS를 우회하며, 반드시
-- 백엔드에서만 사용해야 합니다.
alter table public.places enable row level security;

-- RLS 정책은 인증된 사용자가 *어떤* 행에 접근할 수 있는지 정하고, 이 테이블 권한은
-- 인증된 역할이 해당 작업 자체를 수행할 수 있게 합니다. 기존 수동 생성 테이블에는
-- Supabase의 일반적인 기본 권한이 없을 수 있습니다.
grant select, insert, update on table public.places to authenticated;

drop policy if exists tripmate_places_authenticated_select on public.places;
create policy tripmate_places_authenticated_select
on public.places
for select
to authenticated
using (true);

drop policy if exists tripmate_places_authenticated_insert on public.places;
create policy tripmate_places_authenticated_insert
on public.places
for insert
to authenticated
with check (true);

drop policy if exists tripmate_places_authenticated_update on public.places;
create policy tripmate_places_authenticated_update
on public.places
for update
to authenticated
using (true)
with check (true);

-- 기본 itinerary_items 소유권 정책은 이미 각 사용자의 개인 일정을 보호합니다.
-- 이 마이그레이션은 선택적인 전역 장소 연결만 추가하며, itinerary_items의 읽기/쓰기
-- 접근 범위를 넓히지 않습니다.
do $$
declare
    place_id_data_type text;
    place_id_attnum smallint;
begin
    if to_regclass('public.itinerary_items') is null then
        raise notice 'public.itinerary_items does not exist yet; skipped place_id foreign key.';
        return;
    end if;

    alter table public.itinerary_items
        add column if not exists place_id uuid;

    select c.data_type
      into place_id_data_type
      from information_schema.columns c
     where c.table_schema = 'public'
       and c.table_name = 'itinerary_items'
       and c.column_name = 'place_id';

    if place_id_data_type is distinct from 'uuid' then
        raise exception using message = format(
            'public.itinerary_items.place_id must be uuid before this migration (currently %s).',
            coalesce(place_id_data_type, 'missing')
        ),
        hint = 'Migrate non-UUID place references explicitly, then run this script again.';
    end if;

    select a.attnum
      into place_id_attnum
      from pg_attribute a
     where a.attrelid = 'public.itinerary_items'::regclass
       and a.attname = 'place_id'
       and not a.attisdropped;

    if exists (
        select 1
          from pg_constraint c
         where c.conrelid = 'public.itinerary_items'::regclass
           and c.contype = 'f'
           and array_length(c.conkey, 1) = 1
           and c.conkey[1] = place_id_attnum
           and c.confrelid <> 'public.places'::regclass
    ) then
        raise exception using message = 'public.itinerary_items.place_id already references a different table.',
        hint = 'Review that foreign key manually; this migration will not replace it automatically.';
    end if;

    if not exists (
        select 1
          from pg_constraint c
         where c.conrelid = 'public.itinerary_items'::regclass
           and c.contype = 'f'
           and c.confrelid = 'public.places'::regclass
           and array_length(c.conkey, 1) = 1
           and c.conkey[1] = place_id_attnum
    ) then
        -- NOT VALID는 기존 수동 데이터를 보호하면서, 새로 추가하거나 갱신하는 모든
        -- 일정 행에는 이 연결을 적용합니다.
        alter table public.itinerary_items
            add constraint tripmate_itinerary_items_place_id_fkey
            foreign key (place_id)
            references public.places (id)
            on delete set null
            not valid;
    end if;

    execute 'create index if not exists tripmate_itinerary_items_place_id_idx on public.itinerary_items (place_id)';
end
$$;

commit;
-- ============================================================================
-- END 20260904_google_places_cache.sql
-- ============================================================================

-- ============================================================================
-- BEGIN 20260906_trip_preferences.sql
-- ============================================================================
-- 새 여행 조건. 이미 SQL Editor에서 추가한 프로젝트에도 다시 실행할 수 있다.
alter table public.trips
  add column if not exists travel_party text not null default 'unspecified'
    constraint trips_travel_party_check check (
      travel_party in (
        'unspecified', 'solo', 'couple', 'friends', 'family',
        'family_with_children', 'with_parents', 'senior_couple', 'other'
      )
    ),
  add column if not exists travel_intensity smallint not null default 3
    constraint trips_travel_intensity_check check (travel_intensity between 1 and 5),
  add column if not exists budget_level smallint not null default 3
    constraint trips_budget_level_check check (budget_level between 1 and 5);
-- ============================================================================
-- END 20260906_trip_preferences.sql
-- ============================================================================

-- ============================================================================
-- BEGIN 20260907_activity_logs.sql
-- ============================================================================
-- TripMate 사용자 행동 로그
--
-- Supabase SQL Editor에서 한 번 실행합니다.
-- 이 테이블에는 비밀번호, Bearer 토큰, Supabase 키처럼 민감한 값은 저장하지 않습니다.

create extension if not exists pgcrypto;

create table if not exists public.activity_logs (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    trip_id uuid references public.trips(id) on delete set null,
    event_type text not null,
    entity_type text,
    entity_id uuid,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    constraint activity_logs_event_type_not_blank check (btrim(event_type) <> ''),
    constraint activity_logs_metadata_is_object check (jsonb_typeof(metadata) = 'object')
);

-- 한 사용자의 최근 활동과, 한 여행의 활동을 빠르게 조회한다.
create index if not exists activity_logs_user_created_at_idx
    on public.activity_logs (user_id, created_at desc);

create index if not exists activity_logs_trip_created_at_idx
    on public.activity_logs (trip_id, created_at desc)
    where trip_id is not null;

alter table public.activity_logs enable row level security;

-- 사용자는 자신의 로그만 조회·기록할 수 있다.
-- 로그는 감사 기록이므로 일반 사용자의 수정·삭제 정책은 만들지 않는다.
drop policy if exists activity_logs_select_own on public.activity_logs;
create policy activity_logs_select_own
    on public.activity_logs
    for select
    to authenticated
    using (auth.uid() = user_id);

drop policy if exists activity_logs_insert_own on public.activity_logs;
create policy activity_logs_insert_own
    on public.activity_logs
    for insert
    to authenticated
    with check (auth.uid() = user_id);

comment on table public.activity_logs is
    'TripMate의 사용자 행동 감사 로그. 민감한 인증 정보는 기록하지 않는다.';

comment on column public.activity_logs.event_type is
    '예: auth.signup, auth.login, trip.create, trip.pin, itinerary.create, chat.send';

comment on column public.activity_logs.metadata is
    '기능 분석용 비민감 부가 정보. 비밀번호, 토큰, API 키, 원문 채팅은 저장하지 않는다.';
-- ============================================================================
-- END 20260907_activity_logs.sql
-- ============================================================================

-- ============================================================================
-- BEGIN 20260907_api_request_logs.sql
-- ============================================================================
-- API 요청 성능·오류 분석 로그
-- activity_logs(사용자 행동 기록)와 달리, 이 테이블은 서버 요청 단위의 관측 로그다.

create extension if not exists pgcrypto;

create table if not exists public.api_request_logs (
    id uuid primary key default gen_random_uuid(),
    created_at timestamptz not null default now(),
    request_id text not null,
    method text not null,
    endpoint text not null,
    status_code smallint,
    latency_ms integer check (latency_ms is null or latency_ms >= 0),
    error_type text,
    user_id uuid references auth.users(id) on delete set null,
    trip_id uuid references public.trips(id) on delete set null,
    model text,
    constraint api_request_logs_request_id_not_blank check (btrim(request_id) <> ''),
    constraint api_request_logs_method_not_blank check (btrim(method) <> ''),
    constraint api_request_logs_endpoint_not_blank check (btrim(endpoint) <> '')
);

create index if not exists api_request_logs_created_at_idx
    on public.api_request_logs (created_at desc);
create index if not exists api_request_logs_endpoint_created_at_idx
    on public.api_request_logs (endpoint, created_at desc);
create index if not exists api_request_logs_user_created_at_idx
    on public.api_request_logs (user_id, created_at desc)
    where user_id is not null;

alter table public.api_request_logs enable row level security;

-- 서비스 역할만 서버 관측 로그를 쓰며, 일반 클라이언트에는 직접 접근을 열지 않는다.
-- 운영 화면을 만들 때는 별도의 관리자 전용 조회 API로 제한한다.
-- ============================================================================
-- END 20260907_api_request_logs.sql
-- ============================================================================

-- ============================================================================
-- BEGIN 20260907_itinerary_change_logs.sql
-- ============================================================================
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
-- ============================================================================
-- END 20260907_itinerary_change_logs.sql
-- ============================================================================

-- ============================================================================
-- BEGIN 20260907_operations_dashboard.sql
-- ============================================================================
-- TripMate 운영 대시보드용 조회 SQL
--
-- 전제 테이블
--   public.api_request_logs : API 요청 시간·상태·응답 시간·오류·모델 로그
--   public.profiles         : 회원가입 때 생성되는 사용자 프로필
--
-- 사용법
-- 1. Supabase SQL Editor에 필요한 블록 하나를 복사한다.
-- 2. params의 시작·끝 시각만 원하는 기간으로 바꾼다.
-- 3. 끝 시각은 포함하지 않는다. 예: 9월 7일 하루는 09-07 00:00 ~ 09-08 00:00


-- ============================================================================
-- 0. 공통 기간 설정
-- ============================================================================
-- 아래 params 블록은 각 조회문 맨 앞에 붙여 사용한다.
-- 예시: 2026-09-07 하루 전체
--
-- with params as (
--   select
--     timestamptz '2026-09-07 00:00:00+09' as start_at,
--     timestamptz '2026-09-08 00:00:00+09' as end_at
-- )


-- ============================================================================
-- 1. 기간 필터가 적용된 운영 요약 카드
--    사용자 가입 수 / 전체 요청 수 / 에러율 / 평균 응답 시간 / 성공·실패 수
-- ============================================================================
with params as (
  select
    timestamptz '2026-09-07 00:00:00+09' as start_at,
    timestamptz '2026-09-08 00:00:00+09' as end_at
),
request_summary as (
  select
    count(*) as total_requests,
    count(*) filter (where status_code between 200 and 399) as success_count,
    count(*) filter (where status_code >= 400 or status_code is null) as failure_count,
    round(avg(latency_ms) filter (where latency_ms is not null))::integer as average_latency_ms
  from public.api_request_logs, params
  where created_at >= params.start_at
    and created_at < params.end_at
),
signup_summary as (
  select count(*) as signup_count
  from public.profiles, params
  where created_at >= params.start_at
    and created_at < params.end_at
)
select
  signup_summary.signup_count as user_signup_count,
  request_summary.total_requests,
  request_summary.success_count,
  request_summary.failure_count,
  case
    when request_summary.total_requests = 0 then 0
    else round(request_summary.failure_count::numeric / request_summary.total_requests * 100, 2)
  end as error_rate_percent,
  request_summary.average_latency_ms
from signup_summary cross join request_summary;


-- ============================================================================
-- 2. 시간별 요청량 차트 데이터
--    Supabase Chart 또는 프론트 차트의 x축: hour, y축: request_count
-- ============================================================================
with params as (
  select
    timestamptz '2026-09-07 00:00:00+09' as start_at,
    timestamptz '2026-09-08 00:00:00+09' as end_at
)
select
  date_trunc('hour', created_at at time zone 'Asia/Seoul') as hour,
  count(*) as request_count,
  count(*) filter (where status_code between 200 and 399) as success_count,
  count(*) filter (where status_code >= 400 or status_code is null) as failure_count
from public.api_request_logs, params
where created_at >= params.start_at
  and created_at < params.end_at
group by 1
order by 1;


-- ============================================================================
-- 3. 기능(엔드포인트)별 사용량·오류율
--    "사용자가 우리 웹 기능마다 얼마나 많이 썼는가"를 보는 표
-- ============================================================================
with params as (
  select
    timestamptz '2026-09-07 00:00:00+09' as start_at,
    timestamptz '2026-09-08 00:00:00+09' as end_at
)
select
  endpoint,
  count(*) as request_count,
  count(distinct user_id) filter (where user_id is not null) as unique_user_count,
  count(*) filter (where status_code between 200 and 399) as success_count,
  count(*) filter (where status_code >= 400 or status_code is null) as failure_count,
  coalesce(round(avg(latency_ms) filter (where latency_ms is not null))::integer, 0) as average_latency_ms,
  round(
    count(*) filter (where status_code >= 400 or status_code is null)::numeric
    / nullif(count(*), 0) * 100,
    2
  ) as error_rate_percent
from public.api_request_logs, params
where created_at >= params.start_at
  and created_at < params.end_at
group by endpoint
order by request_count desc, endpoint;


-- ============================================================================
-- 4. 최근 오류 로그 테이블
--    운영자가 "무슨 오류가, 언제, 어느 기능에서 났는가"를 확인한다.
--    민감한 채팅 원문·비밀번호·토큰은 이 테이블에 저장하지 않는다.
-- ============================================================================
with params as (
  select
    timestamptz '2026-09-07 00:00:00+09' as start_at,
    timestamptz '2026-09-08 00:00:00+09' as end_at
)
select
  created_at at time zone 'Asia/Seoul' as occurred_at_kst,
  request_id,
  method,
  endpoint,
  status_code,
  latency_ms,
  error_type,
  user_id,
  trip_id,
  model
from public.api_request_logs, params
where created_at >= params.start_at
  and created_at < params.end_at
  and (status_code >= 400 or status_code is null or error_type is not null)
order by created_at desc
limit 100;


-- ============================================================================
-- 5. 간단한 LLM(Gemini) 로그 요약 영역
--    model 값이 있는 요청만 대상으로 한다. 현재는 챗봇 요청이 해당한다.
-- ============================================================================
with params as (
  select
    timestamptz '2026-09-07 00:00:00+09' as start_at,
    timestamptz '2026-09-08 00:00:00+09' as end_at
)
select
  coalesce(model, 'model_not_recorded') as model,
  count(*) as llm_request_count,
  count(*) filter (where status_code between 200 and 399) as success_count,
  count(*) filter (where status_code >= 400 or status_code is null) as failure_count,
  coalesce(round(avg(latency_ms) filter (where latency_ms is not null))::integer, 0) as average_latency_ms,
  round(
    count(*) filter (where status_code >= 400 or status_code is null)::numeric
    / nullif(count(*), 0) * 100,
    2
  ) as error_rate_percent,
  count(*) filter (where error_type = 'gemini_timeout') as timeout_count,
  count(*) filter (where error_type = 'gemini_error') as gemini_error_count,
  count(*) filter (where error_type = 'gemini_empty_response') as empty_response_count
from public.api_request_logs, params
where created_at >= params.start_at
  and created_at < params.end_at
  and model is not null
group by model
order by llm_request_count desc, model;


-- ============================================================================
-- 6. 운영 화면에 표시할 수 있는 짧은 문장용 수치
--    예: "오늘 요청 120건, 오류율 2.5%, 평균 응답 348ms"
-- ============================================================================
with params as (
  select
    date_trunc('day', now() at time zone 'Asia/Seoul') at time zone 'Asia/Seoul' as start_at,
    (date_trunc('day', now() at time zone 'Asia/Seoul') + interval '1 day') at time zone 'Asia/Seoul' as end_at
),
summary as (
  select
    count(*) as total_requests,
    count(*) filter (where status_code >= 400 or status_code is null) as failure_count,
    round(avg(latency_ms) filter (where latency_ms is not null))::integer as average_latency_ms
  from public.api_request_logs, params
  where created_at >= params.start_at
    and created_at < params.end_at
)
select format(
  '오늘 요청 %s건, 오류율 %s%%, 평균 응답 %sms',
  total_requests,
  case when total_requests = 0 then 0 else round(failure_count::numeric / total_requests * 100, 2) end,
  coalesce(average_latency_ms, 0)
) as dashboard_summary
from summary;
-- ============================================================================
-- END 20260907_operations_dashboard.sql
-- ============================================================================
