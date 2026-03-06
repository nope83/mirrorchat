"""
MirrorChat — Flask Backend
API key management, promo codes, training, chat.
The platform API key NEVER goes to the browser.
"""

import os
import re
import json
import time
import hashlib
import random
import unicodedata
from pathlib import Path
from collections import Counter
from datetime import datetime
from threading import Thread

from flask import Flask, request, jsonify, send_from_directory, session
from openai import OpenAI

# ── Load .env if present ──
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.getenv("SECRET_KEY", "mirrorchat-dev-key-change-me")

# ── Config ──
PLATFORM_API_KEY = os.getenv("OPENAI_API_KEY", "")
PROMO_CODES = set(
    c.strip().upper()
    for c in os.getenv("PROMO_CODES", "").split(",")
    if c.strip()
)
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

# In-memory session store (use Redis in production)
sessions = {}

# ═══════════════════════════════════════════════════════════════════════
# SKIP / TOPICS / HINDI WORDS
# ═══════════════════════════════════════════════════════════════════════
SKIP_PATTERNS = [
    "Voice call", "Missed voice call", "video omitted", "image omitted",
    "document omitted", "You deleted this message", "end-to-end encrypted",
    "sticker omitted", "audio omitted", "GIF omitted", "Contact card",
    "This message was deleted", "Waiting for this message", "location omitted",
    "<Media omitted>",
]

TOPICS = {
    "job": {"job","apply","resume","interview","referral","hiring","company","role","salary","offer","linkedin","work","career","coding","python","sql","meeting","project"},
    "food": {"order","food","eat","lunch","dinner","breakfast","restaurant","pizza","biryani","cooking","recipe","grocery","delivery","khana","kha","chai","coffee","zomato","swiggy"},
    "logistics": {"ghar","home","room","aana","aati","nikal","nikla","chal","chalo","drive","uber","bus","airport","flight","charger","phone","drop","pick","ready"},
    "casual": {"kya","kaisa","kaisi","thk","theek","okay","fine","good","accha","how","doing","going","raha","rahi","hi","hello","hey","morning","night","sleep","bored","free","busy"},
    "emotional": {"sorry","please","miss","love","care","feel","feeling","sad","happy","angry","gussa","upset","worried","tension","stress","cry","hope","trust","promise"},
    "planning": {"plan","tomorrow","kal","weekend","trip","movie","party","hangout","milte","chale","time","when","date"},
}

HINDI_WORDS = set("kya kaise hai haan nahi nhi toh bhi aur ya par se ka ki ke ko me mai ye wo tera mera kuch koi sab bahut bohot zyada thoda bas accha theek chalo hum tum kar karo raha rahi gaya gayi ja jao de do bol bolo sun suno dekh dekho pata pta abhi ab jab kab yaha waha aana jaana khana ruk aa aaja jaldi pehle baad phir bilkul sachme pakka pagal yaar bhai bro ghar kal aaj subah raat kitna kaun kahan kyun lekin agar aisa itna".split())


# ═══════════════════════════════════════════════════════════════════════
# PARSER
# ═══════════════════════════════════════════════════════════════════════
def parse_ts(s):
    for fmt in ["%m/%d/%y, %I:%M:%S %p", "%m/%d/%Y, %I:%M:%S %p",
                "%d/%m/%y, %I:%M:%S %p", "%d/%m/%y, %H:%M"]:
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def parse_chat(text):
    patterns = [
        r'\[(\d+/\d+/\d+,\s+\d+:\d+:\d+\s+[AP]M)\]\s+([^:]+):\s+(.*)',
        r'(\d+/\d+/\d+,\s+\d+:\d+)\s+-\s+([^:]+):\s+(.*)',
    ]
    messages = []
    for line in text.split("\n"):
        for pat in patterns:
            m = re.match(pat, line.strip())
            if m:
                t = m.group(3).strip()
                if any(s in t for s in SKIP_PATTERNS) or not t:
                    break
                messages.append({
                    "sender": m.group(2).strip(),
                    "text": t,
                    "ts": parse_ts(m.group(1)),
                })
                break
    return messages


def read_file_safe(file_bytes):
    for enc in ["utf-8", "utf-8-sig", "latin-1", "cp1252"]:
        try:
            return file_bytes.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return file_bytes.decode("utf-8", errors="ignore")


