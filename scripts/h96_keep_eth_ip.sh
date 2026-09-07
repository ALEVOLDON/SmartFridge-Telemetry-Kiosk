#!/system/bin/sh
# Force Ethernet to a fixed LAN address so iPad/PC bookmarks survive DHCP.
WANT_IP=192.168.0.103
MASK=24
GW=192.168.0.1
DNS1=192.168.0.1
DNS2=8.8.8.8

ip link set eth0 up 2>/dev/null

# Keep LAN 192.168.0.0/24 strictly on eth0 regardless of any VPN tun0
ip route replace 192.168.0.0/24 dev eth0 proto static scope link src ${WANT_IP} table eth0 2>/dev/null
ip rule del pref 9000 2>/dev/null
ip rule add to 192.168.0.0/24 lookup eth0 pref 9000 2>/dev/null
ip rule del pref 9001 2>/dev/null
ip rule add from 192.168.0.0/24 lookup eth0 pref 9001 2>/dev/null

# Ensure port 8088 web UI is always allowed in iptables
iptables -C INPUT -p tcp --dport 8088 -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p tcp --dport 8088 -j ACCEPT
iptables -C OUTPUT -p tcp --sport 8088 -j ACCEPT 2>/dev/null || iptables -I OUTPUT 1 -p tcp --sport 8088 -j ACCEPT

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
