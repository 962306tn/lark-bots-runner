#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bot POD/EU: báo cáo Meta Ads spend (USD) cho thị trường EU về Lark riêng.
- Mỗi giờ: spend từng acc EU + top campaigns
- Sau 8h sáng: tổng kết hôm qua
- Doanh thu Mayzing: chưa tích hợp (chờ API key / VPS)
Dùng chung meta_token + accounts.json với ads-pos-lark-bot.
Chạy: python3 bot.py [--now]
"""
import json, time, hmac, hashlib, base64, urllib.request, urllib.parse, os, sys
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo

TZ8 = timezone(timedelta(hours=-8))  # múi giờ chung POD: acc ads, GA, Mayzing đều UTC-8

DIR = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(DIR, "config.json")))
SHARED = json.load(open(os.path.join(DIR, CFG["shared_config"])))
# GitHub Actions / env overrides
if os.environ.get("META_TOKEN"): SHARED["meta_token"] = os.environ["META_TOKEN"]
if os.environ.get("LARK_WEBHOOK_POD"): CFG["lark_webhook"] = os.environ["LARK_WEBHOOK_POD"]
if os.environ.get("LARK_SIGN_POD"): CFG["lark_sign_secret"] = os.environ["LARK_SIGN_POD"]

# ---------- Gist storage ----------
GIST_ID = os.environ.get("GIST_ID")
GIST_TOKEN = os.environ.get("GIST_TOKEN")

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
    f = g.get("files", {}).get("pod_state.json")
    if f: open(STATE_PATH, "w").write(f.get("content", "{}"))
    print("gist: pod_state downloaded")

def sync_to_gist():
    if not (GIST_ID and GIST_TOKEN): return
    if os.path.exists(STATE_PATH):
        _gist("PATCH", {"files": {"pod_state.json": {"content": open(STATE_PATH).read()}}})
        print("gist: pod_state uploaded")
ACC_PATH = os.path.join(DIR, CFG["accounts_db"])
STATE_PATH = os.path.join(DIR, "state.json")
TZ = ZoneInfo(CFG["timezone"])
MARKET = CFG.get("market", "EU")

def jload(p, d):
    try: return json.load(open(p))
    except Exception: return d

def http_json(url, payload=None, timeout=60):
    req = urllib.request.Request(url, json.dumps(payload).encode() if payload else None,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def lark_send(text):
    ts = str(int(time.time()))
    sign = base64.b64encode(hmac.new(f"{ts}\n{CFG['lark_sign_secret']}".encode(),
                                     b"", hashlib.sha256).digest()).decode()
    r = http_json(CFG["lark_webhook"], {"timestamp": ts, "sign": sign,
                                        "msg_type": "text", "content": {"text": text}})
    if r.get("code") != 0: print("LARK ERROR:", r)
    return r.get("code") == 0

def lark_send_card(title, md, template="blue"):
    ts = str(int(time.time()))
    sign = base64.b64encode(hmac.new(f"{ts}\n{CFG['lark_sign_secret']}".encode(),
                                     b"", hashlib.sha256).digest()).decode()
    r = http_json(CFG["lark_webhook"], {"timestamp": ts, "sign": sign, "msg_type": "interactive",
        "card": {"header": {"title": {"tag": "plain_text", "content": title}, "template": template},
                 "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": md}}]}})
    if r.get("code") != 0: print("LARK CARD ERROR:", r)
    return r.get("code") == 0

def eu_spend(date_preset):
    accounts = jload(ACC_PATH, {})
    eu_ids = {aid for aid, m in accounts.items() if m.get("market") == MARKET}
    url = ("https://graph.facebook.com/v20.0/me/adaccounts"
           f"?fields=name,account_id,currency,insights.date_preset({date_preset})"
           "{spend}&limit=200&access_token=" + urllib.parse.quote(SHARED["meta_token"]))
    rows, total = [], 0.0
    while url:
        d = http_json(url)
        if "error" in d: raise RuntimeError("Meta API: " + d["error"].get("message", "?"))
        for a in d.get("data", []):
            aid = "act_" + a["account_id"]
            if eu_ids and aid not in eu_ids:
                if aid in accounts: continue
                if a["currency"] != "USD": continue  # acc mới chưa phân loại: USD -> tạm EU
            ins = a.get("insights", {}).get("data", [])
            s = float(ins[0].get("spend", 0) or 0) if ins else 0.0
            if s > 0:
                rows.append((a["name"], s, aid))
                total += s
        url = d.get("paging", {}).get("next")
    rows.sort(key=lambda r: -r[1])
    return rows, total

def _act(actions, t):
    return sum(int(a["value"]) for a in actions if a["action_type"] == t)

def campaign_stats(act_id, date_preset):
    """-> list campaign: spend, clicks, ATC, CO, Purchase."""
    url = (f"https://graph.facebook.com/v20.0/{act_id}/insights?level=campaign"
           f"&fields=campaign_name,spend,clicks,actions&date_preset={date_preset}&limit=200"
           "&access_token=" + urllib.parse.quote(SHARED["meta_token"]))
    out = []
    while url:
        d = http_json(url)
        for r in d.get("data", []):
            acts = r.get("actions", [])
            out.append({"campaign": r["campaign_name"], "spend": float(r["spend"]),
                        "clicks": int(r.get("clicks", 0) or 0),
                        "atc": _act(acts, "add_to_cart"),
                        "co": _act(acts, "initiate_checkout"),
                        "pur": _act(acts, "purchase")})
        url = d.get("paging", {}).get("next")
    return out

def build_report(date_preset, with_campaigns=True):
    """-> markdown cho card. Tất cả acc/GA/Mayzing đều UTC-8 nên date_preset khớp ngày -8."""
    rows, total = eu_spend(date_preset)
    lines = "\n".join(f"  • {n}: ${s:,.2f}" for n, s, _ in rows) or "  (chưa có chi tiêu)"
    md = f"🇪🇺 EU POD — META ADS (USD, ngày UTC-8)\n\n💸 Spend:\n{lines}\n\n**➡️ Tổng: ${total:,.2f}**"
    if with_campaigns and rows:
        try:
            camps = []
            for _, _, aid in rows:
                camps += campaign_stats(aid, date_preset)
            clicks = sum(c["clicks"] for c in camps)
            atc = sum(c["atc"] for c in camps)
            co = sum(c["co"] for c in camps)
            pur = sum(c["pur"] for c in camps)
            cr = f"{pur/clicks*100:.2f}%" if clicks else "n/a"
            md += (f"\n\n**📦 FUNNEL: ATC {atc} · CO {co} · Purchase {pur}**"
                   f"\nClicks {clicks:,} · **CR {cr}** (Purchase/Click)")
            hot = [c for c in camps if c["atc"] or c["co"] or c["pur"]]
            hot.sort(key=lambda c: (-c["pur"], -c["co"], -c["atc"]))
            if hot:
                cl = "\n".join(
                    f"  {i+1}. {c['campaign'][:48]}: ${c['spend']:,.2f} | "
                    f"ATC {c['atc']} · CO {c['co']}" + (f" · **Pur {c['pur']}**" if c["pur"] else "")
                    for i, c in enumerate(hot[:12]))
                md += f"\n\n🔥 Designs có ATC/CO/Purchase:\n{cl}"
            else:
                md += "\n\n🔥 Chưa có design nào ra ATC/CO/Purchase"
        except Exception as e:
            print("campaigns fail:", e)
    md += "\n\n🛒 Doanh thu Mayzing: (chưa kết nối)"
    return md

def main():
    force_now = "--now" in sys.argv
    sync_from_gist()
    now = datetime.now(TZ)
    st = jload(STATE_PATH, {})

    warn = ""
    try:
        dl = (date.fromisoformat(SHARED["meta_token_expires"]) - now.date()).days
        if dl <= 7: warn = f"\n⚠️ Meta token hết hạn sau {dl} ngày!"
    except Exception: pass

    now8 = datetime.now(TZ8)
    if force_now or time.time() - st.get("last_hourly", 0) >= 3540:
        title = f"📊 POD UPDATE — ngày {now8.strftime('%d/%m')} UTC-8 ({now8.strftime('%H:%M')} US · {now.strftime('%H:%M')} VN)"
        try:
            md = build_report("today")
        except Exception as e:
            md = f"💸 LỖI Meta: {e}"
        lark_send_card(title, md + warn)
        st["last_hourly"] = time.time()
        print("SENT hourly")

    # tổng kết khi ngày UTC-8 vừa khép (00:00 US = 15:00 VN)
    today8_str = now8.date().isoformat()
    if not force_now and st.get("last_daily") != today8_str:
        y8 = now8.date() - timedelta(days=1)
        try:
            md = build_report("yesterday")
        except Exception as e:
            md = f"💸 LỖI Meta: {e}"
        lark_send_card(f"🌅 POD TỔNG KẾT {y8.strftime('%d/%m/%Y')} (UTC-8)", md + warn, "orange")
        st["last_daily"] = today8_str
        print("SENT daily")

    json.dump(st, open(STATE_PATH, "w"))
    sync_to_gist()
    print("DONE", now.isoformat())

if __name__ == "__main__":
    main()