# ═══════════════════════════════════════════════════════════════════════
# TEXT DNA ENGINE
# ═══════════════════════════════════════════════════════════════════════
def extract_emojis(text):
    result = []
    for ch in text:
        cp = ord(ch)
        if (0x1F600 <= cp <= 0x1F64F or 0x1F300 <= cp <= 0x1F5FF or
            0x1F680 <= cp <= 0x1F6FF or 0x2600 <= cp <= 0x26FF or
            0x2700 <= cp <= 0x27BF or 0x1F900 <= cp <= 0x1F9FF or
            0x1FA00 <= cp <= 0x1FAFF or cp == 0x2764 or cp == 0xFE0F):
            result.append(ch)
    return result


def detect_lang(text):
    words = set(re.findall(r'[a-zA-Z]+', text.lower()))
    if not words:
        return "other"
    hi = sum(1 for w in words if w in HINDI_WORDS)
    r = hi / len(words)
    if r > 0.6: return "hindi"
    if r > 0.15: return "hinglish"
    return "english"


def dominant_topic(text):
    words = set(re.findall(r'[a-zA-Z]+', text.lower()))
    best, bn = "general", 0
    for cat, kws in TOPICS.items():
        n = len(words & kws)
        if n > bn:
            best, bn = cat, n
    return best if bn >= 2 else "general"


def analyze_person(messages, name):
    texts = [m["text"] for m in messages if m["sender"] == name]
    if not texts:
        return None

    dna = {"name": name, "total": len(texts)}

    # Message length
    lens = [len(t) for t in texts]
    wc = [len(t.split()) for t in texts]
    dna["msg_length"] = {
        "avg_chars": round(sum(lens)/len(lens), 1),
        "avg_words": round(sum(wc)/len(wc), 1),
        "short_pct": round(100 * sum(1 for l in lens if l < 15)/len(lens), 1),
        "med_pct": round(100 * sum(1 for l in lens if 15 <= l < 50)/len(lens), 1),
        "long_pct": round(100 * sum(1 for l in lens if l >= 50)/len(lens), 1),
    }

    # Rapid fire
    consec = []
    streak = 0
    for m in messages:
        if m["sender"] == name:
            streak += 1
        else:
            if streak: consec.append(streak)
            streak = 0
    if streak: consec.append(streak)
    dna["rapid_fire"] = {
        "avg_burst": round(sum(consec)/len(consec), 1) if consec else 1,
        "triple_plus_pct": round(100 * sum(1 for c in consec if c >= 3)/len(consec), 1) if consec else 0,
    }

    # Emoji
    all_emojis = []
    emoji_msgs = 0
    for t in texts:
        em = extract_emojis(t)
        if em: emoji_msgs += 1
        all_emojis.extend(em)
    ec = Counter(all_emojis)
    dna["emoji"] = {
        "usage_pct": round(100 * emoji_msgs/len(texts), 1),
        "avg_per_msg": round(len(all_emojis)/len(texts), 2),
        "top": [{"e": e, "c": c} for e, c in ec.most_common(10)],
    }

    # Language
    lc = Counter(detect_lang(t) for t in texts)
    total = sum(lc.values())
    dna["language"] = {
        "english_pct": round(100 * lc.get("english", 0)/total, 1),
        "hinglish_pct": round(100 * lc.get("hinglish", 0)/total, 1),
        "hindi_pct": round(100 * lc.get("hindi", 0)/total, 1),
    }

    # Quirks
    all_lower = " ".join(texts).lower()
    wf = Counter(re.findall(r'[a-zA-Z]+', all_lower))
    quirk_cats = {
        "laugh": ["haha","hehe","lol","lmao","hahaha","loll","xd"],
        "agree": ["haan","han","yes","yeah","yep","yup","hmm","ok","okay","okk","okkk","k","kk","accha","theek","sahi"],
        "greet": ["hi","hey","hello","yo","sup","hii","hiii","heyyy"],
    }
    dna["quirks"] = {}
    for cat, words in quirk_cats.items():
        top = sorted([(w, wf.get(w, 0)) for w in words], key=lambda x: -x[1])
        dna["quirks"][cat] = [(w, c) for w, c in top[:4] if c > 0]

    # Punctuation
    dna["punctuation"] = {
        "all_lower_pct": round(100 * sum(1 for t in texts if t == t.lower())/len(texts), 1),
        "exclaim_pct": round(100 * sum(1 for t in texts if t.rstrip().endswith('!'))/len(texts), 1),
        "ellipsis_pct": round(100 * sum(1 for t in texts if '...' in t)/len(texts), 1),
    }

    # Elongation (nooo, yesss)
    elong = re.findall(r'\b(\w*?(.)\2{2,}\w*)\b', all_lower)
    elong_words = Counter(w[0] for w in elong)
    dna["elongation"] = [{"w": w, "c": c} for w, c in elong_words.most_common(8)]

    # Common full messages
    fm = Counter(t.strip().lower() for t in texts if len(t.strip()) < 40)
    dna["common_msgs"] = [{"m": m, "c": c} for m, c in fm.most_common(15) if c >= 2]

    # ── REACTION PATTERNS — how they respond to different message types ──
    reactions = {
        "to_greeting": [],     # when someone says hi/hey
        "to_question": [],     # when asked something with ?
        "to_emotion": [],      # when someone shares feelings
        "to_news": [],         # when someone shares news/info (long msgs)
        "to_short": [],        # when someone sends a short msg
        "to_funny": [],        # when someone says something funny
    }
    for i in range(len(messages) - 1):
        if messages[i]["sender"] != name and messages[i+1]["sender"] == name:
            prev = messages[i]["text"].lower()
            reply = messages[i+1]["text"]
            if any(g in prev for g in ["hi","hey","hello","hii","hiii","sup","yo","morning","evening"]):
                reactions["to_greeting"].append(reply)
            if "?" in prev:
                reactions["to_question"].append(reply)
            if any(e in prev for e in ["miss","love","sad","happy","sorry","feel","feeling","worried","angry","upset","care"]):
                reactions["to_emotion"].append(reply)
            if len(prev) > 60:
                reactions["to_news"].append(reply)
            if len(prev) < 10:
                reactions["to_short"].append(reply)
            if any(f in prev for f in ["haha","lol","lmao","😂","🤣","funny","joke"]):
                reactions["to_funny"].append(reply)

    dna["reactions"] = {}
    for key, replies in reactions.items():
        if len(replies) >= 3:
            samples = random.sample(replies, min(8, len(replies)))
            dna["reactions"][key] = {
                "count": len(replies),
                "avg_len": round(sum(len(r) for r in replies)/len(replies), 1),
                "emoji_rate": round(100*sum(1 for r in replies if extract_emojis(r))/len(replies), 1),
                "samples": samples,
            }

    return dna


