# PowerDash Scheduler - VPS Deployment Guide

This guide covers deploying PowerDash Scheduler on a VPS with Docker, supporting multi-tenant white-label architecture where each client gets their own isolated subdomain and database.

## Architecture Overview

```
VPS Server
├── Docker
│   ├── powerdash-client1 (Container)
│   ├── powerdash-client2 (Container)
│   └── ... (more clients)
├── Nginx (Reverse Proxy + SSL)
├── Let's Encrypt (Auto SSL)
└── /opt/powerdash/
    ├── app/              # Application source code
    ├── clients/          # Per-client configurations
    │   ├── client1/
    │   │   ├── docker-compose.yml
    │   │   ├── branding_config.yaml
    │   │   └── assets/
    │   └── client2/
    ├── .env              # Global environment variables
    ├── logs/
    └── backups/
```

Each client gets:
- Dedicated Docker container
- Dedicated Docker volume (isolated database)
- Dedicated subdomain (e.g., `client1.yourdomain.com`)
- Custom branding configuration
- SSL certificate

---

## Prerequisites

- Ubuntu 24.04 VPS (2+ vCPU, 4+ GB RAM recommended)
- Domain name with DNS access
- Root SSH access to VPS

---

## Server Information (Example)

| Property | Value |
|----------|-------|
| Provider | Namecheap VPS |
| IP | 104.207.95.232 |
| Domain | powerdashscheduler.com |
| OS | Ubuntu 24.04 |

---

## Step 1: Prepare Deployment Package (Local Machine)

```bash
cd /path/to/Powedash-Scheduler
chmod +x deploy.sh
./deploy.sh
```

This creates a tarball in the `dist/` directory.

---

## Step 2: Upload to Server

```bash
scp dist/powerdash-hr-deploy-*.tar.gz root@YOUR_SERVER_IP:/tmp/
```

---

## Step 3: SSH into Server

```bash
ssh root@YOUR_SERVER_IP
```

---

## Step 4: Run Server Setup

```bash
cd /tmp
tar -xzvf powerdash-hr-deploy-*.tar.gz
cd powerdash-hr-deploy-*
chmod +x deployment/scripts/*.sh
./deployment/scripts/deploy-server.sh
```

