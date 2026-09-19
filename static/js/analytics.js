// Analytics Modal Handling
    let analyticsData = null;
    let dailyChartInstance = null;
    let hourlyChartInstance = null;

    function openAnalyticsModal() {
        const modal = document.getElementById('analytics-modal');
        if (modal) modal.style.display = 'flex';
        loadAnalytics();
    }

    function closeAnalyticsModal() {
        const modal = document.getElementById('analytics-modal');
        if (modal) modal.style.display = 'none';
    }

    const isGhPagesAnalytics = window.location.hostname.includes('github.io') || window.location.protocol === 'file:' || window.location.search.includes('demo=1');

    const mockAnalyticsPayload = {
        health_score: 95,
        energy: {
            daily_avg_kwh: 0.94,
            monthly_forecast_kwh: 28.2,
            monthly_cost: 141,
            currency: '₽',
            daily_cost: 4.7,
            defrost_share_pct: 18.5,
            total_kwh: 16.0,
            days_monitored: 17,
            tariff: 5.0,
            reconciliation: {
                date: "19.09.2026",
                accuracy_pct: 99.4,
                local_kwh: 0.942,
                cloud_kwh: 0.948,
                delta_kwh: 0.006,
                reconciled_at: "2026-09-19T03:00:00"
            }
        },
        voltage_health: {
            red_hours: 0,
            norm_pct: 99.8
        },
        defrost_health: {
            avg_duration_min: 24,
            total_count: 8
        },
        daily_timeline: [
            { date_short: "13.09", kwh: 0.92, krv: 0.32 },
            { date_short: "14.09", kwh: 0.96, krv: 0.35 },
            { date_short: "15.09", kwh: 0.91, krv: 0.31 },
            { date_short: "16.09", kwh: 0.98, krv: 0.36 },
            { date_short: "17.09", kwh: 0.93, krv: 0.33 },
            { date_short: "18.09", kwh: 0.95, krv: 0.34 },
            { date_short: "19.09", kwh: 0.94, krv: 0.34 }
        ],
        hourly_profile: [
            { hour: "00", duty_pct: 30, avg_voltage: 221 },
            { hour: "02", duty_pct: 28, avg_voltage: 223 },
            { hour: "04", duty_pct: 32, avg_voltage: 224 },
            { hour: "06", duty_pct: 35, avg_voltage: 222 },
            { hour: "08", duty_pct: 55, avg_voltage: 219 },
            { hour: "10", duty_pct: 42, avg_voltage: 218 },
            { hour: "12", duty_pct: 45, avg_voltage: 217 },
            { hour: "14", duty_pct: 38, avg_voltage: 218 },
            { hour: "16", duty_pct: 40, avg_voltage: 216 },
            { hour: "18", duty_pct: 50, avg_voltage: 215 },
            { hour: "20", duty_pct: 48, avg_voltage: 217 },
            { hour: "22", duty_pct: 35, avg_voltage: 220 }
        ]
    };

    function loadAnalytics(forceRefresh) {
        if (isGhPagesAnalytics) {
            renderAnalytics(mockAnalyticsPayload);
            return;
        }
        const tariffInput = document.getElementById('analytics-tariff-input');
        const currencySelect = document.getElementById('analytics-currency-select');
        let url = '/api/analytics';
        const params = [];
        if (forceRefresh) params.push('refresh=1');
        if (tariffInput && tariffInput.value) params.push('tariff=' + encodeURIComponent(tariffInput.value));
        if (currencySelect && currencySelect.value) params.push('currency=' + encodeURIComponent(currencySelect.value));
        if (params.length > 0) url += '?' + params.join('&');

        fetch(url)
            .then(r => r.json())
            .then(data => {
                renderAnalytics(data);
            })
            .catch(err => {
                console.error('Ошибка загрузки аналитики, переключение на демо:', err);
                renderAnalytics(mockAnalyticsPayload);
            });
    }

    function saveAndApplyTariff() {
        const tariffInput = document.getElementById('analytics-tariff-input');
        const currencySelect = document.getElementById('analytics-currency-select');
        const tariff = tariffInput ? parseFloat(tariffInput.value) : 5.0;
        const currency = currencySelect ? currencySelect.value : '₽';

        fetch('/api/tariff', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tariff: tariff, currency: currency })
        })
        .then(r => r.json())
        .then(() => {
            loadAnalytics(true);
        })
        .catch(() => {
            loadAnalytics(true);
        });
    }

    function renderAnalytics(data) {
        if (!data || data.error) return;
        analyticsData = data;

        const e = data.energy || {};
        const v = data.voltage_health || {};
        const df = data.defrost_health || {};

        const setText = (id, val) => {
            const el = document.getElementById(id);
            if (el) el.innerText = val;
        };

        setText('analytics-health-badge', `Индекс: ${data.health_score || 88}/100`);
        setText('an-daily-kwh', e.daily_avg_kwh !== undefined ? e.daily_avg_kwh : '—');
        setText('an-monthly-kwh', `${e.monthly_forecast_kwh !== undefined ? e.monthly_forecast_kwh : '—'} кВт·ч`);
        setText('an-monthly-cost', `${Math.round(e.monthly_cost || 0)}`);
        setText('an-currency', e.currency || '₽');
        setText('an-daily-cost', `${e.daily_cost || '—'} ${e.currency || '₽'}`);
        setText('an-defrost-share', e.defrost_share_pct !== undefined ? e.defrost_share_pct : '—');
        setText('an-compressor-share', `${(100 - (e.defrost_share_pct || 0)).toFixed(1)}%`);
        setText('an-red-hours', v.red_hours !== undefined ? v.red_hours : '0');
        setText('an-norm-pct', `${v.norm_pct !== undefined ? v.norm_pct : 100}%`);
        setText('an-defrost-dur', df.avg_duration_min ? Math.round(df.avg_duration_min) : '25');
        setText('an-defrost-count', df.total_count || '0');
        setText('an-total-kwh', e.total_kwh !== undefined ? e.total_kwh : '—');
        setText('an-days-count', e.days_monitored || '17');

        const rec = e.reconciliation || {};
        if (rec && rec.date) {
            const acc = rec.accuracy_pct !== undefined ? rec.accuracy_pct : 99.2;
            setText('an-reconcile-badge', `${acc.toFixed(1)}% точности`);
            setText('an-rec-local', `${rec.local_kwh !== undefined ? rec.local_kwh : '--'}`);
            setText('an-rec-cloud', `${rec.cloud_kwh !== undefined ? rec.cloud_kwh : '--'}`);
            setText('an-rec-delta', `${rec.delta_kwh !== undefined ? rec.delta_kwh : '--'}`);
            if (rec.reconciled_at) {
                const recTime = rec.reconciled_at.substring(11, 16);
                setText('an-rec-time', `Сверено в ${recTime}`);
            }
        }

        const tariffInput = document.getElementById('analytics-tariff-input');
        if (tariffInput && e.tariff) tariffInput.value = e.tariff;
        const currSelect = document.getElementById('analytics-currency-select');
        if (currSelect && e.currency) currSelect.value = e.currency;

        renderAnalyticsCharts(data);
    }

    function triggerEnergyReconciliation() {
        const btn = document.getElementById('an-rec-btn');
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = '<span>⏳</span> Сверка...';
        }
        if (isGhPagesAnalytics) {
            setTimeout(function() {
                if (btn) {
                    btn.disabled = false;
                    btn.innerHTML = '<span>🔄</span> Сверить сейчас';
                }
                loadAnalytics(true);
            }, 600);
            return;
        }
        fetch('/api/reconcile-energy', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        })
        .then(function(r) { return r.json(); })
        .then(function(res) {
            if (res.success || res.date) {
                loadAnalytics(true);
            } else {
                alert(res.error || 'Ошибка сверки');
            }
        })
        .catch(function(err) {
            console.error(err);
            alert('Ошибка запроса: ' + err);
        })
        .then(function() {
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = '<span>🔄</span> Сверить сейчас';
            }
        });
    }

    function renderAnalyticsCharts(data) {
        if (typeof Chart === 'undefined') return;

        // 1. Daily Trend Chart
        const dailyCanvas = document.getElementById('analytics-daily-chart');
        if (dailyCanvas && data.daily_timeline && data.daily_timeline.length > 0) {
            if (dailyChartInstance) dailyChartInstance.destroy();
            const labels = data.daily_timeline.map(d => d.date_short);
            const kwhData = data.daily_timeline.map(d => d.kwh);
            const krvData = data.daily_timeline.map(d => d.krv);

            dailyChartInstance = new Chart(dailyCanvas.getContext('2d'), {
                type: 'bar',
                data: {
                    labels: labels,
                    datasets: [
                        {
                            label: 'Расход (кВт·ч)',
                            data: kwhData,
                            backgroundColor: 'rgba(56, 189, 248, 0.65)',
                            borderColor: '#38bdf8',
                            borderWidth: 1,
                            borderRadius: 6,
                            yAxisID: 'y'
                        },
                        {
                            label: 'КРВ',
                            data: krvData,
                            type: 'line',
                            borderColor: '#10b981',
                            backgroundColor: 'rgba(16, 185, 129, 0.2)',
                            borderWidth: 2.5,
                            pointRadius: 4,
                            pointBackgroundColor: '#10b981',
                            tension: 0.3,
                            yAxisID: 'y1'
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: { labels: { color: '#cbd5e1', font: { size: 11.5, weight: '600' } } }
                    },
                    scales: {
                        x: { ticks: { color: '#94a3b8' }, grid: { color: 'rgba(255,255,255,0.05)' } },
                        y: {
                            type: 'linear',
                            position: 'left',
                            beginAtZero: true,
                            ticks: { color: '#38bdf8' },
                            title: { display: true, text: 'кВт·ч', color: '#38bdf8' },
                            grid: { color: 'rgba(255,255,255,0.05)' }
                        },
                        y1: {
                            type: 'linear',
                            position: 'right',
                            beginAtZero: true,
                            max: 0.8,
                            ticks: { color: '#10b981' },
                            title: { display: true, text: 'КРВ', color: '#10b981' },
                            grid: { drawOnChartArea: false }
                        }
                    }
                }
            });
        }

        // 2. Hourly Profile Chart
        const hourlyCanvas = document.getElementById('analytics-hourly-chart');
        if (hourlyCanvas && data.hourly_profile && data.hourly_profile.length > 0) {
            if (hourlyChartInstance) hourlyChartInstance.destroy();
            const hLabels = data.hourly_profile.map(h => h.hour);
            const dutyData = data.hourly_profile.map(h => h.duty_pct);
            const voltData = data.hourly_profile.map(h => h.avg_voltage);

            hourlyChartInstance = new Chart(hourlyCanvas.getContext('2d'), {
                type: 'bar',
                data: {
                    labels: hLabels,
                    datasets: [
                        {
                            label: 'Активность (%)',
                            data: dutyData,
                            backgroundColor: 'rgba(6, 182, 212, 0.55)',
                            borderColor: '#06b6d4',
                            borderWidth: 1,
                            borderRadius: 4,
                            yAxisID: 'y'
                        },
                        {
                            label: 'Напряжение (В)',
                            data: voltData,
                            type: 'line',
                            borderColor: '#f59e0b',
                            backgroundColor: 'rgba(245, 158, 11, 0.1)',
                            borderWidth: 2,
                            pointRadius: 3,
                            pointBackgroundColor: '#f59e0b',
                            tension: 0.25,
                            yAxisID: 'y1'
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: { labels: { color: '#cbd5e1', font: { size: 11.5, weight: '600' } } }
                    },
                    scales: {
                        x: { ticks: { color: '#94a3b8', maxRotation: 45, minRotation: 45 }, grid: { color: 'rgba(255,255,255,0.05)' } },
                        y: {
                            type: 'linear',
                            position: 'left',
                            beginAtZero: true,
                            max: 100,
                            ticks: { color: '#06b6d4' },
                            title: { display: true, text: '% работы', color: '#06b6d4' },
                            grid: { color: 'rgba(255,255,255,0.05)' }
                        },
                        y1: {
                            type: 'linear',
                            position: 'right',
                            min: 170,
                            max: 235,
                            ticks: { color: '#f59e0b' },
                            title: { display: true, text: 'Вольт (В)', color: '#f59e0b' },
                            grid: { drawOnChartArea: false }
                        }
                    }
                }
            });
        }
    }
