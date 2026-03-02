#!/bin/bash
set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# Configuration
BASE_DIR="/opt/powerdash"
CLIENTS_DIR="$BASE_DIR/clients"
APP_DIR="$BASE_DIR/app"
DOMAIN="powerdashscheduler.com"
BASE_PORT=8600

# Check arguments
if [ $# -lt 2 ]; then
    echo -e "${RED}Usage: $0 <client_name> <display_name>${NC}"
    echo -e "Example: $0 acme 'ACME Corporation'"
    exit 1
fi

CLIENT_NAME=$(echo "$1" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-')
DISPLAY_NAME="$2"

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Please run as root (use sudo)${NC}"
    exit 1
fi

# Check if client already exists
if [ -d "$CLIENTS_DIR/$CLIENT_NAME" ]; then
    echo -e "${RED}Client '$CLIENT_NAME' already exists!${NC}"
    exit 1
fi

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}   Adding Client: $CLIENT_NAME${NC}"
echo -e "${GREEN}========================================${NC}"

# Find next available port
echo -e "\n${YELLOW}[1/6] Allocating port...${NC}"
PORT=$BASE_PORT
while grep -rq "^$PORT$" "$CLIENTS_DIR"/*/port 2>/dev/null; do
    PORT=$((PORT + 1))
done
echo -e "${GREEN}Assigned port: $PORT${NC}"

# Create client directory
echo -e "\n${YELLOW}[2/6] Creating client directory...${NC}"
mkdir -p "$CLIENTS_DIR/$CLIENT_NAME/assets"
echo "$PORT" > "$CLIENTS_DIR/$CLIENT_NAME/port"

# Create branding config
cat > "$CLIENTS_DIR/$CLIENT_NAME/branding_config.yaml" << EOF
# Branding configuration for $DISPLAY_NAME
company:
  name: "$DISPLAY_NAME"
  tagline: "Powered by $DISPLAY_NAME"

colors:
  primary: "#1F4E79"
  secondary: "#2E86AB"
  accent: "#35fbff"
  background: "#000000"

logo:
  enabled: false
  path: "/app/assets/logo.png"

footer:
  show_branding: true
  text: "Powered by $DISPLAY_NAME"
EOF

# Load environment variables
if [ -f "$BASE_DIR/.env" ]; then
    source "$BASE_DIR/.env"
else
    echo -e "${RED}Error: $BASE_DIR/.env not found!${NC}"
    echo -e "Please create the .env file with your credentials first."
    exit 1
fi

# Create docker-compose.yml for this client
echo -e "\n${YELLOW}[3/6] Creating Docker Compose configuration...${NC}"
cat > "$CLIENTS_DIR/$CLIENT_NAME/docker-compose.yml" << EOF
services:
  app:
    image: powerdash-hr:latest
    container_name: powerdash-$CLIENT_NAME
    restart: unless-stopped
    ports:
      - "$PORT:8501"
    environment:
      # Microsoft Graph API
      - GRAPH_TENANT_ID=${GRAPH_TENANT_ID}
      - GRAPH_CLIENT_ID=${GRAPH_CLIENT_ID}
      - GRAPH_CLIENT_SECRET=${GRAPH_CLIENT_SECRET}
      - GRAPH_SCHEDULER_MAILBOX=${GRAPH_SCHEDULER_MAILBOX}
      # OpenAI
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - OPENAI_MODEL=${OPENAI_MODEL:-gpt-4}
      # SMTP
      - SMTP_HOST=${SMTP_HOST}
      - SMTP_PORT=${SMTP_PORT:-587}
      - SMTP_USERNAME=${SMTP_USERNAME}
      - SMTP_PASSWORD=${SMTP_PASSWORD}
      - SMTP_FROM=${SMTP_FROM}
      # IMAP
      - IMAP_HOST=${IMAP_HOST:-imap.gmail.com}
      - IMAP_PORT=${IMAP_PORT:-993}
      - IMAP_USERNAME=${IMAP_USERNAME}
      - IMAP_PASSWORD=${IMAP_PASSWORD}
      # Application
      - CLIENT_NAME=$CLIENT_NAME
      - CLIENT_DISPLAY_NAME=$DISPLAY_NAME
      - DEFAULT_TIMEZONE=${DEFAULT_TIMEZONE:-UTC}
    volumes:
      - powerdash-${CLIENT_NAME}-data:/app/data
      - ./branding_config.yaml:/app/branding_config.yaml:ro
      - ./assets:/app/assets:ro
    networks:
      - powerdash-network
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8501/_stcore/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s

volumes:
  powerdash-${CLIENT_NAME}-data:

networks:
  powerdash-network:
    external: true
EOF

# Start Docker container
echo -e "\n${YELLOW}[4/6] Starting Docker container...${NC}"
cd "$CLIENTS_DIR/$CLIENT_NAME"
docker compose up -d

# Wait for container to be healthy
echo -e "${CYAN}Waiting for container to start...${NC}"
sleep 5

# Create Nginx configuration
echo -e "\n${YELLOW}[5/6] Configuring Nginx...${NC}"
cat > "/etc/nginx/sites-available/$CLIENT_NAME.$DOMAIN" << EOF
server {
    listen 80;
    server_name $CLIENT_NAME.$DOMAIN;

    # Logging
    access_log /var/log/nginx/${CLIENT_NAME}.access.log;
    error_log /var/log/nginx/${CLIENT_NAME}.error.log;

    # Rate limiting
    limit_req zone=powerdash burst=20 nodelay;

    # Proxy to Streamlit container
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 86400;
        proxy_buffering off;
    }

    # Streamlit WebSocket endpoint
    location /_stcore/stream {
        proxy_pass http://127.0.0.1:$PORT/_stcore/stream;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_read_timeout 86400;
    }
}
EOF

# Enable site
ln -sf "/etc/nginx/sites-available/$CLIENT_NAME.$DOMAIN" "/etc/nginx/sites-enabled/"
nginx -t && systemctl reload nginx

# SSL Certificate
echo -e "\n${YELLOW}[6/6] Setting up SSL certificate...${NC}"
echo -e "${CYAN}Requesting SSL certificate from Let's Encrypt...${NC}"
certbot --nginx -d "$CLIENT_NAME.$DOMAIN" --non-interactive --agree-tos --email admin@powerdashscheduler.com --redirect || {
    echo -e "${YELLOW}SSL setup skipped (DNS may not be configured yet).${NC}"
    echo -e "Run this later: certbot --nginx -d $CLIENT_NAME.$DOMAIN"
}

echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}   Client '$CLIENT_NAME' Added Successfully!${NC}"
echo -e "${GREEN}========================================${NC}"
echo -e "\nClient Details:"
echo -e "  - Subdomain: ${CYAN}https://$CLIENT_NAME.$DOMAIN${NC}"
echo -e "  - Port: $PORT"
echo -e "  - Config: $CLIENTS_DIR/$CLIENT_NAME/"
echo -e "\nTo customize branding, edit:"
echo -e "  $CLIENTS_DIR/$CLIENT_NAME/branding_config.yaml"
echo -e "\nTo add a logo, place it in:"
echo -e "  $CLIENTS_DIR/$CLIENT_NAME/assets/logo.png"
