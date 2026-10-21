# -*- coding: utf-8 -*-
"""
بوت الربح من مشاهدة الإعلانات (AdsGram Mini App)
ملف واحد: بوت تيليجرام + سيرفر التطبيق المصغر + لوحة إدارة
التثبيت:  pip install pyTelegramBotAPI flask
"""
import os, re, time, json, hmac, hashlib, sqlite3, threading, secrets, html
from urllib.parse import parse_qsl
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None

import telebot
from telebot import types
from flask import Flask, request, jsonify, Response

# ══════════════ الإعدادات (عدّلها هنا) ══════════════
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS   = [int(x) for x in os.environ.get("ADMIN_IDS", "8924875914").split(",") if x.strip().isdigit()]
WEBAPP_URL  = os.environ.get("WEBAPP_URL", "https://watsh.onrender.com/")
ADSGRAM_BLOCK_ID = os.environ.get("ADSGRAM_BLOCK_ID", "55222")
PORT        = int(os.environ.get("PORT", 8080))
DATABASE_URL = os.environ.get("DATABASE_URL", "")
DB_FILE     = os.environ.get("DB_FILE", "ads_bot.db")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required")

DEFAULTS = {
    "ad_reward": 0.20,        # سعر الإعلان
    "ref_reward": 0.50,       # سعر الإحالة
    "min_withdraw": 10.0,     # حد السحب
    "daily_limit": 15,        # إعلانات يومية
    "min_ads_total": 80,      # إعلانات مطلوبة للسحب
    "min_refs": 5,            # دعوات مطلوبة للسحب
    "est_ad_revenue": 0.005,  # إيراد الإعلان الواحد التقديري (للإحصائيات فقط)
    "min_ad_seconds": 5,      # أقل مدة بين بدء الإعلان واكتماله
    "ad_cooldown_seconds": 5, # الفاصل بين إعلان وآخر
    "ref_paid_total": 0.0,
}
LABELS = {
    "ad_reward": "💵 سعر الإعلان ($)",
    "ref_reward": "👥 سعر الإحالة ($)",
    "min_withdraw": "🏦 حد السحب ($)",
    "daily_limit": "🎬 الإعلانات اليومية",
    "min_ads_total": "📺 إعلانات مطلوبة للسحب",
    "min_refs": "🤝 دعوات مطلوبة للسحب",
    "est_ad_revenue": "📈 إيراد الإعلان التقديري ($)",
    "min_ad_seconds": "⏱ أقل مدة مشاهدة (ثانية)",
    "ad_cooldown_seconds": "⏳ الفاصل بين الإعلانات (ثانية)",
}

# ══════════════ قاعدة البيانات ══════════════
# Neon PostgreSQL is selected automatically when DATABASE_URL is present.
# SQLite remains only as a local-development fallback.
PG = bool(DATABASE_URL)
if PG and not psycopg2:
    raise RuntimeError("Install psycopg2-binary when DATABASE_URL is configured")
if PG:
    db = psycopg2.connect(DATABASE_URL, sslmode="require")
    db.autocommit = False
else:
    db = sqlite3.connect(DB_FILE, check_same_thread=False)
    db.row_factory = sqlite3.Row
lock = threading.RLock()

def _sql(sql):
    return sql.replace("?", "%s") if PG else sql

def q(sql, args=(), one=False):
    with lock:
        cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if PG else db.cursor()
        cur.execute(_sql(sql), args)
        rows = cur.fetchall()
        cur.close()
        return (rows[0] if rows else None) if one else rows

def ex(sql, args=()):
    with lock:
        cur = db.cursor() if PG else db.cursor()
        statement = _sql(sql)
        # PostgreSQL needs RETURNING for generated IDs.
        if PG and statement.lstrip().upper().startswith("INSERT") and "RETURNING" not in statement.upper():
            table = statement.lower().split("into", 1)[1].split("(", 1)[0].strip()
            if table in ("tasks", "ad_views", "withdrawals"):
                statement += " RETURNING id"
        cur.execute(statement, args)
        rid = None
        if PG:
            if cur.description:
                row = cur.fetchone(); rid = row[0] if row else None
            db.commit()
        else:
            db.commit(); rid = cur.lastrowid
        cur.close(); return rid

