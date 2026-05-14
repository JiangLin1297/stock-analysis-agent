"""
StockMind 本地数据库 — SQLite 统一数据层。
提供 K线缓存、股票信息、成分股列表、模糊搜索等功能。
"""

import os
import sys
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

import pandas as pd


def _data_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


DB_PATH = os.path.join(_data_dir(), "stockmind.db")


# ═══════════════════════════════════════════════════════════════
# 连接管理
# ═══════════════════════════════════════════════════════════════

@contextmanager
def _get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """建表+索引，幂等操作。"""
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS stocks (
                symbol TEXT PRIMARY KEY,
                name TEXT,
                industry TEXT,
                list_date TEXT,
                last_quote_time TEXT
            );

            CREATE TABLE IF NOT EXISTS klines (
                symbol TEXT NOT NULL,
                date TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL,
                volume REAL, turnover REAL, pe REAL, pb REAL,
                PRIMARY KEY (symbol, date)
            );

            CREATE TABLE IF NOT EXISTS factors (
                symbol TEXT NOT NULL,
                date TEXT NOT NULL,
                factor_name TEXT NOT NULL,
                score REAL,
                PRIMARY KEY (symbol, date, factor_name)
            );

            CREATE TABLE IF NOT EXISTS index_constituents (
                index_code TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT,
                added_date TEXT,
                PRIMARY KEY (index_code, symbol)
            );

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_klines_symbol ON klines(symbol);
            CREATE INDEX IF NOT EXISTS idx_klines_date ON klines(date);
            CREATE INDEX IF NOT EXISTS idx_stocks_name ON stocks(name);
            CREATE INDEX IF NOT EXISTS idx_constituents_index ON index_constituents(index_code);

            CREATE TABLE IF NOT EXISTS analysis_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                name TEXT,
                type TEXT NOT NULL,
                time_frame TEXT,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_analysis_symbol ON analysis_history(symbol);
            CREATE INDEX IF NOT EXISTS idx_analysis_type ON analysis_history(type);
            CREATE INDEX IF NOT EXISTS idx_analysis_date ON analysis_history(created_at);

            CREATE TABLE IF NOT EXISTS screening_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                scope TEXT,
                top5_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_screening_date ON screening_history(created_at);

            CREATE TABLE IF NOT EXISTS stock_genes (
                symbol TEXT PRIMARY KEY,
                name TEXT,
                avg_ma60_alignment_days REAL DEFAULT 0,
                avg_false_breakout_prob REAL DEFAULT 0,
                avg_washout_volume_ratio REAL DEFAULT 0,
                avg_pullback_depth REAL DEFAULT 0,
                avg_rally_strength REAL DEFAULT 0,
                avg_atr_level REAL DEFAULT 0,
                avg_gap_reaction REAL DEFAULT 0,
                confidence_score REAL DEFAULT 0,
                sample_count INTEGER DEFAULT 0,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS signal_quality (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                date TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                time_frame TEXT,
                signal_price REAL,
                exit_price REAL,
                pnl_pct REAL,
                days_held INTEGER,
                trend_during TEXT,
                factor_snapshot TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_signal_symbol ON signal_quality(symbol);
            CREATE INDEX IF NOT EXISTS idx_signal_date ON signal_quality(date);
            CREATE INDEX IF NOT EXISTS idx_signal_type ON signal_quality(signal_type);
        """)


# ═══════════════════════════════════════════════════════════════
# 元数据
# ═══════════════════════════════════════════════════════════════

def _set_meta(key: str, value: str):
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value)
        )


def _get_meta(key: str):
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else None


# ═══════════════════════════════════════════════════════════════
# 股票信息
# ═══════════════════════════════════════════════════════════════

def save_stock_info(symbol: str, name: str, industry: str = ""):
    """存储/更新股票基本信息。"""
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO stocks (symbol, name, industry, last_quote_time) "
            "VALUES (?, ?, ?, ?)",
            (symbol, name, industry, datetime.now().isoformat())
        )


def get_stock_info(symbol: str) -> dict:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM stocks WHERE symbol=?", (symbol,)
        ).fetchone()
        return dict(row) if row else {}


def search_symbol(keyword: str) -> list:
    """模糊搜索股票，支持代码和中文名称。"""
    with _get_conn() as conn:
        kw = f"%{keyword}%"
        rows = conn.execute(
            "SELECT symbol, name, industry FROM stocks "
            "WHERE symbol LIKE ? OR name LIKE ? "
            "ORDER BY CASE WHEN symbol LIKE ? THEN 0 ELSE 1 END, symbol "
            "LIMIT 20",
            (kw, kw, f"{keyword}%")
        ).fetchall()
        return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════
# K线数据
# ═══════════════════════════════════════════════════════════════

def save_kline(symbol: str, df: pd.DataFrame):
    """将 K线 DataFrame 存入数据库。列: date,open,high,low,close,volume。"""
    if df is None or df.empty:
        return
    records = []
    for _, row in df.iterrows():
        date_val = str(row.get("date", ""))[:10]
        if not date_val:
            continue
        records.append((
            symbol, date_val,
            _safe(row, "open"), _safe(row, "high"),
            _safe(row, "low"), _safe(row, "close"),
            _safe(row, "volume"),
            _safe(row, "turnover"), _safe(row, "pe"), _safe(row, "pb"),
        ))
    if not records:
        return
    with _get_conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO klines "
            "(symbol, date, open, high, low, close, volume, turnover, pe, pb) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            records
        )


def load_kline(symbol: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """从数据库读取 K线，返回与 CSV 缓存相同格式的 DataFrame。"""
    query = "SELECT date, open, high, low, close, volume FROM klines WHERE symbol=?"
    params = [symbol]
    if start_date:
        query += " AND date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND date <= ?"
        params.append(end_date)
    query += " ORDER BY date"
    with _get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    if not rows:
        return pd.DataFrame(columns=["date", "open", "close", "high", "low", "volume"])
    df = pd.DataFrame([dict(r) for r in rows])
    # 确保列顺序与 CSV 缓存一致
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def get_kline_count(symbol: str) -> int:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM klines WHERE symbol=?", (symbol,)
        ).fetchone()
        return row[0] if row else 0


def get_latest_kline_date(symbol: str) -> str:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(date) FROM klines WHERE symbol=?", (symbol,)
        ).fetchone()
        return row[0] if row else None


def _safe(row, col, default=None):
    v = row.get(col, default)
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


# ═══════════════════════════════════════════════════════════════
# 指数成分股
# ═══════════════════════════════════════════════════════════════

def save_index_constituents(index_code: str, symbols: list, names: list = None):
    """批量存入成分股列表。"""
    today = datetime.now().strftime("%Y-%m-%d")
    records = []
    for i, sym in enumerate(symbols):
        name = names[i] if names and i < len(names) else ""
        records.append((index_code, sym, name, today))
    with _get_conn() as conn:
        conn.execute(
            "DELETE FROM index_constituents WHERE index_code=?", (index_code,)
        )
        conn.executemany(
            "INSERT OR REPLACE INTO index_constituents "
            "(index_code, symbol, name, added_date) VALUES (?, ?, ?, ?)",
            records
        )


def load_index_constituents(index_code: str) -> list:
    """返回成分股代码列表。"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT symbol FROM index_constituents WHERE index_code=? ORDER BY symbol",
            (index_code,)
        ).fetchall()
        return [r[0] for r in rows]


# ═══════════════════════════════════════════════════════════════
# 全A股列表
# ═══════════════════════════════════════════════════════════════

def ensure_full_stock_list() -> int:
    """确保全A股列表已加载到 stocks 表。返回股票总数。

    首次调用时从 akshare 下载，后续直接返回数据库记录数。
    """
    if _get_meta("full_stock_list_loaded") == "true":
        with _get_conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]

    try:
        import akshare as ak
        print("[DB] 正在下载全A股列表...")
        df = ak.stock_info_a_code_name()
        if df is None or df.empty:
            print("[DB] akshare stock_info_a_code_name 返回空")
            return 0

        records = []
        for _, row in df.iterrows():
            sym = str(row.get("code", "")).strip()
            name = str(row.get("name", "")).strip()
            if sym:
                records.append((sym, name, "", datetime.now().isoformat()))

        if not records:
            return 0

        with _get_conn() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO stocks (symbol, name, industry, last_quote_time) "
                "VALUES (?, ?, ?, ?)",
                records
            )
        _set_meta("full_stock_list_loaded", "true")
        print(f"[DB] 全A股列表已加载: {len(records)} 只")
        return len(records)
    except Exception as e:
        print(f"[DB] 加载全A股列表失败: {e}")
        return 0


