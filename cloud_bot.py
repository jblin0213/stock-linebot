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
【指令】持股/大盤/晨報/清除
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
