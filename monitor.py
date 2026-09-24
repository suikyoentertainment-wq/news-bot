"""
SUIKYO 当局一次情報モニター（v2）
RSSを監視 → 新着だけを日本語要約 → Discord に通知（X投稿案つき）

v2 の変更点
 1. 公表から MAX_AGE_HOURS 以上経った項目は送らない（既読扱いにして捨てる）
 2. 国内ソースは「要約」、海外ソースは「非公式訳」と表記を分ける
 3. 定例統計などをタイトルのキーワードで除外（API費用もかからない）
    重要度「低」と判定されたものも送らない
 4. PDF記事は本文を無理に読まず、AIに「書かれていないことを補うな」と明示
 5. 要約の数値ルールを追加（概数は四捨五入＋「約」）
 6. 通知に公表日時を表示
"""
import os
import re
import json
import time
import hashlib
import calendar
from datetime import datetime, timezone, timedelta

import requests
import feedparser
import anthropic
from bs4 import BeautifulSoup

# ===== 設定 =====
WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
CONTACT = os.environ.get("CONTACT_EMAIL") or "noreply@example.com"
MODEL = "claude-haiku-4-5-20251001"   # 安価・高速モデル
STATE_FILE = "seen.json"
MAX_PER_RUN = 8        # 1回の実行で処理する上限（API費用の暴走防止）
KEEP = 300             # フィードごとに覚えておく既読件数
MAX_AGE_HOURS = 48     # これより古い公表は送らない
SEND_LOW = False       # 重要度「低」も送るなら True
HEADERS = {"User-Agent": f"SUIKYO news-monitor {CONTACT}"}
JST = timezone(timedelta(hours=9))

# (表示名, RSS URL, 投稿に添えるライセンス表記。空欄＝表記不要)
FEEDS = [
    # --- 米国（連邦政府著作物：著作権なし）---
    ("FRB",        "https://www.federalreserve.gov/feeds/press_all.xml", ""),
    ("SEC",        "https://www.sec.gov/news/pressreleases.rss", ""),
    ("米労働統計局", "https://www.bls.gov/feed/bls_latest.rss", ""),
    ("米経済分析局", "https://apps.bea.gov/rss/rss.xml", ""),
    # --- 日本 ---
    ("日本銀行",    "https://www.boj.or.jp/rss/whatsnew.xml", ""),
    ("金融庁",      "https://www.fsa.go.jp/fsaNewsListAll_rss2.xml", "政府標準利用規約"),
    # --- 英国（Open Government Licence）---
    ("英財務省",    "https://www.gov.uk/government/organisations/hm-treasury.atom", "OGL v3.0"),
    # --- 豪州（CC BY 4.0）---
    ("豪準備銀行",  "https://www.rba.gov.au/rss/rss-cb-media-releases.xml", "CC BY 4.0"),
]

# 原文が日本語のソース（「非公式訳」ではなく「要約」と表記）
DOMESTIC = {"日本銀行", "金融庁"}

# タイトルにこれを含むものは送らない（Xで反応が取れない定例物）
EXCLUDE_KEYWORDS = [
    # 日本銀行の定例統計・定例公表
    "営業毎旬報告", "レポ統計", "資金循環", "時系列統計", "マネタリーベース",
    "預金・貸出", "貸出・預金", "企業物価指数", "サービス価格指数",
    "決済動向", "オペレーション", "国債買入", "共通担保", "補完当座預金",
    "日本銀行勘定", "統計の公表予定", "公表予定",
    # 金融庁の定例物
    "パブリックコメントの結果", "説明会", "意見交換会", "採用",
    # 海外（人事・イベント告知系）
    "Speech by", "speaks at", "Board meeting", "Minutes of the Board",
    "vacancy", "Job ", "Careers",
]

SYSTEM = """あなたは各国の金融当局・政府機関の公表文を日本語で正確に要約する編集者です。
規則:
- 公表文に書かれている事実のみを書く。推測・相場予想・投資判断・売買推奨は一切書かない
- 数値は原文の数字と単位をそのまま残し、括弧で原文表記を併記する
  例: 1,370億ドル（$137 billion）、57万5,000ドル（$575,000）
  換算の目安: 1 million=100万 / 1 billion=10億 / 1 trillion=1兆
- 原文に無い数値は書かない。日付・固有名詞も原文どおり正確に
- 原文をそのまま訳さず、要点を自分の構成でまとめる
出力は次のJSONのみ。前置き・コードブロックは禁止:
{"importance":"高|中|低","headline":"30字以内の日本語見出し","summary":"要点3〜5行。各行は「・」で始め改行で区切る","x_post":"X投稿用本文。70字以内。見出しと要点1点。URLは含めない"}
重要度: 高=政策金利・主要経済指標・大型規制や処分 / 中=通常の政策発表・報告書 / 低=人事・イベント告知・定型公表"""

ICON = {"高": "🔴", "中": "🟡", "低": "⚪"}