# ═══════════════════════════════════════════════════════════════
# 增量更新 K线
# ═══════════════════════════════════════════════════════════════

def update_klines(symbol: str) -> int:
    """增量更新单只股票的 K线数据。返回新增行数。"""
    latest = get_latest_kline_date(symbol)
    if latest:
        try:
            last_dt = datetime.strptime(latest, "%Y-%m-%d")
            gap_days = (datetime.now() - last_dt).days
        except ValueError:
            gap_days = 30
    else:
        gap_days = 500  # 无数据，下载约2年

    if gap_days <= 1:
        return 0  # 已是最新

    ndays = min(gap_days + 5, 800)  # 多下几天确保覆盖

    # 复用 pipeline 的下载逻辑
    exchange = _guess_exchange(symbol)
    df = _fetch_kline_from_api(symbol, exchange, ndays)
    if df is None or df.empty:
        return 0

    # 只保存新数据
    if latest:
        df = df[df["date"] > latest]

    if df.empty:
        return 0

    save_kline(symbol, df)
    return len(df)


def _guess_exchange(symbol: str) -> str:
    if symbol.startswith(("6", "5")):
        return "sh"
    return "sz"


def _fetch_kline_from_api(symbol: str, exchange: str, ndays: int) -> pd.DataFrame:
    """从腾讯 API 下载 K线数据（复用 pipeline 的逻辑）。"""
    import json as _json
    try:
        import urllib.request
        code = f"{exchange}{symbol}"
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{ndays},qfq"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        day_data = data.get("data", {}).get(code, {})
        rows = day_data.get("qfqday") or day_data.get("day") or []
        if not rows:
            return None
        records = []
        for r in rows:
            if len(r) >= 6:
                records.append({
                    "date": r[0],
                    "open": float(r[1]),
                    "close": float(r[2]),
                    "high": float(r[3]),
                    "low": float(r[4]),
                    "volume": float(r[5]) if len(r) > 5 else 0,
                })
        return pd.DataFrame(records)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# 后台数据任务
