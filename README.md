# 🪞 MirrorChat — Clone Any Texting Style

Upload WhatsApp chats → Deep TextDNA fingerprinting → Train AI clone → Chat as anyone.

## Features
- **TextDNA Fingerprinting** — Analyzes emoji patterns, Hinglish ratio, typing quirks, rapid-fire habits, reaction styles
- **OpenAI Fine-tuning** — Trains GPT-4.1-nano with personality-enriched system prompts  
- **Bidirectional** — Bot can mimic either person in the conversation
- **Promo Code System** — Gate access with codes, or let users bring their own API key
- **Single Deploy** — One Python file, deploy anywhere

## Quick Start

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/mirrorchat.git
cd mirrorchat

# 2. Install
pip install flask openai

# 3. Configure (edit .env or set environment variables)
cp .env.example .env
# Edit .env with your OpenAI API key and promo codes

# 4. Run
python app.py

# Opens at http://localhost:5000
```

## Environment Variables

| Variable | Description | Required |
|---|---|---|
| `OPENAI_API_KEY` | Your OpenAI API key (platform default) | Yes |
| `PROMO_CODES` | Comma-separated valid promo codes | Yes |
| `SECRET_KEY` | Flask session secret | Optional |
| `PORT` | Server port (default 5000) | Optional |

## How Promo Codes Work

- You set `OPENAI_API_KEY` — this is YOUR key, billed to you
- You set `PROMO_CODES` — e.g., `MIRROR2025,BETA50,FRIEND100`
- Users with a valid promo code → use your API key (you pay)
- Users without a code → must enter their own OpenAI API key (they pay)

## Deploy

### Render / Railway / Fly.io
```bash
# Set env vars in dashboard, then:
python app.py
```

### Docker
```bash
docker build -t mirrorchat .
docker run -p 5000:5000 --env-file .env mirrorchat
```

## Tech Stack
- **Backend**: Flask (Python)
- **Frontend**: Vanilla HTML/CSS/JS (no build step)
- **AI**: OpenAI GPT-4.1-nano fine-tuning API
- **Parsing**: Custom TextDNA engine (in-browser + server-side)

## License
MIT