def init_db():
    with lock:
        cur = db.cursor()
        if PG:
            statements = [
            """CREATE TABLE IF NOT EXISTS users(id BIGINT PRIMARY KEY, username TEXT, name TEXT, lang TEXT DEFAULT 'ar', balance DOUBLE PRECISION DEFAULT 0, earned_total DOUBLE PRECISION DEFAULT 0, ref_by BIGINT, ref_valid INTEGER DEFAULT 0, ads_total INTEGER DEFAULT 0, ads_today INTEGER DEFAULT 0, win_start DOUBLE PRECISION DEFAULT 0, pending_token TEXT, token_time DOUBLE PRECISION DEFAULT 0, last_ad_at DOUBLE PRECISION DEFAULT 0, banned INTEGER DEFAULT 0, joined DOUBLE PRECISION)""",
            """CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v DOUBLE PRECISION)""",
            """CREATE TABLE IF NOT EXISTS tasks(id BIGSERIAL PRIMARY KEY, title TEXT, kind TEXT, url TEXT, chat_id TEXT)""",
            """CREATE TABLE IF NOT EXISTS user_tasks(uid BIGINT, task_id BIGINT, PRIMARY KEY(uid,task_id))""",
            """CREATE TABLE IF NOT EXISTS ad_views(id BIGSERIAL PRIMARY KEY, uid BIGINT, ts DOUBLE PRECISION, reward DOUBLE PRECISION)""",
            """CREATE TABLE IF NOT EXISTS withdrawals(id BIGSERIAL PRIMARY KEY, uid BIGINT, amount DOUBLE PRECISION, wallet TEXT, status TEXT DEFAULT 'pending', created DOUBLE PRECISION, processed DOUBLE PRECISION)"""
            ]
            for s in statements: cur.execute(s)
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS lang TEXT DEFAULT 'ar'")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_ad_at DOUBLE PRECISION DEFAULT 0")
            cur.execute("ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS processed DOUBLE PRECISION")
        else:
            cur.executescript("""CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT, name TEXT, lang TEXT DEFAULT 'ar', balance REAL DEFAULT 0, earned_total REAL DEFAULT 0, ref_by INTEGER, ref_valid INTEGER DEFAULT 0, ads_total INTEGER DEFAULT 0, ads_today INTEGER DEFAULT 0, win_start REAL DEFAULT 0, pending_token TEXT, token_time REAL DEFAULT 0, last_ad_at REAL DEFAULT 0, banned INTEGER DEFAULT 0, joined REAL); CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v REAL); CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, kind TEXT, url TEXT, chat_id TEXT); CREATE TABLE IF NOT EXISTS user_tasks(uid INTEGER, task_id INTEGER, PRIMARY KEY(uid, task_id)); CREATE TABLE IF NOT EXISTS ad_views(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, ts REAL, reward REAL); CREATE TABLE IF NOT EXISTS withdrawals(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, amount REAL, wallet TEXT, status TEXT DEFAULT 'pending', created REAL, processed REAL);""")
            try: cur.execute("ALTER TABLE users ADD COLUMN lang TEXT DEFAULT 'ar'")
            except Exception: db.rollback()
            try: cur.execute("ALTER TABLE users ADD COLUMN last_ad_at REAL DEFAULT 0")
            except Exception: db.rollback()
        for k,v in DEFAULTS.items():
            cur.execute(_sql("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO NOTHING"), (k,v))
        db.commit(); cur.close()
init_db()

def S(k):
    r = q("SELECT v FROM settings WHERE k=?", (k,), one=True)
    return r["v"] if r else DEFAULTS[k]

def set_S(k, v):
    if PG:
        ex("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=EXCLUDED.v", (k, v))
    else:
        ex("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (k, v))

def get_user(uid):
    return q("SELECT * FROM users WHERE id=?", (uid,), one=True)

def norm(uid):
    """يصفّر العداد اليومي بعد مرور 24 ساعة من أول إعلان في الفترة"""
    u = get_user(uid)
    if u and u["win_start"] and time.time() - u["win_start"] >= 86400:
        ex("UPDATE users SET ads_today=0, win_start=0 WHERE id=?", (uid,))
        u = get_user(uid)
    return u

def wd_status(uid):
    u = norm(uid)
    tasks_total = q("SELECT COUNT(*) c FROM tasks", one=True)["c"]
    tasks_done = q("SELECT COUNT(*) c FROM user_tasks WHERE uid=? AND task_id IN (SELECT id FROM tasks)",
                   (uid,), one=True)["c"]
    minw, mrefs, mads = S("min_withdraw"), int(S("min_refs")), int(S("min_ads_total"))
    def pct(a, b): return 100 if b <= 0 else min(100, int(a * 100 / b))
    checks = [
        {"label": "الرصيد", "ok": u["balance"] >= minw, "text": f"${u['balance']:.2f} / ${minw:.2f}", "pct": pct(u["balance"], minw)},
        {"label": "الأصدقاء المدعوون", "ok": u["ref_valid"] >= mrefs, "text": f"{u['ref_valid']} / {mrefs}", "pct": pct(u["ref_valid"], mrefs)},
        {"label": "الإعلانات المشاهدة", "ok": u["ads_total"] >= mads, "text": f"{u['ads_total']} / {mads}", "pct": pct(u["ads_total"], mads)},
        {"label": "المهام", "ok": tasks_done >= tasks_total, "text": f"{tasks_done} / {tasks_total}", "pct": pct(tasks_done, tasks_total)},
    ]
    return checks, all(c["ok"] for c in checks)

def is_admin(uid):
    return uid in ADMIN_IDS

def esc(s):
    return html.escape(str(s or ""))

# ══════════════ سيرفر التطبيق المصغر ══════════════
app = Flask(__name__)

def verify_init(init_data):
    try:
        d = dict(parse_qsl(init_data, keep_blank_values=True))
        h = d.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, h):
            return None
        if time.time() - int(d.get("auth_date", 0)) > 86400:
            return None
        return json.loads(d["user"])
    except Exception:
        return None

def auth():
    data = request.get_json(silent=True) or {}
    tu = verify_init(data.get("initData", ""))
    if not tu:
        return None, data
    u = norm(tu["id"])
    if not u or u["banned"]:
        return None, data
    return u, data

def state_of(uid):
    u = norm(uid)
    limit = int(S("daily_limit"))
    reset_in = 0
    if u["ads_today"] >= limit and u["win_start"]:
        reset_in = max(0, int(u["win_start"] + 86400 - time.time()))
    checks, can = wd_status(uid)
    return {
        "balance": round(u["balance"], 4), "ads_today": u["ads_today"], "limit": limit,
        "reward": S("ad_reward"), "reset_in": reset_in, "checks": checks, "can_withdraw": can,
        "name": u["name"],
        "cooldown": max(0, int(S("ad_cooldown_seconds") - (time.time() - (u["last_ad_at"] or 0)))),
    }

