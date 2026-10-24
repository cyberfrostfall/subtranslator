'use strict';

const POLL_INTERVAL = 1000;
const POLL_RETRY_INTERVAL = 4000;
const HEALTH_INTERVAL = 10000;

const TASK_STATUS = {
    starting: {label: '等待中', tone: 'neutral', group: 'active'},
    translating: {label: '翻译中', tone: 'active', group: 'active'},
    success: {label: '已完成', tone: 'success', group: 'done'},
    partial: {label: '部分完成', tone: 'warning', group: 'done'},
    skipped: {label: '已跳过', tone: 'neutral', group: 'done'},
    failed: {label: '失败', tone: 'danger', group: 'failed'}
};
const LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR'];

let dashboardVersion = null;
let startTime = null;
let pollTimer = null;
let previousModalFocus = null;

function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, character => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    })[character]);
}

function fileNameFromPath(filePath) {
    return String(filePath || '').replaceAll('\\', '/').split('/').pop() || '未知文件';
}

function placeholder(message) {
    return `<div class="placeholder">${escapeHtml(message)}</div>`;
}

// ---------- 时间 ----------

function formatUptime(startTimeString) {
    if (!startTimeString) return '0h 0m';
    const difference = Math.max(0, Math.floor((Date.now() - new Date(startTimeString)) / 1000));
    const days = Math.floor(difference / 86400);
    const hours = Math.floor((difference % 86400) / 3600);
    const minutes = Math.floor((difference % 3600) / 60);
    return days ? `${days}d ${hours}h` : `${hours}h ${minutes}m`;
}

function relativeTime(timestamp) {
    const time = new Date(timestamp);
    if (Number.isNaN(time.getTime())) return '';
    const difference = Math.max(0, Math.floor((Date.now() - time.getTime()) / 1000));
    if (difference < 10) return '刚刚';
    if (difference < 60) return `${difference} 秒前`;
    if (difference < 3600) return `${Math.floor(difference / 60)} 分钟前`;
    if (difference < 86400) return `${Math.floor(difference / 3600)} 小时前`;
    return time.toLocaleDateString('zh-CN');
}

function clockTime(timestamp) {
    const time = new Date(timestamp);
    if (Number.isNaN(time.getTime())) return '--:--:--';
    return time.toLocaleTimeString('zh-CN', {hour12: false});
}

// 相对时间单独刷新，不触发列表重绘
function refreshRelativeTimes(root = document) {
    root.querySelectorAll('time[data-relative]').forEach(element => {
        element.textContent = relativeTime(element.dateTime);
    });
}

// ---------- 按 key 增量更新列表 ----------

const template = document.createElement('template');

function elementFromHtml(html) {
    template.innerHTML = html.trim();
    return template.content.firstElementChild;
}

/**
 * 让 container 的子元素与 items 一致：只替换内容变化的元素、只插入新元素，
 * 未变化的元素原样保留（不打断文本选择、悬停和动画）。返回新插入的元素。
 */
function reconcile(container, items, keyOf, render) {
    const existing = new Map();
    [...container.children].forEach(element => {
        if (element.dataset.key) existing.set(element.dataset.key, element);
        else element.remove();
    });

    const inserted = [];
    let previous = null;
    for (const item of items) {
        const key = String(keyOf(item));
        const html = render(item);
        let element = existing.get(key);
        if (element) {
            existing.delete(key);
            if (element._html !== html) {
                const replacement = elementFromHtml(html);
                replacement.className = element.className;
                element.replaceWith(replacement);
                element = replacement;
            }
        } else {
            element = elementFromHtml(html);
            inserted.push(element);
        }
        element.dataset.key = key;
        element._html = html;

        const reference = previous ? previous.nextElementSibling : container.firstElementChild;
        if (reference !== element) container.insertBefore(element, reference);
        previous = element;
    }
    existing.forEach(element => element.remove());
    return inserted;
}

function markNew(elements) {
    elements.forEach(element => {
        element.classList.add('is-new');
        element.addEventListener('animationend', () => element.classList.remove('is-new'), {once: true});
    });
}

// ---------- 渲染 ----------

