# -*- coding: utf-8 -*-
"""
بوت الربح من مشاهدة الإعلانات (AdsGram Mini App)
ملف واحد: بوت تيليجرام + سيرفر التطبيق المصغر + لوحة إدارة
التثبيت:  pip install pyTelegramBotAPI flask
"""
import os, re, time, json, hmac, hashlib, sqlite3, threading, secrets, html
from urllib.parse import parse_qsl

import telebot
from telebot import types
from flask import Flask, request, jsonify, Response

# ══════════════ الإعدادات (عدّلها هنا) ══════════════
BOT_TOKEN   = "8851431969:AAHJ1GgFAOM-KfSf2XExHXAQm4vi2BepSZs"
ADMIN_IDS   = [8924875914]                 # آيديات الأدمن
WEBAPP_URL  = "https://watermelon.fps.ms"   # رابط HTTPS الذي يشير لهذا السيرفر
ADSGRAM_BLOCK_ID = "1234"                 # Block ID (Rewarded) من partner.adsgram.ai
PORT        = int(os.environ.get("PORT", 8080))
DB_FILE     = "ads_bot.db"

DEFAULTS = {
    "ad_reward": 0.02,        # سعر الإعلان
    "ref_reward": 0.04,       # سعر الإحالة
    "min_withdraw": 10.0,     # حد السحب
    "daily_limit": 15,        # إعلانات يومية
    "min_ads_total": 80,      # إعلانات مطلوبة للسحب
    "min_refs": 5,            # دعوات مطلوبة للسحب
    "est_ad_revenue": 0.005,  # إيراد الإعلان الواحد التقديري (للإحصائيات فقط)
    "min_ad_seconds": 5,      # أقل مدة بين بدء الإعلان واكتماله
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
}

# ══════════════ قاعدة البيانات ══════════════
db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row
lock = threading.RLock()

def q(sql, args=(), one=False):
    with lock:
        cur = db.execute(sql, args)
        return cur.fetchone() if one else cur.fetchall()

def ex(sql, args=()):
    with lock:
        cur = db.execute(sql, args)
        db.commit()
        return cur.lastrowid

