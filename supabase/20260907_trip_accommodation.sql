-- TripMate 여행 숙소 연결
--
-- 20260904_google_places_cache.sql을 먼저 실행한 뒤 Supabase SQL Editor에서
-- 한 번 실행하세요. 숙소 이름을 텍스트로 중복 저장하지 않고, Google Place로
-- 검증되어 public.places에 캐시된 행을 여행 하나에 연결합니다.

begin;

alter table public.trips
    add column if not exists accommodation_place_id uuid;

-- 같은 이름의 호텔도 Google Place ID가 다른 별도 places 행이므로, 이 외래 키는
-- 동명이인 숙소를 혼동하지 않게 한다. 기존 여행의 숙소는 null로 그대로 둔다.
do $$
begin
    if not exists (
        select 1
        from pg_constraint
        where conrelid = 'public.trips'::regclass
          and conname = 'tripmate_trips_accommodation_place_id_fkey'
    ) then
        alter table public.trips
            add constraint tripmate_trips_accommodation_place_id_fkey
            foreign key (accommodation_place_id)
            references public.places (id)
            on delete set null;
    end if;
end
$$;

create index if not exists tripmate_trips_accommodation_place_id_idx
    on public.trips (accommodation_place_id);

commit;
