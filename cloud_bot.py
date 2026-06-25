# -*- coding: utf-8 -*-
"""
比董的股票AI顧問 — Zeabur 雲端版
資料源：Fugle API（即時台股）+ yfinance（美股）+ Supabase（持股/記憶）
完全不依賴本機，24小時穩定運行
"""
import os, sys, io, json, hmac, hashlib, base64, threading, time
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify
import requests

# UTF-8 輸出
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

TW_TZ = ZoneInfo('Asia/Taipei')
app = Flask(__name__)

# ── 環境變數（Zeabur 注入 / 本機 .env）──────────────────────
def _load_env():
    if os.path.exists('.env'):
        for line in open('.env', encoding='utf-8'):
            line = line.strip()
            if line and '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())
_load_env()

# 全部從環境變數讀取（本機用 .env / Zeabur 用環境變數注入）
LINE_TOKEN   = os.environ.get('LINE_CHANNEL_TOKEN', '')
LINE_SECRET  = os.environ.get('LINE_CHANNEL_SECRET', '')
ANTHROPIC_KEY= os.environ.get('ANTHROPIC_API_KEY', '')
FUGLE_KEY    = os.environ.get('FUGLE_API_KEY', '')
SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')
OWNER_UID    = os.environ.get('OWNER_LINE_UID', 'U8e1e1306697d7fc37264b09586695926')

FUGLE_HEADERS = {'X-API-KEY': FUGLE_KEY}
SB_HEADERS = {
    'apikey': SUPABASE_KEY,
    'Authorization': f'Bearer {SUPABASE_KEY}',
    'Content-Type': 'application/json',
}

# ══════════════════════════════════════════════════════
# Supabase 資料層（純 REST，不用 SDK）
# ══════════════════════════════════════════════════════
def sb_get(table, query=''):
    try:
        r = requests.get(f'{SUPABASE_URL}/rest/v1/{table}?{query}',
                         headers=SB_HEADERS, timeout=8)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f'[SB] get {table} 失敗: {e}')
    return []

def sb_upsert(table, data, on_conflict='code'):
    try:
        h = dict(SB_HEADERS); h['Prefer'] = 'resolution=merge-duplicates'
        r = requests.post(f'{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}',
                          headers=h, json=data, timeout=8)
        return r.status_code in (200, 201, 204)
    except Exception as e:
        print(f'[SB] upsert {table} 失敗: {e}')
    return False

def sb_delete(table, query):
    try:
        requests.delete(f'{SUPABASE_URL}/rest/v1/{table}?{query}',
                        headers=SB_HEADERS, timeout=8)
    except Exception:
        pass


def load_portfolio():
    return sb_get('stock_portfolio', 'select=*&order=code')

def load_memory(uid):
    rows = sb_get('stock_memory', f'user_id=eq.{uid}&select=role,content&order=id')
    return [{'role': r['role'], 'content': r['content']} for r in rows]

def save_memory_turn(uid, role, content):
    sb_upsert('stock_memory', {'user_id': uid, 'role': role, 'content': content,
                               'created_at': datetime.now(TW_TZ).isoformat()},
              on_conflict='id')

def trim_memory(uid, keep=16):
    rows = sb_get('stock_memory', f'user_id=eq.{uid}&select=id&order=id.desc&limit=100')
    if len(rows) > keep:
        old_ids = [str(r['id']) for r in rows[keep:]]
        sb_delete('stock_memory', f'id=in.({",".join(old_ids)})')

def load_permanent():
    return sb_get('stock_permanent', 'select=content&order=id')

# ══════════════════════════════════════════════════════
# Fugle 即時報價（台股）
# ══════════════════════════════════════════════════════
def fugle_quote(code):
    try:
        r = requests.get(f'https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/{code}',
                         headers=FUGLE_HEADERS, timeout=6)
        if r.status_code == 200:
            d = r.json()
            price = float(d.get('lastPrice') or d.get('closePrice') or 0)
            prev  = float(d.get('previousClose') or d.get('referencePrice') or price)
            chg   = round(price - prev, 2)
            pct   = round(chg / prev * 100, 2) if prev else 0
            return {'price': price, 'prev': prev, 'chg': chg, 'pct': pct,
                    'name': d.get('name', code), 'open': d.get('openPrice', 0),
                    'high': d.get('highPrice', 0), 'low': d.get('lowPrice', 0),
                    'vol': d.get('total', {}).get('tradeVolume', 0),
                    'limit_up': d.get('isLimitUpPrice', False),
                    'limit_down': d.get('isLimitDownPrice', False)}
    except Exception as e:
        print(f'[Fugle] {code} 失敗: {e}')
    return {}

