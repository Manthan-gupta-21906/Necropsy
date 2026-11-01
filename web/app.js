// Necropsy web UI. Static, no build step, no CDN.
// Every request has a timeout; every failure has a human message and a retry.
(function () {
    'use strict';

    var API_BASE = (function () {
        var q = new URLSearchParams(window.location.search).get('api');
        if (q) { return q.replace(/\/$/, ''); }
        if (/^https?:$/.test(window.location.protocol)) { return window.location.origin; }
        return 'http://127.0.0.1:8000';
    })();

    var TIMEOUTS = { health: 15000, repos: 10000, exhume: 15000, job: 10000, autopsy: 20000, seance: 320000 };
    var HEALTH_POLL_MS = 5000, SLIDER_DEBOUNCE_MS = 300, HEALTH_FAILURE_LIMIT = 3;

    // ---- DOM helpers -----------------------------------------------------
    function el(id) { return document.getElementById(id); }
    function setHidden(n, h) { if (!n) { return; } if (h) { n.setAttribute('hidden', ''); } else { n.removeAttribute('hidden'); } }
    function setText(n, t) { if (n) { n.textContent = t == null ? '' : String(t); } }
    function clear(n) { if (!n) { return; } while (n.firstChild) { n.removeChild(n.firstChild); } }
    function make(tag, cls, text) {
        var n = document.createElement(tag);
        if (cls) { n.className = cls; }
        if (text != null) { n.textContent = String(text); }
        return n;
    }
    function addRetry(box, label, handler) {
        var b = make('button', 'btn btn-ghost', label || 'Retry');
        b.type = 'button';
        b.addEventListener('click', handler);
        box.appendChild(document.createTextNode(' '));
        box.appendChild(b);
    }

    // ---- Formatting ------------------------------------------------------
    var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    function parseIso(v) {
        var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(v || ''));
        return m ? { y: +m[1], m: +m[2], d: +m[3] } : null;
    }
    function dayNumber(v) { var p = parseIso(v); return p ? Math.floor(Date.UTC(p.y, p.m - 1, p.d) / 86400000) : null; }
    function dateFromDay(day) {
        var d = new Date(Number(day) * 86400000);
        return d.getUTCFullYear() + '-' + String(d.getUTCMonth() + 1).padStart(2, '0') + '-' + String(d.getUTCDate()).padStart(2, '0');
    }
    function prettyDate(v) { var p = parseIso(v); return p ? p.d + ' ' + MONTHS[p.m - 1] + ' ' + p.y : '—'; }
    function prettyMonth(v) { var p = parseIso(v); return p ? MONTHS[p.m - 1] + ' ' + String(p.y).slice(2) : String(v || '—'); }
    function prettyMs(ms) {
        if (typeof ms !== 'number' || isNaN(ms)) { return '—'; }
        return ms < 1000 ? ms + ' ms' : (ms / 1000).toFixed(1) + ' s';
    }

    // ---- HTTP with a hard timeout ---------------------------------------
    function ApiError(message, status, payload) {
        this.name = 'ApiError'; this.message = message; this.status = status || 0; this.payload = payload || null;
    }
    ApiError.prototype = Object.create(Error.prototype);

    function describeError(p, status) {
        if (p && typeof p.detail === 'string') { return p.detail; }
        if (p && p.detail && typeof p.detail.message === 'string') { return p.detail.message; }
        if (p && typeof p.message === 'string') { return p.message; }
        if (status === 404) { return 'Not found (HTTP 404).'; }
        if (status === 422) { return 'The server rejected the request (HTTP 422).'; }
        if (status >= 500) { return 'The server failed with HTTP ' + status + '.'; }
        return 'Request failed with HTTP ' + status + '.';
    }

    function api(path, options, timeoutMs) {
        var opts = options || {}, limit = timeoutMs || 15000;
        var controller = new AbortController();
        var timer = setTimeout(function () { controller.abort(); }, limit);
        if (opts.signal) {
            if (opts.signal.aborted) { controller.abort(); }
            else { opts.signal.addEventListener('abort', function () { controller.abort(); }); }
        }
        var init = { method: opts.method || 'GET', headers: opts.headers || {}, signal: controller.signal };
        if (opts.body !== undefined) {
            init.headers = Object.assign({ 'Content-Type': 'application/json' }, init.headers);
            init.body = JSON.stringify(opts.body);
        }
        return fetch(API_BASE + path, init)
            .then(function (res) {
                return res.text().then(function (text) {
                    var data = null;
                    if (text) { try { data = JSON.parse(text); } catch (e) { data = null; } }
                    if (!res.ok) { throw new ApiError(describeError(data, res.status), res.status, data); }
                    if (text && data === null) { throw new ApiError('The server returned a malformed response.', res.status, null); }
                    return data;
                });
            })
            .catch(function (err) {
                if (err && err.name === 'AbortError') {
                    var cancelled = !!(opts.signal && opts.signal.aborted);
                    var w = new ApiError(cancelled ? 'Cancelled.' :
                        'No response within ' + Math.round(limit / 1000) + ' s. The server may still be working — try again.', 0, null);
                    w.cancelled = cancelled;
                    throw w;
                }
                if (err instanceof ApiError) { throw err; }
                throw new ApiError('Cannot reach the Necropsy server at ' + API_BASE + '. Is it running? (' +
                    (err && err.message ? err.message : 'network error') + ')', 0, null);
            })
            .finally(function () { clearTimeout(timer); });
    }

    // ---- State -----------------------------------------------------------
    var state = {
        repo: null, asOf: null, health: null, healthTimer: null, healthFailures: 0, healthPaused: false,
        sliderTimer: null, indexing: false, seanceBusy: false, seanceController: null,
        seanceTimer: null, seanceStartedAt: 0
    };

    // ---- Machine status --------------------------------------------------
    function renderHealth(health) {
        state.health = health;
        state.healthFailures = 0;
        el('machine-status').classList.remove('is-stale');

        var pill = el('status-pill'), note = el('status-note');
        var ollama = health.ollama || {}, embeddings = health.embeddings || {}, warmup = ollama.warmup || {};
        var busy = health.busy === true, status = health.status || 'unknown';
        var pillText = status, pillClass = 'pill pill-unknown';

        if (status === 'ok') { pillClass = 'pill pill-ok'; }
        else if (status === 'warming') { pillClass = 'pill pill-warn'; }
        else if (status === 'busy') { pillClass = 'pill pill-busy'; pillText = 'busy answering'; }
        else if (status === 'error') { pillClass = 'pill pill-error'; }
        setText(pill, pillText);
        pill.className = pillClass;

        setText(el('live-status'), (ollama.model || 'no model') + ' · ollama ' +
            (ollama.reachable ? 'up' : 'down') + ' · embeddings ' + (embeddings.state || 'unknown'));

        var modelEl = el('status-model');
        if (ollama.model) {
            setText(modelEl, ollama.model + (ollama.model_installed ? ' (installed)' : ' (not installed)'));
            modelEl.className = 'status-value ' + (ollama.model_installed ? 'is-ok' : 'is-error');
        } else {
            setText(modelEl, 'unknown');
            modelEl.className = 'status-value is-error';
        }

        var ollamaEl = el('status-ollama');
        if (busy) { setText(ollamaEl, 'busy answering · generating now'); ollamaEl.className = 'status-value is-warn'; }
        else if (ollama.reachable && ollama.model_installed) { setText(ollamaEl, 'reachable · model ready'); ollamaEl.className = 'status-value is-ok'; }
        else if (warmup.state === 'warming') { setText(ollamaEl, 'loading the model into memory…'); ollamaEl.className = 'status-value is-warn'; }
        else if (ollama.reachable) { setText(ollamaEl, 'reachable · model missing'); ollamaEl.className = 'status-value is-warn'; }
        else { setText(ollamaEl, 'unreachable'); ollamaEl.className = 'status-value is-error'; }

        var embEl = el('status-embeddings'), embState = embeddings.state || 'unknown';
        if (embState === 'ready') { setText(embEl, 'ready · ' + (embeddings.model || 'local model')); embEl.className = 'status-value is-ok'; }
        else if (embState === 'loading') { setText(embEl, 'loading ' + (embeddings.model || 'model') + '…'); embEl.className = 'status-value is-warn'; }
        else if (embState === 'error') { setText(embEl, 'error'); embEl.className = 'status-value is-error'; }
        else { setText(embEl, embState); embEl.className = 'status-value is-warn'; }

        setText(el('status-latency'), prettyMs(health.last_answer_ms));

        var messages = [];
        if (busy) { messages.push('A séance is in flight. Status polling is paused until the ghost finishes.'); }
        if (ollama.error) { messages.push(ollama.error); }
        if (embeddings.state === 'error' && embeddings.message) { messages.push('Embeddings failed: ' + embeddings.message); }
        if (ollama.reachable && !ollama.model_installed && ollama.model) { messages.push('Install the model with: ollama pull ' + ollama.model); }
        setText(note, messages.join(' '));
        setHidden(note, !messages.length);
    }

    function renderHealthError(err) {
        state.healthFailures += 1;
        var pill = el('status-pill'), note = el('status-note');
        el('machine-status').classList.add('is-stale');

        if (state.healthFailures < HEALTH_FAILURE_LIMIT) {
            pill.className = 'pill pill-warn';
            setText(pill, 'stale');
            setText(note, 'Status is stale — the last check failed (' + err.message + '). Keeping the last known values; retrying automatically.');
            setHidden(note, false);
            return;
        }
        pill.className = 'pill pill-error';
        setText(pill, 'offline');
        if (!state.health) {
            setText(el('status-model'), 'unknown');
            setText(el('status-ollama'), 'unreachable');
            el('status-ollama').className = 'status-value is-error';
            setText(el('status-embeddings'), 'unknown');
            setText(el('status-latency'), '—');
        }
        setText(note, err.message + ' The last known values are shown dimmed.');
        setHidden(note, false);
    }

    function pollHealth(force) {
        if (state.healthPaused && !force) { return Promise.resolve(null); }
        return api('/health', {}, TIMEOUTS.health).then(renderHealth).catch(renderHealthError);
    }

    function startHealthPolling() {
        pollHealth(true);
        if (state.healthTimer) { clearInterval(state.healthTimer); }
        state.healthTimer = setInterval(pollHealth, HEALTH_POLL_MS);
    }

    function markBusyStatus() {
        el('status-pill').className = 'pill pill-busy';
        setText(el('status-pill'), 'busy answering');
        setText(el('status-ollama'), 'busy answering · generating now');
        el('status-ollama').className = 'status-value is-warn';
        setText(el('status-note'), 'A séance is in flight. Status polling is paused until the ghost finishes.');
        setHidden(el('status-note'), false);
    }

    // ---- Repos and slider ------------------------------------------------
    function setAsOf(dateStr) {
        state.asOf = dateStr;
        setText(el('date-display'), prettyDate(dateStr));
        setText(el('seance-asof'), 'as of ' + prettyDate(dateStr));
        setText(el('autopsy-asof'), 'as of ' + prettyDate(dateStr));
        el('date-slider').setAttribute('aria-valuetext', prettyDate(dateStr));
    }

    function selectRepo(repo) {
        state.repo = repo;
        setText(el('repo-name'), repo.name || ('repo ' + repo.id));
        setHidden(el('timeline-section'), false);
        setHidden(el('autopsy-section'), false);

        var firstDay = dayNumber(repo.first_commit_at), lastDay = dayNumber(repo.last_commit_at);
        var slider = el('date-slider');

        if (firstDay == null || lastDay == null) {
            setText(el('bound-first'), '—');
            setText(el('bound-last'), '—');
            setText(el('date-display'), 'no commits');
            slider.disabled = true;
            setHidden(el('autopsy-section'), true);
            return;
        }
        slider.disabled = false;
        slider.min = String(firstDay);
        slider.max = String(lastDay);

        var requested = new URLSearchParams(window.location.search).get('as_of');
        var requestedDay = requested ? dayNumber(requested) : null;
        var startDay = lastDay;
        if (requestedDay != null && requestedDay >= firstDay && requestedDay <= lastDay) { startDay = requestedDay; }
        slider.value = String(startDay);

        setText(el('bound-first'), prettyDate(repo.first_commit_at));
        setText(el('bound-last'), prettyDate(repo.last_commit_at));
        setAsOf(dateFromDay(startDay));
        loadAutopsy();
    }

    function loadRepos() {
        return api('/repos', {}, TIMEOUTS.repos)
            .then(function (data) {
                var ready = ((data && data.repos) || []).filter(function (r) { return r.status === 'ready' && r.commit_count > 0; });
                if (!ready.length) { setText(el('repo-name'), 'no repo indexed'); return null; }
                var repo = ready[ready.length - 1];
                selectRepo(repo);
                return repo;
            })
            .catch(function (err) {
                var msg = el('exhume-error');
                setText(msg, 'Could not list indexed repositories. ' + err.message);
                setHidden(msg, false);
                return null;
            });
    }

    function onSliderInput() {
        setAsOf(dateFromDay(el('date-slider').value));
        if (state.sliderTimer) { clearTimeout(state.sliderTimer); }
        state.sliderTimer = setTimeout(function () { state.sliderTimer = null; loadAutopsy(); }, SLIDER_DEBOUNCE_MS);
    }

    // ---- Exhume ----------------------------------------------------------
    function validateSource(value) {
        var s = (value || '').trim();
        if (!s) { return 'Enter a repository URL or a local path.'; }
        if (s.charAt(0) === '-') { return 'A source may not start with "-".'; }
        if (/^https:\/\//i.test(s) || /^ssh:\/\//i.test(s)) { return null; }
        if (/^[\w.\-]+@[\w.\-]+:.+$/.test(s)) { return null; }
        if (/[\\/]/.test(s) || /^[A-Za-z]:/.test(s)) { return null; }
        return 'Use an https:// or ssh:// URL, or a local path that contains .git.';
    }

    function setExhumeBusy(busy) {
        state.indexing = busy;
        var btn = el('btn-exhume');
        btn.disabled = busy;
        setText(btn, busy ? 'Indexing…' : 'Index Repository');
        el('repo-source').disabled = busy;
        setHidden(el('exhume-progress'), !busy);
        if (!busy) { el('exhume-bar').style.width = '0%'; }
    }

    function renderJob(job) {
        var pct = Math.max(0, Math.min(100, Math.round((job.progress || 0) * 100)));
        el('exhume-bar').style.width = pct + '%';
        var label = job.stage || 'working';
        if (job.commit_count) { label += ' · ' + job.commit_count + ' commits'; }
        setText(el('exhume-stage'), label + ' · ' + pct + '%');
    }

    function pollJob(jobId) {
        return api('/exhume/' + encodeURIComponent(jobId), {}, TIMEOUTS.job).then(function (job) {
            renderJob(job);
            if (job.status === 'ready') { return job; }
            if (job.status === 'failed') { throw new ApiError(job.error || 'Indexing failed.', 0, job); }
            return new Promise(function (resolve) { setTimeout(resolve, 800); }).then(function () { return pollJob(jobId); });
        });
    }

    function startExhume() {
        if (state.indexing) { return; }
        var input = el('repo-source'), source = input.value.trim();
        var problem = validateSource(source), errEl = el('exhume-error'), msgEl = el('exhume-message');
        setHidden(errEl, true);
        setHidden(msgEl, true);
        if (problem) { setText(errEl, problem); setHidden(errEl, false); input.focus(); return; }

        setExhumeBusy(true);
        setText(el('exhume-stage'), 'queued · 0%');

        api('/exhume', { method: 'POST', body: { source: source } }, TIMEOUTS.exhume)
            .then(function (job) {
                setText(el('exhume-stage'), (job.stage || 'queued') + ' · 0%');
                return pollJob(job.job_id);
            })
            .then(function (job) {
                setExhumeBusy(false);
                setText(msgEl, 'Indexed ' + (job.commit_count || 0) + ' commits and ' + (job.embeddings || 0) +
                    ' embeddings. ' + prettyDate(job.first_commit_at) + ' → ' + prettyDate(job.last_commit_at) + '.');
                setHidden(msgEl, false);
                return api('/repos', {}, TIMEOUTS.repos);
            })
            .then(function (data) {
                var repos = (data && data.repos) || [];
                var match = repos.filter(function (r) { return r.source === source && r.status === 'ready'; })[0] || repos[repos.length - 1];
                if (match) { selectRepo(match); }
            })
            .catch(function (err) {
                setExhumeBusy(false);
                setText(errEl, err.message);
                setHidden(errEl, false);
                addRetry(errEl, 'Try again', function () { setHidden(errEl, true); startExhume(); });
            });
    }

    // ---- Autopsy ---------------------------------------------------------
    function statCard(label, value, sub) {
        var card = make('div', 'stat');
        card.appendChild(make('span', 'stat-label', label));
        var v = make('span', 'stat-value', value);
        if (sub) { v.appendChild(document.createTextNode(' ')); v.appendChild(make('small', '', sub)); }
        card.appendChild(v);
        return card;
    }

    function renderStats(d) {
        var grid = el('stats-grid');
        clear(grid);
        function val(x) { return x == null ? '—' : x; }
        grid.appendChild(statCard('Commits', val(d.commit_count)));
        grid.appendChild(statCard('Authors', val(d.author_count)));
        grid.appendChild(statCard('Additions', val(d.additions)));
        grid.appendChild(statCard('Deletions', val(d.deletions)));
        grid.appendChild(statCard('Last commit', prettyDate(d.last_commit_at)));
        grid.appendChild(statCard('Tombstones', val(d.tombstone_count)));
        if (d.bus_factor) {
            grid.appendChild(statCard('Bus factor', d.bus_factor.top_author || '—', Math.round((d.bus_factor.share || 0) * 100) + '% of commits'));
        } else {
            grid.appendChild(statCard('Bus factor', '—'));
        }
    }

    function renderChart(months) {
        var host = el('activity-chart');
        clear(host);
        if (!months || !months.length) { host.appendChild(make('p', 'empty-note', 'No commits up to this date.')); return; }

        var max = 1;
        months.forEach(function (m) { if (m.commits > max) { max = m.commits; } });
        var barW = 18, gap = 6, width = Math.max(320, months.length * (barW + gap) + 40), height = 150, base = 118;
        var NS = 'http://www.w3.org/2000/svg';
        function node(name, attrs, text) {
            var n = document.createElementNS(NS, name);
            Object.keys(attrs).forEach(function (k) { n.setAttribute(k, String(attrs[k])); });
            if (text != null) { n.textContent = text; }
            return n;
        }
        var svg = node('svg', { viewBox: '0 0 ' + width + ' ' + height, width: width, height: height, role: 'img', 'aria-label': 'Commits per month' });
        svg.appendChild(node('line', { x1: 28, y1: base, x2: width - 8, y2: base, stroke: '#2E2E38', 'stroke-width': 1 }));

        months.forEach(function (m, i) {
            var h = Math.max(2, Math.round((m.commits / max) * 88)), x = 34 + i * (barW + gap), y = base - h;
            var rect = node('rect', { x: x, y: y, width: barW, height: h, fill: '#D97742', rx: 2 });
            rect.appendChild(node('title', {}, prettyMonth(m.month) + ': ' + m.commits + ' commits'));
            svg.appendChild(rect);
            svg.appendChild(node('text', { x: x + barW / 2, y: base + 14, fill: '#8B857A', 'font-size': 9, 'text-anchor': 'middle', 'font-family': 'monospace' }, prettyMonth(m.month)));
            svg.appendChild(node('text', { x: x + barW / 2, y: y - 4, fill: '#B4AEA1', 'font-size': 9, 'text-anchor': 'middle', 'font-family': 'monospace' }, m.commits));
        });
        host.appendChild(svg);
    }

    function renderCommits(list) {
        var host = el('recent-commits');
        clear(host);
        if (!list || !list.length) {
            var li = make('li');
            li.appendChild(make('p', 'empty-note', 'No commits up to this date.'));
            host.appendChild(li);
            return;
        }
        list.forEach(function (c) {
            var li = make('li');
            li.appendChild(make('span', 'commit-hash', String(c.hash || '').slice(0, 10)));
            li.appendChild(make('span', '', c.subject || '(no subject)'));
            li.appendChild(make('span', 'commit-date', prettyDate(c.at)));
            host.appendChild(li);
        });
    }

    function renderTombstones(list) {
        var host = el('tombstones-list');
        clear(host);
        if (!list || !list.length) {
            var li = make('li');
            li.appendChild(make('p', 'empty-note', 'Nothing has died yet at this date.'));
            host.appendChild(li);
            return;
        }
        list.forEach(function (t) {
            var li = make('li');
            li.appendChild(make('span', 'commit-hash', t.path || 'unknown'));
            li.appendChild(make('span', '', t.epitaph || ''));
            li.appendChild(make('span', 'commit-date', prettyDate(t.died_at) + ' · lived ' + (t.lifespan_days || 0) + ' days'));
            host.appendChild(li);
        });
    }

    function loadAutopsy() {
        if (!state.repo || !state.asOf) { return; }
        var repoId = state.repo.id, asOf = state.asOf;
        setHidden(el('autopsy-loading'), false);
        setHidden(el('autopsy-error'), true);
        setHidden(el('autopsy-section'), false);

        api('/autopsy?repo_id=' + encodeURIComponent(repoId) + '&as_of=' + encodeURIComponent(asOf), {}, TIMEOUTS.autopsy)
            .then(function (data) {
                if (state.asOf !== asOf) { return; }
                setHidden(el('autopsy-loading'), true);
                setHidden(el('autopsy-content'), false);
                renderStats(data);
                renderChart(data.monthly_activity);
                renderCommits(data.recent_commits);
                renderTombstones(data.tombstones);
            })
            .catch(function (err) {
                if (state.asOf !== asOf) { return; }
                setHidden(el('autopsy-loading'), true);
                setHidden(el('autopsy-content'), true);
                var box = el('autopsy-error');
                setText(box, 'Could not load the autopsy. ' + err.message);
                setHidden(box, false);
                addRetry(box, 'Retry', function () { setHidden(box, true); loadAutopsy(); });
            });
    }

    // ---- Séance ----------------------------------------------------------
    function renderSeance(data) {
        var answer = el('answer-text'), list = el('claims-list');
        clear(list);
        setText(answer, data.answer || '');
        var claims = data.claims || [];
        var refused = !claims.length || data.answer === 'The dead do not remember that.';
        answer.className = 'answer' + (refused ? ' is-refusal' : '');

        var remembers = el('remembers-text');
        if (data.remembers) { setText(remembers, 'It remembers: ' + data.remembers); setHidden(remembers, false); }
        else { setHidden(remembers, true); }

        claims.forEach(function (claim) {
            var li = make('li'), btn = make('button', 'citation');
            btn.type = 'button';
            btn.setAttribute('aria-expanded', 'false');
            btn.appendChild(make('span', 'claim-text', claim.claim || '(no claim text)'));
            btn.appendChild(make('span', 'commit-line', String(claim.commit_hash || '').slice(0, 12)));
            btn.appendChild(make('span', 'commit-meta', prettyDate(claim.at) + ' · ' + (claim.subject || '(no subject)')));
            var detail = make('span', 'citation-detail', 'Full hash: ' + (claim.commit_hash || '—') +
                (claim.support ? ' · Model support: ' + claim.support : ''));
            setHidden(detail, true);
            btn.appendChild(detail);
            btn.addEventListener('click', function () {
                var open = btn.getAttribute('aria-expanded') === 'true';
                btn.setAttribute('aria-expanded', open ? 'false' : 'true');
                setHidden(detail, open);
            });
            li.appendChild(btn);
            list.appendChild(li);
        });

        if (!claims.length) {
            var empty = make('li');
            empty.appendChild(make('p', 'empty-note', 'No claim survived citation verification, so the ghost refuses to answer.'));
            list.appendChild(empty);
        }

        var evidence = data.evidence || [], usage = data.usage || {};
        setText(el('evidence-note'), 'Verified ' + claims.length + ' of ' + (data.claims_total || 0) +
            ' proposed claims against ' + evidence.length + ' evidence commits · ' + (data.model || 'model') +
            ' · ' + prettyMs(data.latency_ms) + (usage.total_tokens ? ' · ' + usage.total_tokens + ' tokens' : ''));
        setHidden(el('seance-result'), false);
    }

    function updateElapsed() {
        var s = Math.max(0, Math.floor((Date.now() - state.seanceStartedAt) / 1000));
        setText(el('seance-status'), 'The ghost is thinking… ' + s + ' s. Local CPU generation.' +
            (s > 45 ? ' The first answer can take a few minutes while the local model loads.' : ''));
        setHidden(el('seance-status'), false);
    }

    function startElapsed() {
        state.seanceStartedAt = Date.now();
        updateElapsed();
        if (state.seanceTimer) { clearInterval(state.seanceTimer); }
        state.seanceTimer = setInterval(updateElapsed, 1000);
    }

    function stopElapsed() {
        if (state.seanceTimer) { clearInterval(state.seanceTimer); state.seanceTimer = null; }
    }

    function setSeanceBusy(busy) {
        state.seanceBusy = busy;
        var btn = el('btn-seance');
        btn.disabled = busy;
        setText(btn, busy ? 'Consulting…' : 'Question the Ghost');
        setHidden(el('btn-seance-cancel'), !busy);
        if (!busy) { stopElapsed(); }
    }

    function cancelSeance() {
        if (!state.seanceBusy) { return; }
        setText(el('seance-status'), 'Cancelling…');
        if (state.seanceController) { state.seanceController.abort(); }
    }

    function runSeance() {
        if (state.seanceBusy) { return; }
        var input = el('question-input'), question = input.value.trim();
        var errEl = el('seance-error'), statusEl = el('seance-status');
        setHidden(errEl, true);
        setHidden(el('seance-result'), true);

        function fail(msg) { setText(errEl, msg); setHidden(errEl, false); }
        if (!state.repo) { fail('Index a repository first, then ask the ghost.'); return; }
        if (!state.asOf) { fail('Choose a date on the time slider first.'); return; }
        if (!question) { fail('Type a question for the ghost.'); input.focus(); return; }

        state.seanceController = new AbortController();
        var controller = state.seanceController;
        setSeanceBusy(true);
        startElapsed();
        markBusyStatus();
        state.healthPaused = true;

        var payload = { repo_id: state.repo.id, as_of: state.asOf, question: question };
        api('/seance', { method: 'POST', body: payload, signal: controller.signal }, TIMEOUTS.seance)
            .then(function (data) { setHidden(statusEl, true); renderSeance(data); })
            .catch(function (err) {
                setHidden(statusEl, true);
                if (err && err.cancelled) {
                    fail('Cancelled. The model may still finish the abandoned request in the background, but its answer is discarded.');
                    return;
                }
                fail(err.message);
                addRetry(errEl, 'Ask again', function () { setHidden(errEl, true); runSeance(); });
            })
            .finally(function () {
                setSeanceBusy(false);
                state.seanceController = null;
                state.healthPaused = false;
                pollHealth(true);
            });
    }

    // ---- Routing ---------------------------------------------------------
    function route() {
        var hash = window.location.hash || '';
        var path = hash.split('?')[0];
        var wantsApp = !!new URLSearchParams(window.location.search).get('as_of');

        // In-page anchors are not routes: leave the current view alone.
        if (path && path.indexOf('#/') !== 0) { return; }

        ['landing', 'app', 'notfound'].forEach(function (id) { setHidden(el(id), true); });

        if (path === '#/app' || (wantsApp && (path === '' || path === '#/'))) {
            setHidden(el('app'), false);
        } else if (path === '' || path === '#/') {
            setHidden(el('landing'), false);
        } else if (path === '#/how') {
            setHidden(el('landing'), false);
            var how = el('how');
            if (how) { how.scrollIntoView(); }
            return;
        } else {
            setHidden(el('notfound'), false);
        }
        window.scrollTo(0, 0);
    }

    // ---- Wiring and boot -------------------------------------------------
    function wire() {
        el('btn-exhume').addEventListener('click', startExhume);
        el('repo-source').addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); startExhume(); }
        });
        el('date-slider').addEventListener('input', onSliderInput);
        el('btn-seance').addEventListener('click', runSeance);
        el('btn-seance-cancel').addEventListener('click', cancelSeance);
        el('question-input').addEventListener('keydown', function (e) {
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); runSeance(); }
        });
        el('btn-status-retry').addEventListener('click', function () { pollHealth(true); });

        var chip = el('status-chip'), pop = el('status-popover');
        chip.addEventListener('click', function () {
            var opening = pop.hasAttribute('hidden');
            setHidden(pop, !opening);
            chip.setAttribute('aria-expanded', String(opening));
        });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && !pop.hasAttribute('hidden')) {
                setHidden(pop, true);
                chip.setAttribute('aria-expanded', 'false');
                chip.focus();
            }
        });
    }

    function boot() {
        wire();
        route();
        startHealthPolling();
        loadRepos().then(function (repo) {
            if (repo) { el('repo-source').value = repo.source || ''; }
        });
    }

    window.addEventListener('hashchange', route);
    if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', boot); }
    else { boot(); }
})();