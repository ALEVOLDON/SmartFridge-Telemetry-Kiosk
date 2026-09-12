function toggleFullScreen() {
        if (!document.fullscreenElement) {
            document.documentElement.requestFullscreen().catch(err => {
                alert('Полноэкранный режим: ' + err.message);
            });
            const btn = document.getElementById('fs-btn');
            if (btn) btn.innerText = '🗗 Свернуть';
        } else {
            if (document.exitFullscreen) {
                document.exitFullscreen();
                const btn = document.getElementById('fs-btn');
                if (btn) btn.innerText = '⛶ На весь экран';
            }
        }
    }
    document.addEventListener('fullscreenchange', () => {
        const btn = document.getElementById('fs-btn');
        if (btn) btn.innerText = document.fullscreenElement ? '🗗 Свернуть' : '⛶ На весь экран';
    });

    const ctx = document.getElementById('liveChart').getContext('2d');
    const liveChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: [],
            datasets: [
                { label: 'Мощность (Вт)', data: [], borderColor: '#3b82f6', backgroundColor: 'rgba(59, 130, 246, 0.1)', yAxisID: 'yP', fill: true, tension: 0.2 },
                { label: 'Напряжение (В)', data: [], borderColor: '#06b6d4', borderDash: [5, 5], yAxisID: 'yV', tension: 0.2 },
                { label: 'Морозилка (°C)', data: [], borderColor: '#38bdf8', borderDash: [2, 2], pointRadius: 0, yAxisID: 'yT', tension: 0.3 }
            ]
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            scales: {
                x: { grid: { color: '#334155' }, ticks: { color: '#94a3b8' } },
                yP: { position: 'left', grid: { color: '#334155' }, ticks: { color: '#3b82f6' }, min: 0, suggestedMax: 150 },
                yV: { position: 'right', grid: { drawOnChartArea: false }, ticks: { color: '#06b6d4' }, min: 180, max: 240 },
                yT: { position: 'right', grid: { drawOnChartArea: false }, ticks: { color: '#38bdf8' }, min: -25, max: 10 }
            },
            plugins: { legend: { labels: { color: '#f8fafc' } } }
        }
    });

    function openSettings() {
        fetch('/api/config')
            .then(r => r.json())
            .then(cfg => {
                document.getElementById('cfg-region').value = cfg.api_region || 'eu';
                document.getElementById('cfg-key').value = cfg.api_key || '';
                document.getElementById('cfg-secret').value = cfg.api_secret || '';
                document.getElementById('cfg-device').value = cfg.device_id || '';
                document.getElementById('cfg-temp-sensor').value = cfg.temp_sensor_id || '';
                document.getElementById('settings-modal').style.display = 'flex';
            });
    }

    function closeSettings() {
        document.getElementById('settings-modal').style.display = 'none';
    }

    function saveSettings() {
        const payload = {
            api_region: document.getElementById('cfg-region').value,
            api_key: document.getElementById('cfg-key').value,
            api_secret: document.getElementById('cfg-secret').value,
            device_id: document.getElementById('cfg-device').value,
            temp_sensor_id: document.getElementById('cfg-temp-sensor').value
        };
        fetch('/api/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        }).then(() => {
            closeSettings();
            pollStatus();
        });
    }

    function testNotification() {
        // 1. Play chime sound directly on device
        playGentleChime();
        
        // 2. Trigger native Web Notification if supported
        if ('Notification' in window) {
            if (Notification.permission === 'granted') {
                try {
                    new Notification('🧊 Авто-Монитор: Samsung RT34MB', {
                        body: '🔔 Тест связи: Звук и оповещения работают отлично!',
                        icon: '/static/icon-192.png'
                    });
                } catch(e) {}
            } else if (Notification.permission !== 'denied') {
                Notification.requestPermission().then(permission => {
                    if (permission === 'granted') {
                        try {
                            new Notification('🧊 Авто-Монитор: Samsung RT34MB', {
                                body: '🔔 Оповещения активированы!',
                                icon: '/static/icon-192.png'
                            });
                        } catch(e) {}
                    }
                });
            }
        }
        
        alert('🔔 Звуковой сигнал колокольчика и тестовое оповещение успешно воспроизведены!');
    }

    function printReport() {
        closeMobileDrawer();
        closeArchiveModal();
        
        const executePrint = () => {
            const prevLimit = (typeof currentCyclesLimit !== 'undefined') ? currentCyclesLimit : 10;
            currentCyclesLimit = 'all';
            if (typeof renderMainCyclesTable === 'function') {
                renderMainCyclesTable(typeof fullArchiveCyclesData !== 'undefined' && fullArchiveCyclesData ? fullArchiveCyclesData : allCyclesData);
            }
            setTimeout(() => {
                window.print();
                setTimeout(() => {
                    currentCyclesLimit = prevLimit;
                    if (typeof renderMainCyclesTable === 'function') renderMainCyclesTable();
                }, 1000);
            }, 200);
        };

        if (typeof fullArchiveCyclesData !== 'undefined' && !fullArchiveCyclesData && !isGhPages && typeof loadFullArchive === 'function') {
            loadFullArchive(executePrint);
        } else {
            executePrint();
        }
    }

    function formatHm(sec) {
        sec = Math.max(0, parseInt(sec, 10) || 0);
        let totalMin = Math.round(sec / 60);
        if (totalMin < 1 && sec > 0) totalMin = 1;
        const h = Math.floor(totalMin / 60);
        const m = totalMin % 60;
        if (h > 0 && m > 0) return h + ' ч ' + m + ' мин';
        if (h > 0) return h + ' ч';
        return m + ' мин';
    }

    function formatSec(sec) {
        const h = String(Math.floor(sec / 3600)).padStart(2, '0');
        const m = String(Math.floor((sec % 3600) / 60)).padStart(2, '0');
        const s = String(sec % 60).padStart(2, '0');
        return `${h}:${m}:${s}`;
    }

    let failedPolls = 0;
    let isAutoScanning = false;
    let historyTick = 0;
    let lastStatus = null;
    let prevRunningState = null;
    let liveTimerSec = 0;
    let liveTimerMode = '';

    let soundEnabled = true;
    try {
        if (localStorage.getItem('pc_sound_enabled') !== null) {
            soundEnabled = (localStorage.getItem('pc_sound_enabled') === 'true');
        }
    } catch(e) {}

    function playGentleChime() {
        if (!soundEnabled) return;
        try {
            const a = document.getElementById('pc-audio-player');
            if (a) {
                a.currentTime = 0;
                a.volume = 1.0;
                a.play().catch(() => {});
            }
        } catch(e) {}
    }

    function updateSoundButtonUI() {
        const btn = document.getElementById('pc-sound-btn');
        if (btn) {
            if (soundEnabled) {
                btn.innerHTML = '🔔 Звук: ВКЛ';
                btn.style.color = '#10b981';
                btn.style.borderColor = 'rgba(16,185,129,0.4)';
            } else {
                btn.innerHTML = '🔕 Звук: ВЫКЛ';
                btn.style.color = '#94a3b8';
                btn.style.borderColor = 'rgba(148,163,184,0.3)';
            }
        }
        syncDrawerSoundUI();
    }

    function syncDrawerSoundUI() {
        const icon = document.getElementById('drawer-sound-icon');
        const text = document.getElementById('drawer-sound-text');
        if (icon && text) {
            if (soundEnabled) {
                icon.innerText = '🔔';
                text.innerHTML = 'Звуковой сигнал: <strong style="color:#10b981;">ВКЛ</strong>';
            } else {
                icon.innerText = '🔕';
                text.innerHTML = 'Звуковой сигнал: <strong style="color:#94a3b8;">ВЫКЛ</strong>';
            }
        }
    }

    function openMobileDrawer() {
        syncDrawerSoundUI();
        const backdrop = document.getElementById('drawer-backdrop');
        const content = document.getElementById('drawer-content');
        if (backdrop && content) {
            backdrop.style.display = 'block';
            setTimeout(() => { content.classList.add('open'); }, 10);
        }
    }

    function closeMobileDrawer() {
        const backdrop = document.getElementById('drawer-backdrop');
        const content = document.getElementById('drawer-content');
        if (backdrop && content) {
            content.classList.remove('open');
            setTimeout(() => { backdrop.style.display = 'none'; }, 280);
        }
    }

    function togglePcSound() {
        soundEnabled = !soundEnabled;
        try {
            localStorage.setItem('pc_sound_enabled', String(soundEnabled));
        } catch(e) {}
        updateSoundButtonUI();
        if (soundEnabled) {
            playGentleChime();
        }
    }

    function syncLiveTimer(data) {
        if (!data) return;
        const isRun = (data.current_mode === 'defrost' || data.is_running);
        const mode = isRun ? 'run' : 'rest';
        const srvSec = isRun ? (Number(data.cycle_duration_sec) || 0) : (Number(data.rest_duration_sec) || 0);
        
        if (liveTimerMode !== mode) {
            liveTimerMode = mode;
            liveTimerSec = srvSec;
            renderLiveTimer();
        } else {
            // Only resync if local stopwatch diverged by more than 3 seconds
            if (Math.abs(liveTimerSec - srvSec) > 3) {
                liveTimerSec = srvSec;
                renderLiveTimer();
            }
        }
    }

    function renderLiveTimer() {
        const timerVal = document.getElementById('auto-timer');
        if (timerVal) {
            timerVal.innerText = formatSec(liveTimerSec);
        }
    }

    function tickLiveTimer() {
        if (lastStatus) {
            liveTimerSec++;
            renderLiveTimer();
        }
    }

    function autoDiscoverNewIP() {
        if (isAutoScanning) return;
        isAutoScanning = true;
        
        const cloudText = document.getElementById('cloud-text');
        if (cloudText) cloudText.innerText = '⚡ Потеря связи. Автопоиск нового IP сервера в сети...';
        
        let found = false;
        const currentHost = window.location.hostname;
        let subnetPrefix = '192.168.0.';
        let currentLast = 1;
        const match = currentHost.match(/^(\d+\.\d+\.\d+)\.(\d+)$/);
        if (match) {
            subnetPrefix = match[1] + '.';
            currentLast = parseInt(match[2], 10) || 1;
        }
        
        const ordered = [];
        const seen = {};
        function addOctet(n) {
            if (n < 2 || n > 254 || seen[n] || n === currentLast) return;
            seen[n] = true;
            ordered.push(n);
        }
        for (let d = 1; d <= 253; d++) {
            addOctet(currentLast + d);
            addOctet(currentLast - d);
        }
        
        const BATCH = 24;
        let idx = 0;
        
        function probe(ipNum, done) {
            const targetIp = subnetPrefix + ipNum;
            const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
            const timer = setTimeout(() => { if (controller) controller.abort(); }, 2000);
            const opts = { mode: 'cors' };
            if (controller) opts.signal = controller.signal;
            fetch(`http://${targetIp}:8088/api/status`, opts)
                .then(r => r.json())
                .then(res => {
                    if (res && (res.connected !== undefined || res.power !== undefined) && !found) {
                        found = true;
                        isAutoScanning = false;
                        failedPolls = 0;
                        if (currentHost !== targetIp && currentHost !== 'localhost' && currentHost !== '127.0.0.1') {
                            window.location.replace(`http://${targetIp}:8088${window.location.pathname}`);
                        }
                    }
                })
                .catch(() => {})
                .then(() => {
                    clearTimeout(timer);
                    if (done) done();
                });
        }
        
        function runBatch() {
            if (found || idx >= ordered.length) {
                isAutoScanning = false;
                return;
            }
            const slice = ordered.slice(idx, idx + BATCH);
            idx += BATCH;
            let pending = slice.length;
            slice.forEach(n => {
                probe(n, () => {
                    pending--;
                    if (pending <= 0 && !found) runBatch();
                });
            });
        }
        runBatch();
        
        setTimeout(() => { isAutoScanning = false; }, 45000);
    }

    
    
    // ================= GITHUB PAGES DEMO MODE FALLBACK =================
    const isGhPages = window.location.hostname.includes('github.io') || window.location.protocol === 'file:';
    let demoTimer = 0;
    let demoPower = 132.4;
    let demoVoltage = 219.8;
    let demoIsRunning = true;
    let demoCycleSec = 840;

    const sampleCycles = [
        { "full_end": "2026-09-01T14:25:00", "date": "01.09.2026", "date_short": "01.09", "start": "13:59", "end": "14:25", "duration_sec": 1560, "duration_str": "26 мин", "rest_sec": 3000, "rest_str": "50 мин", "rest_start": "14:25", "rest_end": "15:15", "krv": "0.34", "avg_power": 131.2, "avg_voltage": 208.5, "cycle_type": "cooling" },
        { "full_end": "2026-09-01T13:13:00", "date": "01.09.2026", "date_short": "01.09", "start": "12:33", "end": "13:13", "duration_sec": 2400, "duration_str": "40 мин", "rest_sec": 2760, "rest_str": "46 мин", "rest_start": "13:13", "rest_end": "13:59", "krv": "0.47", "avg_power": 133.0, "avg_voltage": 209.1, "cycle_type": "cooling" },
        { "full_end": "2026-09-01T11:39:00", "date": "01.09.2026", "date_short": "01.09", "start": "11:19", "end": "11:39", "duration_sec": 1200, "duration_str": "20 мин", "rest_sec": 3300, "rest_str": "55 мин", "rest_start": "11:39", "rest_end": "12:33", "krv": "0.27", "avg_power": 129.8, "avg_voltage": 211.4, "cycle_type": "cooling" },
        { "full_end": "2026-09-01T10:25:00", "date": "01.09.2026", "date_short": "01.09", "start": "10:04", "end": "10:25", "duration_sec": 1260, "duration_str": "21 мин", "rest_sec": 3240, "rest_str": "54 мин", "rest_start": "10:25", "rest_end": "11:19", "krv": "0.28", "avg_power": 130.5, "avg_voltage": 210.8, "cycle_type": "cooling" },
        { "full_end": "2026-09-01T08:05:00", "date": "01.09.2026", "date_short": "01.09", "start": "07:45", "end": "08:05", "duration_sec": 1200, "duration_str": "20 мин", "rest_sec": 1080, "rest_str": "18 мин", "rest_start": "08:05", "rest_end": "08:23", "krv": "0.53", "avg_power": 164.5, "avg_voltage": 212.0, "cycle_type": "defrost" },
        { "full_end": "2026-09-01T06:50:00", "date": "01.09.2026", "date_short": "01.09", "start": "06:22", "end": "06:50", "duration_sec": 1680, "duration_str": "28 мин", "rest_sec": 3300, "rest_str": "55 мин", "rest_start": "06:50", "rest_end": "07:45", "krv": "0.34", "avg_power": 132.1, "avg_voltage": 214.2, "cycle_type": "cooling" }
    ];

    function getMockStatus() {
        demoTimer++;
        if (demoIsRunning) {
            demoPower = 129 + Math.sin(demoTimer / 5) * 4 + (Math.random() * 2);
            demoCycleSec += 3;
            if (demoCycleSec > 1600) {
                demoIsRunning = false;
                demoCycleSec = 0;
            }
        } else {
            demoPower = 0.0;
            demoCycleSec += 3;
            if (demoCycleSec > 2800) {
                demoIsRunning = true;
                demoCycleSec = 0;
            }
        }
        demoVoltage = 219 + Math.sin(demoTimer / 8) * 3;

        return {
            "connected": true,
            "connection_source": "local_wifi",
            "power": demoPower,
            "voltage": demoVoltage,
            "current": demoPower > 10 ? (demoPower / demoVoltage) : 0.0,
            "is_running": demoIsRunning,
            "current_mode": demoIsRunning ? "cooling" : "idle",
            "cycle_duration_sec": demoIsRunning ? demoCycleSec : 0,
            "rest_duration_sec": !demoIsRunning ? demoCycleSec : 0,
            "last_update": new Date().toTimeString().split(' ')[0],
            "temp_freezer": demoIsRunning ? -18.8 : -17.4,
            "temp_fridge": 4.1,
            "temp_freezer_estimated": true,
            "temp_fridge_estimated": true,
            "cloud_quota": {
                "remaining": 49994,
                "total_limit": 50000,
                "used_today": 6,
                "forecast_text": "100% экономия: 0 запросов/день при LAN"
            },
            "last_blackout": {
                "detected": true,
                "date": "29.08.2026",
                "start": "13:00",
                "end": "15:03",
                "duration_str": "2 ч 3 мин",
                "food_safety": "🟢 Оценка без датчика: за ≤4 ч камера обычно теряет около 1°C"
            }
        };
    }

    function pollStatus() {
        if (isGhPages) {
            handleStatusPayload(generateMockStatus());
            return;
        }
        fetch('/api/status')
            .then(r => r.json())
            .then(data => {
                handleStatusPayload(data);
            })
            .catch(err => {
                console.error(err);
                failedPolls++;
                if (failedPolls >= 2) {
                    autoDiscoverNewIP();
                }
            });
    }

    function handleStatusPayload(data) {
        failedPolls = 0;
        isAutoScanning = false;
        
        const cloudDot = document.getElementById('cloud-dot');
        const cloudText = document.getElementById('cloud-text');
        if (data.connected) {
            cloudDot.className = 'dot dot-green';
            if (data.connection_source === 'local_wifi') {
                cloudText.innerText = 'Розетка по локальной сети (без облака)';
            } else {
                cloudText.innerText = 'Tuya Cloud API: запасной канал';
            }
            document.getElementById('last-sync').innerText = 'Обновлено: ' + (data.last_update || '--');
        } else {
            cloudDot.className = 'dot dot-red';
            cloudText.innerText = data.error_message || 'Ожидание настройки API Tuya Cloud';
        }

        // Update Channel & Quota Card
        const qCard = document.getElementById('channel-quota-card');
        const qIcon = document.getElementById('channel-icon');
        const qTitle = document.getElementById('channel-title');
        const qSub = document.getElementById('channel-subtitle');
        const qBadge = document.getElementById('quota-badge');
        const cBadge = document.getElementById('channel-badge');

        const quota = data.cloud_quota || {};
        const rem = quota.remaining !== undefined ? quota.remaining : 50000;
        const tot = quota.total_limit || 50000;
        const todayUsed = quota.used_today || 0;
        const isLan = (data.connection_source === 'local_wifi');

        if (qCard && qTitle && qSub) {
            if (isLan) {
                qCard.style.borderLeftColor = '#10b981';
                qIcon.innerText = '🏠';
                qTitle.innerHTML = 'Канал связи: <strong>Прямой локальный Wi-Fi</strong> (без облака)';
                qSub.innerHTML = `🛡️ Запросов в облако сегодня: <strong>${todayUsed}</strong> | ${escapeHTML(quota.forecast_text || '100% экономия: лимиты не тратятся')}`;
                if (qBadge) {
                    qBadge.innerText = `Квота: ${rem} / ${tot}`;
                    qBadge.style.color = '#10b981';
                    qBadge.style.background = 'rgba(16,185,129,0.15)';
                }
                if (cBadge) {
                    cBadge.innerText = 'LAN: 0 запросов';
                    cBadge.style.color = '#38bdf8';
                    cBadge.style.background = 'rgba(56,189,248,0.15)';
                }
            } else {
                qCard.style.borderLeftColor = '#f59e0b';
                qIcon.innerText = '☁️';
                qTitle.innerHTML = 'Канал связи: <strong>Tuya Cloud API (Эко-резерв)</strong>';
                qSub.innerHTML = `⚠️ Запросов сегодня: <strong>${todayUsed}</strong> | ${escapeHTML(quota.forecast_text || 'Эко-режим: ~1400/день')}`;
                if (qBadge) {
                    qBadge.innerText = `Остаток квоты: ${rem} / ${tot}`;
                    qBadge.style.color = '#f59e0b';
                    qBadge.style.background = 'rgba(245,158,11,0.15)';
                }
                if (cBadge) {
                    cBadge.innerText = 'Облако: Эко 25/60с';
                    cBadge.style.color = '#f59e0b';
                    cBadge.style.background = 'rgba(245,158,11,0.15)';
                }
            }
        }

        const dSrv = document.getElementById('drawer-server-ip');
        const dChan = document.getElementById('drawer-channel-info');
        const dQuota = document.getElementById('drawer-quota-info');
        if (dSrv) {
            dSrv.innerText = (window.location.hostname || 'localhost') + (window.location.port ? ':' + window.location.port : '');
        }
        if (dChan) {
            dChan.innerHTML = isLan ? 'Канал: <strong style="color:#10b981;">Прямой LAN Wi-Fi (0 обл.)</strong>' : 'Канал: <strong style="color:#f59e0b;">Tuya Cloud API</strong>';
        }
        if (dQuota) {
            dQuota.innerHTML = `Квота: <strong style="color:#10b981;">${rem} / ${tot}</strong>`;
        }

        document.getElementById('live-power').innerHTML = `${data.power.toFixed(1)} <span class="unit">Вт</span>`;
        document.getElementById('live-voltage').innerHTML = `${data.voltage.toFixed(1)} <span class="unit">В</span>`;
        document.getElementById('live-current').innerHTML = `${data.current.toFixed(2)} <span class="unit">А</span>`;
        
        const freezerEst = (data.temp_freezer_estimated !== undefined)
            ? data.temp_freezer_estimated
            : ((data.temp_is_estimated !== undefined) ? data.temp_is_estimated : (!data.temp_sensor_connected));
        const fridgeEst = (data.temp_fridge_estimated !== undefined) ? data.temp_fridge_estimated : true;
        const fTag = document.getElementById('freezer-tag');
        const rTag = document.getElementById('fridge-tag');
        const fDesc = document.getElementById('freezer-desc');
        const rDesc = document.getElementById('fridge-desc');
        
        if (fTag) {
            if (freezerEst) {
                fTag.innerText = '📊 МОДЕЛЬ (ОЦЕНКА)';
                fTag.style.color = '#38bdf8';
                fTag.style.background = 'rgba(56,189,248,0.15)';
                if (fDesc) fDesc.innerText = 'Теплофизическая модель (физический датчик не подключен)';
            } else {
                fTag.innerText = '📡 ТЕЛЕМЕТРИЯ ДАТЧИКА';
                fTag.style.color = '#10b981';
                fTag.style.background = 'rgba(16,185,129,0.15)';
                if (fDesc) fDesc.innerText = 'Прямые показания с физического термодатчика Tuya';
            }
        }
        if (rTag) {
            if (fridgeEst) {
                rTag.innerText = '📊 МОДЕЛЬ (ОЦЕНКА)';
                rTag.style.color = '#10b981';
                rTag.style.background = 'rgba(16,185,129,0.15)';
                if (rDesc) rDesc.innerText = 'Теплофизическая модель (отдельный датчик камеры не подключен)';
            } else {
                rTag.innerText = '📡 ТЕЛЕМЕТРИЯ ДАТЧИКА';
                rTag.style.color = '#10b981';
                rTag.style.background = 'rgba(16,185,129,0.15)';
                if (rDesc) rDesc.innerText = 'Прямые показания с физического термодатчика Tuya';
            }
        }
        
        if (data.temp_freezer !== undefined) {
            const fVal = document.getElementById('temp-freezer-val');
            if (fVal) fVal.innerHTML = `${data.temp_freezer > 0 ? '+' : ''}${data.temp_freezer.toFixed(1)} <span class="unit">°C</span>`;
        }
        if (data.temp_fridge !== undefined) {
            const rVal = document.getElementById('temp-fridge-val');
            if (rVal) rVal.innerHTML = `${data.temp_fridge > 0 ? '+' : ''}${data.temp_fridge.toFixed(1)} <span class="unit">°C</span>`;
        }

        // Update Blackout Status Banner
        if (data.last_blackout && data.last_blackout.detected) {
            const bCard = document.getElementById('blackout-card');
            const bTitle = document.getElementById('blackout-title');
            const bSub = document.getElementById('blackout-subtitle');
            const bBadge = document.getElementById('blackout-badge');
            
            bTitle.innerHTML = `⚡ Отключение света: <strong>${escapeHTML(data.last_blackout.date)} (${escapeHTML(data.last_blackout.start)} → ${escapeHTML(data.last_blackout.end)})</strong>`;
            bSub.innerHTML = `Длительность: <strong>${escapeHTML(data.last_blackout.duration_str)}</strong>. ${escapeHTML(data.last_blackout.food_safety)}`;
            bBadge.innerText = data.last_blackout.duration_str;
            bBadge.style.color = '#f59e0b';
        }

        const desc = document.getElementById('live-state-desc');
        const timerTitle = document.getElementById('timer-title');
        const timerVal = document.getElementById('auto-timer');
        const verdictEl = document.getElementById('cycle-verdict');
        
        lastStatus = data;
        syncLiveTimer(data);
        if (data.current_mode === 'defrost') {
            desc.innerText = '🔥 Автооттайка No Frost (ТЭН)';
            desc.style.color = '#f97316';
            
            timerTitle.innerText = '🔥 АКТИВНЫЙ РЕЖИМ: АВТООТТАЙКА NO FROST (ТЭН)';
            timerTitle.style.color = '#f97316';
            timerVal.style.color = '#f97316';
            verdictEl.innerHTML = '🔥 Компрессор отключен. Электронагреватель (ТЭН ~165 Вт) растапливает иней на испарителе. Норма: <strong>10–25 минут</strong>.';
        } else if (data.is_running) {
            desc.innerText = '🟢 Компрессор работает (Охлаждение)';
            desc.style.color = '#10b981';
            
            timerTitle.innerText = '🟢 АКТИВНЫЙ РЕЖИМ: ОХЛАЖДЕНИЕ (КОМПРЕССОР)';
            timerTitle.style.color = '#10b981';
            timerVal.style.color = '#10b981';
            verdictEl.innerHTML = '❄️ Компрессор активно морозит продукты. Штатный цикл <strong>15–45 мин</strong>; при загрузке норма до <strong>3.5–5 ч</strong>.';
        } else {
            desc.innerText = '⚪ Компрессор выключен (Отдых)';
            desc.style.color = '#94a3b8';
            
            const restSec = Number(data.rest_duration_sec) || 0;
            const restMin = Math.floor(restSec / 60);
            const estRemain = Math.max(0, 45 - restMin);
            const lockout = data.protection_lockout_sec || 0;
            
            timerTitle.innerText = '⚪ АКТИВНЫЙ РЕЖИМ: ОТДЫХ КОМПРЕССОРА (СТОЯНКА)';
            timerTitle.style.color = '#06b6d4';
            timerVal.style.color = '#06b6d4';
            let restHtml = `🧊 Корпус удерживает мороз. Отдых длится <strong>${restMin} мин</strong> (норма: 35–55 мин). Ожидаемый запуск через ~<strong>${estRemain} мин</strong>.`;
            if (data.protection_active && lockout > 0) {
                restHtml += ` Пауза пуска: <strong>${lockout} с</strong>.`;
            }
            verdictEl.innerHTML = restHtml;
        }

        // Health & Anomaly Diagnostics Banner Update
        const healthCard = document.getElementById('health-card');
        const healthIcon = document.getElementById('health-icon');
        const healthTitle = document.getElementById('health-title');
        const healthSubtitle = document.getElementById('health-subtitle');
        const healthBadge = document.getElementById('health-badge');

        if (data.current_mode === 'defrost') {
            const durMin = Math.floor(data.cycle_duration_sec / 60);
            healthCard.style.borderLeftColor = '#f97316';
            healthIcon.innerText = '🔥';
            healthTitle.innerText = '🔥 Автооттайка испарителя No Frost (ТЭН)';
            healthSubtitle.innerText = `Компрессор спит. Нагреватель растапливает иней уже ${durMin} мин (норма: 10–25 мин). Талая вода уходит в дренаж.`;
            healthBadge.innerText = 'Оттайка: ТЭН 165W';
            healthBadge.style.color = '#f97316';
            healthBadge.style.background = 'rgba(249, 115, 22, 0.15)';
        } else if (data.is_running) {
            const durMin = Math.floor(data.cycle_duration_sec / 60);
            if (durMin <= 45) {
                healthCard.style.borderLeftColor = '#10b981';
                healthIcon.innerText = '🟢';
                healthTitle.innerText = '🟢 Штатная работа компрессора (15–45 мин)';
                healthSubtitle.innerText = `Компрессор работает ${durMin} мин. Система No Frost циркулирует холод штатно.`;
                healthBadge.innerText = 'Режим: Штатный';
                healthBadge.style.color = '#10b981';
                healthBadge.style.background = 'rgba(16, 185, 129, 0.15)';
            } else if (durMin <= 150) {
                healthCard.style.borderLeftColor = '#f59e0b';
                healthIcon.innerText = '🟡';
                healthTitle.innerText = '🟡 Повышенная нагрузка (донабор холода)';
                healthSubtitle.innerText = `Компрессор работает ${durMin} мин. Норма при открывании дверей или малой загрузке (1.5–2.5 ч).`;
                healthBadge.innerText = 'Донабор холода / Загрузка';
                healthBadge.style.color = '#f59e0b';
                healthBadge.style.background = 'rgba(245, 158, 11, 0.15)';
            } else if (durMin <= 420) {
                healthCard.style.borderLeftColor = '#f97316';
                healthIcon.innerText = '📦';
                healthTitle.innerText = '🟠 Глубокая проморозка (загрузка / после блэкаута)';
                healthSubtitle.innerText = `Компрессор работает ${durMin} мин. Штатно после отключения света или большой загрузки теплых продуктов (норма 3.5–5 ч, запас до 7 ч).`;
                healthBadge.innerText = 'Загрузка / после блэкаута';
                healthBadge.style.color = '#f97316';
                healthBadge.style.background = 'rgba(249, 115, 22, 0.15)';
            } else {
                healthCard.style.borderLeftColor = '#ef4444';
                healthIcon.innerText = '⚠️';
                healthTitle.innerText = '🔴 Внимание: Непрерывная работа компрессора (> 7 ч)';
                healthSubtitle.innerText = `Компрессор работает уже ${durMin} мин без остановки. Проверьте дверь, уплотнители, термостат и уровень фреона.`;
                healthBadge.innerText = 'Тревога: Работа > 7ч';
                healthBadge.style.color = '#ef4444';
                healthBadge.style.background = 'rgba(239, 68, 68, 0.15)';
            }
        } else {
            const restSec = data.rest_duration_sec || 0;
            const restMin = Math.floor(restSec / 60);
            if (restMin <= 60) {
                healthCard.style.borderLeftColor = '#06b6d4';
                healthIcon.innerText = '🧊';
                healthTitle.innerText = '⚪ Штатный отдых (Удержание холода)';
                healthSubtitle.innerText = `Компрессор отдыхает ${restMin} мин (норма: 35–55 мин). Термоизоляция и уплотнители отлично держат мороз.`;
                healthBadge.innerText = 'Отдых: Норма';
                healthBadge.style.color = '#06b6d4';
                healthBadge.style.background = 'rgba(6, 182, 212, 0.15)';
            } else if (restMin <= 95) {
                healthCard.style.borderLeftColor = '#3b82f6';
                healthIcon.innerText = '🕒';
                healthTitle.innerText = '🔵 Глубокий покой (> 1 ч отдыха)';
                healthSubtitle.innerText = `Холодильник отдыхает уже ${restMin} мин. Камера глубоко проморожена, дверь не открывали.`;
                healthBadge.innerText = 'Глубокий мороз';
                healthBadge.style.color = '#3b82f6';
                healthBadge.style.background = 'rgba(59, 130, 246, 0.15)';
            } else {
                healthCard.style.borderLeftColor = '#f59e0b';
                healthIcon.innerText = '⚠️';
                healthTitle.innerText = '🟡 Внимание: Длительный простой (> 1.5 ч)';
                healthSubtitle.innerText = `Холодильник не включается уже ${restMin} мин. Если в морозилке тепло, проверьте терморегулятор или таймер оттайки.`;
                healthBadge.innerText = 'Длительный простой';
                healthBadge.style.color = '#f59e0b';
                healthBadge.style.background = 'rgba(245, 158, 11, 0.15)';
            }
        }
        
        // Add to chart
        if (data.last_update) {
            liveChart.data.labels.push(data.last_update);
            liveChart.data.datasets[0].data.push(data.power);
            liveChart.data.datasets[1].data.push(data.voltage);
            liveChart.data.datasets[2].data.push(data.temp_freezer !== undefined ? data.temp_freezer : -18.0);
            if (liveChart.data.labels.length > 25) {
                liveChart.data.labels.shift();
                liveChart.data.datasets[0].data.shift();
                liveChart.data.datasets[1].data.shift();
                liveChart.data.datasets[2].data.shift();
            }
            liveChart.update();
        }

        if (prevRunningState !== null && prevRunningState !== data.is_running) {
            updateCyclesTable();
            historyTick = 0;
            if (prevRunningState === true && data.is_running === false) {
                playGentleChime();
            }
        }
        prevRunningState = data.is_running;

        historyTick++;
        if (historyTick === 1 || historyTick % 3 === 0) {
            updateCyclesTable();
        }
    }


    function escapeHTML(str) {
        if (!str) return '';
        return String(str).replace(/[&<>'"]/g, function(tag) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[tag] || tag;
        });
    }

    let allCyclesData = [];
    let fullArchiveCyclesData = null;
    let isArchiveLoading = false;
    let currentArchiveFilter = 'all';

    function loadFullArchive(callback) {
        if (fullArchiveCyclesData) {
            if (callback) callback();
            return;
        }
        if (isArchiveLoading) return;
        isArchiveLoading = true;

        const tbody = document.getElementById('archive-body');
        if (tbody && document.getElementById('archive-modal').style.display === 'flex') {
            tbody.innerHTML = '<tr><td colspan="10" style="text-align:center; padding:30px; color:#38bdf8;">⏳ Загрузка полного архива измерений...</td></tr>';
        }

        fetch('/api/history?limit=all')
            .then(r => r.json())
            .then(data => {
                isArchiveLoading = false;
                fullArchiveCyclesData = data.cycles || [];
                allBlackoutsData = data.blackouts || [];
                if (callback) {
                    callback();
                } else if (document.getElementById('archive-modal').style.display === 'flex') {
                    renderArchiveTable(currentArchiveFilter);
                }
            })
            .catch(err => {
                console.error("Ошибка загрузки архива:", err);
                isArchiveLoading = false;
                if (callback) callback();
            });
    }

    function openArchiveModal() {
        document.getElementById('archive-modal').style.display = 'flex';
        if (!fullArchiveCyclesData && !isGhPages) {
            renderArchiveTable(currentArchiveFilter);
            loadFullArchive();
        } else {
            renderArchiveTable(currentArchiveFilter);
        }
    }

    function closeArchiveModal() {
        document.getElementById('archive-modal').style.display = 'none';
    }

    function filterArchive(type) {
        currentArchiveFilter = type;
        document.querySelectorAll('.filter-btn').forEach(btn => btn.classList.remove('active'));
        const activeBtn = document.getElementById('filter-' + type);
        if (activeBtn) activeBtn.classList.add('active');
        renderArchiveTable(type);
    }

    let allBlackoutsData = [];

    function createCycleRow(c) {
        const mins = Math.floor((c.duration_sec || 0) / 60);
        let durStr = c.duration_str || formatHm(c.duration_sec);
        let verdict = '🟢 Заморозка (Компрессор)';
        let color = '#10b981';
        let bgStyle = '';
        let restDisplay = c.rest_str || '—';
        let restFrom = c.rest_start || '—';
        let restTo = c.rest_end || '—';
        let krvDisplay = (c.krv !== undefined && c.krv !== null && c.krv !== '') ? `${c.krv}` : '—';
        let pwrDisplay = `${c.avg_power} Вт`;
        
        if (c.live) {
            if (c.cycle_type === 'idle') {
                verdict = '⚪ Отдых (сейчас)';
                color = '#06b6d4';
            } else if (c.cycle_type === 'defrost') {
                verdict = '🔥 Оттайка (сейчас)';
                color = '#f97316';
            } else {
                verdict = '🟢 Заморозка (сейчас)';
                color = '#10b981';
            }
            bgStyle = 'background: rgba(56,189,248,0.10);';
        } else if (c.cycle_type === 'blackout') {
            durStr = `⚡ ${c.duration_str || durStr}`;
            verdict = `⚡ Отключение света (${c.duration_str || durStr})`;
            color = '#f59e0b';
            restDisplay = '⚡ Сбой 220V';
            restFrom = '—';
            restTo = '—';
            krvDisplay = '—';
            pwrDisplay = '0 Вт (Сеть)';
            bgStyle = 'background: rgba(245,158,11,0.08);';
        } else if (c.cycle_type === 'defrost') {
            verdict = '🔥 Автооттайка No Frost (ТЭН)';
            color = '#f97316';
            bgStyle = 'background: rgba(249,115,22,0.06);';
        } else if (mins > 360) {
            verdict = '🔴 Длительная работа (> 6ч)';
            color = '#ef4444';
        } else if (mins < 5) {
            verdict = '🟡 Короткий запуск (< 5 мин)';
            color = '#f59e0b';
        }
        
        const dateStr = c.date_short || c.date || '';
        const row = document.createElement('tr');
        if (bgStyle) row.setAttribute('style', bgStyle);
        row.innerHTML = `
            <td><span style="color:#94a3b8; font-weight:600;">${escapeHTML(dateStr)}</span></td>
            <td><strong>${escapeHTML(c.start)}</strong></td>
            <td><strong>${escapeHTML(c.end)}</strong></td>
            <td><span style="color:${color}; font-weight:bold;">${escapeHTML(durStr)}</span></td>
            <td><span style="color:#06b6d4; font-weight:600;">${escapeHTML(restFrom)}</span></td>
            <td><span style="color:#06b6d4; font-weight:600;">${escapeHTML(restTo)}</span></td>
            <td><span style="color:#06b6d4; font-weight:600;">${escapeHTML(restDisplay)}</span></td>
            <td><span style="font-weight:600; color:#f8fafc;">${escapeHTML(krvDisplay)}</span></td>
            <td>${escapeHTML(pwrDisplay)}</td>
            <td><span style="color:${color}; font-weight:600;">${escapeHTML(verdict)}</span></td>
        `;
        return row;
    }

    function renderArchiveTable(filterType) {
        const thead = document.getElementById('archive-thead');
        const tbody = document.getElementById('archive-body');
        tbody.innerHTML = '';
        
        if (filterType === 'blackout') {
            thead.innerHTML = '<tr><th>Дата</th><th>Отключение</th><th>Включение</th><th>Длительность</th><th>Сохранность продуктов</th></tr>';
            if (allBlackoutsData.length === 0) {
                tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color:#94a3b8; padding:30px;">⚡ Отключений электроэнергии пока не зафиксировано (Сеть стабильна).</td></tr>';
                return;
            }
            allBlackoutsData.forEach(b => {
                const row = document.createElement('tr');
                row.setAttribute('style', 'background: rgba(245,158,11,0.08);');
                row.innerHTML = `
                    <td><strong>${escapeHTML(b.date)}</strong></td>
                    <td><span style="color:#ef4444; font-weight:bold;">${escapeHTML(b.start)}</span></td>
                    <td><span style="color:#10b981; font-weight:bold;">${escapeHTML(b.end)}</span></td>
                    <td><span style="color:#f59e0b; font-weight:bold;">${escapeHTML(b.duration_str)}</span></td>
                    <td><span style="color:#cbd5e1;">${escapeHTML(b.safety)}</span></td>
                `;
                tbody.appendChild(row);
            });
            return;
        }

        // Standard Cycles
        thead.innerHTML = '<tr><th>Дата</th><th>Начало работы</th><th>Конец работы</th><th>Работа</th><th>Начало отдыха</th><th>Конец отдыха</th><th>Отдых</th><th>КРВ</th><th>Ср. мощность</th><th>Режим</th></tr>';
        
        const sourceList = fullArchiveCyclesData || allCyclesData;
        let filtered = sourceList;
        if (filterType === 'cooling') {
            filtered = sourceList.filter(c => c.cycle_type === 'cooling');
        } else if (filterType === 'defrost') {
            filtered = sourceList.filter(c => c.cycle_type === 'defrost');
        }

        if (filtered.length === 0) {
            if (isArchiveLoading) {
                tbody.innerHTML = '<tr><td colspan="10" style="text-align:center; color:#38bdf8; padding:30px;">⏳ Загрузка полного архива измерений...</td></tr>';
            } else {
                tbody.innerHTML = '<tr><td colspan="10" style="text-align:center; color:#94a3b8; padding:30px;">В этой категории пока нет записей.</td></tr>';
            }
            return;
        }

        filtered.forEach(c => {
            tbody.appendChild(createCycleRow(c));
        });
    }

    let currentCyclesLimit = 10;

    function setCyclesLimit(limit) {
        currentCyclesLimit = limit;
        document.querySelectorAll('[id^="limit-"]').forEach(b => b.classList.remove('active'));
        const btn = document.getElementById('limit-' + limit);
        if (btn) btn.classList.add('active');
        
        if ((limit === 'all' || Number(limit) > 30) && !fullArchiveCyclesData && !isGhPages) {
            loadFullArchive(() => {
                renderMainCyclesTable();
            });
        } else {
            renderMainCyclesTable();
        }
    }

    function renderMainCyclesTable(overrideList) {
        const tbody = document.getElementById('cycles-body');
        const source = overrideList || (fullArchiveCyclesData || allCyclesData);
        if (!tbody || source.length === 0) return;
        tbody.innerHTML = '';
        const list = (currentCyclesLimit === 'all') ? source : source.slice(0, Number(currentCyclesLimit) || 10);
        list.forEach(c => {
            tbody.appendChild(createCycleRow(c));
        });
    }

    
    function updateCyclesTable() {
        if (isGhPages) {
            handleHistoryPayload(mockHistoryData);
            return;
        }
        fetch('/api/history?limit=30')
            .then(r => r.json())
            .then(data => {
                handleHistoryPayload(data);
            })
            .catch(console.error);
    }

    function handleHistoryPayload(data) {
        allCyclesData = data.cycles || [];
        allBlackoutsData = data.blackouts || [];
        
        if (data.summary && data.summary.overall_krv) {
            const badge = document.getElementById('krv-badge');
            if (badge) badge.innerText = `КРВ: ${escapeHTML(data.summary.overall_krv)} (${escapeHTML(data.summary.krv_status)})`;
        }

        const countEl = document.getElementById('total-cycles-btn');
        if (countEl) {
            const totalCount = (data.summary && data.summary.total_cycles !== undefined) ? data.summary.total_cycles : (fullArchiveCyclesData ? fullArchiveCyclesData.length : allCyclesData.length);
            countEl.innerText = totalCount;
        }

        // Prepend latest live cycle to fullArchiveCyclesData if loaded
        if (fullArchiveCyclesData && allCyclesData.length > 0) {
            if (allCyclesData[0] && allCyclesData[0].live) {
                if (fullArchiveCyclesData[0] && fullArchiveCyclesData[0].live) {
                    fullArchiveCyclesData[0] = allCyclesData[0];
                } else {
                    fullArchiveCyclesData.unshift(allCyclesData[0]);
                }
            }
        }

        renderMainCyclesTable();
        
        const archModal = document.getElementById('archive-modal');
        if (archModal && archModal.style.display === 'flex') {
            renderArchiveTable(currentArchiveFilter);
        }
    }

    function openAboutModal(tabId) {
        const modal = document.getElementById('about-modal');
        if (modal) {
            modal.style.display = 'flex';
            const targetTab = tabId || 'tab-about';
            const btnKey = targetTab.replace('tab-', '');
            const targetBtn = document.getElementById('tab-btn-' + btnKey);
            if (targetBtn) {
                switchAboutTab(targetTab, targetBtn);
            } else {
                const firstTabBtn = document.getElementById('tab-btn-about');
                if (firstTabBtn) switchAboutTab('tab-about', firstTabBtn);
            }
        }
    }

    function closeAboutModal() {
        const modal = document.getElementById('about-modal');
        if (modal) modal.style.display = 'none';
    }

    function switchAboutTab(tabId, btn) {
        const tabs = ['tab-about', 'tab-faq', 'tab-changelog', 'tab-specs'];
        tabs.forEach(function(t) {
            const el = document.getElementById(t);
            if (el) el.style.display = (t === tabId) ? 'block' : 'none';
        });
        const buttons = document.querySelectorAll('.about-tab-btn');
        buttons.forEach(function(b) {
            b.classList.remove('active');
        });
        if (btn) btn.classList.add('active');
    }

// Close on Escape key
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            closeAboutModal();
            closeArchiveModal();
            if (typeof closeAnalyticsModal === 'function') closeAnalyticsModal();
            closeSettings();
            closeMobileDrawer();
        }
    });

    setInterval(pollStatus, 3000);
    setInterval(tickLiveTimer, 1000);
    updateSoundButtonUI();
    pollStatus();
    updateCyclesTable();
