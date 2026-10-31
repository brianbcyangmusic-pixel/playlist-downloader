// Tank Battle Arena — Admin Panel
// Uses Firebase compat SDK (loaded via CDN script tags)

const ADMIN_EMAIL = 'brian.bcyang27@gmail.com';

function isAdmin() {
  return currentUser && currentUser.email === ADMIN_EMAIL;
}

function requireAdmin(operation) {
  if (!isAdmin()) {
    showToast('Access denied. Admin privileges required.', true);
    log('warn', 'ADMIN', 'Unauthorized ' + operation + ' attempt');
    return false;
  }
  return true;
}

const firebaseConfig = {
    apiKey: "AIzaSyDom3iJV-ad6I04J9Vq_RiBLIMsCUs0sHw",
    authDomain: "tank-battle-arena-897c0.firebaseapp.com",
    databaseURL: "https://tank-battle-arena-897c0-default-rtdb.firebaseio.com",
    projectId: "tank-battle-arena-897c0",
    storageBucket: "tank-battle-arena-897c0.firebasestorage.app",
    messagingSenderId: "392398447255",
    appId: "1:392398447255:web:8bbe8593daf60388c27fd2",
    measurementId: "G-YHY6BSQZGG"
};

let app, auth, db;
let currentUser = null;
let cachedData = { users: null, leaderboard: null, lobbies: null, feedback: null };
let cachedProgression = {}; // uid -> progression data
let userFilter = 'all'; // 'all' | 'real' | 'test'
let selectedUsers = new Set();

// ===================== INIT =====================
function init() {
    app = firebase.initializeApp(firebaseConfig);
    auth = firebase.auth(app);
    db = firebase.database(app);

    auth.onAuthStateChanged(user => {
        currentUser = user;
        const loginScreen = document.getElementById('loginScreen');
        const dashboard = document.getElementById('dashboard');
        const loginError = document.getElementById('loginError');

        if (user && user.email === ADMIN_EMAIL) {
            loginScreen.style.display = 'none';
            dashboard.style.display = 'flex';
            document.getElementById('adminUserEmail').textContent = user.email;
            loadAllData();
            loadProgressionTab();
            loadNvidiaKey();
        } else if (user && user.email) {
            loginScreen.style.display = 'flex';
            dashboard.style.display = 'none';
            loginError.textContent = 'Access denied. Signed in as ' + user.email + '. Only ' + ADMIN_EMAIL + ' can access.';
            auth.signOut();
        } else {
            loginScreen.style.display = 'flex';
            dashboard.style.display = 'none';
            loginError.textContent = '';
        }
    });
}

window.adminSignIn = function() {
    const provider = new firebase.auth.GoogleAuthProvider();
    auth.signInWithPopup(provider).then(result => {
        const email = result.user.email;
        if (email !== ADMIN_EMAIL) {
            document.getElementById('loginError').textContent = 'Access denied. Only ' + ADMIN_EMAIL + ' can access this panel.';
            auth.signOut();
            return;
        }
        document.getElementById('loginError').textContent = '';
    }).catch(e => {
        if (e.code !== 'auth/popup-closed-by-user') {
            document.getElementById('loginError').textContent = 'Login failed: ' + e.message;
        }
    });
};

window.adminSignOut = function() {
    auth.signOut();
};

// ===================== CLEANUP TEST DATA =====================
window.cleanupTestData = function() {
  if (!requireAdmin('cleanupTestData')) return;
  const users = cachedData.users || {};
  const lb = cachedData.leaderboard || [];
  const testUids = Object.keys(users).filter(isTestUid);
  const testLbUids = [...new Set(lb.filter(e => isTestUid(e.uid)).map(e => e.uid))];
    const allTestUids = new Set([...testUids, ...testLbUids]);

    if (allTestUids.size === 0) {
        showToast('No test data found to clean up.');
        return;
    }

    const confirmMsg = `Delete ${allTestUids.size} test ${allTestUids.size === 1 ? 'entry' : 'entries'}?\n` +
        `This will remove data from users, leaderboard, and progression.\n\n` +
        `Test UIDs:\n${[...allTestUids].slice(0, 10).join('\n')}${allTestUids.size > 10 ? `\n...and ${allTestUids.size - 10} more` : ''}\n\nThis cannot be undone.`;
    if (!confirm(confirmMsg)) return;

    const promises = [];
    let deletedCount = 0;

  allTestUids.forEach(uid => {
    promises.push(db.ref('users/' + uid).remove());
    lb.filter(e => e.uid === uid).forEach(e => {
        promises.push(db.ref('leaderboard/' + e.mode + '/' + uid).remove());
    });
    promises.push(db.ref('user_progression/' + uid).remove());
    deletedCount++;
    if (cachedData.users) delete cachedData.users[uid];
    if (cachedData.leaderboard) cachedData.leaderboard = lb.filter(e => e.uid !== uid);
        delete cachedProgression[uid];
    });

    Promise.all(promises).then(() => {
        showToast(`Deleted ${deletedCount} test ${deletedCount === 1 ? 'entry' : 'entries'}`);
        renderUsers();
        renderLeaderboard();
        renderOverview();
    }).catch(e => showToast('Cleanup failed: ' + e.message, true));
};

// ===================== TOAST =====================
function showToast(msg, isError) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = isError ? 'error show' : 'show';
    setTimeout(() => t.classList.remove('show'), 3000);
}

// ===================== TAB SWITCHING =====================
window.switchTab = function(name) {
    document.querySelectorAll('.dash-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    document.querySelector(`.dash-tab[data-tab="${name}"]`).classList.add('active');
    document.getElementById('panel-' + name).classList.add('active');
    if (name === 'users') renderUsers();
    if (name === 'leaderboard') renderLeaderboard();
    if (name === 'lobbies') renderLobbies();
    if (name === 'controls') loadControlsData();
    if (name === 'feedback') { renderFeedback(); loadNvidiaKey(); }
};

// ===================== USER FILTER =====================
function isTestUid(uid) {
    return uid.startsWith('test_user_') || uid === 'debug_test_entry';
}

window.setUserFilter = function(filter) {
    userFilter = filter;
    document.querySelectorAll('.user-filter-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.filter === filter);
        b.style.background = b.dataset.filter === filter ? 'rgba(52,152,219,0.3)' : 'transparent';
        b.style.color = b.dataset.filter === filter ? '#f39c12' : '#999';
    });
    renderUsers();
};

// ===================== DATA LOADING =====================
function loadAllData() {
    const ref = db.ref('/');
    ref.once('value').then(snap => {
        const data = snap.val() || {};
        cachedData.users = data.users || {};

        // Flatten nested leaderboard/{mode}/{uid} into array of {uid, mode, name, score, level, timestamp}
        const rawLb = data.leaderboard || {};
        const lbEntries = [];
        for (const mode in rawLb) {
            const modeData = rawLb[mode];
            if (!modeData || typeof modeData !== 'object') continue;
            for (const uid in modeData) {
                const entry = modeData[uid];
                if (entry && typeof entry === 'object' && entry.score != null) {
                    entry.uid = uid;
                    entry.mode = mode;
                    lbEntries.push(entry);
                }
            }
        }
        cachedData.leaderboard = lbEntries;

        cachedData.lobbies = data.lobbies || {};
        cachedData.feedback = data.feedback || {};
        renderOverview();
        renderUsers();
        renderLeaderboard();
        renderLobbies();
        renderFeedback();
    }).catch(e => showToast('Failed to load data: ' + e.message, true));
}