def fugle_ma(code):
    """用 Fugle 歷史 K 線算 MA"""
    try:
        r = requests.get(f'https://api.fugle.tw/marketdata/v1.0/stock/historical/candles/{code}',
                         headers=FUGLE_HEADERS, params={'timeframe': 'D', 'limit': 60}, timeout=8)
        if r.status_code == 200:
            data = r.json().get('data', [])
            closes = [c['close'] for c in reversed(data)]  # 舊到新
            if len(closes) >= 20:
                ma5  = round(sum(closes[-5:]) / 5, 2)
                ma10 = round(sum(closes[-10:]) / 10, 2)
                ma20 = round(sum(closes[-20:]) / 20, 2)
                return {'ma5': ma5, 'ma10': ma10, 'ma20': ma20}
    except Exception:
        pass
    return {}

def fugle_candles(code, limit=120):
    """從 Fugle 抓歷史日K線，回傳 list[{date, open, high, low, close, volume}]，舊→新"""
    try:
        r = requests.get(f'https://api.fugle.tw/marketdata/v1.0/stock/historical/candles/{code}',
                         headers=FUGLE_HEADERS, params={'timeframe': 'D', 'limit': limit}, timeout=10)
        if r.status_code == 200:
            data = r.json().get('data', [])
            return list(reversed(data))
    except Exception as e:
        print(f'[Fugle candles] {code}: {e}')
    return []

def detect_triangle(code):
    """偵測三角收斂型態，回傳上軌/下軌/突破點/現價/判斷"""
    candles = fugle_candles(code, 120)
    if len(candles) < 30:
        return None

    highs = [c['high'] for c in candles]
    lows  = [c['low']  for c in candles]
    close = candles[-1]['close']

    # 找近 60 根 K 棒的波段高低點（至少間隔 5 根）
    n = min(60, len(candles))
    recent = candles[-n:]
    rh = [c['high'] for c in recent]
    rl = [c['low']  for c in recent]

    # 找局部高點（前後各3根最高）
    swing_highs = []
    swing_lows  = []
    for i in range(3, len(recent)-3):
        if rh[i] == max(rh[i-3:i+4]):
            swing_highs.append((i, rh[i]))
        if rl[i] == min(rl[i-3:i+4]):
            swing_lows.append((i, rl[i]))

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return None

    # 用最後兩個高點畫下降趨勢線（上軌）
    h1, h2 = swing_highs[-2], swing_highs[-1]
    # 用最後兩個低點畫上升趨勢線（下軌）
    l1, l2 = swing_lows[-2], swing_lows[-1]

    # 判斷是否收斂：高點要越來越低，低點要越來越高
    high_declining = h2[1] < h1[1]
    low_rising     = l2[1] > l1[1]

    if not (high_declining and low_rising):
        # 不是三角收斂
        return None

    # 外推到今天（最後一根的位置 = n-1）
    today_idx = n - 1
    if h2[0] != h1[0]:
        upper = h1[1] + (h2[1]-h1[1]) * (today_idx-h1[0]) / (h2[0]-h1[0])
    else:
        upper = h2[1]
    if l2[0] != l1[0]:
        lower = l1[1] + (l2[1]-l1[1]) * (today_idx-l1[0]) / (l2[0]-l1[0])
    else:
        lower = l2[1]

    upper = round(upper, 2)
    lower = round(lower, 2)

    # 收斂幅度
    spread = round((upper - lower) / close * 100, 1) if close else 0

    # 判斷突破方向
    if close > upper:
        status = '🔥 已向上突破！'
    elif close < lower:
        status = '⚠️ 已向下跌破！'
    elif spread < 3:
        status = '⚡ 極度收斂中，即將變盤'
    else:
        status = '📐 收斂中，尚未突破'

    q = fugle_quote(code)
    name = q.get('name', code)

    return {
        'code': code, 'name': name, 'close': close,
        'upper': upper, 'lower': lower, 'spread': spread,
        'status': status,
        'high_points': [h1, h2], 'low_points': [l1, l2],
    }

def auto_watch_triangle(code):
    """偵測三角收斂並自動設定突破警報，回傳說明文字"""
    result = detect_triangle(code)
    if not result:
        q = fugle_quote(code)
        name = q.get('name', code) if q else code
        return f'📐 {code} {name}\n目前沒有明顯的三角收斂型態。\n\n如果你有特定價位要監控，可以直接說：\n「{code} 突破 XX 元通知我」'

    # 自動設定上軌突破警報
    sb_upsert('stock_alert', {
        'code':        result['code'],
        'alert_price': result['upper'],
        'direction':   'above',
        'memo':        f"三角收斂向上突破（上軌{result['upper']}）",
        'active':      True
    })
    # 自動設定下軌跌破警報
    sb_upsert('stock_alert', {
        'code':        result['code'],
        'alert_price': result['lower'],
        'direction':   'below',
        'memo':        f"三角收斂向下跌破（下軌{result['lower']}）",
        'active':      True
    })

    lines = [
        f"📐 {result['code']} {result['name']} 三角收斂分析",
        f"━━━━━━━━━━━━",
        f"現價：{result['close']}",
        f"上軌壓力：{result['upper']}",
        f"下軌支撐：{result['lower']}",
        f"收斂幅度：{result['spread']}%",
        f"狀態：{result['status']}",
        f"",
        f"✅ 已自動設定警報：",
        f"• 突破 {result['upper']} → 通知（做多訊號）",
        f"• 跌破 {result['lower']} → 通知（停損訊號）",
    ]
    return '\n'.join(lines)

