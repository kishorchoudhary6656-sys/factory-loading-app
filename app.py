from flask import Flask, render_template_string, request, redirect, session, Response, jsonify
import os, csv, io, re, json, secrets
import psycopg2
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
from werkzeug.security import generate_password_hash, check_password_hash

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

    # --- Multi-tenant foundation: factories (companies using this platform) + users (people who log in) ---
    cursor.execute('''CREATE TABLE IF NOT EXISTS factories (
        id SERIAL PRIMARY KEY,
        company_name TEXT, display_name TEXT, logo_url TEXT,
        address TEXT, city TEXT, state TEXT, country TEXT,
        phone TEXT, email TEXT, website TEXT, tax_number TEXT,
        status TEXT DEFAULT 'Active',
        plan TEXT DEFAULT 'Free', user_limit INTEGER DEFAULT 5, vehicle_limit INTEGER DEFAULT 10,
        subscription_status TEXT DEFAULT 'Active', plan_start_date TEXT, plan_end_date TEXT,
        created_at TEXT, updated_at TEXT, created_by TEXT
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        factory_id INTEGER REFERENCES factories(id),
        name TEXT, mobile TEXT, email TEXT,
        username TEXT UNIQUE,
        password_hash TEXT,
        role TEXT DEFAULT 'Factory Admin',
        status TEXT DEFAULT 'Active',
        last_login TEXT,
        created_at TEXT, updated_at TEXT
    )''')
    # Audit trail — who did what, when, scoped per factory (Super Admin actions have factory_id = the affected factory)
    cursor.execute('''CREATE TABLE IF NOT EXISTS audit_log (
        id SERIAL PRIMARY KEY,
        factory_id INTEGER,
        user_id INTEGER, user_name TEXT,
        action TEXT, module TEXT, record_id TEXT, details TEXT,
        timestamp TEXT
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS po_items (id SERIAL PRIMARY KEY, factory_id INTEGER, po_number TEXT, item_name TEXT, weight TEXT, ordered_qty INTEGER, barcode TEXT, company TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS dispatch_log (id SERIAL PRIMARY KEY, factory_id INTEGER, po_number TEXT, vehicle_no TEXT, location TEXT, product_name TEXT, loaded_qty INTEGER, timestamp TEXT, barcode TEXT)''')
    # Per-factory active dispatch session (NOT per-browser) so every device in that factory sees the same live session
    cursor.execute('''CREATE TABLE IF NOT EXISTS app_state (factory_id INTEGER, key TEXT, value TEXT, PRIMARY KEY (factory_id, key))''')
    # Companies (Zepto, Flipkart, Reliance, Anand Sweets, etc.) — each PO belongs to one company/folder, scoped per factory
    cursor.execute('''CREATE TABLE IF NOT EXISTS companies (id SERIAL PRIMARY KEY, factory_id INTEGER, name TEXT, sub_brands TEXT)''')
    # Quality Checkers — registered with a reference photo for identity check-in
    cursor.execute('''CREATE TABLE IF NOT EXISTS quality_checkers (id SERIAL PRIMARY KEY, factory_id INTEGER, name TEXT, photo_data TEXT, created_at TEXT)''')
    # A pre-loaded catalog of barcodes (independent of any single PO) so scanning always matches
    cursor.execute('''CREATE TABLE IF NOT EXISTS barcode_catalog (id SERIAL PRIMARY KEY, factory_id INTEGER, barcode TEXT, item_name TEXT)''')
    # Daily Production log — one row per packed unit logged by a Quality Checker
    cursor.execute('''CREATE TABLE IF NOT EXISTS daily_production (
        id SERIAL PRIMARY KEY,
        factory_id INTEGER,
        company TEXT, sub_brand TEXT, item_name_entered TEXT, item_name TEXT, barcode TEXT,
        packing_date TEXT, use_by_date TEXT, batch_number TEXT, quantity INTEGER,
        qc_name TEXT, qc_photo TEXT,
        prod_date TEXT, prod_time TEXT, created_at TEXT
    )''')
    # --- Vehicle Master + Trip/Loading Sessions + Live GPS Tracking ---
    # Vehicle Master = permanent vehicle record (always exists, independent of any loading).
    # Trip = one particular loading/dispatch operation for that vehicle (a vehicle can have many trips over time).
    # Migrate old single-table 'vehicles' (Phase 1) into 'trips' + new 'vehicle_master', if it exists.
    cursor.execute("SELECT to_regclass('public.vehicles')")
    old_vehicles_exists = cursor.fetchone()[0] is not None
    cursor.execute("SELECT to_regclass('public.trips')")
    trips_exists = cursor.fetchone()[0] is not None
    if old_vehicles_exists and not trips_exists:
        cursor.execute('ALTER TABLE vehicles RENAME TO trips')

    cursor.execute('''CREATE TABLE IF NOT EXISTS vehicle_master (
        id SERIAL PRIMARY KEY,
        factory_id INTEGER,
        vehicle_number TEXT,
        is_active BOOLEAN DEFAULT TRUE,
        current_latitude DOUBLE PRECISION, current_longitude DOUBLE PRECISION, current_accuracy DOUBLE PRECISION,
        last_location_at TEXT,
        gps_status TEXT DEFAULT 'offline',
        created_at TEXT, updated_at TEXT
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS trips (
        id SERIAL PRIMARY KEY,
        factory_id INTEGER,
        vehicle_id INTEGER REFERENCES vehicle_master(id),
        vehicle_number TEXT,
        driver_name TEXT, driver_mobile TEXT, start_location TEXT,
        trip_status TEXT DEFAULT 'Loading',
        loading_started_at TEXT,
        tracking_token TEXT UNIQUE,
        tracking_status TEXT DEFAULT 'stopped',
        dispatched_at TEXT, delivered_at TEXT
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS vehicle_po_map (
        id SERIAL PRIMARY KEY,
        factory_id INTEGER,
        trip_id INTEGER REFERENCES trips(id) ON DELETE CASCADE,
        po_number TEXT, company TEXT,
        is_hold BOOLEAN DEFAULT FALSE, is_cancelled BOOLEAN DEFAULT FALSE,
        created_at TEXT,
        UNIQUE(trip_id, po_number)
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS location_history (
        id SERIAL PRIMARY KEY,
        factory_id INTEGER,
        vehicle_id INTEGER REFERENCES vehicle_master(id) ON DELETE CASCADE,
        trip_id INTEGER REFERENCES trips(id) ON DELETE CASCADE,
        latitude DOUBLE PRECISION, longitude DOUBLE PRECISION, accuracy DOUBLE PRECISION,
        recorded_at TEXT
    )''')

    # --- Multi-tenant column backfill for tables that may already exist from before this upgrade ---
    for tbl in ['po_items', 'dispatch_log', 'companies', 'quality_checkers', 'barcode_catalog',
                'daily_production', 'vehicle_master', 'trips', 'vehicle_po_map', 'location_history']:
        cursor.execute(f'ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS factory_id INTEGER')
    # app_state might already exist from before with a single-column PK; rebuild it as composite-keyed if so
    cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='app_state' AND column_name='factory_id'")
    if not cursor.fetchone():
        cursor.execute('ALTER TABLE app_state ADD COLUMN factory_id INTEGER')
        cursor.execute('ALTER TABLE app_state DROP CONSTRAINT IF EXISTS app_state_pkey')

    # Old single-column UNIQUE constraints (from before multi-tenancy) must become factory-scoped,
    # since two different factories can legitimately have the same company/checker name, barcode, or vehicle number.
    for constraint, table in [
        ('companies_name_key', 'companies'),
        ('quality_checkers_name_key', 'quality_checkers'),
        ('barcode_catalog_barcode_key', 'barcode_catalog'),
        ('vehicle_master_vehicle_number_key', 'vehicle_master'),
    ]:
        cursor.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {constraint}')

    # --- Ensure a default factory + admin user exist (first-ever boot, or upgrading from single-tenant) ---
    cursor.execute('SELECT id FROM factories ORDER BY id ASC LIMIT 1')
    row = cursor.fetchone()
    if row:
        default_factory_id = row[0]
    else:
        ts = now_ist().isoformat()
        cursor.execute('''INSERT INTO factories (company_name, display_name, status, created_at, updated_at)
                           VALUES (%s, %s, 'Active', %s, %s) RETURNING id''',
                       ('Real Instant Foods', 'Real Instant Foods', ts, ts))
        default_factory_id = cursor.fetchone()[0]
    cursor.execute('SELECT id FROM users LIMIT 1')
    if not cursor.fetchone():
        ts = now_ist().isoformat()
        # Preserves the exact old login (password: real@8283) so the existing team's workflow doesn't change at all.
        cursor.execute('''INSERT INTO users (factory_id, name, username, password_hash, role, status, created_at, updated_at)
                           VALUES (%s, %s, %s, %s, %s, 'Active', %s, %s)''',
                       (default_factory_id, 'Admin', 'admin', generate_password_hash('real@8283'), 'Factory Admin', ts, ts))

    # Bootstrap a single platform-level Super Admin account (factory_id = NULL) the very first time this
    # migration runs, so there's always a way into the /superadmin panel. Change this password after first login.
    cursor.execute("SELECT id FROM users WHERE role = 'Super Admin' LIMIT 1")
    if not cursor.fetchone():
        ts = now_ist().isoformat()
        cursor.execute('''INSERT INTO users (factory_id, name, username, password_hash, role, status, created_at, updated_at)
                           VALUES (NULL, %s, %s, %s, 'Super Admin', 'Active', %s, %s)''',
                       ('Platform Owner', 'superadmin', generate_password_hash('ChangeMe@2026'), ts, ts))

    # Backfill factory_id on any pre-existing rows (from before multi-tenancy) into the default factory
    for tbl in ['po_items', 'dispatch_log', 'companies', 'quality_checkers', 'barcode_catalog',
                'daily_production', 'vehicle_master', 'trips', 'vehicle_po_map', 'location_history', 'app_state']:
        cursor.execute(f'UPDATE {tbl} SET factory_id = %s WHERE factory_id IS NULL', (default_factory_id,))

    # Re-establish app_state's primary key as (factory_id, key) now that factory_id is populated
    cursor.execute("SELECT constraint_name FROM information_schema.table_constraints WHERE table_name='app_state' AND constraint_type='PRIMARY KEY'")
    if not cursor.fetchone():
        cursor.execute('ALTER TABLE app_state ADD PRIMARY KEY (factory_id, key)')

    # Re-create the old uniqueness guarantees, now scoped per factory instead of globally
    cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_companies_factory_name ON companies(factory_id, name)')
    cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_qc_factory_name ON quality_checkers(factory_id, name)')
    cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_barcode_factory_code ON barcode_catalog(factory_id, barcode)')
    cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_vehicle_factory_number ON vehicle_master(factory_id, vehicle_number)')

    # Defensive column migrations: make sure old columns (from Phase 1) exist so backfill queries below never fail,
    # regardless of whether this is a fresh install or an upgrade from the old single-table schema.
    cursor.execute('ALTER TABLE trips ADD COLUMN IF NOT EXISTS vehicle_id INTEGER')
    cursor.execute('ALTER TABLE trips ADD COLUMN IF NOT EXISTS vehicle_number TEXT')
    cursor.execute('ALTER TABLE trips ADD COLUMN IF NOT EXISTS trip_status TEXT')
    cursor.execute('ALTER TABLE trips ADD COLUMN IF NOT EXISTS vehicle_status TEXT')  # old Phase-1 column name
    cursor.execute('ALTER TABLE trips ADD COLUMN IF NOT EXISTS current_latitude DOUBLE PRECISION')
    cursor.execute('ALTER TABLE trips ADD COLUMN IF NOT EXISTS current_longitude DOUBLE PRECISION')
    cursor.execute('ALTER TABLE trips ADD COLUMN IF NOT EXISTS current_accuracy DOUBLE PRECISION')
    cursor.execute('ALTER TABLE trips ADD COLUMN IF NOT EXISTS last_location_at TEXT')
    cursor.execute("UPDATE trips SET trip_status = vehicle_status WHERE trip_status IS NULL AND vehicle_status IS NOT NULL")
    cursor.execute("UPDATE trips SET trip_status = 'Loading' WHERE trip_status IS NULL")

    # vehicle_po_map: rename old 'vehicle_id' column (which pointed at a trip) to 'trip_id' if upgrading
    cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='vehicle_po_map' AND column_name='vehicle_id'")
    if cursor.fetchone():
        cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='vehicle_po_map' AND column_name='trip_id'")
        if not cursor.fetchone():
            cursor.execute('ALTER TABLE vehicle_po_map RENAME COLUMN vehicle_id TO trip_id')

    # location_history: rename old 'vehicle_id' (which pointed at a trip) to 'trip_id', add fresh 'vehicle_id' pointing at vehicle_master
    cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='location_history' AND column_name='trip_id'")
    has_trip_id_col = cursor.fetchone() is not None
    if not has_trip_id_col:
        cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='location_history' AND column_name='vehicle_id'")
        if cursor.fetchone():
            cursor.execute('ALTER TABLE location_history RENAME COLUMN vehicle_id TO trip_id')
    cursor.execute('ALTER TABLE location_history ADD COLUMN IF NOT EXISTS vehicle_id INTEGER')

    # Backfill: ensure every distinct (factory, vehicle_number) seen in trips has a permanent vehicle_master row
    cursor.execute('SELECT DISTINCT factory_id, vehicle_number FROM trips WHERE vehicle_number IS NOT NULL')
    for (tfid, vnum) in cursor.fetchall():
        cursor.execute('INSERT INTO vehicle_master (factory_id, vehicle_number, created_at, updated_at) VALUES (%s,%s,%s,%s) ON CONFLICT (factory_id, vehicle_number) DO NOTHING',
                       (tfid, vnum, now_ist().isoformat(), now_ist().isoformat()))
    cursor.execute('UPDATE trips SET vehicle_id = vm.id FROM vehicle_master AS vm WHERE vm.vehicle_number = trips.vehicle_number AND vm.factory_id = trips.factory_id AND trips.vehicle_id IS NULL')
    # Backfill vehicle_master's current location from each vehicle's most-recently-updated trip (old Phase-1 data)
    cursor.execute('''UPDATE vehicle_master AS vm SET current_latitude = t.current_latitude, current_longitude = t.current_longitude,
        current_accuracy = t.current_accuracy, last_location_at = t.last_location_at
        FROM trips AS t WHERE t.vehicle_id = vm.id AND t.last_location_at IS NOT NULL
        AND (vm.last_location_at IS NULL OR t.last_location_at > vm.last_location_at)''')
    # Backfill location_history.vehicle_id from its trip's vehicle_id, for any rows still missing it
    cursor.execute('''UPDATE location_history AS lh SET vehicle_id = t.vehicle_id
        FROM trips AS t WHERE t.id = lh.trip_id AND lh.vehicle_id IS NULL''')

    # Defensive migrations in case an older schema already exists
    cursor.execute('ALTER TABLE dispatch_log ADD COLUMN IF NOT EXISTS barcode TEXT')
    cursor.execute('ALTER TABLE po_items ADD COLUMN IF NOT EXISTS company TEXT')
    cursor.execute('ALTER TABLE companies ADD COLUMN IF NOT EXISTS sub_brands TEXT')
    for col, ddl in [('plan', "TEXT DEFAULT 'Free'"), ('user_limit', 'INTEGER DEFAULT 5'), ('vehicle_limit', 'INTEGER DEFAULT 10'),
                      ('subscription_status', "TEXT DEFAULT 'Active'"), ('plan_start_date', 'TEXT'), ('plan_end_date', 'TEXT')]:
        cursor.execute(f'ALTER TABLE factories ADD COLUMN IF NOT EXISTS {col} {ddl}')
    cursor.execute("UPDATE factories SET plan = 'Free' WHERE plan IS NULL")
    cursor.execute("UPDATE factories SET user_limit = 5 WHERE user_limit IS NULL")
    cursor.execute("UPDATE factories SET vehicle_limit = 10 WHERE vehicle_limit IS NULL")
    cursor.execute("UPDATE factories SET subscription_status = 'Active' WHERE subscription_status IS NULL")
    # RIF is the original real business, not a trial signup — give it a generous limit so nothing is ever blocked by mistake
    cursor.execute("UPDATE factories SET plan = 'Enterprise', user_limit = 100, vehicle_limit = 500 WHERE id = %s AND plan = 'Free'", (default_factory_id,))
    # Convenience defaults for the three companies named in the spec (RIF's own setup only), only if not already set
    for cname, subs in [('Zepto', 'Daily Good,Unbranded'), ('Flipkart', 'Unbranded,My Kitchen'), ('Reliance', 'Good Life')]:
        cursor.execute('UPDATE companies SET sub_brands = %s WHERE factory_id = %s AND name = %s AND (sub_brands IS NULL OR sub_brands = %s)', (subs, default_factory_id, cname, ''))
    conn.commit()
    conn.close()