@app.get("/")
def index():
    return Response(PAGE.replace("__BLOCK__", str(ADSGRAM_BLOCK_ID)), mimetype="text/html")

@app.post("/api/state")
def api_state():
    u, _ = auth()
    if not u:
        return jsonify(error="unauthorized"), 401
    return jsonify(state_of(u["id"]))

@app.post("/api/tasks")
def api_tasks():
    u,_=auth()
    if not u: return jsonify(error="unauthorized"),401
    rows=q("SELECT id,title,kind,url FROM tasks ORDER BY id")
    done={r["task_id"] for r in q("SELECT task_id FROM user_tasks WHERE uid=?",(u["id"],))}
    return jsonify(tasks=[{"id":r["id"],"title":r["title"],"kind":r["kind"],"url":r["url"],"done":r["id"] in done} for r in rows])

@app.post("/api/task")
def api_task():
    u,data=auth()
    if not u:return jsonify(error="unauthorized"),401
    t=q("SELECT * FROM tasks WHERE id=?",(int(data.get("id",0)),),one=True)
    if not t:return jsonify(error="task_not_found"),404
    if t["kind"]=="channel":
        try:
            chat=int(t["chat_id"]) if str(t["chat_id"]).lstrip("-").isdigit() else t["chat_id"]
            if bot.get_chat_member(chat,u["id"]).status not in ("member","administrator","creator"): return jsonify(error="join_channel_first"),400
        except Exception:return jsonify(error="cannot_verify_membership"),400
    ex("INSERT INTO user_tasks(uid,task_id) VALUES(?,?) ON CONFLICT(uid,task_id) DO NOTHING",(u["id"],t["id"]))
    return jsonify(ok=True)

@app.post("/api/withdraw")
def api_withdraw():
    u,data=auth()
    if not u:return jsonify(error="unauthorized"),401
    wallet=(data.get("wallet") or "").strip()
    if not re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}",wallet): return jsonify(error="invalid_trc20_wallet"),400
    checks,can=wd_status(u["id"])
    if not can:return jsonify(error="withdrawal_requirements_not_met",checks=checks),400
    with lock:
        cur=get_user(u["id"]); amount=cur["balance"]
        if amount<float(S("min_withdraw")):return jsonify(error="insufficient_balance"),400
        ex("UPDATE users SET balance=0 WHERE id=?",(u["id"],)); wid=ex("INSERT INTO withdrawals(uid,amount,wallet,created) VALUES(?,?,?,?)",(u["id"],amount,wallet,time.time()))
    for a in ADMIN_IDS:
        try:bot.send_message(a,wd_card(wid),reply_markup=wd_kb(wid))
        except Exception:pass
    return jsonify(ok=True,id=wid)

@app.post("/api/start")
def api_start():
    u, _ = auth()
    if not u:
        return jsonify(error="unauthorized"), 401
    with lock:
        u = norm(u["id"])
        cooldown = S("ad_cooldown_seconds") - (time.time() - (u["last_ad_at"] or 0))
        if cooldown > 0:
            return jsonify(error=f"انتظر {int(cooldown) + 1} ثوانٍ قبل الإعلان التالي", cooldown=int(cooldown) + 1), 429
        if u["ads_today"] >= int(S("daily_limit")):
            return jsonify(error="وصلت للحد اليومي. ستتجدد الإعلانات بعد 24 ساعة من أول إعلان."), 429
        tok = secrets.token_hex(16)
        ex("UPDATE users SET pending_token=?, token_time=? WHERE id=?", (tok, time.time(), u["id"]))
    return jsonify(token=tok)

@app.post("/api/complete")
def api_complete():
    u, data = auth()
    if not u:
        return jsonify(error="unauthorized"), 401
    with lock:
        u = norm(u["id"])
        tok = data.get("token")
        if not u["pending_token"] or tok != u["pending_token"]:
            return jsonify(error="طلب غير صالح"), 400
        if time.time() - u["token_time"] < S("min_ad_seconds"):
            ex("UPDATE users SET pending_token=NULL WHERE id=?", (u["id"],))
            return jsonify(error="لم تكتمل مشاهدة الإعلان"), 400
        if u["ads_today"] >= int(S("daily_limit")):
            return jsonify(error="وصلت للحد اليومي"), 429
        now, reward = time.time(), S("ad_reward")
        ex("""UPDATE users SET balance=balance+?, earned_total=earned_total+?,
              ads_today=ads_today+1, ads_total=ads_total+1,
              win_start=CASE WHEN win_start=0 THEN ? ELSE win_start END, last_ad_at=?,
              pending_token=NULL WHERE id=?""", (reward, reward, now, now, u["id"]))
        ex("INSERT INTO ad_views(uid,ts,reward) VALUES(?,?,?)", (u["id"], now, reward))
        # مكافأة الإحالة عند أول إعلان يشاهده المدعو
        if u["ads_total"] == 0 and u["ref_by"]:
            rr = S("ref_reward")
            ex("UPDATE users SET balance=balance+?, earned_total=earned_total+?, ref_valid=ref_valid+1 WHERE id=?",
               (rr, rr, u["ref_by"]))
            set_S("ref_paid_total", S("ref_paid_total") + rr)
            try:
                bot.send_message(u["ref_by"], f"🎉 صديقك شاهد أول إعلان! أُضيف <b>${rr:.2f}</b> لرصيدك.")
            except Exception:
                pass
    st = state_of(u["id"])
    st["earned"] = reward
    return jsonify(st)

