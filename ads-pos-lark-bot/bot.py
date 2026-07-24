#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bot báo cáo Meta Ads + Pancake POS -> Lark, nhóm theo thị trường.
- PH (Philippines): quy đổi tất cả về VND (spend + doanh thu Pancake)
- EU (POD): tính bằng USD (doanh thu Mayzing - sắp tích hợp)
- VN / nhóm khác: VND
Chạy: python3 bot.py            (chế độ scheduled - 15 phút/lần)
      python3 bot.py --now      (ép gửi báo cáo tổng hợp ngay)
Mapping tài khoản -> thị trường: accounts.json (sửa field "market": PH/EU/VN)
"""
import json, time, hmac, hashlib, base64, urllib.request, urllib.parse, os, sys
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

DIR = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(DIR, "config.json")))
# GitHub Actions / env overrides (secrets không nằm trong repo)
for _env, _key in [("META_TOKEN", "meta_token"), ("PANCAKE_API_KEY", "pancake_api_key"),
                   ("LARK_WEBHOOK_PH", "lark_webhook"), ("LARK_SIGN_PH", "lark_sign_secret"),
                   ("PANCAKE_SHOP_NAME", "pancake_shop_name")]:
    if os.environ.get(_env): CFG[_key] = os.environ[_env]
if os.environ.get("PANCAKE_SHOP_ID"): CFG["pancake_shop_id"] = int(os.environ["PANCAKE_SHOP_ID"])

# ---------- Gist storage (repo public không chứa dữ liệu kinh doanh) ----------
GIST_ID = os.environ.get("GIST_ID")
GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_FILES_DOWN = ["state.json", "history.json", "accounts.json", "products.json"]
GIST_FILES_UP = ["state.json", "history.json", "accounts.json", "dashboard.html"]

def _gist(method="GET", payload=None):
    req = urllib.request.Request(f"https://api.github.com/gists/{GIST_ID}",
        json.dumps(payload).encode() if payload else None,
        {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"},
        method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())

def sync_from_gist():
    if not (GIST_ID and GIST_TOKEN): return
    g = _gist()
    for fname, meta in g.get("files", {}).items():
        if fname not in GIST_FILES_DOWN: continue
        content = meta.get("content", "")
        if meta.get("truncated"):
            with urllib.request.urlopen(meta["raw_url"], timeout=60) as r:
                content = r.read().decode()
        open(os.path.join(DIR, fname), "w").write(content)
    print("gist: downloaded", [f for f in g.get("files", {}) if f in GIST_FILES_DOWN])

def sync_to_gist():
    if not (GIST_ID and GIST_TOKEN): return
    files = {}
    for fname in GIST_FILES_UP:
        p = os.path.join(DIR, fname)
        if os.path.exists(p):
            files[fname] = {"content": open(p).read()}
    _gist("PATCH", {"files": files})
    print("gist: uploaded", list(files))
ACC_PATH = os.path.join(DIR, "accounts.json")
STATE_PATH = os.path.join(DIR, "state.json")
TZ = ZoneInfo(CFG["timezone"])
MARKET_LABEL = {"PH": "🇵🇭 PHILIPPINES", "EU": "🇪🇺 EU POD", "VN": "🇻🇳 VIỆT NAM"}
MARKET_CUR = {"PH": "VND", "EU": "USD", "VN": "VND"}

def jload(p, default):
    try: return json.load(open(p))
    except Exception: return default

def jsave(p, d): json.dump(d, open(p, "w"), ensure_ascii=False, indent=1)

def http_json(url, payload=None, timeout=60):
    req = urllib.request.Request(url, json.dumps(payload).encode() if payload else None,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def _lark_sign():
    ts = str(int(time.time()))
    sign = base64.b64encode(hmac.new(f"{ts}\n{CFG['lark_sign_secret']}".encode(),
                                     b"", hashlib.sha256).digest()).decode()
    return ts, sign

def lark_send(text):
    ts, sign = _lark_sign()
    r = http_json(CFG["lark_webhook"], {"timestamp": ts, "sign": sign,
                                        "msg_type": "text", "content": {"text": text}})
    if r.get("code") != 0: print("LARK ERROR:", r)
    return r.get("code") == 0

def lark_send_card(title, md, template="blue"):
    """Tin dạng card — hỗ trợ **bôi đậm** qua lark_md."""
    ts, sign = _lark_sign()
    r = http_json(CFG["lark_webhook"], {"timestamp": ts, "sign": sign, "msg_type": "interactive",
        "card": {"header": {"title": {"tag": "plain_text", "content": title}, "template": template},
                 "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": md}}]}})
    if r.get("code") != 0: print("LARK CARD ERROR:", r)
    return r.get("code") == 0

def send_report_post(msg, template="blue"):
    """Text nhiều dòng -> card: dòng đầu = title, bôi đậm dòng quan trọng."""
    lines = msg.split("\n")
    if "🚨" in msg: template = "red"
    body = "\n".join(f"**{l}**" if l.startswith(("➡️", "📈", "🚨", "🛒")) else l
                     for l in lines[1:]).strip("\n")
    return lark_send_card(lines[0], body, template)

# ---------- FX ----------
def get_fx(state):
    """USD->rates. Cache 12h, fallback config."""
    fx = state.get("fx", {})
    if fx.get("ts", 0) > time.time() - 43200:
        return fx["rates"]
    try:
        r = http_json("https://open.er-api.com/v6/latest/USD", timeout=20)
        rates = {"VND": r["rates"]["VND"], "PHP": r["rates"]["PHP"],
                 "EUR": r["rates"]["EUR"], "USD": 1.0}
        state["fx"] = {"ts": time.time(), "rates": rates}
        return rates
    except Exception as e:
        print("FX fallback:", e)
        return state.get("fx", {}).get("rates") or {"VND": 26244.0, "PHP": 61.5, "EUR": 0.85, "USD": 1.0}

def conv(amount, from_cur, to_cur, fx):
    if from_cur == to_cur: return amount
    usd = amount / fx.get(from_cur, 1.0)
    return usd * fx.get(to_cur, 1.0)

def fmt(v, cur):
    if cur == "VND": return f"₫{v:,.0f}"
    if cur == "PHP": return f"₱{v:,.0f}"
    if cur == "EUR": return f"€{v:,.2f}"
    if cur == "USD": return f"${v:,.2f}"
    return f"{v:,.2f} {cur}"

# ---------- Meta ----------
def meta_spend(date_preset):
    url = ("https://graph.facebook.com/v20.0/me/adaccounts"
           f"?fields=name,account_id,currency,timezone_offset_hours_utc,insights.date_preset({date_preset})"
           "{spend}&limit=200&access_token=" + urllib.parse.quote(CFG["meta_token"]))
    out = []
    while url:
        d = http_json(url)
        if "error" in d: raise RuntimeError("Meta API: " + d["error"].get("message", "?"))
        for a in d.get("data", []):
            ins = a.get("insights", {}).get("data", [])
            s = float(ins[0].get("spend", 0) or 0) if ins else 0.0
            out.append({"id": "act_" + a["account_id"], "name": a["name"],
                        "currency": a["currency"], "spend": s,
                        "tz": int(a.get("timezone_offset_hours_utc", 8) or 8)})
        url = d.get("paging", {}).get("next")
    return out

# ---------- Điều chỉnh múi giờ về ngày Philippines (UTC+8) ----------
PH_TZ_OFFSET = 8

def _hourly_rows(act_id, since, until, level="account"):
    fields = "spend" + (",campaign_name" if level == "campaign" else "")
    tr = urllib.parse.quote(json.dumps({"since": since, "until": until}))
    url = (f"https://graph.facebook.com/v20.0/{act_id}/insights?level={level}"
           f"&fields={fields}&breakdowns=hourly_stats_aggregated_by_advertiser_time_zone"
           f"&time_range={tr}&time_increment=1&limit=500"
           "&access_token=" + urllib.parse.quote(CFG["meta_token"]))
    rows = []
    while url:
        d = http_json(url)
        rows += d.get("data", [])
        url = d.get("paging", {}).get("next")
    return rows

def adjusted_ph_spend(act_id, ph_date, acct_tz, level="account"):
    """Spend đúng khung ngày PH (UTC+8) cho acc có múi giờ khác.
    Ngày PH [00-24) = giờ acc [24-shift, 24) hôm trước + [0, 24-shift) hôm nay.
    -> level=account: float | level=campaign: {campaign_name: spend}"""
    shift = (PH_TZ_OFFSET - acct_tz) % 24
    since = (ph_date - timedelta(days=1)).isoformat()
    until = ph_date.isoformat()
    out = {}
    for r in _hourly_rows(act_id, since, until, level):
        hr = int(r.get("hourly_stats_aggregated_by_advertiser_time_zone", "0")[:2])
        keep = (r["date_start"] == since and hr >= 24 - shift) or \
               (r["date_start"] == until and hr < 24 - shift)
        if keep:
            k = r.get("campaign_name", "_total")
            out[k] = out.get(k, 0.0) + float(r.get("spend", 0) or 0)
    return sum(out.values()) if level == "account" else out

def apply_tz_adjustment(spends, accounts, ph_date):
    """Sửa spend của các acc PH lệch múi giờ về đúng ngày PH."""
    for a in spends:
        m = accounts.get(a["id"], {})
        if m.get("market") == "PH" and a.get("tz", 8) != PH_TZ_OFFSET and a["spend"] > 0:
            try:
                a["spend"] = adjusted_ph_spend(a["id"], ph_date, a["tz"])
                a["tz_adjusted"] = True
            except Exception as e:
                print("tz adjust fail", a["name"], e)
    return spends

def group_by_market(spends, accounts, fx):
    """-> {market: {"rows":[(name, spend_orig, cur_orig, spend_conv)], "total": x}}"""
    groups = {}
    changed = False
    for a in spends:
        meta = accounts.get(a["id"])
        if not meta:
            m = "EU" if a["currency"] == "USD" else "PH"
            accounts[a["id"]] = meta = {"name": a["name"], "currency": a["currency"],
                                        "market": m, "auto": True, "tz": a.get("tz", 8)}
            changed = True
        if a.get("tz") is not None and meta.get("tz") != a["tz"]:
            meta["tz"] = a["tz"]; changed = True
        m = meta.get("market", "PH")
        if a["spend"] <= 0: continue
        tcur = MARKET_CUR.get(m, "VND")
        sc = conv(a["spend"], a["currency"], tcur, fx)
        g = groups.setdefault(m, {"rows": [], "total": 0.0})
        g["rows"].append((a["name"], a["spend"], a["currency"], sc))
        g["total"] += sc
    for g in groups.values(): g["rows"].sort(key=lambda r: -r[3])
    if changed: jsave(ACC_PATH, accounts)
    return groups

# ---------- Pancake ----------
def pancake_orders(start_dt, end_dt):
    orders, page = [], 1
    while page <= 10:
        url = (f"https://pos.pages.fm/api/v1/shops/{CFG['pancake_shop_id']}/orders"
               f"?api_key={CFG['pancake_api_key']}&page_size=100&page_number={page}"
               f"&updateStatus=inserted_at&startDateTime={int(start_dt.timestamp())}"
               f"&endDateTime={int(end_dt.timestamp())}")
        d = http_json(url)
        data = d.get("data", [])
        orders += data
        if len(data) < 100: break
        page += 1
    return orders

def order_revenue(orders): return sum(o.get("cod", 0) or 0 for o in orders)

def order_desc(o):
    oid = o.get("system_id") or o.get("id")
    amt = o.get("cod", 0) or 0
    prov = (o.get("shipping_address") or {}).get("province_name") or ""
    qty = sum(i.get("quantity", 1) or 1 for i in (o.get("items") or []))
    return f"  • #{oid} — ₱{amt:,.0f} ({qty} sp{', ' + prov if prov else ''})"

# ---------- Report ----------
def build_report(title, spends, accounts, fx, orders_n, rev_php, note_eu=""):
    """Chỉ báo cáo các thị trường trong CFG['markets'] (mặc định PH+VN). EU do bot POD phụ trách."""
    include = CFG.get("markets", ["PH", "VN"])
    groups = group_by_market(spends, accounts, fx)
    parts = [title]
    if "PH" in include:
        ph = groups.get("PH", {"rows": [], "total": 0.0})
        rev_vnd = conv(rev_php, "PHP", "VND", fx)
        lines = "\n".join(f"  • {n}: {fmt(sc,'VND')}" + (f" ({fmt(s,c)})" if c != "VND" else "")
                          for n, s, c, sc in ph["rows"]) or "  (chưa có chi tiêu)"
        extra = (f"\n📈 Chi phí/Doanh số: {ph['total']/rev_vnd*100:.1f}% · "
                 f"ROAS {rev_vnd/ph['total']:.2f}") if ph["total"] > 0 and rev_vnd > 0 else ""
        parts.append(f"{MARKET_LABEL['PH']} (VND)\n"
                     f"(tỷ giá: $1 = ₫{fx['VND']:,.0f} · ₱1 = ₫{fx['VND']/fx['PHP']:,.0f})\n"
                     f"\n💸 Spend:\n{lines}\n"
                     f"\n➡️ Tổng spend: {fmt(ph['total'],'VND')}\n"
                     f"🛒 {CFG['pancake_shop_name']}: {orders_n} đơn · {fmt(rev_vnd,'VND')} (₱{rev_php:,.0f}){extra}")
    for m, g in groups.items():
        if m == "PH" or m not in include: continue
        cur = MARKET_CUR.get(m, "VND")
        lines = "\n".join(f"  • {n}: {fmt(sc,cur)}" for n, s, c, sc in g["rows"])
        parts.append(f"{MARKET_LABEL.get(m, m)} ({cur})\n💸 Spend:\n{lines}\n➡️ Tổng: {fmt(g['total'],cur)}")
    return "\n\n".join(parts)

def fmt_short(v):
    if abs(v) >= 1e9: return f"₫{v/1e9:.2f}B"
    if abs(v) >= 1e6: return f"₫{v/1e6:.2f}M"
    if abs(v) >= 1e3: return f"₫{v/1e3:.0f}K"
    return f"₫{v:.0f}"

def product_md(prods):
    """Markdown card: chi phí/doanh số theo sản phẩm (DS = tất cả đơn, DS Confirm = sale đã chốt)."""
    blocks = []
    for p in prods:
        def pct(rev): return f" ({p['spend_vnd']/rev*100:.0f}%)" if rev > 0 and p['spend_vnd'] > 0 else ""
        def roas(rev): return f" · ROAS {rev/p['spend_vnd']:.2f}" if p['spend_vnd'] > 0 else ""
        blocks.append(
            f"**{p['name']}**\n"
            f"Spend: ₫{p['spend_vnd']:,}\n"
            f"DS: ₫{p['rev_vnd']:,} · {p['qty']} sp{pct(p['rev_vnd'])}{roas(p['rev_vnd'])}\n"
            f"**DS Confirm: ₫{p['rev_c_vnd']:,} · {p['qty_c']} sp{pct(p['rev_c_vnd'])}{roas(p['rev_c_vnd'])}**")
    return "\n\n".join(blocks)

# ---------- Dashboard (PH) ----------
def campaign_insights(act_id, date_preset="today"):
    url = (f"https://graph.facebook.com/v20.0/{act_id}/insights?level=campaign"
           "&fields=campaign_name,spend,clicks,impressions,actions"
           f"&date_preset={date_preset}&limit=100&access_token=" + urllib.parse.quote(CFG["meta_token"]))
    d = http_json(url)
    out = []
    for r in d.get("data", []):
        pur = sum(int(a["value"]) for a in r.get("actions", [])
                  if a["action_type"] == "purchase")
        out.append({"campaign": r["campaign_name"], "spend": float(r["spend"]),
                    "clicks": int(r.get("clicks", 0) or 0), "purchases": pur})
    return out

def product_agg(orders):
    """Doanh thu theo sản phẩm (phân bổ COD theo tỷ lệ số lượng trong đơn)."""
    prod = {}
    for o in orders:
        items = o.get("items") or []
        qty_total = sum(i.get("quantity", 1) or 1 for i in items) or 1
        cod = o.get("cod", 0) or 0
        for i in items:
            name = ((i.get("variation_info") or {}).get("name") or "Khác").strip()
            q = i.get("quantity", 1) or 1
            p = prod.setdefault(name, {"qty": 0, "rev": 0.0, "orders": 0})
            p["qty"] += q; p["rev"] += cod * q / qty_total; p["orders"] += 1
    return sorted(prod.items(), key=lambda kv: -kv[1]["rev"])

def update_history(st, fx, spend_vnd_today, rev_php_today, orders_today_n):
    h = jload(os.path.join(DIR, "history.json"), {})
    today = datetime.now(TZ).date().isoformat()
    h[today] = {"spend_vnd": round(spend_vnd_today), "rev_php": round(rev_php_today, 2),
                "rev_vnd": round(conv(rev_php_today, "PHP", "VND", fx)), "orders": orders_today_n}
    # giữ 60 ngày
    for k in sorted(h)[:-60]: h.pop(k, None)
    jsave(os.path.join(DIR, "history.json"), h)
    return h

def compute_products(accounts, fx, ph_groups, orders, date_preset="today"):
    """-> (camps, prods): campaign spend + chi phí/doanh số theo sản phẩm.
    Acc lệch múi giờ (≠ UTC+8): spend campaign được chỉnh về đúng khung ngày PH."""
    ph_date = datetime.now(TZ).date() - timedelta(days=1 if date_preset == "yesterday" else 0)
    camps = []
    for aid, meta in accounts.items():
        if meta.get("market") != "PH": continue
        row = next((r for r in ph_groups.get("rows", []) if r[0] == meta["name"]), None)
        if not row: continue
        try:
            clist = campaign_insights(aid, date_preset)
            if meta.get("tz", 8) != PH_TZ_OFFSET:
                try:
                    adj = adjusted_ph_spend(aid, ph_date, meta["tz"], "campaign")
                    for c in clist: c["spend"] = adj.pop(c["campaign"], 0.0)
                    for name, sp in adj.items():  # campaign chỉ chạy trong khung giờ chênh
                        clist.append({"campaign": name, "spend": sp, "clicks": 0, "purchases": 0})
                except Exception as e:
                    print("campaign tz adjust fail", aid, e)
            for c in clist:
                if c["spend"] > 0:
                    c["account"] = meta["name"]
                    c["spend_vnd"] = round(conv(c["spend"], meta["currency"], "VND", fx))
                    camps.append(c)
        except Exception as e:
            print("campaign_insights fail", aid, e)
    camps.sort(key=lambda c: -c["spend_vnd"])
    # ---- chi phí / doanh số theo SẢN PHẨM ----
    pmap = {k: v for k, v in jload(os.path.join(DIR, "products.json"), {}).items()
            if not k.startswith("_")}
    def match_product(campaign):
        cl = campaign.lower()
        for prod, pats in pmap.items():
            if any(p.lower() in cl for p in pats): return prod
        return "Khác / chưa mapping"
    pspend = {}
    for c in camps:
        pspend[match_product(c["campaign"])] = pspend.get(match_product(c["campaign"]), 0) + c["spend_vnd"]
    exclude = CFG.get("confirmed_exclude_statuses", [0, 6, 7])  # new, hủy, xóa
    conf = product_agg([o for o in orders if o.get("status") not in exclude])
    conf = {k: v for k, v in conf}
    prods = []
    seen_names = set()
    for k, v in product_agg(orders):
        rev_vnd = round(conv(v["rev"], "PHP", "VND", fx))
        cv = conf.get(k, {"rev": 0, "qty": 0})
        rev_c_vnd = round(conv(cv["rev"], "PHP", "VND", fx))
        sp = round(pspend.get(k, 0))
        prods.append({"name": k, "qty": v["qty"], "rev_php": round(v["rev"]),
                      "rev_vnd": rev_vnd, "spend_vnd": sp,
                      "rev_c_vnd": rev_c_vnd, "qty_c": cv["qty"],
                      "roas": round(rev_vnd / sp, 2) if sp > 0 else None,
                      "roas_c": round(rev_c_vnd / sp, 2) if sp > 0 else None})
        seen_names.add(k)
    for k, sp in pspend.items():  # sản phẩm có spend nhưng chưa có đơn
        if k not in seen_names and sp > 0:
            prods.append({"name": k, "qty": 0, "rev_php": 0, "rev_vnd": 0,
                          "rev_c_vnd": 0, "qty_c": 0,
                          "spend_vnd": round(sp), "roas": 0, "roas_c": 0})
    prods.sort(key=lambda p: -p["spend_vnd"])
    return camps, prods

def gen_dashboard(camps, prods, fx, history):
    now = datetime.now(TZ)
    days = sorted(history)[-14:]
    data = {"updated": now.strftime("%H:%M %d/%m/%Y"),
            "today": history.get(now.date().isoformat(), {}),
            "trend": {"labels": [d[5:] for d in days],
                      "spend": [history[d]["spend_vnd"] for d in days],
                      "rev": [history[d]["rev_vnd"] for d in days],
                      "orders": [history[d]["orders"] for d in days]},
            "campaigns": camps[:40], "products": prods[:20],
            "fx": {"VND": fx["VND"], "PHP": fx["PHP"]}}
    tpl = open(os.path.join(DIR, "dashboard_template.html")).read()
    html = tpl.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False))
    open(os.path.join(DIR, "dashboard.html"), "w").write(html)
    print("dashboard.html updated")

# ---------- Main ----------
def main():
    force_now = "--now" in sys.argv
    sync_from_gist()
    now = datetime.now(TZ)
    st = jload(STATE_PATH, {})
    accounts = jload(ACC_PATH, {})
    first_run = not st
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    fx = get_fx(st)

    orders_today = pancake_orders(midnight, now)
    seen = set(st.get("seen_ids", []))
    ids_today = [o.get("system_id") or o.get("id") for o in orders_today]
    new_orders = [o for o in orders_today if (o.get("system_id") or o.get("id")) not in seen]
    n, rev = len(orders_today), order_revenue(orders_today)

    warn = ""
    try:
        dl = (date.fromisoformat(CFG["meta_token_expires"]) - now.date()).days
        if dl <= 7: warn = f"\n⚠️ Meta token hết hạn sau {dl} ngày — cần gia hạn!"
    except Exception: pass

    # 1. đơn mới — gộp theo SKU (không liệt kê từng đơn)
    if new_orders and not first_run:
        k, add = len(new_orders), order_revenue(new_orders)
        sku_lines = "\n".join(
            f"  • {name}: {v['orders']} đơn · {v['qty']} sp · ₱{v['rev']:,.0f}"
            for name, v in product_agg(new_orders))
        lark_send(f"🔔 +{k} ĐƠN MỚI\n{sku_lines}"
                  f"\n💰 +₱{add:,.0f}"
                  f"\n📦 Hôm nay: {n} đơn · ₱{rev:,.0f}")
        print(f"SENT sale noti ({k})")

    # 2. báo cáo hàng giờ (hoặc --now) + sản phẩm + so sánh + cảnh báo ROAS + dashboard
    if force_now or time.time() - st.get("last_hourly", 0) >= 3540:
        alert = ""
        try:
            spends = meta_spend("today")
            group_by_market(spends, accounts, fx)  # đăng ký acc mới + cập nhật tz
            apply_tz_adjustment(spends, accounts, now.date())
            msg = build_report(f"📊 TỔNG HỢP HÔM NAY — {now.strftime('%H:%M %d/%m/%Y')} giờ PH",
                               spends, accounts, fx, n, rev)
            groups = group_by_market(spends, accounts, fx)
            ph = groups.get("PH", {"rows": [], "total": 0.0})
            rev_vnd = conv(rev, "PHP", "VND", fx)
            # --- chi phí/doanh số theo sản phẩm (tin nhắn riêng) ---
            try:
                camps, prods = compute_products(accounts, fx, ph, orders_today)
                msg2 = product_md(prods)
            except Exception as e:
                camps, prods, msg2 = [], [], ""
                print("products fail:", e)
            # --- so sánh cùng giờ hôm qua (từ snapshot) ---
            snaps = st.setdefault("snapshots", {})
            hh = now.strftime("%H")
            today_key, yday_key = now.date().isoformat(), (now.date() - timedelta(days=1)).isoformat()
            snaps.setdefault(today_key, {})[hh] = {"spend": round(ph["total"]),
                                                   "rev": round(rev_vnd), "orders": n}
            for k in sorted(snaps)[:-3]: snaps.pop(k, None)
            y = snaps.get(yday_key, {}).get(hh)
            if y:
                def pct(a, b): return f"{(a-b)/b*100:+.0f}%" if b else "n/a"
                msg += (f"\n\n🕑 SO VỚI CÙNG GIỜ HÔM QUA:"
                        f"\n  Spend {fmt_short(ph['total'])} vs {fmt_short(y['spend'])} ({pct(ph['total'], y['spend'])})"
                        f"\n  Doanh thu {fmt_short(rev_vnd)} vs {fmt_short(y['rev'])} ({pct(rev_vnd, y['rev'])})"
                        f"\n  Đơn {n} vs {y['orders']} ({pct(n, y['orders'])})")
            # --- cảnh báo ROAS dưới ngưỡng ---
            thr = CFG.get("roas_alert_threshold", 2.8)
            min_spend = CFG.get("roas_alert_min_spend_vnd", 500000)
            if ph["total"] >= min_spend and rev_vnd > 0:
                roas_now = rev_vnd / ph["total"]
                if roas_now < thr:
                    alert = (f"🚨 CẢNH BÁO: ROAS {roas_now:.2f} DƯỚI NGƯỠNG {thr}\n"
                             f"(spend {fmt_short(ph['total'])} · doanh thu {fmt_short(rev_vnd)})\n\n")
            # --- dashboard + history ---
            try:
                hist = update_history(st, fx, ph["total"], rev, n)
                gen_dashboard(camps, prods, fx, hist)
            except Exception as e:
                print("dashboard fail:", e)
        except Exception as e:
            msg = f"📊 UPDATE {now.strftime('%H:%M %d/%m')}\n💸 Meta Ads: LỖI — {e}\n" \
                  f"🛒 {CFG['pancake_shop_name']}: {n} đơn · ₱{rev:,.0f}"
            msg2 = ""
        send_report_post(alert + msg + warn)
        if msg2: lark_send_card("🛒 CHI PHÍ / DOANH SỐ THEO SẢN PHẨM", msg2, "turquoise")
        st["last_hourly"] = time.time()
        print("SENT hourly")

    # 3. tổng kết hôm qua
    today_str = now.date().isoformat()
    if not force_now and now.hour >= CFG["daily_report_hour"] and st.get("last_daily") != today_str:
        y0 = midnight - timedelta(days=1)
        yo = pancake_orders(y0, midnight - timedelta(seconds=1))
        try:
            spends = meta_spend("yesterday")
            group_by_market(spends, accounts, fx)
            apply_tz_adjustment(spends, accounts, now.date() - timedelta(days=1))
            msg = build_report(f"🌅 TỔNG KẾT {y0.strftime('%d/%m/%Y')} (ngày theo giờ PH)",
                               spends, accounts, fx, len(yo), order_revenue(yo))
            try:
                ph_y = group_by_market(spends, accounts, fx).get("PH", {"rows": [], "total": 0.0})
                _, prods_y = compute_products(accounts, fx, ph_y, yo, "yesterday")
                msg2_d = product_md(prods_y)
            except Exception as e:
                msg2_d = ""
                print("daily products fail:", e)
        except Exception as e:
            msg = f"🌅 TỔNG KẾT {y0.strftime('%d/%m/%Y')}\n💸 Meta Ads: LỖI — {e}\n" \
                  f"🛒 {CFG['pancake_shop_name']}: {len(yo)} đơn · ₱{order_revenue(yo):,.0f}"
            msg2_d = ""
        send_report_post(msg + warn, "orange")
        if msg2_d: lark_send_card("🛒 CHI PHÍ / DOANH SỐ THEO SẢN PHẨM (hôm qua)", msg2_d, "turquoise")
        st["last_daily"] = today_str
        print("SENT daily")

    st["seen_ids"] = ids_today
    st["last_check"] = time.time()
    jsave(STATE_PATH, st)
    sync_to_gist()
    print(f"DONE {now.isoformat()} | orders={n} new={len(new_orders)}")

if __name__ == "__main__":
    main()
