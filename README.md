# 🛒 Supermarket Deal Monitor

Daily UK supermarket price tracker that compares Tesco, Sainsbury's, and Iceland, then sends the best deals to your WhatsApp every morning at 08:00 UTC.

## Features

- **Daily price alerts** via WhatsApp (Twilio) at 08:00 UTC
- **Price history** tracking per item with trend detection (up/down/stable)
- **Brand preferences** — save your favourite brands so searches use them automatically
- **Shopping plan** — submit a list, get an AI-optimised store-by-store route that balances price vs. trip count
- **Single item search** — look up any product across all stores instantly

---

## Quick Start

### 1. Clone & install

```bash
git clone https://github.com/YOUR_USERNAME/supermarket-deal-monitor.git
cd supermarket-deal-monitor

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Fill in `.env` with your keys (see [Services Setup](#services-setup) below).

### 3. Start Redis

```bash
# Local (Docker)
docker run -d -p 6379:6379 redis:7-alpine

# Or use a managed service — see Services Setup
```

### 4. Run

```bash
python main.py
```

API is live at `http://localhost:8000`. Interactive docs at `http://localhost:8000/docs`.

---

## Services Setup

### SearchAPI.io
1. Sign up at [searchapi.io](https://www.searchapi.io)
2. Copy your API key → `SEARCHAPI_API_KEY`

### Twilio WhatsApp
1. Create a Twilio account at [twilio.com](https://www.twilio.com)
2. In the console, go to **Messaging → Try it out → Send a WhatsApp message**
3. Join the sandbox by sending the join code from your phone
4. Copy your **Account SID** and **Auth Token** → `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN`
5. Keep `TWILIO_WHATSAPP_FROM=whatsapp:+14155238886` for sandbox testing

> For production, apply for a Twilio WhatsApp sender number and update `TWILIO_WHATSAPP_FROM`.

### Redis
- **Local**: `redis://localhost:6379` (Docker above)
- **Upstash** (free tier, great for serverless): [upstash.com](https://upstash.com) — copy the `rediss://` URL
- **Railway**: add a Redis plugin, copy the `REDIS_URL` from the variables tab

### OpenAI
1. Get an API key at [platform.openai.com](https://platform.openai.com/api-keys)
2. Set `OPENAI_API_KEY` in `.env`

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/` | Run a deal check and send WhatsApp alert |
| `GET` | `/my-groceries` | List saved items with brand preferences |
| `POST` | `/my-groceries/add` | Add / update a grocery preference |
| `POST` | `/my-groceries/remove` | Remove a grocery item |
| `GET` | `/my-groceries/prices` | Price history for all saved items |
| `POST` | `/search` | Search for a specific product |
| `POST` | `/shopping-plan` | Generate an optimised store-by-store plan |

Full interactive docs: `http://localhost:8000/docs`

---

## Deployment

### Railway (recommended)

```bash
# Install Railway CLI
npm install -g @railway/cli

railway login
railway init
railway add --plugin redis       # provisions Redis automatically
railway up
```

Set the env vars in the Railway dashboard under **Variables**.

### Render / Fly.io / any Docker host

A basic `Dockerfile`:

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "main.py"]
```

---

## Scheduling

The app uses **APScheduler** to run the deal check every day at **08:00 UTC** automatically — no cron jobs or external schedulers needed.

Set `DEFAULT_PHONE_NUMBER` in `.env` to the number that should receive scheduled alerts.

To change the schedule, edit the `CronTrigger` in `main.py`:

```python
CronTrigger(hour=8, minute=0)   # 08:00 UTC daily
CronTrigger(hour=8, minute=0, day_of_week="mon-fri")   # weekdays only
```

---

## Project Structure

```
supermarket-deal-monitor/
├── main.py            # FastAPI app — all logic lives here
├── requirements.txt
├── .env.example       # Copy to .env and fill in your keys
├── .gitignore
└── README.md
```