# ══════════════ البوت ══════════════
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
BOT_USER = bot.get_me().username
tmp = {}  # حالة مؤقتة للأدمن

def btn(text, data):
    return types.InlineKeyboardButton(text, callback_data=data)

def menu(cid, uid):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("🎬 شاهد الإعلانات واربح", web_app=types.WebAppInfo(url=WEBAPP_URL)))
    kb.add(btn("📋 المهام / Tasks", "tasks"), btn("💰 رصيدي / Balance", "bal"))
    kb.add(btn("👥 دعوة الأصدقاء / Invite", "ref"), btn("💸 سحب / Withdraw", "wd"))
    kb.add(btn("🌐 العربية / English", "lang"))
    if is_admin(uid):
        kb.add(btn("⚙️ لوحة الإدارة", "adm"))
    bot.send_message(cid,
        f"👋 أهلاً بك!\n\n🎬 شاهد <b>{int(S('daily_limit'))}</b> إعلان يومياً واربح <b>${S('ad_reward'):.2f}</b> عن كل إعلان\n"
        f"👥 ادعُ أصدقاءك واربح <b>${S('ref_reward'):.2f}</b> عن كل صديق\n"
        f"💸 حد السحب <b>${S('min_withdraw'):.2f}</b>", reply_markup=kb)

@bot.message_handler(commands=["start"])
def start(m):
    uid = m.from_user.id
    if not get_user(uid):
        ref = None
        parts = (m.text or "").split()
        if len(parts) > 1 and parts[1].isdigit() and int(parts[1]) != uid and get_user(int(parts[1])):
            ref = int(parts[1])
        ex("INSERT INTO users(id,username,name,lang,ref_by,joined) VALUES(?,?,?,?,?,?)",
           (uid, m.from_user.username or "", m.from_user.first_name or "", ("en" if (m.from_user.language_code or "").startswith("en") else "ar"), ref, time.time()))
        if ref:
            try:
                bot.send_message(ref, "👥 انضم صديق عبر رابطك! ستصلك المكافأة عندما يشاهد أول إعلان.")
            except Exception:
                pass
    u = get_user(uid)
    if u["banned"]:
        return bot.send_message(m.chat.id, "🚫 حسابك محظور.")
    menu(m.chat.id, uid)

@bot.message_handler(commands=["admin"])
def admin_cmd(m):
    if is_admin(m.from_user.id):
        admin_panel(m.chat.id)

def admin_panel(cid):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(btn("📊 الإحصائيات والإيرادات", "adm_stats"), btn("💵 الأسعار والحدود", "adm_set"))
    kb.add(btn("📋 إدارة المهام", "adm_tasks"), btn("📢 نشر للجميع", "adm_bc"))
    kb.add(btn("💸 طلبات السحب", "adm_wd"), btn("🚫 حظر / فك حظر", "adm_ban"))
    kb.add(btn("🔙 القائمة", "menu"))
    bot.send_message(cid, "⚙️ <b>لوحة الإدارة</b>", reply_markup=kb)

def stats_text():
    now = time.time()
    users = q("SELECT COUNT(*) c FROM users", one=True)["c"]
    new24 = q("SELECT COUNT(*) c FROM users WHERE joined>?", (now - 86400,), one=True)["c"]
    a24 = q("SELECT COUNT(*) c, COALESCE(SUM(reward),0) s FROM ad_views WHERE ts>?", (now - 86400,), one=True)
    aall = q("SELECT COUNT(*) c, COALESCE(SUM(reward),0) s FROM ad_views", one=True)
    est = S("est_ad_revenue")
    pend = q("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM withdrawals WHERE status='pending'", one=True)
    paid = q("SELECT COALESCE(SUM(amount),0) s FROM withdrawals WHERE status='approved'", one=True)["s"]
    bal = q("SELECT COALESCE(SUM(balance),0) s FROM users", one=True)["s"]
    income_all = aall["c"] * est
    cost_all = aall["s"] + S("ref_paid_total")
    return (
        "📊 <b>الإحصائيات</b>\n\n"
        f"👤 المستخدمون: <b>{users}</b> (جدد آخر 24س: {new24})\n\n"
        f"🎬 إعلانات آخر 24س: <b>{a24['c']}</b> | إيراد تقديري ${a24['c']*est:.2f}\n"
        f"🎬 إجمالي الإعلانات: <b>{aall['c']}</b> | إيراد تقديري <b>${income_all:.2f}</b>\n\n"
        f"💰 مكافآت الإعلانات للمستخدمين: ${aall['s']:.2f}\n"
        f"👥 مكافآت الإحالات: ${S('ref_paid_total'):.2f}\n"
        f"📉 صافي تقديري: <b>${income_all - cost_all:.2f}</b>\n\n"
        f"🏦 أرصدة المستخدمين الحالية: ${bal:.2f}\n"
        f"⏳ سحوبات معلقة: {pend['c']} (${pend['s']:.2f})\n"
        f"✅ سحوبات مدفوعة: ${paid:.2f}\n\n"
        "ℹ️ الإيراد الحقيقي يظهر في لوحة AdsGram. الرقم التقديري يعتمد على "
        "«إيراد الإعلان التقديري» من إعدادات الأسعار."
    )

