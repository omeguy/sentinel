const API_BASE = "";

window.onload = () => {
    document.getElementById('api-key').value = localStorage.getItem('tracy_api_key') || "";
    document.getElementById('user-id').value = localStorage.getItem('tracy_user_id') || "";
};

function saveCredentials() {
    localStorage.setItem('tracy_api_key', document.getElementById('api-key').value);
    localStorage.setItem('tracy_user_id', document.getElementById('user-id').value);
    alert("Credentials Saved!");
}

async function request(path, method, body = null) {
    const key = document.getElementById('api-key').value;
    const output = document.getElementById('json-output');
    output.innerText = "// Connecting to Sentinel...";

    try {
        const res = await fetch(`${API_BASE}${path}`, {
            method: method,
            headers: { 'x-api-key': key, 'Content-Type': 'application/json' },
            body: body ? JSON.stringify(body) : null
        });
        const data = await res.json();
        output.innerText = JSON.stringify(data, null, 4);
    } catch (err) {
        output.innerText = "Connection Error: " + err.message + "\nCheck CORS settings in backend.";
    }
}

function openForm(type) {
    const area = document.getElementById('work-area');
    const body = document.getElementById('work-body');
    const title = document.getElementById('work-title');
    const confirmBtn = document.getElementById('work-confirm');
    const user = document.getElementById('user-id').value;

    area.classList.remove('hidden');
    body.innerHTML = "";

    if (type === 'create') {
        title.innerText = "Create Bot Configuration";
        body.innerHTML = `<textarea id="payload" style="width:100%; height:250px; font-family:monospace;">${JSON.stringify(DEFAULT_TEMPLATE, null, 4)}</textarea>`;
        confirmBtn.onclick = () => {
            const json = JSON.parse(document.getElementById('payload').value);
            request(`/v1/users/${user}/bots`, 'POST', json);
        };
    } else {
        title.innerText = `Target Bot for ${type.toUpperCase()}`;
        body.innerHTML = `<input type="text" id="bot-target" style="width:100%; padding:12px; border:1px solid #ddd;" placeholder="Enter Bot Name">`;
        confirmBtn.onclick = () => {
            const bot = document.getElementById('bot-target').value;
            let path = `/v1/users/${user}/bots/${bot}`;
            let method = "GET";
            if (type === 'start') { path += "/engine/start"; method = "POST"; }
            if (type === 'stop') { path += "/engine/stop"; method = "POST"; }
            if (type === 'logs') { path += "/logs"; }
            request(path, method);
        };
    }
}

function runHealth() { request('/health', 'GET'); }
function runEnsureDB() { request('/v1/infra/bot-db/ensure', 'POST'); }
function closeWorkArea() { document.getElementById('work-area').classList.add('hidden'); }
function clearOutput() { document.getElementById('json-output').innerText = "// Ready."; }

const DEFAULT_TEMPLATE = {
    "bot_name": "tracy_bot",
    "env": {
        "APP_MODE": "dev",
        "BOT_NODE_MAP": "DemoStrategy:forex:EURUSD",
        "MT5_LOGIN": "123456",
        "MT5_PASSWORD": "password",
        "MT5_SERVER": "Demo-MT5"
    },
    "enable_vnc": true,
    "persist_volume": true,
    "enable_novnc": false
};