// ===================== OVERVIEW =====================
function renderOverview() {
    const users = cachedData.users || {};
    const lb = cachedData.leaderboard || {};
    const lobbies = cachedData.lobbies || {};

    const uids = Object.keys(users);
    const realUids = uids.filter(uid => !isTestUid(uid));
    const testUids = uids.filter(isTestUid);
    const onlineCount = uids.filter(uid => users[uid].online === true).length;
    const activeLobbies = Object.keys(lobbies).filter(id => lobbies[id].status === 'waiting' || lobbies[id].status === 'playing').length;

    document.getElementById('statUsers').textContent = uids.length;
    document.getElementById('statOnline').textContent = onlineCount;
    document.getElementById('statLB').textContent = lb.length;
    document.getElementById('statLobbies').textContent = activeLobbies;

    // Show breakdown
    const infoEl = document.getElementById('overviewRecent');
    infoEl.innerHTML = `
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px;">
            <div class="stat-card"><div class="num">${realUids.length}</div><div class="lbl">REAL USERS</div></div>
            <div class="stat-card"><div class="num">${testUids.length}</div><div class="lbl">TEST PROFILES</div></div>
            <div class="stat-card"><div class="num">${lb.length}</div><div class="lbl">LEADERBOARD</div></div>
        </div>
        <p style="color:#888;font-size:9px;letter-spacing:1px;margin-bottom:8px;">RECENT REAL USER LOGINS</p>
    `;

    const recent = uids
        .filter(uid => !isTestUid(uid))
        .map(uid => ({ uid, ...users[uid] }))
        .filter(u => u.lastLogin)
        .sort((a, b) => (b.lastLogin || 0) - (a.lastLogin || 0))
        .slice(0, 5);

    if (recent.length) {
        let html = '<table class="data-table"><thead><tr><th>Email</th><th>Last Login</th></tr></thead><tbody>';
        recent.forEach(u => {
            const d = new Date(u.lastLogin).toLocaleString();
            html += `<tr><td>${esc(u.email || u.uid)}</td><td style="color:#666;font-size:8px;">${d}</td></tr>`;
        });
        html += '</tbody></table>';
        infoEl.innerHTML += html;
    } else {
        infoEl.innerHTML += '<p style="color:#555;">No recent logins</p>';
    }
}

// ===================== USERS =====================
window.renderUsers = function() {
    const users = cachedData.users || {};
    const query = (document.getElementById('usersSearch').value || '').toLowerCase();

    let uids = Object.keys(users);
    // Apply filter
    if (userFilter === 'real') uids = uids.filter(uid => !isTestUid(uid));
    else if (userFilter === 'test') uids = uids.filter(isTestUid);
        // Apply search
    if (query) {
        uids = uids.filter(uid => {
            const u = users[uid];
            return uid.toLowerCase().includes(query)
                || (u.email || '').toLowerCase().includes(query)
                || (u.name || '').toLowerCase().includes(query)
                || (u.friendCode || '').toLowerCase().includes(query);
        });
    }
    uids.sort();

    // Prune selectedUsers of UIDs no longer in the filtered list
    selectedUsers.forEach(uid => { if (!uids.includes(uid)) selectedUsers.delete(uid); });
    updateBulkActionBar();

    let html = '';
    uids.forEach(uid => {
        const u = users[uid];
        const email = u.email || '—';
        const name = u.name || uid.slice(0, 12);
        const online = u.online;
        const lastLogin = u.lastLogin ? new Date(u.lastLogin).toLocaleString() : '—';
        const hasProg = cachedProgression[uid] ? 'Yes' : '—';
        const dotClass = online ? 'dot-online' : 'dot-offline';
        const isTest = isTestUid(uid);
        const dimTest = isTest && userFilter !== 'test';
        const rowBg = dimTest ? 'style="opacity:0.5;"' : '';
        const checked = selectedUsers.has(uid) ? 'checked' : '';

        html += `<tr ${rowBg}>
            <td><input type="checkbox" class="user-checkbox" value="${uid}" ${checked} onchange="toggleUserSelection('${uid}', this.checked)" style="accent-color:#f39c12;"></td>
            <td class="id-cell">${isTest ? '🧪 ' : ''}${esc(u.friendCode || uid.slice(0, 8))}</td>
            <td>${esc(email)}</td>
            <td>${esc(name)}</td>
            <td><span class="${dotClass}"></span>${online ? 'Online' : 'Offline'}</td>
            <td style="font-size:8px;color:#666;">${lastLogin}</td>
            <td>${hasProg}</td>
            <td>
                <button class="btn-sm" onclick="viewUser('${uid}')">VIEW</button>
                <button class="btn-sm danger" onclick="deleteUser('${uid}')">DELETE</button>
            </td>
        </tr>`;
    });
    document.getElementById('usersBody').innerHTML = html || '<tr><td colspan="8" style="text-align:center;color:#555;padding:20px;">No users found.</td></tr>';
    // Sync select-all checkbox state
    const selectAll = document.getElementById('selectAllCheckbox');
    if (selectAll) selectAll.checked = uids.length > 0 && selectedUsers.size === uids.length;
};

// ===================== MULTI-SELECT DELETE =====================
function updateBulkActionBar() {
    const bar = document.getElementById('bulkActionBar');
    const count = document.getElementById('bulkCount');
    if (selectedUsers.size > 0) {
        bar.style.display = 'flex';
        count.textContent = selectedUsers.size + ' SELECTED';
    } else {
        bar.style.display = 'none';
    }
}

window.toggleUserSelection = function(uid, checked) {
    if (checked) selectedUsers.add(uid);
    else selectedUsers.delete(uid);
    updateBulkActionBar();
    const selectAll = document.getElementById('selectAllCheckbox');
    if (selectAll && !checked) selectAll.checked = false;
};

window.toggleSelectAll = function(checked) {
    const users = cachedData.users || {};
    const query = (document.getElementById('usersSearch').value || '').toLowerCase();
    let uids = Object.keys(users);
    if (userFilter === 'real') uids = uids.filter(uid => !isTestUid(uid));
    else if (userFilter === 'test') uids = uids.filter(isTestUid);
    if (query) {
        uids = uids.filter(uid => {
            const u = users[uid];
            return uid.toLowerCase().includes(query)
                || (u.email || '').toLowerCase().includes(query)
                || (u.name || '').toLowerCase().includes(query);
        });
    }
    if (checked) uids.forEach(uid => selectedUsers.add(uid));
    else selectedUsers.clear();
    document.querySelectorAll('.user-checkbox').forEach(cb => cb.checked = checked);
    updateBulkActionBar();
};

