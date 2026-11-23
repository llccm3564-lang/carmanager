-- car_listings 에 성능기록부 링크·요약 칸 추가 (한 번만 실행)
-- performance_url     : 성능기록부 페이지 주소 (로그인 없이 누구나 열림, 엠파크 등)
-- performance_summary : 기록부 요약 (등록번호·차대번호·최초등록일·주행거리·사고이력·단순수리·기록부 있음/없음)
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS performance_url TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS performance_summary TEXT;