# ═══════════════════════════════════════════════════════════════

def _background_initial_download():
    """首次启动：下载全A股列表 + 沪深300成分股 + 2年K线。"""
    print("[DB] 首次启动，开始下载全A股列表 + 沪深300数据...")
    try:
        ensure_full_stock_list()
    except Exception as e:
        print(f"[DB] 全A股列表下载失败(继续): {e}")
    try:
        from analysis.screener import _get_constituents
        symbols = _get_constituents("hs300")
        if not symbols:
            print("[DB] 获取成分股失败，跳过")
            return

        save_index_constituents("000300", symbols)
        print(f"[DB] 成分股列表已保存: {len(symbols)} 只")

        success = 0
        for i, sym in enumerate(symbols):
            try:
                n = update_klines(sym)
                if n > 0:
                    success += 1
                # 同时保存股票代码到 stocks 表（名称后续由 get_compressed_data 补充）
                info = get_stock_info(sym)
                if not info:
                    save_stock_info(sym, sym)
            except Exception as e:
                print(f"[DB] {sym} 下载失败: {e}")

            if (i + 1) % 50 == 0:
                print(f"[DB] 进度: {i + 1}/{len(symbols)} (成功 {success})")

        _set_meta("initialized", "true")
        _set_meta("last_full_download", datetime.now().isoformat())
        _set_meta("stock_count", str(len(symbols)))
        print(f"[DB] 初始下载完成: {success}/{len(symbols)} 只股票")
    except Exception as e:
        print(f"[DB] 初始下载异常: {e}")


def _background_incremental_update():
    """后续启动：增量更新到最新交易日。"""
    # 确保全A股列表已加载（首次升级后补加载）
    if _get_meta("full_stock_list_loaded") != "true":
        try:
            ensure_full_stock_list()
        except Exception:
            pass
    try:
        symbols = load_index_constituents("000300")
        if not symbols:
            _background_initial_download()
            return

        updated = 0
        total_new = 0
        for sym in symbols:
            try:
                n = update_klines(sym)
                if n > 0:
                    updated += 1
                    total_new += n
            except Exception:
                pass

        _set_meta("last_incremental", datetime.now().isoformat())
        if total_new > 0:
            print(f"[DB] 增量更新完成: {updated} 只股票有新数据，共 {total_new} 条K线")
        else:
            print("[DB] 数据已是最新")
    except Exception as e:
        print(f"[DB] 增量更新异常: {e}")


def start_background_data_task():
    """启动后台数据任务（非阻塞）。"""
    init_db()
    initialized = _get_meta("initialized")
    if initialized == "true":
        target = _background_incremental_update
    else:
        target = _background_initial_download

    t = threading.Thread(target=target, daemon=True, name="db-data-task")
    t.start()
    return t


# ═══════════════════════════════════════════════════════════════
# 历史记录
# ═══════════════════════════════════════════════════════════════