window.clearSelection = function() {
    selectedUsers.clear();
    document.querySelectorAll('.user-checkbox').forEach(cb => cb.checked = false);
    const selectAll = document.getElementById('selectAllCheckbox');
    if (selectAll) selectAll.checked = false;
    updateBulkActionBar();
};

window.deleteSelectedUsers = function() {
  if (!requireAdmin('deleteSelectedUsers')) return;
  if (selectedUsers.size === 0) return;
    const count = selectedUsers.size;
    if (!confirm(`Delete ${count} selected user${count > 1 ? 's' : ''}?\nThis will remove data from users, leaderboard, and progression for all selected UIDs.\n\nThis cannot be undone.`)) return;

    const promises = [];
    selectedUsers.forEach(uid => {
        promises.push(db.ref('users/' + uid).remove());
        promises.push(db.ref('leaderboard/' + uid).remove());
        promises.push(db.ref('user_progression/' + uid).remove());
    });

    Promise.all(promises).then(() => {
        showToast(`Deleted ${count} user${count > 1 ? 's' : ''}`);
        selectedUsers.forEach(uid => {
            delete cachedData.users[uid];
            delete cachedData.leaderboard[uid];
            delete cachedProgression[uid];
        });
        selectedUsers.clear();
        updateBulkActionBar();
        renderUsers();
        renderLeaderboard();
        renderOverview();
    }).catch(e => showToast('Bulk delete failed: ' + e.message, true));
};

// Load a single user's progression data from Firebase
function loadUserProgressionData(uid) {
    return db.ref('user_progression/' + uid).once('value').then(snap => {
        const data = snap.val() || {};
        cachedProgression[uid] = data;
        return data;
    });
}

window.viewUser = function(uid) {
    const u = cachedData.users[uid] || {};
    const isTest = isTestUid(uid);

    // Load progression first, then show modal
    loadUserProgressionData(uid).then(prog => {
        let fields = Object.keys(u).filter(k => k !== 'profile' && k !== 'settings').map(k => {
            const val = typeof u[k] === 'object' ? JSON.stringify(u[k]) : u[k];
            return `<div class="field"><label>${k}</label><input id="editUser_${k}" value="${esc(String(val))}"></div>`;
        }).join('');

        // Profile editor
        let profileFields = '';
        if (u.profile) {
            profileFields = '<div style="margin-top:12px;border-top:1px solid #333;padding-top:10px;"><p style="color:#888;font-size:9px;letter-spacing:1px;margin-bottom:6px;">PROFILE</p>';
            Object.keys(u.profile).forEach(k => {
                const val = typeof u.profile[k] === 'object' ? JSON.stringify(u.profile[k]) : u.profile[k];
                profileFields += `<div class="field"><label>profile.${k}</label><input id="editUser_profile_${k}" value="${esc(String(val))}"></div>`;
            });
            profileFields += '</div>';
        }

        // Settings editor
        let settingsFields = '';
        if (u.settings) {
            settingsFields = '<div style="margin-top:12px;border-top:1px solid #333;padding-top:10px;"><p style="color:#888;font-size:9px;letter-spacing:1px;margin-bottom:6px;">SETTINGS</p>';
            Object.keys(u.settings).forEach(k => {
                const val = typeof u.settings[k] === 'object' ? JSON.stringify(u.settings[k]) : u.settings[k];
                settingsFields += `<div class="field"><label>settings.${k}</label><input id="editUser_settings_${k}" value="${esc(String(val))}"></div>`;
            });
            settingsFields += '</div>';
        }

        // Progression editor
        const p = prog || {};
        const progHtml = `<div style="margin-top:12px;border-top:1px solid #333;padding-top:10px;">
            <p style="color:#f39c12;font-size:9px;letter-spacing:1px;margin-bottom:6px;">PROGRESSION (gems, coins, upgrades)
                <span style="color:#666;font-weight:400;">— saved to Firebase, user gets on next reload</span>
            </p>
            <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;">
                <div class="field"><label>Gems</label><input id="prog_gems" type="number" value="${p.gems || 0}"></div>
                <div class="field"><label>Coins</label><input id="prog_coins" type="number" value="${p.coins || 0}"></div>
                <div class="field"><label>XP</label><input id="prog_xp" type="number" value="${p.xp || 0}"></div>
                <div class="field"><label>Upgrade Points</label><input id="prog_upgradePoints" type="number" value="${p.upgradePoints || 0}"></div>
                <div class="field"><label>Speed</label><input id="prog_up_speed" type="number" value="${(p.upgrades && p.upgrades.speed) || 0}"></div>
                <div class="field"><label>Fuel</label><input id="prog_up_fuel" type="number" value="${(p.upgrades && p.upgrades.fuel) || 0}"></div>
                <div class="field"><label>Mine Radius</label><input id="prog_up_mineRadius" type="number" value="${(p.upgrades && p.upgrades.mineRadius) || 0}"></div>
                <div class="field"><label>Tank Stage</label><input id="prog_tankStage" type="number" value="${p.tankStage || 1}"></div>
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px;">
                <div class="field"><label>Owned Skins (comma-sep)</label><input id="prog_ownedSkins" value="${(p.ownedSkins || ['classic']).join(', ')}"></div>
                <div class="field"><label>Equipped Skin</label><input id="prog_equippedSkin" value="${p.equippedSkin || 'classic'}"></div>
                <div class="field"><label>Owned Weapons (comma-sep)</label><input id="prog_ownedWeapons" value="${(p.ownedWeapons || ['standard']).join(', ')}"></div>
                <div class="field"><label>Equipped Weapon</label><input id="prog_equippedWeapon" value="${p.equippedWeapon || 'standard'}"></div>
            </div>
        </div>`;

        const rawCampaign = prog.campaign || {};
        _campaignEditState[uid] = _ensureFullCampaign(rawCampaign);
        const campaignHtml = `<div style="margin-top:12px;border-top:1px solid #333;padding-top:10px;">
            <p style="color:#f39c12;font-size:9px;letter-spacing:1px;margin-bottom:6px;">CAMPAIGN — Stage/Level Completion
                <span style="color:#666;font-weight:400;">— click level numbers to toggle, then SAVE</span>
            </p>
            <div style="display:flex;gap:12px;align-items:center;margin-bottom:8px;">
                <div class="field" style="margin:0;"><label>Unlocked Stage (0-4)</label>
                    <input id="campaign_unlockedStage" type="number" min="0" max="4" value="${_campaignEditState[uid].unlockedStage}"
                        onchange="renderCampaignGrid('${uid}')">
                </div>
            </div>
            <div id="campaignGrid_${uid}" style="margin-bottom:4px;"></div>
        </div>`;

        const ls = prog.lifetimeStats || {};
        const lsHtml = `<div style="margin-top:12px;border-top:1px solid #333;padding-top:10px;">
            <p style="color:#3498db;font-size:9px;letter-spacing:1px;margin-bottom:6px;">LIFETIME STATS
                <span style="color:#666;font-weight:400;">— read-only, synced from game client</span>
            </p>
            <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;font-size:11px;">
                <div><span style="color:#888;">Total Games:</span> ${ls.totalGames || 0}</div>
                <div><span style="color:#888;">Wins:</span> ${ls.wins || 0}</div>
                <div><span style="color:#888;">Losses:</span> ${ls.losses || 0}</div>
                <div><span style="color:#888;">Kills:</span> ${ls.kills || 0}</div>
                <div><span style="color:#888;">Deaths:</span> ${ls.deaths || 0}</div>
                <div><span style="color:#888;">Best Score:</span> ${ls.bestScore || 0}</div>
            </div>
        </div>`;

        showModal(`Edit User${isTest ? ' 🧪' : ''}: ${uid.slice(0, 20)}…`, `
            ${fields}
            ${profileFields}
            ${settingsFields}
            ${progHtml}
            ${campaignHtml}
            ${lsHtml}
            <div class="modal-actions">
                <button class="btn-sm" onclick="closeModal()">CANCEL</button>
                <button class="btn-sm success" onclick="saveUser('${uid}')">SAVE USER DATA</button>
                <button class="btn-sm success" onclick="saveUserProgression('${uid}')" style="border-color:#27ae60;background:rgba(39,174,96,0.15);">SAVE PROGRESSION</button>
                <button class="btn-sm success" onclick="saveUserCampaign('${uid}')" style="border-color:#8e44ad;background:rgba(142,68,173,0.15);">SAVE CAMPAIGN</button>
            </div>
        `);
        renderCampaignGrid(uid);
    }).catch(e => showToast('Error loading progression: ' + e.message, true));
};

