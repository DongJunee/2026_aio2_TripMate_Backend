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