def save_analysis_result(symbol: str, name: str, type_: str,
                         result_dict: dict, time_frame: str = None):
    """保存一次分析结果到 analysis_history。type: 深度分析/智能选股/回测/实盘进化"""
    import json as _json
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO analysis_history (symbol, name, type, time_frame, result_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (symbol, name or symbol, type_, time_frame,
             _json.dumps(result_dict, ensure_ascii=False, default=str),
             datetime.now().isoformat())
        )


def save_screening_result(top5_list: list, scope: str = ""):
    """保存一次选股 Top5 结果到 screening_history。"""
    import json as _json
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO screening_history (date, scope, top5_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (datetime.now().strftime("%Y-%m-%d"), scope,
             _json.dumps(top5_list, ensure_ascii=False, default=str),
             datetime.now().isoformat())
        )


def get_analysis_history(symbol: str = None, type_: str = None,
                         limit: int = 50) -> list:
    """查询历史分析记录。返回 [{id, symbol, name, type, time_frame, result_json, created_at}]"""
    import json as _json
    query = "SELECT * FROM analysis_history WHERE 1=1"
    params = []
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol)
    if type_:
        query += " AND type = ?"
        params.append(type_)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        try:
            d["result"] = _json.loads(d.pop("result_json"))
        except Exception:
            d["result"] = {}
        results.append(d)
    return results


def get_screening_history(limit: int = 30) -> list:
    """查询历史选股记录。"""
    import json as _json
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM screening_history ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        try:
            d["top5"] = _json.loads(d.pop("top5_json"))
        except Exception:
            d["top5"] = []
        results.append(d)
    return results


def delete_analysis_record(record_id: int):
    """删除单条分析记录。"""
    with _get_conn() as conn:
        conn.execute("DELETE FROM analysis_history WHERE id=?", (record_id,))


def delete_analysis_records(record_ids: list):
    """批量删除分析记录。"""
    if not record_ids:
        return
    with _get_conn() as conn:
        conn.executemany(
            "DELETE FROM analysis_history WHERE id=?",
            [(rid,) for rid in record_ids]
        )


# ═══════════════════════════════════════════════════════════════
# 统计信息
# ═══════════════════════════════════════════════════════════════

def get_db_stats() -> dict:
    """返回数据库统计信息。"""
    with _get_conn() as conn:
        stock_count = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
        kline_count = conn.execute("SELECT COUNT(*) FROM klines").fetchone()[0]
        constituent_count = conn.execute(
            "SELECT COUNT(*) FROM index_constituents WHERE index_code='000300'"
        ).fetchone()[0]
    return {
        "stocks": stock_count,
        "klines": kline_count,
        "csi300_constituents": constituent_count,
        "initialized": _get_meta("initialized") == "true",
        "last_full_download": _get_meta("last_full_download"),
        "last_incremental": _get_meta("last_incremental"),
        "db_path": DB_PATH,
        "db_size_mb": round(os.path.getsize(DB_PATH) / 1024 / 1024, 2) if os.path.exists(DB_PATH) else 0,
    }


# ═══════════════════════════════════════════════════════════════
# 个股主力基因库
# ═══════════════════════════════════════════════════════════════

def update_stock_gene(symbol: str, name: str, gene_data: dict):
    """更新个股主力基因档案。gene_data 为各维度均值字典，自动增量更新。

    gene_data keys: ma60_alignment_days, false_breakout_prob, washout_volume_ratio,
                    pullback_depth, rally_strength, atr_level, gap_reaction
    """
    existing = get_stock_gene(symbol)
    sample = existing.get("sample_count", 0) if existing else 0
    old = sample
    new_count = gene_data.pop("sample_count", 1)
    sample = old + new_count

    def _incr(old_val, new_val):
        if old_val is None:
            old_val = 0
        return round((old_val * old + new_val * new_count) / sample, 6) if sample else new_val

    fields = {
        "avg_ma60_alignment_days": _incr(
            existing.get("avg_ma60_alignment_days", 0) if existing else 0,
            gene_data.get("ma60_alignment_days", 0)),
        "avg_false_breakout_prob": _incr(
            existing.get("avg_false_breakout_prob", 0) if existing else 0,
            gene_data.get("false_breakout_prob", 0)),
        "avg_washout_volume_ratio": _incr(
            existing.get("avg_washout_volume_ratio", 0) if existing else 0,
            gene_data.get("washout_volume_ratio", 0)),
        "avg_pullback_depth": _incr(
            existing.get("avg_pullback_depth", 0) if existing else 0,
            gene_data.get("pullback_depth", 0)),
        "avg_rally_strength": _incr(
            existing.get("avg_rally_strength", 0) if existing else 0,
            gene_data.get("rally_strength", 0)),
        "avg_atr_level": _incr(
            existing.get("avg_atr_level", 0) if existing else 0,
            gene_data.get("atr_level", 0)),
        "avg_gap_reaction": _incr(
            existing.get("avg_gap_reaction", 0) if existing else 0,
            gene_data.get("gap_reaction", 0)),
    }

    # 置信度：样本越多越高，10个样本以上满分
    confidence = min(100, round(sample * 10, 1))

    with _get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO stock_genes "
            "(symbol, name, avg_ma60_alignment_days, avg_false_breakout_prob, "
            "avg_washout_volume_ratio, avg_pullback_depth, avg_rally_strength, "
            "avg_atr_level, avg_gap_reaction, confidence_score, sample_count, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, name,
             fields["avg_ma60_alignment_days"], fields["avg_false_breakout_prob"],
             fields["avg_washout_volume_ratio"], fields["avg_pullback_depth"],
             fields["avg_rally_strength"], fields["avg_atr_level"],
             fields["avg_gap_reaction"],
             confidence, sample, datetime.now().isoformat())
        )


