#!/bin/bash

# Colors
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Configuration
CLIENTS_DIR="/opt/powerdash/clients"
DOMAIN="powerdashscheduler.com"

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}   PowerDash HR - Client List${NC}"
echo -e "${GREEN}========================================${NC}"

if [ ! -d "$CLIENTS_DIR" ] || [ -z "$(ls -A $CLIENTS_DIR 2>/dev/null)" ]; then
    echo -e "\n${YELLOW}No clients configured yet.${NC}"
    echo -e "Add a client with: ./add-client.sh <name> '<Display Name>'"
    exit 0
fi

printf "\n%-15s %-8s %-10s %s\n" "CLIENT" "PORT" "STATUS" "URL"
echo "---------------------------------------------------------------"

for client_dir in "$CLIENTS_DIR"/*/; do
    if [ -d "$client_dir" ]; then
        client_name=$(basename "$client_dir")
        port=$(cat "$client_dir/port" 2>/dev/null || echo "N/A")

        # Check container status
        container_status=$(docker inspect -f '{{.State.Status}}' "powerdash-$client_name" 2>/dev/null || echo "not found")

        if [ "$container_status" = "running" ]; then
            status="${GREEN}running${NC}"
        elif [ "$container_status" = "exited" ]; then
            status="${RED}stopped${NC}"
        else
            status="${YELLOW}unknown${NC}"
        fi

        url="https://$client_name.$DOMAIN"

        printf "%-15s %-8s " "$client_name" "$port"
        echo -e "$status ${CYAN}$url${NC}"
    fi
done

echo ""