init_db()

def current_factory_id():
    return session.get('factory_id')

def log_audit(cursor, action, module, record_id='', details=''):
    """Records an entry in the audit trail. Call this right before conn.commit() in the route
    that performs the action, using the SAME cursor/connection so it's part of the same transaction."""
    cursor.execute('INSERT INTO audit_log (factory_id, user_id, user_name, action, module, record_id, details, timestamp) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                   (current_factory_id(), session.get('user_id'), session.get('user_name'), action, module, str(record_id), details, now_ist().isoformat()))

def check_usage_limit(cursor, kind):
    """Returns (ok, message). kind is 'user' or 'vehicle'. Super Admin-created/managed limits live on factories."""
    fid = current_factory_id()
    cursor.execute('SELECT user_limit, vehicle_limit, plan FROM factories WHERE id = %s', (fid,))
    row = cursor.fetchone()
    if not row:
        return True, ''
    user_limit, vehicle_limit, plan = row
    if kind == 'user':
        cursor.execute("SELECT COUNT(*) FROM users WHERE factory_id = %s AND status = 'Active'", (fid,))
        current = cursor.fetchone()[0]
        limit = user_limit
    else:
        cursor.execute('SELECT COUNT(*) FROM vehicle_master WHERE factory_id = %s', (fid,))
        current = cursor.fetchone()[0]
        limit = vehicle_limit
    if limit is not None and current >= limit:
        return False, f'Your "{plan}" plan allows up to {limit} {kind}s. You are at {current}/{limit}. Contact support to upgrade.'
    return True, ''

def is_super_admin():
    return session.get('role') == 'Super Admin'

@app.before_request
def enforce_viewer_readonly():
    """Backend-enforced permission: a Viewer-role user can look at every page but can never
    submit a form or call a mutating endpoint. This is enforced here (not just hidden in the UI)
    so it can't be bypassed by calling the URL directly."""
    if request.method == 'POST' and session.get('role') == 'Viewer' and request.endpoint not in ('login', 'logout', 'register'):
        return "Access denied — your account is view-only. Contact your Factory Admin for edit access.", 403

PLATFORM_NAME = 'AI Factory ERP'

@app.context_processor
def inject_factory_branding():
    """Makes factory_display_name / factory_initials / platform_name available in every template
    automatically, without every route needing to pass them explicitly. Falls back to neutral
    platform branding when no one is logged in yet (e.g. the login page itself)."""
    fid = session.get('factory_id')
    name = PLATFORM_NAME
    logo_url = None
    if fid:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT display_name, company_name, logo_url FROM factories WHERE id = %s', (fid,))
        row = cursor.fetchone()
        conn.close()
        if row:
            name = row[0] or row[1] or name
            logo_url = row[2]
    initials = ''.join(w[0] for w in name.split()[:2]).upper() if name else 'AI'
    return dict(factory_display_name=name, factory_initials=initials, factory_logo_url=logo_url, platform_name=PLATFORM_NAME)

def get_active_session():
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM app_state WHERE factory_id = %s AND key IN ('cur_po', 'cur_vehicle', 'cur_location')", (current_factory_id(),))
    rows = dict(cursor.fetchall())
    conn.close()
    return {
        'cur_po': rows.get('cur_po', ''),
        'cur_vehicle': rows.get('cur_vehicle', ''),
        'cur_location': rows.get('cur_location', ''),
    }

def set_active_session(po_number, vehicle_no, location):
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    for key, value in [('cur_po', po_number), ('cur_vehicle', vehicle_no), ('cur_location', location)]:
        cursor.execute('INSERT INTO app_state (factory_id, key, value) VALUES (%s, %s, %s) ON CONFLICT(factory_id, key) DO UPDATE SET value = EXCLUDED.value', (fid, key, value))
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
    base = f"""
    <div class="nav">
        <a href="/" class="{cls('dashboard')}">Dashboard</a>
        <a href="/companies" class="{cls('companies')}">Companies</a>
        <a href="/pos" class="{cls('pos')}">Manage POs</a>
        <a href="/production" class="{cls('production')}">Daily Production</a>
        <a href="/vehicles" class="{cls('vehicles')}">Vehicles</a>
        <a href="/history" class="{cls('history')}">History</a>"""
    # Role-gated nav links are written as literal Jinja syntax (evaluated per-request when the page
    # renders), since nav_html() itself only runs once at import time and can't see the logged-in user.
    users_link = "\n        {% if session.get('role') == 'Factory Admin' %}<a href=\"/users\" class=\"" + cls('users') + "\">Users</a><a href=\"/audit\" class=\"" + cls('audit') + "\">Audit Log</a>{% endif %}"
    account_link = "\n        <a href=\"/change_password\" class=\"" + cls('change_password') + "\">🔑 Change Password</a><a href=\"/logout\">Logout</a>"
    tail = """
    </div>
    """
    return base + users_link + account_link + tail

TOPBAR_TEMPLATE = """
<div class="topbar">
    <div class="brand">
        <div class="brand-logo">{{ factory_initials }}</div>
        <div>
            <h1>{{ factory_display_name }}</h1>
            <span>AI Dispatch &amp; Packing ERP</span>
        </div>
    </div>
    __NAV__
</div>
"""

DASHBOARD_HTML = STYLE_BLOCK + """
<title>Dashboard | {{ factory_display_name }}</title>
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
            <h1 class="hero-title">{{ factory_display_name }}</h1>
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

    <footer>{{ factory_display_name }} &middot; {{ platform_name }}</footer>
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
<title>Manage POs | {{ factory_display_name }}</title>
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
            Both .csv and .xlsx (Excel) files work. Required columns: <span style="font-family:monospace; color:var(--text);">po_number, item_name, ordered_qty</span> (weight and barcode are optional — add later if not in the file).<br>
            Common export formats also work automatically — e.g. Zepto's <span style="font-family:monospace; color:var(--text);">PurchaseOrderId, Sku, PO_Qty</span>.<br>
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

    <footer>{{ factory_display_name }} &middot; {{ platform_name }}</footer>
</div>
</body>
</html>
"""

COMPANIES_HTML = STYLE_BLOCK + """
<title>Companies | {{ factory_display_name }}</title>
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

    <footer>{{ factory_display_name }} &middot; {{ platform_name }}</footer>
</div>
</body>
</html>
"""

PRODUCTION_HTML = STYLE_BLOCK + """
<title>Daily Production | {{ factory_display_name }}</title>
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

    <footer>{{ factory_display_name }} &middot; {{ platform_name }}</footer>
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
<title>History | {{ factory_display_name }}</title>
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

    <footer>{{ factory_display_name }} &middot; {{ platform_name }}</footer>
</div>
</body>
</html>
"""

LOGIN_HTML = STYLE_BLOCK + """
<title>Login | {{ platform_name }}</title>
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
            <div class="brand-logo" style="margin:0 auto 16px;">AI</div>
            <h2 style="margin:0 0 4px; font-size:19px;">{{ platform_name }}</h2>
            <div style="color:var(--text-muted); font-size:12.5px; margin-bottom:20px;">AI Dispatch &amp; Packing ERP</div>
            <form method="POST">
                <input type="text" name="username" placeholder="Username" autofocus value="{{ username or '' }}">
                <input type="password" name="password" placeholder="Password" style="margin-top:8px;">
                <button type="submit" class="btn btn-block" style="margin-top:10px;">Login</button>
            </form>
            {% if error %}
            <div style="color:#fca5a5; font-size:12.5px; margin-top:12px;">Incorrect username or password, please try again.</div>
            {% endif %}
            {% if inactive %}
            <div style="color:#fca5a5; font-size:12.5px; margin-top:12px;">This account is inactive. Contact your admin.</div>
            {% endif %}
            {% if request.args.get('registered') %}
            <div style="color:#86efac; font-size:12.5px; margin-top:12px;">Account created! Please log in.</div>
            {% endif %}
            <div style="margin-top:18px; padding-top:14px; border-top:1px solid var(--border); font-size:12.5px; color:var(--text-muted);">
                New company? <a href="/register" style="color:var(--primary); font-weight:600; text-decoration:none;">Create an account</a>
            </div>
        </div>
    </div>
</body>
</html>
"""

@app.route('/')
def home():
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT DISTINCT po_number, company FROM po_items WHERE factory_id = %s', (fid,))
    po_list = cursor.fetchall()
    cursor.execute('SELECT id, po_number, vehicle_no, location, product_name, loaded_qty, timestamp FROM dispatch_log WHERE factory_id = %s ORDER BY id DESC LIMIT 200', (fid,))
    logs = cursor.fetchall()
    cursor.execute('SELECT COALESCE(SUM(loaded_qty), 0) FROM dispatch_log WHERE factory_id = %s', (fid,))
    total_loaded = cursor.fetchone()[0]

    # Item-level progress: ordered vs dispatched per (po_number, barcode)
    cursor.execute('SELECT po_number, barcode, item_name, weight, company, SUM(ordered_qty) FROM po_items WHERE factory_id = %s GROUP BY po_number, barcode, item_name, weight, company', (fid,))
    po_item_rows = cursor.fetchall()
    cursor.execute('SELECT po_number, barcode, SUM(loaded_qty) FROM dispatch_log WHERE factory_id = %s GROUP BY po_number, barcode', (fid,))
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
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT name, sub_brands FROM companies WHERE factory_id = %s ORDER BY name', (fid,))
    rows = cursor.fetchall()
    cursor.execute('SELECT company, COUNT(DISTINCT po_number), COUNT(*) FROM po_items WHERE factory_id = %s AND company IS NOT NULL GROUP BY company', (fid,))
    stats = {r[0]: (r[1], r[2]) for r in cursor.fetchall()}
    conn.close()
    companies = [{'name': n, 'sub_brands': sb, 'po_count': stats.get(n, (0, 0))[0], 'item_count': stats.get(n, (0, 0))[1]} for n, sb in rows]
    error_msg = request.args.get('error')
    return render_template_string(COMPANIES_HTML, companies=companies, error_msg=error_msg)

@app.route('/companies/add', methods=['POST'])
def companies_add():
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    name = request.form.get('name', '').strip()
    sub_brands = request.form.get('sub_brands', '').strip()
    if not name:
        return redirect('/companies')
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute('INSERT INTO companies (factory_id, name, sub_brands) VALUES (%s, %s, %s)', (fid, name, sub_brands))
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
    fid = current_factory_id()
    name = request.form.get('name', '').strip()
    sub_brands = request.form.get('sub_brands', '').strip()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('UPDATE companies SET sub_brands = %s WHERE factory_id = %s AND name = %s', (sub_brands, fid, name))
    conn.commit()
    conn.close()
    return redirect('/companies')

@app.route('/production')
def production_page():
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    tab = request.args.get('tab', 'log')
    msg = request.args.get('msg')
    msg_ok = request.args.get('ok') == '1'

    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT name, sub_brands FROM companies WHERE factory_id = %s ORDER BY name', (fid,))
    company_rows = cursor.fetchall()
    companies = [{'name': n, 'sub_brands': sb} for n, sb in company_rows]
    company_subbrands = {n: [s.strip() for s in (sb or '').split(',') if s.strip()] for n, sb in company_rows}

    cursor.execute('SELECT id, name, photo_data FROM quality_checkers WHERE factory_id = %s ORDER BY name', (fid,))
    qc_rows = cursor.fetchall()
    quality_checkers = [{'id': r[0], 'name': r[1], 'photo_data': r[2]} for r in qc_rows]
    qc_photos = {r[1]: r[2] for r in qc_rows}

    grouped_history = []
    by_company, by_item, grand_total, total_entries = [], [], 0, 0
    catalog_count = 0

    if tab == 'history':
        cursor.execute('''SELECT id, company, sub_brand, item_name, batch_number, quantity, qc_name,
                           packing_date, use_by_date, prod_date, prod_time, created_at
                           FROM daily_production WHERE factory_id = %s ORDER BY id DESC LIMIT 500''', (fid,))
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
        cursor.execute('SELECT company, SUM(quantity) FROM daily_production WHERE factory_id = %s GROUP BY company ORDER BY SUM(quantity) DESC', (fid,))
        company_totals = cursor.fetchall()
        grand_total = sum(t or 0 for _, t in company_totals)
        by_company = [{'company': c, 'total': t or 0, 'percent': round(((t or 0) / grand_total) * 100) if grand_total else 0} for c, t in company_totals]

        cursor.execute('SELECT item_name, SUM(quantity) FROM daily_production WHERE factory_id = %s GROUP BY item_name ORDER BY SUM(quantity) DESC LIMIT 20', (fid,))
        by_item = [{'item_name': i, 'total': t or 0} for i, t in cursor.fetchall()]

        cursor.execute('SELECT COUNT(*) FROM daily_production WHERE factory_id = %s', (fid,))
        total_entries = cursor.fetchone()[0]

    elif tab == 'admin':
        cursor.execute('SELECT COUNT(*) FROM barcode_catalog WHERE factory_id = %s', (fid,))
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
    fid = current_factory_id()
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
        (factory_id, company, sub_brand, item_name_entered, item_name, barcode, packing_date, use_by_date,
         batch_number, quantity, qc_name, qc_photo, prod_date, prod_time, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)''',
        (fid, company, sub_brand, item_name_entered, item_name, barcode, packing_date, use_by_date,
         batch_number, quantity, qc_name, qc_photo, now.strftime('%d %b %Y'), now.strftime('%I:%M %p'), now.isoformat()))
    conn.commit()
    conn.close()
    return redirect('/production?tab=history&ok=1&msg=' + quote('Production entry logged successfully.'))

@app.route('/production/edit/<int:entry_id>', methods=['POST'])
def production_edit(entry_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT created_at FROM daily_production WHERE id = %s AND factory_id = %s', (entry_id, fid))
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

    cursor.execute('UPDATE daily_production SET packing_date=%s, use_by_date=%s, batch_number=%s, quantity=%s WHERE id=%s AND factory_id=%s',
                   (packing_date, use_by_date, batch_number, quantity, entry_id, fid))
    conn.commit()
    conn.close()
    return redirect('/production?tab=history&ok=1&msg=' + quote('Entry updated.'))

@app.route('/production/delete/<int:entry_id>', methods=['POST'])
def production_delete(entry_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT created_at FROM daily_production WHERE id = %s AND factory_id = %s', (entry_id, fid))
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
    cursor.execute('DELETE FROM daily_production WHERE id = %s AND factory_id = %s', (entry_id, fid))
    conn.commit()
    conn.close()
    return redirect('/production?tab=history&ok=1&msg=' + quote('Entry deleted.'))

@app.route('/production/qc/add', methods=['POST'])
def production_qc_add():
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    name = request.form.get('name', '').strip()
    photo_data = request.form.get('photo_data', '').strip()
    if not name or not photo_data:
        return redirect('/production?tab=admin&ok=0&msg=' + quote('Name and a photo are both required.'))
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute('INSERT INTO quality_checkers (factory_id, name, photo_data, created_at) VALUES (%s, %s, %s, %s)',
                       (fid, name, photo_data, now_ist().isoformat()))
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
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM quality_checkers WHERE id = %s AND factory_id = %s', (qc_id, fid))
    conn.commit()
    conn.close()
    return redirect('/production?tab=admin')

@app.route('/production/barcode_catalog/add', methods=['POST'])
def barcode_catalog_add():
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    barcode = request.form.get('barcode', '').strip()
    item_name = request.form.get('item_name', '').strip()
    if not barcode or not item_name:
        return redirect('/production?tab=admin')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('INSERT INTO barcode_catalog (factory_id, barcode, item_name) VALUES (%s, %s, %s) ON CONFLICT (factory_id, barcode) DO UPDATE SET item_name = EXCLUDED.item_name', (fid, barcode, item_name))
    conn.commit()
    conn.close()
    return redirect('/production?tab=admin&ok=1&msg=' + quote('Barcode added to catalog.'))

@app.route('/production/barcode_catalog/import', methods=['POST'])
def barcode_catalog_import():
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
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
        cursor.execute('INSERT INTO barcode_catalog (factory_id, barcode, item_name) VALUES (%s, %s, %s) ON CONFLICT (factory_id, barcode) DO UPDATE SET item_name = EXCLUDED.item_name', (fid, barcode, item_name))
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
    fid = current_factory_id()
    barcode = request.args.get('barcode', '').strip()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT item_name FROM barcode_catalog WHERE factory_id = %s AND barcode = %s', (fid, barcode))
    row = cursor.fetchone()
    if not row:
        cursor.execute('SELECT item_name FROM po_items WHERE factory_id = %s AND barcode = %s LIMIT 1', (fid, barcode))
        row = cursor.fetchone()
    conn.close()
    if row:
        return {'found': True, 'item_name': row[0]}
    return {'found': False}

@app.route('/history')
def history_page():
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    search_po = request.args.get('po', '').strip()
    conn = get_conn()
    cursor = conn.cursor()
    if search_po:
        cursor.execute('SELECT po_number, vehicle_no, location, product_name, loaded_qty, timestamp FROM dispatch_log WHERE factory_id = %s AND po_number ILIKE %s ORDER BY id DESC', (fid, f'%{search_po}%'))
    else:
        cursor.execute('SELECT po_number, vehicle_no, location, product_name, loaded_qty, timestamp FROM dispatch_log WHERE factory_id = %s ORDER BY id DESC LIMIT 200', (fid,))
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
    fid = current_factory_id()
    filter_company = request.args.get('company', '').strip()
    conn = get_conn()
    cursor = conn.cursor()
    if filter_company:
        cursor.execute('SELECT id, po_number, item_name, weight, ordered_qty, barcode, company FROM po_items WHERE factory_id = %s AND company = %s ORDER BY po_number, id DESC', (fid, filter_company))
    else:
        cursor.execute('SELECT id, po_number, item_name, weight, ordered_qty, barcode, company FROM po_items WHERE factory_id = %s ORDER BY po_number, id DESC', (fid,))
    rows = cursor.fetchall()
    cursor.execute('SELECT name FROM companies WHERE factory_id = %s ORDER BY name', (fid,))
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
    fid = current_factory_id()
    po_number = request.form.get('po_number', '').strip()
    company = request.form.get('company', '').strip()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM po_items WHERE factory_id = %s AND po_number = %s AND company = %s', (fid, po_number, company))
    conn.commit()
    conn.close()
    return redirect('/pos?company=' + quote(company) if company else '/pos')

@app.route('/pos/add', methods=['POST'])
def pos_add():
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    company = request.form.get('company', '').strip()
    po_number = request.form.get('po_number', '').strip()
    item_name = request.form.get('item_name', '').strip()
    weight = request.form.get('weight', '').strip()
    ordered_qty = request.form.get('ordered_qty', '').strip() or 0
    barcode = request.form.get('barcode', '').strip()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('INSERT INTO po_items (factory_id, po_number, item_name, weight, ordered_qty, barcode, company) VALUES (%s, %s, %s, %s, %s, %s, %s)',
                   (fid, po_number, item_name, weight, int(ordered_qty), barcode, company))
    log_audit(cursor, 'Product Updated', 'Manage POs', po_number, f'Added {item_name} ({ordered_qty}) to PO {po_number}')
    conn.commit()
    conn.close()
    return redirect('/pos?company=' + quote(company) if company else '/pos')

# Accepts flexible header names (English or common Hindi-transliterated variants),
# plus real-world PO export formats (e.g. PoNumber, SkuDesc, EAN, Quantity)
CSV_HEADER_MAP = {
    'po_number': ['po_number', 'po number', 'po no', 'ponumber', 'po', 'po_no', 'purchaseorderid', 'purchase order id', 'purchase_order_id', 'poid', 'po id'],
    'item_name': ['item_name', 'item name', 'item', 'product', 'product_name', 'skudesc', 'sku desc', 'sku_desc', 'sku'],
    'weight': ['weight', 'wt', 'size'],
    'ordered_qty': ['ordered_qty', 'ordered quantity', 'quantity', 'qty', 'order_qty', 'po_qty', 'po qty', 'poqty'],
    'barcode': ['barcode', 'bar code', 'code', 'ean', 'ean code'],
}

WEIGHT_PATTERN = re.compile(r'\(?(\d+(?:\.\d+)?)\s*(kg|kgs|gm|gms|g|ml|ltr|l)\)?(?:\s|$)', re.IGNORECASE)

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
    fid = current_factory_id()
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
    required = ['po_number', 'item_name', 'ordered_qty']  # barcode is optional — can be added later
    missing = [k for k in required if k not in colmap]
    if missing:
        return redirect('/pos?ok=0&msg=' + quote(f'These columns were not found in the file: {", ".join(missing)}'))

    conn = get_conn()
    cursor = conn.cursor()
    inserted, skipped = 0, 0
    for row in rows:
        po_number = (row.get(colmap['po_number']) or '').strip()
        item_name = (row.get(colmap['item_name']) or '').strip()
        barcode = (row.get(colmap['barcode']) or '').strip() if 'barcode' in colmap else ''
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
        cursor.execute('INSERT INTO po_items (factory_id, po_number, item_name, weight, ordered_qty, barcode, company) VALUES (%s, %s, %s, %s, %s, %s, %s)',
                       (fid, po_number, item_name, weight, ordered_qty, barcode, company))
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
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM po_items WHERE id = %s AND factory_id = %s', (item_id, fid))
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
    fid = current_factory_id()
    barcode = request.args.get('barcode', '').strip()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT item_name, weight FROM po_items WHERE factory_id = %s AND barcode = %s LIMIT 1', (fid, barcode))
    item = cursor.fetchone()
    conn.close()
    if item:
        return {'found': True, 'item_name': item[0], 'weight': item[1]}
    return {'found': False}

@app.route('/process_scan', methods=['POST'])
def process_scan():
    if not session.get('logged_in'): return {'ok': False, 'error': 'not logged in'}, 401
    fid = current_factory_id()
    barcode = request.form['barcode'].strip()
    m_units = request.form['qty']
    active = get_active_session()
    po_number = active['cur_po'] or 'Unknown'
    vehicle_no = active['cur_vehicle'] or 'Unknown'
    location = active['cur_location'] or 'Unknown'
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT item_name, weight FROM po_items WHERE factory_id = %s AND barcode = %s', (fid, barcode))
    item = cursor.fetchone()
    if item:
        total_qty = calculate_qty(item[1], m_units)
        cursor.execute('INSERT INTO dispatch_log (factory_id, po_number, vehicle_no, location, product_name, loaded_qty, timestamp, barcode) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                       (fid, po_number, vehicle_no, location, f"{item[0]} ({item[1]})", total_qty, now_ist().strftime("%d %b %Y, %I:%M %p"), barcode))
        conn.commit()
        conn.close()
        return {'ok': True}, 200
    conn.close()
    return {'ok': False, 'error': 'barcode not found'}, 404

@app.route('/dispatch/edit/<int:log_id>', methods=['POST'])
def dispatch_edit(log_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    new_qty = request.form.get('loaded_qty', '').strip()
    try:
        new_qty = int(float(new_qty))
    except ValueError:
        return redirect('/')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('UPDATE dispatch_log SET loaded_qty = %s WHERE id = %s AND factory_id = %s', (new_qty, log_id, fid))
    conn.commit()
    conn.close()
    return redirect('/')

@app.route('/dispatch/delete/<int:log_id>', methods=['POST'])
def dispatch_delete(log_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM dispatch_log WHERE id = %s AND factory_id = %s', (log_id, fid))
    conn.commit()
    conn.close()
    return redirect('/')

@app.route('/export_csv')
def export_csv():
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT display_name, company_name FROM factories WHERE id = %s', (fid,))
    frow = cursor.fetchone()
    factory_name = (frow[0] or frow[1]) if frow else 'Export'
    cursor.execute('SELECT po_number, vehicle_no, location, product_name, loaded_qty, timestamp FROM dispatch_log WHERE factory_id = %s ORDER BY id DESC', (fid,))
    rows = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([factory_name])
    writer.writerow(['PO Number', 'Vehicle No', 'Location', 'Product', 'Qty Loaded', 'Time'])
    writer.writerows(rows)

    safe_name = re.sub(r'[^A-Za-z0-9_-]+', '_', factory_name).strip('_') or 'export'
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={safe_name}_dispatch_log.csv'}
    )

@app.route('/export_progress_csv')
def export_progress_csv():
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT display_name, company_name FROM factories WHERE id = %s', (fid,))
    frow = cursor.fetchone()
    factory_name = (frow[0] or frow[1]) if frow else 'Export'
    cursor.execute('SELECT po_number, barcode, item_name, weight, company, SUM(ordered_qty) FROM po_items WHERE factory_id = %s GROUP BY po_number, barcode, item_name, weight, company', (fid,))
    po_item_rows = cursor.fetchall()
    cursor.execute('SELECT po_number, barcode, SUM(loaded_qty) FROM dispatch_log WHERE factory_id = %s GROUP BY po_number, barcode', (fid,))
    dispatched_map = {(r[0], r[1]): (r[2] or 0) for r in cursor.fetchall()}
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([factory_name])
    writer.writerow(['Company', 'PO Number', 'Item', 'Weight', 'Barcode', 'Ordered Qty', 'Loaded Qty', 'Pending Qty', '% Complete'])
    for po_number, barcode, item_name, weight, company, ordered in po_item_rows:
        ordered = ordered or 0
        dispatched = dispatched_map.get((po_number, barcode), 0)
        pending = max(ordered - dispatched, 0)
        percent = min(100, round((dispatched / ordered) * 100)) if ordered else 0
        writer.writerow([company or '', po_number, item_name, weight, barcode, ordered, dispatched, pending, f'{percent}%'])

    safe_name = re.sub(r'[^A-Za-z0-9_-]+', '_', factory_name).strip('_') or 'export'
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={safe_name}_po_progress.csv'}
    )

def calculate_qty(weight, master_units):
    # Quantity entered on scan is the direct bag/unit count — no multiplier applied.
    return int(master_units)



# ===========================================================================
# VEHICLE MASTER + TRIP/LOADING SESSIONS + LIVE GPS TRACKING
# ===========================================================================

def gen_tracking_token():
    return secrets.token_urlsafe(24)

def po_progress(cursor, po_number, vehicle_number):
    fid = current_factory_id()
    cursor.execute('SELECT COALESCE(SUM(ordered_qty),0) FROM po_items WHERE factory_id = %s AND po_number = %s', (fid, po_number))
    ordered = cursor.fetchone()[0] or 0
    cursor.execute('SELECT COALESCE(SUM(loaded_qty),0) FROM dispatch_log WHERE factory_id = %s AND po_number = %s AND vehicle_no = %s', (fid, po_number, vehicle_number))
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

TRIP_STATUS_BADGE = {'Loading':'badge-blue', 'Ready to Dispatch':'badge-green', 'In Transit':'badge-blue',
                      'Delivered':'badge-green', 'Cancelled':'badge-amber', 'Available':'badge-green'}

def recompute_trip_status(cursor, trip_id):
    fid = current_factory_id()
    cursor.execute('SELECT trip_status, vehicle_number FROM trips WHERE id = %s AND factory_id = %s', (trip_id, fid))
    row = cursor.fetchone()
    if not row: return
    current, vehicle_number = row
    if current in ('In Transit', 'Delivered', 'Cancelled'):
        return  # dispatched/delivered/cancelled trips are not auto-changed
    cursor.execute('SELECT po_number, is_hold, is_cancelled FROM vehicle_po_map WHERE trip_id = %s', (trip_id,))
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
        cursor.execute('UPDATE trips SET trip_status = %s WHERE id = %s', (new_status, trip_id))

def get_or_create_vehicle(cursor, vehicle_number):
    fid = current_factory_id()
    cursor.execute('SELECT id FROM vehicle_master WHERE factory_id = %s AND vehicle_number = %s', (fid, vehicle_number))
    row = cursor.fetchone()
    if row:
        return row[0]
    ts = now_ist().isoformat()
    cursor.execute('INSERT INTO vehicle_master (factory_id, vehicle_number, created_at, updated_at) VALUES (%s,%s,%s,%s) RETURNING id',
                   (fid, vehicle_number, ts, ts))
    return cursor.fetchone()[0]

def vehicle_master_status(cursor, vehicle_id):
    """Returns (status_label, active_trip_id_or_None) for a vehicle master row, derived from its latest trip."""
    fid = current_factory_id()
    cursor.execute('SELECT id, trip_status FROM trips WHERE vehicle_id = %s AND factory_id = %s ORDER BY id DESC LIMIT 1', (vehicle_id, fid))
    row = cursor.fetchone()
    if not row:
        return ('Available', None)
    trip_id, trip_status = row
    if trip_status in ('Delivered', 'Cancelled'):
        return ('Available', None)
    return (trip_status, trip_id)

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

VEHICLES_LIST_HTML = STYLE_BLOCK + """
<title>Vehicles | {{ factory_display_name }}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css">
</head>
<body>
<div class="container">
""" + TOPBAR_TEMPLATE.replace("__NAV__", nav_html('vehicles')) + """
    <div class="card-header" style="margin-bottom:14px;">
        <h2>🚚 Vehicles</h2>
        <div style="display:flex; gap:8px;">
            <button class="btn btn-outline btn-sm" onclick="document.getElementById('addVehicleModal').style.display='flex'">+ Add Vehicle</button>
            <a href="/vehicles/start_loading" class="btn btn-primary btn-sm">+ Start New Vehicle Loading</a>
        </div>
    </div>

    <div id="addVehicleModal" class="modal" style="{% if add_vehicle_error %}display:flex;{% endif %}">
        <div class="modal-content">
            <h3>Add New Vehicle</h3>
            {% if add_vehicle_error %}<div style="color:#fca5a5; font-size:12.5px; margin-bottom:8px;">{{ add_vehicle_error }}</div>{% endif %}
            <form method="POST" action="/vehicles/add">
                <input type="text" name="vehicle_number" placeholder="Vehicle Number (e.g. RJ14GA1234)" required autofocus>
                <button type="submit" class="btn btn-block" style="margin-top:8px;">Add Vehicle</button>
            </form>
            <button class="btn btn-outline btn-block" style="margin-top:8px;" onclick="document.getElementById('addVehicleModal').style.display='none'">Close</button>
        </div>
    </div>


    <div class="stats-grid">
        <div class="stat-card"><div class="icon">🚛</div><div class="label">Total Vehicles</div><div class="value">{{ summary.total }}</div></div>
        <div class="stat-card"><div class="icon">📦</div><div class="label">Loading</div><div class="value">{{ summary.loading }}</div></div>
        <div class="stat-card"><div class="icon">🛣️</div><div class="label">In Transit</div><div class="value">{{ summary.transit }}</div></div>
        <div class="stat-card"><div class="icon">🟢</div><div class="label">Available</div><div class="value">{{ summary.available }}</div></div>
        <div class="stat-card"><div class="icon">📴</div><div class="label">Location Offline</div><div class="value">{{ summary.offline }}</div></div>
    </div>

    <div class="card">
        <div class="card-header"><h2>🗺️ Live Vehicle Map</h2></div>
        <div id="liveMap" style="height:340px; border-radius:12px; overflow:hidden;"></div>
        <div style="font-size:12px; color:var(--text-dim); margin-top:8px;">🟢 live &amp; fresh &nbsp; 🟡 live but a few min old &nbsp; ⚪ last known location only</div>
    </div>

    <div class="card">
        <div class="card-header"><h2>🔍 All Vehicles</h2></div>
        <form method="GET" action="/vehicles" style="margin-bottom:14px;">
            <input type="text" name="q" value="{{ q }}" placeholder="Search Vehicle Number, Driver Name or Mobile...">
        </form>
        <table class="table">
            <tr><th>Vehicle No.</th><th>Driver</th><th>Mobile</th><th>Status</th><th>Current / Last Location</th><th>Last Updated</th><th></th></tr>
            {% for veh in vehicles %}
            <tr>
                <td>{{ veh.vehicle_number }}</td>
                <td>{{ veh.driver_name or '—' }}</td>
                <td>{{ veh.driver_mobile or '—' }}</td>
                <td><span class="badge {{ veh.status_class }}">{{ veh.status }}</span></td>
                <td>{% if veh.has_location %}{{ veh.lat_display }}, {{ veh.lng_display }}{% else %}No location yet{% endif %}</td>
                <td><span style="color:{{ veh.freshness_color }};">●</span> {{ veh.freshness_label }}</td>
                <td><a href="/vehicles/{{ veh.id }}" class="btn btn-outline btn-sm">Open</a></td>
            </tr>
            {% endfor %}
            {% if not vehicles %}<tr><td colspan="7" style="text-align:center; color:var(--text-dim); padding:20px;">No vehicles found.</td></tr>{% endif %}
        </table>
    </div>
</div>
<script>
const map = L.map('liveMap').setView([20.5937, 78.9629], 5);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '&copy; OpenStreetMap' }).addTo(map);
const markers = {{ map_markers|tojson }};
const bounds = [];
markers.forEach(m => {
    const color = m.color === 'green' ? '#4ade80' : (m.color === 'yellow' ? '#fbbf24' : '#94a3b8');
    const icon = L.divIcon({html: `<div style="background:${color}; width:16px; height:16px; border-radius:50%; border:2px solid white; box-shadow:0 0 6px rgba(0,0,0,0.5);"></div>`, className: ''});
    const mk = L.marker([m.lat, m.lng], {icon}).addTo(map);
    mk.bindPopup(`<b>${m.vehicle_number}</b><br>Driver: ${m.driver_name || '—'} (${m.driver_mobile || '—'})<br>Status: ${m.status}<br>Active PO: ${m.po_count}<br>${m.mode} — ${m.freshness}<br><a href="/vehicles/${m.id}">Open →</a>`);
    bounds.push([m.lat, m.lng]);
});
if (bounds.length) map.fitBounds(bounds, {padding:[30,30]});
</script>
</body></html>
"""

START_LOADING_HTML = STYLE_BLOCK + """
<title>Start New Vehicle Loading | {{ factory_display_name }}</title>
</head>
<body>
<div class="container">
""" + TOPBAR_TEMPLATE.replace("__NAV__", nav_html('vehicles')) + """
    <div class="card">
        <div class="card-header"><h2>🚀 Start New Vehicle Loading</h2><a href="/vehicles" class="btn btn-outline btn-sm">← Back</a></div>
        <form method="POST" action="/vehicles/start_loading" id="startLoadingForm">
            <div>
                <label>Vehicle</label>
                <select id="vehicleSelect" name="vehicle_choice" onchange="onVehicleChange()" required>
                    <option value="">Select vehicle...</option>
                    {% for veh in existing_vehicles %}
                    <option value="{{ veh.vehicle_number }}" data-driver="{{ veh.driver_name or '' }}" data-mobile="{{ veh.driver_mobile or '' }}" data-status="{{ veh.status }}">
                        {{ veh.vehicle_number }} ({{ veh.status }})
                    </option>
                    {% endfor %}
                    <option value="__new__">+ Add New Vehicle</option>
                </select>
            </div>
            <div id="newVehicleRow" style="display:none;">
                <label>New Vehicle Number *</label>
                <input type="text" id="newVehicleNumber" name="new_vehicle_number" placeholder="e.g. RJ14GA1234">
            </div>
            <div class="form-grid" style="margin-top:6px;">
                <div><label>Driver Name *</label><input type="text" name="driver_name" id="driverName" required></div>
                <div><label>Driver Mobile Number *</label><input type="tel" name="driver_mobile" id="driverMobile" required pattern="[0-9]{10}" maxlength="10" placeholder="10 digit mobile"></div>
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
function onVehicleChange() {
    const sel = document.getElementById('vehicleSelect');
    const opt = sel.options[sel.selectedIndex];
    if (sel.value === '__new__') {
        document.getElementById('newVehicleRow').style.display = 'block';
        document.getElementById('newVehicleNumber').required = true;
        document.getElementById('driverName').value = '';
        document.getElementById('driverMobile').value = '';
    } else {
        document.getElementById('newVehicleRow').style.display = 'none';
        document.getElementById('newVehicleNumber').required = false;
        document.getElementById('driverName').value = opt.dataset.driver || '';
        document.getElementById('driverMobile').value = opt.dataset.mobile || '';
    }
}
{% if preselect_vehicle %}
window.addEventListener('DOMContentLoaded', () => {
    document.getElementById('vehicleSelect').value = "{{ preselect_vehicle }}";
    onVehicleChange();
});
{% endif %}
document.getElementById('startLoadingForm').addEventListener('submit', function(e){
    const selects = Array.from(document.querySelectorAll('select[name="po_numbers"]')).map(s => s.value).filter(Boolean);
    const unique = new Set(selects);
    if (selects.length === 0) { alert('Kam se kam 1 PO add karo.'); e.preventDefault(); return; }
    if (unique.size !== selects.length) { alert('Same PO do baar add nahi ho sakta.'); e.preventDefault(); return; }
    if (document.getElementById('vehicleSelect').value === '') { alert('Vehicle select karo.'); e.preventDefault(); return; }
});
</script>
</body></html>
"""

VEHICLE_MASTER_DETAIL_HTML = STYLE_BLOCK + """
<title>{{ veh.vehicle_number }} | Vehicle Details</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css">
</head>
<body>
<div class="container">
""" + TOPBAR_TEMPLATE.replace("__NAV__", nav_html('vehicles')) + """
    <div class="card">
        <div class="card-header">
            <h2>🚛 {{ veh.vehicle_number }} <span class="badge {{ veh.status_class }}">{{ veh.status }}</span></h2>
            <div style="display:flex; gap:8px;">
                <a href="/vehicles/start_loading?vehicle={{ veh.vehicle_number }}" class="btn btn-primary btn-sm">+ Start New Loading</a>
                <a href="/vehicles" class="btn btn-outline btn-sm">← Back</a>
            </div>
        </div>
        <div class="form-grid">
            <div><strong>Driver (current/last trip):</strong> {{ veh.driver_name or '—' }}</div>
            <div><strong>Mobile:</strong> {{ veh.driver_mobile or '—' }}</div>
            <div><strong>GPS Status:</strong> {{ veh.mode }}</div>
        </div>
    </div>

    <div class="card">
        <div class="card-header"><h2>📍 {{ veh.mode }} Location</h2></div>
        {% if veh.has_location %}
        <div id="curMap" style="height:280px; border-radius:12px;"></div>
        <div style="margin-top:10px; font-size:13px; color:var(--text-dim);">
            <span style="color:{{ veh.freshness_color }};">●</span> Last Updated: {{ veh.freshness_label }}
            &nbsp;|&nbsp; Lat/Lng: {{ veh.lat_display }}, {{ veh.lng_display }}
            &nbsp;|&nbsp; <a href="https://www.google.com/maps?q={{ veh.lat_display }},{{ veh.lng_display }}" target="_blank" style="color:var(--primary);">Open in Google Maps →</a>
        </div>
        {% else %}
        <p style="color:var(--text-dim);">Is vehicle ki abhi tak koi location record nahi hui.</p>
        {% endif %}
    </div>

    <div class="card">
        <div class="card-header"><h2>🧾 Trip History</h2></div>
        <table class="table">
            <tr><th>Trip</th><th>Date</th><th>PO Count</th><th>Status</th><th></th></tr>
            {% for t in trips %}
            <tr>
                <td>#{{ t.id }}</td><td>{{ t.loading_started_at }}</td><td>{{ t.po_count }}</td>
                <td><span class="badge {{ t.status_class }}">{{ t.trip_status }}</span></td>
                <td><a href="/trips/{{ t.id }}" class="btn btn-outline btn-sm">Open</a></td>
            </tr>
            {% endfor %}
            {% if not trips %}<tr><td colspan="5" style="text-align:center; color:var(--text-dim); padding:20px;">Koi trip nahi hui abhi tak.</td></tr>{% endif %}
        </table>
    </div>

    <div class="card">
        <div class="card-header"><h2>🛣️ Route History</h2></div>
        <form method="GET" action="/vehicles/{{ veh.id }}" style="display:flex; gap:8px; margin-bottom:14px;">
            <select name="route_trip" onchange="this.form.submit()" style="flex:1;">
                <option value="">Select a trip to view its route...</option>
                {% for t in trips %}<option value="{{ t.id }}" {% if t.id == route_trip_id %}selected{% endif %}>Trip #{{ t.id }} — {{ t.loading_started_at }}</option>{% endfor %}
            </select>
        </form>
        {% if route_points %}
        <div id="routeMap" style="height:300px; border-radius:12px;"></div>
        {% elif route_trip_id %}
        <p style="color:var(--text-dim);">Is trip ke liye koi location record nahi mila.</p>
        {% endif %}
    </div>

    <div class="card">
        <div class="card-header"><h2>📜 Location History</h2></div>
        <table class="table">
            <tr><th>Date</th><th>Time</th><th>Lat</th><th>Lng</th></tr>
            {% for h in location_history %}
            <tr><td>{{ h.date }}</td><td>{{ h.time }}</td><td>{{ h.lat }}</td><td>{{ h.lng }}</td></tr>
            {% endfor %}
            {% if not location_history %}<tr><td colspan="4" style="text-align:center; color:var(--text-dim); padding:20px;">Koi location history nahi hai.</td></tr>{% endif %}
        </table>
    </div>
</div>
<script>
{% if veh.has_location %}
const map = L.map('curMap').setView([{{ veh.lat_display }}, {{ veh.lng_display }}], 13);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '&copy; OpenStreetMap' }).addTo(map);
L.marker([{{ veh.lat_display }}, {{ veh.lng_display }}]).addTo(map).bindPopup('{{ veh.mode }} — {{ veh.freshness_label }}').openPopup();
{% endif %}
{% if route_points %}
const rmap = L.map('routeMap').setView({{ route_points[0]|tojson }}, 12);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '&copy; OpenStreetMap' }).addTo(rmap);
const route = {{ route_points|tojson }};
L.polyline(route, {color:'#3b82f6', weight:4}).addTo(rmap);
L.marker(route[0]).addTo(rmap).bindPopup('Trip start');
L.marker(route[route.length-1]).addTo(rmap).bindPopup('Latest point');
rmap.fitBounds(route);
{% endif %}
</script>
</body></html>
"""

TRIP_DETAIL_HTML = STYLE_BLOCK + """
<title>{{ t.vehicle_number }} — Trip #{{ t.id }}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css">
</head>
<body>
<div class="container">
""" + TOPBAR_TEMPLATE.replace("__NAV__", nav_html('vehicles')) + """
    <div class="card">
        <div class="card-header">
            <h2>🚛 {{ t.vehicle_number }} — Trip #{{ t.id }} <span class="badge {{ t.status_class }}">{{ t.trip_status }}</span></h2>
            <div style="display:flex; gap:8px;">
                <a href="/vehicles/{{ t.vehicle_id }}" class="btn btn-outline btn-sm">🚛 Vehicle Page</a>
                <a href="/vehicles" class="btn btn-outline btn-sm">← All Vehicles</a>
            </div>
        </div>
        <div class="form-grid">
            <div><strong>Driver:</strong> {{ t.driver_name }}</div>
            <div><strong>Mobile:</strong> {{ t.driver_mobile }}</div>
            <div><strong>Start Location:</strong> {{ t.start_location }}</div>
            <div><strong>Loading Started:</strong> {{ t.loading_started_at }}</div>
            <div><strong>Tracking:</strong> {{ t.tracking_status }} — <span style="color:{{ t.freshness_color }};">●</span> {{ t.freshness_label }}</div>
        </div>
        <div style="margin-top:14px; display:flex; gap:8px; flex-wrap:wrap;">
            {% if t.trip_status not in ['In Transit','Delivered','Cancelled'] %}
                <form method="POST" action="/trips/{{ t.id }}/regen_token" style="display:inline;"><button class="btn btn-outline btn-sm">🔗 Generate/Reset Tracking Link</button></form>
                {% if t.tracking_token %}
                <button class="btn btn-outline btn-sm" onclick="navigator.clipboard.writeText('{{ track_url }}'); alert('Link copied!');">📋 Copy Tracking Link</button>
                <a class="btn btn-outline btn-sm" target="_blank" href="https://wa.me/91{{ t.driver_mobile }}?text={{ track_url_encoded }}">📲 Share on WhatsApp</a>
                {% endif %}
                {% if t.tracking_status == 'active' %}
                <form method="POST" action="/trips/{{ t.id }}/stop_tracking" style="display:inline;"><button class="btn btn-danger btn-sm">⛔ Stop Tracking</button></form>
                {% endif %}
            {% endif %}
            {% if t.trip_status == 'Ready to Dispatch' %}
                <form method="POST" action="/trips/{{ t.id }}/dispatch" style="display:inline;"><button class="btn btn-primary btn-sm">🚀 Dispatch Vehicle</button></form>
            {% endif %}
            {% if t.trip_status == 'In Transit' %}
                <form method="POST" action="/trips/{{ t.id }}/deliver" style="display:inline;"><button class="btn btn-primary btn-sm">✅ Mark Delivered</button></form>
            {% endif %}
        </div>
    </div>

    {% if t.current_latitude %}
    <div class="card">
        <div class="card-header"><h2>📍 Live Location &amp; Route</h2></div>
        <div id="routeMap" style="height:300px; border-radius:12px;"></div>
    </div>
    {% endif %}

    <div class="card">
        <div class="card-header">
            <h2>📦 Attached POs — {{ t.completed_count }}/{{ t.po_count }} completed ({{ t.percent }}% overall)</h2>
        </div>
        <div class="progress-track" style="margin-bottom:14px;"><div class="progress-fill" style="width:{{ t.percent }}%;"></div></div>
        <table class="table">
            <tr><th>PO Number</th><th>Ordered</th><th>Loaded</th><th>Pending</th><th>Status</th><th></th></tr>
            {% for p in pos %}
            <tr>
                <td>{{ p.po_number }} <span style="color:var(--text-dim); font-size:11px;">({{ p.company }})</span></td>
                <td>{{ p.ordered }}</td><td>{{ p.loaded }}</td><td>{{ p.pending }}</td>
                <td><span class="badge {{ p.status_class }}">{{ p.status }}</span></td>
                <td style="white-space:nowrap;">
                    {% if t.trip_status not in ['In Transit','Delivered','Cancelled'] %}
                    <form method="POST" action="/trips/{{ t.id }}/set_active_po" style="display:inline;">
                        <input type="hidden" name="po_number" value="{{ p.po_number }}">
                        <button class="btn btn-outline btn-sm">Scan This</button>
                    </form>
                    <form method="POST" action="/trips/{{ t.id }}/po_status" style="display:inline;">
                        <input type="hidden" name="po_number" value="{{ p.po_number }}">
                        <input type="hidden" name="action" value="{{ 'resume' if p.is_hold else 'hold' }}">
                        <button class="btn btn-outline btn-sm">{{ 'Resume' if p.is_hold else 'Hold' }}</button>
                    </form>
                    <form method="POST" action="/trips/{{ t.id }}/remove_po" style="display:inline;" onsubmit="return confirm('Is PO ko trip se remove karein?');">
                        <input type="hidden" name="po_number" value="{{ p.po_number }}">
                        <button class="btn btn-danger btn-sm">Remove</button>
                    </form>
                    {% endif %}
                </td>
            </tr>
            {% endfor %}
        </table>
        {% if t.trip_status not in ['In Transit','Delivered','Cancelled'] %}
        <form method="POST" action="/trips/{{ t.id }}/add_po" style="margin-top:14px; display:flex; gap:8px;">
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
{% if t.current_latitude %}
const map = L.map('routeMap').setView([{{ t.current_latitude }}, {{ t.current_longitude }}], 13);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '&copy; OpenStreetMap' }).addTo(map);
const route = {{ route_points|tojson }};
if (route.length > 1) L.polyline(route, {color:'#3b82f6', weight:4}).addTo(map);
L.marker([{{ t.current_latitude }}, {{ t.current_longitude }}]).addTo(map).bindPopup('Current location — {{ t.freshness_label }}').openPopup();
{% endif %}
</script>
</body></html>
"""

TRACK_HTML = STYLE_BLOCK + """
<title>Live Tracking | {{ t.vehicle_number }}</title>
</head>
<body>
<div class="container" style="max-width:480px;">
    <div class="card" style="text-align:center; margin-top:40px;">
        <div style="font-size:40px;">🚚</div>
        <h2 style="margin:10px 0 4px;">{{ t.vehicle_number }}</h2>
        <div style="color:var(--text-dim);">Driver: {{ t.driver_name }}</div>
        <div class="badge {{ t.status_class }}" style="margin-top:10px; display:inline-block;">{{ t.trip_status }}</div>

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

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/vehicles')
def vehicles_page():
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    q = request.args.get('q', '').strip().lower()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('''SELECT id, vehicle_number, current_latitude, current_longitude, last_location_at
                       FROM vehicle_master WHERE factory_id = %s ORDER BY vehicle_number''', (fid,))
    rows = cursor.fetchall()
    vehicles, map_markers = [], []
    summary = {'total': 0, 'loading': 0, 'transit': 0, 'available': 0, 'offline': 0}
    for vid, vnum, lat, lng, last_loc in rows:
        status, active_trip_id = vehicle_master_status(cursor, vid)
        driver_name, driver_mobile, tracking_status, po_count = None, None, 'stopped', 0
        if active_trip_id:
            cursor.execute('SELECT driver_name, driver_mobile, tracking_status FROM trips WHERE id = %s AND factory_id = %s', (active_trip_id, fid))
            driver_name, driver_mobile, tracking_status = cursor.fetchone()
            cursor.execute('SELECT COUNT(*) FROM vehicle_po_map WHERE trip_id = %s AND is_cancelled = FALSE', (active_trip_id,))
            po_count = cursor.fetchone()[0]
        else:
            cursor.execute('SELECT driver_name, driver_mobile FROM trips WHERE vehicle_id = %s AND factory_id = %s ORDER BY id DESC LIMIT 1', (vid, fid))
            last_trip = cursor.fetchone()
            if last_trip: driver_name, driver_mobile = last_trip

        if q and q not in vnum.lower() and q not in (driver_name or '').lower() and q not in (driver_mobile or ''):
            continue

        label, color, secs = freshness_info(last_loc)
        is_live = tracking_status == 'active' and status in ('Loading', 'Ready to Dispatch', 'In Transit')
        mode = 'LIVE' if (is_live and secs <= 600) else ('LAST KNOWN' if lat is not None else 'NO DATA')
        gps_offline = lat is None or secs > 600 or not is_live

        summary['total'] += 1
        if status == 'Loading': summary['loading'] += 1
        elif status == 'In Transit': summary['transit'] += 1
        elif status == 'Available': summary['available'] += 1
        if gps_offline: summary['offline'] += 1

        d = {'id': vid, 'vehicle_number': vnum, 'driver_name': driver_name, 'driver_mobile': driver_mobile,
             'status': status, 'status_class': TRIP_STATUS_BADGE.get(status, 'badge-amber'),
             'has_location': lat is not None, 'lat_display': lat, 'lng_display': lng,
             'freshness_label': label, 'freshness_color': color if lat is not None else 'grey'}
        vehicles.append(d)
        if lat is not None:
            map_markers.append({'id': vid, 'lat': lat, 'lng': lng, 'vehicle_number': vnum, 'driver_name': driver_name,
                                 'driver_mobile': driver_mobile, 'status': status, 'po_count': po_count,
                                 'color': 'green' if (is_live and secs <= 60) else ('yellow' if (is_live and secs <= 600) else 'grey'),
                                 'mode': mode, 'freshness': label})
    conn.commit()
    conn.close()
    return render_template_string(VEHICLES_LIST_HTML, vehicles=vehicles, summary=summary, map_markers=map_markers,
                                   q=request.args.get('q', ''), add_vehicle_error=request.args.get('add_error', ''))

@app.route('/vehicles/add', methods=['POST'])
def vehicles_add():
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    vehicle_number = request.form.get('vehicle_number', '').strip().upper()
    if not vehicle_number:
        return redirect('/vehicles?add_error=' + quote('Vehicle number zaroori hai.'))
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM vehicle_master WHERE factory_id = %s AND vehicle_number = %s', (fid, vehicle_number))
    if cursor.fetchone():
        conn.close()
        return redirect('/vehicles?add_error=' + quote(f'Vehicle {vehicle_number} pehle se system mein hai.'))
    ok, limit_msg = check_usage_limit(cursor, 'vehicle')
    if not ok:
        conn.close()
        return redirect('/vehicles?add_error=' + quote(limit_msg))
    vehicle_id = get_or_create_vehicle(cursor, vehicle_number)
    log_audit(cursor, 'Vehicle Created', 'Vehicles', vehicle_id, f'Added vehicle {vehicle_number}')
    conn.commit()
    conn.close()
    return redirect(f'/vehicles/{vehicle_id}')

@app.route('/vehicles/start_loading', methods=['GET', 'POST'])
def vehicles_start_loading():
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()

    if request.method == 'GET':
        cursor.execute('SELECT id, vehicle_number FROM vehicle_master WHERE factory_id = %s ORDER BY vehicle_number', (fid,))
        existing_vehicles = []
        for vid, vnum in cursor.fetchall():
            status, _ = vehicle_master_status(cursor, vid)
            cursor.execute('SELECT driver_name, driver_mobile FROM trips WHERE vehicle_id = %s AND factory_id = %s ORDER BY id DESC LIMIT 1', (vid, fid))
            last = cursor.fetchone()
            existing_vehicles.append({'vehicle_number': vnum, 'status': status,
                                       'driver_name': last[0] if last else '', 'driver_mobile': last[1] if last else ''})
        cursor.execute('SELECT DISTINCT po_number FROM po_items WHERE factory_id = %s ORDER BY po_number', (fid,))
        all_po_numbers = [r[0] for r in cursor.fetchall()]
        conn.close()
        preselect = request.args.get('vehicle', '')
        return render_template_string(START_LOADING_HTML, existing_vehicles=existing_vehicles,
                                       all_po_numbers=all_po_numbers, preselect_vehicle=preselect)

    # POST — create the trip
    vehicle_choice = request.form.get('vehicle_choice', '').strip()
    new_vehicle_number = request.form.get('new_vehicle_number', '').strip().upper()
    vehicle_number = new_vehicle_number if vehicle_choice == '__new__' else vehicle_choice.strip().upper()
    driver_name = request.form.get('driver_name', '').strip()
    driver_mobile = request.form.get('driver_mobile', '').strip()
    start_location = request.form.get('start_location', '').strip()
    po_numbers = [p.strip() for p in request.form.getlist('po_numbers') if p.strip()]
    po_numbers = list(dict.fromkeys(po_numbers))

    if not (vehicle_number and driver_name and driver_mobile and start_location and po_numbers):
        conn.close()
        return "Sabhi required fields aur kam se kam 1 PO zaroori hai. <a href='/vehicles/start_loading'>Back</a>", 400
    if not re.match(r'^[0-9]{10}$', driver_mobile):
        conn.close()
        return "Driver mobile number 10 digit ka valid number hona chahiye. <a href='/vehicles/start_loading'>Back</a>", 400

    is_new_vehicle = False
    cursor.execute('SELECT id FROM vehicle_master WHERE factory_id = %s AND vehicle_number = %s', (fid, vehicle_number))
    if not cursor.fetchone():
        is_new_vehicle = True
        ok, limit_msg = check_usage_limit(cursor, 'vehicle')
        if not ok:
            conn.close()
            return f"{limit_msg} <a href='/vehicles/start_loading'>Back</a>", 400
    vehicle_id = get_or_create_vehicle(cursor, vehicle_number)
    status, active_trip_id = vehicle_master_status(cursor, vehicle_id)
    if active_trip_id:
        conn.close()
        return f"Vehicle {vehicle_number} already has an active loading session (Trip #{active_trip_id}). <a href='/vehicles'>Back</a>", 400

    for po in po_numbers:
        cursor.execute('''SELECT tr.vehicle_number FROM vehicle_po_map m JOIN trips tr ON tr.id = m.trip_id
                           WHERE tr.factory_id = %s AND m.po_number = %s AND m.is_cancelled = FALSE AND tr.trip_status NOT IN ('Delivered','Cancelled')''', (fid, po))
        clash = cursor.fetchone()
        if clash:
            conn.close()
            return f"PO {po} pehle se vehicle {clash[0]} mein active hai. <a href='/vehicles/start_loading'>Back</a>", 400

    token = gen_tracking_token()
    cursor.execute('''INSERT INTO trips (factory_id, vehicle_id, vehicle_number, driver_name, driver_mobile, start_location, trip_status,
                       loading_started_at, tracking_token, tracking_status) VALUES (%s,%s,%s,%s,%s,%s,'Loading',%s,%s,'active') RETURNING id''',
                   (fid, vehicle_id, vehicle_number, driver_name, driver_mobile, start_location, now_ist().strftime("%d %b %Y, %I:%M %p"), token))
    trip_id = cursor.fetchone()[0]
    for po in po_numbers:
        cursor.execute('SELECT company FROM po_items WHERE factory_id = %s AND po_number = %s LIMIT 1', (fid, po))
        comp = cursor.fetchone()
        company = comp[0] if comp else ''
        cursor.execute('INSERT INTO vehicle_po_map (factory_id, trip_id, po_number, company, created_at) VALUES (%s,%s,%s,%s,%s)',
                       (fid, trip_id, po, company, now_ist().isoformat()))
    cursor.execute('UPDATE vehicle_master SET updated_at = %s WHERE id = %s', (now_ist().isoformat(), vehicle_id))
    log_audit(cursor, 'Loading Started', 'Vehicles', trip_id, f'Vehicle {vehicle_number}, driver {driver_name}, POs: {", ".join(po_numbers)}')
    conn.commit()
    conn.close()
    return redirect(f'/trips/{trip_id}')

@app.route('/vehicles/<int:vehicle_id>')
def vehicle_master_detail(vehicle_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT id, vehicle_number, current_latitude, current_longitude, last_location_at FROM vehicle_master WHERE id = %s AND factory_id = %s', (vehicle_id, fid))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return "Vehicle not found. <a href='/vehicles'>Back</a>", 404
    vid, vnum, lat, lng, last_loc = row
    status, active_trip_id = vehicle_master_status(cursor, vid)
    driver_name, driver_mobile, tracking_status = None, None, 'stopped'
    if active_trip_id:
        cursor.execute('SELECT driver_name, driver_mobile, tracking_status FROM trips WHERE id = %s AND factory_id = %s', (active_trip_id, fid))
        driver_name, driver_mobile, tracking_status = cursor.fetchone()
    else:
        cursor.execute('SELECT driver_name, driver_mobile FROM trips WHERE vehicle_id = %s AND factory_id = %s ORDER BY id DESC LIMIT 1', (vid, fid))
        last = cursor.fetchone()
        if last: driver_name, driver_mobile = last

    label, color, secs = freshness_info(last_loc)
    is_live = tracking_status == 'active' and status in ('Loading', 'Ready to Dispatch', 'In Transit')
    mode = 'LIVE' if (is_live and secs <= 600) else 'LAST KNOWN'
    veh = {'id': vid, 'vehicle_number': vnum, 'driver_name': driver_name, 'driver_mobile': driver_mobile,
           'status': status, 'status_class': TRIP_STATUS_BADGE.get(status, 'badge-amber'),
           'has_location': lat is not None, 'lat_display': lat, 'lng_display': lng,
           'freshness_label': label, 'freshness_color': color if lat is not None else 'grey', 'mode': mode if lat is not None else 'NO DATA'}

    cursor.execute('SELECT id, trip_status, loading_started_at FROM trips WHERE vehicle_id = %s AND factory_id = %s ORDER BY id DESC', (vid, fid))
    trips = []
    for tid, tstatus, started in cursor.fetchall():
        cursor.execute('SELECT COUNT(*) FROM vehicle_po_map WHERE trip_id = %s', (tid,))
        po_count = cursor.fetchone()[0]
        trips.append({'id': tid, 'trip_status': tstatus, 'loading_started_at': started, 'po_count': po_count,
                       'status_class': TRIP_STATUS_BADGE.get(tstatus, 'badge-amber')})

    route_trip_id = request.args.get('route_trip', type=int)
    route_points = []
    if route_trip_id:
        cursor.execute('SELECT latitude, longitude FROM location_history WHERE trip_id = %s AND factory_id = %s ORDER BY id ASC LIMIT 1000', (route_trip_id, fid))
        route_points = [[r[0], r[1]] for r in cursor.fetchall()]

    cursor.execute('SELECT recorded_at, latitude, longitude FROM location_history WHERE vehicle_id = %s AND factory_id = %s ORDER BY id DESC LIMIT 50', (vid, fid))
    location_history = []
    for rec_at, hlat, hlng in cursor.fetchall():
        try:
            dt = datetime.fromisoformat(rec_at)
            date_s, time_s = dt.strftime('%d %b %Y'), dt.strftime('%I:%M %p')
        except Exception:
            date_s, time_s = rec_at, ''
        location_history.append({'date': date_s, 'time': time_s, 'lat': round(hlat, 5), 'lng': round(hlng, 5)})

    conn.commit()
    conn.close()
    return render_template_string(VEHICLE_MASTER_DETAIL_HTML, veh=veh, trips=trips, route_trip_id=route_trip_id,
                                   route_points=route_points, location_history=location_history)

def _trip_dict(cursor, row):
    (tid, vehicle_id, vehicle_number, driver_name, driver_mobile, start_location, trip_status,
     loading_started_at, cur_lat, cur_lng, cur_acc, last_loc_at, tracking_token, tracking_status,
     dispatched_at, delivered_at) = row
    cursor.execute('SELECT po_number, is_hold, is_cancelled FROM vehicle_po_map WHERE trip_id = %s', (tid,))
    pos = cursor.fetchall()
    completed, total_ordered, total_loaded, active_count = 0, 0, 0, 0
    for po_number, is_hold, is_cancelled in pos:
        if is_cancelled: continue
        active_count += 1
        ordered, loaded = po_progress(cursor, po_number, vehicle_number)
        total_ordered += ordered
        total_loaded += min(loaded, ordered) if ordered else loaded
        if ordered and loaded >= ordered: completed += 1
    percent = min(100, round((total_loaded / total_ordered) * 100)) if total_ordered else 0
    label, color, _ = freshness_info(last_loc_at)
    return {
        'id': tid, 'vehicle_id': vehicle_id, 'vehicle_number': vehicle_number, 'driver_name': driver_name,
        'driver_mobile': driver_mobile, 'start_location': start_location, 'trip_status': trip_status,
        'loading_started_at': loading_started_at, 'current_latitude': cur_lat, 'current_longitude': cur_lng,
        'tracking_token': tracking_token, 'tracking_status': tracking_status, 'po_count': active_count,
        'completed_count': completed, 'percent': percent, 'freshness_label': label, 'freshness_color': color,
        'status_class': TRIP_STATUS_BADGE.get(trip_status, 'badge-amber'),
    }

@app.route('/trips/<int:trip_id>')
def trip_detail(trip_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    recompute_trip_status(cursor, trip_id)
    cursor.execute('''SELECT id, vehicle_id, vehicle_number, driver_name, driver_mobile, start_location, trip_status,
        loading_started_at, current_latitude, current_longitude, current_accuracy, last_location_at,
        tracking_token, tracking_status, dispatched_at, delivered_at FROM trips WHERE id = %s AND factory_id = %s''', (trip_id, fid))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return "Trip not found. <a href='/vehicles'>Back</a>", 404
    t = _trip_dict(cursor, row)

    cursor.execute('SELECT po_number, company, is_hold, is_cancelled FROM vehicle_po_map WHERE trip_id = %s ORDER BY id', (trip_id,))
    pos = []
    for po_number, company, is_hold, is_cancelled in cursor.fetchall():
        ordered, loaded = po_progress(cursor, po_number, t['vehicle_number'])
        pending = max(ordered - loaded, 0)
        status, status_class = po_status_label(ordered, loaded, is_hold, is_cancelled)
        pos.append({'po_number': po_number, 'company': company, 'ordered': ordered, 'loaded': loaded,
                    'pending': pending, 'status': status, 'status_class': status_class, 'is_hold': is_hold})

    attached = {p['po_number'] for p in pos}
    cursor.execute('SELECT DISTINCT po_number FROM po_items WHERE factory_id = %s ORDER BY po_number', (fid,))
    available_pos = [p[0] for p in cursor.fetchall() if p[0] not in attached]

    cursor.execute('SELECT latitude, longitude FROM location_history WHERE trip_id = %s ORDER BY id ASC LIMIT 500', (trip_id,))
    route_points = [[r[0], r[1]] for r in cursor.fetchall()]

    conn.commit()
    conn.close()
    track_url = request.url_root.rstrip('/') + f"/track/{t['tracking_token']}" if t['tracking_token'] else ''
    return render_template_string(TRIP_DETAIL_HTML, t=t, pos=pos, available_pos=available_pos,
                                   route_points=route_points, track_url=track_url, track_url_encoded=quote(track_url))

@app.route('/trips/<int:trip_id>/add_po', methods=['POST'])
def trip_add_po(trip_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    po = request.form.get('po_number', '').strip()
    if not po: return redirect(f'/trips/{trip_id}')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT vehicle_number FROM trips WHERE id = %s AND factory_id = %s', (trip_id, fid))
    trow = cursor.fetchone()
    if not trow:
        conn.close(); return redirect('/vehicles')
    cursor.execute('''SELECT tr.vehicle_number FROM vehicle_po_map m JOIN trips tr ON tr.id = m.trip_id
                       WHERE tr.factory_id = %s AND m.po_number = %s AND m.is_cancelled = FALSE AND tr.trip_status NOT IN ('Delivered','Cancelled')''', (fid, po))
    clash = cursor.fetchone()
    if clash:
        conn.close()
        return f"PO {po} already active on vehicle {clash[0]}. <a href='/trips/{trip_id}'>Back</a>", 400
    cursor.execute('SELECT company FROM po_items WHERE factory_id = %s AND po_number = %s LIMIT 1', (fid, po))
    comp = cursor.fetchone()
    company = comp[0] if comp else ''
    cursor.execute('INSERT INTO vehicle_po_map (factory_id, trip_id, po_number, company, created_at) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (trip_id, po_number) DO NOTHING',
                   (fid, trip_id, po, company, now_ist().isoformat()))
    recompute_trip_status(cursor, trip_id)
    conn.commit()
    conn.close()
    return redirect(f'/trips/{trip_id}')

@app.route('/trips/<int:trip_id>/remove_po', methods=['POST'])
def trip_remove_po(trip_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    po = request.form.get('po_number', '').strip()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM trips WHERE id = %s AND factory_id = %s', (trip_id, fid))
    if not cursor.fetchone():
        conn.close(); return redirect('/vehicles')
    cursor.execute('DELETE FROM vehicle_po_map WHERE trip_id = %s AND po_number = %s', (trip_id, po))
    recompute_trip_status(cursor, trip_id)
    conn.commit()
    conn.close()
    return redirect(f'/trips/{trip_id}')

@app.route('/trips/<int:trip_id>/po_status', methods=['POST'])
def trip_po_status(trip_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    po = request.form.get('po_number', '').strip()
    action = request.form.get('action', '')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM trips WHERE id = %s AND factory_id = %s', (trip_id, fid))
    if not cursor.fetchone():
        conn.close(); return redirect('/vehicles')
    if action == 'hold':
        cursor.execute('UPDATE vehicle_po_map SET is_hold = TRUE WHERE trip_id = %s AND po_number = %s', (trip_id, po))
    elif action == 'resume':
        cursor.execute('UPDATE vehicle_po_map SET is_hold = FALSE WHERE trip_id = %s AND po_number = %s', (trip_id, po))
    recompute_trip_status(cursor, trip_id)
    conn.commit()
    conn.close()
    return redirect(f'/trips/{trip_id}')

@app.route('/trips/<int:trip_id>/set_active_po', methods=['POST'])
def trip_set_active_po(trip_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    po = request.form.get('po_number', '').strip()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT vehicle_number, start_location FROM trips WHERE id = %s AND factory_id = %s', (trip_id, fid))
    row = cursor.fetchone()
    conn.close()
    if row:
        set_active_session(po, row[0], row[1])
    return redirect('/')

@app.route('/trips/<int:trip_id>/regen_token', methods=['POST'])
def trip_regen_token(trip_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE trips SET tracking_token = %s, tracking_status = 'active' WHERE id = %s AND factory_id = %s", (gen_tracking_token(), trip_id, fid))
    conn.commit()
    conn.close()
    return redirect(f'/trips/{trip_id}')

@app.route('/trips/<int:trip_id>/stop_tracking', methods=['POST'])
def trip_stop_tracking(trip_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE trips SET tracking_status = 'stopped' WHERE id = %s AND factory_id = %s", (trip_id, fid))
    conn.commit()
    conn.close()
    return redirect(f'/trips/{trip_id}')

@app.route('/trips/<int:trip_id>/dispatch', methods=['POST'])
def trip_dispatch(trip_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE trips SET trip_status = 'In Transit', dispatched_at = %s WHERE id = %s AND factory_id = %s",
                   (now_ist().strftime("%d %b %Y, %I:%M %p"), trip_id, fid))
    log_audit(cursor, 'Dispatch Completed', 'Vehicles', trip_id, 'Trip marked In Transit')
    conn.commit()
    conn.close()
    return redirect(f'/trips/{trip_id}')

@app.route('/trips/<int:trip_id>/deliver', methods=['POST'])
def trip_deliver(trip_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE trips SET trip_status = 'Delivered', delivered_at = %s, tracking_status = 'stopped' WHERE id = %s AND factory_id = %s",
                   (now_ist().strftime("%d %b %Y, %I:%M %p"), trip_id, fid))
    log_audit(cursor, 'Delivery Completed', 'Vehicles', trip_id, 'Trip marked Delivered')
    conn.commit()
    conn.close()
    return redirect(f'/trips/{trip_id}')

@app.route('/track/<token>')
def track_page(token):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('''SELECT id, vehicle_number, driver_name, driver_mobile, start_location, trip_status,
        tracking_status FROM trips WHERE tracking_token = %s''', (token,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return "Invalid or expired tracking link.", 404
    tid, vehicle_number, driver_name, driver_mobile, start_location, trip_status, tracking_status = row
    t = {'vehicle_number': vehicle_number, 'driver_name': driver_name, 'trip_status': trip_status,
         'status_class': TRIP_STATUS_BADGE.get(trip_status, 'badge-amber')}
    trackable = tracking_status == 'active' and trip_status not in ('Delivered', 'Cancelled')
    return render_template_string(TRACK_HTML, t=t, token=token, trackable=trackable)

@app.route('/track/<token>/ping', methods=['POST'])
def track_ping(token):
    data = request.get_json(silent=True) or {}
    lat, lng, acc = data.get('lat'), data.get('lng'), data.get('accuracy')
    if lat is None or lng is None:
        return jsonify({'ok': False, 'error': 'missing coordinates'}), 400
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, factory_id, vehicle_id, trip_status, tracking_status FROM trips WHERE tracking_token = %s", (token,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return jsonify({'ok': False, 'error': 'invalid token'}), 404
    tid, tfid, vehicle_id, trip_status, tracking_status = row
    if tracking_status != 'active' or trip_status in ('Delivered', 'Cancelled'):
        conn.close()
        return jsonify({'ok': False, 'error': 'tracking not active'}), 403
    ts = now_ist().isoformat()
    # Update the trip's own snapshot (for backward-compat display) and the permanent vehicle_master record
    cursor.execute('''UPDATE trips SET current_latitude=%s, current_longitude=%s, current_accuracy=%s, last_location_at=%s WHERE id=%s''',
                   (lat, lng, acc, ts, tid))
    if vehicle_id:
        cursor.execute('''UPDATE vehicle_master SET current_latitude=%s, current_longitude=%s, current_accuracy=%s,
                           last_location_at=%s, gps_status='live', updated_at=%s WHERE id=%s''',
                       (lat, lng, acc, ts, ts, vehicle_id))
    cursor.execute('INSERT INTO location_history (factory_id, vehicle_id, trip_id, latitude, longitude, accuracy, recorded_at) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                   (tfid, vehicle_id, tid, lat, lng, acc, ts))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


REGISTER_HTML = STYLE_BLOCK + """
<title>Create Account | {{ platform_name }}</title>
</head>
<body style="background-color:#0b1120; background-image:
        radial-gradient(circle at 15% 20%, rgba(59,130,246,0.20), transparent 45%),
        radial-gradient(circle at 85% 80%, rgba(139,92,246,0.16), transparent 50%);
    background-attachment:fixed; margin:0; min-height:100vh; position:relative; overflow:hidden;">
    <div class="hero-particle" style="left:12%; animation-delay:0s;"></div>
    <div class="hero-particle" style="left:30%; animation-delay:2s;"></div>
    <div class="hero-particle" style="left:70%; animation-delay:3.5s;"></div>
    <div class="conveyor-track" style="opacity:0.6;"></div>
    <div class="factory-truck">🚚</div>
    <div style="display:flex; justify-content:center; align-items:center; min-height:100vh; position:relative; z-index:3; padding:24px 0;">
        <div class="card" style="width:360px; text-align:center; padding:32px 28px; backdrop-filter:blur(16px);">
            <div class="brand-logo" style="margin:0 auto 14px;">AI</div>
            <h2 style="margin:0 0 4px; font-size:19px;">Create Your Company Account</h2>
            <div style="color:var(--text-muted); font-size:12.5px; margin-bottom:18px;">on {{ platform_name }}</div>
            {% if error %}
            <div style="color:#fca5a5; font-size:12.5px; margin-bottom:12px; text-align:left;">{{ error }}</div>
            {% endif %}
            <form method="POST" style="text-align:left;">
                <label style="margin-top:0;">Company Name</label>
                <input type="text" name="company_name" placeholder="e.g. ABC Foods Pvt Ltd" required value="{{ form.company_name or '' }}">
                <label>Your Name</label>
                <input type="text" name="admin_name" placeholder="Admin's full name" required value="{{ form.admin_name or '' }}">
                <label>Username (for login)</label>
                <input type="text" name="username" placeholder="e.g. abcfoods_admin" required value="{{ form.username or '' }}">
                <label>Mobile</label>
                <input type="text" name="mobile" placeholder="10-digit mobile number" value="{{ form.mobile or '' }}">
                <label>Password</label>
                <input type="password" name="password" placeholder="Choose a password" required>
                <label>Confirm Password</label>
                <input type="password" name="confirm_password" placeholder="Re-enter password" required>
                <button type="submit" class="btn btn-block" style="margin-top:16px;">Create Account</button>
            </form>
            <div style="margin-top:16px; padding-top:14px; border-top:1px solid var(--border); font-size:12.5px; color:var(--text-muted);">
                Already have an account? <a href="/login" style="color:var(--primary); font-weight:600; text-decoration:none;">Log in</a>
            </div>
        </div>
    </div>
</body>
</html>
"""

USERS_HTML = STYLE_BLOCK + """
<title>Users | {{ factory_display_name }}</title>
</head>
<body>
<div class="container">
""" + TOPBAR_TEMPLATE.replace("__NAV__", nav_html('users')) + """
    <div class="stats-grid">
        <div class="stat-card"><div class="icon">📦</div><div class="label">Plan</div><div class="value" style="font-size:18px;">{{ plan_info.plan }}</div></div>
        <div class="stat-card"><div class="icon">👥</div><div class="label">Users</div><div class="value">{{ plan_info.user_count }}/{{ plan_info.user_limit }}</div></div>
        <div class="stat-card"><div class="icon">🚚</div><div class="label">Vehicles</div><div class="value">{{ plan_info.vehicle_count }}/{{ plan_info.vehicle_limit }}</div></div>
    </div>

    <div class="card">
        <div class="card-header">
            <h2>➕ Add Team Member</h2>
        </div>
        {% if error_msg %}
        <div class="badge badge-amber" style="display:block; padding:10px 14px; margin-bottom:14px; font-size:13px;">{{ error_msg }}</div>
        {% endif %}
        {% if ok_msg %}
        <div class="badge badge-green" style="display:block; padding:10px 14px; margin-bottom:14px; font-size:13px;">{{ ok_msg }}</div>
        {% endif %}
        <form method="POST" action="/users/add" class="form-grid">
            <div><label>Name</label><input type="text" name="name" required></div>
            <div><label>Username</label><input type="text" name="username" required></div>
            <div><label>Mobile</label><input type="text" name="mobile"></div>
            <div><label>Password</label><input type="password" name="password" required></div>
            <div>
                <label>Role</label>
                <select name="role">
                    <option value="Factory Admin">Factory Admin (full access)</option>
                    <option value="Manager" selected>Manager (full access, can't manage users)</option>
                    <option value="Viewer">Viewer (read-only)</option>
                </select>
            </div>
            <div><button type="submit" class="btn btn-block" style="margin-top:18px;">Add User</button></div>
        </form>
    </div>

    <div class="card">
        <div class="card-header"><h2>👥 Team Members</h2></div>
        {% if users|length > 0 %}
        <table>
            <thead><tr><th>Name</th><th>Username</th><th>Mobile</th><th>Role</th><th>Status</th><th>Last Login</th><th></th></tr></thead>
            <tbody>
            {% for u in users %}
                <tr>
                    <td>{{ u.name }}</td>
                    <td>{{ u.username }}</td>
                    <td>{{ u.mobile or '—' }}</td>
                    <td><span class="badge badge-blue">{{ u.role }}</span></td>
                    <td><span class="badge {{ 'badge-green' if u.status == 'Active' else 'badge-amber' }}">{{ u.status }}</span></td>
                    <td style="color:var(--text-muted); font-size:12px;">{{ u.last_login or 'Never' }}</td>
                    <td>
                        {% if u.id != session.get('user_id') %}
                        <form method="POST" action="/users/toggle/{{ u.id }}" style="display:inline;">
                            <button type="submit" class="btn btn-outline btn-sm">{{ 'Deactivate' if u.status == 'Active' else 'Activate' }}</button>
                        </form>
                        {% endif %}
                    </td>
                </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <div class="empty-state">No team members yet.</div>
        {% endif %}
    </div>

    <footer>{{ factory_display_name }} &middot; {{ platform_name }}</footer>
</div>
</body>
</html>
"""

AUDIT_HTML = STYLE_BLOCK + """
<title>Audit Log | {{ factory_display_name }}</title>
</head>
<body>
<div class="container">
""" + TOPBAR_TEMPLATE.replace("__NAV__", nav_html('users')) + """
    <div class="card">
        <div class="card-header">
            <h2>📋 Audit Log</h2>
            <span style="color:var(--text-muted); font-size:12.5px;">Last 200 actions</span>
        </div>
        {% if entries|length > 0 %}
        <table>
            <thead><tr><th>When</th><th>User</th><th>Action</th><th>Module</th><th>Details</th></tr></thead>
            <tbody>
            {% for e in entries %}
                <tr>
                    <td style="color:var(--text-muted); font-size:12px; white-space:nowrap;">{{ e.timestamp }}</td>
                    <td>{{ e.user_name or '—' }}</td>
                    <td><span class="badge badge-blue">{{ e.action }}</span></td>
                    <td>{{ e.module }}</td>
                    <td style="color:var(--text-muted); font-size:12.5px;">{{ e.details }}</td>
                </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <div class="empty-state">No activity recorded yet.</div>
        {% endif %}
    </div>

    <footer>{{ factory_display_name }} &middot; {{ platform_name }}</footer>
</div>
</body>
</html>
"""

@app.route('/audit')
def audit_page():
    if not session.get('logged_in'): return redirect('/login')
    if session.get('role') != 'Factory Admin':
        return "Access denied — only a Factory Admin can view the audit log.", 403
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT timestamp, user_name, action, module, details FROM audit_log WHERE factory_id = %s ORDER BY id DESC LIMIT 200', (fid,))
    entries = [{'timestamp': r[0], 'user_name': r[1], 'action': r[2], 'module': r[3], 'details': r[4]} for r in cursor.fetchall()]
    conn.close()
    return render_template_string(AUDIT_HTML, entries=entries)

@app.route('/users')
def users_page():
    if not session.get('logged_in'): return redirect('/login')
    if session.get('role') != 'Factory Admin':
        return "Access denied — only a Factory Admin can manage team members.", 403
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT id, name, username, mobile, role, status, last_login FROM users WHERE factory_id = %s ORDER BY id', (fid,))
    users = [{'id': r[0], 'name': r[1], 'username': r[2], 'mobile': r[3], 'role': r[4], 'status': r[5], 'last_login': r[6]} for r in cursor.fetchall()]
    cursor.execute('SELECT plan, user_limit, vehicle_limit FROM factories WHERE id = %s', (fid,))
    plan, user_limit, vehicle_limit = cursor.fetchone()
    cursor.execute("SELECT COUNT(*) FROM users WHERE factory_id = %s AND status = 'Active'", (fid,))
    user_count = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM vehicle_master WHERE factory_id = %s', (fid,))
    vehicle_count = cursor.fetchone()[0]
    plan_info = {'plan': plan, 'user_limit': user_limit, 'vehicle_limit': vehicle_limit, 'user_count': user_count, 'vehicle_count': vehicle_count}
    conn.close()
    return render_template_string(USERS_HTML, users=users, plan_info=plan_info, error_msg=request.args.get('error'), ok_msg=request.args.get('ok'))

@app.route('/users/add', methods=['POST'])
def users_add():
    if not session.get('logged_in'): return redirect('/login')
    if session.get('role') != 'Factory Admin':
        return "Access denied — only a Factory Admin can manage team members.", 403
    fid = current_factory_id()
    name = request.form.get('name', '').strip()
    username = request.form.get('username', '').strip()
    mobile = request.form.get('mobile', '').strip()
    password = request.form.get('password', '')
    role = request.form.get('role', 'Manager')
    if role not in ('Factory Admin', 'Manager', 'Viewer'):
        role = 'Manager'
    if not (name and username and password):
        return redirect('/users?error=' + quote('Name, username, and password are required.'))
    if len(password) < 6:
        return redirect('/users?error=' + quote('Password must be at least 6 characters.'))
    conn = get_conn()
    cursor = conn.cursor()
    ok, limit_msg = check_usage_limit(cursor, 'user')
    if not ok:
        conn.close()
        return redirect('/users?error=' + quote(limit_msg))
    cursor.execute('SELECT id FROM users WHERE username = %s', (username,))
    if cursor.fetchone():
        conn.close()
        return redirect('/users?error=' + quote(f'Username "{username}" is already taken.'))
    ts = now_ist().isoformat()
    cursor.execute('''INSERT INTO users (factory_id, name, mobile, username, password_hash, role, status, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, 'Active', %s, %s)''',
                   (fid, name, mobile, username, generate_password_hash(password), role, ts, ts))
    log_audit(cursor, 'User Created', 'Users', '', f'Added {name} ({username}) as {role}')
    conn.commit()
    conn.close()
    return redirect('/users?ok=' + quote(f'{name} added successfully.'))

@app.route('/users/toggle/<int:user_id>', methods=['POST'])
def users_toggle(user_id):
    if not session.get('logged_in'): return redirect('/login')
    if session.get('role') != 'Factory Admin':
        return "Access denied — only a Factory Admin can manage team members.", 403
    fid = current_factory_id()
    if user_id == session.get('user_id'):
        return redirect('/users?error=' + quote("You can't deactivate your own account."))
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT status FROM users WHERE id = %s AND factory_id = %s', (user_id, fid))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return redirect('/users')
    new_status = 'Inactive' if row[0] == 'Active' else 'Active'
    cursor.execute('UPDATE users SET status = %s, updated_at = %s WHERE id = %s AND factory_id = %s', (new_status, now_ist().isoformat(), user_id, fid))
    conn.commit()
    conn.close()
    return redirect('/users')

@app.route('/register', methods=['GET', 'POST'])
def register():
    error = None
    form = {}
    if request.method == 'POST':
        form = {k: request.form.get(k, '').strip() for k in ['company_name', 'admin_name', 'username', 'mobile']}
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not (form['company_name'] and form['admin_name'] and form['username'] and password):
            error = 'Company name, your name, username, and password are all required.'
        elif password != confirm_password:
            error = 'Passwords do not match.'
        elif len(password) < 6:
            error = 'Password must be at least 6 characters.'
        elif not re.match(r'^[a-zA-Z0-9_.]+$', form['username']):
            error = 'Username can only contain letters, numbers, dots, and underscores.'

        if not error:
            conn = get_conn()
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM users WHERE username = %s', (form['username'],))
            if cursor.fetchone():
                error = f"Username \"{form['username']}\" is already taken. Please choose another."
                conn.close()
            else:
                ts = now_ist().isoformat()
                cursor.execute('''INSERT INTO factories (company_name, display_name, status, created_at, updated_at, created_by)
                                   VALUES (%s, %s, 'Active', %s, %s, %s) RETURNING id''',
                               (form['company_name'], form['company_name'], ts, ts, form['username']))
                new_fid = cursor.fetchone()[0]
                cursor.execute('''INSERT INTO users (factory_id, name, mobile, username, password_hash, role, status, created_at, updated_at)
                                   VALUES (%s, %s, %s, %s, %s, 'Factory Admin', 'Active', %s, %s)''',
                               (new_fid, form['admin_name'], form['mobile'], form['username'], generate_password_hash(password), ts, ts))
                conn.commit()
                conn.close()
                return redirect('/login?registered=1')
    return render_template_string(REGISTER_HTML, error=error, form=form)

SUPERADMIN_HTML = STYLE_BLOCK + """
<title>Super Admin | {{ platform_name }}</title>
</head>
<body>
<div class="container">
    <div class="topbar">
        <div class="brand">
            <div class="brand-logo">SA</div>
            <div>
                <h1>{{ platform_name }}</h1>
                <span>Super Admin &middot; Platform Control</span>
            </div>
        </div>
        <div class="nav"><a href="/change_password" class="">🔑 Change Password</a><a href="/logout" class="">Logout</a></div>
    </div>

    <div class="stats-grid">
        <div class="stat-card"><div class="icon">🏭</div><div class="label">Total Factories</div><div class="value">{{ summary.total }}</div></div>
        <div class="stat-card"><div class="icon">✅</div><div class="label">Active</div><div class="value">{{ summary.active }}</div></div>
        <div class="stat-card"><div class="icon">⛔</div><div class="label">Suspended</div><div class="value">{{ summary.suspended }}</div></div>
        <div class="stat-card"><div class="icon">👥</div><div class="label">Total Users</div><div class="value">{{ summary.total_users }}</div></div>
    </div>

    <div class="card">
        <div class="card-header"><h2>➕ Create Factory</h2></div>
        {% if error_msg %}
        <div class="badge badge-amber" style="display:block; padding:10px 14px; margin-bottom:14px; font-size:13px;">{{ error_msg }}</div>
        {% endif %}
        {% if ok_msg %}
        <div class="badge badge-green" style="display:block; padding:10px 14px; margin-bottom:14px; font-size:13px;">{{ ok_msg }}</div>
        {% endif %}
        <form method="POST" action="/superadmin/create" class="form-grid">
            <div><label>Company Name</label><input type="text" name="company_name" required></div>
            <div><label>Admin Name</label><input type="text" name="admin_name" required></div>
            <div><label>Admin Username</label><input type="text" name="username" required></div>
            <div><label>Admin Password</label><input type="password" name="password" required></div>
            <div><button type="submit" class="btn btn-block" style="margin-top:18px;">Create Factory</button></div>
        </form>
    </div>

    <div class="card">
        <div class="card-header"><h2>🏢 All Factories</h2></div>
        {% if factories|length > 0 %}
        <table>
            <thead><tr><th>Company</th><th>Status</th><th>Plan</th><th>Users</th><th>Vehicles</th><th>PO Items</th><th>Created</th><th></th></tr></thead>
            <tbody>
            {% for f in factories %}
                <tr>
                    <td>{{ f.company_name }}</td>
                    <td><span class="badge {{ 'badge-green' if f.status == 'Active' else 'badge-amber' }}">{{ f.status }}</span></td>
                    <td>
                        <form method="POST" action="/superadmin/update_plan/{{ f.id }}" style="display:flex; gap:4px; align-items:center;">
                            <select name="plan" style="margin:0; padding:6px 8px; font-size:12px;">
                                {% for p in ['Free', 'Basic', 'Professional', 'Enterprise'] %}
                                <option value="{{ p }}" {{ 'selected' if f.plan == p else '' }}>{{ p }}</option>
                                {% endfor %}
                            </select>
                            <input type="number" name="user_limit" value="{{ f.user_limit }}" style="width:52px; margin:0; padding:6px; font-size:12px;" title="User limit">
                            <input type="number" name="vehicle_limit" value="{{ f.vehicle_limit }}" style="width:52px; margin:0; padding:6px; font-size:12px;" title="Vehicle limit">
                            <button type="submit" class="btn btn-outline btn-sm">Save</button>
                        </form>
                    </td>
                    <td>{{ f.user_count }}</td>
                    <td>{{ f.vehicle_count }}</td>
                    <td>{{ f.po_count }}</td>
                    <td style="color:var(--text-muted); font-size:12px;">{{ f.created_at }}</td>
                    <td>
                        <form method="POST" action="/superadmin/toggle/{{ f.id }}" style="display:inline;">
                            <button type="submit" class="btn btn-outline btn-sm">{{ 'Suspend' if f.status == 'Active' else 'Activate' }}</button>
                        </form>
                    </td>
                </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <div class="empty-state">No factories yet.</div>
        {% endif %}
    </div>

    <footer>{{ platform_name }} &middot; Super Admin Panel</footer>
</div>
</body>
</html>
"""

@app.route('/superadmin')
def superadmin_page():
    if not session.get('logged_in'): return redirect('/login')
    if not is_super_admin():
        return "Access denied — Super Admin only.", 403
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT id, company_name, status, plan, user_limit, vehicle_limit, created_at FROM factories ORDER BY id')
    rows = cursor.fetchall()
    factories = []
    summary = {'total': 0, 'active': 0, 'suspended': 0, 'total_users': 0}
    for fid, cname, status, plan, user_limit, vehicle_limit, created_at in rows:
        cursor.execute('SELECT COUNT(*) FROM users WHERE factory_id = %s', (fid,))
        user_count = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM vehicle_master WHERE factory_id = %s', (fid,))
        vehicle_count = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM po_items WHERE factory_id = %s', (fid,))
        po_count = cursor.fetchone()[0]
        factories.append({'id': fid, 'company_name': cname, 'status': status, 'plan': plan, 'user_limit': user_limit, 'vehicle_limit': vehicle_limit,
                           'created_at': (created_at or '')[:10], 'user_count': user_count, 'vehicle_count': vehicle_count, 'po_count': po_count})
        summary['total'] += 1
        summary['total_users'] += user_count
        if status == 'Active': summary['active'] += 1
        else: summary['suspended'] += 1
    conn.close()
    return render_template_string(SUPERADMIN_HTML, factories=factories, summary=summary,
                                   error_msg=request.args.get('error'), ok_msg=request.args.get('ok'))

@app.route('/superadmin/update_plan/<int:factory_id>', methods=['POST'])
def superadmin_update_plan(factory_id):
    if not session.get('logged_in'): return redirect('/login')
    if not is_super_admin():
        return "Access denied — Super Admin only.", 403
    plan = request.form.get('plan', 'Free')
    if plan not in ('Free', 'Basic', 'Professional', 'Enterprise'):
        plan = 'Free'
    try:
        user_limit = int(request.form.get('user_limit', 5))
        vehicle_limit = int(request.form.get('vehicle_limit', 10))
    except ValueError:
        return redirect('/superadmin?error=' + quote('User/vehicle limits must be numbers.'))
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('UPDATE factories SET plan = %s, user_limit = %s, vehicle_limit = %s, updated_at = %s WHERE id = %s',
                   (plan, user_limit, vehicle_limit, now_ist().isoformat(), factory_id))
    cursor.execute('INSERT INTO audit_log (factory_id, user_id, user_name, action, module, record_id, details, timestamp) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                   (factory_id, session.get('user_id'), session.get('user_name'), 'Plan Updated', 'Super Admin', factory_id,
                    f'Plan={plan}, user_limit={user_limit}, vehicle_limit={vehicle_limit}', now_ist().isoformat()))
    conn.commit()
    conn.close()
    return redirect('/superadmin?ok=' + quote('Plan updated.'))

@app.route('/superadmin/create', methods=['POST'])
def superadmin_create():
    if not session.get('logged_in'): return redirect('/login')
    if not is_super_admin():
        return "Access denied — Super Admin only.", 403
    company_name = request.form.get('company_name', '').strip()
    admin_name = request.form.get('admin_name', '').strip()
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    if not (company_name and admin_name and username and password):
        return redirect('/superadmin?error=' + quote('All fields are required.'))
    if len(password) < 6:
        return redirect('/superadmin?error=' + quote('Password must be at least 6 characters.'))
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM users WHERE username = %s', (username,))
    if cursor.fetchone():
        conn.close()
        return redirect('/superadmin?error=' + quote(f'Username "{username}" is already taken.'))
    ts = now_ist().isoformat()
    cursor.execute('''INSERT INTO factories (company_name, display_name, status, created_at, updated_at, created_by)
                       VALUES (%s, %s, 'Active', %s, %s, %s) RETURNING id''',
                   (company_name, company_name, ts, ts, session.get('username')))
    new_fid = cursor.fetchone()[0]
    cursor.execute('''INSERT INTO users (factory_id, name, username, password_hash, role, status, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, 'Factory Admin', 'Active', %s, %s)''',
                   (new_fid, admin_name, username, generate_password_hash(password), ts, ts))
    cursor.execute('INSERT INTO audit_log (factory_id, user_id, user_name, action, module, record_id, details, timestamp) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                   (new_fid, session.get('user_id'), session.get('user_name'), 'Factory Created', 'Super Admin', new_fid, f'Created by Super Admin, admin user: {username}', ts))
    conn.commit()
    conn.close()
    return redirect('/superadmin?ok=' + quote(f'{company_name} created successfully.'))

@app.route('/superadmin/toggle/<int:factory_id>', methods=['POST'])
def superadmin_toggle(factory_id):
    if not session.get('logged_in'): return redirect('/login')
    if not is_super_admin():
        return "Access denied — Super Admin only.", 403
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT status FROM factories WHERE id = %s', (factory_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return redirect('/superadmin')
    new_status = 'Suspended' if row[0] == 'Active' else 'Active'
    ts = now_ist().isoformat()
    cursor.execute('UPDATE factories SET status = %s, updated_at = %s WHERE id = %s', (new_status, ts, factory_id))
    cursor.execute('INSERT INTO audit_log (factory_id, user_id, user_name, action, module, record_id, details, timestamp) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                   (factory_id, session.get('user_id'), session.get('user_name'), f'Factory {new_status}', 'Super Admin', factory_id, '', ts))
    conn.commit()
    conn.close()
    return redirect('/superadmin')

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = False
    inactive = False
    username = ''
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT id, factory_id, name, password_hash, role, status FROM users WHERE username = %s', (username,))
        row = cursor.fetchone()
        if row and check_password_hash(row[3], password):
            user_id, factory_id, name, _, role, status = row
            if status != 'Active':
                inactive = True
            elif role == 'Super Admin':
                # Platform-level account: not tied to any single factory, skips the per-factory active check.
                cursor.execute('UPDATE users SET last_login = %s WHERE id = %s', (now_ist().isoformat(), user_id))
                conn.commit()
                session['logged_in'] = True
                session['user_id'] = user_id
                session['factory_id'] = None
                session['user_name'] = name
                session['username'] = username
                session['role'] = role
                conn.close()
                return redirect('/superadmin')
            else:
                cursor.execute('SELECT status FROM factories WHERE id = %s', (factory_id,))
                frow = cursor.fetchone()
                if not frow or frow[0] != 'Active':
                    inactive = True
                else:
                    cursor.execute('UPDATE users SET last_login = %s WHERE id = %s', (now_ist().isoformat(), user_id))
                    conn.commit()
                    session['logged_in'] = True
                    session['user_id'] = user_id
                    session['factory_id'] = factory_id
                    session['user_name'] = name
                    session['username'] = username
                    session['role'] = role
                    conn.close()
                    return redirect('/')
        else:
            error = True
        conn.close()
    return render_template_string(LOGIN_HTML, error=error, inactive=inactive, username=username)

CHANGE_PASSWORD_HTML = STYLE_BLOCK + """
<title>Change Password | {{ factory_display_name }}</title>
</head>
<body>
<div class="container">
""" + TOPBAR_TEMPLATE.replace("__NAV__", "") + """
    <div class="card" style="max-width:420px; margin:0 auto;">
        <div class="card-header"><h2>🔑 Change Password</h2></div>
        {% if error %}
        <div class="badge badge-amber" style="display:block; padding:10px 14px; margin-bottom:14px; font-size:13px;">{{ error }}</div>
        {% endif %}
        {% if ok %}
        <div class="badge badge-green" style="display:block; padding:10px 14px; margin-bottom:14px; font-size:13px;">Password changed successfully.</div>
        {% endif %}
        <form method="POST">
            <label>Current Password</label>
            <input type="password" name="current_password" required autofocus>
            <label>New Password</label>
            <input type="password" name="new_password" required>
            <label>Confirm New Password</label>
            <input type="password" name="confirm_password" required>
            <button type="submit" class="btn btn-block" style="margin-top:16px;">Change Password</button>
        </form>
        <a href="{{ '/superadmin' if session.get('role') == 'Super Admin' else '/' }}" style="display:block; text-align:center; margin-top:14px; color:var(--text-muted); font-size:12.5px; text-decoration:none;">← Back</a>
    </div>
    <footer>{{ factory_display_name }} &middot; {{ platform_name }}</footer>
</div>
</body>
</html>
"""

@app.route('/change_password', methods=['GET', 'POST'])
def change_password():
    if not session.get('logged_in'): return redirect('/login')
    error = None
    ok = False
    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT password_hash FROM users WHERE id = %s', (session.get('user_id'),))
        row = cursor.fetchone()
        if not row or not check_password_hash(row[0], current_password):
            error = 'Current password is incorrect.'
        elif len(new_password) < 6:
            error = 'New password must be at least 6 characters.'
        elif new_password != confirm_password:
            error = 'New password and confirmation do not match.'
        elif new_password == current_password:
            error = 'New password must be different from the current password.'
        else:
            cursor.execute('UPDATE users SET password_hash = %s, updated_at = %s WHERE id = %s',
                           (generate_password_hash(new_password), now_ist().isoformat(), session.get('user_id')))
            conn.commit()
            ok = True
        conn.close()
    return render_template_string(CHANGE_PASSWORD_HTML, error=error, ok=ok)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