def _ema(data, period):
    k = 2 / (period + 1)
    val = data[0]
    for d in data[1:]:
        val = d * k + val * (1 - k)
    return val

def detect_breakout_ready(code):
    """偵測「蓄勢待發」：漲過一波→回檔不破→均線多排→量縮→接近前高
    回傳 dict 或 None"""
    candles = fugle_candles(code, 120)
    if len(candles) < 60:
        return None

    closes  = [c['close'] for c in candles]
    highs   = [c['high'] for c in candles]
    lows    = [c['low'] for c in candles]
    volumes = [c['volume'] for c in candles]
    current = closes[-1]
    today_vol = volumes[-1]

    # 1. 均線多排 MA8 > MA21 > MA55 且全部向上
    ma8  = sum(closes[-8:]) / 8
    ma21 = sum(closes[-21:]) / 21
    ma55 = sum(closes[-55:]) / 55
    if not (ma8 > ma21 > ma55):
        return None

    # MA55 向上（跟 10 天前比）
    if len(closes) >= 65:
        ma55_10ago = sum(closes[-65:-10]) / 55
        if ma55 <= ma55_10ago:
            return None

    # 2. MACD 零軸上方
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    dif = ema12 - ema26
    if dif < 0:
        return None

    # 3. 找前高（近 30 天最高點）
    recent_high = max(highs[-30:])

    # 4. 現價接近前高但尚未突破（距離 0~5%）
    dist_pct = round((recent_high - current) / current * 100, 1)
    if dist_pct < -1 or dist_pct > 5:
        return None

    # 5. 量縮判定：近 5 天平均量 < 前 15 天平均量的 70%
    if len(volumes) >= 20:
        avg_vol_5  = sum(volumes[-5:]) / 5
        avg_vol_15 = sum(volumes[-20:-5]) / 15
        vol_shrink = avg_vol_5 < avg_vol_15 * 0.7
    else:
        vol_shrink = False

    # 6. 第一攻存在：過去 60 天內有一段 ≥15% 的漲幅
    min_60 = min(lows[-60:])
    max_60 = max(highs[-60:])
    first_wave = (max_60 - min_60) / min_60 * 100 if min_60 > 0 else 0
    if first_wave < 15:
        return None

    q = fugle_quote(code)
    name = q.get('name', code) if q else code

    return {
        'code': code, 'name': name,
        'close': current, 'recent_high': recent_high,
        'dist_pct': dist_pct,
        'vol_shrink': vol_shrink,
        'ma8': round(ma8, 2), 'ma21': round(ma21, 2), 'ma55': round(ma55, 2),
        'macd_dif': round(dif, 2),
        'first_wave_pct': round(first_wave, 1),
        'today_vol': today_vol,
    }

def daily_breakout_scan():
    """每日早盤自動掃描：找出蓄勢待發的股票，設突破警報"""
    import urllib3; urllib3.disable_warnings()
    print('[掃描] 開始每日蓄勢待發掃描...')

    # 從 TWSE + TPEX 抓今日成交量前 100 名
    stocks = []
    try:
        r = requests.get('https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL',
                         timeout=15, verify=False)
        for item in r.json():
            code = item.get('Code','').strip()
            vol  = item.get('TradeVolume','0').replace(',','')
            try: vol_k = int(vol) // 1000
            except: vol_k = 0
            if code.isdigit() and len(code) == 4 and vol_k > 500:
                stocks.append({'code': code, 'vol': vol_k})
    except Exception as e:
        print(f'[掃描] TWSE 失敗: {e}')
    try:
        r = requests.get('https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes',
                         timeout=15, verify=False)
        for item in r.json():
            code = item.get('SecuritiesCompanyCode','').strip()
            vol  = item.get('TradeVolume','0').replace(',','')
            try: vol_k = int(float(vol)) // 1000
            except: vol_k = 0
            if code.isdigit() and len(code) == 4 and vol_k > 500:
                stocks.append({'code': code, 'vol': vol_k})
    except Exception as e:
        print(f'[掃描] TPEX 失敗: {e}')

    stocks.sort(key=lambda s: s['vol'], reverse=True)
    scan_list = [s['code'] for s in stocks[:100]]
    print(f'[掃描] 掃描 {len(scan_list)} 支熱門股...')

    ready = []
    for i, code in enumerate(scan_list):
        try:
            r = detect_breakout_ready(code)
            if r:
                ready.append(r)
                # 自動設定突破前高警報
                sb_upsert('stock_alert', {
                    'code': code,
                    'alert_price': r['recent_high'],
                    'direction': 'above',
                    'memo': f"蓄勢待發！距前高{r['dist_pct']}%，{'量縮中' if r['vol_shrink'] else '量穩'}",
                    'active': True,
                })
        except Exception as e:
            print(f'[掃描] {code} 錯誤: {e}')
        # 每 20 支暫停 3 秒，避免 Fugle API 限流
        if (i + 1) % 20 == 0:
            time.sleep(3)

    print(f'[掃描] 完成，找到 {len(ready)} 支蓄勢待發')

    if ready:
        ready.sort(key=lambda x: x['dist_pct'])
        lines = ['🔍 今日蓄勢待發偵測', '━━━━━━━━━━━━']
        for r in ready[:15]:
            shrink_tag = ' 📉量縮' if r['vol_shrink'] else ''
            lines.append(
                f"⚡ {r['code']} {r['name']}\n"
                f"   現{r['close']} → 前高{r['recent_high']}（差{r['dist_pct']}%）{shrink_tag}\n"
                f"   MA多排 MACD:{r['macd_dif']:+.1f} 第一攻{r['first_wave_pct']}%"
            )
        lines.append('━━━━━━━━━━━━')
        lines.append(f'共 {len(ready)} 支符合，已自動設突破警報 🔔')
        lines.append('突破時會即時通知大人進場！')
        push_owner('\n'.join(lines))
    else:
        push_owner('🔍 今日蓄勢待發掃描完成\n暫無符合條件的股票（均線多排+量縮+接近前高）')

