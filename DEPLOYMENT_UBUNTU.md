# Greenie AI Kiosk - Ubuntu Server Deployment Guide

This guide describes how to deploy the Greenie AI Kiosk FastAPI backend on an Ubuntu Linux server on **Port 5007**.

---

## 1. Quick Start Commands

### Step 1: Clone / Copy Files to Ubuntu Server
```bash
git clone <your-repo-url> /opt/greenie_kiosk
cd /opt/greenie_kiosk
```

### Step 2: Set Up Python Virtual Environment
```bash
sudo apt update && sudo apt install -y python3-pip python3-venv
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Step 3: Configure Environment Variables
Copy the example file and edit with your API keys:
```bash
cp .env.example .env
nano .env
```
Ensure the port is set to `5007`:
```env
HOST=0.0.0.0
PORT=5007
LLM_PROVIDER=groq
GROQ_API_KEY=your_actual_groq_api_key
GROQ_MODEL_NAME=qwen/qwen3.8-27b
```

### Step 4: Ingest Knowledge Base (FAISS Index)
```bash
python scripts/ingest.py
```

### Step 5: Start the API Server
#### Option A: Run directly with Python
```bash
python main.py
```

#### Option B: Run with Uvicorn
```bash
uvicorn main:api --host 0.0.0.0 --port 5007 --workers 2
```

#### Option C: Production with Gunicorn & Uvicorn Workers
```bash
gunicorn -w 2 -k uvicorn.workers.UvicornWorker main:api --bind 0.0.0.0:5007
```

---

## 2. Firewall Configuration (UFW)
Allow traffic on port **5007**:
```bash
sudo ufw allow 5007/tcp
sudo ufw status
```

---

## 3. Production Systemd Service (Auto-restart on reboot)

Create a systemd service file:
```bash
sudo nano /etc/systemd/system/greenie.service
```

Paste the following configuration:
```ini
[Unit]
Description=Greenie AI Kiosk API Server
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/opt/greenie_kiosk
EnvironmentFile=/opt/greenie_kiosk/.env
ExecStart=/opt/greenie_kiosk/.venv/bin/gunicorn -w 2 -k uvicorn.workers.UvicornWorker main:api --bind 0.0.0.0:5007
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable greenie
sudo systemctl start greenie
sudo systemctl status greenie
```

To view live server logs:
```bash
sudo journalctl -u greenie -f
```

---

## 4. API Endpoints Reference

| Method | Endpoint | Description | Example Request Payload |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | Server health, active model, and port check | `curl http://localhost:5007/health` |
| `GET` | `/products` | Catalog of products & bundles | `curl http://localhost:5007/products` |
| `POST` | `/chat` | Chat with Greenie Avatar | `curl -X POST http://localhost:5007/chat -H "Content-Type: application/json" -d '{"message":"Any mug under 500?","session_id":"u1"}'` |
| `POST` | `/session/reset` | Clear memory for session | `curl -X POST http://localhost:5007/session/reset -H "Content-Type: application/json" -d '{"session_id":"u1"}'` |
