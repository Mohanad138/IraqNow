# -*- coding: utf-8 -*-
import os, re, json, time, asyncio, subprocess, difflib
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import (MessageEntityTextUrl, MessageMediaPhoto, MessageMediaDocument, MessageMediaWebPage)
import requests

# قراءة المتغيرات من البيئة (Secrets)
TG_API_ID = int(os.getenv("TG_API_ID"))
TG_API_HASH = os.getenv("TG_API_HASH")
TG_SESSION = os.getenv("TG_SESSION")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# متغيرات فيسبوك
FB_TOKEN_IRAQ = os.getenv("FB_TOKEN_IRAQ")
PAGE_ID_IRAQ = os.getenv("PAGE_ID_IRAQ")

# موديل Groq الصحيح
GROQ_MODEL = "openai/gpt-oss-120b" 
GRAPH_API_VERSION = "v21.0"
STATE_FILE = "relay_state.json"
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

TEST_MODE = False 

CHANNELS = {
    -1009001073232: {
        "name": "العراق الآن", 
        "target_channel": "@IQNowNews", 
        "source_username": "@IRAQ2TV",
        "fb_page_id": PAGE_ID_IRAQ,
        "fb_token": FB_TOKEN_IRAQ,
        "tg_link": "https://t.me/IQNowNews",
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

async def call_groq(prompt, max_retries=3):
    if not GROQ_API_KEY: raise RuntimeError("no groq key")
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    body = {"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3}
    
    def _sync_request():
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
                
    return await asyncio.to_thread(_sync_request)

async def apply_processing(config, text):
    if not text: return {"should_post": True, "text": ""}
    custom_prompt = config.get("prompt", "")
    if not custom_prompt: return {"should_post": True, "text": text}
    
    full_prompt = f"""{custom_prompt}

النص المراد معالجته:
{text}

أعد النتيجة بصيغة JSON فقط: {{"should_post": true/false, "text": "النص المعالج"}}"""
    try:
        res_raw = await call_groq(full_prompt)
        
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

def _sync_post_to_facebook(page_id, token, text, media_path=None, is_video=False, tg_link=""):
    # 🛑 التحقق من وجود البيانات وطباعة الخطأ إذا كانت مفقودة
    if not page_id or not token:
        print("⚠️ تخطي فيسبوك: الـ PAGE_ID أو الـ FB_TOKEN غير موجود في إعدادات GitHub Secrets!")
        return
        
    try:
        print(f"⏳ جاري محاولة النشر على فيسبوك (الصفحة: {page_id})...")
        base_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{page_id}"
        post_id = None
        
        if media_path:
            if is_video:
                with open(media_path, 'rb') as f:
                    res = requests.post(f"{base_url}/videos", data={"access_token": token, "description": text}, files={"source": f}, timeout=300)
            else:
                with open(media_path, 'rb') as f:
                    res = requests.post(f"{base_url}/photos", data={"access_token": token, "message": text}, files={"source": f}, timeout=60)
        else:
            res = requests.post(f"{base_url}/feed", data={"access_token": token, "message": text}, timeout=20)
        
        res_data = res.json()
        
        # 🛑 طباعة خطأ فيسبوك إذا رفض النشر (توكن منتهي، صلاحيات خاطئة، إلخ)
        if "error" in res_data:
            print(f"❌ خطأ فيسبوك: {res_data['error']['message']} (الكود: {res_data['error'].get('code')})")
            return
            
        post_id = res_data.get("post_id") or res_data.get("id")
        print(f"✅ نُشر على فيسبوك بنجاح! (ID: {post_id})")
        
        if post_id and tg_link:
            comment_text = f"تابعنا على التلكرام 👇\n{tg_link}"
            comment_res = requests.post(f"https://graph.facebook.com/{GRAPH_API_VERSION}/{post_id}/comments", data={"access_token": token, "message": comment_text}, timeout=20)
            if "error" not in comment_res.json():
                print(f"✅ تمت إضافة تعليق التلكرام على فيسبوك.")
            else:
                print(f"⚠️ فشل إضافة التعليق: {comment_res.json()['error']['message']}")
                
    except Exception as e:
        print(f"❌ فشل النشر على فيسبوك (استثناء): {e}")

async def post_to_facebook(page_id, token, text, media_path=None, is_video=False, tg_link=""):
    await asyncio.to_thread(_sync_post_to_facebook, page_id, token, text, media_path, is_video, tg_link)

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
    media_path = None
    is_video = False
    
    if media_msgs:
        first_m = media_msgs[0]
        if isinstance(first_m.media, MessageMediaDocument) and first_m.video:
            is_video = True
        try: 
            media_path = await client.download_media(first_m, file=DOWNLOAD_DIR)
        except Exception as e: 
            print(f"⚠️ فشل تحميل الميديا: {e}")

    if not final_text.strip() and not media_path: return

    target_channel = config.get("target_channel")
    fb_page_id = config.get("fb_page_id")
    fb_token = config.get("fb_token")
    tg_link = config.get("tg_link")

    async def _post_tg():
        if target_channel:
            try:
                if media_path:
                    if len(final_text) > 1024:
                        await client.send_file(target_channel, media_path, supports_streaming=is_video, force_document=False)
                        await client.send_message(target_channel, final_text)
                    else:
                        await client.send_file(target_channel, media_path, caption=final_text, supports_streaming=is_video, force_document=False)
                else:
                    await client.send_message(target_channel, final_text)
                print(f"✅ نُشر بنجاح إلى تلكرام ({target_channel})")
            except Exception as e:
                print(f"❌ فشل النشر إلى تلكرام: {e}")

    async def _post_fb():
        if fb_page_id and fb_token:
            await post_to_facebook(fb_page_id, fb_token, final_text, media_path, is_video, tg_link)

    # تشغيل النشر بالتوازي
    if target_channel or (fb_page_id and fb_token):
        await asyncio.gather(_post_tg(), _post_fb())

    if media_path and os.path.exists(media_path):
        os.remove(media_path)

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
        
        messages = []
        async for msg in client.iter_messages(entity_ref, min_id=last_id, reverse=True):
            messages.append(msg)
            
    if not messages: return
    
    groups = []
    processed_ids = set()
    
    for msg in messages:
        if msg.id in processed_ids: continue
        if msg.grouped_id:
            group = [m for m in messages if m.grouped_id == msg.grouped_id and m.id not in processed_ids]
            group.sort(key=lambda m: m.id)
            groups.append(group)
            processed_ids.update(m.id for m in group)
        else:
            groups.append([msg])
            processed_ids.add(msg.id)

    sem = asyncio.Semaphore(5)
    
    async def process_group_with_sem(group):
        async with sem:
            await process_batch(source_id, group)
    
    await asyncio.gather(*(process_group_with_sem(g) for g in groups))
    
    if groups:
        max_processed_id = max(g[-1].id for g in groups)
        state[str(source_id)] = max(state.get(str(source_id), 0), max_processed_id)
        save_state(state)

async def main():
    print("🚀 بدء التشغيل...")
    
    # 🔍 فحص المتغيرات وطباعتها للتأكد من وصولها
    if not FB_TOKEN_IRAQ or not PAGE_ID_IRAQ:
        print("⚠️ تحذير هام: لم يتم العثور على FB_TOKEN_IRAQ أو PAGE_ID_IRAQ في إعدادات GitHub Secrets!")
    else:
        print(f"🔑 تم العثور على بيانات فيسبوك (الصفحة: {PAGE_ID_IRAQ})")

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