def taiex_quote():
    """加權指數（Fugle 代碼 IX0001）"""
    q = fugle_quote('IX0001')
    return q

# ══════════════════════════════════════════════════════
# 美股（yfinance）
# ══════════════════════════════════════════════════════
def us_index(sym, name, intraday=False):
    try:
        import yfinance as yf
        import pandas as pd
        dfd = yf.download(sym, period='5d', interval='1d', progress=False, auto_adjust=True)
        if isinstance(dfd.columns, pd.MultiIndex):
            dfd.columns = dfd.columns.get_level_values(0)
        if dfd.empty or len(dfd) < 2:
            return f"  ⚠️ {name}：無資料"
        prev = float(dfd['Close'].iloc[-2])
        if intraday:
            dfm = yf.download(sym, period='1d', interval='1m', progress=False, auto_adjust=True)
            if isinstance(dfm.columns, pd.MultiIndex):
                dfm.columns = dfm.columns.get_level_values(0)
            price = float(dfm['Close'].iloc[-1]) if not dfm.empty else float(dfd['Close'].iloc[-1])
            lbl = '盤中'
        else:
            price = float(dfd['Close'].iloc[-1]); lbl = '收盤'
        chg = price - prev; pct = chg / prev * 100 if prev else 0
        icon = '🔴' if chg >= 0 else '🟢'
        return f"  {icon} {name}：{price:,.2f}（{lbl} {chg:+.2f} / {pct:+.2f}%）"
    except Exception:
        return f"  ⚠️ {name}：資料抓取中"

def us_market_open():
    n = datetime.now(TW_TZ)
    t = n.hour * 60 + n.minute
    return t >= 22*60 or t <= 5*60

# ══════════════════════════════════════════════════════
# 系統上下文（給 AI）
# ══════════════════════════════════════════════════════
_ctx_cache = {'ts': 0, 'data': ''}
def build_context():
    # 快取 60 秒，避免每次重打 API 超時
    global _ctx_cache
    if time.time() - _ctx_cache['ts'] < 60 and _ctx_cache['data']:
        return _ctx_cache['data']
    ctx = []
    now = datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M')
    us_open = us_market_open()
    ctx.append(f'現在時間：{now}（台灣）')
    ctx.append(f'美股狀態：{"盤中（22:30~04:00）" if us_open else "休盤"}')
    ctx.append('台股：紅=漲 綠=跌，漲跌停±10%，交易09:00-13:30')

    # 加權指數
    t = taiex_quote()
    if t:
        icon = '🔴' if t['chg'] >= 0 else '🟢'
        ctx.append(f"\n【加權指數】{t['price']:,.0f} {icon}{t['chg']:+.0f}（{t['pct']:+.2f}%）")

    # 美股
    ctx.append(f'\n【美股{"即時盤中" if us_open else "昨日收盤"}】')
    for sym, name in [('^DJI','道瓊'),('^IXIC','那斯達克'),('^GSPC','標普500'),('^SOX','費半'),('^VIX','VIX')]:
        ctx.append(us_index(sym, name, us_open))

    # 持股
    port = load_portfolio()
    if port:
        ctx.append('\n【持股即時】')
        for h in port:
            try:
                q = fugle_quote(h['code'])
                if q and q['price'] > 0:
                    cost = float(h['avg_price'])
                    profit = round((q['price'] - cost) * int(h['lots']) * 1000, 0)
                    pct = round((q['price']/cost - 1) * 100, 2) if cost else 0
                    icon = '📈' if profit >= 0 else '📉'
                    tag = ' 🔴漲停' if q.get('limit_up') else (' 🟢跌停' if q.get('limit_down') else '')
                    ctx.append(f"  {h['code']} {h['name']}｜均{cost}×{h['lots']}張 現{q['price']}（{q['pct']:+.1f}%）{tag}\n"
                               f"  {icon}損益{'+' if profit>=0 else ''}{profit:,.0f}（{pct:+.1f}%）")
                else:
                    ctx.append(f"  {h['code']} {h['name']}：資料取得中")
            except Exception as e:
                ctx.append(f"  {h['code']}：{str(e)[:20]}")
    else:
        ctx.append('\n【持股】尚未登記')

    result = '\n'.join(ctx)
    _ctx_cache['ts'] = time.time()
    _ctx_cache['data'] = result
    return result