window.saveUser = function(uid) {
  if (!requireAdmin('saveUser')) return;
    const fields = document.querySelectorAll('#modalContent [id^="editUser_"]');
    const update = {};
    fields.forEach(el => {
        if (el.id.startsWith('editUser_prog_') || el.id.startsWith('prog_')) return; // skip progression fields
        const key = el.id.replace('editUser_', '');
        const val = el.value;
        let parsed;
        try { parsed = JSON.parse(val); } catch(e) { parsed = val; }
        if (key.startsWith('profile_')) {
            if (!update.profile) update.profile = cachedData.users[uid].profile || {};
            update.profile[key.replace('profile_', '')] = parsed;
        } else if (key.startsWith('settings_')) {
            if (!update.settings) update.settings = cachedData.users[uid].settings || {};
            update.settings[key.replace('settings_', '')] = parsed;
        } else {
            update[key] = parsed;
        }
    });

    db.ref('users/' + uid).update(update).then(() => {
        showToast('User data saved');
        cachedData.users[uid] = { ...cachedData.users[uid], ...update };
        renderUsers();
        renderOverview();
    }).catch(e => showToast('Save failed: ' + e.message, true));
};

// Save progression data for any user to Firebase user_progression/<uid>
window.saveUserProgression = function(uid) {
  if (!requireAdmin('saveUserProgression')) return;
    const getVal = (id) => parseInt(document.getElementById(id).value) || 0;
    const getStr = (id) => document.getElementById(id).value.trim();

    const existing = cachedProgression[uid] || {};
    const progression = { ...existing };

    progression.gems = getVal('prog_gems');
    progression.coins = getVal('prog_coins');
    progression.xp = getVal('prog_xp');
    progression.upgradePoints = getVal('prog_upgradePoints');
    progression.upgrades = {
        speed: getVal('prog_up_speed'),
        fuel: getVal('prog_up_fuel'),
        mineRadius: getVal('prog_up_mineRadius')
    };
    progression.tankStage = getVal('prog_tankStage');
    progression.ownedSkins = getStr('prog_ownedSkins').split(',').map(s => s.trim()).filter(Boolean);
    progression.equippedSkin = getStr('prog_equippedSkin');
    progression.ownedWeapons = getStr('prog_ownedWeapons').split(',').map(s => s.trim()).filter(Boolean);
    progression.equippedWeapon = getStr('prog_equippedWeapon');
    progression.updatedAt = Date.now();

    db.ref('user_progression/' + uid).set(progression).then(() => {
        showToast('Progression saved to Firebase for ' + uid.slice(0, 12));
        cachedProgression[uid] = progression;
        renderUsers();
    }).catch(e => showToast('Save failed: ' + e.message, true));
};

// ===================== CAMPAIGN EDITING (User Modal) =====================

let _campaignEditState = {};

// Ensure campaign data always has 5 complete stages with 12 levels each,
// so it passes the stages.length === 5 validation in progression.js:getCampaignData()
function _ensureFullCampaign(data) {
    if (!data || typeof data !== 'object') data = {};
    const stages = [];
    for (let si = 0; si < 5; si++) {
        const existingStage = data.stages && data.stages[si];
        const completed = [];
        for (let li = 0; li < 12; li++) {
            completed.push(!!(existingStage && existingStage.completed && existingStage.completed[li]));
        }
        stages.push({
            completed,
            records: (existingStage && existingStage.records) || {}
        });
    }
    return {
        unlockedStage: Math.min(Math.max(0, data.unlockedStage || 0), 4),
        stages
    };
}

window.renderCampaignGrid = function(uid) {
    const state = _campaignEditState[uid];
    if (!state) return;

    const unlockedInput = document.getElementById('campaign_unlockedStage');
    if (unlockedInput) {
        state.unlockedStage = parseInt(unlockedInput.value) || 0;
    }

    const container = document.getElementById('campaignGrid_' + uid);
    if (!container) return;

    let html = '';
    for (let si = 0; si < 5; si++) {
        const stageData = state.stages && state.stages[si] ? state.stages[si] : { completed: [] };
        html += `<div style="margin-bottom:5px;font-size:9px;color:#888;">
            <span style="display:inline-block;width:50px;font-weight:700;color:#f39c12;">S${si + 1}</span>`;
        for (let li = 0; li < 12; li++) {
            const completed = !!(stageData.completed && stageData.completed[li]);
            const unlocked = si <= state.unlockedStage;
            html += `<span
                onclick="${unlocked ? `toggleCampaignLevel('${uid}', ${si}, ${li})` : ''}"
                style="display:inline-block;width:18px;height:18px;line-height:18px;text-align:center;
                       margin:1px;border-radius:3px;cursor:${unlocked ? 'pointer' : 'default'};
                       background:${completed ? '#27ae60' : unlocked ? '#444' : '#222'};
                       color:${completed ? '#fff' : unlocked ? '#888' : '#444'};
                       border:1px solid ${completed ? '#2ecc71' : unlocked ? '#555' : '#2a2a2a'};
                       transition:all 0.1s;">${li + 1}</span>`;
        }
        if (state.stages && state.stages[si] && state.stages[si].records) {
            const recordKeys = Object.keys(state.stages[si].records);
            if (recordKeys.length > 0) {
                html += `<span style="margin-left:6px;color:#666;font-size:7px;">(${recordKeys.length} records)</span>`;
            }
        }
        html += `</div>`;
    }
    container.innerHTML = html;
};

