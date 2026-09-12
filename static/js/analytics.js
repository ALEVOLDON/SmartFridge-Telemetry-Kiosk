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

    function loadAnalytics(forceRefresh) {
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
                console.error('Ошибка загрузки аналитики:', err);
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

        const tariffInput = document.getElementById('analytics-tariff-input');
        if (tariffInput && e.tariff) tariffInput.value = e.tariff;
        const currSelect = document.getElementById('analytics-currency-select');
        if (currSelect && e.currency) currSelect.value = e.currency;

        renderAnalyticsCharts(data);
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