@bot.callback_query_handler(func=lambda c: True)
def cb(c):
    uid, d, cid = c.from_user.id, c.data, c.message.chat.id
    u = get_user(uid)
    if not u or u["banned"]:
        return bot.answer_callback_query(c.id)

    # ---- إنجاز مهمة ----
    if d.startswith("tc_"):
        tid = int(d[3:])
        t = q("SELECT * FROM tasks WHERE id=?", (tid,), one=True)
        if not t:
            return bot.answer_callback_query(c.id, "المهمة غير موجودة", show_alert=True)
        if t["kind"] == "channel":
            chat = t["chat_id"]
            chat = int(chat) if chat.lstrip("-").isdigit() else chat
            try:
                st = bot.get_chat_member(chat, uid).status
            except Exception:
                return bot.answer_callback_query(c.id, "تعذر التحقق. تأكد أن البوت مشرف في القناة.", show_alert=True)
            if st not in ("member", "administrator", "creator"):
                return bot.answer_callback_query(c.id, "❌ لم تنضم للقناة بعد", show_alert=True)
        ex("INSERT INTO user_tasks(uid,task_id) VALUES(?,?) ON CONFLICT(uid,task_id) DO NOTHING", (uid, tid))
        bot.answer_callback_query(c.id, "✅ تم إنجاز المهمة", show_alert=True)
        return show_tasks(cid, uid)

    bot.answer_callback_query(c.id)

    if d == "menu":
        menu(cid, uid)
    elif d == "lang":
        old = get_user(uid); new = "en" if old["lang"] == "ar" else "ar"
        ex("UPDATE users SET lang=? WHERE id=?", (new, uid)); bot.send_message(cid, "✅ Language changed / تم تغيير اللغة"); menu(cid, uid)
    elif d == "bal":
        u = norm(uid)
        checks, can = wd_status(uid)
        lines = "\n".join(f"{'✅' if k['ok'] else '⬜'} {k['label']}: {k['text']}" for k in checks)
        bot.send_message(cid,
            f"💰 رصيدك: <b>${u['balance']:.2f}</b>\n"
            f"🎬 إعلانات اليوم: {u['ads_today']}/{int(S('daily_limit'))}\n\n"
            f"<b>شروط السحب:</b>\n{lines}")
    elif d == "ref":
        u = get_user(uid)
        link = f"https://t.me/{BOT_USER}?start={uid}"
        bot.send_message(cid,
            f"👥 <b>الدعوات</b>\n\nاربح <b>${S('ref_reward'):.2f}</b> عن كل صديق يشاهد أول إعلان.\n"
            f"أصدقاء فعّالون: <b>{u['ref_valid']}</b>\n\n🔗 رابطك:\n<code>{link}</code>")
    elif d == "tasks":
        show_tasks(cid, uid)
    elif d.startswith("t_"):
        t = q("SELECT * FROM tasks WHERE id=?", (int(d[2:]),), one=True)
        if not t:
            return
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("🔗 فتح", url=t["url"]))
        kb.add(btn("✅ تحقق / تم", f"tc_{t['id']}"), btn("🔙 المهام", "tasks"))
        how = {"channel": "انضم للقناة ثم اضغط «تحقق».", "bot": "شغّل البوت ثم اضغط «تم».",
               "link": "افتح الرابط ثم اضغط «تم»."}[t["kind"]]
        bot.send_message(cid, f"📌 <b>{esc(t['title'])}</b>\n\n{how}", reply_markup=kb)
    elif d == "wd":
        withdraw_start(cid, uid)

    # ================= الأدمن =================
    elif d.startswith(("adm", "set_", "tk_", "tdel_", "wa_", "wr_")):
        if not is_admin(uid):
            return
        admin_cb(c, cid, uid, d)

def show_tasks(cid, uid):
    ts = q("SELECT * FROM tasks")
    if not ts:
        return bot.send_message(cid, "📋 لا توجد مهام حالياً.")
    done = {r["task_id"] for r in q("SELECT task_id FROM user_tasks WHERE uid=?", (uid,))}
    kb = types.InlineKeyboardMarkup(row_width=1)
    for t in ts:
        kb.add(btn(f"{'✅' if t['id'] in done else '⬜'} {t['title']}", f"t_{t['id']}"))
    kb.add(btn("🔙 القائمة", "menu"))
    bot.send_message(cid, f"📋 <b>المهام</b> ({len(done & {t['id'] for t in ts})}/{len(ts)})\nأكمل كل المهام لتتمكن من السحب.", reply_markup=kb)

# ---------- السحب ----------
def withdraw_start(cid, uid):
    checks, can = wd_status(uid)
    if not can:
        lines = "\n".join(f"{'✅' if k['ok'] else '❌'} {k['label']}: {k['text']}" for k in checks)
        return bot.send_message(cid, f"⚠️ لم تكتمل شروط السحب:\n\n{lines}")
    msg = bot.send_message(cid, "💸 السحب عبر <b>USDT (شبكة TRC20)</b> فقط.\nأرسل عنوان محفظتك (يبدأ بحرف T):\nللإلغاء: /cancel")
    bot.register_next_step_handler(msg, withdraw_wallet)