# ===== 補助関数 =====
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def entry_id(e):
    raw = e.get("id") or e.get("link") or e.get("title", "")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def entry_date(e):
    """公表日時（UTC）を返す。RSSに日付が無ければURL中の yymmdd から推定。不明なら None"""
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
    # 例: fso260821a.pdf / ac260820.htm → 2026-08-21 / 2026-08-20
    for m in re.finditer(r"(?<!\d)(2\d)(\d{2})(\d{2})(?!\d)", e.get("link", "")):
        try:
            d = datetime(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=JST)
            return d.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def is_excluded(e):
    title = e.get("title", "")
    return any(k.lower() in title.lower() for k in EXCLUDE_KEYWORDS)


def fetch_feed(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    if not feed.entries:
        raise ValueError("記事が0件（URL違いの可能性）")
    return feed.entries


def fetch_text(entry):
    """記事本文を取得。PDFや取得失敗時はRSSの概要で代用し、その旨を明記"""
    fallback = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True)
    note = "\n\n（注意：本文は取得できていません。上記の概要とタイトルのみを根拠にし、書かれていない内容を補わないこと）"
    link = entry.get("link", "")
    if not link or link.lower().endswith(".pdf"):
        return (fallback or "（概要なし）") + note
    try:
        r = requests.get(link, headers=HEADERS, timeout=20)
        r.raise_for_status()
        if "pdf" in r.headers.get("Content-Type", "").lower():
            return (fallback or "（概要なし）") + note
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)
        return text[:8000] if len(text) > 200 else (fallback or "（概要なし）") + note
    except Exception:
        return (fallback or "（概要なし）") + note


def summarize(client, source, entry, body):
    msg = client.messages.create(
        model=MODEL,
        max_tokens=800,
        system=SYSTEM,
        messages=[{
            "role": "user",
            "content": f"発表元: {source}\nタイトル: {entry.get('title', '')}\n\n本文:\n{body}",
        }],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    text = re.sub(r"```(json)?", "", text).strip()
    return json.loads(text)


def post_discord(content):
    r = requests.post(WEBHOOK, json={"content": content[:1900]}, timeout=20)
    r.raise_for_status()
    time.sleep(1)


def fmt_date(d):
    return d.astimezone(JST).strftime("%m/%d %H:%M") + " JST" if d else "日付不明・要確認"


def build_message(source, lic, entry, s, d):
    link = entry.get("link", "")
    label = "要約" if source in DOMESTIC else "非公式訳"
    credit = f"出典:{source}" + (f"（{lic}）" if lic else "")
    x_text = f"{s['x_post']}\n\n{credit}｜{label}\n{link}"
    return (
        f"{ICON.get(s.get('importance'), '⚪')} **{s['headline']}**\n"
        f"{source}｜公表 {fmt_date(d)}\n\n{s['summary']}\n\n"
        f"**X投稿案**（数字は原文と照合してから投稿）\n```\n{x_text}\n```"
    )


# ===== 本体 =====
def main():
    state = load_state()
    client = anthropic.Anthropic()
    now = datetime.now(timezone.utc)
    limit = now - timedelta(hours=MAX_AGE_HOURS)
    candidates, initialized, failed = [], [], []
    skipped_old = skipped_kw = skipped_low = 0

    for source, url, lic in FEEDS:
        try:
            entries = fetch_feed(url)
        except Exception as ex:
            print(f"[失敗] {source}: {ex}")
            failed.append(source)
            continue

        ids = [entry_id(e) for e in entries]
        if url not in state:
            # 初回：既存記事は既読扱いにして送らない
            state[url] = ids[:KEEP]
            initialized.append(source)
            continue

        seen = set(state[url])
        for e, eid in zip(reversed(entries), reversed(ids)):   # 古い順
            if eid in seen:
                continue
            d = entry_date(e)
            # 古い・除外対象は API を使わず既読にして捨てる
            if d and d < limit:
                skipped_old += 1
                state[url] = ([eid] + state[url])[:KEEP]
                continue
            if is_excluded(e):
                skipped_kw += 1
                state[url] = ([eid] + state[url])[:KEEP]
                continue
            candidates.append((url, source, lic, e, eid, d))

    if initialized:
        note = f"✅ 監視を開始しました：{'、'.join(initialized)}"
        if failed:
            note += f"\n⚠️ 取得できなかったフィード：{'、'.join(failed)}"
        post_discord(note)

    for url, source, lic, e, eid, d in candidates[:MAX_PER_RUN]:
        try:
            s = summarize(client, source, e, fetch_text(e))
            if s.get("importance") == "低" and not SEND_LOW:
                skipped_low += 1
                state[url] = ([eid] + state[url])[:KEEP]
                continue
            content = build_message(source, lic, e, s, d)
        except Exception as ex:
            print(f"[要約失敗] {source}: {ex}")
            content = (f"⚪ **{e.get('title', '(無題)')}**\n{source}｜公表 {fmt_date(d)}"
                       f"（要約失敗・原文を確認）\n{e.get('link', '')}")
        try:
            post_discord(content)
        except Exception as ex:
            print(f"[Discord送信失敗] {ex}")
            continue   # 未送信は既読にせず次回再送
        state[url] = ([eid] + state[url])[:KEEP]

    save_state(state)
    print(f"新着 {len(candidates)} 件 / 処理 {min(len(candidates), MAX_PER_RUN)} 件 / "
          f"除外: 古い {skipped_old}・定例 {skipped_kw}・重要度低 {skipped_low}")


if __name__ == "__main__":
    main()
