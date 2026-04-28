-- PostgreSQL schema for Trading project
-- Target:
--   host: localhost
--   port: 5432
--   db:   trading_dev
--   user: postgres
--
-- Example:
--   PGPASSWORD=postgres psql -h localhost -p 5432 -U postgres -d trading_dev -f trading_dev_postgres_schema.sql

BEGIN;

SET client_min_messages TO warning;
SET search_path TO public;

CREATE TABLE IF NOT EXISTS stocks (
    code           varchar(10) PRIMARY KEY,
    name           varchar(100),
    market         varchar(10) NOT NULL,
    listed_date    date,
    delisted_date  date,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_stocks_market
        CHECK (market IN ('TSE', 'OTC'))
);

CREATE INDEX IF NOT EXISTS idx_stocks_market
    ON stocks (market);

CREATE TABLE IF NOT EXISTS daily_prices (
    code         varchar(10) NOT NULL,
    trade_date   date NOT NULL,
    open         numeric(12, 4),
    high         numeric(12, 4),
    low          numeric(12, 4),
    close        numeric(12, 4),
    volume       bigint,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_daily_prices
        PRIMARY KEY (code, trade_date),
    CONSTRAINT fk_daily_prices_stock
        FOREIGN KEY (code) REFERENCES stocks(code) ON DELETE CASCADE,
    CONSTRAINT chk_daily_prices_volume
        CHECK (volume IS NULL OR volume >= 0),
    CONSTRAINT chk_daily_prices_ohlc_positive
        CHECK (
            (open  IS NULL OR open  > 0) AND
            (high  IS NULL OR high  > 0) AND
            (low   IS NULL OR low   > 0) AND
            (close IS NULL OR close > 0)
        ),
    CONSTRAINT chk_daily_prices_range
        CHECK (
            high IS NULL OR low IS NULL OR high >= low
        ),
    CONSTRAINT chk_daily_prices_open_in_range
        CHECK (
            open IS NULL OR low IS NULL OR high IS NULL OR open BETWEEN low AND high
        ),
    CONSTRAINT chk_daily_prices_close_in_range
        CHECK (
            close IS NULL OR low IS NULL OR high IS NULL OR close BETWEEN low AND high
        )
);

CREATE INDEX IF NOT EXISTS idx_daily_prices_trade_date
    ON daily_prices (trade_date);

CREATE INDEX IF NOT EXISTS idx_daily_prices_trade_date_code
    ON daily_prices (trade_date, code);

CREATE TABLE IF NOT EXISTS universe_snapshots (
    trade_date    date NOT NULL,
    code          varchar(10) NOT NULL,
    avg_vol_5d    numeric(18, 2),
    avg_vol_60d   numeric(18, 2),
    vol_surge_ratio numeric(18, 6),
    vol_rank      integer,
    vol_surge_rank integer,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_universe_snapshots
        PRIMARY KEY (trade_date, code),
    CONSTRAINT fk_universe_snapshots_stock
        FOREIGN KEY (code) REFERENCES stocks(code) ON DELETE CASCADE,
    CONSTRAINT chk_universe_avg_vol_5d
        CHECK (avg_vol_5d IS NULL OR avg_vol_5d >= 0),
    CONSTRAINT chk_universe_avg_vol_60d
        CHECK (avg_vol_60d IS NULL OR avg_vol_60d >= 0),
    CONSTRAINT chk_universe_vol_surge_ratio
        CHECK (vol_surge_ratio IS NULL OR vol_surge_ratio >= 0),
    CONSTRAINT chk_universe_vol_rank
        CHECK (vol_rank IS NULL OR vol_rank > 0),
    CONSTRAINT chk_universe_vol_surge_rank
        CHECK (vol_surge_rank IS NULL OR vol_surge_rank > 0)
);

ALTER TABLE universe_snapshots ADD COLUMN IF NOT EXISTS avg_vol_60d numeric(18, 2);
ALTER TABLE universe_snapshots ADD COLUMN IF NOT EXISTS vol_surge_ratio numeric(18, 6);
ALTER TABLE universe_snapshots ADD COLUMN IF NOT EXISTS vol_surge_rank integer;

CREATE INDEX IF NOT EXISTS idx_universe_snapshots_date_rank
    ON universe_snapshots (trade_date, vol_rank, code);

CREATE INDEX IF NOT EXISTS idx_universe_snapshots_date_surge_rank
    ON universe_snapshots (trade_date, vol_surge_rank, code);

CREATE INDEX IF NOT EXISTS idx_universe_snapshots_code_date
    ON universe_snapshots (code, trade_date);

