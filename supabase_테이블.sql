-- 카매니저 매물 데이터 테이블
-- Supabase SQL Editor에서 한 번만 실행

CREATE TABLE car_listings (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    car_id TEXT UNIQUE NOT NULL,          -- 차량 고유번호 (카매니저 EnCarNo)
    model TEXT,                           -- 차종
    price TEXT,                           -- 가격
    year TEXT,                            -- 연식
    mileage TEXT,                         -- 주행거리
    fuel TEXT,                            -- 연료
    transmission TEXT,                    -- 변속기
    color TEXT,                           -- 색상
    accident TEXT,                        -- 사고여부
    plate TEXT,                           -- 번호판
    region TEXT,                          -- 지역
    seller TEXT,                          -- 판매자
    phone TEXT,                           -- 연락처
    photo_url TEXT,                       -- 사진 URL
    detail_url TEXT,                      -- 상세 페이지 링크
    status TEXT DEFAULT 'active',         -- active(등록중) / removed(사라짐)
    first_seen TIMESTAMPTZ DEFAULT NOW(), -- 최초 발견
    last_seen TIMESTAMPTZ DEFAULT NOW(),  -- 최근 확인
    removed_at TIMESTAMPTZ,              -- 사라진 시각
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 보안: 서비스 키로만 접근 (anon 키로는 읽기만)
ALTER TABLE car_listings ENABLE ROW LEVEL SECURITY;

-- anon 키로 읽기 허용
CREATE POLICY "읽기 허용" ON car_listings
    FOR SELECT USING (true);

-- anon 키로 쓰기 허용 (프로그램에서 데이터 넣으려면 필요)
CREATE POLICY "쓰기 허용" ON car_listings
    FOR INSERT WITH CHECK (true);

CREATE POLICY "수정 허용" ON car_listings
    FOR UPDATE USING (true);

-- 검색 속도를 위한 인덱스
CREATE INDEX idx_car_id ON car_listings (car_id);
CREATE INDEX idx_status ON car_listings (status);
CREATE INDEX idx_last_seen ON car_listings (last_seen);