def generate_system_prompt(dna):
    name = dna["name"]
    ml = dna["msg_length"]
    rf = dna["rapid_fire"]
    emo = dna["emoji"]
    lang = dna["language"]
    quirks = dna["quirks"]
    punc = dna["punctuation"]
    reactions = dna.get("reactions", {})

    top_emojis = " ".join(e["e"] for e in emo["top"][:8]) if emo["top"] else "rarely"
    laugh = ", ".join(f'"{w}"' for w, c in quirks.get("laugh", [])[:3]) or "rarely laughs"
    agree = ", ".join(f'"{w}"' for w, c in quirks.get("agree", [])[:3]) or "varies"
    greet = ", ".join(f'"{w}"' for w, c in quirks.get("greet", [])[:3]) or "casual"
    common = ", ".join(f'"{p["m"]}"' for p in dna.get("common_msgs", [])[:8])
    elong = ", ".join(f'"{e["w"]}"' for e in dna.get("elongation", [])[:5]) or "none"

    if lang["hinglish_pct"] > 40:
        lang_d = f"Hinglish mix (~{lang['hinglish_pct']}%)"
    elif lang["english_pct"] > 60:
        lang_d = f"mostly English ({lang['english_pct']}%)"
    else:
        lang_d = f"mixed: EN {lang['english_pct']}% Hinglish {lang['hinglish_pct']}% HI {lang['hindi_pct']}%"

    fire_d = f"sends {rf['avg_burst']:.0f} msgs in a row" if rf["avg_burst"] > 1.5 else "usually single messages"

    if emo["usage_pct"] > 40:
        emo_d = f"Heavy emoji ({emo['usage_pct']}%). Favorites: {top_emojis}"
    elif emo["usage_pct"] > 15:
        emo_d = f"Moderate emoji ({emo['usage_pct']}%). Top: {top_emojis}"
    else:
        emo_d = f"Rarely uses emojis ({emo['usage_pct']}%)"

    caps_d = "types everything lowercase" if punc["all_lower_pct"] > 70 else "normal caps"

    # Build reaction examples
    reaction_text = ""
    if reactions:
        reaction_text = "\n=== HOW YOU REACT TO DIFFERENT MESSAGES ===\n"
        for key, data in reactions.items():
            label = key.replace("to_", "When someone sends ").replace("_", " ")
            samples = data.get("samples", [])[:4]
            if samples:
                sample_str = " | ".join(f'"{s}"' for s in samples)
                reaction_text += f"- {label}: you typically reply like: {sample_str}\n"

    return f"""You are {name} texting on WhatsApp. You are a REAL person, not a bot.

=== HOW TO THINK ===
Before replying, understand WHAT the other person is saying:
1. Is it a greeting? Reply with YOUR greeting style, not a random topic.
2. Is it a question? Answer it naturally in your way.
3. Is it sharing news/info? React to THAT specific news.
4. Is it emotional? Respond to the emotion appropriately.
5. Is it casual chat? Keep it casual.
6. Is it about plans? Respond about the plans.
ALWAYS reply to what was ACTUALLY said. Read the message carefully. Stay on topic.
If they send multiple messages, read ALL of them and respond to the combined context.

=== YOUR TEXTING PERSONALITY ===
- Language: {lang_d}. {caps_d}.
- Message length: ~{ml['avg_chars']} chars, {ml['avg_words']} words. {ml['short_pct']}% of your msgs are short (<15 chars).
- {fire_d}.
- {emo_d}
- Laughing style: {laugh}
- Agreement style: {agree}
- Greeting style: {greet}
- Word stretching: {elong}
- Common replies: {common}
- Punctuation: {punc['exclaim_pct']}% ends with !, {punc['ellipsis_pct']}% uses ...
{reaction_text}
=== CRITICAL RULES ===
1. THINK about what was said, then reply in {name}'s style. Context first, style second.
2. Keep messages ~{ml['avg_chars']} chars. {"Multiple short messages separated by \\n" if rf['avg_burst'] > 1.5 else "Single messages"}.
3. Match {name}'s EXACT vocabulary. If {name} says "okk" never say "okay". If "haan" never say "yes".
4. {"Use emojis like " + top_emojis + " naturally — don't overuse or underuse" if emo['usage_pct'] > 15 else "You barely use emojis — don't add them"}.
5. Never be robotic. You're a real person having a real conversation.
6. If someone says "hi", just say hi back in your style. Don't dump random info.
7. Stay on the topic the other person brought up. Don't switch topics randomly."""