CREATE TABLE IF NOT EXISTS institutional_net (
    code          varchar(10) NOT NULL,
    trade_date    date NOT NULL,
    foreign_net   integer,
    trust_net     integer,
    dealer_net    integer,
    total_net     integer,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_institutional_net
        PRIMARY KEY (code, trade_date),
    CONSTRAINT fk_institutional_net_stock
        FOREIGN KEY (code) REFERENCES stocks(code) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_institutional_net_trade_date
    ON institutional_net (trade_date);

CREATE TABLE IF NOT EXISTS margin_balance (
    code                 varchar(10) NOT NULL,
    trade_date           date NOT NULL,
    margin_buy           bigint,
    margin_sell          bigint,
    margin_balance       bigint,
    margin_limit         bigint,
    short_sell           bigint,
    short_buy            bigint,
    short_balance        bigint,
    short_limit          bigint,
    margin_short_ratio   numeric(12, 4),
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_margin_balance
        PRIMARY KEY (code, trade_date),
    CONSTRAINT fk_margin_balance_stock
        FOREIGN KEY (code) REFERENCES stocks(code) ON DELETE CASCADE,
    CONSTRAINT chk_margin_short_ratio
        CHECK (margin_short_ratio IS NULL OR margin_short_ratio >= 0)
);

CREATE INDEX IF NOT EXISTS idx_margin_balance_trade_date
    ON margin_balance (trade_date);

CREATE TABLE IF NOT EXISTS foreign_holding (
    code             varchar(10) NOT NULL,
    trade_date       date NOT NULL,
    foreign_shares   bigint,
    holding_pct      numeric(8, 6),
    retail_pct       numeric(8, 6),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_foreign_holding
        PRIMARY KEY (code, trade_date),
    CONSTRAINT fk_foreign_holding_stock
        FOREIGN KEY (code) REFERENCES stocks(code) ON DELETE CASCADE,
    CONSTRAINT chk_foreign_shares
        CHECK (foreign_shares IS NULL OR foreign_shares >= 0),
    CONSTRAINT chk_holding_pct
        CHECK (holding_pct IS NULL OR (holding_pct >= 0 AND holding_pct <= 1)),
    CONSTRAINT chk_retail_pct
        CHECK (retail_pct IS NULL OR (retail_pct >= 0 AND retail_pct <= 1))
);

CREATE INDEX IF NOT EXISTS idx_foreign_holding_trade_date
    ON foreign_holding (trade_date);

CREATE TABLE IF NOT EXISTS dividends (
    code          varchar(10) NOT NULL,
    ex_date       date NOT NULL,
    cash_div      numeric(12, 4) NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_dividends
        PRIMARY KEY (code, ex_date),
    CONSTRAINT fk_dividends_stock
        FOREIGN KEY (code) REFERENCES stocks(code) ON DELETE CASCADE,
    CONSTRAINT chk_dividends_cash_div
        CHECK (cash_div >= 0)
);

CREATE INDEX IF NOT EXISTS idx_dividends_ex_date
    ON dividends (ex_date);

CREATE TABLE IF NOT EXISTS db_meta (
    key          varchar(100) PRIMARY KEY,
    value        text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_stocks_updated_at ON stocks;
CREATE TRIGGER trg_stocks_updated_at
BEFORE UPDATE ON stocks
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_daily_prices_updated_at ON daily_prices;
CREATE TRIGGER trg_daily_prices_updated_at
BEFORE UPDATE ON daily_prices
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_universe_snapshots_updated_at ON universe_snapshots;
CREATE TRIGGER trg_universe_snapshots_updated_at
BEFORE UPDATE ON universe_snapshots
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_institutional_net_updated_at ON institutional_net;
CREATE TRIGGER trg_institutional_net_updated_at
BEFORE UPDATE ON institutional_net
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_margin_balance_updated_at ON margin_balance;
CREATE TRIGGER trg_margin_balance_updated_at
BEFORE UPDATE ON margin_balance
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_foreign_holding_updated_at ON foreign_holding;
CREATE TRIGGER trg_foreign_holding_updated_at
BEFORE UPDATE ON foreign_holding
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_dividends_updated_at ON dividends;
CREATE TRIGGER trg_dividends_updated_at
BEFORE UPDATE ON dividends
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_db_meta_updated_at ON db_meta;
CREATE TRIGGER trg_db_meta_updated_at
BEFORE UPDATE ON db_meta
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE stocks IS '股票主檔，包含上市/上櫃與可能已下市股票。';
COMMENT ON TABLE daily_prices IS '日線 OHLCV。trade_date 使用 DATE，方便區間查詢與排序。';
COMMENT ON TABLE universe_snapshots IS '每日歷史宇宙快照，通常用 5 日均量排名建立。';
COMMENT ON TABLE institutional_net IS '三大法人日買賣超，單位通常為張。';
COMMENT ON TABLE margin_balance IS '融資融券餘額與資券比。';
COMMENT ON TABLE foreign_holding IS '外資持股資料；holding_pct / retail_pct 採 0~1 小數口徑。';
COMMENT ON TABLE dividends IS '歷史配息資料；cash_div 為每股現金股利。';
COMMENT ON TABLE db_meta IS 'key-value 型態的資料庫 metadata。';

COMMENT ON COLUMN foreign_holding.holding_pct IS '0~1 小數，例如 0.3512 代表 35.12%。';
COMMENT ON COLUMN foreign_holding.retail_pct IS '0~1 小數；若使用 1 - holding_pct 推估，僅為粗估值。';
COMMENT ON COLUMN daily_prices.volume IS '成交量，建議統一成張。';
COMMENT ON COLUMN institutional_net.total_net IS '可直接存來源值，或以 foreign_net + trust_net + dealer_net 重算。';
COMMENT ON COLUMN margin_balance.margin_short_ratio IS '資券比 = margin_balance / short_balance。';
COMMENT ON COLUMN universe_snapshots.vol_surge_ratio IS '近5日均量 / 近60日均量，相對暴量倍數。';
COMMENT ON COLUMN universe_snapshots.vol_surge_rank IS '按 vol_surge_ratio 的每日排名（1=最強）。';

COMMIT;