SYSTEM_PROMPT = """你是JV大人的私人股票AI顧問「比董」，透過LINE即時對話。

【風格】繁體中文、直接給重點、300字內、適當emoji、給具體建議
【台股】紅漲綠跌、漲跌停±10%、09:00-13:30
【美股時間】台灣時間22:30開盤~隔日04:00收盤（系統已標示是否盤中，不要說查不到）

【你的真實能力 — 絕對不要說你做不到】
• 你可以抓 Fugle 即時報價（現價/漲跌/成交量）
• 你可以抓 Fugle 歷史日K線（60~120天），計算MA、三角收斂、支撐壓力
• 你可以分析用戶傳來的K線截圖（有Claude Vision能力）
• 你可以設定價格警報並真正寫入資料庫（盤中每分鐘監控）
• 你可以自動偵測三角收斂的上下軌並設定突破/跌破警報
• 每天09:30自動掃描熱門股100支，找出「蓄勢待發」的股票（均線多排+量縮+接近前高），自動設突破警報

【快捷指令（系統自動處理，不走AI）】
• 持股/大盤/晨報/清除
• 盯著XXXX — 自動偵測三角收斂+設警報
• 分析XXXX — 技術速覽（報價+MA+三角收斂）
• XXXX 突破 XX元 — 設定向上突破警報
• XXXX 跌破 XX元 — 設定向下跌破警報
• 停止訂 XXXX — 刪除該股警報
• 警報清單 — 查看所有警報
• 傳圖片 — AI分析K線截圖
• 掃描 — 手動觸發蓄勢待發掃描（每天09:30也會自動跑）

【重要】遇到用戶請你「盯著」「監控」某支股票時，不要說你做不到，引導他直接打「盯著XXXX」。
遇到用戶傳K線截圖時，你會直接看到圖片內容進行分析。
投資有風險，建議僅供參考。"""

