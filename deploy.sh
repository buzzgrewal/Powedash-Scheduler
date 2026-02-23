#!/bin/bash
set -e

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}   PowerDash HR - Create Deploy Package${NC}"
echo -e "${GREEN}========================================${NC}"

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Create timestamp
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
PACKAGE_NAME="powerdash-hr-deploy-$TIMESTAMP"

echo -e "\n${YELLOW}[1/3] Creating deployment package...${NC}"

# Create dist directory
mkdir -p dist
rm -rf "dist/$PACKAGE_NAME"
mkdir -p "dist/$PACKAGE_NAME"

# Copy application files
cp -r app.py "dist/$PACKAGE_NAME/"
cp -r requirements.txt "dist/$PACKAGE_NAME/"
cp -r Dockerfile "dist/$PACKAGE_NAME/"
cp -r *.py "dist/$PACKAGE_NAME/" 2>/dev/null || true
cp -r *.json "dist/$PACKAGE_NAME/" 2>/dev/null || true
cp -r *.png "dist/$PACKAGE_NAME/" 2>/dev/null || true
cp -r deployment "dist/$PACKAGE_NAME/"
cp -r .env.example "dist/$PACKAGE_NAME/"

# Copy testing files if they exist
if [ -d "testing_files" ]; then
    cp -r testing_files "dist/$PACKAGE_NAME/"
fi

echo -e "\n${YELLOW}[2/3] Making scripts executable...${NC}"
chmod +x "dist/$PACKAGE_NAME/deployment/scripts/"*.sh

echo -e "\n${YELLOW}[3/3] Creating archive...${NC}"
cd dist
tar -czvf "$PACKAGE_NAME.tar.gz" "$PACKAGE_NAME"

echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}   Package Created Successfully!${NC}"
echo -e "${GREEN}========================================${NC}"
echo -e "\nPackage: ${YELLOW}dist/$PACKAGE_NAME.tar.gz${NC}"
echo -e "\nUpload to server with:"
echo -e "  scp dist/$PACKAGE_NAME.tar.gz root@159.198.41.92:/tmp/"
echo -e "\nThen on the server:"
echo -e "  cd /tmp"
echo -e "  tar -xzvf $PACKAGE_NAME.tar.gz"
echo -e "  cd $PACKAGE_NAME"
echo -e "  ./deployment/scripts/deploy-server.sh"
