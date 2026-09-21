"""
SUIKYO 当局一次情報モニター
RSSを監視 → 新着だけを日本語要約 → Discord に通知（X投稿案つき）
"""
import os
import re
import json
import time
import hashlib

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
HEADERS = {"User-Agent": f"SUIKYO news-monitor {CONTACT}"}

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

SYSTEM = """あなたは各国の金融当局・政府機関の公表文を日本語で正確に要約する編集者です。
規則:
- 公表文に書かれている事実のみを書く。推測・相場予想・投資判断・売買推奨は一切書かない
- 数値・日付・固有名詞は原文どおり正確に
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


def fetch_feed(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    if not feed.entries:
        raise ValueError("記事が0件（URL違いの可能性）")
    return feed.entries


def fetch_text(entry):
    """記事本文を取得。失敗したらRSSの概要で代用"""
    fallback = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True)
    link = entry.get("link")
    if not link:
        return fallback
    try:
        r = requests.get(link, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)
        return text[:8000] if len(text) > 200 else fallback
    except Exception:
        return fallback


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


def build_message(source, lic, entry, s):
    link = entry.get("link", "")
    credit = f"出典:{source}" + (f"（{lic}）" if lic else "")
    x_text = f"{s['x_post']}\n\n{credit}｜非公式訳\n{link}"
    return (
        f"{ICON.get(s.get('importance'), '⚪')} **{s['headline']}**\n"
        f"{source}\n\n{s['summary']}\n\n"
        f"**X投稿案**\n```\n{x_text}\n```"
    )


# ===== 本体 =====
def main():
    state = load_state()
    client = anthropic.Anthropic()
    candidates, initialized, failed = [], [], []

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
            if eid not in seen:
                candidates.append((url, source, lic, e, eid))

    if initialized:
        note = f"✅ 監視を開始しました：{'、'.join(initialized)}"
        if failed:
            note += f"\n⚠️ 取得できなかったフィード：{'、'.join(failed)}"
        post_discord(note)

    for url, source, lic, e, eid in candidates[:MAX_PER_RUN]:
        try:
            s = summarize(client, source, e, fetch_text(e))
            content = build_message(source, lic, e, s)
        except Exception as ex:
            print(f"[要約失敗] {source}: {ex}")
            content = f"⚪ **{e.get('title', '(無題)')}**\n{source}（要約失敗・原文を確認）\n{e.get('link', '')}"
        try:
            post_discord(content)
        except Exception as ex:
            print(f"[Discord送信失敗] {ex}")
            continue   # 未送信は既読にせず次回再送
        state[url] = ([eid] + state[url])[:KEEP]

    save_state(state)
    print(f"新着 {len(candidates)} 件 / 処理 {min(len(candidates), MAX_PER_RUN)} 件")


if __name__ == "__main__":
    main()