with lock:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY, username TEXT, name TEXT,
        balance REAL DEFAULT 0, earned_total REAL DEFAULT 0,
        ref_by INTEGER, ref_valid INTEGER DEFAULT 0,
        ads_total INTEGER DEFAULT 0, ads_today INTEGER DEFAULT 0,
        win_start REAL DEFAULT 0, pending_token TEXT, token_time REAL DEFAULT 0,
        banned INTEGER DEFAULT 0, joined REAL);
    CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v REAL);
    CREATE TABLE IF NOT EXISTS tasks(
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, kind TEXT, url TEXT, chat_id TEXT);
    CREATE TABLE IF NOT EXISTS user_tasks(uid INTEGER, task_id INTEGER, PRIMARY KEY(uid, task_id));
    CREATE TABLE IF NOT EXISTS ad_views(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, ts REAL, reward REAL);
    CREATE TABLE IF NOT EXISTS withdrawals(
        id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, amount REAL,
        wallet TEXT, status TEXT DEFAULT 'pending', created REAL);
    """)
    for k, v in DEFAULTS.items():
        db.execute("INSERT OR IGNORE INTO settings(k,v) VALUES(?,?)", (k, v))
    db.commit()

def S(k):
    r = q("SELECT v FROM settings WHERE k=?", (k,), one=True)
    return r["v"] if r else DEFAULTS[k]

def set_S(k, v):
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

@app.post("/api/start")
def api_start():
    u, _ = auth()
    if not u:
        return jsonify(error="unauthorized"), 401
    with lock:
        u = norm(u["id"])
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
              win_start=CASE WHEN win_start=0 THEN ? ELSE win_start END,
              pending_token=NULL WHERE id=?""", (reward, reward, now, u["id"]))
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
    kb.add(btn("📋 المهام", "tasks"), btn("💰 رصيدي", "bal"))
    kb.add(btn("👥 دعوة الأصدقاء", "ref"), btn("💸 سحب الأرباح", "wd"))
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
        ex("INSERT INTO users(id,username,name,ref_by,joined) VALUES(?,?,?,?,?)",
           (uid, m.from_user.username or "", m.from_user.first_name or "", ref, time.time()))
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
        ex("INSERT OR IGNORE INTO user_tasks(uid,task_id) VALUES(?,?)", (uid, tid))
        bot.answer_callback_query(c.id, "✅ تم إنجاز المهمة", show_alert=True)
        return show_tasks(cid, uid)

    bot.answer_callback_query(c.id)

    if d == "menu":
        menu(cid, uid)
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
            ex("UPDATE withdrawals SET status='approved' WHERE id=?", (wid,))
            bot.send_message(w["uid"], f"✅ تم دفع طلب السحب #{wid} بمبلغ ${w['amount']:.2f}.")
            bot.send_message(cid, f"✅ تم اعتماد #{wid}")
        else:
            ex("UPDATE withdrawals SET status='rejected' WHERE id=?", (wid,))
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
PAGE = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>شاهد واربح</title>
<link href="https://fonts.googleapis.com/css2?family=Tajawal:wght@400;500;700;800&display=swap" rel="stylesheet">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script src="https://sad.adsgram.ai/js/sad.min.js"></script>
<style>
:root{--bg:#0f1b2d;--card:#172a44;--line:#27405f;--txt:#efe8d8;--mut:#93a4ba;--gold:#f3b93f;--ok:#5cbf94;--bad:#e5735f}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--txt);font-family:'Tajawal',system-ui,sans-serif;padding:20px 18px 40px;min-height:100vh}
.top{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:8px}
.hi{color:var(--mut);font-size:14px}
.bal{font-size:40px;font-weight:800;color:var(--gold);letter-spacing:-1px;direction:ltr;font-variant-numeric:tabular-nums}
.ringwrap{position:relative;width:230px;height:230px;margin:14px auto 10px}
svg{transform:rotate(-90deg)}
.track{stroke:var(--line)}
.arc{stroke:var(--gold);stroke-linecap:round;transition:stroke-dashoffset .7s cubic-bezier(.2,.8,.2,1)}
.center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
.count{font-size:54px;font-weight:800;line-height:1;direction:ltr;font-variant-numeric:tabular-nums}
.count small{font-size:24px;color:var(--mut);font-weight:500}
.sub{color:var(--mut);font-size:14px;margin-top:6px}
button.go{width:100%;border:0;border-radius:16px;padding:17px;font:800 18px 'Tajawal';background:var(--gold);color:#241a02;cursor:pointer;transition:transform .12s,opacity .2s}
button.go:active{transform:scale(.98)}
button.go:disabled{background:var(--line);color:var(--mut);cursor:not-allowed}
button.go:focus-visible{outline:3px solid var(--txt);outline-offset:3px}
h2{font-size:17px;margin:30px 0 4px}
.hint{color:var(--mut);font-size:13px;margin:0 0 14px}
.row{padding:12px 0;border-top:1px solid var(--line)}
.row:first-of-type{border-top:0}
.rh{display:flex;justify-content:space-between;font-size:15px;margin-bottom:8px}
.rh b{direction:ltr;font-weight:500;color:var(--mut);font-variant-numeric:tabular-nums}
.row.ok .rh b{color:var(--ok)}
.bar{height:6px;border-radius:6px;background:var(--line);overflow:hidden}
.bar i{display:block;height:100%;background:var(--gold);border-radius:6px;transition:width .6s}
.row.ok .bar i{background:var(--ok)}
.note{margin-top:18px;padding:12px 14px;border-radius:12px;background:var(--card);color:var(--mut);font-size:13px;line-height:1.7}
#toast{position:fixed;bottom:24px;left:50%;transform:translate(-50%,30px);background:var(--txt);color:#1a1a1a;padding:12px 20px;border-radius:14px;font-weight:700;opacity:0;transition:.3s;pointer-events:none;max-width:88%;text-align:center}
#toast.show{opacity:1;transform:translate(-50%,0)}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style></head>
<body>
<div class="top"><div><div class="hi" id="hi">أهلاً</div><div class="hi">رصيدك الحالي</div></div><div class="bal" id="bal">$0.00</div></div>

<div class="ringwrap">
  <svg width="230" height="230" viewBox="0 0 230 230">
    <circle class="track" cx="115" cy="115" r="100" fill="none" stroke-width="14"/>
    <circle class="arc" id="arc" cx="115" cy="115" r="100" fill="none" stroke-width="14" stroke-dasharray="628.3" stroke-dashoffset="628.3"/>
  </svg>
  <div class="center"><div class="count"><span id="done">0</span><small>/<span id="lim">15</span></small></div><div class="sub">إعلانات اليوم</div></div>
</div>

<button class="go" id="go" disabled>جارٍ التحميل...</button>

<h2>شروط السحب</h2>
<p class="hint">أكمل الشروط الأربعة ثم اطلب السحب من البوت.</p>
<div id="checks"></div>
<div class="note">تتجدد إعلانات اليوم بعد 24 ساعة من مشاهدة أول إعلان. أكمل الإعلان حتى النهاية ليُحتسب لك الربح.</div>
<div id="toast"></div>

<script>
const tg=window.Telegram.WebApp;tg.ready();tg.expand();
const BLOCK="__BLOCK__";
let AdC=null;
try{AdC=window.Adsgram.init({blockId:BLOCK})}catch(e){}
const $=id=>document.getElementById(id);
let st=null,resetAt=0,busy=false;

async function api(p,body){
  const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(Object.assign({initData:tg.initData},body||{}))});
  return r.json();
}
function toast(t){const e=$('toast');e.textContent=t;e.className='show';clearTimeout(e._t);e._t=setTimeout(()=>e.className='',2600)}
function fmt(sec){const h=Math.floor(sec/3600),m=Math.floor(sec%3600/60),x=sec%60;return [h,m,x].map(n=>String(n).padStart(2,'0')).join(':')}

