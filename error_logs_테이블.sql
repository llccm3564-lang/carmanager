-- Supabase SQL Editor에서 실행
-- (한 번만 실행하면 됨)

CREATE TABLE error_logs (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    content TEXT,
    pc_name TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

ALTER TABLE error_logs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Anyone can insert" ON error_logs FOR INSERT WITH CHECK (true);
CREATE POLICY "Anyone can select" ON error_logs FOR SELECT USING (true);
