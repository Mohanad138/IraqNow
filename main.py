# -*- coding: utf-8 -*-
import os, re, json, time, asyncio, subprocess, difflib
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import (MessageEntityTextUrl, MessageMediaPhoto, MessageMediaDocument, MessageMediaWebPage)
import requests

TG_API_ID = int(os.getenv("TG_API_ID"))
TG_API_HASH = os.getenv("TG_API_HASH")
TG_SESSION = os.getenv("TG_SESSION")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

GROQ_MODEL = "openai/gpt-oss-120b"
STATE_FILE = "relay_state.json"
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

CHANNELS = {
    -1009001073232: {
        "name": "العراق الآن", "target": "@IQNowNews",
        "source_username": "@RN24_IQ",
        "add_link": True, "link": "https://t.me/IQNowNews",
        "prompt": """أنت محرر صحفي عراقي. أعد صياغة النص بأسلوب صحفي.
- انشر أي خبر سياسي أو اقتصادي أو عام (اجعل should_post=true).
- إذا كان إعلاناً تجارياً صريحاً (بيع، اشتراك، عروض، طلب مراسلة معرف) اجعل should_post=false.
- إذا كان النص طويلاً جداً، قم باختصاره بدون حذف المعلومات المهمة.
- لا تذكر اسم قناة المصدر أو روابطها نهائياً."""
    },
}

client = TelegramClient(StringSession(TG_SESSION), TG_API_ID, TG_API_HASH)

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except Exception: return {}
    return {}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f: json.dump(state, f, ensure_ascii=False, indent=2)

state = load_state()
DUP_HISTORY_SIZE = 30
DUP_SIMILARITY_THRESHOLD = 0.80   

