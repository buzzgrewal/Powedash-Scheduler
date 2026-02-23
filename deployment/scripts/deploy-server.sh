#!/bin/bash
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}   PowerDash HR - Server Setup${NC}"
echo -e "${GREEN}========================================${NC}"

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Please run as root (use sudo)${NC}"
    exit 1
fi

echo -e "\n${YELLOW}[1/7] Updating system packages...${NC}"
apt-get update && apt-get upgrade -y

echo -e "\n${YELLOW}[2/7] Installing Docker...${NC}"
if ! command -v docker &> /dev/null; then
    apt-get install -y apt-transport-https ca-certificates curl software-properties-common
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
    systemctl enable docker
    systemctl start docker
    echo -e "${GREEN}Docker installed successfully${NC}"
else
    echo -e "${GREEN}Docker already installed${NC}"
fi

echo -e "\n${YELLOW}[3/7] Installing Nginx...${NC}"
if ! command -v nginx &> /dev/null; then
    apt-get install -y nginx
    systemctl enable nginx
    systemctl start nginx
    echo -e "${GREEN}Nginx installed successfully${NC}"
else
    echo -e "${GREEN}Nginx already installed${NC}"
fi

echo -e "\n${YELLOW}[4/7] Installing Certbot...${NC}"
if ! command -v certbot &> /dev/null; then
    apt-get install -y certbot python3-certbot-nginx
    echo -e "${GREEN}Certbot installed successfully${NC}"
else
    echo -e "${GREEN}Certbot already installed${NC}"
fi

echo -e "\n${YELLOW}[5/7] Setting up firewall...${NC}"
apt-get install -y ufw
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable
echo -e "${GREEN}Firewall configured${NC}"

echo -e "\n${YELLOW}[6/7] Creating PowerDash directories...${NC}"
mkdir -p /opt/powerdash/{app,clients,logs,backups}
mkdir -p /opt/powerdash/app/deployment/scripts

# Create Docker network
docker network create powerdash-network 2>/dev/null || true

echo -e "\n${YELLOW}[7/7] Configuring Nginx rate limiting...${NC}"
# Add rate limiting zone to nginx.conf if not exists
if ! grep -q "limit_req_zone" /etc/nginx/nginx.conf; then
    sed -i '/http {/a \    # Rate limiting for PowerDash\n    limit_req_zone $binary_remote_addr zone=powerdash:10m rate=10r/s;' /etc/nginx/nginx.conf
fi

# Test nginx configuration
nginx -t

echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}   Server Setup Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo -e "\nNext steps:"
echo -e "1. Copy application files to /opt/powerdash/app/"
echo -e "2. Create .env file with your credentials"
echo -e "3. Build Docker image: cd /opt/powerdash/app && docker build -t powerdash-hr:latest ."
echo -e "4. Add your first client: ./deployment/scripts/add-client.sh demo 'Demo Company'"