def withdraw_wallet(m):
    uid = m.from_user.id
    if not m.text or m.text.startswith("/"):
        return bot.send_message(m.chat.id, "تم الإلغاء.")
    if not re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", m.text.strip()):
        msg = bot.send_message(m.chat.id, "❌ العنوان غير صحيح. أرسل عنوان USDT على شبكة TRC20 (34 حرفاً ويبدأ بـ T):")
        return bot.register_next_step_handler(msg, withdraw_wallet)
    with lock:
        checks, can = wd_status(uid)
        u = get_user(uid)
        if not can:
            return bot.send_message(m.chat.id, "⚠️ لم تعد الشروط مكتملة.")
        amount = u["balance"]
        ex("UPDATE users SET balance=0 WHERE id=?", (uid,))
        wid = ex("INSERT INTO withdrawals(uid,amount,wallet,created) VALUES(?,?,?,?)", (uid, amount, m.text.strip(), time.time()))
    bot.send_message(m.chat.id, f"✅ تم إرسال طلب السحب #{wid} بمبلغ ${amount:.2f}. سيُراجع قريباً.")
    for a in ADMIN_IDS:
        try:
            bot.send_message(a, wd_card(wid), reply_markup=wd_kb(wid))
        except Exception:
            pass

def wd_card(wid):
    w = q("SELECT * FROM withdrawals WHERE id=?", (wid,), one=True)
    return (f"💸 <b>طلب سحب #{w['id']}</b>\nالمستخدم: <code>{w['uid']}</code>\n"
            f"المبلغ: <b>${w['amount']:.2f}</b>\nالمحفظة: <code>{esc(w['wallet'])}</code>")

def wd_kb(wid):
    kb = types.InlineKeyboardMarkup()
    kb.add(btn("✅ تم الدفع", f"wa_{wid}"), btn("❌ رفض وإرجاع الرصيد", f"wr_{wid}"))
    return kb

# ---------- أوامر الأدمن ----------
def admin_cb(c, cid, uid, d):
    if d == "adm":
        admin_panel(cid)
    elif d == "adm_stats":
        bot.send_message(cid, stats_text())
    elif d == "adm_set":
        kb = types.InlineKeyboardMarkup(row_width=1)
        for k, lab in LABELS.items():
            kb.add(btn(f"{lab}: {S(k):g}", f"set_{k}"))
        kb.add(btn("🔙 رجوع", "adm"))
        bot.send_message(cid, "اختر الإعداد لتغييره:", reply_markup=kb)
    elif d.startswith("set_"):
        key = d[4:]
        msg = bot.send_message(cid, f"أرسل القيمة الجديدة لـ {LABELS[key]}\n(الحالية: {S(key):g})")
        bot.register_next_step_handler(msg, lambda m: save_setting(m, key))
    elif d == "adm_tasks":
        kb = types.InlineKeyboardMarkup(row_width=1)
        for t in q("SELECT * FROM tasks"):
            kb.add(btn(f"🗑 {t['title']} ({t['kind']})", f"tdel_{t['id']}"))
        kb.add(btn("➕ إضافة مهمة", "adm_addtask"), btn("🔙 رجوع", "adm"))
        bot.send_message(cid, "📋 المهام الحالية (اضغط للحذف):", reply_markup=kb)
    elif d.startswith("tdel_"):
        tid = int(d[5:])
        ex("DELETE FROM tasks WHERE id=?", (tid,))
        ex("DELETE FROM user_tasks WHERE task_id=?", (tid,))
        bot.send_message(cid, "🗑 تم حذف المهمة.")
    elif d == "adm_addtask":
        msg = bot.send_message(cid, "أرسل عنوان المهمة (مثال: انضم لقناتنا):")
        bot.register_next_step_handler(msg, task_title)
    elif d.startswith("tk_"):
        tmp[uid]["kind"] = d[3:]
        if d == "tk_channel":
            msg = bot.send_message(cid, "أرسل معرف القناة (@channel أو -100123...). يجب أن يكون البوت مشرفاً فيها:")
            bot.register_next_step_handler(msg, task_chat)
        else:
            msg = bot.send_message(cid, "أرسل الرابط:")
            bot.register_next_step_handler(msg, task_url)
    elif d == "adm_bc":
        msg = bot.send_message(cid, "📢 أرسل أي رسالة (نص/صورة/فيديو/ملف) وسأنشرها لكل المستخدمين.\nللإلغاء: /cancel")
        bot.register_next_step_handler(msg, do_broadcast)
    elif d == "adm_wd":
        rows = q("SELECT id FROM withdrawals WHERE status='pending' ORDER BY id")
        if not rows:
            return bot.send_message(cid, "لا توجد طلبات معلقة.")
        for r in rows:
            bot.send_message(cid, wd_card(r["id"]), reply_markup=wd_kb(r["id"]))
    elif d.startswith(("wa_", "wr_")):
        wid = int(d[3:])
        w = q("SELECT * FROM withdrawals WHERE id=?", (wid,), one=True)
        if not w or w["status"] != "pending":
            return bot.send_message(cid, "الطلب معالج مسبقاً.")
        if d.startswith("wa_"):
            ex("UPDATE withdrawals SET status='approved', processed=? WHERE id=?", (wid,))
            bot.send_message(w["uid"], f"✅ تم دفع طلب السحب #{wid} بمبلغ ${w['amount']:.2f}.")
            bot.send_message(cid, f"✅ تم اعتماد #{wid}")
        else:
            ex("UPDATE withdrawals SET status='rejected', processed=? WHERE id=?", (time.time(), wid))
            ex("UPDATE users SET balance=balance+? WHERE id=?", (w["amount"], w["uid"]))
            bot.send_message(w["uid"], f"❌ رُفض طلب السحب #{wid} وأُعيد المبلغ لرصيدك.")
            bot.send_message(cid, f"❌ تم رفض #{wid} وإرجاع الرصيد")
    elif d == "adm_ban":
        msg = bot.send_message(cid, "أرسل آيدي المستخدم لتبديل حالة الحظر:")
        bot.register_next_step_handler(msg, toggle_ban)