# ═══════════════════════════════════════════════════════════════════════
# SMART SEGMENTATION
# ═══════════════════════════════════════════════════════════════════════
def detect_topic_shift(before, after):
    if len(before) < 3 or len(after) < 3:
        return False
    t1 = dominant_topic(" ".join(m["text"] for m in before[-5:]))
    t2 = dominant_topic(" ".join(m["text"] for m in after[:5]))
    return t1 != "general" and t2 != "general" and t1 != t2


def smart_segment(messages):
    if not messages: return []
    GAP = 15 * 60
    blocks, cur = [], [messages[0]]
    for i in range(1, len(messages)):
        split = False
        p, c = messages[i-1].get("ts"), messages[i].get("ts")
        if p and c and (c - p).total_seconds() > GAP:
            split = True
        if not split and len(cur) >= 4:
            if detect_topic_shift(cur, messages[i:i+5]):
                split = True
        if split:
            if len(cur) >= 2:
                blocks.append(cur)
            cur = [messages[i]]
        else:
            cur.append(messages[i])
    if len(cur) >= 2:
        blocks.append(cur)
    return blocks


def build_bidirectional_pairs(blocks, people, dna_map):
    pairs = []
    for block in blocks:
        topic = dominant_topic(" ".join(m["text"] for m in block))
        block_people = list(set(m["sender"] for m in block))

        for pa in block_people:
            for pb in block_people:
                if pa == pb: continue
                ms = [m for m in block if m["sender"] in (pa, pb)]
                if len(ms) < 2: continue

                turns, i = [], 0
                while i < len(ms):
                    s = ms[i]["sender"]
                    burst = []
                    while i < len(ms) and ms[i]["sender"] == s:
                        burst.append(ms[i]["text"])
                        i += 1
                    turns.append({"sender": s, "texts": burst, "combined": "\n".join(burst)})

                for j in range(len(turns) - 1):
                    if turns[j]["sender"] == turns[j+1]["sender"]: continue
                    speaker = turns[j]["sender"]
                    replier = turns[j+1]["sender"]
                    ctx = []
                    for k in range(max(0, j-3), j):
                        ctx.append({"role": "user" if turns[k]["sender"] == speaker else "assistant",
                                     "content": turns[k]["combined"]})
                    pairs.append({
                        "mimic": replier, "talker": speaker,
                        "context": ctx, "input": turns[j]["combined"],
                        "output": turns[j+1]["combined"], "topic": topic,
                    })
    return pairs