function renderStats(stats) {
    startTime = stats.start_time;
    document.getElementById('stat-success').textContent = stats.total_translated || 0;
    document.getElementById('stat-failed').textContent = stats.total_failed || 0;
    document.getElementById('stat-progress').textContent = stats.in_progress || 0;
    document.getElementById('stat-uptime').textContent = formatUptime(startTime);
    document.querySelector('.stat[data-tone="active"]').classList.toggle('is-live', stats.in_progress > 0);
    document.querySelector('.stat[data-tone="danger"]').classList.toggle('is-alert', stats.total_failed > 0);
}

function shortTaskMessage(message) {
    const text = String(message || '');
    if (text.startsWith('已保存到 ')) return `已保存为 ${fileNameFromPath(text.slice(5))}`;
    if (text.startsWith('错误: ')) return text.slice(4);
    return text;
}

function renderTask(log) {
    const status = TASK_STATUS[log.status] || TASK_STATUS.translating;
    const progress = Math.min(100, Math.max(0, Number(log.progress) || 0));
    const name = fileNameFromPath(log.file_path);
    const showBar = status.group === 'active';
    return `
        <article class="task" data-group="${status.group}" data-tone="${status.tone}">
            <div class="task-main">
                <span class="task-name" title="${escapeHtml(log.file_path)}">${escapeHtml(name)}</span>
                <span class="badge" data-tone="${status.tone}">${status.label}</span>
            </div>
            ${showBar ? `
            <div class="progress" role="progressbar" aria-label="${escapeHtml(name)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress}">
                <span style="width: ${progress}%"></span>
            </div>` : ''}
            <div class="task-meta">
                <span class="task-message" title="${escapeHtml(log.message)}">${escapeHtml(shortTaskMessage(log.message))}</span>
                ${showBar ? `<span class="task-percent">${progress}%</span>` : ''}
                <time data-relative datetime="${escapeHtml(log.updated_at)}"></time>
            </div>
        </article>`;
}

function renderTasks(logs) {
    const container = document.getElementById('translation-progress-list');
    const counts = {all: logs.length, active: 0, done: 0, failed: 0};
    logs.forEach(log => {
        counts[(TASK_STATUS[log.status] || TASK_STATUS.translating).group] += 1;
    });
    document.querySelectorAll('#task-filter [data-count]').forEach(element => {
        element.textContent = counts[element.dataset.count];
    });

    if (!logs.length) {
        container.innerHTML = placeholder('还没有任务。新的字幕文件出现后会自动开始翻译');
        return;
    }
    // 进行中的任务置顶，其余保持最近更新优先
    const ordered = [...logs].sort((a, b) => {
        const activeA = (TASK_STATUS[a.status] || {}).group === 'active';
        const activeB = (TASK_STATUS[b.status] || {}).group === 'active';
        return activeB - activeA;
    });
    const inserted = reconcile(container, ordered, log => log.file_path, renderTask);
    if (dashboardVersion !== null) markNew(inserted);
    refreshRelativeTimes(container);
    updateFilterEmptyState(container);
}

function groupContentLogs(logs) {
    // 日志按最新优先排列；连续的同一文件归为一组
    const groups = [];
    for (const log of logs) {
        const last = groups[groups.length - 1];
        if (last && last.file_path === log.file_path) last.items.push(log);
        else groups.push({file_path: log.file_path, items: [log]});
    }
    // 组的 key 取组内最早一条，新译文追加到组顶部时 key 不变
    groups.forEach(group => { group.key = `${group.file_path}#${group.items[group.items.length - 1].id}`; });
    return groups;
}

function renderFeedGroup(group) {
    return `
        <section class="feed-group">
            <header class="feed-group-header">
                <span class="feed-file" title="${escapeHtml(group.file_path)}">${escapeHtml(fileNameFromPath(group.file_path))}</span>
                <span class="feed-count"></span>
                <time data-relative></time>
            </header>
            <div class="feed-lines"></div>
        </section>`;
}

function renderFeedLine(log) {
    return `
        <div class="feed-line">
            <p class="feed-translated">${escapeHtml(log.translated)}</p>
            <p class="feed-original">${escapeHtml(log.original)}</p>
        </div>`;
}