# ══════════════════════════════════════════════════════
# AI 對話
# ══════════════════════════════════════════════════════
def chat_ai(uid, msg):
    msg = msg.strip()
    # 快捷指令
    if msg in ['持股','持股狀況','我的持股','持倉']:
        return f'📊 即時狀況：\n{build_context()}'
    if msg in ['大盤','加權指數']:
        t = taiex_quote()
        if t:
            icon = '🔴' if t['chg']>=0 else '🟢'
            return f"📊 加權指數\n{icon} {t['price']:,.0f}點\n漲跌 {t['chg']:+.0f}（{t['pct']:+.2f}%）"
        return '❌ 大盤資料取得中'
    if msg in ['清除','重置','/reset']:
        sb_delete('stock_memory', f'user_id=eq.{uid}')
        return '✅ 對話記憶已清除！'
    # 停止訂閱警報：「停止訂 6194 1471」
    if msg.startswith('停止訂') or msg.startswith('取消警報') or msg.startswith('取消訂'):
        import re
        codes = re.findall(r'\d{4}', msg)
        if codes:
            removed = []
            for code in codes:
                sb_delete('stock_alert', f'code=eq.{code}')
                _alert_fired.discard(next((k for k in list(_alert_fired) if k.startswith(code+'_')), ''))
                removed.append(code)
            # 清除 _alert_fired 中相關的
            for key in list(_alert_fired):
                if any(key.startswith(c+'_') for c in codes):
                    _alert_fired.discard(key)
            remaining = sb_get('stock_alert', 'select=code,alert_price,direction,memo&active=eq.true')
            rem_txt = '\n'.join([f"• {a['code']} {a.get('direction','')} {a.get('alert_price','')} {a.get('memo','')}" for a in remaining]) or '（無）'
            return f"✅ 已從資料庫移除警報：{', '.join(removed)}\n\n📋 剩餘警報：\n{rem_txt}"
        return '⚠️ 請指定股票代碼，例如：停止訂 6194 1471'

    # 查看所有警報：「警報清單」
    if msg in ['警報清單','我的警報','訂閱清單','監控清單']:
        alerts = sb_get('stock_alert', 'select=code,alert_price,direction,memo&active=eq.true')
        if not alerts:
            return '📋 目前沒有設定任何警報'
        lines = ['📋 目前警報清單：']
        for a in alerts:
            d = '跌至' if a.get('direction')=='below' else '漲至'
            lines.append(f"• {a['code']} {d} {a.get('alert_price','')} {a.get('memo','')}")
        return '\n'.join(lines)

    # 手動觸發蓄勢待發掃描
    if msg in ['掃描','掃描蓄勢','找突破','找股票']:
        threading.Thread(target=daily_breakout_scan, daemon=True).start()
        return '🔍 蓄勢待發掃描開始！\n掃描今日成交量前100名，約需3~5分鐘\n完成後會自動推送結果 📊'

    # 盯著 / 監控三角收斂：「盯著1101三角突破」「盯1101」
    import re as _re
    watch_match = _re.match(r'(?:盯著|盯|監控|觀察|看著)\s*(\d{4})', msg)
    if watch_match:
        code = watch_match.group(1)
        return auto_watch_triangle(code)

    # 分析某支股票：「分析1101」「技術分析2330」
    analyze_match = _re.match(r'(?:分析|技術分析|看一下)\s*(\d{4})', msg)
    if analyze_match:
        code = analyze_match.group(1)
        q = fugle_quote(code)
        ma = fugle_ma(code)
        tri = detect_triangle(code)
        name = q.get('name', code) if q else code
        lines = [f'📊 {code} {name} 技術速覽']
        if q and q.get('price'):
            lines.append(f"現價 {q['price']}（{'🔴' if q['chg']>=0 else '🟢'}{q['pct']:+.1f}%）")
            lines.append(f"今日 {q.get('open','-')}/{q.get('high','-')}/{q.get('low','-')} 量{q.get('vol',0):,}")
        if ma:
            lines.append(f"MA5={ma.get('ma5','-')} MA10={ma.get('ma10','-')} MA20={ma.get('ma20','-')}")
        if tri:
            lines.append(f"\n📐 三角收斂：上軌{tri['upper']} 下軌{tri['lower']}（幅度{tri['spread']}%）")
            lines.append(f"狀態：{tri['status']}")
        else:
            lines.append('\n📐 無明顯三角收斂')
        return '\n'.join(lines)

    # 設定突破價格警報：「1101 突破 36.5 通知」「2330 跌破 950 通知」
    price_alert_match = _re.match(r'(\d{4})\s*(?:突破|漲到|漲至)\s*([\d.]+)', msg)
    price_drop_match  = _re.match(r'(\d{4})\s*(?:跌破|跌到|跌至)\s*([\d.]+)', msg)
    if price_alert_match:
        code, price = price_alert_match.group(1), float(price_alert_match.group(2))
        q = fugle_quote(code)
        name = q.get('name', code) if q else code
        sb_upsert('stock_alert', {'code': code, 'alert_price': price, 'direction': 'above', 'memo': f'突破{price}', 'active': True})
        return f'✅ 已設定：{code} {name} 突破 {price} 元時通知\n現價：{q.get("price","-")}'
    if price_drop_match:
        code, price = price_drop_match.group(1), float(price_drop_match.group(2))
        q = fugle_quote(code)
        name = q.get('name', code) if q else code
        sb_upsert('stock_alert', {'code': code, 'alert_price': price, 'direction': 'below', 'memo': f'跌破{price}', 'active': True})
        return f'✅ 已設定：{code} {name} 跌破 {price} 元時通知\n現價：{q.get("price","-")}'

    if msg.startswith('記住：') or msg.startswith('記住:'):
        content = msg.split('：',1)[-1].split(':',1)[-1].strip()
        sb_upsert('stock_permanent', {'content': content,
                  'created_at': datetime.now(TW_TZ).isoformat()}, on_conflict='id')
        return f'✅ 已永久記住：\n「{content}」🧠'

    # AI 對話
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    except ImportError:
        return '❌ anthropic 套件未安裝'

    ctx = build_context()
    perm = load_permanent()
    perm_txt = ('\n\n【永久記憶】\n' + '\n'.join(f'• {p["content"]}' for p in perm)) if perm else ''
    system = f'{SYSTEM_PROMPT}\n\n【即時資料】\n{ctx}{perm_txt}'

    history = load_memory(uid)
    history.append({'role':'user','content':msg})
    history = history[-16:]

    try:
        resp = client.messages.create(model='claude-haiku-4-5-20251001',
                                       max_tokens=700, system=system, messages=history)
        reply = resp.content[0].text
        save_memory_turn(uid, 'user', msg)
        save_memory_turn(uid, 'assistant', reply)
        trim_memory(uid)
        return reply
    except Exception as e:
        return f'❌ AI 處理失敗：{str(e)[:80]}'

