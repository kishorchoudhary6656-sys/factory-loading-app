from flask import Flask, render_template_string, request, redirect, session, Response
import sqlite3, csv, io, re
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

app = Flask(__name__)
app.secret_key = 'real_instant_foods_final_2026'

IST = timezone(timedelta(hours=5, minutes=30))
def now_ist():
    return datetime.now(timezone.utc).astimezone(IST)

def init_db():
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS po_items (id INTEGER PRIMARY KEY AUTOINCREMENT, po_number TEXT, item_name TEXT, weight TEXT, ordered_qty INTEGER, barcode TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS dispatch_log (id INTEGER PRIMARY KEY AUTOINCREMENT, po_number TEXT, vehicle_no TEXT, location TEXT, product_name TEXT, loaded_qty INTEGER, timestamp TEXT)''')
    # Shared, global state (NOT per-browser) so every device sees the same active dispatch session live
    cursor.execute('''CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT)''')
    # Companies (Zepto, Flipkart, Reliance, Anand Sweets, etc.) — each PO belongs to one company/folder
    cursor.execute('''CREATE TABLE IF NOT EXISTS companies (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE)''')
    # Add barcode column to dispatch_log if migrating from an older schema
    try:
        cursor.execute('ALTER TABLE dispatch_log ADD COLUMN barcode TEXT')
    except sqlite3.OperationalError:
        pass
    # Add company column to po_items if migrating from an older schema
    try:
        cursor.execute('ALTER TABLE po_items ADD COLUMN company TEXT')
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()
init_db()

def get_active_session():
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM app_state WHERE key IN ('cur_po', 'cur_vehicle', 'cur_location')")
    rows = dict(cursor.fetchall())
    conn.close()
    return {
        'cur_po': rows.get('cur_po', ''),
        'cur_vehicle': rows.get('cur_vehicle', ''),
        'cur_location': rows.get('cur_location', ''),
    }

def set_active_session(po_number, vehicle_no, location):
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    for key, value in [('cur_po', po_number), ('cur_vehicle', vehicle_no), ('cur_location', location)]:
        cursor.execute('INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, value))
    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# Shared design system (plain CSS - no Jinja braces used, safe to concatenate)
# ---------------------------------------------------------------------------
STYLE_BLOCK = """
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
    :root {
        --primary: #3b82f6;
        --primary-dark: #2563eb;
        --accent: #8b5cf6;
        --bg: #0b1120;
        --bg-glow: radial-gradient(circle at 15% 0%, rgba(59,130,246,0.16), transparent 45%),
                   radial-gradient(circle at 85% 20%, rgba(139,92,246,0.14), transparent 45%);
        --card: rgba(30,41,59,0.65);
        --card-solid: #1e293b;
        --card-alt: #263349;
        --border: rgba(148,163,184,0.16);
        --text: #f8fafc;
        --text-muted: #94a3b8;
        --success: #22c55e;
        --warn: #f59e0b;
        --danger: #ef4444;
        --radius: 16px;
    }
    * { box-sizing: border-box; }
    body {
        font-family: 'Inter', system-ui, sans-serif;
        background-color: var(--bg);
        background-image: var(--bg-glow);
        background-attachment: fixed;
        color: var(--text);
        margin: 0;
        padding: 0 20px 60px;
        min-height: 100vh;
    }
    .container { max-width: 1150px; margin: 0 auto; }

    .topbar {
        display: flex; justify-content: space-between; align-items: center;
        padding: 22px 0 18px; flex-wrap: wrap; gap: 14px;
    }
    .brand { display: flex; align-items: center; gap: 13px; }
    .brand-logo {
        width: 46px; height: 46px; border-radius: 12px;
        background: linear-gradient(135deg, var(--primary), var(--accent));
        display: flex; align-items: center; justify-content: center;
        font-weight: 800; font-size: 17px; letter-spacing: -0.5px;
        box-shadow: 0 6px 18px rgba(59,130,246,0.35);
    }
    .brand h1 { font-size: 19px; margin: 0; font-weight: 700; letter-spacing: -0.2px; }
    .brand span { font-size: 12px; color: var(--text-muted); font-weight: 500; }

    .nav {
        display: flex; gap: 6px; background: var(--card); border: 1px solid var(--border);
        padding: 5px; border-radius: 12px; backdrop-filter: blur(12px);
    }
    .nav a {
        color: var(--text-muted); text-decoration: none; padding: 9px 16px;
        border-radius: 8px; font-size: 13.5px; font-weight: 600; transition: all 0.15s;
    }
    .nav a:hover { color: var(--text); background: rgba(255,255,255,0.04); }
    .nav a.active { color: white; background: linear-gradient(135deg, var(--primary), var(--primary-dark)); }

    .btn {
        background: linear-gradient(135deg, var(--primary), var(--primary-dark));
        color: white; font-weight: 600; border: none; border-radius: 10px;
        padding: 12px 20px; cursor: pointer; font-size: 14px; font-family: inherit;
        transition: transform 0.12s, box-shadow 0.12s; box-shadow: 0 4px 14px rgba(59,130,246,0.28);
    }
    .btn:hover { transform: translateY(-1px); box-shadow: 0 6px 20px rgba(59,130,246,0.4); }
    .btn:active { transform: translateY(0); }
    .btn-outline {
        background: rgba(255,255,255,0.03); border: 1px solid var(--border); color: var(--text);
        box-shadow: none;
    }
    .btn-outline:hover { background: rgba(255,255,255,0.07); box-shadow: none; }
    .btn-danger { background: rgba(239,68,68,0.12); color: #fca5a5; border: 1px solid rgba(239,68,68,0.25); box-shadow: none; }
    .btn-danger:hover { background: rgba(239,68,68,0.2); box-shadow: none; }
    .btn-sm { padding: 8px 14px; font-size: 12.5px; border-radius: 8px; }
    .btn-block { width: 100%; }

    .stats-grid {
        display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
        gap: 16px; margin-bottom: 26px;
    }
    .stat-card {
        background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
        padding: 20px; backdrop-filter: blur(12px); position: relative; overflow: hidden;
    }
    .stat-card::before {
        content: ''; position: absolute; top: -30px; right: -30px; width: 90px; height: 90px;
        border-radius: 50%; background: radial-gradient(circle, rgba(59,130,246,0.18), transparent 70%);
    }
    .stat-card .label { color: var(--text-muted); font-size: 12.5px; font-weight: 600; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.04em; }
    .stat-card .value { font-size: 30px; font-weight: 800; letter-spacing: -0.5px; }
    .stat-card .icon { font-size: 20px; margin-bottom: 10px; opacity: 0.9; }

    .card {
        background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
        padding: 22px; margin-bottom: 22px; backdrop-filter: blur(12px);
    }
    .card-header {
        display: flex; justify-content: space-between; align-items: center;
        margin-bottom: 18px; flex-wrap: wrap; gap: 10px;
    }
    .card-header h2 { font-size: 15.5px; margin: 0; font-weight: 700; display: flex; align-items: center; gap: 8px; }

    table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
    th {
        text-align: left; color: var(--text-muted); font-weight: 600; font-size: 11.5px;
        text-transform: uppercase; letter-spacing: 0.04em; padding: 10px 14px; border-bottom: 1px solid var(--border);
    }
    td { padding: 13px 14px; border-bottom: 1px solid var(--border); }
    tr:last-child td { border-bottom: none; }
    tbody tr { transition: background 0.12s; }
    tbody tr:hover td { background: rgba(255,255,255,0.025); }

    .badge {
        display: inline-block; border-radius: 7px; padding: 4px 10px; font-size: 12px; font-weight: 700;
    }
    .badge-green { background: rgba(34,197,94,0.14); color: #4ade80; }
    .badge-blue { background: rgba(59,130,246,0.14); color: #60a5fa; }
    .badge-amber { background: rgba(245,158,11,0.14); color: #fbbf24; }

    .progress-track { width: 100%; height: 8px; border-radius: 5px; background: rgba(255,255,255,0.06); overflow: hidden; margin-top: 6px; }
    .progress-fill { height: 100%; border-radius: 5px; background: linear-gradient(90deg, var(--primary), var(--accent)); transition: width 0.3s; }

    .empty-state { text-align: center; color: var(--text-muted); padding: 34px 10px; font-size: 13.5px; }

    #reader { width: 100%; border-radius: var(--radius); overflow: hidden; margin-bottom: 16px; display: none; border: 1px solid var(--border); }

    .modal {
        display: none; position: fixed; inset: 0; background: rgba(5,10,20,0.82);
        justify-content: center; align-items: center; z-index: 50; backdrop-filter: blur(4px);
    }
    .modal-content {
        background: var(--card-solid); border: 1px solid var(--border); padding: 28px;
        border-radius: var(--radius); width: 320px; max-width: 90vw; text-align: center;
        box-shadow: 0 20px 60px rgba(0,0,0,0.5);
    }
    .modal-content h3 { margin-top: 0; font-size: 16px; font-weight: 700; }

    input, select {
        width: 100%; padding: 12px 14px; margin: 8px 0; border-radius: 10px;
        border: 1px solid var(--border); background: rgba(255,255,255,0.03); color: var(--text);
        font-size: 14.5px; font-family: inherit; transition: border 0.15s;
    }
    input::placeholder { color: #64748b; }
    input:focus, select:focus { outline: none; border-color: var(--primary); }
    label { display: block; text-align: left; font-size: 12.5px; color: var(--text-muted); font-weight: 600; margin: 12px 0 2px; }

    .form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 4px 16px; align-items: end; }

    footer { text-align: center; color: var(--text-muted); font-size: 12px; margin-top: 34px; opacity: 0.7; }

    @keyframes pulse {
        0% { opacity: 1; box-shadow: 0 0 0 0 rgba(74,222,128,0.5); }
        70% { opacity: 0.6; box-shadow: 0 0 0 5px rgba(74,222,128,0); }
        100% { opacity: 1; box-shadow: 0 0 0 0 rgba(74,222,128,0); }
    }

    @media (max-width: 640px) {
        .topbar { flex-direction: column; align-items: flex-start; }
        .nav { width: 100%; overflow-x: auto; }
    }
</style>
"""

def nav_html(active):
    def cls(name):
        return "active" if active == name else ""
    return f"""
    <div class="nav">
        <a href="/" class="{cls('dashboard')}">Dashboard</a>
        <a href="/companies" class="{cls('companies')}">Companies</a>
        <a href="/pos" class="{cls('pos')}">Manage POs</a>
        <a href="/history" class="{cls('history')}">History</a>
    </div>
    """

TOPBAR_TEMPLATE = """
<div class="topbar">
    <div class="brand">
        <div class="brand-logo">RIF</div>
        <div>
            <h1>REAL INSTANT FOODS</h1>
            <span>AI Dispatch &amp; Packing ERP</span>
        </div>
    </div>
    __NAV__
</div>
"""

DASHBOARD_HTML = STYLE_BLOCK + """
<title>Dashboard | REAL INSTANT FOODS</title>
</head>
<body>
<div class="container">
""" + TOPBAR_TEMPLATE.replace("__NAV__", nav_html('dashboard')) + """
    <div class="stats-grid">
        <div class="stat-card">
            <div class="icon">📦</div>
            <div class="label">Active POs</div>
            <div class="value">{{ po_list|length }}</div>
        </div>
        <div class="stat-card">
            <div class="icon">🚚</div>
            <div class="label">Dispatch Entries</div>
            <div class="value">{{ logs|length }}</div>
        </div>
        <div class="stat-card">
            <div class="icon">✅</div>
            <div class="label">Total Bags Loaded</div>
            <div class="value">{{ total_loaded }}</div>
        </div>
    </div>

    <div id="reader"></div>

    <div class="card">
        <div class="card-header">
            <h2>🧾 Active Dispatch Session <span style="display:inline-flex; align-items:center; gap:5px; font-size:11px; font-weight:700; color:#4ade80; background:rgba(34,197,94,0.12); padding:3px 9px; border-radius:20px; vertical-align:middle; margin-left:4px;"><span style="width:6px; height:6px; border-radius:50%; background:#4ade80; display:inline-block; animation:pulse 1.5s infinite;"></span>LIVE</span></h2>
            {% if session_set %}
            <button class="btn btn-outline btn-sm" onclick="document.getElementById('sessionModal').style.display='flex'">Change</button>
            {% endif %}
        </div>
        {% if session_set %}
        <div style="display:flex; gap:28px; flex-wrap:wrap; margin-bottom:16px;">
            <div><div style="color:var(--text-muted); font-size:12px; font-weight:600; margin-bottom:3px;">PO NUMBER</div><div style="font-weight:700; font-size:15px;">{{ cur_po }}</div></div>
            <div><div style="color:var(--text-muted); font-size:12px; font-weight:600; margin-bottom:3px;">VEHICLE NO.</div><div style="font-weight:700; font-size:15px;">{{ cur_vehicle }}</div></div>
            <div><div style="color:var(--text-muted); font-size:12px; font-weight:600; margin-bottom:3px;">LOCATION</div><div style="font-weight:700; font-size:15px;">{{ cur_location }}</div></div>
        </div>
        <button class="btn" onclick="startScanner()">⚡ Start AI Scan</button>
        <div style="display:flex; gap:8px; margin-top:12px; align-items:center;">
            <input type="text" id="manualBarcodeInput" placeholder="Or type barcode manually" style="margin:0;">
            <button class="btn btn-outline" style="white-space:nowrap;" onclick="lookupManualBarcode()">Look Up</button>
        </div>
        {% else %}
        <div class="empty-state" style="padding-bottom:10px;">Set the PO, vehicle, and location before starting a scan.</div>
        <button class="btn" onclick="document.getElementById('sessionModal').style.display='flex'">Set Session &amp; Start</button>
        {% endif %}
    </div>

    <div class="card">
        <div class="card-header">
            <h2>📊 Item Progress (Ordered vs Loaded)</h2>
            <a href="/export_progress_csv" class="btn btn-outline btn-sm" style="text-decoration:none;">⬇ Export Progress CSV</a>
        </div>
        {% if item_progress|length > 0 %}
        {% for p in item_progress %}
        <div style="margin-bottom:16px;">
            <div style="display:flex; justify-content:space-between; font-size:13.5px; margin-bottom:2px; gap:10px;">
                <span style="font-weight:600;">{{ p.item_name }} <span style="color:var(--text-muted); font-weight:500;">({{ p.po_number }}{% if p.company %} &middot; {{ p.company }}{% endif %})</span></span>
                <span style="color:var(--text-muted); white-space:nowrap;">{{ p.dispatched }} / {{ p.ordered }} &middot; {{ p.pending }} pending</span>
            </div>
            <div class="progress-track"><div class="progress-fill" style="width:{{ p.percent }}%;"></div></div>
        </div>
        {% endfor %}
        {% else %}
        <div class="empty-state">No PO items added yet.</div>
        {% endif %}
    </div>

    <div class="card">
        <div class="card-header">
            <h2>🚚 Dispatch Log</h2>
            <a href="/export_csv" class="btn btn-outline btn-sm" style="text-decoration:none;">⬇ Export CSV</a>
        </div>
        {% if logs|length > 0 %}
        <table>
            <thead><tr>
                <th>PO Number</th><th>Vehicle No.</th><th>Location</th><th>Product</th><th>Qty Loaded</th><th>Time</th><th></th>
            </tr></thead>
            <tbody>
            {% for log in logs %}
            <tr>
                <td>{{ log[1] or '—' }}</td>
                <td>{{ log[2] or '—' }}</td>
                <td>{{ log[3] or '—' }}</td>
                <td>{{ log[4] }}</td>
                <td><span class="badge badge-green">{{ log[5] }}</span></td>
                <td style="color:var(--text-muted);">{{ log[6] }}</td>
                <td style="white-space:nowrap;">
                    <button class="btn btn-outline btn-sm" onclick="openEditLog('{{ log[0] }}', '{{ log[5] }}')">Edit</button>
                    <form method="POST" action="/dispatch/delete/{{ log[0] }}" style="display:inline;" onsubmit="return confirm('Delete this entry?');">
                        <button type="submit" class="btn btn-danger btn-sm">Delete</button>
                    </form>
                </td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <div class="empty-state">No dispatch entries yet.</div>
        {% endif %}
    </div>

    <footer>REAL INSTANT FOODS &middot; AI ERP System</footer>
</div>

<div id="editLogModal" class="modal">
    <div class="modal-content">
        <h3>Edit Loaded Quantity</h3>
        <form method="POST" id="editLogForm" action="/dispatch/edit/0">
            <input type="number" name="loaded_qty" id="editQtyInput" placeholder="Quantity" required>
            <button type="submit" class="btn btn-block" style="margin-top:8px;">Save</button>
        </form>
        <button class="btn btn-outline btn-block" style="margin-top:8px;" onclick="document.getElementById('editLogModal').style.display='none'">Cancel</button>
    </div>
</div>

<div id="scanModal" class="modal">
    <div class="modal-content">
        <h3>Item Scanned</h3>
        <div id="scannedItemInfo" style="background:rgba(255,255,255,0.04); border-radius:10px; padding:14px; margin:12px 0; text-align:left;">
            <div style="font-size:12px; color:var(--text-muted); font-weight:600;">ITEM</div>
            <div id="scannedItemName" style="font-weight:700; font-size:15px; margin-bottom:8px;">—</div>
            <div style="font-size:12px; color:var(--text-muted); font-weight:600;">WEIGHT</div>
            <div id="scannedItemWeight" style="font-weight:700; font-size:15px;">—</div>
        </div>
        <label id="qtyLabel" style="text-align:left;">How many bags were loaded?</label>
        <input type="number" id="qtyInput" placeholder="Quantity" value="1">
        <button class="btn btn-block" id="confirmLoadBtn" onclick="submitQty()">Confirm Load</button>
        <button class="btn btn-outline btn-block" style="margin-top:8px;" onclick="document.getElementById('scanModal').style.display='none'">Close</button>
    </div>
</div>

<div id="sessionModal" class="modal" style="{% if not session_set %}display:flex;{% endif %}">
    <div class="modal-content">
        <h3>Set Dispatch Session</h3>
        {% if po_list|length > 0 %}
        <form method="POST" action="/start_session">
            <select name="po_number" required>
                <option value="" disabled selected>Select PO Number</option>
                {% for po in po_list %}
                <option value="{{ po[0] }}">{{ po[0] }}{% if po[1] %} — {{ po[1] }}{% endif %}</option>
                {% endfor %}
            </select>
            <input type="text" name="vehicle_no" placeholder="Vehicle Number (e.g. KA-01-AB-1234)" required>
            <input type="text" name="location" placeholder="Location / Destination" required>
            <button type="submit" class="btn btn-block" style="margin-top:8px;">Start Scanning</button>
        </form>
        {% else %}
        <div class="empty-state" style="padding:10px 0 16px;">No POs added yet. Add a PO item on the "Manage POs" page first, then start a session here.</div>
        <a href="/pos" class="btn btn-block" style="text-decoration:none; display:block; box-sizing:border-box;">➕ Go to Manage POs</a>
        {% endif %}
        <button class="btn btn-outline btn-block" style="margin-top:8px;" onclick="document.getElementById('sessionModal').style.display='none'">Close</button>
    </div>
</div>

<script src="https://unpkg.com/html5-qrcode"></script>
<script>
    let lastBarcode = "";
    function startScanner() {
        const reader = document.getElementById('reader');
        reader.style.display = 'block';
        const scanner = new Html5Qrcode("reader");
        scanner.start({facingMode: "environment"}, {fps: 30, qrbox: 200}, (data) => {
            scanner.stop();
            reader.style.display = 'none';
            handleBarcode(data);
        });
    }
    function lookupManualBarcode() {
        const val = document.getElementById('manualBarcodeInput').value.trim();
        if (!val) return;
        handleBarcode(val);
        document.getElementById('manualBarcodeInput').value = '';
    }
    function handleBarcode(data) {
        lastBarcode = data;
        document.getElementById('scannedItemName').textContent = 'Looking up…';
        document.getElementById('scannedItemWeight').textContent = '—';
        document.getElementById('qtyInput').style.display = 'none';
        document.getElementById('qtyLabel').style.display = 'none';
        document.getElementById('confirmLoadBtn').style.display = 'none';
        document.getElementById('scanModal').style.display = 'flex';
        fetch('/lookup_barcode?barcode=' + encodeURIComponent(data))
            .then(res => res.json())
            .then(info => {
                if (info.found) {
                    document.getElementById('scannedItemName').textContent = info.item_name;
                    document.getElementById('scannedItemWeight').textContent = info.weight || 'Not specified';
                    document.getElementById('qtyInput').style.display = 'block';
                    document.getElementById('qtyLabel').style.display = 'block';
                    document.getElementById('confirmLoadBtn').style.display = 'block';
                    document.getElementById('qtyInput').value = 1;
                } else {
                    document.getElementById('scannedItemName').textContent = 'Barcode not found in any PO';
                    document.getElementById('scannedItemWeight').textContent = '—';
                }
            })
            .catch(() => {
                document.getElementById('scannedItemName').textContent = 'Could not look up item';
                document.getElementById('scannedItemWeight').textContent = '—';
            });
    }
    function submitQty() {
        let qty = document.getElementById('qtyInput').value;
        fetch('/process_scan', {
            method: 'POST',
            body: 'barcode=' + encodeURIComponent(lastBarcode) + '&qty=' + qty,
            headers: {'Content-Type': 'application/x-www-form-urlencoded'}
        }).then(() => location.reload());
    }
    function openEditLog(logId, currentQty) {
        document.getElementById('editLogForm').action = '/dispatch/edit/' + logId;
        document.getElementById('editQtyInput').value = currentQty;
        document.getElementById('editLogModal').style.display = 'flex';
    }

    // Keep every device (loading staff, owner, etc.) in sync automatically.
    // Skips the refresh while any modal is open so it never interrupts data entry.
    setInterval(() => {
        const modalsOpen = Array.from(document.querySelectorAll('.modal')).some(m => getComputedStyle(m).display === 'flex');
        const typing = document.activeElement && ['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement.tagName);
        if (!modalsOpen && !typing) location.reload();
    }, 12000);
</script>
</body>
</html>
"""

POS_HTML = STYLE_BLOCK + """
<title>Manage POs | REAL INSTANT FOODS</title>
</head>
<body>
<div class="container">
""" + TOPBAR_TEMPLATE.replace("__NAV__", nav_html('pos')) + """
    {% if filter_company %}
    <div style="margin-bottom:18px; display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
        <span style="color:var(--text-muted); font-size:13px;">Showing POs for:</span>
        <span class="badge badge-blue" style="font-size:13px;">{{ filter_company }}</span>
        <a href="/pos" class="btn btn-outline btn-sm" style="text-decoration:none;">Show All</a>
    </div>
    {% endif %}

    <div class="card">
        <div class="card-header">
            <h2>📤 Bulk Import from CSV</h2>
        </div>
        {% if import_msg %}
        <div class="badge {% if import_ok %}badge-green{% else %}badge-amber{% endif %}" style="display:block; padding:10px 14px; margin-bottom:14px; font-size:13px;">{{ import_msg }}</div>
        {% endif %}
        <form method="POST" action="/pos/import_csv" enctype="multipart/form-data">
            <label>Company / Client</label>
            <select name="company" required>
                <option value="" disabled {% if not filter_company %}selected{% endif %}>Select company</option>
                {% for c in companies %}
                <option value="{{ c }}" {% if c == filter_company %}selected{% endif %}>{{ c }}</option>
                {% endfor %}
            </select>
            <label>Choose CSV File</label>
            <input type="file" name="csv_file" accept=".csv" required style="padding:10px;">
            <button type="submit" class="btn" style="margin-top:10px;">Upload &amp; Import</button>
        </form>
        <div style="color:var(--text-muted); font-size:12.5px; margin-top:12px; line-height:1.6;">
            Your CSV should have these columns (any order, header row required):<br>
            <span style="font-family:monospace; color:var(--text);">po_number, item_name, weight, ordered_qty, barcode</span><br>
            Example: <span style="font-family:monospace;">PO-2026-014, Instant Noodles, 1kg, 500, 8901234567890</span><br>
            No company yet? <a href="/companies" style="color:var(--primary);">Add one here</a> first.
        </div>
    </div>

    <div class="card">
        <div class="card-header">
            <h2>➕ Add New PO Item</h2>
        </div>
        <form method="POST" action="/pos/add">
            <div class="form-grid">
                <div>
                    <label>Company / Client</label>
                    <select name="company" required>
                        <option value="" disabled {% if not filter_company %}selected{% endif %}>Select company</option>
                        {% for c in companies %}
                        <option value="{{ c }}" {% if c == filter_company %}selected{% endif %}>{{ c }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div>
                    <label>PO Number</label>
                    <input type="text" name="po_number" placeholder="e.g. PO-2026-014" required>
                </div>
                <div>
                    <label>Item Name</label>
                    <input type="text" name="item_name" placeholder="e.g. Instant Noodles" required>
                </div>
                <div>
                    <label>Weight</label>
                    <input type="text" name="weight" placeholder="e.g. 1kg or 500g" required>
                </div>
                <div>
                    <label>Ordered Quantity (bags)</label>
                    <input type="number" name="ordered_qty" placeholder="e.g. 500" required>
                </div>
                <div>
                    <label>Barcode</label>
                    <input type="text" name="barcode" placeholder="Scan or type barcode" required>
                </div>
            </div>
            <button type="submit" class="btn" style="margin-top:14px;">Add PO Item</button>
        </form>
        {% if companies|length == 0 %}
        <div style="color:var(--text-muted); font-size:12.5px; margin-top:10px;">No companies yet — <a href="/companies" style="color:var(--primary);">add one first</a>.</div>
        {% endif %}
    </div>

    <div class="card">
        <div class="card-header">
            <h2>📋 {% if filter_company %}{{ filter_company }}'s PO Items{% else %}All PO Items{% endif %}</h2>
        </div>
        {% if items|length > 0 %}
        <table>
            <thead><tr>
                <th>Company</th><th>PO Number</th><th>Item</th><th>Weight</th><th>Ordered Qty</th><th>Barcode</th><th></th>
            </tr></thead>
            <tbody>
            {% for it in items %}
            <tr>
                <td><span class="badge badge-blue">{{ it[6] or 'Unassigned' }}</span></td>
                <td style="font-weight:600;">{{ it[1] }}</td>
                <td>{{ it[2] }}</td>
                <td>{{ it[3] }}</td>
                <td>{{ it[4] }}</td>
                <td style="color:var(--text-muted); font-family:monospace;">{{ it[5] }}</td>
                <td>
                    <form method="POST" action="/pos/delete/{{ it[0] }}" onsubmit="return confirm('Delete this item?');">
                        <button type="submit" class="btn btn-danger btn-sm">Delete</button>
                    </form>
                </td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <div class="empty-state">No PO items added yet. Use the form above to add one.</div>
        {% endif %}
    </div>

    <footer>REAL INSTANT FOODS &middot; AI ERP System</footer>
</div>
</body>
</html>
"""

COMPANIES_HTML = STYLE_BLOCK + """
<title>Companies | REAL INSTANT FOODS</title>
</head>
<body>
<div class="container">
""" + TOPBAR_TEMPLATE.replace("__NAV__", nav_html('companies')) + """
    <div class="card">
        <div class="card-header">
            <h2>➕ Add New Company</h2>
        </div>
        {% if error_msg %}
        <div class="badge badge-amber" style="display:block; padding:10px 14px; margin-bottom:14px; font-size:13px;">{{ error_msg }}</div>
        {% endif %}
        <form method="POST" action="/companies/add">
            <div style="display:flex; gap:10px; flex-wrap:wrap;">
                <input type="text" name="name" placeholder="e.g. Zepto, Flipkart, Reliance, Anand Sweets" style="flex:1; min-width:220px; margin:0;" required>
                <button type="submit" class="btn" style="white-space:nowrap;">Add Company</button>
            </div>
        </form>
    </div>

    <div class="card">
        <div class="card-header">
            <h2>🏢 Company Folders</h2>
        </div>
        {% if companies|length > 0 %}
        <div style="display:grid; grid-template-columns:repeat(auto-fill, minmax(220px, 1fr)); gap:14px;">
            {% for c in companies %}
            <a href="/pos?company={{ c.name }}" style="text-decoration:none; color:inherit;">
                <div style="background:rgba(255,255,255,0.03); border:1px solid var(--border); border-radius:12px; padding:18px; transition:background 0.15s;" onmouseover="this.style.background='rgba(255,255,255,0.06)'" onmouseout="this.style.background='rgba(255,255,255,0.03)'">
                    <div style="font-size:26px; margin-bottom:8px;">🏢</div>
                    <div style="font-weight:700; font-size:15px; margin-bottom:6px;">{{ c.name }}</div>
                    <div style="color:var(--text-muted); font-size:12.5px;">{{ c.po_count }} PO(s) &middot; {{ c.item_count }} item(s)</div>
                </div>
            </a>
            {% endfor %}
        </div>
        {% else %}
        <div class="empty-state">No companies added yet. Add one above — e.g. Zepto, Flipkart, Reliance, Anand Sweets.</div>
        {% endif %}
    </div>

    <footer>REAL INSTANT FOODS &middot; AI ERP System</footer>
</div>
</body>
</html>
"""

HISTORY_HTML = STYLE_BLOCK + """
<title>History | REAL INSTANT FOODS</title>
</head>
<body>
<div class="container">
""" + TOPBAR_TEMPLATE.replace("__NAV__", nav_html('history')) + """
    <div class="card">
        <div class="card-header">
            <h2>🔍 Search Dispatch History</h2>
        </div>
        <form method="GET" action="/history">
            <div style="display:flex; gap:10px; flex-wrap:wrap;">
                <input type="text" name="po" placeholder="Enter PO Number" value="{{ search_po or '' }}" style="flex:1; min-width:200px; margin:0;">
                <button type="submit" class="btn" style="white-space:nowrap;">Search</button>
                {% if search_po %}
                <a href="/history" class="btn btn-outline" style="text-decoration:none; white-space:nowrap;">Clear</a>
                {% endif %}
            </div>
        </form>
    </div>

    {% if search_po %}
    <div class="stats-grid">
        <div class="stat-card">
            <div class="icon">📦</div>
            <div class="label">PO Number</div>
            <div class="value" style="font-size:20px;">{{ search_po }}</div>
        </div>
        <div class="stat-card">
            <div class="icon">🚚</div>
            <div class="label">Total Trips / Vehicles</div>
            <div class="value">{{ vehicle_count }}</div>
        </div>
        <div class="stat-card">
            <div class="icon">✅</div>
            <div class="label">Total Items Loaded</div>
            <div class="value">{{ total_loaded }}</div>
        </div>
    </div>
    {% endif %}

    <div class="card">
        <div class="card-header">
            <h2>📋 {% if search_po %}Records for {{ search_po }}{% else %}All Dispatch Records{% endif %}</h2>
        </div>
        {% if records|length > 0 %}
        <table>
            <thead><tr>
                <th>PO Number</th><th>Vehicle No.</th><th>Location</th><th>Product</th><th>Qty Loaded</th><th>Time</th>
            </tr></thead>
            <tbody>
            {% for r in records %}
            <tr>
                <td style="font-weight:600;">{{ r[0] or '—' }}</td>
                <td>{{ r[1] or '—' }}</td>
                <td>{{ r[2] or '—' }}</td>
                <td>{{ r[3] }}</td>
                <td><span class="badge badge-green">{{ r[4] }}</span></td>
                <td style="color:var(--text-muted);">{{ r[5] }}</td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <div class="empty-state">{% if search_po %}No dispatch records found for this PO.{% else %}No dispatch history yet.{% endif %}</div>
        {% endif %}
    </div>

    <footer>REAL INSTANT FOODS &middot; AI ERP System</footer>
</div>
</body>
</html>
"""

LOGIN_HTML = STYLE_BLOCK + """
<title>Login | REAL INSTANT FOODS</title>
</head>
<body>
<div style="display:flex; justify-content:center; align-items:center; min-height:100vh;">
    <div class="card" style="width:320px; text-align:center; padding:36px 28px;">
        <div class="brand-logo" style="margin:0 auto 16px;">RIF</div>
        <h2 style="margin:0 0 4px; font-size:19px;">REAL INSTANT FOODS</h2>
        <div style="color:var(--text-muted); font-size:12.5px; margin-bottom:20px;">AI Dispatch &amp; Packing ERP</div>
        <form method="POST">
            <input type="password" name="password" placeholder="Password" autofocus>
            <button type="submit" class="btn btn-block" style="margin-top:10px;">Login</button>
        </form>
        {% if error %}
        <div style="color:#fca5a5; font-size:12.5px; margin-top:12px;">Incorrect password, please try again.</div>
        {% endif %}
    </div>
</div>
</body>
</html>
"""

@app.route('/')
def home():
    if not session.get('logged_in'): return redirect('/login')
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT DISTINCT po_number, company FROM po_items GROUP BY po_number')
    po_list = cursor.fetchall()
    cursor.execute('SELECT id, po_number, vehicle_no, location, product_name, loaded_qty, timestamp FROM dispatch_log ORDER BY id DESC LIMIT 200')
    logs = cursor.fetchall()
    cursor.execute('SELECT COALESCE(SUM(loaded_qty), 0) FROM dispatch_log')
    total_loaded = cursor.fetchone()[0]

    # Item-level progress: ordered vs dispatched per (po_number, barcode)
    cursor.execute('SELECT po_number, barcode, item_name, weight, company, SUM(ordered_qty) FROM po_items GROUP BY po_number, barcode')
    po_item_rows = cursor.fetchall()
    cursor.execute('SELECT po_number, barcode, SUM(loaded_qty) FROM dispatch_log GROUP BY po_number, barcode')
    dispatched_map = {(r[0], r[1]): (r[2] or 0) for r in cursor.fetchall()}
    conn.close()

    active = get_active_session()

    item_progress = []
    for po_number, barcode, item_name, weight, company, ordered in po_item_rows:
        ordered = ordered or 0
        dispatched = dispatched_map.get((po_number, barcode), 0)
        pending = max(ordered - dispatched, 0)
        percent = min(100, round((dispatched / ordered) * 100)) if ordered else 0
        label = f"{item_name} ({weight})" if weight else item_name
        item_progress.append({
            'po_number': po_number, 'item_name': label, 'company': company,
            'ordered': ordered, 'dispatched': dispatched, 'pending': pending, 'percent': percent
        })

    session_set = bool(active['cur_po'] and active['cur_vehicle'] and active['cur_location'])
    return render_template_string(
        DASHBOARD_HTML,
        po_list=po_list,
        logs=logs,
        total_loaded=total_loaded,
        item_progress=item_progress,
        session_set=session_set,
        cur_po=active['cur_po'],
        cur_vehicle=active['cur_vehicle'],
        cur_location=active['cur_location']
    )

@app.route('/companies')
def companies_page():
    if not session.get('logged_in'): return redirect('/login')
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT name FROM companies ORDER BY name')
    names = [r[0] for r in cursor.fetchall()]
    cursor.execute('SELECT company, COUNT(DISTINCT po_number), COUNT(*) FROM po_items WHERE company IS NOT NULL GROUP BY company')
    stats = {r[0]: (r[1], r[2]) for r in cursor.fetchall()}
    conn.close()
    companies = [{'name': n, 'po_count': stats.get(n, (0, 0))[0], 'item_count': stats.get(n, (0, 0))[1]} for n in names]
    error_msg = request.args.get('error')
    return render_template_string(COMPANIES_HTML, companies=companies, error_msg=error_msg)

@app.route('/companies/add', methods=['POST'])
def companies_add():
    if not session.get('logged_in'): return redirect('/login')
    name = request.form.get('name', '').strip()
    if not name:
        return redirect('/companies')
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    try:
        cursor.execute('INSERT INTO companies (name) VALUES (?)', (name,))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return redirect('/companies?error=' + quote(f'"{name}" already exists.'))
    conn.close()
    return redirect('/companies')

@app.route('/history')
def history_page():
    if not session.get('logged_in'): return redirect('/login')
    search_po = request.args.get('po', '').strip()
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    if search_po:
        cursor.execute('SELECT po_number, vehicle_no, location, product_name, loaded_qty, timestamp FROM dispatch_log WHERE po_number LIKE ? ORDER BY id DESC', (f'%{search_po}%',))
    else:
        cursor.execute('SELECT po_number, vehicle_no, location, product_name, loaded_qty, timestamp FROM dispatch_log ORDER BY id DESC LIMIT 200')
    records = cursor.fetchall()
    conn.close()

    vehicle_count = len(set(r[1] for r in records if r[1]))
    total_loaded = sum(r[4] or 0 for r in records)

    return render_template_string(
        HISTORY_HTML,
        records=records,
        search_po=search_po,
        vehicle_count=vehicle_count,
        total_loaded=total_loaded
    )

@app.route('/pos')
def pos_page():
    if not session.get('logged_in'): return redirect('/login')
    filter_company = request.args.get('company', '').strip()
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    if filter_company:
        cursor.execute('SELECT id, po_number, item_name, weight, ordered_qty, barcode, company FROM po_items WHERE company = ? ORDER BY id DESC', (filter_company,))
    else:
        cursor.execute('SELECT id, po_number, item_name, weight, ordered_qty, barcode, company FROM po_items ORDER BY id DESC')
    items = cursor.fetchall()
    cursor.execute('SELECT name FROM companies ORDER BY name')
    companies = [r[0] for r in cursor.fetchall()]
    conn.close()
    import_msg = request.args.get('msg')
    import_ok = request.args.get('ok') == '1'
    return render_template_string(POS_HTML, items=items, import_msg=import_msg, import_ok=import_ok, companies=companies, filter_company=filter_company)

@app.route('/pos/add', methods=['POST'])
def pos_add():
    if not session.get('logged_in'): return redirect('/login')
    company = request.form.get('company', '').strip()
    po_number = request.form.get('po_number', '').strip()
    item_name = request.form.get('item_name', '').strip()
    weight = request.form.get('weight', '').strip()
    ordered_qty = request.form.get('ordered_qty', '').strip() or 0
    barcode = request.form.get('barcode', '').strip()
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('INSERT INTO po_items (po_number, item_name, weight, ordered_qty, barcode, company) VALUES (?, ?, ?, ?, ?, ?)',
                   (po_number, item_name, weight, int(ordered_qty), barcode, company))
    conn.commit()
    conn.close()
    return redirect('/pos?company=' + quote(company) if company else '/pos')

# Accepts flexible header names (English or common Hindi-transliterated variants),
# plus real-world PO export formats (e.g. PoNumber, SkuDesc, EAN, Quantity)
CSV_HEADER_MAP = {
    'po_number': ['po_number', 'po number', 'po no', 'ponumber', 'po', 'po_no'],
    'item_name': ['item_name', 'item name', 'item', 'product', 'product_name', 'skudesc', 'sku desc', 'sku_desc', 'sku'],
    'weight': ['weight', 'wt', 'size'],
    'ordered_qty': ['ordered_qty', 'ordered quantity', 'quantity', 'qty', 'order_qty'],
    'barcode': ['barcode', 'bar code', 'code', 'ean', 'ean code'],
}

WEIGHT_PATTERN = re.compile(r'\((\d+(?:\.\d+)?)\s*(kg|g)\)', re.IGNORECASE)

def _extract_weight(text):
    """Pull a weight like '500 g' or '1 kg' out of a product description string."""
    matches = WEIGHT_PATTERN.findall(text or '')
    if not matches:
        return ''
    amount, unit = matches[-1]
    return f"{amount}{unit.lower()}"

def _map_csv_headers(fieldnames):
    normalized = {f.strip().lower(): f for f in fieldnames if f}
    resolved = {}
    for key, aliases in CSV_HEADER_MAP.items():
        for alias in aliases:
            if alias in normalized:
                resolved[key] = normalized[alias]
                break
    return resolved

@app.route('/pos/import_csv', methods=['POST'])
def pos_import_csv():
    if not session.get('logged_in'): return redirect('/login')
    company = request.form.get('company', '').strip()
    if not company:
        return redirect('/pos?ok=0&msg=' + quote('Please select a company for this import.'))
    file = request.files.get('csv_file')
    if not file or file.filename == '':
        return redirect('/pos?ok=0&msg=' + quote('No file selected.'))

    try:
        raw = file.read().decode('utf-8-sig', errors='ignore')
    except Exception:
        return redirect('/pos?ok=0&msg=' + quote('Could not read the file, please try again.'))

    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames:
        return redirect('/pos?ok=0&msg=' + quote('No header row found in the CSV.'))

    colmap = _map_csv_headers(reader.fieldnames)
    required = ['po_number', 'item_name', 'ordered_qty', 'barcode']
    missing = [k for k in required if k not in colmap]
    if missing:
        return redirect('/pos?ok=0&msg=' + quote(f'These columns were not found in the CSV: {", ".join(missing)}'))

    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    inserted, skipped = 0, 0
    for row in reader:
        po_number = (row.get(colmap['po_number']) or '').strip()
        item_name = (row.get(colmap['item_name']) or '').strip()
        barcode = (row.get(colmap['barcode']) or '').strip()
        qty_raw = (row.get(colmap['ordered_qty']) or '').strip()
        if 'weight' in colmap:
            weight = (row.get(colmap['weight']) or '').strip()
        else:
            weight = _extract_weight(item_name)
        if not po_number or not item_name or not qty_raw:
            skipped += 1
            continue
        try:
            ordered_qty = int(float(qty_raw))
        except ValueError:
            skipped += 1
            continue
        cursor.execute('INSERT INTO po_items (po_number, item_name, weight, ordered_qty, barcode, company) VALUES (?, ?, ?, ?, ?, ?)',
                       (po_number, item_name, weight, ordered_qty, barcode, company))
        inserted += 1
    conn.commit()
    conn.close()

    msg = f'{inserted} items imported successfully for {company}.'
    if skipped:
        msg += f' {skipped} rows skipped (incomplete data).'
    return redirect('/pos?company=' + quote(company) + '&ok=1&msg=' + quote(msg))

@app.route('/pos/delete/<int:item_id>', methods=['POST'])
def pos_delete(item_id):
    if not session.get('logged_in'): return redirect('/login')
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('DELETE FROM po_items WHERE id = ?', (item_id,))
    conn.commit()
    conn.close()
    return redirect('/pos')

@app.route('/start_session', methods=['POST'])
def start_session():
    if not session.get('logged_in'): return redirect('/login')
    po_number = request.form.get('po_number', '').strip()
    vehicle_no = request.form.get('vehicle_no', '').strip()
    location = request.form.get('location', '').strip()
    set_active_session(po_number, vehicle_no, location)
    return redirect('/')

@app.route('/lookup_barcode')
def lookup_barcode():
    if not session.get('logged_in'): return {'found': False}, 401
    barcode = request.args.get('barcode', '').strip()
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT item_name, weight FROM po_items WHERE barcode = ? LIMIT 1', (barcode,))
    item = cursor.fetchone()
    conn.close()
    if item:
        return {'found': True, 'item_name': item[0], 'weight': item[1]}
    return {'found': False}

@app.route('/process_scan', methods=['POST'])
def process_scan():
    barcode = request.form['barcode'].strip()
    m_units = request.form['qty']
    active = get_active_session()
    po_number = active['cur_po'] or 'Unknown'
    vehicle_no = active['cur_vehicle'] or 'Unknown'
    location = active['cur_location'] or 'Unknown'
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT item_name, weight FROM po_items WHERE barcode = ?', (barcode,))
    item = cursor.fetchone()
    if item:
        total_qty = calculate_qty(item[1], m_units)
        cursor.execute('INSERT INTO dispatch_log (po_number, vehicle_no, location, product_name, loaded_qty, timestamp, barcode) VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (po_number, vehicle_no, location, f"{item[0]} ({item[1]})", total_qty, now_ist().strftime("%d %b %Y, %I:%M %p"), barcode))
        conn.commit()
        conn.close()
        return {'ok': True}, 200
    conn.close()
    return {'ok': False, 'error': 'barcode not found'}, 404

@app.route('/dispatch/edit/<int:log_id>', methods=['POST'])
def dispatch_edit(log_id):
    if not session.get('logged_in'): return redirect('/login')
    new_qty = request.form.get('loaded_qty', '').strip()
    try:
        new_qty = int(float(new_qty))
    except ValueError:
        return redirect('/')
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE dispatch_log SET loaded_qty = ? WHERE id = ?', (new_qty, log_id))
    conn.commit()
    conn.close()
    return redirect('/')

@app.route('/dispatch/delete/<int:log_id>', methods=['POST'])
def dispatch_delete(log_id):
    if not session.get('logged_in'): return redirect('/login')
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('DELETE FROM dispatch_log WHERE id = ?', (log_id,))
    conn.commit()
    conn.close()
    return redirect('/')

@app.route('/export_csv')
def export_csv():
    if not session.get('logged_in'): return redirect('/login')
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT po_number, vehicle_no, location, product_name, loaded_qty, timestamp FROM dispatch_log ORDER BY id DESC')
    rows = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['PO Number', 'Vehicle No', 'Location', 'Product', 'Qty Loaded', 'Time'])
    writer.writerows(rows)

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=dispatch_log.csv'}
    )

@app.route('/export_progress_csv')
def export_progress_csv():
    if not session.get('logged_in'): return redirect('/login')
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT po_number, barcode, item_name, weight, company, SUM(ordered_qty) FROM po_items GROUP BY po_number, barcode')
    po_item_rows = cursor.fetchall()
    cursor.execute('SELECT po_number, barcode, SUM(loaded_qty) FROM dispatch_log GROUP BY po_number, barcode')
    dispatched_map = {(r[0], r[1]): (r[2] or 0) for r in cursor.fetchall()}
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Company', 'PO Number', 'Item', 'Weight', 'Barcode', 'Ordered Qty', 'Loaded Qty', 'Pending Qty', '% Complete'])
    for po_number, barcode, item_name, weight, company, ordered in po_item_rows:
        ordered = ordered or 0
        dispatched = dispatched_map.get((po_number, barcode), 0)
        pending = max(ordered - dispatched, 0)
        percent = min(100, round((dispatched / ordered) * 100)) if ordered else 0
        writer.writerow([company or '', po_number, item_name, weight, barcode, ordered, dispatched, pending, f'{percent}%'])

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=po_progress.csv'}
    )

def calculate_qty(weight, master_units):
    # Quantity entered on scan is the direct bag/unit count — no multiplier applied.
    return int(master_units)

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = False
    if request.method == 'POST':
        if request.form.get('password') == 'real@8283':
            session['logged_in'] = True
            return redirect('/')
        error = True
    return render_template_string(LOGIN_HTML, error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