def get_stock_gene(symbol: str) -> dict:
    """查询个股基因档案，不存在返回空字典。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM stock_genes WHERE symbol=?", (symbol,)
        ).fetchone()
        return dict(row) if row else {}


def get_all_stock_genes(min_confidence: float = 0) -> list:
    """返回所有基因档案，可按最低置信度过滤。"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM stock_genes WHERE confidence_score >= ? ORDER BY sample_count DESC",
            (min_confidence,)
        ).fetchall()
        return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════
# 信号表现跟踪
# ═══════════════════════════════════════════════════════════════

def save_signal_outcome(symbol: str, date: str, signal_type: str, time_frame: str,
                        signal_price: float, exit_price: float = None,
                        pnl_pct: float = None, days_held: int = None,
                        trend_during: str = "", factor_snapshot: dict = None):
    """保存一次买卖信号的完整表现记录。"""
    import json as _json
    snapshot_str = _json.dumps(factor_snapshot, ensure_ascii=False) if factor_snapshot else None
    with _get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO signal_quality "
            "(symbol, date, signal_type, time_frame, signal_price, exit_price, "
            "pnl_pct, days_held, trend_during, factor_snapshot) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, date, signal_type, time_frame, signal_price, exit_price,
             pnl_pct, days_held, trend_during, snapshot_str)
        )
        return cur.lastrowid


def update_signal_outcome(record_id: int, exit_price: float, pnl_pct: float,
                          days_held: int, trend_during: str = ""):
    """更新已平仓信号的出场数据。"""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE signal_quality SET exit_price=?, pnl_pct=?, days_held=?, trend_during=? "
            "WHERE id=?",
            (exit_price, pnl_pct, days_held, trend_during, record_id)
        )


def get_signal_performance(symbol: str = None, signal_type: str = None,
                           time_frame: str = None, limit: int = 100) -> list:
    """查询信号表现记录，支持多维过滤。"""
    import json as _json
    query = "SELECT * FROM signal_quality WHERE 1=1"
    params = []
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol)
    if signal_type:
        query += " AND signal_type = ?"
        params.append(signal_type)
    if time_frame:
        query += " AND time_frame = ?"
        params.append(time_frame)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        if d.get("factor_snapshot"):
            try:
                d["factor_snapshot"] = _json.loads(d["factor_snapshot"])
            except Exception:
                pass
        results.append(d)
    return results


def get_signal_accuracy(symbol: str = None, time_frame: str = None) -> dict:
    """统计信号准确率和平均收益。"""
    query = "SELECT signal_type, COUNT(*) as cnt, AVG(pnl_pct) as avg_pnl, " \
            "SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as win_cnt " \
            "FROM signal_quality WHERE exit_price IS NOT NULL"
    params = []
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol)
    if time_frame:
        query += " AND time_frame = ?"
        params.append(time_frame)
    query += " GROUP BY signal_type"
    with _get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    result = {}
    for r in rows:
        d = dict(r)
        cnt = d["cnt"] or 0
        result[d["signal_type"]] = {
            "count": cnt,
            "avg_pnl": round(d["avg_pnl"] or 0, 2),
            "win_rate": round((d["win_cnt"] or 0) / cnt * 100, 1) if cnt else 0,
        }
    return result
