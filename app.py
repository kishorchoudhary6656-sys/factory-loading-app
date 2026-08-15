from flask import Flask, render_template_string, request, redirect, session, Response, jsonify
import os, csv, io, re, json, secrets
import psycopg2
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

app = Flask(__name__)
app.secret_key = 'real_instant_foods_final_2026'

# --- Database (PostgreSQL via Neon — persists forever, unlike Render's local disk) ---
DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL environment variable is not set. "
        "This app needs a persistent Postgres database (e.g. from neon.tech) "
        "so data is never lost on redeploy/restart. "
        "Set DATABASE_URL in your Render service's Environment settings."
    )

def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

IST = timezone(timedelta(hours=5, minutes=30))
def now_ist():
    return datetime.now(timezone.utc).astimezone(IST)

def init_db():
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS po_items (id SERIAL PRIMARY KEY, po_number TEXT, item_name TEXT, weight TEXT, ordered_qty INTEGER, barcode TEXT, company TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS dispatch_log (id SERIAL PRIMARY KEY, po_number TEXT, vehicle_no TEXT, location TEXT, product_name TEXT, loaded_qty INTEGER, timestamp TEXT, barcode TEXT)''')
    # Shared, global state (NOT per-browser) so every device sees the same active dispatch session live
    cursor.execute('''CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT)''')
    # Companies (Zepto, Flipkart, Reliance, Anand Sweets, etc.) — each PO belongs to one company/folder
    cursor.execute('''CREATE TABLE IF NOT EXISTS companies (id SERIAL PRIMARY KEY, name TEXT UNIQUE, sub_brands TEXT)''')
    # Quality Checkers — registered with a reference photo for identity check-in
    cursor.execute('''CREATE TABLE IF NOT EXISTS quality_checkers (id SERIAL PRIMARY KEY, name TEXT UNIQUE, photo_data TEXT, created_at TEXT)''')
    # A pre-loaded catalog of barcodes (independent of any single PO) so scanning always matches
    cursor.execute('''CREATE TABLE IF NOT EXISTS barcode_catalog (id SERIAL PRIMARY KEY, barcode TEXT UNIQUE, item_name TEXT)''')
    # Daily Production log — one row per packed unit logged by a Quality Checker
    cursor.execute('''CREATE TABLE IF NOT EXISTS daily_production (
        id SERIAL PRIMARY KEY,
        company TEXT, sub_brand TEXT, item_name_entered TEXT, item_name TEXT, barcode TEXT,
        packing_date TEXT, use_by_date TEXT, batch_number TEXT, quantity INTEGER,
        qc_name TEXT, qc_photo TEXT,
        prod_date TEXT, prod_time TEXT, created_at TEXT
    )''')
    # --- Vehicle Loading & Live Tracking (multi-PO per vehicle + driver GPS tracking) ---
    cursor.execute('''CREATE TABLE IF NOT EXISTS vehicles (
        id SERIAL PRIMARY KEY,
        vehicle_number TEXT, driver_name TEXT, driver_mobile TEXT, start_location TEXT,
        vehicle_status TEXT DEFAULT 'Loading',
        loading_started_at TEXT,
        current_latitude DOUBLE PRECISION, current_longitude DOUBLE PRECISION, current_accuracy DOUBLE PRECISION,
        last_location_at TEXT,
        tracking_token TEXT UNIQUE,
        tracking_status TEXT DEFAULT 'stopped',
        dispatched_at TEXT, delivered_at TEXT
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS vehicle_po_map (
        id SERIAL PRIMARY KEY,
        vehicle_id INTEGER REFERENCES vehicles(id) ON DELETE CASCADE,
        po_number TEXT, company TEXT,
        is_hold BOOLEAN DEFAULT FALSE, is_cancelled BOOLEAN DEFAULT FALSE,
        created_at TEXT,
        UNIQUE(vehicle_id, po_number)
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS location_history (
        id SERIAL PRIMARY KEY,
        vehicle_id INTEGER REFERENCES vehicles(id) ON DELETE CASCADE,
        latitude DOUBLE PRECISION, longitude DOUBLE PRECISION, accuracy DOUBLE PRECISION,
        recorded_at TEXT
    )''')
    # Defensive migrations in case an older schema already exists
    cursor.execute('ALTER TABLE dispatch_log ADD COLUMN IF NOT EXISTS barcode TEXT')
    cursor.execute('ALTER TABLE po_items ADD COLUMN IF NOT EXISTS company TEXT')
    cursor.execute('ALTER TABLE companies ADD COLUMN IF NOT EXISTS sub_brands TEXT')
    # Convenience defaults for the three companies named in the spec, only if not already set
    for cname, subs in [('Zepto', 'Daily Good,Unbranded'), ('Flipkart', 'Unbranded,My Kitchen'), ('Reliance', 'Good Life')]:
        cursor.execute('UPDATE companies SET sub_brands = %s WHERE name = %s AND (sub_brands IS NULL OR sub_brands = %s)', (subs, cname, ''))
    conn.commit()
    conn.close()
init_db()

def get_active_session():
    conn = get_conn()
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
    conn = get_conn()
    cursor = conn.cursor()
    for key, value in [('cur_po', po_number), ('cur_vehicle', vehicle_no), ('cur_location', location)]:
        cursor.execute('INSERT INTO app_state (key, value) VALUES (%s, %s) ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value', (key, value))
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

    /* Animated factory hero scene */
    .hero-factory {
        position: relative;
        height: 280px;
        border-radius: var(--radius);
        overflow: hidden;
        margin-bottom: 26px;
        border: 1px solid var(--border);
        background:
            radial-gradient(circle at 20% 15%, rgba(59,130,246,0.20), transparent 55%),
            radial-gradient(circle at 85% 75%, rgba(139,92,246,0.16), transparent 50%),
            repeating-linear-gradient(90deg, rgba(255,255,255,0.025) 0, rgba(255,255,255,0.025) 1px, transparent 1px, transparent 40px),
            repeating-linear-gradient(0deg, rgba(255,255,255,0.025) 0, rgba(255,255,255,0.025) 1px, transparent 1px, transparent 40px),
            linear-gradient(160deg, #0b1120 0%, #131c30 50%, #0b1120 100%);
    }
    .hero-scan {
        position: absolute; top: 0; left: -30%; width: 30%; height: 100%;
        background: linear-gradient(90deg, transparent, rgba(59,130,246,0.10), transparent);
        animation: scanMove 5s linear infinite;
        z-index: 1;
    }
    @keyframes scanMove {
        0% { left: -30%; }
        100% { left: 100%; }
    }

    /* Distant factory skyline silhouette, sits behind everything */
    .hero-skyline {
        position: absolute; bottom: 78px; left: 0; width: 100%; height: 60px;
        background:
            linear-gradient(to top, rgba(15,23,42,0.9), transparent),
            repeating-linear-gradient(90deg, rgba(148,163,184,0.10) 0 26px, transparent 26px 60px);
        opacity: 0.6;
    }
    .hero-chimney {
        position: absolute; bottom: 78px; width: 10px; height: 44px;
        background: rgba(148,163,184,0.22); border-radius: 2px 2px 0 0;
    }
    .hero-smoke {
        position: absolute; bottom: 122px; width: 10px; height: 10px;
        border-radius: 50%; background: rgba(226,232,240,0.18);
        animation: smokeRise 4s ease-in infinite;
    }
    .hero-smoke.s2 { animation-delay: 1.3s; left: 4px; }
    .hero-smoke.s3 { animation-delay: 2.6s; left: -3px; }
    @keyframes smokeRise {
        0% { transform: translateY(0) scale(0.6); opacity: 0; }
        20% { opacity: 0.5; }
        100% { transform: translateY(-70px) scale(1.6); opacity: 0; }
    }

    /* Floating dust / light particles for atmosphere */
    .hero-particle {
        position: absolute; width: 3px; height: 3px; border-radius: 50%;
        background: rgba(147,197,253,0.55);
        animation: particleFloat 6s linear infinite;
    }
    @keyframes particleFloat {
        0% { transform: translateY(20px) translateX(0); opacity: 0; }
        15% { opacity: 0.8; }
        85% { opacity: 0.4; }
        100% { transform: translateY(-160px) translateX(20px); opacity: 0; }
    }

    .hero-content {
        position: relative; z-index: 2;
        display: flex; flex-direction: column; justify-content: flex-start;
        padding: 26px 32px 0;
        max-width: 640px;
        background: linear-gradient(90deg, rgba(11,17,32,0.55) 0%, rgba(11,17,32,0.25) 70%, transparent 100%);
        height: 155px;
    }
    .hero-tag {
        display: inline-flex; align-items: center; gap: 6px; width: fit-content;
        background: rgba(59,130,246,0.14); color: #93c5fd; font-size: 11.5px; font-weight: 700;
        padding: 5px 12px; border-radius: 20px; letter-spacing: 0.05em; text-transform: uppercase;
        margin-bottom: 12px;
    }
    .hero-title {
        font-size: 34px; font-weight: 800; letter-spacing: -0.5px; margin: 0 0 6px;
        background: linear-gradient(90deg, #ffffff, #bcd4ff);
        -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent;
    }
    .hero-sub { color: var(--text-muted); font-size: 14px; font-weight: 500; max-width: 460px; }
    .hero-clock { color: #93c5fd; font-size: 12.5px; font-weight: 700; margin-top: 14px; font-family: monospace; }

    .conveyor-track {
        position: absolute; bottom: 0; left: 0; width: 100%; height: 46px;
        background: repeating-linear-gradient(90deg, #1e293b 0, #1e293b 22px, #16213a 22px, #16213a 44px);
        border-top: 3px solid #334155;
        animation: conveyorMove 1.2s linear infinite;
        z-index: 1;
    }
    @keyframes conveyorMove {
        from { background-position: 0 0; }
        to { background-position: -44px 0; }
    }
    .factory-box {
        position: absolute; bottom: 40px; font-size: 26px; z-index: 1;
        animation: boxSlide 7s linear infinite;
        filter: drop-shadow(0 4px 6px rgba(0,0,0,0.4));
    }
    .factory-box.b2 { animation-delay: 2.3s; }
    .factory-box.b3 { animation-delay: 4.6s; }
    @keyframes boxSlide {
        0% { left: -8%; opacity: 0; transform: translateY(0); }
        6% { opacity: 1; }
        94% { opacity: 1; }
        100% { left: 106%; opacity: 0; }
    }
    .factory-truck {
        position: absolute; bottom: 44px; font-size: 34px; z-index: 1;
        animation: truckDrive 9s ease-in-out infinite;
        filter: drop-shadow(0 4px 6px rgba(0,0,0,0.4));
    }
    @keyframes truckDrive {
        0% { left: -12%; transform: scaleX(-1); }
        45% { left: 78%; transform: scaleX(-1); }
        50% { left: 78%; transform: scaleX(1); }
        95% { left: -12%; transform: scaleX(1); }
        100% { left: -12%; transform: scaleX(-1); }
    }
    .factory-worker {
        position: absolute; bottom: 44px; font-size: 22px; z-index: 1;
        animation: workerBob 2.2s ease-in-out infinite;
    }
    @keyframes workerBob {
        0%, 100% { transform: translateY(0); }
        50% { transform: translateY(-4px); }
    }

    @media (max-width: 640px) {
        .hero-factory { height: 250px; }
        .hero-title { font-size: 22px; }
        .hero-content { padding: 18px 20px 0; height: 130px; max-width: 100%; }
        .hero-sub { font-size: 12.5px; }
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
        <a href="/production" class="{cls('production')}">Daily Production</a>
        <a href="/vehicles" class="{cls('vehicles')}">Vehicles</a>
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
    <div class="hero-factory">
        <div class="hero-skyline"></div>
        <div class="hero-chimney" style="left:72%;">
            <div class="hero-smoke"></div>
            <div class="hero-smoke s2"></div>
            <div class="hero-smoke s3"></div>
        </div>
        <div class="hero-chimney" style="left:88%; height:34px;"></div>
        <div class="hero-particle" style="left:30%; animation-delay:0s;"></div>
        <div class="hero-particle" style="left:45%; animation-delay:1.5s;"></div>
        <div class="hero-particle" style="left:60%; animation-delay:3s;"></div>
        <div class="hero-particle" style="left:75%; animation-delay:2s;"></div>
        <div class="hero-particle" style="left:90%; animation-delay:4s;"></div>
        <div class="hero-scan"></div>
        <div class="hero-content">
            <span class="hero-tag">⚡ AI-Powered ERP &middot; Live</span>
            <h1 class="hero-title">REAL INSTANT FOODS</h1>
            <div class="hero-sub">Dispatch, packing &amp; loading — tracked in real time, floor to office</div>
            <div class="hero-clock" id="heroClock">—</div>
        </div>
        <div class="factory-worker" style="left:6%;">👷</div>
        <div class="factory-worker" style="left:16%; animation-delay:0.6s;">👷‍♂️</div>
        <div class="factory-box b1">📦</div>
        <div class="factory-box b2">📦</div>
        <div class="factory-box b3">📦</div>
        <div class="factory-truck">🚚</div>
        <div class="conveyor-track"></div>
    </div>

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
    function updateHeroClock() {
        const el = document.getElementById('heroClock');
        if (!el) return;
        const now = new Date();
        const opts = { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true };
        el.textContent = now.toLocaleString('en-IN', opts) + ' IST';
    }
    updateHeroClock();
    setInterval(updateHeroClock, 1000);
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
    }, 60000);
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
            <h2>📤 Bulk Import from CSV / Excel</h2>
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
            <label>Choose CSV or Excel File</label>
            <input type="file" name="csv_file" accept=".csv,.xlsx" required style="padding:10px;">
            <button type="submit" class="btn" style="margin-top:10px;">Upload &amp; Import</button>
        </form>
        <div style="color:var(--text-muted); font-size:12.5px; margin-top:12px; line-height:1.6;">
            Both .csv and .xlsx (Excel) files work. Your file should have these columns (any order, header row required):<br>
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
        {% if po_groups|length > 0 %}
        {% for g in po_groups %}
        <div style="border:1px solid var(--border); border-radius:12px; margin-bottom:16px; overflow:hidden;">
            <div style="display:flex; justify-content:space-between; align-items:center; padding:12px 16px; background:rgba(255,255,255,0.03); flex-wrap:wrap; gap:8px;">
                <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
                    <span class="badge badge-blue">{{ g.company or 'Unassigned' }}</span>
                    <span style="font-weight:700; font-size:14px;">{{ g.po_number }}</span>
                    <span style="color:var(--text-muted); font-size:12.5px;">{{ g.rows|length }} item(s) &middot; {{ g.total_ordered }} total ordered</span>
                </div>
                <form method="POST" action="/pos/delete_po" onsubmit="return confirm('Delete the ENTIRE PO {{ g.po_number }} ({{ g.rows|length }} items)? This cannot be undone.');">
                    <input type="hidden" name="po_number" value="{{ g.po_number }}">
                    <input type="hidden" name="company" value="{{ g.company }}">
                    <button type="submit" class="btn btn-danger btn-sm">🗑 Delete Entire PO</button>
                </form>
            </div>
            <table>
                <thead><tr>
                    <th>Item</th><th>Weight</th><th>Ordered Qty</th><th>Barcode</th><th></th>
                </tr></thead>
                <tbody>
                {% for it in g.rows %}
                <tr>
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
        </div>
        {% endfor %}
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
                <input type="text" name="sub_brands" placeholder="Sub-brands, comma separated (optional)" style="flex:1; min-width:220px; margin:0;">
                <button type="submit" class="btn" style="white-space:nowrap;">Add Company</button>
            </div>
        </form>
    </div>

    <div class="card">
        <div class="card-header">
            <h2>🏢 Company Folders</h2>
        </div>
        {% if companies|length > 0 %}
        <div style="display:grid; grid-template-columns:repeat(auto-fill, minmax(240px, 1fr)); gap:14px;">
            {% for c in companies %}
            <div style="background:rgba(255,255,255,0.03); border:1px solid var(--border); border-radius:12px; padding:18px;">
                <a href="/pos?company={{ c.name }}" style="text-decoration:none; color:inherit;">
                    <div style="font-size:26px; margin-bottom:8px;">🏢</div>
                    <div style="font-weight:700; font-size:15px; margin-bottom:6px;">{{ c.name }}</div>
                    <div style="color:var(--text-muted); font-size:12.5px; margin-bottom:8px;">{{ c.po_count }} PO(s) &middot; {{ c.item_count }} item(s)</div>
                </a>
                {% if c.sub_brands %}
                <div style="margin-bottom:8px;">
                    {% for sb in c.sub_brands.split(',') %}
                    <span class="badge badge-blue" style="margin:2px 3px 0 0;">{{ sb.strip() }}</span>
                    {% endfor %}
                </div>
                {% endif %}
                <form method="POST" action="/companies/edit_subbrands" style="display:flex; gap:6px;" onsubmit="return true;">
                    <input type="hidden" name="name" value="{{ c.name }}">
                    <input type="text" name="sub_brands" value="{{ c.sub_brands or '' }}" placeholder="Sub-brands, comma separated" style="margin:0; font-size:12px; padding:8px;">
                    <button type="submit" class="btn btn-outline btn-sm" style="white-space:nowrap;">Save</button>
                </form>
            </div>
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

PRODUCTION_HTML = STYLE_BLOCK + """
<title>Daily Production | REAL INSTANT FOODS</title>
</head>
<body>
<div class="container">
""" + TOPBAR_TEMPLATE.replace("__NAV__", nav_html('production')) + """
    <div class="nav" style="margin-bottom:20px; width:fit-content;">
        <a href="/production?tab=log" class="{% if tab == 'log' %}active{% endif %}">📝 Log Entry</a>
        <a href="/production?tab=history" class="{% if tab == 'history' %}active{% endif %}">📅 History</a>
        <a href="/production?tab=summary" class="{% if tab == 'summary' %}active{% endif %}">📊 Master Summary</a>
        <a href="/production?tab=admin" class="{% if tab == 'admin' %}active{% endif %}">⚙️ Admin</a>
    </div>

    {% if msg %}
    <div class="badge {% if msg_ok %}badge-green{% else %}badge-amber{% endif %}" style="display:block; padding:10px 14px; margin-bottom:18px; font-size:13px;">{{ msg }}</div>
    {% endif %}

    {% if tab == 'log' %}
    <div class="card">
        <div class="card-header"><h2>📝 Log a Production Entry</h2></div>

        {% if companies|length == 0 %}
        <div class="empty-state">No companies yet — <a href="/companies" style="color:var(--primary);">add one first</a>.</div>
        {% elif quality_checkers|length == 0 %}
        <div class="empty-state">No Quality Checkers registered yet — go to the <a href="/production?tab=admin" style="color:var(--primary);">Admin tab</a> to register one first.</div>
        {% else %}
        <form method="POST" action="/production/add" id="prodForm">
            <div class="form-grid">
                <div>
                    <label>Company</label>
                    <select name="company" id="prodCompany" required onchange="updateSubBrands()">
                        <option value="" disabled selected>Select company</option>
                        {% for c in companies %}
                        <option value="{{ c.name }}">{{ c.name }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div>
                    <label>Sub-brand</label>
                    <select name="sub_brand" id="prodSubBrand" required>
                        <option value="" disabled selected>Select company first</option>
                    </select>
                </div>
                <div>
                    <label>Item Being Packed</label>
                    <input type="text" name="item_name_entered" placeholder="e.g. Raw Peanut 500g" required>
                </div>
            </div>

            <div style="margin-top:18px; padding:16px; background:rgba(255,255,255,0.03); border-radius:12px;">
                <label style="margin-top:0;">Quality Checker Check-in</label>
                <select name="qc_name" id="qcSelect" required onchange="showQcPhoto()">
                    <option value="" disabled selected>Select your name</option>
                    {% for qc in quality_checkers %}
                    <option value="{{ qc.name }}">{{ qc.name }}</option>
                    {% endfor %}
                </select>
                <div id="qcRefPhotoWrap" style="display:none; margin:10px 0; align-items:center; gap:10px;">
                    <span style="font-size:12px; color:var(--text-muted);">Registered photo:</span>
                    <img id="qcRefPhoto" style="width:52px; height:52px; border-radius:8px; object-fit:cover; vertical-align:middle; margin-left:8px; border:1px solid var(--border);">
                </div>
                <label>Take a check-in selfie to confirm it's you</label>
                <input type="file" accept="image/*" capture="user" id="qcPhotoFile" onchange="compressImage(this, 'qcPhotoData', 'qcPhotoPreview')">
                <input type="hidden" name="qc_photo" id="qcPhotoData" required>
                <img id="qcPhotoPreview" style="display:none; width:64px; height:64px; border-radius:8px; object-fit:cover; margin-top:8px; border:2px solid var(--primary);">
            </div>

            <div style="margin-top:18px; padding:16px; background:rgba(255,255,255,0.03); border-radius:12px;">
                <label style="margin-top:0;">Scan Packet Barcode</label>
                <div id="prodReader" style="width:100%; border-radius:10px; display:none; margin-bottom:10px;"></div>
                <div style="display:flex; gap:8px; flex-wrap:wrap;">
                    <button type="button" class="btn btn-outline" onclick="startProdScanner()">📷 Scan Barcode</button>
                    <input type="text" id="prodManualBarcode" placeholder="Or type barcode manually" style="margin:0; flex:1; min-width:160px;">
                    <button type="button" class="btn btn-outline" onclick="lookupProdBarcode()">Look Up</button>
                </div>
                <div id="prodScannedInfo" style="display:none; margin-top:12px; background:rgba(255,255,255,0.04); border-radius:10px; padding:12px;">
                    <div style="font-size:12px; color:var(--text-muted); font-weight:600;">SCANNED ITEM</div>
                    <div id="prodScannedName" style="font-weight:700;">—</div>
                    <div style="font-size:12px; color:var(--text-muted); margin-top:4px;">EAN / Barcode: <span id="prodScannedBarcode" style="font-family:monospace;">—</span></div>
                </div>
                <input type="hidden" name="barcode" id="prodBarcodeField" required>
                <input type="hidden" name="item_name" id="prodItemNameField">
            </div>

            <div class="form-grid" style="margin-top:18px;">
                <div>
                    <label>Packing Date</label>
                    <input type="date" name="packing_date" required>
                </div>
                <div>
                    <label>Use-by Date</label>
                    <input type="date" name="use_by_date" required>
                </div>
                <div>
                    <label>Batch Number</label>
                    <input type="text" name="batch_number" placeholder="e.g. B-2026-081" required>
                </div>
                <div>
                    <label>Quantity</label>
                    <input type="number" name="quantity" placeholder="e.g. 100" required>
                </div>
            </div>

            <button type="submit" class="btn" style="margin-top:18px;">Log Production Entry</button>
        </form>
        {% endif %}
    </div>

    {% elif tab == 'history' %}
    {% if grouped_history|length > 0 %}
    {% for group in grouped_history %}
    <div class="card">
        <div class="card-header">
            <h2>📅 {{ group.date }}</h2>
            <span class="badge badge-blue">{{ group.entries|length }} entries &middot; {{ group.total_qty }} total qty</span>
        </div>
        <table>
            <thead><tr>
                <th>Time</th><th>Company</th><th>Sub-brand</th><th>Item</th><th>Batch</th><th>Qty</th><th>QC</th><th></th>
            </tr></thead>
            <tbody>
            {% for e in group.entries %}
            <tr>
                <td style="color:var(--text-muted);">{{ e.prod_time }}</td>
                <td>{{ e.company }}</td>
                <td><span class="badge badge-blue">{{ e.sub_brand }}</span></td>
                <td>{{ e.item_name }}</td>
                <td>{{ e.batch_number }}</td>
                <td><span class="badge badge-green">{{ e.quantity }}</span></td>
                <td>{{ e.qc_name }}</td>
                <td style="white-space:nowrap;">
                    {% if e.editable %}
                    <button class="btn btn-outline btn-sm" onclick="openEditProd('{{ e.id }}', '{{ e.packing_date }}', '{{ e.use_by_date }}', '{{ e.batch_number }}', '{{ e.quantity }}')">Edit</button>
                    <form method="POST" action="/production/delete/{{ e.id }}" style="display:inline;" onsubmit="return confirm('Delete this entry?');">
                        <button type="submit" class="btn btn-danger btn-sm">Delete</button>
                    </form>
                    {% else %}
                    <span class="badge badge-amber">🔒 Locked</span>
                    {% endif %}
                </td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
    </div>
    {% endfor %}
    {% else %}
    <div class="card"><div class="empty-state">No production entries yet.</div></div>
    {% endif %}

    <div id="editProdModal" class="modal">
        <div class="modal-content">
            <h3>Edit Production Entry</h3>
            <form method="POST" id="editProdForm" action="/production/edit/0">
                <label style="text-align:left;">Packing Date</label>
                <input type="date" name="packing_date" id="editPackingDate" required>
                <label style="text-align:left;">Use-by Date</label>
                <input type="date" name="use_by_date" id="editUseByDate" required>
                <label style="text-align:left;">Batch Number</label>
                <input type="text" name="batch_number" id="editBatchNumber" required>
                <label style="text-align:left;">Quantity</label>
                <input type="number" name="quantity" id="editQuantity" required>
                <button type="submit" class="btn btn-block" style="margin-top:10px;">Save</button>
            </form>
            <button class="btn btn-outline btn-block" style="margin-top:8px;" onclick="document.getElementById('editProdModal').style.display='none'">Cancel</button>
        </div>
    </div>

    {% elif tab == 'summary' %}
    <div class="stats-grid">
        <div class="stat-card">
            <div class="icon">📦</div>
            <div class="label">Total Production (All Time)</div>
            <div class="value">{{ grand_total }}</div>
        </div>
        <div class="stat-card">
            <div class="icon">🏢</div>
            <div class="label">Companies</div>
            <div class="value">{{ by_company|length }}</div>
        </div>
        <div class="stat-card">
            <div class="icon">📋</div>
            <div class="label">Total Entries Logged</div>
            <div class="value">{{ total_entries }}</div>
        </div>
    </div>

    <div class="card">
        <div class="card-header"><h2>🏢 Production by Company</h2></div>
        {% if by_company|length > 0 %}
        {% for c in by_company %}
        <div style="margin-bottom:14px;">
            <div style="display:flex; justify-content:space-between; font-size:13.5px; margin-bottom:2px;">
                <span style="font-weight:600;">{{ c.company }}</span>
                <span style="color:var(--text-muted);">{{ c.total }}</span>
            </div>
            <div class="progress-track"><div class="progress-fill" style="width:{{ c.percent }}%;"></div></div>
        </div>
        {% endfor %}
        {% else %}
        <div class="empty-state">No production data yet.</div>
        {% endif %}
    </div>

    <div class="card">
        <div class="card-header"><h2>📦 Top Items Produced</h2></div>
        {% if by_item|length > 0 %}
        <table>
            <thead><tr><th>Item</th><th>Total Quantity</th></tr></thead>
            <tbody>
            {% for it in by_item %}
            <tr><td>{{ it.item_name }}</td><td><span class="badge badge-green">{{ it.total }}</span></td></tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <div class="empty-state">No production data yet.</div>
        {% endif %}
    </div>

    {% elif tab == 'admin' %}
    <div class="card">
        <div class="card-header"><h2>👤 Register Quality Checker</h2></div>
        <form method="POST" action="/production/qc/add" id="qcRegisterForm">
            <label>Name</label>
            <input type="text" name="name" placeholder="Quality Checker's full name" required>
            <label>Reference Photo</label>
            <input type="file" accept="image/*" capture="user" id="qcRegPhotoFile" onchange="compressImage(this, 'qcRegPhotoData', 'qcRegPhotoPreview')">
            <input type="hidden" name="photo_data" id="qcRegPhotoData" required>
            <img id="qcRegPhotoPreview" style="display:none; width:70px; height:70px; border-radius:10px; object-fit:cover; margin-top:8px; border:2px solid var(--primary);">
            <button type="submit" class="btn" style="margin-top:12px;">Register</button>
        </form>
    </div>

    <div class="card">
        <div class="card-header"><h2>👥 Registered Quality Checkers</h2></div>
        {% if quality_checkers|length > 0 %}
        <div style="display:grid; grid-template-columns:repeat(auto-fill, minmax(160px, 1fr)); gap:14px;">
            {% for qc in quality_checkers %}
            <div style="background:rgba(255,255,255,0.03); border:1px solid var(--border); border-radius:12px; padding:14px; text-align:center;">
                <img src="{{ qc.photo_data }}" style="width:56px; height:56px; border-radius:50%; object-fit:cover; margin-bottom:8px;">
                <div style="font-weight:600; font-size:13px; margin-bottom:8px;">{{ qc.name }}</div>
                <form method="POST" action="/production/qc/delete/{{ qc.id }}" onsubmit="return confirm('Remove this Quality Checker?');">
                    <button type="submit" class="btn btn-danger btn-sm" style="width:100%;">Remove</button>
                </form>
            </div>
            {% endfor %}
        </div>
        {% else %}
        <div class="empty-state">No Quality Checkers registered yet.</div>
        {% endif %}
    </div>

    <div class="card">
        <div class="card-header"><h2>📇 Barcode Catalog</h2></div>
        <div style="color:var(--text-muted); font-size:12.5px; margin-bottom:14px;">
            Pre-load every known barcode/EAN here so scanning in Daily Production and Dispatch always finds a match, even for items not on a specific PO. Currently <strong style="color:var(--text);">{{ catalog_count }}</strong> barcode(s) loaded.
        </div>
        <form method="POST" action="/production/barcode_catalog/import" enctype="multipart/form-data" style="margin-bottom:18px;">
            <label>Bulk Import (CSV or Excel — columns: barcode, item_name)</label>
            <input type="file" name="catalog_file" accept=".csv,.xlsx" required style="padding:10px;">
            <button type="submit" class="btn" style="margin-top:10px;">Upload &amp; Import</button>
        </form>
        <form method="POST" action="/production/barcode_catalog/add">
            <div style="display:flex; gap:10px; flex-wrap:wrap;">
                <input type="text" name="barcode" placeholder="Barcode / EAN" style="flex:1; min-width:160px; margin:0;" required>
                <input type="text" name="item_name" placeholder="Item name" style="flex:1; min-width:160px; margin:0;" required>
                <button type="submit" class="btn btn-outline" style="white-space:nowrap;">Add Single Barcode</button>
            </div>
        </form>
    </div>
    {% endif %}

    <footer>REAL INSTANT FOODS &middot; AI ERP System</footer>
</div>

<script src="https://unpkg.com/html5-qrcode"></script>
<script>
    const companySubBrands = {{ company_subbrands_json | safe }};
    const qcPhotos = {{ qc_photos_json | safe }};

    function updateSubBrands() {
        const company = document.getElementById('prodCompany').value;
        const subSelect = document.getElementById('prodSubBrand');
        const subs = companySubBrands[company] || [];
        subSelect.innerHTML = '';
        if (subs.length === 0) {
            subSelect.innerHTML = '<option value="" disabled selected>No sub-brands set for this company</option>';
            return;
        }
        subSelect.innerHTML = '<option value="" disabled selected>Select sub-brand</option>' +
            subs.map(s => `<option value="${s}">${s}</option>`).join('');
    }

    function showQcPhoto() {
        const name = document.getElementById('qcSelect').value;
        const wrap = document.getElementById('qcRefPhotoWrap');
        const img = document.getElementById('qcRefPhoto');
        if (qcPhotos[name]) {
            img.src = qcPhotos[name];
            wrap.style.display = 'flex';
        } else {
            wrap.style.display = 'none';
        }
    }

    // Resizes/compresses a captured photo client-side before it's ever sent to
    // the server, so photo storage stays small and the page never hangs on upload.
    function compressImage(inputEl, hiddenFieldId, previewId) {
        const file = inputEl.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = function(e) {
            const img = new Image();
            img.onload = function() {
                const maxDim = 220;
                let w = img.width, h = img.height;
                if (w > h && w > maxDim) { h = h * (maxDim / w); w = maxDim; }
                else if (h > maxDim) { w = w * (maxDim / h); h = maxDim; }
                const canvas = document.createElement('canvas');
                canvas.width = w; canvas.height = h;
                canvas.getContext('2d').drawImage(img, 0, 0, w, h);
                const dataUrl = canvas.toDataURL('image/jpeg', 0.6);
                document.getElementById(hiddenFieldId).value = dataUrl;
                if (previewId) {
                    const prev = document.getElementById(previewId);
                    prev.src = dataUrl;
                    prev.style.display = 'inline-block';
                }
            };
            img.src = e.target.result;
        };
        reader.readAsDataURL(file);
    }

    let prodLastBarcode = "";
    function startProdScanner() {
        const reader = document.getElementById('prodReader');
        reader.style.display = 'block';
        const scanner = new Html5Qrcode("prodReader");
        scanner.start({facingMode: "environment"}, {fps: 30, qrbox: 200}, (data) => {
            scanner.stop();
            reader.style.display = 'none';
            resolveProdBarcode(data);
        });
    }
    function lookupProdBarcode() {
        const val = document.getElementById('prodManualBarcode').value.trim();
        if (!val) return;
        resolveProdBarcode(val);
    }
    function resolveProdBarcode(barcode) {
        prodLastBarcode = barcode;
        document.getElementById('prodScannedInfo').style.display = 'block';
        document.getElementById('prodScannedName').textContent = 'Looking up…';
        document.getElementById('prodScannedBarcode').textContent = barcode;
        fetch('/production/lookup_barcode?barcode=' + encodeURIComponent(barcode))
            .then(res => res.json())
            .then(info => {
                if (info.found) {
                    document.getElementById('prodScannedName').textContent = info.item_name;
                    document.getElementById('prodBarcodeField').value = barcode;
                    document.getElementById('prodItemNameField').value = info.item_name;
                } else {
                    document.getElementById('prodScannedName').textContent = 'Not found in catalog — add it in Admin tab first';
                    document.getElementById('prodBarcodeField').value = '';
                    document.getElementById('prodItemNameField').value = '';
                }
            })
            .catch(() => {
                document.getElementById('prodScannedName').textContent = 'Could not look up item';
            });
    }

    function openEditProd(id, packingDate, useByDate, batchNumber, quantity) {
        document.getElementById('editProdForm').action = '/production/edit/' + id;
        document.getElementById('editPackingDate').value = packingDate;
        document.getElementById('editUseByDate').value = useByDate;
        document.getElementById('editBatchNumber').value = batchNumber;
        document.getElementById('editQuantity').value = quantity;
        document.getElementById('editProdModal').style.display = 'flex';
    }
</script>
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
<body style="background-color:#0b1120; background-image:
        radial-gradient(circle at 15% 20%, rgba(59,130,246,0.20), transparent 45%),
        radial-gradient(circle at 85% 80%, rgba(139,92,246,0.16), transparent 50%);
    background-attachment:fixed; margin:0; min-height:100vh; position:relative; overflow:hidden;">
    <div class="hero-particle" style="left:12%; animation-delay:0s;"></div>
    <div class="hero-particle" style="left:30%; animation-delay:2s;"></div>
    <div class="hero-particle" style="left:55%; animation-delay:1s;"></div>
    <div class="hero-particle" style="left:70%; animation-delay:3.5s;"></div>
    <div class="hero-particle" style="left:88%; animation-delay:2.6s;"></div>
    <div class="conveyor-track" style="opacity:0.6;"></div>
    <div class="factory-box b1">📦</div>
    <div class="factory-box b2">📦</div>
    <div class="factory-box b3">📦</div>
    <div class="factory-truck">🚚</div>
    <div style="display:flex; justify-content:center; align-items:center; min-height:100vh; position:relative; z-index:3;">
        <div class="card" style="width:320px; text-align:center; padding:36px 28px; backdrop-filter:blur(16px);">
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
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT DISTINCT po_number, company FROM po_items')
    po_list = cursor.fetchall()
    cursor.execute('SELECT id, po_number, vehicle_no, location, product_name, loaded_qty, timestamp FROM dispatch_log ORDER BY id DESC LIMIT 200')
    logs = cursor.fetchall()
    cursor.execute('SELECT COALESCE(SUM(loaded_qty), 0) FROM dispatch_log')
    total_loaded = cursor.fetchone()[0]

    # Item-level progress: ordered vs dispatched per (po_number, barcode)
    cursor.execute('SELECT po_number, barcode, item_name, weight, company, SUM(ordered_qty) FROM po_items GROUP BY po_number, barcode, item_name, weight, company')
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
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT name, sub_brands FROM companies ORDER BY name')
    rows = cursor.fetchall()
    cursor.execute('SELECT company, COUNT(DISTINCT po_number), COUNT(*) FROM po_items WHERE company IS NOT NULL GROUP BY company')
    stats = {r[0]: (r[1], r[2]) for r in cursor.fetchall()}
    conn.close()
    companies = [{'name': n, 'sub_brands': sb, 'po_count': stats.get(n, (0, 0))[0], 'item_count': stats.get(n, (0, 0))[1]} for n, sb in rows]
    error_msg = request.args.get('error')
    return render_template_string(COMPANIES_HTML, companies=companies, error_msg=error_msg)

@app.route('/companies/add', methods=['POST'])
def companies_add():
    if not session.get('logged_in'): return redirect('/login')
    name = request.form.get('name', '').strip()
    sub_brands = request.form.get('sub_brands', '').strip()
    if not name:
        return redirect('/companies')
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute('INSERT INTO companies (name, sub_brands) VALUES (%s, %s)', (name, sub_brands))
        conn.commit()
    except psycopg2.IntegrityError:
        conn.rollback()
        conn.close()
        return redirect('/companies?error=' + quote(f'"{name}" already exists.'))
    conn.close()
    return redirect('/companies')

@app.route('/companies/edit_subbrands', methods=['POST'])
def companies_edit_subbrands():
    if not session.get('logged_in'): return redirect('/login')
    name = request.form.get('name', '').strip()
    sub_brands = request.form.get('sub_brands', '').strip()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('UPDATE companies SET sub_brands = %s WHERE name = %s', (sub_brands, name))
    conn.commit()
    conn.close()
    return redirect('/companies')

@app.route('/production')
def production_page():
    if not session.get('logged_in'): return redirect('/login')
    tab = request.args.get('tab', 'log')
    msg = request.args.get('msg')
    msg_ok = request.args.get('ok') == '1'

    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT name, sub_brands FROM companies ORDER BY name')
    company_rows = cursor.fetchall()
    companies = [{'name': n, 'sub_brands': sb} for n, sb in company_rows]
    company_subbrands = {n: [s.strip() for s in (sb or '').split(',') if s.strip()] for n, sb in company_rows}

    cursor.execute('SELECT id, name, photo_data FROM quality_checkers ORDER BY name')
    qc_rows = cursor.fetchall()
    quality_checkers = [{'id': r[0], 'name': r[1], 'photo_data': r[2]} for r in qc_rows]
    qc_photos = {r[1]: r[2] for r in qc_rows}

    grouped_history = []
    by_company, by_item, grand_total, total_entries = [], [], 0, 0
    catalog_count = 0

    if tab == 'history':
        cursor.execute('''SELECT id, company, sub_brand, item_name, batch_number, quantity, qc_name,
                           packing_date, use_by_date, prod_date, prod_time, created_at
                           FROM daily_production ORDER BY id DESC LIMIT 500''')
        rows = cursor.fetchall()
        now = now_ist()
        groups = {}
        order = []
        for r in rows:
            (pid, company, sub_brand, item_name, batch_number, quantity, qc_name,
             packing_date, use_by_date, prod_date, prod_time, created_at) = r
            try:
                created_dt = datetime.fromisoformat(created_at)
            except Exception:
                created_dt = now
            editable = (now - created_dt) <= timedelta(hours=12)
            entry = {
                'id': pid, 'company': company, 'sub_brand': sub_brand, 'item_name': item_name,
                'batch_number': batch_number, 'quantity': quantity, 'qc_name': qc_name,
                'packing_date': packing_date, 'use_by_date': use_by_date,
                'prod_time': prod_time, 'editable': editable
            }
            if prod_date not in groups:
                groups[prod_date] = {'date': prod_date, 'entries': [], 'total_qty': 0}
                order.append(prod_date)
            groups[prod_date]['entries'].append(entry)
            groups[prod_date]['total_qty'] += quantity or 0
        grouped_history = [groups[d] for d in order]

    elif tab == 'summary':
        cursor.execute('SELECT company, SUM(quantity) FROM daily_production GROUP BY company ORDER BY SUM(quantity) DESC')
        company_totals = cursor.fetchall()
        grand_total = sum(t or 0 for _, t in company_totals)
        by_company = [{'company': c, 'total': t or 0, 'percent': round(((t or 0) / grand_total) * 100) if grand_total else 0} for c, t in company_totals]

        cursor.execute('SELECT item_name, SUM(quantity) FROM daily_production GROUP BY item_name ORDER BY SUM(quantity) DESC LIMIT 20')
        by_item = [{'item_name': i, 'total': t or 0} for i, t in cursor.fetchall()]

        cursor.execute('SELECT COUNT(*) FROM daily_production')
        total_entries = cursor.fetchone()[0]

    elif tab == 'admin':
        cursor.execute('SELECT COUNT(*) FROM barcode_catalog')
        catalog_count = cursor.fetchone()[0]

    conn.close()

    return render_template_string(
        PRODUCTION_HTML,
        tab=tab, msg=msg, msg_ok=msg_ok,
        companies=companies, quality_checkers=quality_checkers,
        company_subbrands_json=json.dumps(company_subbrands),
        qc_photos_json=json.dumps(qc_photos),
        grouped_history=grouped_history,
        by_company=by_company, by_item=by_item, grand_total=grand_total, total_entries=total_entries,
        catalog_count=catalog_count
    )

@app.route('/production/add', methods=['POST'])
def production_add():
    if not session.get('logged_in'): return redirect('/login')
    company = request.form.get('company', '').strip()
    sub_brand = request.form.get('sub_brand', '').strip()
    item_name_entered = request.form.get('item_name_entered', '').strip()
    barcode = request.form.get('barcode', '').strip()
    item_name = request.form.get('item_name', '').strip()
    packing_date = request.form.get('packing_date', '').strip()
    use_by_date = request.form.get('use_by_date', '').strip()
    batch_number = request.form.get('batch_number', '').strip()
    quantity_raw = request.form.get('quantity', '').strip()
    qc_name = request.form.get('qc_name', '').strip()
    qc_photo = request.form.get('qc_photo', '').strip()

    if not barcode or not item_name:
        return redirect('/production?tab=log&ok=0&msg=' + quote('Please scan and confirm a barcode before submitting.'))
    if not qc_name or not qc_photo:
        return redirect('/production?tab=log&ok=0&msg=' + quote('Quality Checker check-in (name + photo) is required.'))
    try:
        quantity = int(float(quantity_raw))
    except ValueError:
        return redirect('/production?tab=log&ok=0&msg=' + quote('Quantity must be a number.'))

    now = now_ist()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('''INSERT INTO daily_production
        (company, sub_brand, item_name_entered, item_name, barcode, packing_date, use_by_date,
         batch_number, quantity, qc_name, qc_photo, prod_date, prod_time, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)''',
        (company, sub_brand, item_name_entered, item_name, barcode, packing_date, use_by_date,
         batch_number, quantity, qc_name, qc_photo, now.strftime('%d %b %Y'), now.strftime('%I:%M %p'), now.isoformat()))
    conn.commit()
    conn.close()
    return redirect('/production?tab=history&ok=1&msg=' + quote('Production entry logged successfully.'))

@app.route('/production/edit/<int:entry_id>', methods=['POST'])
def production_edit(entry_id):
    if not session.get('logged_in'): return redirect('/login')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT created_at FROM daily_production WHERE id = %s', (entry_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return redirect('/production?tab=history')
    try:
        created_dt = datetime.fromisoformat(row[0])
    except Exception:
        created_dt = now_ist()
    if (now_ist() - created_dt) > timedelta(hours=12):
        conn.close()
        return redirect('/production?tab=history&ok=0&msg=' + quote('This entry is locked (older than 12 hours) and can no longer be edited.'))

    packing_date = request.form.get('packing_date', '').strip()
    use_by_date = request.form.get('use_by_date', '').strip()
    batch_number = request.form.get('batch_number', '').strip()
    quantity_raw = request.form.get('quantity', '').strip()
    try:
        quantity = int(float(quantity_raw))
    except ValueError:
        conn.close()
        return redirect('/production?tab=history&ok=0&msg=' + quote('Quantity must be a number.'))

    cursor.execute('UPDATE daily_production SET packing_date=%s, use_by_date=%s, batch_number=%s, quantity=%s WHERE id=%s',
                   (packing_date, use_by_date, batch_number, quantity, entry_id))
    conn.commit()
    conn.close()
    return redirect('/production?tab=history&ok=1&msg=' + quote('Entry updated.'))

@app.route('/production/delete/<int:entry_id>', methods=['POST'])
def production_delete(entry_id):
    if not session.get('logged_in'): return redirect('/login')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT created_at FROM daily_production WHERE id = %s', (entry_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return redirect('/production?tab=history')
    try:
        created_dt = datetime.fromisoformat(row[0])
    except Exception:
        created_dt = now_ist()
    if (now_ist() - created_dt) > timedelta(hours=12):
        conn.close()
        return redirect('/production?tab=history&ok=0&msg=' + quote('This entry is locked (older than 12 hours) and can no longer be deleted.'))
    cursor.execute('DELETE FROM daily_production WHERE id = %s', (entry_id,))
    conn.commit()
    conn.close()
    return redirect('/production?tab=history&ok=1&msg=' + quote('Entry deleted.'))

@app.route('/production/qc/add', methods=['POST'])
def production_qc_add():
    if not session.get('logged_in'): return redirect('/login')
    name = request.form.get('name', '').strip()
    photo_data = request.form.get('photo_data', '').strip()
    if not name or not photo_data:
        return redirect('/production?tab=admin&ok=0&msg=' + quote('Name and a photo are both required.'))
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute('INSERT INTO quality_checkers (name, photo_data, created_at) VALUES (%s, %s, %s)',
                       (name, photo_data, now_ist().isoformat()))
        conn.commit()
    except psycopg2.IntegrityError:
        conn.rollback()
        conn.close()
        return redirect('/production?tab=admin&ok=0&msg=' + quote(f'"{name}" is already registered.'))
    conn.close()
    return redirect('/production?tab=admin&ok=1&msg=' + quote(f'{name} registered successfully.'))

@app.route('/production/qc/delete/<int:qc_id>', methods=['POST'])
def production_qc_delete(qc_id):
    if not session.get('logged_in'): return redirect('/login')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM quality_checkers WHERE id = %s', (qc_id,))
    conn.commit()
    conn.close()
    return redirect('/production?tab=admin')

@app.route('/production/barcode_catalog/add', methods=['POST'])
def barcode_catalog_add():
    if not session.get('logged_in'): return redirect('/login')
    barcode = request.form.get('barcode', '').strip()
    item_name = request.form.get('item_name', '').strip()
    if not barcode or not item_name:
        return redirect('/production?tab=admin')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('INSERT INTO barcode_catalog (barcode, item_name) VALUES (%s, %s) ON CONFLICT (barcode) DO UPDATE SET item_name = EXCLUDED.item_name', (barcode, item_name))
    conn.commit()
    conn.close()
    return redirect('/production?tab=admin&ok=1&msg=' + quote('Barcode added to catalog.'))

@app.route('/production/barcode_catalog/import', methods=['POST'])
def barcode_catalog_import():
    if not session.get('logged_in'): return redirect('/login')
    file = request.files.get('catalog_file')
    if not file or file.filename == '':
        return redirect('/production?tab=admin&ok=0&msg=' + quote('No file selected.'))
    filename_lower = file.filename.lower()
    if not (filename_lower.endswith('.csv') or filename_lower.endswith('.xlsx')):
        return redirect('/production?tab=admin&ok=0&msg=' + quote('Please upload a .csv or .xlsx file.'))

    fieldnames, rows = _read_spreadsheet_rows(file)
    if fieldnames is None:
        return redirect('/production?tab=admin&ok=0&msg=' + quote('Could not read the file — check it has a header row.'))

    colmap = _map_csv_headers(fieldnames)
    if 'barcode' not in colmap or 'item_name' not in colmap:
        return redirect('/production?tab=admin&ok=0&msg=' + quote('File must have barcode and item_name columns.'))

    conn = get_conn()
    cursor = conn.cursor()
    inserted, skipped = 0, 0
    for row in rows:
        barcode = (row.get(colmap['barcode']) or '').strip()
        item_name = (row.get(colmap['item_name']) or '').strip()
        if not barcode or not item_name:
            skipped += 1
            continue
        cursor.execute('INSERT INTO barcode_catalog (barcode, item_name) VALUES (%s, %s) ON CONFLICT (barcode) DO UPDATE SET item_name = EXCLUDED.item_name', (barcode, item_name))
        inserted += 1
    conn.commit()
    conn.close()
    msg = f'{inserted} barcodes imported into the catalog.'
    if skipped:
        msg += f' {skipped} rows skipped (incomplete data).'
    return redirect('/production?tab=admin&ok=1&msg=' + quote(msg))

@app.route('/production/lookup_barcode')
def production_lookup_barcode():
    if not session.get('logged_in'): return {'found': False}, 401
    barcode = request.args.get('barcode', '').strip()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT item_name FROM barcode_catalog WHERE barcode = %s', (barcode,))
    row = cursor.fetchone()
    if not row:
        cursor.execute('SELECT item_name FROM po_items WHERE barcode = %s LIMIT 1', (barcode,))
        row = cursor.fetchone()
    conn.close()
    if row:
        return {'found': True, 'item_name': row[0]}
    return {'found': False}

@app.route('/history')
def history_page():
    if not session.get('logged_in'): return redirect('/login')
    search_po = request.args.get('po', '').strip()
    conn = get_conn()
    cursor = conn.cursor()
    if search_po:
        cursor.execute('SELECT po_number, vehicle_no, location, product_name, loaded_qty, timestamp FROM dispatch_log WHERE po_number ILIKE %s ORDER BY id DESC', (f'%{search_po}%',))
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
    conn = get_conn()
    cursor = conn.cursor()
    if filter_company:
        cursor.execute('SELECT id, po_number, item_name, weight, ordered_qty, barcode, company FROM po_items WHERE company = %s ORDER BY po_number, id DESC', (filter_company,))
    else:
        cursor.execute('SELECT id, po_number, item_name, weight, ordered_qty, barcode, company FROM po_items ORDER BY po_number, id DESC')
    rows = cursor.fetchall()
    cursor.execute('SELECT name FROM companies ORDER BY name')
    companies = [r[0] for r in cursor.fetchall()]
    conn.close()

    # Group items by (company, po_number) so a whole wrongly-uploaded PO can be deleted in one go
    groups_map = {}
    order = []
    for it in rows:
        key = (it[6] or '', it[1])
        if key not in groups_map:
            groups_map[key] = {'company': it[6] or '', 'po_number': it[1], 'rows': [], 'total_ordered': 0}
            order.append(key)
        groups_map[key]['rows'].append(it)
        groups_map[key]['total_ordered'] += it[4] or 0
    po_groups = [groups_map[k] for k in order]

    import_msg = request.args.get('msg')
    import_ok = request.args.get('ok') == '1'
    return render_template_string(POS_HTML, po_groups=po_groups, import_msg=import_msg, import_ok=import_ok, companies=companies, filter_company=filter_company)

@app.route('/pos/delete_po', methods=['POST'])
def pos_delete_po():
    if not session.get('logged_in'): return redirect('/login')
    po_number = request.form.get('po_number', '').strip()
    company = request.form.get('company', '').strip()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM po_items WHERE po_number = %s AND company = %s', (po_number, company))
    conn.commit()
    conn.close()
    return redirect('/pos?company=' + quote(company) if company else '/pos')

@app.route('/pos/add', methods=['POST'])
def pos_add():
    if not session.get('logged_in'): return redirect('/login')
    company = request.form.get('company', '').strip()
    po_number = request.form.get('po_number', '').strip()
    item_name = request.form.get('item_name', '').strip()
    weight = request.form.get('weight', '').strip()
    ordered_qty = request.form.get('ordered_qty', '').strip() or 0
    barcode = request.form.get('barcode', '').strip()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('INSERT INTO po_items (po_number, item_name, weight, ordered_qty, barcode, company) VALUES (%s, %s, %s, %s, %s, %s)',
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

def _read_spreadsheet_rows(file_storage):
    """Reads an uploaded CSV or XLSX file and returns (fieldnames, rows) where
    rows is an iterable of dicts, matching csv.DictReader's shape either way.
    Returns (None, None) if the file type isn't supported or has no header row."""
    filename = (file_storage.filename or '').lower()
    raw_bytes = file_storage.read()

    if filename.endswith('.xlsx'):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
            ws = wb.active
            rows_iter = ws.iter_rows(values_only=True)
            header = next(rows_iter, None)
            if not header or all(h is None for h in header):
                return None, None
            fieldnames = [str(h).strip() if h is not None else '' for h in header]
            data_rows = []
            for row in rows_iter:
                if row is None or all(v is None for v in row):
                    continue
                row_dict = {}
                for fname, val in zip(fieldnames, row):
                    if val is None:
                        val = ''
                    elif isinstance(val, float) and val.is_integer():
                        val = str(int(val))
                    else:
                        val = str(val)
                    row_dict[fname] = val
                data_rows.append(row_dict)
            return fieldnames, data_rows
        except Exception:
            return None, None
    else:
        # Default to CSV (also covers files without a .csv extension)
        try:
            raw = raw_bytes.decode('utf-8-sig', errors='ignore')
        except Exception:
            return None, None
        reader = csv.DictReader(io.StringIO(raw))
        if not reader.fieldnames:
            return None, None
        return reader.fieldnames, list(reader)

@app.route('/pos/import_csv', methods=['POST'])
def pos_import_csv():
    if not session.get('logged_in'): return redirect('/login')
    company = request.form.get('company', '').strip()
    if not company:
        return redirect('/pos?ok=0&msg=' + quote('Please select a company for this import.'))
    file = request.files.get('csv_file')
    if not file or file.filename == '':
        return redirect('/pos?ok=0&msg=' + quote('No file selected.'))

    filename_lower = file.filename.lower()
    if not (filename_lower.endswith('.csv') or filename_lower.endswith('.xlsx')):
        return redirect('/pos?ok=0&msg=' + quote('Please upload a .csv or .xlsx file.'))

    fieldnames, rows = _read_spreadsheet_rows(file)
    if fieldnames is None:
        return redirect('/pos?ok=0&msg=' + quote('Could not read the file — check it has a header row and try again.'))

    colmap = _map_csv_headers(fieldnames)
    required = ['po_number', 'item_name', 'ordered_qty', 'barcode']
    missing = [k for k in required if k not in colmap]
    if missing:
        return redirect('/pos?ok=0&msg=' + quote(f'These columns were not found in the file: {", ".join(missing)}'))

    conn = get_conn()
    cursor = conn.cursor()
    inserted, skipped = 0, 0
    for row in rows:
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
        cursor.execute('INSERT INTO po_items (po_number, item_name, weight, ordered_qty, barcode, company) VALUES (%s, %s, %s, %s, %s, %s)',
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
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM po_items WHERE id = %s', (item_id,))
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
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT item_name, weight FROM po_items WHERE barcode = %s LIMIT 1', (barcode,))
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
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT item_name, weight FROM po_items WHERE barcode = %s', (barcode,))
    item = cursor.fetchone()
    if item:
        total_qty = calculate_qty(item[1], m_units)
        cursor.execute('INSERT INTO dispatch_log (po_number, vehicle_no, location, product_name, loaded_qty, timestamp, barcode) VALUES (%s, %s, %s, %s, %s, %s, %s)',
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
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('UPDATE dispatch_log SET loaded_qty = %s WHERE id = %s', (new_qty, log_id))
    conn.commit()
    conn.close()
    return redirect('/')

@app.route('/dispatch/delete/<int:log_id>', methods=['POST'])
def dispatch_delete(log_id):
    if not session.get('logged_in'): return redirect('/login')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM dispatch_log WHERE id = %s', (log_id,))
    conn.commit()
    conn.close()
    return redirect('/')

@app.route('/export_csv')
def export_csv():
    if not session.get('logged_in'): return redirect('/login')
    conn = get_conn()
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
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT po_number, barcode, item_name, weight, company, SUM(ordered_qty) FROM po_items GROUP BY po_number, barcode, item_name, weight, company')
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


# ===========================================================================
# VEHICLE LOADING & LIVE GPS TRACKING
# ===========================================================================

def gen_tracking_token():
    return secrets.token_urlsafe(24)

def po_progress(cursor, po_number, vehicle_number):
    cursor.execute('SELECT COALESCE(SUM(ordered_qty),0) FROM po_items WHERE po_number = %s', (po_number,))
    ordered = cursor.fetchone()[0] or 0
    cursor.execute('SELECT COALESCE(SUM(loaded_qty),0) FROM dispatch_log WHERE po_number = %s AND vehicle_no = %s', (po_number, vehicle_number))
    loaded = cursor.fetchone()[0] or 0
    return ordered, loaded

def po_status_label(ordered, loaded, is_hold, is_cancelled):
    if is_cancelled: return ('Cancelled', 'badge-amber')
    if is_hold: return ('Hold', 'badge-amber')
    if ordered and loaded >= ordered: return ('Completed', 'badge-green')
    if loaded > 0: return ('Loading', 'badge-blue')
    return ('Pending', 'badge-amber')

def freshness_info(last_location_at):
    if not last_location_at:
        return ('No location yet', 'grey', 999999)
    try:
        then = datetime.fromisoformat(last_location_at)
    except Exception:
        return ('Unknown', 'grey', 999999)
    secs = (now_ist() - then).total_seconds()
    if secs < 60: label = f"{int(secs)} sec ago"
    elif secs < 3600: label = f"{int(secs//60)} min ago"
    else: label = f"{int(secs//3600)} hr ago"
    if secs <= 60: color = 'green'
    elif secs <= 600: color = 'yellow'
    else: color = 'red'
    return (label, color, secs)

def recompute_vehicle_status(cursor, vehicle_id):
    cursor.execute('SELECT vehicle_status FROM vehicles WHERE id = %s', (vehicle_id,))
    row = cursor.fetchone()
    if not row: return
    current = row[0]
    if current in ('In Transit', 'Delivered', 'Cancelled'):
        return  # dispatched/delivered/cancelled vehicles are not auto-changed
    cursor.execute('SELECT vehicle_number FROM vehicles WHERE id = %s', (vehicle_id,))
    vehicle_number = cursor.fetchone()[0]
    cursor.execute('SELECT po_number, is_hold, is_cancelled FROM vehicle_po_map WHERE vehicle_id = %s', (vehicle_id,))
    pos = cursor.fetchall()
    if not pos:
        return
    all_done = True
    for po_number, is_hold, is_cancelled in pos:
        if is_cancelled: continue
        ordered, loaded = po_progress(cursor, po_number, vehicle_number)
        if is_hold or not (ordered and loaded >= ordered):
            all_done = False
            break
    new_status = 'Ready to Dispatch' if all_done else 'Loading'
    if new_status != current:
        cursor.execute('UPDATE vehicles SET vehicle_status = %s WHERE id = %s', (new_status, vehicle_id))

VEHICLES_LIST_HTML = STYLE_BLOCK + """
<title>Vehicles | REAL INSTANT FOODS</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css">
</head>
<body>
<div class="container">
""" + TOPBAR_TEMPLATE.replace("__NAV__", nav_html('vehicles')) + """
    <div class="card">
        <div class="card-header"><h2>🚚 Live Vehicle Tracking</h2></div>
        <div id="liveMap" style="height:340px; border-radius:12px; overflow:hidden;"></div>
        <div style="font-size:12px; color:var(--text-dim); margin-top:8px;">🟢 recently updated &nbsp; 🟡 a few minutes old &nbsp; 🔴 stale / no signal</div>
    </div>

    <div class="card">
        <div class="card-header">
            <h2>➕ Start New Vehicle Loading</h2>
        </div>
        <form method="POST" action="/vehicles/create" id="createVehicleForm">
            <div class="form-grid">
                <div><label>Vehicle Number *</label><input type="text" name="vehicle_number" required placeholder="e.g. RJ14GA1234"></div>
                <div><label>Driver Name *</label><input type="text" name="driver_name" required></div>
                <div><label>Driver Mobile Number *</label><input type="tel" name="driver_mobile" required pattern="[0-9]{10}" maxlength="10" placeholder="10 digit mobile"></div>
                <div><label>Starting Location *</label><input type="text" name="start_location" required></div>
            </div>
            <div style="margin-top:6px;">
                <label>Attach PO(s) *</label>
                <div id="poRows"></div>
                <button type="button" class="btn btn-outline btn-sm" onclick="addPoRow()">+ Add PO</button>
            </div>
            <button type="submit" class="btn btn-primary btn-block" style="margin-top:14px;">Start Loading</button>
        </form>
    </div>

    <div class="card">
        <div class="card-header"><h2>🟢 Active Vehicles</h2></div>
        <table class="table">
            <tr><th>Vehicle</th><th>Driver</th><th>Status</th><th>POs</th><th>Progress</th><th>Location</th><th></th></tr>
            {% for v in active_vehicles %}
            <tr>
                <td>{{ v.vehicle_number }}</td>
                <td>{{ v.driver_name }}<br><span style="color:var(--text-dim); font-size:11px;">{{ v.driver_mobile }}</span></td>
                <td><span class="badge {{ v.status_class }}">{{ v.vehicle_status }}</span></td>
                <td>{{ v.completed_count }}/{{ v.po_count }} done</td>
                <td style="min-width:120px;">
                    <div class="progress-track"><div class="progress-fill" style="width:{{ v.percent }}%;"></div></div>
                </td>
                <td><span style="color:{{ v.freshness_color }};">●</span> {{ v.freshness_label }}</td>
                <td><a href="/vehicles/{{ v.id }}" class="btn btn-outline btn-sm">Open</a></td>
            </tr>
            {% endfor %}
            {% if not active_vehicles %}<tr><td colspan="7" style="text-align:center; color:var(--text-dim); padding:20px;">No active vehicles right now.</td></tr>{% endif %}
        </table>
    </div>

    <div class="card">
        <div class="card-header"><h2>📜 Completed / Past Vehicles</h2></div>
        <table class="table">
            <tr><th>Vehicle</th><th>Driver</th><th>Status</th><th>Started</th><th></th></tr>
            {% for v in past_vehicles %}
            <tr>
                <td>{{ v.vehicle_number }}</td><td>{{ v.driver_name }}</td>
                <td><span class="badge {{ v.status_class }}">{{ v.vehicle_status }}</span></td>
                <td>{{ v.loading_started_at }}</td>
                <td><a href="/vehicles/{{ v.id }}" class="btn btn-outline btn-sm">Open</a></td>
            </tr>
            {% endfor %}
            {% if not past_vehicles %}<tr><td colspan="5" style="text-align:center; color:var(--text-dim); padding:20px;">Nothing here yet.</td></tr>{% endif %}
        </table>
    </div>
</div>
<script>
const ALL_POS = {{ all_po_numbers|tojson }};
let poRowCount = 0;
function addPoRow() {
    poRowCount++;
    const div = document.createElement('div');
    div.style.cssText = 'display:flex; gap:8px; margin-bottom:8px; align-items:center;';
    div.innerHTML = `<select name="po_numbers" required style="flex:1;"><option value="">Select PO...</option>` +
        ALL_POS.map(p => `<option value="${p}">${p}</option>`).join('') +
        `</select><button type="button" class="btn btn-danger btn-sm" onclick="this.parentElement.remove()">Remove</button>`;
    document.getElementById('poRows').appendChild(div);
}
addPoRow();
document.getElementById('createVehicleForm').addEventListener('submit', function(e){
    const selects = Array.from(document.querySelectorAll('select[name="po_numbers"]')).map(s => s.value).filter(Boolean);
    const unique = new Set(selects);
    if (selects.length === 0) { alert('Kam se kam 1 PO add karo.'); e.preventDefault(); return; }
    if (unique.size !== selects.length) { alert('Same PO do baar add nahi ho sakta.'); e.preventDefault(); return; }
});

const map = L.map('liveMap').setView([20.5937, 78.9629], 5);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '&copy; OpenStreetMap' }).addTo(map);
const markers = {{ map_markers|tojson }};
const bounds = [];
markers.forEach(m => {
    const color = m.color === 'green' ? '#4ade80' : (m.color === 'yellow' ? '#fbbf24' : '#94a3b8');
    const icon = L.divIcon({html: `<div style="background:${color}; width:16px; height:16px; border-radius:50%; border:2px solid white; box-shadow:0 0 6px rgba(0,0,0,0.5);"></div>`, className: ''});
    const mk = L.marker([m.lat, m.lng], {icon}).addTo(map);
    mk.bindPopup(`<b>${m.vehicle_number}</b><br>Driver: ${m.driver_name} (${m.driver_mobile})<br>Status: ${m.vehicle_status}<br>POs: ${m.po_count}<br>Last update: ${m.freshness}<br><a href="/vehicles/${m.id}">Open →</a>`);
    bounds.push([m.lat, m.lng]);
});
if (bounds.length) map.fitBounds(bounds, {padding:[30,30]});
</script>
</body></html>
"""

VEHICLE_DETAIL_HTML = STYLE_BLOCK + """
<title>{{ v.vehicle_number }} | Vehicle Tracking</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css">
</head>
<body>
<div class="container">
""" + TOPBAR_TEMPLATE.replace("__NAV__", nav_html('vehicles')) + """
    <div class="card">
        <div class="card-header">
            <h2>🚛 {{ v.vehicle_number }} <span class="badge {{ v.status_class }}">{{ v.vehicle_status }}</span></h2>
            <a href="/vehicles" class="btn btn-outline btn-sm">← Back</a>
        </div>
        <div class="form-grid">
            <div><strong>Driver:</strong> {{ v.driver_name }}</div>
            <div><strong>Mobile:</strong> {{ v.driver_mobile }}</div>
            <div><strong>Start Location:</strong> {{ v.start_location }}</div>
            <div><strong>Loading Started:</strong> {{ v.loading_started_at }}</div>
            <div><strong>Tracking:</strong> {{ v.tracking_status }} — <span style="color:{{ v.freshness_color }};">●</span> {{ v.freshness_label }}</div>
        </div>
        <div style="margin-top:14px; display:flex; gap:8px; flex-wrap:wrap;">
            {% if v.vehicle_status not in ['In Transit','Delivered','Cancelled'] %}
                <form method="POST" action="/vehicles/{{ v.id }}/regen_token" style="display:inline;"><button class="btn btn-outline btn-sm">🔗 Generate/Reset Tracking Link</button></form>
                {% if v.tracking_token %}
                <button class="btn btn-outline btn-sm" onclick="navigator.clipboard.writeText('{{ track_url }}'); alert('Link copied!');">📋 Copy Tracking Link</button>
                <a class="btn btn-outline btn-sm" target="_blank" href="https://wa.me/91{{ v.driver_mobile }}?text={{ track_url_encoded }}">📲 Share on WhatsApp</a>
                {% endif %}
                {% if v.tracking_status == 'active' %}
                <form method="POST" action="/vehicles/{{ v.id }}/stop_tracking" style="display:inline;"><button class="btn btn-danger btn-sm">⛔ Stop Tracking</button></form>
                {% endif %}
            {% endif %}
            {% if v.vehicle_status == 'Ready to Dispatch' %}
                <form method="POST" action="/vehicles/{{ v.id }}/dispatch" style="display:inline;"><button class="btn btn-primary btn-sm">🚀 Dispatch Vehicle</button></form>
            {% endif %}
            {% if v.vehicle_status == 'In Transit' %}
                <form method="POST" action="/vehicles/{{ v.id }}/deliver" style="display:inline;"><button class="btn btn-primary btn-sm">✅ Mark Delivered</button></form>
            {% endif %}
        </div>
    </div>

    {% if v.current_latitude %}
    <div class="card">
        <div class="card-header"><h2>📍 Live Location &amp; Route</h2></div>
        <div id="routeMap" style="height:300px; border-radius:12px;"></div>
    </div>
    {% endif %}

    <div class="card">
        <div class="card-header">
            <h2>📦 Attached POs — {{ v.completed_count }}/{{ v.po_count }} completed ({{ v.percent }}% overall)</h2>
        </div>
        <div class="progress-track" style="margin-bottom:14px;"><div class="progress-fill" style="width:{{ v.percent }}%;"></div></div>
        <table class="table">
            <tr><th>PO Number</th><th>Ordered</th><th>Loaded</th><th>Pending</th><th>Status</th><th></th></tr>
            {% for p in pos %}
            <tr>
                <td>{{ p.po_number }} <span style="color:var(--text-dim); font-size:11px;">({{ p.company }})</span></td>
                <td>{{ p.ordered }}</td><td>{{ p.loaded }}</td><td>{{ p.pending }}</td>
                <td><span class="badge {{ p.status_class }}">{{ p.status }}</span></td>
                <td style="white-space:nowrap;">
                    {% if v.vehicle_status not in ['In Transit','Delivered','Cancelled'] %}
                    <form method="POST" action="/vehicles/{{ v.id }}/set_active_po" style="display:inline;">
                        <input type="hidden" name="po_number" value="{{ p.po_number }}">
                        <button class="btn btn-outline btn-sm">Scan This</button>
                    </form>
                    <form method="POST" action="/vehicles/{{ v.id }}/po_status" style="display:inline;">
                        <input type="hidden" name="po_number" value="{{ p.po_number }}">
                        <input type="hidden" name="action" value="{{ 'resume' if p.is_hold else 'hold' }}">
                        <button class="btn btn-outline btn-sm">{{ 'Resume' if p.is_hold else 'Hold' }}</button>
                    </form>
                    <form method="POST" action="/vehicles/{{ v.id }}/remove_po" style="display:inline;" onsubmit="return confirm('Is PO ko vehicle se remove karein?');">
                        <input type="hidden" name="po_number" value="{{ p.po_number }}">
                        <button class="btn btn-danger btn-sm">Remove</button>
                    </form>
                    {% endif %}
                </td>
            </tr>
            {% endfor %}
        </table>
        {% if v.vehicle_status not in ['In Transit','Delivered','Cancelled'] %}
        <form method="POST" action="/vehicles/{{ v.id }}/add_po" style="margin-top:14px; display:flex; gap:8px;">
            <select name="po_number" required style="flex:1;">
                <option value="">+ Add another PO...</option>
                {% for po in available_pos %}<option value="{{ po }}">{{ po }}</option>{% endfor %}
            </select>
            <button class="btn btn-outline btn-sm">Add PO</button>
        </form>
        {% endif %}
    </div>
</div>
<script>
{% if v.current_latitude %}
const map = L.map('routeMap').setView([{{ v.current_latitude }}, {{ v.current_longitude }}], 13);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '&copy; OpenStreetMap' }).addTo(map);
const route = {{ route_points|tojson }};
if (route.length > 1) L.polyline(route, {color:'#3b82f6', weight:4}).addTo(map);
L.marker([{{ v.current_latitude }}, {{ v.current_longitude }}]).addTo(map).bindPopup('Current location — {{ v.freshness_label }}').openPopup();
{% endif %}
</script>
</body></html>
"""

TRACK_HTML = STYLE_BLOCK + """
<title>Live Tracking | {{ v.vehicle_number }}</title>
</head>
<body>
<div class="container" style="max-width:480px;">
    <div class="card" style="text-align:center; margin-top:40px;">
        <div style="font-size:40px;">🚚</div>
        <h2 style="margin:10px 0 4px;">{{ v.vehicle_number }}</h2>
        <div style="color:var(--text-dim);">Driver: {{ v.driver_name }}</div>
        <div class="badge {{ v.status_class }}" style="margin-top:10px; display:inline-block;">{{ v.vehicle_status }}</div>

        {% if not trackable %}
        <p style="margin-top:24px; color:var(--text-dim);">Yeh tracking link ab active nahi hai. Trip complete ho chuki hai ya tracking band kar di gayi hai.</p>
        {% else %}
        <p style="margin-top:20px; font-size:13px; color:var(--text-dim);">आपकी live location vehicle tracking के लिए share की जा रही है।</p>
        <button id="startBtn" class="btn btn-primary btn-block" style="margin-top:16px;" onclick="startTracking()">Start Location Tracking</button>
        <div id="status" style="margin-top:14px; font-size:13px; color:var(--text-dim);"></div>
        {% endif %}
    </div>
</div>
<script>
const TOKEN = "{{ token }}";
let watchId = null;
function startTracking() {
    if (!navigator.geolocation) { document.getElementById('status').innerText = 'GPS is not supported on this browser.'; return; }
    document.getElementById('startBtn').innerText = '📡 Tracking Live...';
    document.getElementById('startBtn').disabled = true;
    function send(pos) {
        fetch('/track/' + TOKEN + '/ping', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({lat: pos.coords.latitude, lng: pos.coords.longitude, accuracy: pos.coords.accuracy})
        }).then(r => r.json()).then(d => {
            document.getElementById('status').innerText = d.ok ? ('Last sent: ' + new Date().toLocaleTimeString()) : 'Tracking stopped by admin.';
            if (!d.ok && watchId) { navigator.geolocation.clearWatch(watchId); }
        }).catch(()=>{});
    }
    navigator.geolocation.getCurrentPosition(send, err => { document.getElementById('status').innerText = 'Location permission denied.'; }, {enableHighAccuracy:true});
    watchId = navigator.geolocation.watchPosition(send, err => {}, {enableHighAccuracy:true, maximumAge: 15000});
    setInterval(() => { navigator.geolocation.getCurrentPosition(send, ()=>{}, {enableHighAccuracy:true}); }, 20000);
}
</script>
</body></html>
"""

def _vehicle_row_dict(cursor, row):
    (vid, vehicle_number, driver_name, driver_mobile, start_location, vehicle_status,
     loading_started_at, cur_lat, cur_lng, cur_acc, last_loc_at, tracking_token,
     tracking_status, dispatched_at, delivered_at) = row
    cursor.execute('SELECT po_number, is_hold, is_cancelled FROM vehicle_po_map WHERE vehicle_id = %s', (vid,))
    pos = cursor.fetchall()
    completed, total_ordered, total_loaded = 0, 0, 0
    active_count = 0
    for po_number, is_hold, is_cancelled in pos:
        if is_cancelled: continue
        active_count += 1
        ordered, loaded = po_progress(cursor, po_number, vehicle_number)
        total_ordered += ordered
        total_loaded += min(loaded, ordered) if ordered else loaded
        if ordered and loaded >= ordered: completed += 1
    percent = min(100, round((total_loaded / total_ordered) * 100)) if total_ordered else 0
    label, color, _ = freshness_info(last_loc_at)
    status_class = {'Loading':'badge-blue','Ready to Dispatch':'badge-green','In Transit':'badge-blue','Delivered':'badge-green','Cancelled':'badge-amber'}.get(vehicle_status, 'badge-amber')
    return {
        'id': vid, 'vehicle_number': vehicle_number, 'driver_name': driver_name, 'driver_mobile': driver_mobile,
        'start_location': start_location, 'vehicle_status': vehicle_status, 'loading_started_at': loading_started_at,
        'current_latitude': cur_lat, 'current_longitude': cur_lng, 'tracking_token': tracking_token,
        'tracking_status': tracking_status, 'po_count': active_count, 'completed_count': completed,
        'percent': percent, 'freshness_label': label, 'freshness_color': color, 'status_class': status_class,
    }

@app.route('/vehicles')
def vehicles_page():
    if not session.get('logged_in'): return redirect('/login')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT DISTINCT po_number FROM po_items ORDER BY po_number')
    all_po_numbers = [r[0] for r in cursor.fetchall()]
    cursor.execute('''SELECT id, vehicle_number, driver_name, driver_mobile, start_location, vehicle_status,
        loading_started_at, current_latitude, current_longitude, current_accuracy, last_location_at,
        tracking_token, tracking_status, dispatched_at, delivered_at FROM vehicles ORDER BY id DESC''')
    rows = cursor.fetchall()
    active_vehicles, past_vehicles, map_markers = [], [], []
    for row in rows:
        recompute_vehicle_status(cursor, row[0])
        d = _vehicle_row_dict(cursor, row)
        if d['vehicle_status'] in ('Delivered', 'Cancelled'):
            past_vehicles.append(d)
        else:
            active_vehicles.append(d)
        if d['current_latitude'] is not None:
            map_markers.append({'id': d['id'], 'lat': d['current_latitude'], 'lng': d['current_longitude'],
                                 'vehicle_number': d['vehicle_number'], 'driver_name': d['driver_name'],
                                 'driver_mobile': d['driver_mobile'], 'vehicle_status': d['vehicle_status'],
                                 'po_count': d['po_count'], 'color': d['freshness_color'], 'freshness': d['freshness_label']})
    conn.commit()
    conn.close()
    return render_template_string(VEHICLES_LIST_HTML, active_vehicles=active_vehicles, past_vehicles=past_vehicles,
                                   all_po_numbers=all_po_numbers, map_markers=map_markers)

@app.route('/vehicles/create', methods=['POST'])
def vehicles_create():
    if not session.get('logged_in'): return redirect('/login')
    vehicle_number = request.form.get('vehicle_number', '').strip().upper()
    driver_name = request.form.get('driver_name', '').strip()
    driver_mobile = request.form.get('driver_mobile', '').strip()
    start_location = request.form.get('start_location', '').strip()
    po_numbers = [p.strip() for p in request.form.getlist('po_numbers') if p.strip()]
    po_numbers = list(dict.fromkeys(po_numbers))  # de-dupe, keep order

    if not (vehicle_number and driver_name and driver_mobile and start_location and po_numbers):
        return "Sabhi required fields aur kam se kam 1 PO zaroori hai. <a href='/vehicles'>Back</a>", 400
    if not re.match(r'^[0-9]{10}$', driver_mobile):
        return "Driver mobile number 10 digit ka valid number hona chahiye. <a href='/vehicles'>Back</a>", 400

    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM vehicles WHERE vehicle_number = %s AND vehicle_status NOT IN ('Delivered','Cancelled')", (vehicle_number,))
    if cursor.fetchone():
        conn.close()
        return f"Vehicle {vehicle_number} already has an active loading session. <a href='/vehicles'>Back</a>", 400

    for po in po_numbers:
        cursor.execute('''SELECT v.vehicle_number FROM vehicle_po_map m JOIN vehicles v ON v.id = m.vehicle_id
                           WHERE m.po_number = %s AND m.is_cancelled = FALSE AND v.vehicle_status NOT IN ('Delivered','Cancelled')''', (po,))
        clash = cursor.fetchone()
        if clash:
            conn.close()
            return f"PO {po} pehle se vehicle {clash[0]} mein active hai. <a href='/vehicles'>Back</a>", 400

    token = gen_tracking_token()
    cursor.execute('''INSERT INTO vehicles (vehicle_number, driver_name, driver_mobile, start_location, vehicle_status,
                       loading_started_at, tracking_token, tracking_status) VALUES (%s,%s,%s,%s,'Loading',%s,%s,'active') RETURNING id''',
                   (vehicle_number, driver_name, driver_mobile, start_location, now_ist().strftime("%d %b %Y, %I:%M %p"), token))
    vehicle_id = cursor.fetchone()[0]
    for po in po_numbers:
        cursor.execute('SELECT company FROM po_items WHERE po_number = %s LIMIT 1', (po,))
        comp = cursor.fetchone()
        company = comp[0] if comp else ''
        cursor.execute('INSERT INTO vehicle_po_map (vehicle_id, po_number, company, created_at) VALUES (%s,%s,%s,%s)',
                       (vehicle_id, po, company, now_ist().isoformat()))
    conn.commit()
    conn.close()
    return redirect(f'/vehicles/{vehicle_id}')

@app.route('/vehicles/<int:vehicle_id>')
def vehicle_detail(vehicle_id):
    if not session.get('logged_in'): return redirect('/login')
    conn = get_conn()
    cursor = conn.cursor()
    recompute_vehicle_status(cursor, vehicle_id)
    cursor.execute('''SELECT id, vehicle_number, driver_name, driver_mobile, start_location, vehicle_status,
        loading_started_at, current_latitude, current_longitude, current_accuracy, last_location_at,
        tracking_token, tracking_status, dispatched_at, delivered_at FROM vehicles WHERE id = %s''', (vehicle_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return "Vehicle not found. <a href='/vehicles'>Back</a>", 404
    v = _vehicle_row_dict(cursor, row)

    cursor.execute('SELECT po_number, company, is_hold, is_cancelled FROM vehicle_po_map WHERE vehicle_id = %s ORDER BY id', (vehicle_id,))
    pos = []
    for po_number, company, is_hold, is_cancelled in cursor.fetchall():
        ordered, loaded = po_progress(cursor, po_number, v['vehicle_number'])
        pending = max(ordered - loaded, 0)
        status, status_class = po_status_label(ordered, loaded, is_hold, is_cancelled)
        pos.append({'po_number': po_number, 'company': company, 'ordered': ordered, 'loaded': loaded,
                    'pending': pending, 'status': status, 'status_class': status_class, 'is_hold': is_hold})

    attached = {p['po_number'] for p in pos}
    cursor.execute('SELECT DISTINCT po_number FROM po_items ORDER BY po_number')
    available_pos = [p[0] for p in cursor.fetchall() if p[0] not in attached]

    cursor.execute('SELECT latitude, longitude FROM location_history WHERE vehicle_id = %s ORDER BY id ASC LIMIT 500', (vehicle_id,))
    route_points = [[r[0], r[1]] for r in cursor.fetchall()]

    conn.commit()
    conn.close()
    track_url = request.url_root.rstrip('/') + f"/track/{v['tracking_token']}" if v['tracking_token'] else ''
    return render_template_string(VEHICLE_DETAIL_HTML, v=v, pos=pos, available_pos=available_pos,
                                   route_points=route_points, track_url=track_url, track_url_encoded=quote(track_url))

@app.route('/vehicles/<int:vehicle_id>/add_po', methods=['POST'])
def vehicle_add_po(vehicle_id):
    if not session.get('logged_in'): return redirect('/login')
    po = request.form.get('po_number', '').strip()
    if not po: return redirect(f'/vehicles/{vehicle_id}')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT vehicle_number FROM vehicles WHERE id = %s', (vehicle_id,))
    vrow = cursor.fetchone()
    if not vrow:
        conn.close(); return redirect('/vehicles')
    cursor.execute('''SELECT v.vehicle_number FROM vehicle_po_map m JOIN vehicles v ON v.id = m.vehicle_id
                       WHERE m.po_number = %s AND m.is_cancelled = FALSE AND v.vehicle_status NOT IN ('Delivered','Cancelled')''', (po,))
    clash = cursor.fetchone()
    if clash:
        conn.close()
        return f"PO {po} already active on vehicle {clash[0]}. <a href='/vehicles/{vehicle_id}'>Back</a>", 400
    cursor.execute('SELECT company FROM po_items WHERE po_number = %s LIMIT 1', (po,))
    comp = cursor.fetchone()
    company = comp[0] if comp else ''
    cursor.execute('INSERT INTO vehicle_po_map (vehicle_id, po_number, company, created_at) VALUES (%s,%s,%s,%s) ON CONFLICT (vehicle_id, po_number) DO NOTHING',
                   (vehicle_id, po, company, now_ist().isoformat()))
    recompute_vehicle_status(cursor, vehicle_id)
    conn.commit()
    conn.close()
    return redirect(f'/vehicles/{vehicle_id}')

@app.route('/vehicles/<int:vehicle_id>/remove_po', methods=['POST'])
def vehicle_remove_po(vehicle_id):
    if not session.get('logged_in'): return redirect('/login')
    po = request.form.get('po_number', '').strip()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM vehicle_po_map WHERE vehicle_id = %s AND po_number = %s', (vehicle_id, po))
    recompute_vehicle_status(cursor, vehicle_id)
    conn.commit()
    conn.close()
    return redirect(f'/vehicles/{vehicle_id}')

@app.route('/vehicles/<int:vehicle_id>/po_status', methods=['POST'])
def vehicle_po_status(vehicle_id):
    if not session.get('logged_in'): return redirect('/login')
    po = request.form.get('po_number', '').strip()
    action = request.form.get('action', '')
    conn = get_conn()
    cursor = conn.cursor()
    if action == 'hold':
        cursor.execute('UPDATE vehicle_po_map SET is_hold = TRUE WHERE vehicle_id = %s AND po_number = %s', (vehicle_id, po))
    elif action == 'resume':
        cursor.execute('UPDATE vehicle_po_map SET is_hold = FALSE WHERE vehicle_id = %s AND po_number = %s', (vehicle_id, po))
    recompute_vehicle_status(cursor, vehicle_id)
    conn.commit()
    conn.close()
    return redirect(f'/vehicles/{vehicle_id}')

@app.route('/vehicles/<int:vehicle_id>/set_active_po', methods=['POST'])
def vehicle_set_active_po(vehicle_id):
    if not session.get('logged_in'): return redirect('/login')
    po = request.form.get('po_number', '').strip()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT vehicle_number, start_location FROM vehicles WHERE id = %s', (vehicle_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        set_active_session(po, row[0], row[1])
    return redirect('/')

@app.route('/vehicles/<int:vehicle_id>/regen_token', methods=['POST'])
def vehicle_regen_token(vehicle_id):
    if not session.get('logged_in'): return redirect('/login')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE vehicles SET tracking_token = %s, tracking_status = 'active' WHERE id = %s", (gen_tracking_token(), vehicle_id))
    conn.commit()
    conn.close()
    return redirect(f'/vehicles/{vehicle_id}')

@app.route('/vehicles/<int:vehicle_id>/stop_tracking', methods=['POST'])
def vehicle_stop_tracking(vehicle_id):
    if not session.get('logged_in'): return redirect('/login')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE vehicles SET tracking_status = 'stopped' WHERE id = %s", (vehicle_id,))
    conn.commit()
    conn.close()
    return redirect(f'/vehicles/{vehicle_id}')

@app.route('/vehicles/<int:vehicle_id>/dispatch', methods=['POST'])
def vehicle_dispatch(vehicle_id):
    if not session.get('logged_in'): return redirect('/login')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE vehicles SET vehicle_status = 'In Transit', dispatched_at = %s WHERE id = %s",
                   (now_ist().strftime("%d %b %Y, %I:%M %p"), vehicle_id))
    conn.commit()
    conn.close()
    return redirect(f'/vehicles/{vehicle_id}')

@app.route('/vehicles/<int:vehicle_id>/deliver', methods=['POST'])
def vehicle_deliver(vehicle_id):
    if not session.get('logged_in'): return redirect('/login')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE vehicles SET vehicle_status = 'Delivered', delivered_at = %s, tracking_status = 'stopped' WHERE id = %s",
                   (now_ist().strftime("%d %b %Y, %I:%M %p"), vehicle_id))
    conn.commit()
    conn.close()
    return redirect(f'/vehicles/{vehicle_id}')

@app.route('/track/<token>')
def track_page(token):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('''SELECT id, vehicle_number, driver_name, driver_mobile, start_location, vehicle_status,
        tracking_status FROM vehicles WHERE tracking_token = %s''', (token,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return "Invalid or expired tracking link.", 404
    vid, vehicle_number, driver_name, driver_mobile, start_location, vehicle_status, tracking_status = row
    status_class = {'Loading':'badge-blue','Ready to Dispatch':'badge-green','In Transit':'badge-blue','Delivered':'badge-green','Cancelled':'badge-amber'}.get(vehicle_status, 'badge-amber')
    v = {'vehicle_number': vehicle_number, 'driver_name': driver_name, 'vehicle_status': vehicle_status, 'status_class': status_class}
    trackable = tracking_status == 'active' and vehicle_status not in ('Delivered', 'Cancelled')
    return render_template_string(TRACK_HTML, v=v, token=token, trackable=trackable)

@app.route('/track/<token>/ping', methods=['POST'])
def track_ping(token):
    data = request.get_json(silent=True) or {}
    lat, lng, acc = data.get('lat'), data.get('lng'), data.get('accuracy')
    if lat is None or lng is None:
        return jsonify({'ok': False, 'error': 'missing coordinates'}), 400
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, vehicle_status, tracking_status FROM vehicles WHERE tracking_token = %s", (token,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return jsonify({'ok': False, 'error': 'invalid token'}), 404
    vid, vehicle_status, tracking_status = row
    if tracking_status != 'active' or vehicle_status in ('Delivered', 'Cancelled'):
        conn.close()
        return jsonify({'ok': False, 'error': 'tracking not active'}), 403
    ts = now_ist().isoformat()
    cursor.execute('''UPDATE vehicles SET current_latitude=%s, current_longitude=%s, current_accuracy=%s, last_location_at=%s WHERE id=%s''',
                   (lat, lng, acc, ts, vid))
    cursor.execute('INSERT INTO location_history (vehicle_id, latitude, longitude, accuracy, recorded_at) VALUES (%s,%s,%s,%s,%s)',
                   (vid, lat, lng, acc, ts))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

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