function renderFeed(logs) {
    const container = document.getElementById('translation-content-list');
    document.getElementById('feed-meta').textContent = logs.length ? `最近 ${logs.length} 条` : '最新在上';
    if (!logs.length) {
        container.innerHTML = placeholder('译文会在每批字幕翻译完成后立即出现在这里');
        return;
    }

    // 用户正在往下翻看时，新内容插到顶部不应把视图顶走
    const anchored = container.scrollTop > 24;
    const previousHeight = container.scrollHeight;

    const groups = groupContentLogs(logs);
    reconcile(container, groups, group => group.key, renderFeedGroup);
    groups.forEach(group => {
        const element = container.querySelector(`[data-key="${CSS.escape(group.key)}"]`);
        element.querySelector('.feed-count').textContent = `${group.items.length} 条`;
        const time = element.querySelector('time');
        time.dateTime = group.items[0].timestamp;
        const lines = reconcile(element.querySelector('.feed-lines'), group.items, log => log.id, renderFeedLine);
        if (dashboardVersion !== null) markNew(lines);
    });
    refreshRelativeTimes(container);

    if (anchored) container.scrollTop += container.scrollHeight - previousHeight;
}

function renderLog(log) {
    const level = LOG_LEVELS.includes(log.level) ? log.level : 'INFO';
    return `
        <div class="log" data-level="${level}">
            <time class="log-time" datetime="${escapeHtml(log.timestamp)}" title="${escapeHtml(new Date(log.timestamp).toLocaleString('zh-CN'))}">${clockTime(log.timestamp)}</time>
            <span class="log-level">${level}</span>
            <span class="log-message">${escapeHtml(log.message)}</span>
        </div>`;
}

function renderLogs(logs) {
    const container = document.getElementById('log-list');
    if (!logs.length) {
        container.innerHTML = placeholder('暂无系统日志');
        return;
    }
    const atBottom = container.scrollHeight - container.scrollTop <= container.clientHeight + 40;
    reconcile(container, logs, log => log.id ?? `${log.timestamp}${log.message}`, renderLog);
    updateFilterEmptyState(container);
    if (atBottom) container.scrollTop = container.scrollHeight;
}

// ---------- 筛选（只切换 CSS，不重绘） ----------

function updateFilterEmptyState(container) {
    const visible = [...container.children].some(element => element.dataset.key && element.offsetParent !== null);
    container.classList.toggle('is-filtered-empty', !visible && container.children.length > 0);
}

function setSegmentedFilter(groupId, listId, filter) {
    const list = document.getElementById(listId);
    list.dataset.filter = filter;
    document.querySelectorAll(`#${groupId} button`).forEach(button => {
        button.setAttribute('aria-pressed', String(button.dataset.filter === filter));
    });
    updateFilterEmptyState(list);
}

function setTaskFilter(filter) {
    setSegmentedFilter('task-filter', 'translation-progress-list', filter);
    document.querySelectorAll('.stat[data-filter]').forEach(stat => {
        stat.setAttribute('aria-pressed', String(stat.dataset.filter === filter));
    });
}

// ---------- 数据同步 ----------

function setConnection(state, label) {
    const element = document.getElementById('connection-state');
    element.dataset.state = state;
    document.getElementById('connection-label').textContent = label;
}

async function poll() {
    window.clearTimeout(pollTimer);
    let delay = POLL_INTERVAL;
    try {
        const query = dashboardVersion === null ? '' : `?since=${dashboardVersion}`;
        const response = await fetch(`/api/dashboard${query}`, {cache: 'no-store'});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        if (data.changed) {
            renderStats(data.stats);
            renderTasks(data.translation_logs);
            renderFeed(data.translation_content_logs);
            renderLogs(data.logs);
        }
        dashboardVersion = data.version;
        if (document.getElementById('connection-state').dataset.state === 'disconnected') {
            await checkHealth();
        }
    } catch (error) {
        console.error('同步仪表盘失败:', error);
        setConnection('disconnected', '连接中断');
        delay = POLL_RETRY_INTERVAL;
    }
    if (!document.hidden) pollTimer = window.setTimeout(poll, delay);
}

async function checkHealth() {
    try {
        const response = await fetch('/health', {cache: 'no-store'});
        if (response.ok) setConnection('connected', '运行中');
        else setConnection('warning', '监控异常');
    } catch {
        setConnection('disconnected', '连接中断');
    }
}

// ---------- 主题与配置 ----------