def save_setting(m, key):
    try:
        v = float((m.text or "").replace(",", "."))
        set_S(key, v)
        bot.send_message(m.chat.id, f"✅ تم: {LABELS[key]} = {v:g}")
    except Exception:
        bot.send_message(m.chat.id, "❌ قيمة غير صالحة.")

def task_title(m):
    tmp[m.from_user.id] = {"title": m.text}
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(btn("📣 انضمام لقناة/مجموعة (يُتحقق تلقائياً)", "tk_channel"),
           btn("🤖 تشغيل بوت", "tk_bot"), btn("🔗 رابط عادي", "tk_link"))
    bot.send_message(m.chat.id, "اختر نوع المهمة:", reply_markup=kb)

def task_chat(m):
    t = tmp[m.from_user.id]
    t["chat_id"] = (m.text or "").strip()
    if t["chat_id"].startswith("@"):
        t["url"] = "https://t.me/" + t["chat_id"][1:]
        return save_task(m)
    msg = bot.send_message(m.chat.id, "أرسل رابط دعوة القناة:")
    bot.register_next_step_handler(msg, task_url)

def task_url(m):
    tmp[m.from_user.id]["url"] = (m.text or "").strip()
    save_task(m)

def save_task(m):
    t = tmp.pop(m.from_user.id)
    ex("INSERT INTO tasks(title,kind,url,chat_id) VALUES(?,?,?,?)",
       (t["title"], t["kind"], t["url"], t.get("chat_id", "")))
    bot.send_message(m.chat.id, "✅ تمت إضافة المهمة.")

def do_broadcast(m):
    if m.text == "/cancel":
        return bot.send_message(m.chat.id, "تم الإلغاء.")
    ids = [r["id"] for r in q("SELECT id FROM users WHERE banned=0")]
    bot.send_message(m.chat.id, f"⏳ جاري النشر إلى {len(ids)} مستخدم...")
    def run():
        ok = fail = 0
        for i in ids:
            try:
                bot.copy_message(i, m.chat.id, m.message_id)
                ok += 1
            except Exception:
                fail += 1
            time.sleep(0.05)
        bot.send_message(m.chat.id, f"📢 اكتمل النشر\n✅ وصل: {ok}\n❌ فشل: {fail}")
    threading.Thread(target=run, daemon=True).start()

def toggle_ban(m):
    if not (m.text or "").strip().isdigit():
        return bot.send_message(m.chat.id, "❌ آيدي غير صالح.")
    u = get_user(int(m.text))
    if not u:
        return bot.send_message(m.chat.id, "المستخدم غير موجود.")
    nb = 0 if u["banned"] else 1
    ex("UPDATE users SET banned=? WHERE id=?", (nb, u["id"]))
    bot.send_message(m.chat.id, "🚫 تم الحظر" if nb else "✅ تم فك الحظر")

