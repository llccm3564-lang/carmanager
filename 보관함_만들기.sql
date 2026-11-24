-- 사진·원문 보관함 만들기 (한 번만 실행)
-- 실행: https://supabase.com/dashboard/project/ftuiisbcrtmcyphexzfj/sql/new 에 붙여 넣고 Run
--
-- carmanager 보관함 (비공개)
--   photos/<차 고유번호>/<n>.jpg          상세 사진 원본
--   raw/detail/<차 고유번호>.html.gz      카매니저 상세 페이지 원문
--   raw/record/<차 고유번호>.html.gz      성능기록부 페이지 원문
-- 프로그램(공개용 키)은 「올리기」만 가능 — 읽기·덮어쓰기·지우기 불가. 보기는 관리 화면 Storage 에서.

INSERT INTO storage.buckets (id, name, public)
VALUES ('carmanager', 'carmanager', false)
ON CONFLICT (id) DO NOTHING;

DROP POLICY IF EXISTS "carmanager 올리기만" ON storage.objects;
CREATE POLICY "carmanager 올리기만" ON storage.objects
  FOR INSERT TO anon
  WITH CHECK (bucket_id = 'carmanager');
