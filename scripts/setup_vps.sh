#!/usr/bin/env bash
# scripts/setup_vps.sh — Ubuntu 22.04 LTS
# Uso: sudo bash scripts/setup_vps.sh

set -euo pipefail

echo "=== autoxtrade — Setup VPS Ubuntu 22.04 ==="

# 1. Atualiza sistema
apt-get update -y && apt-get upgrade -y

# 2. Instala dependências base
apt-get install -y --no-install-recommends \
    curl wget git unzip ca-certificates gnupg lsb-release \
    build-essential libpq-dev \
    ufw fail2ban

# 3. Docker + Docker Compose
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu \
  $(lsb_release -cs) stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

systemctl enable docker
systemctl start docker

# 4. Adiciona usuário ao grupo docker (se não for root)
if [ -n "${SUDO_USER:-}" ]; then
    usermod -aG docker "$SUDO_USER"
    echo "Usuário $SUDO_USER adicionado ao grupo docker."
fi

# 5. Firewall básico
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
echo "UFW configurado."

# 6. Fail2ban
systemctl enable fail2ban
systemctl start fail2ban
echo "Fail2ban ativo."

# 7. Cria diretório da aplicação
APP_DIR="/opt/autoxtrade"
mkdir -p "$APP_DIR"
echo "Diretório da aplicação: $APP_DIR"

echo ""
echo "=== Setup concluído! ==="
echo "Próximos passos:"
echo "  1. Copie os arquivos do projeto para $APP_DIR"
echo "  2. Crie $APP_DIR/.env a partir de .env.example"
echo "  3. Adicione os certificados TLS em $APP_DIR/nginx/certs/"
echo "  4. Execute: cd $APP_DIR && docker compose up -d"
echo "  5. Execute migrations: docker compose exec backend alembic upgrade head"
echo "  6. Execute seed: docker compose exec backend python scripts/seed_db.py"