window.toggleCampaignLevel = function(uid, stageIdx, levelIdx) {
    const state = _campaignEditState[uid];
    if (!state) return;

    if (!state.stages) state.stages = [];
    if (!state.stages[stageIdx]) {
        state.stages[stageIdx] = { completed: [] };
    }
    if (!state.stages[stageIdx].completed) {
        state.stages[stageIdx].completed = [];
    }

    state.stages[stageIdx].completed[levelIdx] = !state.stages[stageIdx].completed[levelIdx];
    renderCampaignGrid(uid);
};

window.saveUserCampaign = function(uid) {
    if (!requireAdmin('saveUserCampaign')) return;

    const unlockedInput = document.getElementById('campaign_unlockedStage');
    if (!unlockedInput) return;

    const unlockedStage = parseInt(unlockedInput.value) || 0;
    const editedState = _campaignEditState[uid];
    if (!editedState) return;

    editedState.unlockedStage = unlockedStage;

    // Normalize to full 5-stage structure so progression.js:getCampaignData() doesn't reject it
    const normalized = _ensureFullCampaign(editedState);

    const existing = cachedProgression[uid] || {};
    const progression = { ...existing };
    progression.campaign = normalized;
    progression.updatedAt = Date.now();

    db.ref('user_progression/' + uid).set(progression).then(() => {
        showToast('Campaign saved for ' + uid.slice(0, 12));
        cachedProgression[uid] = progression;
        _campaignEditState[uid] = JSON.parse(JSON.stringify(normalized));
    }).catch(e => showToast('Save failed: ' + e.message, true));
};

window.deleteUser = function(uid) {
  if (!requireAdmin('deleteUser')) return;
  if (!confirm('Delete user ' + uid + ' and all their data? This cannot be undone.')) return;
    const lb = cachedData.leaderboard || [];
    const lbPromises = lb.filter(e => e.uid === uid).map(e => db.ref('leaderboard/' + e.mode + '/' + uid).remove());
    const promises = [
        db.ref('users/' + uid).remove(),
        ...lbPromises,
        db.ref('user_progression/' + uid).remove()
    ];
    Promise.all(promises).then(() => {
        showToast('User deleted');
        delete cachedData.users[uid];
        cachedData.leaderboard = lb.filter(e => e.uid !== uid);
        delete cachedProgression[uid];
        renderUsers();
        renderLeaderboard();
        renderOverview();
    }).catch(e => showToast('Delete failed: ' + e.message, true));
};

// ===================== LEADERBOARD =====================
let _lbMode = 'all';

window.setLBMode = function(mode) {
    _lbMode = mode;
    document.querySelectorAll('.admin-lb-mode').forEach(b => {
        const isActive = b.dataset.mode === mode;
        b.style.background = isActive ? 'rgba(52,152,219,0.3)' : 'transparent';
        b.style.color = isActive ? '#f39c12' : '#999';
    });
    renderLeaderboard();
};

window.renderLeaderboard = function() {
    const lb = cachedData.leaderboard || [];
    const query = (document.getElementById('lbSearch').value || '').toLowerCase();
    const modeFilter = _lbMode;

    let entries = lb.filter(e => {
        if (query && !e.uid.toLowerCase().includes(query) && !(e.name || '').toLowerCase().includes(query)) return false;
        if (modeFilter !== 'all' && (e.mode || '') !== modeFilter) return false;
        return true;
    });
    entries.sort((a, b) => (b.score || 0) - (a.score || 0));

    let html = '';
    entries.forEach((e, i) => {
        const medal = i === 0 ? '🥇' : i === 1 ? '🥈' : i === 2 ? '🥉' : '';
        const isTest = isTestUid(e.uid);
        const users = cachedData.users || {};
        const friendCode = users[e.uid] ? users[e.uid].friendCode : null;
        html += `<tr${isTest ? ' style="opacity:0.5;"' : ''}>
            <td class="id-cell">${medal} ${isTest ? '🧪 ' : ''}${esc(friendCode || e.uid.slice(0, 12))}</td>
            <td>${esc(e.name || '—')}</td>
            <td><span class="mode-badge" style="font-size:9px;color:#888;background:rgba(255,255,255,0.06);padding:2px 6px;border-radius:3px;">${(e.mode || '?').toUpperCase()}</span></td>
            <td style="color:#27ae60;font-weight:700;">${(e.score || 0).toLocaleString()}</td>
            <td>Lv.${e.level || 1}</td>
            <td style="font-size:8px;color:#666;">${e.timestamp ? new Date(e.timestamp).toLocaleString() : '—'}</td>
            <td>
                <button class="btn-sm" onclick="editLBEntry('${e.uid}','${e.mode}')">EDIT</button>
                <button class="btn-sm danger" onclick="deleteLBEntry('${e.uid}','${e.mode}')">DELETE</button>
            </td>
        </tr>`;
    });
    document.getElementById('lbBody').innerHTML = html || '<tr><td colspan="7" style="text-align:center;color:#555;padding:20px;">No leaderboard entries.</td></tr>';
};

window.editLBEntry = function(uid, mode) {
    const lb = cachedData.leaderboard || [];
    const e = lb.find(x => x.uid === uid && x.mode === mode) || {};
    showModal('Edit Leaderboard Entry', `
        <div class="field"><label>Mode</label><input id="lbEditMode" value="${esc(mode)}" style="color:#888;background:#222;"></div>
        <div class="field"><label>Name</label><input id="lbEditName" value="${esc(e.name || '')}"></div>
        <div class="field"><label>Score</label><input id="lbEditScore" value="${e.score || 0}"></div>
        <div class="field"><label>Level</label><input id="lbEditLevel" value="${e.level || 1}"></div>
        <div class="modal-actions">
            <button class="btn-sm" onclick="closeModal()">CANCEL</button>
            <button class="btn-sm success" onclick="saveLBEntry('${uid}','${mode}')">SAVE</button>
        </div>
    `);
};

window.saveLBEntry = function(uid, mode) {
    const name = document.getElementById('lbEditName').value.trim();
    const score = parseInt(document.getElementById('lbEditScore').value) || 0;
    const level = parseInt(document.getElementById('lbEditLevel').value) || 1;
    const lb = cachedData.leaderboard || [];
    const existing = lb.find(x => x.uid === uid && x.mode === mode);
    const timestamp = existing ? existing.timestamp : Date.now();

    db.ref('leaderboard/' + mode + '/' + uid).set({ name, score, level, timestamp }).then(() => {
        showToast('Leaderboard entry saved');
        closeModal();
        if (existing) {
            existing.name = name;
            existing.score = score;
            existing.level = level;
            existing.timestamp = timestamp;
        } else {
            lb.push({ uid, mode, name, score, level, timestamp });
        }
        renderLeaderboard();
        renderOverview();
    }).catch(e => showToast('Save failed: ' + e.message, true));
};

window.deleteLBEntry = function(uid, mode) {
    if (!confirm('Delete leaderboard entry for ' + uid + ' (' + mode + ')?')) return;
    db.ref('leaderboard/' + mode + '/' + uid).remove().then(() => {
        showToast('Entry deleted');
        cachedData.leaderboard = (cachedData.leaderboard || []).filter(e => !(e.uid === uid && e.mode === mode));
        renderLeaderboard();
        renderOverview();
    }).catch(e => showToast('Delete failed: ' + e.message, true));
};