def _norm_for_dup(text):
    t = text.lower()
    t = re.sub(r'https?://\S+', ' ', t)
    t = re.sub(r'[\u064B-\u065F\u0670\u0640]', '', t)
    t = t.replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا').replace('ى', 'ي').replace('ة', 'ه')
    t = re.sub(r'[^\w\u0600-\u06FF\s]', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()

def is_duplicate_content(source_id, text):
    if not text or not text.strip(): return False
    norm = _norm_for_dup(text)
    if not norm: return False
    recent = state.setdefault("recent_texts", {}).get(str(source_id), [])
    for prev in recent:
        prev_norm = _norm_for_dup(prev)
        if difflib.SequenceMatcher(None, norm, prev_norm).ratio() >= DUP_SIMILARITY_THRESHOLD: return True
    return False

def record_published_text(source_id, text):
    if not text or not text.strip(): return
    key = str(source_id)
    norm = _norm_for_dup(text)
    if not norm: return
    bucket = state.setdefault("recent_texts", {}).setdefault(key, [])
    bucket.append(norm)
    state["recent_texts"][key] = bucket[-DUP_HISTORY_SIZE:]

def commit_state():
    save_state(state)
    try:
        subprocess.run(["git", "add", STATE_FILE], check=False)
        c = subprocess.run(["git", "commit", "-m", "update state"], capture_output=True, text=True)
        if c.returncode != 0 and "nothing to commit" in (c.stdout + c.stderr).lower(): return
        for attempt in range(1, 4):
            subprocess.run(["git", "pull", "--rebase", "origin", "main"], capture_output=True, text=True)
            p = subprocess.run(["git", "push"], capture_output=True, text=True)
            if p.returncode == 0: return
            time.sleep(2 * attempt)
    except Exception as e: pass

def _is_small_thumbnail_photo(photo):
    try:
        dims = [(getattr(s, "w", 0), getattr(s, "h", 0)) for s in photo.sizes if hasattr(s, "w")]
        if not dims: return False
        max_w = max(w for w, _ in dims)
        max_h = max(h for _, h in dims)
        if max_w < 250 and max_h < 250: return True
        if max_w > 0 and max_h > 0:
            ratio = max_w / max_h
            if 0.85 <= ratio <= 1.18 and max(max_w, max_h) <= 800: return True
        return False
    except Exception: return False

def _is_usable_media_msg(m):
    media = m.media
    if isinstance(media, (MessageMediaPhoto, MessageMediaDocument)): return True
    if isinstance(media, MessageMediaWebPage):
        wp = getattr(media, "webpage", None)
        photo = getattr(wp, "photo", None)
        if photo and not _is_small_thumbnail_photo(photo): return True
    return False

def _media_payload(m):
    if isinstance(m.media, MessageMediaWebPage): return m.photo
    return m.media

def extract_hidden_link_urls(message, source_username=None):
    if not message or not getattr(message, "entities", None): return []
    handle = (source_username or "").lstrip("@").lower()
    skip_anchor_re = re.compile(r'read\s*more|اقرأ\s*المزيد', re.IGNORECASE)
    urls = []
    try:
        for entity, anchor_text in message.get_entities_text(MessageEntityTextUrl):
            url = entity.url
            if handle and f"t.me/{handle}".lower() in url.lower(): continue
            if skip_anchor_re.search(anchor_text or ""): continue
            urls.append(url)
    except Exception: pass
    return urls

def clean_text(text, source_username=None):
    if not text: return ""
    cleaned = text.replace('📊', '')
    if source_username: cleaned = re.sub(re.escape(source_username), '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'(https?://)?t\.me/\S+', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'https?://t\.co/\S+', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'(?i)forwarded from.*', '', cleaned)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return cleaned

SHARE_PHRASE_RE = re.compile(r'شارك\s+(القناة|المنشور)\s+مع\s+[اأ]?صدقائ?ك?', re.IGNORECASE)
def insert_link_after_share_phrase(text, link_text):
    match = SHARE_PHRASE_RE.search(text)
    if not match: return None
    newline_idx = text.find('\n', match.end())
    if newline_idx == -1: return text.rstrip() + f"\n{link_text}"
    return text[:newline_idx] + f"\n{link_text}" + text[newline_idx:]

def contains_click_here_phrase(text): return bool(re.search(r'اضغط\s*هنا.{0,15}(للمشاهدة|لمشاهدة|مشاهدة)', text)) if text else False

def is_hardcoded_ad(text):
    if not text: return False
    ad_keywords = [
        r"راسل\s+المعرف", r"للحجز", r"للطلب", 
        r"سعر", r"دينار", r"خصم", r"اشتراك"
    ]
    for kw in ad_keywords:
        if re.search(kw, text, re.IGNORECASE):
            return True
    return False

def call_groq(prompt, max_retries=3):
    if not GROQ_API_KEY: raise RuntimeError("no groq key")
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    body = {"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3}
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=30)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                time.sleep(2 ** attempt); continue
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            if attempt == max_retries: raise e
            time.sleep(2 ** attempt)

async def apply_processing(config, text):
    if not text: return {"should_post": True, "text": ""}
    custom_prompt = config.get("prompt", "")
    if not custom_prompt: return {"should_post": True, "text": text}
    
    full_prompt = f"""{custom_prompt}

النص المراد معالجته:
{text}

أعد النتيجة بصيغة JSON فقط: {{"should_post": true/false, "text": "النص المعالج"}}"""
    try:
        res_raw = call_groq(full_prompt)
        if res_raw.startswith("```"):
            res_raw = res_raw.strip("`").strip()
            if res_raw.lower().startswith("json"): res_raw = res_raw[4:].strip()
        res = json.loads(res_raw)
        return {"should_post": bool(res.get("should_post", True)), "text": (res.get("text") or text).strip()}
    except Exception as e:
        print(f"❌ خطأ المعالجة: {e}")
        return {"should_post": False, "text": ""}

async def process_batch(source_id, messages):
    config = CHANNELS.get(source_id)
    if not config: return
    
    raw_text = next((m.message for m in messages if m.message), "")
    text_msg = next((m for m in messages if m.message), None)
    
    if contains_click_here_phrase(raw_text): return
    
    if is_hardcoded_ad(raw_text):
        print(f"⏭️ تجاهل إعلان تجاري (فلتر برمجي) — {config['name']}")
        return
    
    hidden_links = extract_hidden_link_urls(text_msg, config.get("source_username"))
    if hidden_links: 
        raw_text = (raw_text.rstrip() + "\n\n" + "\n".join(hidden_links)) if raw_text else "\n".join(hidden_links)
    
    cleaned = clean_text(raw_text, config.get("source_username"))
    
    if is_duplicate_content(source_id, cleaned): return
    
    res = await apply_processing(config, cleaned)
    should_post = res.get("should_post", True)
    final_text = res.get("text", cleaned)
    
    if not should_post and cleaned: 
        print(f"⏭️ تجاهل إعلان (ذكاء اصطناعي) — {config['name']}")
        return 
    
    if cleaned: record_published_text(source_id, cleaned)
    if final_text and is_duplicate_content(f"{source_id}#final", final_text): return
    if final_text: record_published_text(f"{source_id}#final", final_text)
    
    media_msgs = [m for m in messages if _is_usable_media_msg(m)]

    if config.get("add_link") and config.get("link"):
        link_text = config["link"]
        inserted = None
        if config.get("link_after_share_phrase") and final_text:
            inserted = insert_link_after_share_phrase(final_text, link_text)
        if inserted is not None: final_text = inserted
        else: final_text = f"{final_text}\n\n{link_text}" if final_text else link_text

    try:
        if media_msgs:
            try:
                to_send = _media_payload(media_msgs[0]) if len(media_msgs) == 1 else [_media_payload(m) for m in media_msgs]
                await client.send_file(config["target"], to_send, caption=final_text or None, parse_mode="html")
                print(f"✅ نُشر (وسائط) — {config['name']}")
                return
            except Exception as e:
                if "protected chat" in str(e):
                    files = []
                    for m in media_msgs:
                        try:
                            path = await client.download_media(m, file=f"{DOWNLOAD_DIR}/")
                            if path: files.append(path)
                        except Exception: pass
                    if files:
                        to_send2 = files[0] if len(files) == 1 else files
                        await client.send_file(config["target"], to_send2, caption=final_text or None, parse_mode="html")
                        for f in files:
                            try: os.remove(f)
                            except OSError: pass
                        print(f"✅ نُشر (وسائط محمية) — {config['name']}")
                        return
                else: raise e
                
        if final_text:
            await client.send_message(config["target"], final_text, parse_mode="html", link_preview=False)
            print(f"✅ نُشر (نص) — {config['name']}")
    except Exception as e: print(f"❌ خطأ بالنشر — {config['name']}: {e}")

async def catch_up_channel(source_id):
    config = CHANNELS.get(source_id, {})
    entity_ref = config.get("source_username") or source_id
    key = str(source_id)
    last_id = state.get(key, 0)
    
    if last_id == 0:
        latest = await client.get_messages(entity_ref, limit=1)
        if latest: last_id = max(latest[0].id - 5, 0)
        state[key] = last_id
        
    messages = await client.get_messages(entity_ref, min_id=last_id, limit=200)
    if not messages: return
    messages = list(reversed(messages))
    processed_ids = set()
    
    for msg in messages:
        if msg.id in processed_ids: continue
        if msg.grouped_id:
            group = [m for m in messages if m.grouped_id == msg.grouped_id and m.id not in processed_ids]
            group.sort(key=lambda m: m.id)
            await process_batch(source_id, group)
            processed_ids.update(m.id for m in group)
            new_last = group[-1].id
        else:
            await process_batch(source_id, [msg])
            processed_ids.add(msg.id)
            new_last = msg.id
        state[key] = new_last

async def main():
    await client.start()
    await catch_up_channel(-1009001073232)
    await client.disconnect()
    commit_state()

if __name__ == "__main__":
    asyncio.run(main())
