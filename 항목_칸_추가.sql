-- car_listings 에 상세·성능기록부 항목 칸 추가 (한 번만 실행)
-- 실행: https://supabase.com/dashboard/project/ftuiisbcrtmcyphexzfj/sql/new 에 붙여 넣고 Run
-- 항목마다 상태(값/없음/오류)는 items_json 에, 오류 난 항목 이름은 items_errors 에 들어감

-- ── 상세 (카매니저 상세 페이지) ──
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS cm_car_no TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS full_name TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS car_number TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS mileage_km INTEGER;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS model_year TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS first_registered TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS presented_no TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS presented_date TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS union_name TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS union_phone TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS shop_name TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS shop_phone TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS dealer_name TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS dealer_phone TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS dealer_license TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS ad_line TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS seizure_count INTEGER;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS mortgage_count INTEGER;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS unpaid_tax TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS options TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS option_count INTEGER;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS photo_count INTEGER;

-- ── 성능기록부 ──
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS record_exists TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS record_no TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS vin TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS inspection_valid TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS record_mileage_km INTEGER;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS accident TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS simple_repair TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS frame_damage TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS outer_panel_1 TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS outer_panel_2 TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS repaired_parts TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS usage_change TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS special_history TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS tuning TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS recall TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS engine_issues TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS inspector_note TEXT;

-- ── 항목 상태 ──
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS items_json TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS items_errors TEXT;
ALTER TABLE car_listings ADD COLUMN IF NOT EXISTS items_updated_at TIMESTAMPTZ;

-- ── 칸 설명 (Supabase 화면에 한글로 보임) ──
COMMENT ON COLUMN car_listings.cm_car_no IS '카매니저 고유번호(숫자)';
COMMENT ON COLUMN car_listings.full_name IS '차명 전체 (상세)';
COMMENT ON COLUMN car_listings.car_number IS '차량번호 전체 (상세)';
COMMENT ON COLUMN car_listings.mileage_km IS '주행거리 km (상세)';
COMMENT ON COLUMN car_listings.model_year IS '연식 (상세 연형)';
COMMENT ON COLUMN car_listings.first_registered IS '최초등록일 (상세)';
COMMENT ON COLUMN car_listings.presented_no IS '제시번호';
COMMENT ON COLUMN car_listings.presented_date IS '제시일';
COMMENT ON COLUMN car_listings.union_name IS '조합';
COMMENT ON COLUMN car_listings.union_phone IS '조합 전화';
COMMENT ON COLUMN car_listings.shop_name IS '상사';
COMMENT ON COLUMN car_listings.shop_phone IS '상사 전화';
COMMENT ON COLUMN car_listings.dealer_name IS '딜러 이름';
COMMENT ON COLUMN car_listings.dealer_phone IS '딜러 휴대폰';
COMMENT ON COLUMN car_listings.dealer_license IS '종사원증 번호';
COMMENT ON COLUMN car_listings.ad_line IS '한줄광고·사고내역';
COMMENT ON COLUMN car_listings.seizure_count IS '압류 건수';
COMMENT ON COLUMN car_listings.mortgage_count IS '저당 건수';
COMMENT ON COLUMN car_listings.unpaid_tax IS '세금미납 정보';
COMMENT ON COLUMN car_listings.description IS '차량정보 상세내역 (딜러 설명)';
COMMENT ON COLUMN car_listings.options IS '체크된 옵션 (그룹:옵션 | ...)';
COMMENT ON COLUMN car_listings.option_count IS '체크된 옵션 수';
COMMENT ON COLUMN car_listings.photo_count IS '상세 사진 수';
COMMENT ON COLUMN car_listings.record_exists IS '성능기록부 있음/없음/확인 전';
COMMENT ON COLUMN car_listings.record_no IS '기록부 발행번호';
COMMENT ON COLUMN car_listings.vin IS '차대번호 (기록부)';
COMMENT ON COLUMN car_listings.inspection_valid IS '검사유효기간 (기록부)';
COMMENT ON COLUMN car_listings.record_mileage_km IS '기록부 주행거리 km';
COMMENT ON COLUMN car_listings.accident IS '사고이력 (있음/없음)';
COMMENT ON COLUMN car_listings.simple_repair IS '단순수리 (있음/없음)';
COMMENT ON COLUMN car_listings.frame_damage IS '주요골격 이상 (있음/없음 + 랭크)';
COMMENT ON COLUMN car_listings.outer_panel_1 IS '외판부위 1랭크 이상';
COMMENT ON COLUMN car_listings.outer_panel_2 IS '외판부위 2랭크 이상';
COMMENT ON COLUMN car_listings.repaired_parts IS '교환·판금 부위';
COMMENT ON COLUMN car_listings.usage_change IS '용도변경 (렌트/리스/영업용)';
COMMENT ON COLUMN car_listings.special_history IS '특별이력 (침수/화재)';
COMMENT ON COLUMN car_listings.tuning IS '튜닝';
COMMENT ON COLUMN car_listings.recall IS '리콜 대상·이행';
COMMENT ON COLUMN car_listings.engine_issues IS '주요장치 이상 (묶음 > 항목: 상태)';
COMMENT ON COLUMN car_listings.inspector_note IS '점검자 특기사항';
COMMENT ON COLUMN car_listings.items_json IS '항목 전체 + 항목별 상태(값/없음/오류)와 이유';
COMMENT ON COLUMN car_listings.items_errors IS '오류 난 항목 이름 목록 (확인 필요)';
COMMENT ON COLUMN car_listings.items_updated_at IS '항목 마지막으로 뽑은 때';
