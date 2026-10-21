#!/usr/bin/env bash
# Set up Gemma 4 12B on an Oracle Cloud Always Free Ampere A1 VM
# (4 OCPU / 24 GB RAM, Ubuntu 22.04/24.04 arm64).
#
#   Ollama (127.0.0.1:11434, never exposed)  ←  Caddy (HTTPS + bearer token)  ←  GitHub Actions
#
# Usage (on the VM):
#   sudo DOMAIN=ollama.example.com bash setup.sh
#
# DOMAIN must resolve to this VM's public IP. If omitted, <ip>.sslip.io is used.
# MODEL defaults to gemma4:12b. TOKEN is generated unless given.
# Re-running is safe: it keeps the existing token unless TOKEN is passed.
set -euo pipefail

MODEL="${MODEL:-gemma4:12b}"
NUM_CTX="${NUM_CTX:-32768}"
TOKEN_FILE=/etc/ollama-proxy-token

if [[ $EUID -ne 0 ]]; then
  echo "run with sudo" >&2
  exit 1
fi

if [[ -z "${DOMAIN:-}" ]]; then
  ip="$(curl -fsS https://api.ipify.org)"
  DOMAIN="${ip//./-}.sslip.io"
fi

if [[ -n "${TOKEN:-}" ]]; then
  echo "$TOKEN" > "$TOKEN_FILE"
elif [[ ! -s "$TOKEN_FILE" ]]; then
  openssl rand -hex 32 > "$TOKEN_FILE"
fi
chmod 600 "$TOKEN_FILE"
TOKEN="$(cat "$TOKEN_FILE")"

echo "=== [1/5] Ollama ==="
if ! command -v ollama >/dev/null; then
  curl -fsSL https://ollama.com/install.sh | sh
fi

# Single slot so the ~25k-token system prompt stays in the KV cache between
# articles; keep the model loaded forever; q8 KV cache to fit 32k context.
mkdir -p /etc/systemd/system/ollama.service.d
cat > /etc/systemd/system/ollama.service.d/override.conf <<EOF
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_CONTEXT_LENGTH=${NUM_CTX}"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
EOF
systemctl daemon-reload
systemctl enable --now ollama
systemctl restart ollama
for _ in $(seq 30); do
  curl -fsS http://127.0.0.1:11434/api/version >/dev/null && break
  sleep 1
done

echo "=== [2/5] Pull ${MODEL} ==="
ollama pull "$MODEL"

echo "=== [3/5] Caddy ==="
if ! command -v caddy >/dev/null; then
  apt-get update
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
    | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update
  apt-get install -y caddy
fi

cat > /etc/caddy/Caddyfile <<EOF
${DOMAIN} {
	@authorized header Authorization "Bearer ${TOKEN}"
	handle @authorized {
		reverse_proxy 127.0.0.1:11434 {
			# Ollama only accepts loopback Host headers when bound to 127.0.0.1
			header_up Host 127.0.0.1:11434
		}
	}
	respond 401
}
EOF
chown root:caddy /etc/caddy/Caddyfile
chmod 640 /etc/caddy/Caddyfile
systemctl enable caddy
systemctl reload-or-restart caddy

echo "=== [4/5] Firewall (Oracle images REJECT everything but 22) ==="
for port in 80 443; do
  iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null \
    || iptables -I INPUT -p tcp --dport "$port" -j ACCEPT
done
if command -v netfilter-persistent >/dev/null; then
  netfilter-persistent save
fi

echo "=== [5/5] Warm-up (loads the model into RAM) ==="
curl -fsS http://127.0.0.1:11434/api/generate \
  -d "{\"model\":\"${MODEL}\",\"prompt\":\"hi\",\"stream\":false,\"options\":{\"num_predict\":1}}" \
  >/dev/null && echo "model loaded"

cat <<EOF

Done. Add these in GitHub → repo Settings → Secrets and variables → Actions:

  Variables:  OLLAMA_BASE_URL = https://${DOMAIN}
              OLLAMA_MODEL    = ${MODEL}
  Secrets:    OLLAMA_SELFHOST_TOKEN = ${TOKEN}

Also open TCP 80 and 443 in the VCN security list (see README.md), then test:

  curl -H "Authorization: Bearer ${TOKEN}" https://${DOMAIN}/api/tags
EOF
