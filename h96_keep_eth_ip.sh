#!/system/bin/sh
# Force Ethernet to a fixed LAN address so iPad/PC bookmarks survive DHCP.
WANT_IP=192.168.0.103
MASK=24
GW=192.168.0.1
DNS1=192.168.0.1
DNS2=8.8.8.8

ip link set eth0 up 2>/dev/null
CUR=$(ip -4 -o addr show eth0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n 1)
if [ "$CUR" = "$WANT_IP" ]; then
    exit 0
fi

ip addr flush dev eth0 2>/dev/null
ip addr add ${WANT_IP}/${MASK} brd 192.168.0.255 dev eth0 2>/dev/null
ip route replace 192.168.0.0/24 dev eth0 2>/dev/null
ip route replace default via ${GW} dev eth0 2>/dev/null
ndc resolver setnetdns eth0 '' ${DNS1} ${DNS2} >/dev/null 2>&1
setprop net.dns1 ${DNS1} >/dev/null 2>&1
echo "eth0 set to ${WANT_IP} (was ${CUR:-none})"
