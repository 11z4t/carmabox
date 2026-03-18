#!/bin/bash
# Deploy CARMA Box to dev-HA (LXC 513 via Proxmox)
set -euo pipefail

PROXMOX="root@192.168.5.2"
LXC_ID=513
TARGET="/etc/homeassistant/custom_components"
ARCHIVE="/tmp/carmabox-deploy.tar.gz"

echo "=== CARMA Box → dev-HA (LXC $LXC_ID) ==="

# Pack
echo "Packing..."
tar czf "$ARCHIVE" -C custom_components carmabox/

# Send to Proxmox
echo "Uploading to Proxmox..."
scp -q "$ARCHIVE" "$PROXMOX:/tmp/"

# Deploy inside LXC
echo "Deploying to LXC $LXC_ID..."
ssh "$PROXMOX" "pct push $LXC_ID /tmp/carmabox-deploy.tar.gz /tmp/carmabox-deploy.tar.gz && \
  pct exec $LXC_ID -- bash -c 'rm -rf $TARGET/carmabox && tar xzf /tmp/carmabox-deploy.tar.gz -C $TARGET/'"

# Restart HA
echo "Restarting Home Assistant..."
ssh "$PROXMOX" "pct exec $LXC_ID -- systemctl restart homeassistant"
sleep 10

# Verify
STATUS=$(ssh "$PROXMOX" "pct exec $LXC_ID -- systemctl is-active homeassistant")
if [ "$STATUS" = "active" ]; then
    echo "=== Deploy OK — HA active ==="
else
    echo "=== DEPLOY FAILED — HA status: $STATUS ==="
    exit 1
fi

# Check logs for errors
ERRORS=$(ssh "$PROXMOX" "pct exec $LXC_ID -- journalctl -u homeassistant --no-pager -n 30 2>/dev/null | grep -ci 'error.*carma' || true")
if [ "$ERRORS" -gt 0 ]; then
    echo "WARNING: $ERRORS CARMA Box errors in HA log"
    ssh "$PROXMOX" "pct exec $LXC_ID -- journalctl -u homeassistant --no-pager -n 30 | grep -i carma"
else
    echo "No CARMA Box errors in log"
fi

rm -f "$ARCHIVE"
echo "Done."
