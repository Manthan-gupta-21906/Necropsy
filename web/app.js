// necropsy/web/app.js — Séance with the Ghost
// ---------------------------------------------------------------
// Static, no build step, no CDN. Every request has a timeout and every
// failure has a human message plus a Retry button. Nothing stays on
// "loading" forever.

(function () {
    'use strict';

    // --- Configuration ---------------------------------------------------

    var API_BASE = (function () {
        var fromQuery = new URLSearchParams(window.location.search).get('api');
        if (fromQuery) { return fromQuery.replace(/\/$/, ''); }
        if (window.location.protocol === 'http:' || window.location.protocol === 'https:') {
            return window.location.origin;
        }
        return 'http://127.0.0.1:8000';
    })();

    var TIMEOUTS = {
        health: 6000,
        repos: 10000,
        exhume: 15000,
        job: 10000,
        autopsy: 20000,
        seance: 320000
    };

    var HEALTH_POLL_MS = 5000;
    var SLIDER_DEBOUNCE_MS = 300;

    // --- Small DOM helpers -----------------------------------------------

    function el(id) { return document.getElementById(id); }

    function setHidden(node, hidden) {
        if (!node) { return; }
        if (hidden) { node.setAttribute('hidden', ''); }
        else { node.removeAttribute('hidden'); }
    }

    function setText(node, text) {
        if (node) { node.textContent = text == null ? '' : String(text); }
    }

    function clear(node) {
        if (!node) { return; }
        while (node.firstChild) { node.removeChild(node.firstChild); }
    }

    function make(tag, className, text) {
        var node = document.createElement(tag);
        if (className) { node.className = className; }
        if (text != null) { node.textContent = String(text); }
        return node;
    }

    function addRetry(container, label, handler) {
        var btn = make('button', 'btn-ghost', label || 'Retry');
        btn.type = 'button';
        btn.addEventListener('click', handler);
        container.appendChild(document.createTextNode(' '));
        container.appendChild(btn);
    }

    // --- Formatting -------------------------------------------------------

    var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

    function parseIso(value) {
        if (!value) { return null; }
        var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value));
        if (!m) { return null; }
        return { y: Number(m[1]), m: Number(m[2]), d: Number(m[3]) };
    }

    function dayNumber(value) {
        var p = parseIso(value);
        if (!p) { return null; }
        return Math.floor(Date.UTC(p.y, p.m - 1, p.d) / 86400000);
    }

    function dateFromDay(day) {
        var d = new Date(Number(day) * 86400000);
        var mm = String(d.getUTCMonth() + 1).padStart(2, '0');
        var dd = String(d.getUTCDate()).padStart(2, '0');
        return d.getUTCFullYear() + '-' + mm + '-' + dd;
    }

    function prettyDate(value) {
        var p = parseIso(value);
        if (!p) { return '—'; }
        return p.d + ' ' + MONTHS[p.m - 1] + ' ' + p.y;
    }

    function prettyMonth(value) {
        var p = parseIso(value);
        if (!p) { return String(value || '—'); }
        return MONTHS[p.m - 1] + ' ' + String(p.y).slice(2);
    }

    function prettyMs(ms) {
        if (typeof ms !== 'number' || isNaN(ms)) { return '—'; }
        if (ms < 1000) { return ms + ' ms'; }
        return (ms / 1000).toFixed(1) + ' s';
    }

    // --- HTTP with a hard timeout ----------------------------------------

    function ApiError(message, status, payload) {
        this.name = 'ApiError';
        this.message = message;
        this.status = status || 0;
        this.payload = payload || null;
    }
    ApiError.prototype = Object.create(Error.prototype);

    function describeError(payload, status) {
        if (payload && typeof payload.detail === 'string') { return payload.detail; }
        if (payload && payload.detail && typeof payload.detail.message === 'string') {
            return payload.detail.message;
        }
        if (payload && typeof payload.message === 'string') { return payload.message; }
        if (status === 404) { return 'Not found (HTTP 404).'; }
        if (status === 422) { return 'The server rejected the request (HTTP 422).'; }
        if (status >= 500) { return 'The server failed with HTTP ' + status + '.'; }
        return 'Request failed with HTTP ' + status + '.';
    }

    function api(path, options, timeoutMs) {
        var opts = options || {};
        var limit = timeoutMs || 15000;
        var controller = new AbortController();
        var timer = setTimeout(function () { controller.abort(); }, limit);
        var init = {
            method: opts.method || 'GET',
            headers: opts.headers || {},
            signal: controller.signal
        };
        if (opts.body !== undefined) {
            init.headers = Object.assign({ 'Content-Type': 'application/json' }, init.headers);
            init.body = JSON.stringify(opts.body);
        }

        return fetch(API_BASE + path, init)
            .then(function (res) {
                return res.text().then(function (text) {
                    var data = null;
                    if (text) {
                        try { data = JSON.parse(text); }
                        catch (e) { data = null; }
                    }
                    if (!res.ok) {
                        throw new ApiError(describeError(data, res.status), res.status, data);
                    }
                    if (text && data === null) {
                        throw new ApiError('The server returned a malformed response.', res.status, null);
                    }
                    return data;
                });
            })
            .catch(function (err) {
                if (err && err.name === 'AbortError') {
                    throw new ApiError(
                        'No response within ' + Math.round(limit / 1000) + ' s. ' +
                        'The server may still be working — try again.',
                        0, null
                    );
                }
                if (err instanceof ApiError) { throw err; }
                throw new ApiError(
                    'Cannot reach the Necropsy server at ' + API_BASE + '. ' +
                    'Is it running? (' + (err && err.message ? err.message : 'network error') + ')',
                    0, null
                );
            })
            .finally(function () { clearTimeout(timer); });
    }

    // --- State ------------------------------------------------------------

    var state = {
        repo: null,
        asOf: null,
        health: null,
        healthTimer: null,
        sliderTimer: null,
        indexing: false,
        seanceBusy: false,
        lastSeance: null
    };

    // --- Machine status ---------------------------------------------------

    function renderHealth(health) {
        state.health = health;
        var pill = el('status-pill');
        var note = el('status-note');
        var ollama = health.ollama || {};
        var embeddings = health.embeddings || {};
        var warmup = ollama.warmup || {};

        var status = health.status || 'unknown';
        var pillText = status;
        var pillClass = 'pill pill-unknown';

        if (status === 'ok') { pillClass = 'pill pill-ok'; }
        else if (status === 'warming') { pillClass = 'pill pill-warn'; }
        else if (status === 'error') { pillClass = 'pill pill-error'; }

        setText(pill, pillText);
        pill.className = pillClass;

        var modelEl = el('status-model');
        if (ollama.model) {
            setText(modelEl, ollama.model + (ollama.model_installed ? ' (installed)' : ' (not installed)'));
            modelEl.className = 'status-value ' + (ollama.model_installed ? 'is-ok' : 'is-error');
        } else {
            setText(modelEl, 'unknown');
            modelEl.className = 'status-value is-error';
        }

        var ollamaEl = el('status-ollama');
        if (ollama.reachable && ollama.model_installed) {
            setText(ollamaEl, 'reachable · model ready');
            ollamaEl.className = 'status-value is-ok';
        } else if (warmup.state === 'warming') {
            setText(ollamaEl, 'loading the model into memory…');
            ollamaEl.className = 'status-value is-warn';
        } else if (ollama.reachable) {
            setText(ollamaEl, 'reachable · model missing');
            ollamaEl.className = 'status-value is-warn';
        } else {
            setText(ollamaEl, 'unreachable');
            ollamaEl.className = 'status-value is-error';
        }

        var embEl = el('status-embeddings');
        var embState = embeddings.state || 'unknown';
        if (embState === 'ready') {
            setText(embEl, 'ready · ' + (embeddings.model || 'local model'));
            embEl.className = 'status-value is-ok';
        } else if (embState === 'loading') {
            setText(embEl, 'loading ' + (embeddings.model || 'model') + '…');
            embEl.className = 'status-value is-warn';
        } else if (embState === 'error') {
            setText(embEl, 'error');
            embEl.className = 'status-value is-error';
        } else {
            setText(embEl, embState);
            embEl.className = 'status-value is-warn';
        }

        setText(el('status-latency'), prettyMs(health.last_answer_ms));

        // One honest note explaining anything that is not right.
        var messages = [];
        if (ollama.error) { messages.push(ollama.error); }
        if (embeddings.state === 'error' && embeddings.message) {
            messages.push('Embeddings failed: ' + embeddings.message);
        }
        if (ollama.reachable && !ollama.model_installed && ollama.model) {
            messages.push('Install the model with: ollama pull ' + ollama.model);
        }
        if (messages.length) {
            setText(note, messages.join(' '));
            setHidden(note, false);
        } else {
            setText(note, '');
            setHidden(note, true);
        }
    }

    function renderHealthError(err) {
        setText(el('status-pill'), 'offline');
        el('status-pill').className = 'pill pill-error';
        setText(el('status-model'), 'unknown');
        setText(el('status-ollama'), 'unreachable');
        el('status-ollama').className = 'status-value is-error';
        setText(el('status-embeddings'), 'unknown');
        setText(el('status-latency'), '—');
        var note = el('status-note');
        setText(note, err.message);
        setHidden(note, false);
    }

    function pollHealth() {
        return api('/health', {}, TIMEOUTS.health)
            .then(renderHealth)
            .catch(renderHealthError);
    }

    function startHealthPolling() {
        pollHealth();
        if (state.healthTimer) { clearInterval(state.healthTimer); }
        state.healthTimer = setInterval(pollHealth, HEALTH_POLL_MS);
    }

    // --- Repos ------------------------------------------------------------

    function selectRepo(repo) {
        state.repo = repo;
        setText(el('repo-name'), repo.name || ('repo ' + repo.id));
        setHidden(el('timeline-section'), false);
        setHidden(el('autopsy-section'), false);

        var firstDay = dayNumber(repo.first_commit_at);
        var lastDay = dayNumber(repo.last_commit_at);
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

        // Deep link: ?as_of=YYYY-MM-DD selects a starting boundary.
        var requested = new URLSearchParams(window.location.search).get('as_of');
        var requestedDay = requested ? dayNumber(requested) : null;
        var startDay = lastDay;
        if (requestedDay != null && requestedDay >= firstDay && requestedDay <= lastDay) {
            startDay = requestedDay;
        }
        slider.value = String(startDay);
        slider.setAttribute('value', String(startDay));

        setText(el('bound-first'), prettyDate(repo.first_commit_at));
        setText(el('bound-last'), prettyDate(repo.last_commit_at));

        setAsOf(dateFromDay(startDay));
        loadAutopsy();
    }

    function setAsOf(dateStr) {
        state.asOf = dateStr;
        setText(el('date-display'), prettyDate(dateStr));
        setText(el('seance-asof'), 'as of ' + prettyDate(dateStr));
        setText(el('autopsy-asof'), 'as of ' + prettyDate(dateStr));
        var slider = el('date-slider');
        slider.setAttribute('aria-valuetext', prettyDate(dateStr));
    }

    function loadRepos() {
        return api('/repos', {}, TIMEOUTS.repos)
            .then(function (data) {
                var repos = (data && data.repos) || [];
                var ready = repos.filter(function (r) { return r.status === 'ready' && r.commit_count > 0; });
                if (!ready.length) {
                    setText(el('repo-name'), 'no repo indexed');
                    return null;
                }
                var repo = ready[ready.length - 1];
                selectRepo(repo);
                return repo;
            })
            .catch(function (err) {
                // Non-fatal at boot: the exhume form still works.
                var msg = el('exhume-error');
                setText(msg, 'Could not list indexed repositories. ' + err.message);
                setHidden(msg, false);
                return null;
            });
    }

    // --- Exhume -----------------------------------------------------------

    function validateSource(value) {
        var source = (value || '').trim();
        if (!source) { return 'Enter a repository URL or a local path.'; }
        if (source.charAt(0) === '-') { return 'A source may not start with "-".'; }
        if (/^https:\/\//i.test(source) || /^ssh:\/\//i.test(source)) { return null; }
        if (/^[\w.\-]+@[\w.\-]+:.+$/.test(source)) { return null; }
        if (/[\\/]/.test(source) || /^[A-Za-z]:/.test(source)) { return null; }
        return 'Use an https:// or ssh:// URL, or a local path that contains .git.';
    }

    function setExhumeBusy(busy) {
        state.indexing = busy;
        var btn = el('btn-exhume');
        btn.disabled = busy;
        setText(btn, busy ? 'Indexing…' : 'Index Repository');
        el('repo-source').disabled = busy;
        setHidden(el('exhume-progress'), !busy);
        if (!busy) {
            el('exhume-bar').style.width = '0%';
        }
    }

    function renderJob(job) {
        var pct = Math.max(0, Math.min(100, Math.round((job.progress || 0) * 100)));
        el('exhume-bar').style.width = pct + '%';
        var label = job.stage || 'working';
        if (job.commit_count) { label += ' · ' + job.commit_count + ' commits'; }
        setText(el('exhume-stage'), label + ' · ' + pct + '%');
    }

    function pollJob(jobId) {
        return api('/exhume/' + encodeURIComponent(jobId), {}, TIMEOUTS.job)
            .then(function (job) {
                renderJob(job);
                if (job.status === 'ready') { return job; }
                if (job.status === 'failed') {
                    throw new ApiError(job.error || 'Indexing failed.', 0, job);
                }
                return new Promise(function (resolve) { setTimeout(resolve, 800); })
                    .then(function () { return pollJob(jobId); });
            });
    }

    function startExhume() {
        if (state.indexing) { return; }

        var sourceInput = el('repo-source');
        var source = sourceInput.value.trim();
        var problem = validateSource(source);
        var errEl = el('exhume-error');
        var msgEl = el('exhume-message');

        setHidden(errEl, true);
        setHidden(msgEl, true);

        if (problem) {
            setText(errEl, problem);
            setHidden(errEl, false);
            sourceInput.focus();
            return;
        }

        setExhumeBusy(true);
        setText(el('exhume-stage'), 'queued · 0%');

        api('/exhume', { method: 'POST', body: { source: source } }, TIMEOUTS.exhume)
            .then(function (job) {
                setText(el('exhume-stage'), (job.stage || 'queued') + ' · 0%');
                return pollJob(job.job_id);
            })
            .then(function (job) {
                setExhumeBusy(false);
                setText(msgEl,
                    'Indexed ' + (job.commit_count || 0) + ' commits and ' +
                    (job.embeddings || 0) + ' embeddings. ' +
                    prettyDate(job.first_commit_at) + ' → ' + prettyDate(job.last_commit_at) + '.');
                msgEl.className = 'message message-ok';
                setHidden(msgEl, false);
                return api('/repos', {}, TIMEOUTS.repos);
            })
            .then(function (data) {
                var repos = (data && data.repos) || [];
                var match = repos.filter(function (r) {
                    return r.source === source && r.status === 'ready';
                })[0] || repos[repos.length - 1];
                if (match) { selectRepo(match); }
            })
            .catch(function (err) {
                setExhumeBusy(false);
                setHidden(el('exhume-progress'), true);
                setText(errEl, err.message);
                setHidden(errEl, false);
                addRetry(errEl, 'Try again', function () {
                    setHidden(errEl, true);
                    startExhume();
                });
            });
    }

    // --- Autopsy ----------------------------------------------------------

    function statCard(label, value, sub) {
        var card = make('div', 'stat');
        card.appendChild(make('span', 'stat-label', label));
        var v = make('span', 'stat-value', value);
        if (sub) {
            v.appendChild(document.createTextNode(' '));
            v.appendChild(make('small', '', sub));
        }
        card.appendChild(v);
        return card;
    }

    function renderStats(data) {
        var grid = el('stats-grid');
        clear(grid);
        grid.appendChild(statCard('Commits', data.commit_count == null ? '—' : data.commit_count));
        grid.appendChild(statCard('Authors', data.author_count == null ? '—' : data.author_count));
        grid.appendChild(statCard('Additions', data.additions == null ? '—' : data.additions));
        grid.appendChild(statCard('Deletions', data.deletions == null ? '—' : data.deletions));
        grid.appendChild(statCard('Last commit', prettyDate(data.last_commit_at)));
        grid.appendChild(statCard('Tombstones', data.tombstone_count == null ? '—' : data.tombstone_count));

        if (data.bus_factor) {
            grid.appendChild(statCard(
                'Bus factor',
                data.bus_factor.top_author || '—',
                Math.round((data.bus_factor.share || 0) * 100) + '% of commits'
            ));
        } else {
            grid.appendChild(statCard('Bus factor', '—'));
        }
    }

    function renderChart(months) {
        var host = el('activity-chart');
        clear(host);

        if (!months || !months.length) {
            host.appendChild(make('p', 'chart-empty', 'No commits up to this date.'));
            return;
        }

        var max = 1;
        months.forEach(function (m) { if (m.commits > max) { max = m.commits; } });

        var barW = 18;
        var gap = 6;
        var width = Math.max(320, months.length * (barW + gap) + 40);
        var height = 150;
        var baseline = 118;
        var svgNS = 'http://www.w3.org/2000/svg';
        var svg = document.createElementNS(svgNS, 'svg');
        svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
        svg.setAttribute('width', String(width));
        svg.setAttribute('height', String(height));
        svg.setAttribute('role', 'img');

        var axis = document.createElementNS(svgNS, 'line');
        axis.setAttribute('x1', '28');
        axis.setAttribute('y1', String(baseline));
        axis.setAttribute('x2', String(width - 8));
        axis.setAttribute('y2', String(baseline));
        axis.setAttribute('stroke', '#2b2b34');
        axis.setAttribute('stroke-width', '1');
        svg.appendChild(axis);

        months.forEach(function (m, i) {
            var h = Math.max(2, Math.round((m.commits / max) * 88));
            var x = 34 + i * (barW + gap);
            var y = baseline - h;

            var rect = document.createElementNS(svgNS, 'rect');
            rect.setAttribute('x', String(x));
            rect.setAttribute('y', String(y));
            rect.setAttribute('width', String(barW));
            rect.setAttribute('height', String(h));
            rect.setAttribute('fill', '#3f7c88');
            rect.setAttribute('rx', '1');
            var title = document.createElementNS(svgNS, 'title');
            title.textContent = prettyMonth(m.month) + ': ' + m.commits + ' commits';
            rect.appendChild(title);
            svg.appendChild(rect);

            var label = document.createElementNS(svgNS, 'text');
            label.setAttribute('x', String(x + barW / 2));
            label.setAttribute('y', String(baseline + 14));
            label.setAttribute('fill', '#6f6a60');
            label.setAttribute('font-size', '9');
            label.setAttribute('text-anchor', 'middle');
            label.setAttribute('font-family', 'monospace');
            label.textContent = prettyMonth(m.month);
            svg.appendChild(label);

            var count = document.createElementNS(svgNS, 'text');
            count.setAttribute('x', String(x + barW / 2));
            count.setAttribute('y', String(y - 4));
            count.setAttribute('fill', '#a8a294');
            count.setAttribute('font-size', '9');
            count.setAttribute('text-anchor', 'middle');
            count.setAttribute('font-family', 'monospace');
            count.textContent = String(m.commits);
            svg.appendChild(count);
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
            li.appendChild(document.createTextNode(c.subject || '(no subject)'));
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
            li.appendChild(document.createTextNode(t.epitaph || ''));
            li.appendChild(make('span', 'commit-date',
                prettyDate(t.died_at) + ' · lived ' + (t.lifespan_days || 0) + ' days'));
            host.appendChild(li);
        });
    }

    function loadAutopsy() {
        if (!state.repo || !state.asOf) { return; }
        var repoId = state.repo.id;
        var asOf = state.asOf;

        setHidden(el('autopsy-loading'), false);
        setHidden(el('autopsy-error'), true);
        setHidden(el('autopsy-section'), false);

        api('/autopsy?repo_id=' + encodeURIComponent(repoId) +
            '&as_of=' + encodeURIComponent(asOf), {}, TIMEOUTS.autopsy)
            .then(function (data) {
                // Ignore a response that arrived after the slider moved again.
                if (state.asOf !== asOf) { return; }
                setHidden(el('autopsy-loading'), true);
                setHidden(el('autopsy-content'), false);
                renderStats(data);
                renderChart(data.monthly_activity);
                renderCommits(data.recent_commits);
                renderTombstones(data.tombstones);
                setHidden(el('autopsy-section'), false);
            })
            .catch(function (err) {
                if (state.asOf !== asOf) { return; }
                setHidden(el('autopsy-loading'), true);
                setHidden(el('autopsy-content'), true);
                var box = el('autopsy-error');
                setText(box, 'Could not load the autopsy. ' + err.message);
                setHidden(box, false);
                addRetry(box, 'Retry', function () {
                    setHidden(box, true);
                    loadAutopsy();
                });
            });
    }

    // --- Slider -----------------------------------------------------------

    function onSliderInput() {
        var slider = el('date-slider');
        var dateStr = dateFromDay(slider.value);
        setAsOf(dateStr);

        if (state.sliderTimer) { clearTimeout(state.sliderTimer); }
        state.sliderTimer = setTimeout(function () {
            state.sliderTimer = null;
            loadAutopsy();
        }, SLIDER_DEBOUNCE_MS);
    }

    // --- Séance -----------------------------------------------------------

    function renderSeance(data) {
        var result = el('seance-result');
        var answer = el('answer-text');
        var claimsList = el('claims-list');

        clear(claimsList);
        setText(answer, data.answer || '');
        var claims = data.claims || [];
        var refused = !claims.length || data.answer === 'The dead do not remember that.';
        answer.className = 'answer' + (refused ? ' is-refusal' : '');

        var remembers = el('remembers-text');
        if (data.remembers) {
            setText(remembers, 'It remembers: ' + data.remembers);
            setHidden(remembers, false);
        } else {
            setHidden(remembers, true);
        }

        claims.forEach(function (claim) {
            var li = make('li');
            var btn = make('button', 'citation');
            btn.type = 'button';
            btn.setAttribute('aria-expanded', 'false');

            btn.appendChild(make('span', 'claim-text', claim.claim || '(no claim text)'));
            btn.appendChild(make('span', 'commit-line', String(claim.commit_hash || '').slice(0, 12)));
            btn.appendChild(make('span', 'commit-meta',
                prettyDate(claim.at) + ' · ' + (claim.subject || '(no subject)')));

            var detail = make('span', 'citation-detail',
                'Full hash: ' + (claim.commit_hash || '—') +
                (claim.support ? ' · Model support: ' + claim.support : ''));
            setHidden(detail, true);
            btn.appendChild(detail);

            btn.addEventListener('click', function () {
                var open = btn.getAttribute('aria-expanded') === 'true';
                btn.setAttribute('aria-expanded', open ? 'false' : 'true');
                setHidden(detail, open);
            });

            li.appendChild(btn);
            claimsList.appendChild(li);
        });

        if (!claims.length) {
            var empty = make('li');
            empty.appendChild(make('p', 'empty-note',
                'No claim survived citation verification, so the ghost refuses to answer.'));
            claimsList.appendChild(empty);
        }

        var evidence = data.evidence || [];
        setText(el('evidence-note'),
            'Verified ' + claims.length + ' of ' + (data.claims_total || 0) +
            ' proposed claims against ' + evidence.length + ' evidence commits · ' +
            (data.model || 'model') + ' · ' + prettyMs(data.latency_ms));

        setHidden(result, false);
    }

    function runSeance() {
        if (state.seanceBusy) { return; }

        var questionInput = el('question-input');
        var question = questionInput.value.trim();
        var errEl = el('seance-error');
        var statusEl = el('seance-status');

        setHidden(errEl, true);
        setHidden(el('seance-result'), true);

        if (!state.repo) {
            setText(errEl, 'Index a repository first, then ask the ghost.');
            setHidden(errEl, false);
            return;
        }
        if (!state.asOf) {
            setText(errEl, 'Choose a date on the time slider first.');
            setHidden(errEl, false);
            return;
        }
        if (!question) {
            setText(errEl, 'Type a question for the ghost.');
            setHidden(errEl, false);
            questionInput.focus();
            return;
        }

        state.seanceBusy = true;
        var btn = el('btn-seance');
        btn.disabled = true;
        setText(btn, 'Consulting…');
        setText(statusEl, 'The ghost is thinking. The first answer can take a few minutes ' +
            'while the local model loads.');
        setHidden(statusEl, false);

        var payload = { repo_id: state.repo.id, as_of: state.asOf, question: question };
        state.lastSeance = payload;

        api('/seance', { method: 'POST', body: payload }, TIMEOUTS.seance)
            .then(function (data) {
                state.seanceBusy = false;
                btn.disabled = false;
                setText(btn, 'Question the Ghost');
                setHidden(statusEl, true);
                renderSeance(data);
                pollHealth();
            })
            .catch(function (err) {
                state.seanceBusy = false;
                btn.disabled = false;
                setText(btn, 'Question the Ghost');
                setHidden(statusEl, true);
                setText(errEl, err.message);
                setHidden(errEl, false);
                addRetry(errEl, 'Ask again', function () {
                    setHidden(errEl, true);
                    runSeance();
                });
            });
    }

    // --- Wiring -----------------------------------------------------------

    function wire() {
        el('btn-exhume').addEventListener('click', startExhume);
        el('repo-source').addEventListener('keydown', function (event) {
            if (event.key === 'Enter') {
                event.preventDefault();
                startExhume();
            }
        });

        el('date-slider').addEventListener('input', onSliderInput);

        el('btn-seance').addEventListener('click', runSeance);
        el('question-input').addEventListener('keydown', function (event) {
            if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
                event.preventDefault();
                runSeance();
            }
        });

        el('btn-status-retry').addEventListener('click', pollHealth);
    }

    function boot() {
        wire();
        startHealthPolling();
        loadRepos().then(function (repo) {
            if (repo) {
                setText(el('repo-source'), repo.source || '');
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
})();