# ══════════════ صفحة التطبيق المصغر ══════════════
PAGE = r"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><title>Ads Rewards</title><script src="https://telegram.org/js/telegram-web-app.js"></script><script src="https://sad.adsgram.ai/js/sad.min.js"></script><script src="//libtl.com/sdk.js" data-zone="12089458" data-sdk="show_12089458"></script><style>
:root{--bg:#0e1726;--card:#17263b;--line:#29415e;--txt:#f5f7fb;--mut:#9eb0c8;--gold:#f4b942;--ok:#56c596;--danger:#e57070}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:15px Arial,sans-serif;padding:16px 14px 82px}.top{display:flex;justify-content:space-between;align-items:center}.mut{color:var(--mut)}.balance{color:var(--gold);font-size:30px;font-weight:800;direction:ltr}.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:16px;margin-top:14px}.hero{text-align:center}.count{font-size:50px;color:var(--gold);font-weight:800;direction:ltr}.primary{width:100%;border:0;border-radius:14px;padding:15px;background:var(--gold);font-size:17px;font-weight:700;color:#231b05}.primary:disabled{opacity:.45}.tabs{position:fixed;bottom:0;left:0;right:0;background:#142238;border-top:1px solid var(--line);display:grid;grid-template-columns:repeat(3,1fr);padding:8px 5px calc(8px + env(safe-area-inset-bottom));z-index:2}.tab{border:0;background:none;color:var(--mut);font-size:12px;padding:8px}.tab.active{color:var(--gold);font-weight:700}.panel{display:none}.panel.active{display:block}.item{padding:13px 0;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:8px}.item:last-child{border:0}.smallbtn{border:1px solid var(--gold);background:transparent;color:var(--gold);border-radius:10px;padding:8px 10px}.field{width:100%;padding:13px;border-radius:10px;background:#0e1726;color:var(--txt);border:1px solid var(--line);margin:8px 0;direction:ltr}.tag{color:var(--ok)}#toast{position:fixed;top:18px;left:50%;transform:translateX(-50%);background:#fff;color:#111;border-radius:10px;padding:10px 16px;display:none;z-index:5}
</style></head><body><div id="toast"></div><div class="top"><div><div id="hello">أهلاً</div><div class="mut" id="sub">لوحة الأرباح</div></div><div class="balance" id="bal">$0.00</div></div><section id="ads" class="panel active"><div class="card hero"><div class="mut" data-ar="إعلانات اليوم" data-en="Today's ads"></div><div><span class="count" id="done">0</span> / <span id="lim">15</span></div><button class="primary" id="watch" disabled>...</button></div><div class="card"><b data-ar="معلومات السحب" data-en="Withdrawal progress"></b><div id="checks"></div></div></section><section id="tasks" class="panel"><div class="card"><h3 data-ar="المهام" data-en="Tasks"></h3><div class="mut" data-ar="أكمل المهام لفتح السحب" data-en="Complete tasks to unlock withdrawals"></div><div id="tasklist"></div></div></section><section id="withdraw" class="panel"><div class="card"><h3 data-ar="السحب عبر USDT TRC20" data-en="USDT TRC20 withdrawal"></h3><div class="mut" data-ar="أرسل عنوانًا يبدأ بحرف T. سيصل الطلب إلى لوحة الإدارة." data-en="Enter a wallet address starting with T. The request goes to admin panel."></div><input id="wallet" class="field" placeholder="T..." maxlength="34"><button class="primary" id="wd" data-ar="إرسال طلب السحب" data-en="Submit withdrawal"></button><div id="wdinfo" class="mut" style="margin-top:12px"></div></div></section><nav class="tabs"><button class="tab active" data-tab="ads">🎬<br><span data-ar="الإعلانات" data-en="Ads"></span></button><button class="tab" data-tab="tasks">📋<br><span data-ar="المهام" data-en="Tasks"></span></button><button class="tab" data-tab="withdraw">💸<br><span data-ar="السحب" data-en="Withdraw"></span></button></nav><script>
const tg=window.Telegram.WebApp;tg.ready();tg.expand();const BLOCK="__BLOCK__";let ad=null,st=null,lang=(tg.initDataUnsafe.user?.language_code||'ar').startsWith('en')?'en':'ar';try{ad=Adsgram.init({blockId:BLOCK})}catch(e){}const $=x=>document.getElementById(x);function toast(x){$('toast').textContent=x;$('toast').style.display='block';setTimeout(()=>$('toast').style.display='none',2600)}function tr(){document.querySelectorAll('[data-ar]').forEach(e=>e.textContent=e.dataset[lang]);document.documentElement.lang=lang;document.documentElement.dir=lang==='ar'?'rtl':'ltr'}async function api(p,b={}){let r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({initData:tg.initData,...b})});return r.json()}function render(d){st=d;$('hello').textContent=(lang==='ar'?'أهلاً ':'Hello ')+(d.name||'');$('bal').textContent='$'+Number(d.balance).toFixed(2);$('done').textContent=d.ads_today;$('lim').textContent=d.limit;$('watch').disabled=d.ads_today>=d.limit||d.cooldown>0;$('watch').textContent=d.cooldown>0?(lang==='ar'?'انتظر '+d.cooldown+' ثوانٍ':'Wait '+d.cooldown+' seconds'):d.ads_today>=d.limit?(lang==='ar'?'اكتمل الحد اليومي':'Daily limit reached'):(lang==='ar'?'شاهد إعلانًا واربح $':'Watch an ad and earn $')+Number(d.reward).toFixed(2);$('checks').innerHTML=d.checks.map(c=>'<div class="item"><span>'+(c.ok?'✅ ':'⬜ ')+c.label+'</span><span>'+c.text+'</span></div>').join('');tr();if(d.cooldown>0){let n=d.cooldown;let t=setInterval(()=>{n--;if(n<=0){clearInterval(t);load()}else{$('watch').disabled=true;$('watch').textContent=(lang==='ar'?'انتظر ':'Wait ')+n+(lang==='ar'?' ثوانٍ':' seconds')}},1000)}}async function load(){let d=await api('/api/state');if(d.error)return toast(lang==='ar'?'افتح التطبيق من البوت':'Open the app from the bot');render(d);let t=await api('/api/tasks');if(t.tasks)$('tasklist').innerHTML=t.tasks.map(x=>'<div class="item"><span>'+x.title+'</span><button class="smallbtn" onclick="doTask('+x.id+')">'+(x.done?'✅':'تحقق')+'</button></div>').join('')}async function doTask(id){let d=await api('/api/task',{id});toast(d.error||'✅');load()}$('watch').onclick=async()=>{let s=await api('/api/start');if(s.error)return toast(s.error);try{if(!ad)throw 0;await ad.show();let d=await api('/api/complete',{token:s.token});if(d.error)toast(d.error);else{render(d);toast('+$'+Number(d.earned).toFixed(2))}}catch(e){toast(lang==='ar'?'لم يكتمل الإعلان':'Ad was not completed')}};$('wd').onclick=async()=>{let w=$('wallet').value.trim();if(!/^T[1-9A-HJ-NP-Za-km-z]{33}$/.test(w))return toast(lang==='ar'?'عنوان TRC20 غير صحيح':'Invalid TRC20 address');let d=await api('/api/withdraw',{wallet:w});toast(d.error||('✅ '+(lang==='ar'?'تم إرسال الطلب':'Request submitted')));if(!d.error)load()};document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab,.panel').forEach(x=>x.classList.remove('active'));b.classList.add('active');$(b.dataset.tab).classList.add('active')});tr();load();</script></body></html>"""


# ══════════════ التشغيل ══════════════
if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT, use_reloader=False), daemon=True).start()
    print("✅ Bot & Mini App running on port", PORT)
    bot.infinity_polling(skip_pending=True)
