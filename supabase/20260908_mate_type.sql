-- TripMate Mate 답변 방식 저장
-- Supabase SQL Editor에서 한 번 실행하세요.

begin;

alter table public.profiles
    add column if not exists mate_type text not null default 'assistant'
    check (mate_type in ('assistant', 'guide', 'senior'));

update public.profiles
set mate_type = 'assistant'
where mate_type is null;

commit;
