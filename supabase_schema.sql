-- 比董股票AI顧問 — Supabase 資料表
-- 在 Supabase Dashboard → SQL Editor 貼上執行

-- 持股
CREATE TABLE IF NOT EXISTS stock_portfolio (
  code      TEXT PRIMARY KEY,
  name      TEXT,
  avg_price NUMERIC,
  lots      INT,
  shares    INT,
  buy_date  TEXT,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 對話記憶
CREATE TABLE IF NOT EXISTS stock_memory (
  id         BIGSERIAL PRIMARY KEY,
  user_id    TEXT,
  role       TEXT,
  content    TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 永久記憶
CREATE TABLE IF NOT EXISTS stock_permanent (
  id         BIGSERIAL PRIMARY KEY,
  content    TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 價格警報
CREATE TABLE IF NOT EXISTS stock_alert (
  code        TEXT PRIMARY KEY,
  name        TEXT,
  alert_price NUMERIC,
  direction   TEXT DEFAULT 'below',
  memo        TEXT,
  active      BOOLEAN DEFAULT TRUE,
  created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 開放讀寫權限（RLS 關閉，用 service key）
ALTER TABLE stock_portfolio DISABLE ROW LEVEL SECURITY;
ALTER TABLE stock_memory    DISABLE ROW LEVEL SECURITY;
ALTER TABLE stock_permanent DISABLE ROW LEVEL SECURITY;
ALTER TABLE stock_alert     DISABLE ROW LEVEL SECURITY;
