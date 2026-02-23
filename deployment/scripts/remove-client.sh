#!/bin/bash
set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Configuration
BASE_DIR="/opt/powerdash"
CLIENTS_DIR="$BASE_DIR/clients"
DOMAIN="powerdashscheduler.com"

# Check arguments
if [ $# -lt 1 ]; then
    echo -e "${RED}Usage: $0 <client_name> [--keep-data]${NC}"
    echo -e "Example: $0 acme"
    echo -e "Example: $0 acme --keep-data"
    exit 1
fi

CLIENT_NAME="$1"
KEEP_DATA=false

if [ "$2" == "--keep-data" ]; then
    KEEP_DATA=true
fi

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Please run as root (use sudo)${NC}"
    exit 1
fi

# Check if client exists
if [ ! -d "$CLIENTS_DIR/$CLIENT_NAME" ]; then
    echo -e "${RED}Client '$CLIENT_NAME' not found!${NC}"
    exit 1
fi

echo -e "${YELLOW}========================================${NC}"
echo -e "${YELLOW}   Removing Client: $CLIENT_NAME${NC}"
echo -e "${YELLOW}========================================${NC}"

# Confirm removal
read -p "Are you sure you want to remove client '$CLIENT_NAME'? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Cancelled."
    exit 0
fi

# Stop and remove Docker container
echo -e "\n${YELLOW}[1/4] Stopping Docker container...${NC}"
cd "$CLIENTS_DIR/$CLIENT_NAME"
docker compose down || true

# Backup data if requested
if [ "$KEEP_DATA" = true ]; then
    echo -e "\n${YELLOW}[2/4] Backing up data...${NC}"
    BACKUP_DIR="$BASE_DIR/backups/$CLIENT_NAME-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$BACKUP_DIR"

    # Backup volume data
    docker run --rm -v "powerdash-${CLIENT_NAME}-data:/data" -v "$BACKUP_DIR:/backup" \
        alpine tar -czvf /backup/data.tar.gz /data 2>/dev/null || true

    # Backup config
    cp -r "$CLIENTS_DIR/$CLIENT_NAME" "$BACKUP_DIR/config"

    echo -e "${GREEN}Backup saved to: $BACKUP_DIR${NC}"
else
    echo -e "\n${YELLOW}[2/4] Removing Docker volume...${NC}"
    docker volume rm "powerdash-${CLIENT_NAME}-data" 2>/dev/null || true
fi

# Remove Nginx configuration
echo -e "\n${YELLOW}[3/4] Removing Nginx configuration...${NC}"
rm -f "/etc/nginx/sites-enabled/$CLIENT_NAME.$DOMAIN"
rm -f "/etc/nginx/sites-available/$CLIENT_NAME.$DOMAIN"
nginx -t && systemctl reload nginx

# Remove client directory
echo -e "\n${YELLOW}[4/4] Removing client directory...${NC}"
rm -rf "$CLIENTS_DIR/$CLIENT_NAME"

echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}   Client '$CLIENT_NAME' Removed${NC}"
echo -e "${GREEN}========================================${NC}"

if [ "$KEEP_DATA" = true ]; then
    echo -e "\nBackup location: $BACKUP_DIR"
fi
