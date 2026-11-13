# -*- coding: utf-8 -*-
import os, re, json, time, asyncio, subprocess, difflib
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import (MessageEntityTextUrl, MessageMediaPhoto, MessageMediaDocument, MessageMediaWebPage)
import requests

# قراءة المتغيرات من البيئة
TG_API_ID = int(os.getenv("TG_API_ID"))
TG_API_HASH = os.getenv("TG_API_HASH")
TG_SESSION = os.getenv("TG_SESSION")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# موديل Groq الصحيح
GROQ_MODEL = "llama-3.3-70b-versatile" 
STATE_FILE = "relay_state.json"

# اجعلها True لتجربة آخر 5 منشورات، ثم أرجعها False
TEST_MODE = False 

CHANNELS = {
    -1009001073232: {
        "name": "العراق الآن", 
        # 👇 تم إضافة رابط قناتك هنا
        "target_channel": "@IQNowNews", 
        "source_username": "@IRAQ2TV",
        "prompt": """أنت محرر صحفي عراقي. أعد صياغة النص التالي ليكون مشوقاً وجذاباً للقراءة.
- **تحذير صارم جداً:** ممنوع منعاً باتاً إضافة أي معلومة، رقم، اسم، أو تفاصيل غير موجودة في النص الأصلي. مهمتك فقط إعادة ترتيب الكلمات الموجودة وجعلها مشوقة.
- حافظ على الأرقام والأسماء كما هي تماماً.
- **ممنوع ذكر اسم "واحد العراق" أو أي اسم قناة أخرى.**
- انشر أي خبر سياسي أو اقتصادي أو عام (اجعل should_post=true).
- اجعل should_post=false فقط إذا كان إعلاناً تجارياً صريحاً.
- لا تذكر اسم قناة المصدر أو روابطها نهائياً."""
    }
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
    
    # حذف اسم "واحد العراق" برمجياً
    cleaned = re.sub(r'واحد\s*العراق', '', cleaned, flags=re.IGNORECASE)
    
    cleaned = re.sub(r'(https?://)?t\.me/\S+', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'https?://t\.co/\S+', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'(?i)forwarded from.*', '', cleaned)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return cleaned

def contains_click_here_phrase(text): return bool(re.search(r'اضغط\s*هنا.{0,15}(للمشاهدة|لمشاهدة|مشاهدة)', text)) if text else False

def is_hardcoded_ad(text):
    if not text: return False
    ad_keywords = [r"راسل\s+المعرف", r"@IQED3", r"للحجز", r"للطلب", r"للتواصل", r"سعر", r"دينار", r"خصم", r"اشتراك", r"ملخصات\s+ذهبية"]
    for kw in ad_keywords:
        if re.search(kw, text, re.IGNORECASE): return True
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
        
        # استخراج JSON بشكل آمن
        match = re.search(r'\{.*\}', res_raw, re.DOTALL)
        if match:
            res = json.loads(match.group(0))
        else:
            print("⚠️ لم يتم العثور على JSON صالح في رد الذكاء الاصطناعي.")
            return {"should_post": False, "text": ""}
            
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
    media_bytes = None
    is_video = False
    
    # تحميل الميديا في الذاكرة
    if media_msgs:
        first_m = media_msgs[0]
        if isinstance(first_m.media, MessageMediaDocument) and first_m.video:
            is_video = True
        try: 
            media_bytes = await client.download_media(first_m, file=bytes)
        except Exception as e: 
            print(f"⚠️ فشل تحميل الميديا: {e}")

    if not final_text.strip() and not media_bytes: return

    # 🚀 النشر إلى قناة تلكرام المستهدفة
    target_channel = config.get("target_channel")
    if target_channel:
        try:
            if media_bytes:
                # التحقق من طول النص (تيليجرام يسمح بـ 1024 حرف كحد أقصى للتعليق على الميديا)
                if len(final_text) > 1024:
                    await client.send_file(target_channel, media_bytes, supports_streaming=is_video)
                    await client.send_message(target_channel, final_text)
                else:
                    await client.send_file(target_channel, media_bytes, caption=final_text, supports_streaming=is_video)
            else:
                # إرسال النص فقط
                await client.send_message(target_channel, final_text)
            print(f"✅ نُشر بنجاح إلى تلكرام ({target_channel})")
        except Exception as e:
            print(f"❌ فشل النشر إلى تلكرام: {e}")

async def catch_up_channel(source_id):
    config = CHANNELS.get(source_id, {})
    entity_ref = config.get("source_username") or source_id
    
    if TEST_MODE:
        messages = await client.get_messages(entity_ref, limit=5)
    else:
        last_id = state.get(str(source_id), 0)
        if last_id == 0:
            latest = await client.get_messages(entity_ref, limit=1)
            if latest: last_id = latest[0].id - 5
            state[str(source_id)] = last_id
        
        # استخدام iter_messages لضمان عدم ضياع أي رسالة مهما كان عددها
        messages = []
        async for msg in client.iter_messages(entity_ref, min_id=last_id, reverse=True):
            messages.append(msg)
            
    if not messages: return
    
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
        
        # حفظ الحالة محلياً لضمان عدم التكرار
        state[str(source_id)] = new_last
        save_state(state)

async def main():
    print("🚀 بدء التشغيل...")
    await client.start()
    tasks = []
    for source_id in CHANNELS:
        tasks.append(catch_up_channel(source_id))
    await asyncio.gather(*tasks)
    await client.disconnect()
    commit_state()
    print("✅ انتهت التشغيلة.")

if __name__ == "__main__":
    asyncio.run(main())