// ===================== PROGRESSION (self) =====================
function loadProgressionTab() {
    let P;
    try {
        const raw = localStorage.getItem('tankBattle_progression');
        P = raw ? JSON.parse(raw) : null;
    } catch(e) { P = null; }

    const fields = document.getElementById('progFields');
    const upgrades = document.getElementById('progUpgrades');
    const inventory = document.getElementById('progInventory');

    if (!P) {
        fields.innerHTML = '<p style="color:#666;">No local progression data found. Play a game first to create progression data.</p>';
        upgrades.innerHTML = '';
        inventory.innerHTML = '';
        return;
    }

    // Currency fields
    const currencyFields = ['gems', 'coins', 'xp', 'upgradePoints', 'highestSingleScore', 'totalKills', 'aiWins'];
    let html = '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">';
    currencyFields.forEach(key => {
        html += `<div class="field"><label>${key}</label>
            <input id="prog_${key}" type="number" value="${P[key] || 0}" style="width:100%;"
                onchange="saveProgressionField('${key}')"></div>`;
    });
    html += '</div>';
    fields.innerHTML = html;

    // Upgrade levels
    if (P.upgrades) {
        let uhtml = '<p style="color:#888;font-size:9px;letter-spacing:1px;margin-bottom:6px;">UPGRADES</p>';
        uhtml += '<div style="display:flex;gap:10px;flex-wrap:wrap;">';
        Object.keys(P.upgrades).forEach(key => {
            uhtml += `<div class="field" style="flex:1;min-width:80px;"><label>${key}</label>
                <input id="prog_upgrade_${key}" type="number" value="${P.upgrades[key] || 0}"
                    onchange="saveProgressionUpgrade('${key}')"></div>`;
        });
        uhtml += '</div>';
        upgrades.innerHTML = uhtml;
    }

    // Inventory
    let ihtml = '<p style="color:#888;font-size:9px;letter-spacing:1px;margin-bottom:6px;">INVENTORY</p>';
    ['ownedSkins', 'equippedSkin', 'ownedWeapons', 'equippedWeapon'].forEach(key => {
        const val = Array.isArray(P[key]) ? P[key].join(', ') : P[key];
        ihtml += `<div class="field"><label>${key}</label>
            <input id="prog_inv_${key}" value="${esc(val || '')}"
                onchange="saveProgressionInventory('${key}')"></div>`;
    });
    inventory.innerHTML = ihtml;
}

window.saveProgressionField = function(key) {
    const val = parseInt(document.getElementById('prog_' + key).value) || 0;
    updateLocalProgression(key, val);
};

window.saveProgressionUpgrade = function(key) {
    const val = parseInt(document.getElementById('prog_upgrade_' + key).value) || 0;
    const keyPath = 'upgrades.' + key;
    updateLocalProgression(keyPath, val);
};

window.saveProgressionInventory = function(key) {
    const raw = document.getElementById('prog_inv_' + key).value;
    let val;
    if (key === 'equippedSkin' || key === 'equippedWeapon') {
        val = raw.trim();
    } else {
        val = raw.split(',').map(s => s.trim()).filter(Boolean);
    }
    updateLocalProgression(key, val);
};

function updateLocalProgression(key, val) {
    try {
        const raw = localStorage.getItem('tankBattle_progression');
        const P = raw ? JSON.parse(raw) : {};
        const keys = key.split('.');
        if (keys.length === 1) {
            P[keys[0]] = val;
        } else {
            if (!P[keys[0]]) P[keys[0]] = {};
            P[keys[0]][keys[1]] = val;
        }
        localStorage.setItem('tankBattle_progression', JSON.stringify(P));
        showToast(key + ' = ' + val);
    } catch(e) {
        showToast('Save failed: ' + e.message, true);
    }
}

// ===================== LOBBIES =====================
function renderLobbies() {
    const lobbies = cachedData.lobbies || {};
    const ids = Object.keys(lobbies);
    ids.sort();

    let html = '';
    ids.forEach(id => {
        const l = lobbies[id];
        const playerCount = l.players ? Object.keys(l.players).length : 0;
        const players = l.players ? Object.keys(l.players).map(pid => l.players[pid].name || pid.slice(0, 8)).join(', ') : '—';
        html += `<tr>
            <td class="id-cell">${esc(id.slice(0, 24))}…</td>
            <td>${esc(l.code || '—')}</td>
            <td>${esc(l.mode || '—')}</td>
            <td>${esc(l.status || '—')}</td>
            <td>${playerCount} (${esc(players)})</td>
            <td>
                <button class="btn-sm danger" onclick="deleteLobby('${id}')">DELETE</button>
            </td>
        </tr>`;
    });
    document.getElementById('lobbiesBody').innerHTML = html || '<tr><td colspan="6" style="text-align:center;color:#555;padding:20px;">No lobbies.</td></tr>';
}

window.deleteLobby = function(id) {
    if (!confirm('Delete lobby ' + id + '?')) return;
    db.ref('lobbies/' + id).remove().then(() => {
        showToast('Lobby deleted');
        delete cachedData.lobbies[id];
        renderLobbies();
        renderOverview();
    }).catch(e => showToast('Delete failed: ' + e.message, true));
};

// ===================== CONTROLS =====================
window.loadControlsData = function() {
    const panel = document.getElementById('panel-controls');
    if (!panel || !panel.classList.contains('active')) return;
    document.getElementById('controlsBody').innerHTML = '<tr><td colspan="15" class="loading">Loading...</td></tr>';

    db.ref('user_progression').once('value').then(snap => {
        const allProg = snap.val() || {};
        Object.keys(allProg).forEach(uid => { cachedProgression[uid] = allProg[uid]; });
        Object.keys(cachedData.users).forEach(uid => {
            if (!cachedProgression[uid]) cachedProgression[uid] = {};
        });
        renderControls();
    }).catch(e => {
        document.getElementById('controlsBody').innerHTML = '<tr><td colspan="15" style="text-align:center;color:#e94560;padding:20px;">Error: ' + esc(e.message) + '</td></tr>';
    });
}