# ═══════════════════════════════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/validate-promo", methods=["POST"])
def validate_promo():
    """Validate promo code. Never expose the actual API key."""
    code = request.json.get("code", "").strip().upper()
    if code in PROMO_CODES:
        token = hashlib.sha256(f"{code}-{time.time()}".encode()).hexdigest()[:32]
        sessions[token] = {"type": "promo", "code": code, "created": time.time()}
        return jsonify({"valid": True, "token": token, "message": f"✅ Code accepted! Training is on us."})
    return jsonify({"valid": False, "message": "❌ Invalid promo code."})


@app.route("/api/validate-key", methods=["POST"])
def validate_key():
    """Validate user's own OpenAI API key."""
    key = request.json.get("key", "").strip()
    if not key.startswith("sk-"):
        return jsonify({"valid": False, "message": "❌ Invalid key format."})
    try:
        client = OpenAI(api_key=key)
        client.models.list()
        token = hashlib.sha256(f"{key}-{time.time()}".encode()).hexdigest()[:32]
        sessions[token] = {"type": "own", "key": key, "created": time.time()}
        return jsonify({"valid": True, "token": token, "message": "✅ API key verified!"})
    except Exception as e:
        return jsonify({"valid": False, "message": f"❌ Key error: {str(e)[:100]}"})


def get_openai_client(token):
    """Get OpenAI client from session token. Key never touches frontend."""
    sess = sessions.get(token)
    if not sess:
        return None
    if sess["type"] == "promo":
        return OpenAI(api_key=PLATFORM_API_KEY)
    else:
        return OpenAI(api_key=sess["key"])


@app.route("/api/parse", methods=["POST"])
def parse_files():
    """Parse uploaded chat files and return TextDNA."""
    if "files" not in request.files:
        return jsonify({"error": "No files"}), 400

    all_messages = []
    file_info = []
    for f in request.files.getlist("files"):
        text = read_file_safe(f.read())
        msgs = parse_chat(text)
        if len(msgs) < 5:
            continue
        all_messages.extend(msgs)
        people = Counter(m["sender"] for m in msgs)
        file_info.append({"name": f.filename, "count": len(msgs), "people": dict(people)})

    if not all_messages:
        return jsonify({"error": "No valid messages found"}), 400

    # Analyze DNA for each person
    people = list(set(m["sender"] for m in all_messages))
    dna_map = {}
    for person in people:
        dna = analyze_person(all_messages, person)
        if dna:
            dna_map[person] = dna

    # Segment and build pairs
    blocks = smart_segment(all_messages)
    pairs = build_bidirectional_pairs(blocks, people, dna_map)

    # Direction stats
    dir_stats = Counter(f"{p['talker']}→{p['mimic']}" for p in pairs)

    return jsonify({
        "files": file_info,
        "people": list(dna_map.keys()),
        "dna": dna_map,
        "blocks": len(blocks),
        "pairs": len(pairs),
        "pairs_data": pairs,
        "directions": dict(dir_stats),
    })


