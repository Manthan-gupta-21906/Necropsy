// Necropsy Web UI — Séance with the Ghost
// Communicates with the FastAPI backend at http://127.0.0.1:8000

(function() {
    'use strict';

    // --- Configuration ---
    const API_BASE = 'http://127.0.0.1:8000';

    // DOM elements
    const main = document.getElementById('main-content');
    const machineStatus = document.getElementById('machine-status');
    const statusModel = document.getElementById('status-model');
    const statusOllama = document.getElementById('status-ollama');
    const statusEmbeddings = document.getElementById('status-embeddings');
    const statusLatency = document.getElementById('status-latency');

    // Exhume section
    const exhumeSection = document.getElementById('exhume-section');
    const repoSource = document.getElementById('repo-source');
    const btnExhume = document.getElementById('btn-exhume');
    const exhumeProgress = document.getElementById('exhume-progress');
    const exhumeBar = exhumeProgress ? exhumeProgress.querySelector('.progress-bar') : null;
    const exhumeMessage = document.getElementById('exhume-message');

    // Autopsy section
    const autopsySection = document.getElementById('autopsy-section');
    const statsGrid = document.getElementById('stats-grid');

    // Séance section
    const seanceSection = document.getElementById('seance-section');
    const dateSlider = document.getElementById('date-slider');
    const dateDisplay = document.getElementById('date-display');
    const questionInput = document.getElementById('question-input');
    const btnSeance = document.getElementById('btn-seance');
    const seanceResult = document.getElementById('seance-result');
    const answerText = document.getElementById('answer-text');
    const claimsList = document.getElementById('claims-list');
    const noAnswer = document.getElementById('no-answer');

    // Graveyard section
    const graveyardSection = document.getElementById('graveyard-section');
    const tombstonesList = document.getElementById('tombstones-list');

    // State
    let currentAsOf = new Date().getFullYear();

    // --- Machine Status Polling ---
    function pollMachineStatus() {
        fetch(`${API_BASE}/health`)
            .then(r => r.json())
            .then(data => {
                const modelName = data.model || 'none';
                statusModel.textContent = `Model: ${modelName}`;
                statusOllama.textContent = `Ollama: ${data.ollama_reachable ? 'reachable' : 'not reachable'}`;
                statusEmbeddings.textContent = `Embeddings: ${data.embeddings_loaded ? 'loaded' : 'not loaded'}`;
                statusLatency.textContent = data.last_answer_latency_ms !== null
                    ? `Last answer: ${data.last_answer_latency_ms}ms`
                    : 'Last answer: —';

                // Update overall status class
                if (!data.ollama_reachable) {
                    machineStatus.className = 'panel error';
                } else if (data.status === 'ok') {
                    machineStatus.className = 'panel';
                } else {
                    machineStatus.className = 'panel error';
                }
            })
            .catch(() => {
                statusOllama.textContent = 'Ollama: not reachable';
                machineStatus.className = 'panel error';
            });
    }

    // Poll every 3 seconds
    setInterval(pollMachineStatus, 3000);
    pollMachineStatus(); // immediate

    // --- Exhume: Index a Repository ---
    function handleExhume() {
        const source = repoSource.value.trim();
        if (!source) return;

        btnExhume.disabled = true;
        btnExhume.textContent = 'Indexing...';
        exhumeMessage.textContent = '';
        exhumeProgress.classList.remove('hidden');
        if (exhumeBar) exhumeBar.style.width = '0%';

        fetch(`${API_BASE}/exhume`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source })
        })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'ready') {
                exhumeMessage.textContent = data.message || 'Repository indexed successfully.';
                exhumeMessage.className = 'message success';
                // Auto-switch to autopsy section
                setTimeout(() => {
                    exhumeSection.hidden = true;
                    autopsySection.hidden = false;
                    loadAutopsyStats();
                }, 1500);
            } else {
                exhumeMessage.textContent = data.message || 'Error indexing repository.';
                exhumeMessage.className = 'message error';
            }
        })
        .catch(err => {
            exhumeMessage.textContent = `Network error: ${err.message}`;
            exhumeMessage.className = 'message error';
        })
        .finally(() => {
            btnExhume.disabled = false;
            btnExhume.textContent = 'Index Repository';
            exhumeProgress.classList.add('hidden');
        });
    }

    btnExhume.addEventListener('click', handleExhume);

    // --- Autopsy: Load Stats ---
    function loadAutopsyStats() {
        // We need a repo_id; try to get it from the exhume response or list repos
        fetch(`${API_BASE}/repos`)
            .then(r => r.json())
            .then(repos => {
                if (!repos || !repos.length) {
                    statsGrid.innerHTML = '<p>No indexed repositories.</p>';
                    return;
                }
                // Use the most recently indexed repo
                const repoId = repos[repos.length - 1].id;
                return fetch(`${API_BASE}/autopsy`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ repo_id: repoId, as_of: currentAsOf })
                });
            })
            .then(r => r.json())
            .then(data => {
                if (!data) return;
                renderAutopsyStats(data);
            })
            .catch(() => {
                statsGrid.innerHTML = '<p>Error loading autopsy stats.</p>';
            });
    }

    function renderAutopsyStats(stats) {
        const {
            time_of_death,
            last_gasp_commit,
            weekly_activity,
            bus_factor,
            eras
        } = stats;

        let html = '';

        // Time of death
        html += `
            <div class="stat-item">
                <div class="stat-label">Time of death</div>
                <div class="stat-value">${time_of_death || '—'}</div>
            </div>
        `;

        // Last gasp commit
        html += `
            <div class="stat-item">
                <div class="stat-label">Last gasp commit</div>
                <div class="stat-value">${last_gasp_commit || '—'}</div>
            </div>
        `;

        // Weekly activity
        if (weekly_activity && weekly_activity.length) {
            html += `
                <div class="stat-item">
                    <div class="stat-label">Weekly activity</div>
                    <div class="stat-value">
                        <ul style="margin:0;padding-left:1rem;">
                            ${weekly_activity.map(w => `<li>${w.week}: ${w.commits} commits</li>`).join('')}
                        </ul>
                    </div>
                </div>
            `;
        }

        // Bus factor
        if (bus_factor) {
            html += `
                <div class="stat-item">
                    <div class="stat-label">Bus factor</div>
                    <div class="stat-value">
                        <p>Top author churn: ${bus_factor.top_author_churn !== undefined ? bus_factor.top_author_churn : '—'}</p>
                        ${bus_factor.top_author_id !== undefined ? `<p>Top author ID: ${bus_factor.top_author_id}</p>` : ''}
                    </div>
                </div>
            `;
        }

        // Eras (display only)
        if (eras && eras.length) {
            html += `
                <div class="stat-item">
                    <div class="stat-label">Project eras</div>
                    <div class="stat-value">
                        <p>${eras.map(e => `${e.label} (${e.start_at} – ${e.end_at})`).join('; ')}</p>
                    </div>
                </div>
            `;
        }

        if (html === '') {
            html = '<p>No analytics available for this date.</p>';
        }

        statsGrid.innerHTML = html;
    }

    // --- Séance: Time Slider ---
    function initDateSlider() {
        // Set min/max based on reasonable range
        dateSlider.min = 1900;
        dateSlider.max = 2026;
        dateSlider.value = currentAsOf;
        dateDisplay.textContent = currentAsOf;

        // Update display on slider change
        dateSlider.addEventListener('input', (e) => {
            currentAsOf = parseInt(e.target.value);
            dateDisplay.textContent = currentAsOf;

            // If we have a repo indexed, refresh séance and autopsy
            // We'll just update the displayed date; the next séance will use this date
        });
    }

    // --- Séance: Question the Ghost ---
    function handleSéance() {
        const question = questionInput.value.trim();
        if (!question) return;

        btnSeance.disabled = true;
        btnSeance.textContent = 'Thinking...';
        seanceResult.classList.add('hidden');
        noAnswer.classList.add('hidden');

        fetch(`${API_BASE}/seance`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                repo_id: getRepoId(),
                as_of: `${currentAsOf}-12-31T23:59:59Z`, // end of year UTC
                question
            })
        })
        .then(r => r.json())
        .then(data => {
            btnSeance.disabled = false;
            btnSeance.textContent = 'Question the Ghost';

            if (data answer is undefined) {
                // Something went wrong
                answerText.textContent = 'Error communicating with the ghost.';
                seanceResult.classList.remove('hidden');
                return;
            }

            // Display answer
            answerText.textContent = data.answer || '"The dead do not remember that."';

            // Display claims
            claimsList.innerHTML = '';
            if (data.claims && data.claims.length) {
                data.claims.forEach(c => {
                    const li = document.createElement('div');
                    li.className = 'claim';
                    li.innerHTML = `
                        <span class="claim-text">${escapeHtml(c.claim)}</span>
                        <span class="hash">Commit ${c.commit_hash ? c.commit_hash.substring(0,8) + '…' : '—'}</span>
                        <span class="support">${escapeHtml(c.support)}</span>
                    `;
                    claimsList.appendChild(li);
                });
            } else {
                // No surviving claims
                const li = document.createElement('div');
                li.className = 'claim';
                li.textContent = '"The dead do not remember that."';
                claimsList.appendChild(li);
            }

            seanceResult.classList.remove('hidden');
        })
        .catch(err => {
            btnSeance.disabled = false;
            btnSeance.textContent = 'Question the Ghost';
            console.error('Séance error:', err);
            answerText.textContent = `Network error: ${err.message}`;
            seanceResult.classList.remove('hidden');
        });
    }

    function getRepoId() {
        // Try to get from localStorage or use a default
        const saved = localStorage.getItem('necropsy_last_repo_id');
        return saved || 1;
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // --- Graveyard: Tombstones ---
    function loadGraveyard(repoId, asOf) {
        fetch(`${API_BASE}/repos`)
            .then(r => r.json())
            .then(repos => {
                if (!repos || !repos.length) return;
                const id = repos[repos.length - 1].id;
                return fetch(`${API_BASE}/autopsy`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ repo_id: id, as_of: `${currentAsOf}-12-31T23:59:59Z` })
                });
            })
            .then(r => r.json())
            .then(data => {
                // Autopsy doesn't return tombstones directly; we'd need a separate endpoint
                // For now, hide graveyard or show placeholder
                tombstonesList.innerHTML = '<p>Tombstones available after séance with deletion evidence.</p>';
            })
            .catch(() => {
                tombstonesList.innerHTML = '<p>Error loading graveyard.</p>';
            });
    }

    // --- Initialization ---
    function init() {
        initDateSlider();

        // Form submissions
        btnSeance.addEventListener('click', handleSéance);

        // Allow pressing Enter in the question textarea
        questionInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                handleSéance();
            }
        });

        // Initial status poll
        pollMachineStatus();

        // Load autopsy stats for the initial date
        setTimeout(loadAutopsyStats, 500);
    }

    // Start when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();