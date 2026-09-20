#!/bin/sh
# Set up the zero-context public tunnel on a clique server host.
# No account, no sudo, no tailscale: cloudflared quick tunnel as a
# user systemd unit. The https URL lands in ~/.clique/public_url and
# the server advertises it as public_url in / and /v1/clique.
#
#   sh deploy/setup-tunnel.sh          # install + enable + print URL
#
# Requires: linux with systemd user session (enable lingering so it
# survives reboot: loginctl enable-linger $USER, needs sudo once).
set -eu

PORT="${CLIQUE_PORT:-7777}"
BIN="$HOME/bin"
UNITDIR="$HOME/.config/systemd/user"

mkdir -p "$BIN" "$UNITDIR" "$HOME/.clique"

if [ ! -x "$BIN/cloudflared" ]; then
    case "$(uname -m)" in
        aarch64|arm64) arch=arm64 ;;
        x86_64|amd64)  arch=amd64 ;;
        *) echo "unsupported arch $(uname -m)" >&2; exit 1 ;;
    esac
    echo "fetching cloudflared ($arch)..."
    curl -fsSL --connect-timeout 10 -o "$BIN/cloudflared" \
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$arch"
    chmod +x "$BIN/cloudflared"
fi
"$BIN/cloudflared" --version

cat > "$BIN/clique-tunnel.sh" <<EOF
#!/bin/bash
# Quick tunnel for the clique server: public HTTPS URL, no account, no sudo.
# Writes the URL to ~/.clique/public_url so the server can advertise it.
set -u
rm -f ~/.clique/public_url
$BIN/cloudflared tunnel --url http://127.0.0.1:$PORT --no-autoupdate 2>&1 | while IFS= read -r line; do
  echo "\$line"
  u=\$(grep -oE "https://[a-z0-9-]+\\.trycloudflare\\.com" <<<"\$line" | head -1)
  if [ -n "\$u" ]; then echo "\$u" > ~/.clique/public_url; fi
done
EOF
chmod +x "$BIN/clique-tunnel.sh"

cat > "$UNITDIR/clique-tunnel.service" <<EOF
[Unit]
Description=Clique public tunnel (cloudflared quick tunnel, no account)
After=network-online.target clique-server.service
Wants=clique-server.service

[Service]
ExecStart=$BIN/clique-tunnel.sh
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now clique-tunnel
echo "waiting for tunnel URL..."
i=0
while [ $i -lt 30 ]; do
    if [ -s "$HOME/.clique/public_url" ]; then
        echo "public URL: $(cat "$HOME/.clique/public_url")"
        echo "note: this URL rotates when the tunnel restarts;"
        echo "the current one is always shown by: curl http://127.0.0.1:$PORT/"
        exit 0
    fi
    sleep 2; i=$((i+1))
done
echo "tunnel did not report a URL in 60s; check:" >&2
echo "  journalctl --user -u clique-tunnel -n 30" >&2
exit 1