function render(d){
  st=d;
  $('hi').textContent='أهلاً '+(d.name||'');
  $('bal').textContent='$'+d.balance.toFixed(2);
  $('done').textContent=d.ads_today;$('lim').textContent=d.limit;
  $('arc').style.strokeDashoffset=628.3*(1-Math.min(1,d.ads_today/d.limit));
  resetAt=d.reset_in?Date.now()/1000+d.reset_in:0;
  $('checks').innerHTML=d.checks.map(c=>`<div class="row ${c.ok?'ok':''}"><div class="rh"><span>${c.ok?'✓ ':''}${c.label}</span><b>${c.text}</b></div><div class="bar"><i style="width:${c.pct}%"></i></div></div>`).join('');
  tick();
}
function tick(){
  const b=$('go');if(!st||busy)return;
  if(st.ads_today>=st.limit){
    const left=Math.max(0,Math.round(resetAt-Date.now()/1000));
    b.disabled=true;b.textContent=left>0?'تتجدد بعد '+fmt(left):'حدّث الصفحة';
    if(left===0&&resetAt)load();
  }else{b.disabled=false;b.textContent='شاهد إعلاناً واربح $'+st.reward.toFixed(2)}
}
setInterval(tick,1000);

async function load(){const d=await api('/api/state');if(d.error){$('go').textContent='افتح التطبيق من داخل البوت';return}render(d)}

$('go').onclick=async()=>{
  if(busy)return;busy=true;$('go').disabled=true;$('go').textContent='جارٍ تحميل الإعلان...';
  try{
    const r=await api('/api/start');
    if(r.error){toast(r.error);return}
    if(!AdC){toast('الإعلان غير جاهز، حاول بعد قليل');return}
    await AdC.show();
    const c=await api('/api/complete',{token:r.token});
    if(c.error){toast(c.error);return}
    render(c);toast('+ $'+c.earned.toFixed(2)+' أُضيفت لرصيدك');
    tg.HapticFeedback&&tg.HapticFeedback.notificationOccurred('success');
  }catch(e){toast('لم يكتمل الإعلان، جرّب مرة أخرى')}
  finally{busy=false;tick()}
};
load();
</script></body></html>
"""

# ══════════════ التشغيل ══════════════
if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT, use_reloader=False), daemon=True).start()
    print("✅ Bot & Mini App running on port", PORT)
    bot.infinity_polling(skip_pending=True)