This installs:
- Docker
- Nginx
- Certbot (Let's Encrypt)
- UFW Firewall
- Creates directory structure

---

## Step 5: Copy Application Files

```bash
cp -r /tmp/powerdash-hr-deploy-*/* /opt/powerdash/app/
```

---

## Step 6: Create Environment File

```bash
cat > /opt/powerdash/.env << 'EOF'
GRAPH_TENANT_ID=your-tenant-id
GRAPH_CLIENT_ID=your-client-id
GRAPH_CLIENT_SECRET=your-client-secret
GRAPH_SCHEDULER_MAILBOX=scheduling@yourdomain.com
OPENAI_API_KEY=sk-your-openai-key
OPENAI_MODEL=gpt-4o
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-email@gmail.com
SMTP_PASSWORD=your-app-password
SMTP_FROM=your-email@gmail.com
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
IMAP_USERNAME=your-email@gmail.com
IMAP_PASSWORD=your-app-password
DEFAULT_TIMEZONE=UTC
EOF
```

---

## Step 7: Create Streamlit Secrets

```bash
mkdir -p /opt/powerdash/app/.streamlit

cat > /opt/powerdash/app/.streamlit/secrets.toml << 'EOF'
# Microsoft Graph API Configuration
GRAPH_TENANT_ID = "your-tenant-id"
GRAPH_CLIENT_ID = "your-client-id"
GRAPH_CLIENT_SECRET = "your-client-secret"
GRAPH_SCHEDULER_MAILBOX = "scheduling@yourdomain.com"

# OpenAI Configuration
OPENAI_API_KEY = "sk-your-openai-key"
OPENAI_MODEL = "gpt-4o"

# SMTP Configuration
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USERNAME = "your-email@gmail.com"
SMTP_PASSWORD = "your-app-password"
SMTP_FROM = "your-email@gmail.com"

# IMAP Configuration
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
IMAP_USERNAME = "your-email@gmail.com"
IMAP_PASSWORD = "your-app-password"

# Application Settings
DEFAULT_TIMEZONE = "UTC"
EOF
```

**Important:** Use `gpt-4o` or `gpt-5.2` for the OpenAI model as they support vision (PDF parsing).

---

## Step 8: Apply Code Fixes

Fix the ics_utils.py f-string backslash issue:

```bash
cat > /tmp/fix.py << 'FIXEOF'
lines = open('/opt/powerdash/app/ics_utils.py').readlines()
for i, line in enumerate(lines):
    if "(('\\n' + self.url)" in line:
        lines[i] = line.replace("(('\\n' + self.url)", "(chr(10) + self.url")
        print(f'Fixed line {i+1}')
open('/opt/powerdash/app/ics_utils.py', 'w').writelines(lines)
FIXEOF
python3 /tmp/fix.py
```

---

## Step 9: Build Docker Image

```bash
cd /opt/powerdash/app
docker build -t powerdash-hr:latest .
```

If build fails with package errors, the Dockerfile should use:
```dockerfile
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    curl \
    && rm -rf /var/lib/apt/lists/*
```

---

## Step 10: Configure DNS

In your domain registrar's DNS panel, add:

| Type | Host | Value |
|------|------|-------|
| A | @ | YOUR_SERVER_IP |
| A | * | YOUR_SERVER_IP |

Wait 5-30 minutes for DNS propagation.

---

## Step 11: Deploy First Client

```bash
/opt/powerdash/app/deployment/scripts/add-client.sh demo "Demo Company"
```

This creates:
- Docker container: `powerdash-demo`
- Volume: `demo_powerdash-demo-data`
- Nginx config: `/etc/nginx/sites-available/demo.yourdomain.com`
- Client config: `/opt/powerdash/clients/demo/`

---

## Step 12: Setup SSL

```bash
certbot --nginx -d demo.yourdomain.com
```

For the root domain:
```bash
certbot --nginx -d yourdomain.com
```

---

## Client Management Commands

### Add New Client

```bash
/opt/powerdash/app/deployment/scripts/add-client.sh clientname "Client Display Name"
certbot --nginx -d clientname.yourdomain.com
```

### List All Clients

```bash
/opt/powerdash/app/deployment/scripts/list-clients.sh
```

### Remove Client

```bash
/opt/powerdash/app/deployment/scripts/remove-client.sh clientname
# Or keep data backup:
/opt/powerdash/app/deployment/scripts/remove-client.sh clientname --keep-data
```

### Update All Clients (after code changes)

```bash
/opt/powerdash/app/deployment/scripts/update-client.sh all
```

---

## Updating Code from GitHub

```bash
cd /opt/powerdash/app
rm -rf *.py *.json *.png
git clone https://github.com/buzzgrewal/Powedash-Scheduler.git /tmp/newcode
cp -r /tmp/newcode/* /opt/powerdash/app/
rm -rf /tmp/newcode

# Apply fixes
cat > /tmp/fix.py << 'FIXEOF'
lines = open('/opt/powerdash/app/ics_utils.py').readlines()
for i, line in enumerate(lines):
    if "(('\\n' + self.url)" in line:
        lines[i] = line.replace("(('\\n' + self.url)", "(chr(10) + self.url")
open('/opt/powerdash/app/ics_utils.py', 'w').writelines(lines)
print('Fixed!')
FIXEOF
python3 /tmp/fix.py

# Rebuild and restart
docker build --no-cache -t powerdash-hr:latest .
/opt/powerdash/app/deployment/scripts/update-client.sh all
```

---

## Docker Commands

### View Running Containers

```bash
docker ps | grep powerdash
```

### View Container Logs

```bash
docker logs powerdash-demo --tail 100
docker logs -f powerdash-demo  # Follow logs
```

### Restart Client

```bash
cd /opt/powerdash/clients/demo
docker compose restart
```

### Stop/Start Client

```bash
docker compose -f /opt/powerdash/clients/demo/docker-compose.yml down
docker compose -f /opt/powerdash/clients/demo/docker-compose.yml up -d
```

---

## Nginx Commands

```bash
nginx -t                    # Test configuration
systemctl reload nginx      # Reload after changes
systemctl restart nginx     # Full restart
```

---

## SSL Certificate Management

```bash
certbot certificates        # List all certificates
certbot renew --dry-run     # Test renewal
certbot renew               # Renew all certificates
```

---

## Troubleshooting

### Container Won't Start

```bash
docker logs powerdash-clientname
docker inspect powerdash-clientname
```

### 502 Bad Gateway

1. Check if container is running: `docker ps`
2. Check container health: `docker inspect powerdash-clientname`
3. Check nginx logs: `tail -f /var/log/nginx/clientname.error.log`

### OpenAI API Errors

- "image_url not supported" → Change model to `gpt-4o` or `gpt-5.2`
- Check API key in `/opt/powerdash/app/.streamlit/secrets.toml`

### Numpy Import Error

Add to requirements.txt:
```
numpy<2.0.0
pandas
```

Then rebuild Docker image with `--no-cache`.

---

## Directory Structure

```
/opt/powerdash/
├── .env                    # Global environment variables
├── app/                    # Application source code
│   ├── Dockerfile
│   ├── app.py
│   ├── requirements.txt
│   ├── .streamlit/
│   │   └── secrets.toml
│   └── deployment/
│       └── scripts/
│           ├── add-client.sh
│           ├── remove-client.sh
│           ├── list-clients.sh
│           └── update-client.sh
├── clients/                # Per-client configurations
│   ├── demo/
│   │   ├── docker-compose.yml
│   │   ├── branding_config.yaml
│   │   ├── assets/
│   │   └── port
│   └── clientname/
├── logs/
└── backups/
```

---

## Security Notes

1. **Firewall**: UFW allows only SSH (22), HTTP (80), HTTPS (443)
2. **SSL**: All traffic encrypted via Let's Encrypt
3. **Container Isolation**: Each client runs in isolated container
4. **Database Isolation**: Each client has separate Docker volume
5. **Secrets**: Store in `.streamlit/secrets.toml` (not in code)

---

## Backup & Recovery

### Backup Client Data

```bash
docker run --rm \
  -v powerdash-clientname-data:/data \
  -v /opt/powerdash/backups:/backup \
  alpine tar -czvf /backup/clientname-$(date +%Y%m%d).tar.gz /data
```

### Restore Client Data

```bash
docker run --rm \
  -v powerdash-clientname-data:/data \
  -v /opt/powerdash/backups:/backup \
  alpine tar -xzvf /backup/clientname-backup.tar.gz -C /
```

---

## Support

- Container logs: `docker logs powerdash-clientname`
- Nginx logs: `/var/log/nginx/`
- Application logs: Check container stdout

---

## Quick Reference

| Task | Command |
|------|---------|
| Add client | `/opt/powerdash/app/deployment/scripts/add-client.sh name "Name"` |
| List clients | `/opt/powerdash/app/deployment/scripts/list-clients.sh` |
| Remove client | `/opt/powerdash/app/deployment/scripts/remove-client.sh name` |
| View logs | `docker logs powerdash-name --tail 100` |
| Restart client | `cd /opt/powerdash/clients/name && docker compose restart` |
| Rebuild image | `cd /opt/powerdash/app && docker build --no-cache -t powerdash-hr:latest .` |
| Update all | `/opt/powerdash/app/deployment/scripts/update-client.sh all` |
| SSL setup | `certbot --nginx -d name.domain.com` |