window.renderControls = function() {
    const users = cachedData.users || {};
    const query = (document.getElementById('controlsSearch').value || '').toLowerCase();

    let uids = Object.keys(users);
    if (query) {
        uids = uids.filter(uid => {
            const u = users[uid];
            return uid.toLowerCase().includes(query)
                || (u.name || '').toLowerCase().includes(query)
                || (u.friendCode || '').toLowerCase().includes(query);
        });
    }
    uids.sort();

    const isTest = (uid) => uid.startsWith('test_user_') || uid === 'debug_test_entry';

    let html = '';
    uids.forEach(uid => {
        const u = users[uid];
        const name = u.name || uid.slice(0, 12);
        const p = cachedProgression[uid] || {};
        const campaign = p.campaign || {};
        const test = isTest(uid);
        const rowStyle = test ? 'opacity:0.5;' : '';

        const ownedSkins = (p.ownedSkins || ['classic']).join(', ');
        const equippedSkin = p.equippedSkin || 'classic';
        const ownedWeapons = (p.ownedWeapons || ['standard']).join(', ');
        const equippedWeapon = p.equippedWeapon || 'standard';

        html += `<tr style="${rowStyle}">
            <td class="id-cell">${test ? '🧪 ' : ''}${esc(u.friendCode || uid.slice(0, 8))}</td>
            <td>${esc(name)}</td>
            <td><input class="ctrl-input" id="ctrl_${uid}_gems" type="number" value="${p.gems || 0}"></td>
            <td><input class="ctrl-input" id="ctrl_${uid}_coins" type="number" value="${p.coins || 0}"></td>
            <td><input class="ctrl-input" id="ctrl_${uid}_xp" type="number" value="${p.xp || 0}"></td>
            <td><input class="ctrl-input" id="ctrl_${uid}_speed" type="number" value="${(p.upgrades && p.upgrades.speed) || 0}"></td>
            <td><input class="ctrl-input" id="ctrl_${uid}_fuel" type="number" value="${(p.upgrades && p.upgrades.fuel) || 0}"></td>
            <td><input class="ctrl-input" id="ctrl_${uid}_mineRadius" type="number" value="${(p.upgrades && p.upgrades.mineRadius) || 0}"></td>
            <td><input class="ctrl-input" id="ctrl_${uid}_upgradePoints" type="number" value="${p.upgradePoints || 0}"></td>
            <td><input class="ctrl-input" id="ctrl_${uid}_tankStage" type="number" min="1" value="${p.tankStage || 1}" style="width:50px;"></td>
            <td><input class="ctrl-input" id="ctrl_${uid}_unlockedStage" type="number" min="0" max="4" value="${campaign.unlockedStage || 0}" style="width:50px;"></td>
            <td><input class="ctrl-input" id="ctrl_${uid}_ownedSkins" value="${esc(ownedSkins)}" style="width:90px;font-size:9px;"></td>
            <td><input class="ctrl-input" id="ctrl_${uid}_equippedSkin" value="${esc(equippedSkin)}" style="width:70px;font-size:9px;"></td>
            <td><input class="ctrl-input" id="ctrl_${uid}_ownedWeapons" value="${esc(ownedWeapons)}" style="width:90px;font-size:9px;"></td>
            <td><input class="ctrl-input" id="ctrl_${uid}_equippedWeapon" value="${esc(equippedWeapon)}" style="width:70px;font-size:9px;"></td>
            <td style="white-space:nowrap;">
                <button class="btn-sm" onclick="viewUser('${uid}')" style="border-color:#3498db;">FULL</button>
                <button class="btn-sm success" onclick="saveControl('${uid}')" style="border-color:#27ae60;">SAVE</button>
            </td>
        </tr>`;
    });

    document.getElementById('controlsBody').innerHTML = html || '<tr><td colspan="15" style="text-align:center;color:#555;padding:20px;">No users found.</td></tr>';
};

window.saveControl = function(uid) {
    const getNum = (id) => parseInt(document.getElementById(id).value) || 0;
    const getStr = (id) => document.getElementById(id).value.trim();

    // Start from existing data so admin saves don't wipe out missions, stats, etc.
    const existing = cachedProgression[uid] || {};
    const progression = { ...existing };

    progression.gems = getNum('ctrl_' + uid + '_gems');
    progression.coins = getNum('ctrl_' + uid + '_coins');
    progression.xp = getNum('ctrl_' + uid + '_xp');
    progression.upgradePoints = getNum('ctrl_' + uid + '_upgradePoints');
    progression.tankStage = getNum('ctrl_' + uid + '_tankStage');
    progression.upgrades = {
        speed: getNum('ctrl_' + uid + '_speed'),
        fuel: getNum('ctrl_' + uid + '_fuel'),
        mineRadius: getNum('ctrl_' + uid + '_mineRadius')
    };

    const existingCampaign = existing.campaign || {};
    progression.campaign = _ensureFullCampaign({
        ...existingCampaign,
        unlockedStage: getNum('ctrl_' + uid + '_unlockedStage')
    });

    progression.ownedSkins = getStr('ctrl_' + uid + '_ownedSkins').split(',').map(s => s.trim()).filter(Boolean);
    progression.equippedSkin = getStr('ctrl_' + uid + '_equippedSkin');
    progression.ownedWeapons = getStr('ctrl_' + uid + '_ownedWeapons').split(',').map(s => s.trim()).filter(Boolean);
    progression.equippedWeapon = getStr('ctrl_' + uid + '_equippedWeapon');
    progression.updatedAt = Date.now();

    db.ref('user_progression/' + uid).set(progression).then(() => {
        showToast('Saved: ' + uid.slice(0, 12));
        cachedProgression[uid] = progression;
    }).catch(e => showToast('Save failed: ' + e.message, true));
};

// ===================== RAW DATA =====================
window.loadRaw = function() {
    const path = document.getElementById('rawPath').value.trim() || '/';
    const ref = db.ref(path);
    ref.once('value').then(snap => {
        const val = snap.val();
        document.getElementById('rawOutput').textContent = val ? JSON.stringify(val, null, 2) : '(empty / null)';
    }).catch(e => {
        document.getElementById('rawOutput').textContent = 'Error: ' + e.message;
    });
};

// ===================== FEEDBACK =====================
const NVIDIA_KEY_STORAGE = 'tankBattle_nvidiaKey';

window.saveNvidiaKey = function() {
    const key = document.getElementById('nvidiaApiKey').value.trim();
    if (key) {
        try { localStorage.setItem(NVIDIA_KEY_STORAGE, key); } catch(e) {}
    } else {
        try { localStorage.removeItem(NVIDIA_KEY_STORAGE); } catch(e) {}
    }
};

function loadNvidiaKey() {
    try {
        const key = localStorage.getItem(NVIDIA_KEY_STORAGE);
        if (key) {
            document.getElementById('nvidiaApiKey').value = key;
        }
    } catch(e) {}
}

window.renderFeedback = function() {
    const feedback = cachedData.feedback || {};
    const query = (document.getElementById('feedbackSearch').value || '').toLowerCase();

    let ids = Object.keys(feedback);
    ids.sort((a, b) => {
        const ta = feedback[a].timestamp || 0;
        const tb = feedback[b].timestamp || 0;
        return typeof ta === 'number' ? tb - ta : 0;
    });

    if (query) {
        ids = ids.filter(id => {
            const f = feedback[id];
            return (f.message || '').toLowerCase().includes(query)
                || (f.type || '').toLowerCase().includes(query)
                || (f.displayName || f.email || '').toLowerCase().includes(query);
        });
    }

    document.getElementById('feedbackCount').textContent = ids.length + ' entries';

    let html = '';
    ids.forEach(id => {
        const f = feedback[id];
        const ts = f.timestamp ? new Date(f.timestamp).toLocaleString() : '—';
        const type = f.type || 'other';
        const typeLabels = { suggestion: '💡', bug: '🐛', feature: '✨', other: '📝' };
        const typeIcon = typeLabels[type] || '📝';
        const user = f.displayName || f.email || f.uid ? (f.uid || '').slice(0, 8) : 'Anonymous';
        const msg = f.message || '(empty)';
        const shortMsg = msg.length > 120 ? msg.slice(0, 120) + '…' : msg;

        html += `<tr>
            <td style="font-size:8px;color:#666;white-space:nowrap;">${ts}</td>
            <td class="id-cell">${esc(user)}${f.email ? '<br><span style="color:#666;font-size:7px;">' + esc(f.email) + '</span>' : ''}</td>
            <td>${typeIcon} ${type}</td>
            <td class="val-cell" style="max-width:300px;" title="${esc(msg)}">${esc(shortMsg)}</td>
            <td>
                <button class="btn-sm" onclick="viewFeedback('${id}')">VIEW</button>
                <button class="btn-sm danger" onclick="deleteFeedback('${id}')">DELETE</button>
            </td>
        </tr>`;
    });
    document.getElementById('feedbackBody').innerHTML = html || '<tr><td colspan="5" style="text-align:center;color:#555;padding:20px;">No feedback yet.</td></tr>';
};