@app.route("/api/train", methods=["POST"])
def start_training():
    """Build training data and start fine-tuning on OpenAI."""
    data = request.json
    token = data.get("token")
    client = get_openai_client(token)
    if not client:
        return jsonify({"error": "Invalid session. Re-enter API key or promo code."}), 401

    dna_map = data.get("dna", {})
    pairs = data.get("pairs", [])
    targets = data.get("targets", list(dna_map.keys()))

    if not pairs:
        return jsonify({"error": "No training pairs"}), 400

    # Filter to target mimics
    pairs = [p for p in pairs if p["mimic"] in targets]

    # Build JSONL
    examples = []
    for p in pairs:
        mimic = p["mimic"]
        if mimic not in dna_map: continue
        sys_prompt = generate_system_prompt(dna_map[mimic])
        sys_prompt += f"\nChatting with: {p['talker']}\nTopic: {p.get('topic', 'general')}"

        msgs = [{"role": "system", "content": sys_prompt}]
        for ctx in p.get("context", []):
            msgs.append({"role": ctx["role"], "content": ctx["content"]})
        msgs.append({"role": "user", "content": p["input"]})
        msgs.append({"role": "assistant", "content": p["output"]})
        examples.append({"messages": msgs})

    random.shuffle(examples)
    examples = examples[:5000]
    val_n = max(1, len(examples) // 10)
    val, train = examples[:val_n], examples[val_n:]

    train_jsonl = "\n".join(json.dumps(e, ensure_ascii=False) for e in train)
    val_jsonl = "\n".join(json.dumps(e, ensure_ascii=False) for e in val)

    try:
        # Upload files
        import io
        train_file = client.files.create(
            file=io.BytesIO(train_jsonl.encode("utf-8")),
            purpose="fine-tune"
        )
        val_file = client.files.create(
            file=io.BytesIO(val_jsonl.encode("utf-8")),
            purpose="fine-tune"
        )

        # Wait for processing
        for fid in [train_file.id, val_file.id]:
            while True:
                f = client.files.retrieve(fid)
                if f.status == "processed": break
                if f.status == "error": return jsonify({"error": "File processing failed"}), 500
                time.sleep(2)

        # Create job
        job = client.fine_tuning.jobs.create(
            training_file=train_file.id,
            validation_file=val_file.id,
            model="gpt-4.1-nano-2025-04-14",
            suffix="mirror-clone",
            hyperparameters={"n_epochs": 3},
        )

        return jsonify({
            "job_id": job.id,
            "train_count": len(train),
            "val_count": len(val),
            "status": "started",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/train-status", methods=["POST"])
def train_status():
    """Check training job status."""
    data = request.json
    token = data.get("token")
    job_id = data.get("job_id")
    client = get_openai_client(token)
    if not client:
        return jsonify({"error": "Invalid session"}), 401

    try:
        job = client.fine_tuning.jobs.retrieve(job_id)
        events = client.fine_tuning.jobs.list_events(fine_tuning_job_id=job_id, limit=5)
        event_msgs = [e.message for e in events.data] if events.data else []

        return jsonify({
            "status": job.status,
            "model": job.fine_tuned_model,
            "events": event_msgs,
            "error": job.error.message if job.error else None,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chat", methods=["POST"])
def chat():
    """Chat with the fine-tuned model."""
    data = request.json
    token = data.get("token")
    model = data.get("model")
    messages = data.get("messages", [])
    client = get_openai_client(token)
    if not client:
        return jsonify({"error": "Invalid session"}), 401

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=200,
            temperature=0.8,
        )
        reply = resp.choices[0].message.content.strip()
        return jsonify({"reply": reply})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"""
╔═══════════════════════════════════════════╗
║  🪞 MirrorChat Server                     ║
║  http://localhost:{port}                    ║
║                                            ║
║  Platform API: {'✅ Set' if PLATFORM_API_KEY and PLATFORM_API_KEY != 'sk-REPLACE-WITH-YOUR-OPENAI-KEY' else '❌ Not set'}               ║
║  Promo codes:  {len(PROMO_CODES)} active               ║
╚═══════════════════════════════════════════╝
    """)
    app.run(host="0.0.0.0", port=port, debug=True)
