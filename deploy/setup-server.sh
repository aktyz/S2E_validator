#!/usr/bin/env bash
# One-time setup of the demo server. Run as root on a fresh Ubuntu 24.04:
#
#   sudo bash setup-server.sh api.example.org '<the deploy public key line>'
#
# It can be run again; every step checks before it changes anything.
#
# What it leaves behind:
#   s2e      the user the service runs as. No login, no sudo.
#   deploy   the user GitHub Actions logs in as. It owns the code and may
#            restart the service, nothing else.
#   /opt/s2e/app        the code (owned by deploy, read-only to the service)
#   /opt/s2e/venv       the Python environment
#   /etc/s2e            settings (readable by the service, not by others)
#   /var/lib/s2e        the batch log and the keys (the service's only writable place)
#
# The service can therefore never change its own code. A deploy cannot read
# the signing key directly, but it replaces the code the service runs, so
# whoever can deploy can run code as the service: guard the deploy workflow.
set -euo pipefail

DOMAIN="${1:?usage: setup-server.sh <api domain> '<deploy ssh public key>'}"
DEPLOY_KEY="${2:?usage: setup-server.sh <api domain> '<deploy ssh public key>'}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi
if [[ ! "$DOMAIN" =~ ^[a-z0-9.-]+$ ]]; then
  echo "the domain may hold only lowercase letters, digits, dots and hyphens" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv nginx certbot python3-certbot-nginx rsync ufw unattended-upgrades

# Only SSH and the web are reachable. The service itself listens on localhost.
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable

id -u s2e >/dev/null 2>&1 || useradd --system --home-dir /var/lib/s2e --shell /usr/sbin/nologin s2e
id -u deploy >/dev/null 2>&1 || useradd --create-home --shell /bin/bash deploy

install -d -o deploy -g deploy -m 0755 /opt/s2e /opt/s2e/app
install -d -o root -g s2e -m 0750 /etc/s2e
install -d -o s2e -g s2e -m 0700 /var/lib/s2e /var/lib/s2e/keys

install -d -o deploy -g deploy -m 0700 /home/deploy/.ssh
if ! grep -qsF "$DEPLOY_KEY" /home/deploy/.ssh/authorized_keys; then
  echo "$DEPLOY_KEY" >> /home/deploy/.ssh/authorized_keys
fi
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 0600 /home/deploy/.ssh/authorized_keys

# The one thing deploy may do as root.
cat > /etc/sudoers.d/s2e-deploy <<'EOF'
deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart s2e-validator.service
EOF
chmod 0440 /etc/sudoers.d/s2e-deploy
visudo -cf /etc/sudoers.d/s2e-deploy

[[ -x /opt/s2e/venv/bin/python ]] || sudo -u deploy python3 -m venv /opt/s2e/venv

if [[ ! -f /etc/s2e/validator.env ]]; then
  install -o root -g s2e -m 0640 "$HERE/validator.env.example" /etc/s2e/validator.env
  echo "wrote /etc/s2e/validator.env from the example: edit it before starting the service"
fi
if [[ ! -f /etc/s2e/campaign.json ]]; then
  install -o root -g s2e -m 0640 "$HERE/../config/example_campaign.json" /etc/s2e/campaign.json
fi

install -o root -g root -m 0644 "$HERE/s2e-validator.service" /etc/systemd/system/s2e-validator.service
systemctl daemon-reload
systemctl enable s2e-validator.service

sed "s/API_DOMAIN/$DOMAIN/g" "$HERE/nginx.conf" > /etc/nginx/sites-available/s2e-validator
ln -sf /etc/nginx/sites-available/s2e-validator /etc/nginx/sites-enabled/s2e-validator
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

cat <<EOF

Server is set up. Still to do, in this order (see validator/README.md):
  1. Point $DOMAIN at this server, then: certbot --nginx -d $DOMAIN
  2. Run the "Deploy validator" workflow once, so the code is in /opt/s2e/app.
  3. Make the signing key ON THIS SERVER and send only the address it prints:
       sudo -u s2e /opt/s2e/venv/bin/python -m s2e_validator.tools.new_key \\
         --out /var/lib/s2e/keys/devnet-validator.json
  4. Fill in /etc/s2e/validator.env, then: systemctl start s2e-validator
EOF