function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem('subtranslatorTheme', theme);
    const toggle = document.getElementById('theme-toggle');
    const nextThemeName = theme === 'dark' ? '浅色' : '深色';
    toggle.setAttribute('aria-label', `切换${nextThemeName}主题`);
    toggle.title = `切换${nextThemeName}主题`;
}

function toggleTheme() {
    setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
}

function openConfigModal() {
    const modal = document.getElementById('config-modal');
    previousModalFocus = document.activeElement;
    loadConfig();
    modal.classList.add('active');
    modal.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
    requestAnimationFrame(() => document.getElementById('api-key').focus());
}

function closeConfigModal() {
    const modal = document.getElementById('config-modal');
    modal.classList.remove('active');
    modal.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
    if (previousModalFocus) previousModalFocus.focus();
}

function keepFocusInModal(event) {
    if (event.key !== 'Tab') return;
    const modal = document.getElementById('config-modal');
    const focusable = [...modal.querySelectorAll('button, input, select, [tabindex]:not([tabindex="-1"])')]
        .filter(element => !element.disabled);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
    }
}

async function loadConfig() {
    try {
        const data = await (await fetch('/api/config', {cache: 'no-store'})).json();
        Object.entries(data).forEach(([key, value]) => {
            const input = document.getElementById(key.toLowerCase().replaceAll('_', '-'));
            if (input) input.value = value || '';
        });
    } catch (error) {
        console.error('加载配置失败:', error);
    }
}

function showConfigAlert(type, message) {
    const alert = document.getElementById('config-alert');
    alert.className = `alert ${type}`;
    alert.textContent = message;
    alert.style.display = 'block';
    window.setTimeout(() => {
        alert.style.display = 'none';
    }, 5000);
}

async function saveConfig(event) {
    event.preventDefault();
    const config = Object.fromEntries(new FormData(event.currentTarget).entries());
    try {
        const data = await (await fetch('/api/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(config)
        })).json();
        showConfigAlert(data.success ? 'success' : 'error', data.message || data.error || '请求失败');
    } catch (error) {
        showConfigAlert('error', `保存失败: ${error.message}`);
    }
}

// ---------- 初始化 ----------

function bindEvents() {
    const modal = document.getElementById('config-modal');
    document.getElementById('theme-toggle').addEventListener('click', toggleTheme);
    document.getElementById('config-button').addEventListener('click', openConfigModal);
    document.getElementById('close-config-button').addEventListener('click', closeConfigModal);
    document.getElementById('cancel-config-button').addEventListener('click', closeConfigModal);
    document.getElementById('config-form').addEventListener('submit', saveConfig);
    modal.addEventListener('click', event => {
        if (event.target === modal) closeConfigModal();
    });
    modal.addEventListener('keydown', keepFocusInModal);
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape' && modal.classList.contains('active')) closeConfigModal();
    });

    document.querySelectorAll('#task-filter button').forEach(button => {
        button.addEventListener('click', () => setTaskFilter(button.dataset.filter));
    });
    document.querySelectorAll('#log-filter button').forEach(button => {
        button.addEventListener('click', () => setSegmentedFilter('log-filter', 'log-list', button.dataset.filter));
    });
    document.querySelectorAll('.stat[data-filter]').forEach(stat => {
        stat.addEventListener('click', () => {
            const list = document.getElementById('translation-progress-list');
            const filter = list.dataset.filter === stat.dataset.filter ? 'all' : stat.dataset.filter;
            setTaskFilter(filter);
            document.querySelector('.tasks-panel').scrollIntoView({behavior: 'smooth', block: 'nearest'});
        });
    });

    // 标签页在后台时暂停轮询，切回来立即同步
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            window.clearTimeout(pollTimer);
        } else {
            poll();
            checkHealth();
        }
    });
}

function initialize() {
    bindEvents();
    setTheme(document.documentElement.dataset.theme || 'light');
    setTaskFilter('all');
    checkHealth();
    poll();
    window.setInterval(() => { if (!document.hidden) checkHealth(); }, HEALTH_INTERVAL);
    window.setInterval(() => {
        if (document.hidden) return;
        refreshRelativeTimes();
        document.getElementById('stat-uptime').textContent = formatUptime(startTime);
    }, 5000);
}

initialize();
