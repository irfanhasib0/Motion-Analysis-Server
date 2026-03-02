#!/bin/bash
# Cloud deployment script

set -e

echo "========================================="
echo "WebRTC Cloud Setup Script"
echo "========================================="
echo ""

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
    echo "Error: Docker is not installed"
    echo "Install Docker: curl -fsSL https://get.docker.com | sh"
    exit 1
fi

# Check if Docker Compose is installed
if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
    echo "Error: Docker Compose is not installed"
    echo "Install Docker Compose: sudo apt install docker-compose-plugin"
    exit 1
fi

# Generate random secret if not set
if grep -q "CHANGE_ME_LONG_RANDOM" docker-compose.yml; then
    echo "⚠️  Generating random TURN secret..."
    SECRET=$(openssl rand -hex 32)
    
    # Update docker-compose.yml
    sed -i "s/CHANGE_ME_LONG_RANDOM/$SECRET/g" docker-compose.yml
    
    # Update turnserver.conf
    sed -i "s/CHANGE_ME_LONG_RANDOM/$SECRET/g" coturn/turnserver.conf
    
    echo "✓ Generated TURN secret: $SECRET"
    echo ""
fi

# Check if domain is configured
if grep -q "example.com" docker-compose.yml; then
    echo "⚠️  WARNING: You need to configure your domain!"
    echo ""
    echo "Please edit the following files and replace 'example.com' with your actual domain:"
    echo "  - docker-compose.yml (TURN_REALM, TURN_HOST)"
    echo "  - coturn/turnserver.conf (realm)"
    echo "  - cloudflared/config.yml (hostname, tunnel UUID)"
    echo ""
    read -p "Have you configured your domain? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Please configure domain settings first."
        exit 1
    fi
fi

# Check firewall
echo "Checking firewall configuration..."
if command -v ufw &> /dev/null; then
    if sudo ufw status | grep -q "Status: active"; then
        echo "UFW is active. Opening required ports..."
        sudo ufw allow 3478/tcp
        sudo ufw allow 3478/udp
        sudo ufw allow 49160:49200/udp
        echo "✓ Firewall rules added"
    fi
fi

# Pull images
echo "Pulling Docker images..."
docker-compose pull

# Build signaling server
echo "Building signaling server..."
docker-compose build signaling

# Start services
echo "Starting services..."
docker-compose up -d

# Wait for services to start
echo "Waiting for services to start..."
sleep 5

# Check service status
echo ""
echo "Service Status:"
docker-compose ps

# Show logs
echo ""
echo "Recent logs:"
docker-compose logs --tail=20

echo ""
echo "========================================="
echo "Cloud Setup Complete!"
echo "========================================="
echo ""
echo "Services running:"
echo "  - Redis: localhost:6379"
echo "  - Signaling: localhost:8000"
echo "  - Coturn: 0.0.0.0:3478"
echo "  - Cloudflared: tunnel active"
echo ""
echo "Next steps:"
echo "1. Verify Cloudflare tunnel: https://signal.example.com"
echo "2. Configure Cloudflare Access for signal.example.com"
echo "3. Test TURN server: turnutils_uclient -v turn.example.com"
echo ""
echo "View logs: docker-compose logs -f"
echo "Stop services: docker-compose down"
echo ""
