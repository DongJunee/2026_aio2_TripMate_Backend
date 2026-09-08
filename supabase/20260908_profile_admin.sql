-- 운영 대시보드 관리자 권한
-- 기존 profiles 테이블에 관리자 여부를 저장한다.
begin;

alter table public.profiles
    add column if not exists is_admin boolean not null default false;

comment on column public.profiles.is_admin is
    '운영 대시보드 접근 권한. true이면 관리자.';

-- 일반 로그인 사용자는 자신의 프로필을 수정할 수 있지만
-- is_admin 컬럼만큼은 직접 변경할 수 없도록 제한한다.
revoke update (is_admin) on table public.profiles from anon, authenticated;

commit;

-- 관리자 지정 예시(실제 관리자 이메일로 바꿔 SQL Editor에서 1회 실행)
-- update public.profiles as p
-- set is_admin = true
-- from auth.users as u
-- where p.id = u.id
--   and lower(u.email) = lower('admin@example.com');
