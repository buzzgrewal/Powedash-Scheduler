#!/bin/bash
set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Configuration
CLIENTS_DIR="/opt/powerdash/clients"

# Check arguments
if [ $# -lt 1 ]; then
    echo -e "${RED}Usage: $0 <client_name|all>${NC}"
    echo -e "Example: $0 acme"
    echo -e "Example: $0 all"
    exit 1
fi

TARGET="$1"

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Please run as root (use sudo)${NC}"
    exit 1
fi

update_client() {
    local client_name="$1"

    if [ ! -d "$CLIENTS_DIR/$client_name" ]; then
        echo -e "${RED}Client '$client_name' not found!${NC}"
        return 1
    fi

    echo -e "${YELLOW}Updating client: $client_name${NC}"

    cd "$CLIENTS_DIR/$client_name"

    # Pull latest image and restart
    docker compose pull 2>/dev/null || true
    docker compose up -d --force-recreate

    echo -e "${GREEN}Client '$client_name' updated${NC}"
}

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}   PowerDash HR - Update Clients${NC}"
echo -e "${GREEN}========================================${NC}"

if [ "$TARGET" = "all" ]; then
    echo -e "\n${YELLOW}Rebuilding Docker image...${NC}"
    cd /opt/powerdash/app
    docker build -t powerdash-hr:latest .

    echo -e "\n${YELLOW}Updating all clients...${NC}"
    for client_dir in "$CLIENTS_DIR"/*/; do
        if [ -d "$client_dir" ]; then
            client_name=$(basename "$client_dir")
            update_client "$client_name"
        fi
    done
else
    update_client "$TARGET"
fi

echo -e "\n${GREEN}Update complete!${NC}"