# ══════════════════════════════════════════════════════
# LINE
# ══════════════════════════════════════════════════════
def verify_sig(body, sig):
    if not LINE_SECRET: return True
    mac = hmac.new(LINE_SECRET.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(mac).decode() == sig

def reply_line(token, text):
    if not LINE_TOKEN: return
    chunks = [text[i:i+2000] for i in range(0, min(len(text),6000), 2000)]
    try:
        requests.post('https://api.line.me/v2/bot/message/reply',
            headers={'Authorization':f'Bearer {LINE_TOKEN}','Content-Type':'application/json'},
            json={'replyToken':token,'messages':[{'type':'text','text':c} for c in chunks]}, timeout=10)
    except Exception as e:
        print(f'[LINE] reply 失敗: {e}')

def push_line(uid, text):
    if not LINE_TOKEN: return
    chunks = [text[i:i+2000] for i in range(0, min(len(text),6000), 2000)]
    try:
        requests.post('https://api.line.me/v2/bot/message/push',
            headers={'Authorization':f'Bearer {LINE_TOKEN}','Content-Type':'application/json'},
            json={'to':uid,'messages':[{'type':'text','text':c} for c in chunks]}, timeout=10)
    except Exception as e:
        print(f'[LINE] push 失敗: {e}')

def push_owner(text):
    push_line(OWNER_UID, text)

@app.route('/webhook', methods=['POST'])
def webhook():
    body = request.get_data()
    if not verify_sig(body, request.headers.get('X-Line-Signature','')):
        return 'bad sig', 400
    try:
        events = json.loads(body).get('events', [])
    except Exception:
        return 'bad', 400
    for ev in events:
        if ev.get('type') == 'follow':
            reply_line(ev.get('replyToken',''),
                '👋 我是比董，你的股票AI顧問！\n傳「持股」看損益\n傳「大盤」看指數 📊')
        elif ev.get('type')=='message' and ev.get('message',{}).get('type')=='text':
            uid = ev['source'].get('userId','')
            token = ev.get('replyToken','')
            text = ev['message']['text'].strip()
            def proc(u, tk, m):
                try:
                    requests.post('https://api.line.me/v2/bot/chat/loading/start',
                        headers={'Authorization':f'Bearer {LINE_TOKEN}','Content-Type':'application/json'},
                        json={'chatId':u,'loadingSeconds':30}, timeout=5)
                except Exception: pass
                try:
                    ans = chat_ai(u, m)
                except Exception as e:
                    ans = f'❌ AI 處理失敗：{type(e).__name__}: {str(e)[:60]}'
                push_line(u, ans)
            threading.Thread(target=proc, args=(uid,token,text), daemon=True).start()

        elif ev.get('type')=='message' and ev.get('message',{}).get('type')=='image':
            uid = ev['source'].get('userId','')
            msg_id = ev['message']['id']
            def proc_image(u, mid):
                try:
                    requests.post('https://api.line.me/v2/bot/chat/loading/start',
                        headers={'Authorization':f'Bearer {LINE_TOKEN}','Content-Type':'application/json'},
                        json={'chatId':u,'loadingSeconds':40}, timeout=5)
                except Exception: pass
                try:
                    # 從 LINE 下載圖片
                    img_resp = requests.get(
                        f'https://api-data.line.me/v2/bot/message/{mid}/content',
                        headers={'Authorization':f'Bearer {LINE_TOKEN}'}, timeout=15)
                    import base64
                    img_b64 = base64.standard_b64encode(img_resp.content).decode()
                    mime = img_resp.headers.get('Content-Type','image/jpeg').split(';')[0]

                    # 傳給 Claude Vision 分析
                    import anthropic
                    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
                    ctx = build_context()
                    perm = load_permanent()
                    perm_txt = ('\n\n【永久記憶】\n' + '\n'.join(f'• {p["content"]}' for p in perm)) if perm else ''
                    system = f'{SYSTEM_PROMPT}\n\n【即時資料】\n{ctx}{perm_txt}'
                    resp = client.messages.create(
                        model='claude-sonnet-4-6',
                        max_tokens=800,
                        system=system,
                        messages=[{'role':'user','content':[
                            {'type':'image','source':{'type':'base64','media_type':mime,'data':img_b64}},
                            {'type':'text','text':'請分析這張K線圖或股票截圖，給出專業的技術分析意見。包括：趨勢、支撐壓力、型態、建議操作方向。'}
                        ]}]
                    )
                    ans = resp.content[0].text
                except Exception as e:
                    ans = f'❌ 圖片分析失敗：{type(e).__name__}: {str(e)[:80]}'
                push_line(u, ans)
            threading.Thread(target=proc_image, args=(uid,msg_id), daemon=True).start()
    return 'OK', 200

@app.route('/health')
def health():
    return jsonify({'status':'ok','service':'比董股票AI顧問(雲端版)'})

@app.route('/')
def home():
    return '比董的股票AI顧問 - 雲端版運行中 📊'

# ══════════════════════════════════════════════════════
# 晨報排程 + 價格警報
# ══════════════════════════════════════════════════════
def morning_briefing():
    now = datetime.now(TW_TZ).strftime('%Y/%m/%d')
    lines = [f'🌅 比董早安晨報 {now}', '━━━━━━━━━━━━', '🇺🇸 美股昨收']
    for sym,name in [('^DJI','道瓊'),('^IXIC','那斯達克'),('^GSPC','標普500'),('^SOX','費半'),('^VIX','VIX')]:
        lines.append(us_index(sym,name,False))
    lines.append('━━━━━━━━━━━━')
    lines.append('祝大人今天順利！台股09:00開盤 📈')
    return '\n'.join(lines)

_sent_morning = set()
_sent_health  = set()
_sent_scan    = set()
_alert_fired  = set()
_self_url     = None   # 啟動後自動偵測

def self_ping():
    """自我保活：每10分鐘 ping 自己，防止 Zeabur 閒置睡著"""
    global _self_url
    if not _self_url:
        port = int(os.environ.get('PORT', 8090))
        _self_url = f'http://localhost:{port}/health'
    try:
        requests.get(_self_url, timeout=5)
        print('[keep-alive] ping ok')
    except Exception as e:
        print(f'[keep-alive] ping 失敗: {e}')

def health_check():
    """每天早上 06:00 自我健診，把結果推給大人"""
    errors = []
    ok     = []

    # 1. Fugle 報價
    try:
        q = fugle_quote('2330')
        if q and q['price'] > 0:
            ok.append(f'✅ Fugle API：台積電 {q["price"]}')
        else:
            errors.append('❌ Fugle API：報價異常')
    except Exception as e:
        errors.append(f'❌ Fugle API：{str(e)[:40]}')

    # 2. Supabase 連線
    try:
        port = sb_get('portfolio', 'select=code&limit=1')
        ok.append('✅ Supabase：連線正常')
    except Exception as e:
        errors.append(f'❌ Supabase：{str(e)[:40]}')

    # 3. Anthropic API
    try:
        import anthropic
        c = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        r = c.messages.create(model='claude-haiku-4-5-20251001',
                              max_tokens=5, messages=[{'role':'user','content':'hi'}])
        ok.append('✅ Anthropic API：正常')
    except Exception as e:
        errors.append(f'❌ Anthropic API：{str(e)[:40]}')

    # 4. 環境變數
    if not LINE_TOKEN:  errors.append('❌ LINE Token 未設定')
    else:               ok.append('✅ LINE Token：已設定')

    status = '🟢 全部正常' if not errors else f'🔴 發現 {len(errors)} 個問題'
    lines  = [f'🤖 比董健康檢查 {datetime.now(TW_TZ).strftime("%Y/%m/%d %H:%M")}',
              '━━━━━━━━━━━━', status, '']
    lines += ok
    if errors:
        lines += [''] + errors
        lines += ['', '⚠️ 請通知克勞弟查看 Zeabur 環境設定']
    push_owner('\n'.join(lines))

def scheduler():
    print('[排程] 晨報+警報+保活排程啟動')
    _last_ping = 0
    while True:
        try:
            now   = datetime.now(TW_TZ)
            today = now.strftime('%Y%m%d')
            hhmm  = now.strftime('%H:%M')

            # 自我保活：每10分鐘 ping 一次（防 Zeabur 睡著）
            if time.time() - _last_ping >= 600:
                _last_ping = time.time()
                threading.Thread(target=self_ping, daemon=True).start()

            # 每天 06:00 健康診斷
            if hhmm == '06:00' and today not in _sent_health:
                _sent_health.add(today)
                threading.Thread(target=health_check, daemon=True).start()

            # 晨報 08:50（平日）
            if hhmm == '08:50' and now.weekday() < 5 and today not in _sent_morning:
                _sent_morning.add(today)
                try: push_owner(morning_briefing())
                except Exception as e: print(f'[晨報] {e}')

            # 每日 09:30 蓄勢待發掃描（平日開盤後30分鐘）
            if hhmm == '09:30' and now.weekday() < 5 and today not in _sent_scan:
                _sent_scan.add(today)
                threading.Thread(target=daily_breakout_scan, daemon=True).start()

            # 價格警報（盤中每次檢查）
            if 9 <= now.hour <= 13 and now.weekday() < 5:
                for a in sb_get('stock_alert', 'select=*&active=eq.true'):
                    key = f"{a['code']}_{a['alert_price']}_{today}"
                    if key in _alert_fired: continue
                    q = fugle_quote(a['code'])
                    if q and q['price'] > 0:
                        hit = (a['direction']=='below' and q['price']<=float(a['alert_price'])) or \
                              (a['direction']=='above' and q['price']>=float(a['alert_price']))
                        if hit:
                            _alert_fired.add(key)
                            d = '跌至' if a['direction']=='below' else '漲至'
                            push_owner(f"🚨 價格警報！\n{a['code']} {a.get('name','')}\n現價 {q['price']} {d} {a['alert_price']}\n{a.get('memo','')}")
        except Exception as e:
            print(f'[排程] {e}')
        time.sleep(60)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8090))
    print(f'=== 比董股票AI顧問 雲端版 Port:{port} ===')
    print(f'LINE Token: {"✅" if LINE_TOKEN else "❌"}  Supabase: {"✅" if SUPABASE_KEY else "❌"}')
    threading.Thread(target=scheduler, daemon=True).start()
    app.run(host='0.0.0.0', port=port, debug=False)
