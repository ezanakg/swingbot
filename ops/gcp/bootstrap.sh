#!/usr/bin/env bash
# swingbot bootstrap for a fresh Debian 12 VM (Google Cloud e2-micro, Always Free tier). Run ONCE as root:
#
#   sudo bash bootstrap.sh https://github.com/<you>/swingbot.git [branch]
#
# What it does: installs Python 3.11 + git, adds 2 GB of swap (1 GB RAM is tight for pandas/pyarrow), creates the
# unprivileged `swingbot` service user, clones the repo into /opt/swingbot, builds the venv, installs the systemd
# service + timers and the `swingbotctl` helper. It is safe to re-run (it updates the checkout).
#
# What it deliberately does NOT do: touch secrets. Afterwards copy .env and the encrypted credential, or sign in
# through an SSH tunnel, then run preflight and enable the timers. The steps are in docs/GO_LIVE.md section 9a.
set -euo pipefail

REPO="${1:?usage: bootstrap.sh <git-url> [branch]}"
BRANCH="${2:-main}"
INSTALL=/opt/swingbot
export DEBIAN_FRONTEND=noninteractive

echo "== packages"
apt-get update -q
apt-get install -y -q --no-install-recommends python3 python3-venv python3-pip git ca-certificates tzdata sudo
python3 - <<'PY'
import sys
assert sys.version_info >= (3, 11), f"python {sys.version.split()[0]} found; swingbot needs 3.11+ (Debian 12 ships 3.11)"
PY
timedatectl set-timezone America/New_York >/dev/null 2>&1 || true  # cosmetic: the timers carry their own zone

echo "== swap"
if ! swapon --show --noheadings | grep -q '^/swapfile'; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap -q /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== service user"
id -u swingbot >/dev/null 2>&1 || useradd --system --create-home --home-dir /home/swingbot --shell /usr/sbin/nologin swingbot

echo "== checkout ($BRANCH)"
if [ ! -d "$INSTALL/.git" ]; then
  git clone -q --branch "$BRANCH" "$REPO" "$INSTALL"
  chown -R swingbot:swingbot "$INSTALL"
else
  sudo -u swingbot -H git -C "$INSTALL" fetch -q origin "$BRANCH"
  sudo -u swingbot -H git -C "$INSTALL" checkout -q "$BRANCH"
  sudo -u swingbot -H git -C "$INSTALL" pull -q --ff-only origin "$BRANCH"
fi
mkdir -p "$INSTALL"/var/{data,logs,locks,session,home,cache}
chown -R swingbot:swingbot "$INSTALL/var"
chmod 700 "$INSTALL/var/session"

echo "== python environment"
sudo -u swingbot -H bash -c "cd '$INSTALL' && [ -x .venv/bin/python ] || python3 -m venv .venv"
sudo -u swingbot -H bash -c "cd '$INSTALL' && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -r requirements.txt && .venv/bin/pip install -q -e ."

echo "== systemd units"
install -m 0644 "$INSTALL/ops/systemd/swingbot@.service" /etc/systemd/system/swingbot@.service
for t in manage scan report; do
  install -m 0644 "$INSTALL/ops/systemd/swingbot-$t.timer" "/etc/systemd/system/swingbot-$t.timer"
done
install -m 0755 "$INSTALL/ops/gcp/swingbotctl" /usr/local/bin/swingbotctl
systemctl daemon-reload

cat <<MSG

bootstrap done.
Next (docs/GO_LIVE.md section 9a):
  1. put .env at $INSTALL/.env (owner swingbot, mode 600) and the credential at
     $INSTALL/var/session/robinhood_mcp.cred.enc -- or sign in here:  swingbotctl auth --port 8765 --no-browser
  2. swingbotctl preflight
  3. systemctl enable --now swingbot-manage.timer swingbot-scan.timer swingbot-report.timer && swingbotctl timers
MSG
