# Host Autopilot on Free Cloud Instances (OCI / AWS)

This guide walks you through deploying Autopilot on a free cloud instance (Oracle Cloud Infrastructure Always Free or AWS Free Tier) to run nightly scans and serve the Web Dashboard.

---

## 1. Cloud Provider Comparison

| Feature | Oracle Cloud (OCI) Always Free (Recommended) | AWS Free Tier |
|---|---|---|
| **Free Period** | **Lifetime** (Always Free) | **12 Months** |
| **Resources** | Up to 4 Ampere ARM CPUs + 24 GB RAM (or 2x AMD 1-Core + 1 GB RAM VMs) | 1x `t2.micro` or `t3.micro` (1-Core, 1 GB RAM) |
| **Pacing Scan** | No constraints, easily runs 90 min scans | Can run scans but limited by 1 GB RAM memory overhead |
| **Recommendation** | **Highly Recommended** for permanent, free lifetime hosting. | Good if you already use AWS and only need it temporarily. |

---

## 2. Oracle Cloud (OCI) Step-by-Step Setup

### Step 2.1: Spin up the VM
1. Sign up for an account at [oracle.com/cloud/free](https://oracle.com/cloud/free/).
2. Navigate to **Compute** -> **Instances** -> **Create Instance**.
3. Configure the VM:
   - **Image**: Ubuntu 22.04 LTS (or Oracle Linux).
   - **Shape**: 
     - *ARM option (Best)*: `VM.Standard.A1.Flex` (Choose 2-4 OCPUs, 12-24 GB RAM).
     - *AMD option*: `VM.Standard.E2.1.Micro` (1 OCPU, 1 GB RAM).
   - **SSH Keys**: Download/Save your private key.
4. Click **Create** and copy your **Public IP Address**.

### Step 2.2: Open Network Ports (OCI Inbound Rules)
By default, OCI blocks external traffic. You must open the ports in the OCI dashboard:
1. In the Instance details, click on your **Subnet** link.
2. Click on your **Default Security List**.
3. Click **Add Ingress Rules**:
   - **Source CIDR**: `0.0.0.0/0`
   - **IP Protocol**: `TCP`
   - **Destination Port Range**: `8000,3001,5173`
   - **Description**: Autopilot ports (Dashboard, FreeLLMAPI API, FreeLLMAPI UI)
4. Click **Add Ingress Rules**.

---

## 3. Server Software Setup

SSH into your cloud server (e.g. `ssh -i private.key ubuntu@YOUR_PUBLIC_IP`) and run the setup commands.

### Step 3.1: Install Dependencies
```bash
sudo apt update && sudo apt upgrade -y
# Install Python 3, Node.js and Git
sudo apt install -y python3-pip python3-venv git curl
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
```

### Step 3.2: Clone Repository & Setup Python Environment
```bash
git clone https://github.com/mauryarahul007/autopilot-jobhunt.git
cd autopilot-jobhunt

# Setup python virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

### Step 3.3: Configure Local OS Firewall
Oracle Cloud images have strict local firewalls that must be opened on the machine itself:
```bash
# For Ubuntu:
sudo ufw allow 8000/tcp
sudo ufw allow 3001/tcp
sudo ufw allow 5173/tcp
sudo ufw reload

# If UFW is not active and iptables is blocking (Oracle's default):
sudo iptables -I INPUT 6 -p tcp --dport 8000 -j ACCEPT
sudo iptables -I INPUT 6 -p tcp --dport 3001 -j ACCEPT
sudo iptables -I INPUT 6 -p tcp --dport 5173 -j ACCEPT
sudo netfilter-persistent save
```

### Step 3.4: Set up FreeLLMAPI
Run FreeLLMAPI on port `3001`:
```bash
cd freellmapi
npm install
npm run build

# Generate encryption key and copy to .env
cp .env.example .env
node -e "console.log('ENCRYPTION_KEY=' + require('crypto').randomBytes(32).toString('hex'))" >> .env

# Run migrations
npm run db:migration:up

# Start FreeLLMAPI server in a screen/tmux session or PM2
sudo npm install -g pm2
pm2 start npm --name "freellmapi" -- run dev
```
*(Note down the API key and setup code printed in the logs using `pm2 logs freellmapi`.)*

### Step 3.5: Configure Autopilot
Initialize settings and paste your credentials:
```bash
cd ~/autopilot-jobhunt
# Copy configuration templates if not exist
cp config.example.json config.json
cp .env.example .env

# Edit config.json with your details, including FreeLLMAPI options:
# {
#   "gemini_api_key": "YOUR_GEMINI_KEY",
#   "freellmapi_api_key": "YOUR_FREELLMAPI_KEY_HERE",
#   "freellmapi_base_url": "http://localhost:3001/v1"
# }
nano config.json
```

---

## 4. Run Autopilot Dashboard as a Service

To keep the web dashboard running in the background persistently, create a systemd service:

1. Create a service file:
   ```bash
   sudo nano /etc/systemd/system/autopilot-dashboard.service
   ```
2. Paste the following configuration (replace `ubuntu` and path names if different):
   ```ini
   [Unit]
   Description=Autopilot Job Hunting Dashboard
   After=network.target

   [Service]
   Type=simple
   User=ubuntu
   WorkingDirectory=/home/ubuntu/autopilot-jobhunt
   ExecStart=/home/ubuntu/autopilot-jobhunt/.venv/bin/python -m job_hunt.main dashboard --port 8000
   Restart=on-failure

   [Install]
   WantedBy=multi-user.target
   ```
3. Enable and start the service:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable autopilot-dashboard.service
   sudo systemctl start autopilot-dashboard.service
   ```

You can now open `http://YOUR_PUBLIC_IP:8000` to review jobs and tailor resumes!

---

## 5. Schedule Nightly Scans (Cron)

Set up a cron job to automatically scan for roles every night:

1. Open the cron editor:
   ```bash
   crontab -e
   ```
2. Add the following entry to run the scanner every night at 2:00 AM server time (adjust path if needed):
   ```text
   0 2 * * * cd /home/ubuntu/autopilot-jobhunt && /home/ubuntu/autopilot-jobhunt/.venv/bin/python -m job_hunt.main scan >> /home/ubuntu/autopilot-jobhunt/scan.log 2>&1
   ```
3. Save and close. Logs will accumulate in `scan.log` which can be monitored.
