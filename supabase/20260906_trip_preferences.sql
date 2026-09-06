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
