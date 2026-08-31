#!/data/data/com.termux/files/usr/bin/sh
# Termux:Boot — wait for Ethernet, pin 192.168.0.103, run fridge monitor forever.
export PREFIX=/data/data/com.termux/files/usr
export HOME=/data/data/com.termux/files/home
export PATH=$PREFIX/bin:$PATH
DIR=$HOME/Samsung_RT34MB_Monitor
PY=$PREFIX/bin/python3
FIX=$DIR/h96_keep_eth_ip.sh
GW=192.168.0.1

termux-wake-lock >/dev/null 2>&1

i=0
while [ "$i" -lt 90 ]; do
    if ip link show eth0 2>/dev/null | grep -q "state UP"; then
        break
    fi
    i=$((i + 1))
    sleep 2
done
sleep 3
su 0 sh "$FIX" >/dev/null 2>&1

j=0
while [ "$j" -lt 30 ]; do
    if ping -c 1 -W 1 "$GW" >/dev/null 2>&1; then
        break
    fi
    su 0 sh "$FIX" >/dev/null 2>&1
    j=$((j + 1))
    sleep 2
done

cd "$DIR" || exit 0

(
    while true; do
        su 0 sh "$FIX" >/dev/null 2>&1
        if ! pgrep -f "auto_monitor.py" >/dev/null 2>&1; then
            "$PY" auto_monitor.py --host 0.0.0.0 --port 8088 >> "$DIR/monitor.log" 2>&1
        fi
        sleep 10
    done
) >/dev/null 2>&1 &