window.viewFeedback = function(id) {
    const f = cachedData.feedback[id];
    if (!f) return;
    const ts = f.timestamp ? new Date(f.timestamp).toLocaleString() : '—';
    const typeLabels = { suggestion: '💡 Suggestion', bug: '🐛 Bug Report', feature: '✨ Feature Request', other: '📝 Other' };
    const typeLabel = typeLabels[f.type] || f.type || 'Other';
    showModal('Feedback Details', `
        <div style="display:grid;gap:6px;font-size:10px;">
            <div><span style="color:#888;">Date:</span> ${ts}</div>
            <div><span style="color:#888;">Type:</span> ${typeLabel}</div>
            <div><span style="color:#888;">User:</span> ${esc(f.displayName || '—')}</div>
            <div><span style="color:#888;">Email:</span> ${esc(f.email || '—')}</div>
            <div><span style="color:#888;">UID:</span> <span style="font-size:8px;color:#666;">${esc(f.uid || '—')}</span></div>
            <div style="margin-top:8px;"><span style="color:#888;">Message:</span></div>
            <div style="background:rgba(0,0,0,0.3);border:1px solid #222;border-radius:4px;padding:10px;font-size:10px;color:#bdc3c7;white-space:pre-wrap;line-height:1.5;">${esc(f.message || '(empty)')}</div>
        </div>
        <div class="modal-actions">
            <button class="btn-sm" onclick="closeModal()">CLOSE</button>
        </div>
    `);
};

window.deleteFeedback = function(id) {
    if (!requireAdmin('deleteFeedback')) return;
    if (!confirm('Delete this feedback entry?')) return;
    db.ref('feedback/' + id).remove().then(() => {
        showToast('Feedback deleted');
        delete cachedData.feedback[id];
        renderFeedback();
    }).catch(e => showToast('Delete failed: ' + e.message, true));
};

window.clearAllFeedback = function() {
    if (!requireAdmin('clearAllFeedback')) return;
    if (!confirm('Delete ALL feedback entries? This cannot be undone.')) return;
    db.ref('feedback').remove().then(() => {
        showToast('All feedback cleared');
        cachedData.feedback = {};
        renderFeedback();
    }).catch(e => showToast('Clear failed: ' + e.message, true));
};

window.analyzeFeedback = function() {
    const apiKey = document.getElementById('nvidiaApiKey').value.trim();
    if (apiKey) {
        try { localStorage.setItem(NVIDIA_KEY_STORAGE, apiKey); } catch(e) {}
    }
    if (!apiKey) {
        document.getElementById('aiAnalysisResult').innerHTML = '<span style="color:#e74c3c;">Please enter your NVIDIA API key above.</span>';
        return;
    }

    const feedback = cachedData.feedback || {};
    const entries = Object.values(feedback);
    if (!entries.length) {
        document.getElementById('aiAnalysisResult').innerHTML = '<span style="color:#888;">No feedback to analyze.</span>';
        return;
    }

    const btn = document.getElementById('analyzeBtn');
    btn.disabled = true;
    btn.textContent = 'ANALYZING...';
    document.getElementById('aiAnalysisResult').innerHTML = '<span style="color:#888;">Analyzing ' + entries.length + ' entries with AI...</span>';

    const feedbackSummary = entries.map((f, i) => {
        const type = f.type || 'other';
        const msg = (f.message || '').slice(0, 500);
        return `[${i + 1}] Type: ${type}\nMessage: ${msg}\n`;
    }).join('\n');

    const prompt = `You are analyzing player feedback for a tank battle game. Analyze the following feedback entries and provide:
1. TOP ISSUES — most frequently mentioned problems or bugs (if any)
2. TOP REQUESTS — most requested features or improvements
3. SENTIMENT — overall sentiment analysis
4. ACTION ITEMS — specific, prioritized recommendations for what to improve first

Keep it concise and actionable. Use bullet points.

FEEDBACK ENTRIES:
${feedbackSummary}`;

    fetch('https://integrate.api.nvidia.com/v1/chat/completions', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + apiKey
        },
        body: JSON.stringify({
            model: 'meta/llama-3.1-8b-instruct',
            messages: [
                { role: 'system', content: 'You are a game analytics expert who provides concise, actionable feedback analysis.' },
                { role: 'user', content: prompt }
            ],
            temperature: 0.3,
            max_tokens: 1024,
            top_p: 0.9
        })
    }).then(res => {
        if (!res.ok) {
            return res.text().then(text => {
                throw new Error('API error (' + res.status + '): ' + text.slice(0, 200));
            });
        }
        return res.json();
    }).then(data => {
        const result = data.choices && data.choices[0] && data.choices[0].message ? data.choices[0].message.content : 'No response from AI.';
        document.getElementById('aiAnalysisResult').innerHTML = result.replace(/\n/g, '<br>');
    }).catch(err => {
        document.getElementById('aiAnalysisResult').innerHTML = '<span style="color:#e74c3c;">Analysis failed: ' + esc(err.message) + '</span>';
    }).finally(() => {
        btn.disabled = false;
        btn.textContent = '🔍 ANALYZE WITH AI';
    });
};

// ===================== MODAL =====================
let modalOverlay, modalContent;

function showModal(title, body) {
    if (!modalOverlay) {
        modalOverlay = document.createElement('div');
        modalOverlay.className = 'modal-overlay';
        modalOverlay.id = 'modalOverlay';
        modalOverlay.innerHTML = '<div class="modal-box"><h2 id="modalTitle"></h2><div id="modalContent"></div></div>';
        document.body.appendChild(modalOverlay);
        modalContent = document.getElementById('modalContent');
        modalOverlay.addEventListener('click', e => { if (e.target === modalOverlay) closeModal(); });
    }
    document.getElementById('modalTitle').textContent = title;
    modalContent.innerHTML = body;
    modalOverlay.classList.add('open');
}

window.closeModal = function() {
    if (modalOverlay) modalOverlay.classList.remove('open');
};

// ===================== HELPERS =====================
function esc(s) {
    if (typeof s !== 'string') return String(s || '');
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ===================== START =====================
document.addEventListener('DOMContentLoaded', init);
