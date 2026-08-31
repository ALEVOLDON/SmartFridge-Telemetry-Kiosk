#!/data/data/com.termux/files/usr/bin/sh

# Prevent CPU from sleeping
termux-wake-lock 2>/dev/null

# Start SSH daemon
pgrep -x sshd >/dev/null 2>&1 || sshd

# Enable persistent ADB over Wi-Fi
su 0 sh -c 'setprop service.adb.tcp.port 5555; setprop persist.adb.tcp.port 5555; stop adbd; start adbd' >/dev/null 2>&1 &

# Restore PM2 dashboard and bots
pm2 resurrect >/dev/null 2>&1 || pm2 start /data/data/com.termux/files/home/bots/bot-manager-dashboard/server.js --name Bot-Manager-Dashboard
