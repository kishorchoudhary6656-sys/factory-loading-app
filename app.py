from flask import Flask, render_template_string, request, redirect, session, Response, jsonify
import os, csv, io, re, json, secrets, math, base64
import requests
import psycopg2
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    raise RuntimeError(
        'SECRET_KEY environment variable is not set. Add it in Render → Environment before starting the app.'
    )
app.secret_key = SECRET_KEY

# --- M1: production security hardening ---
# SESSION_COOKIE_SECURE defaults to True (cookie only sent over HTTPS, as Render terminates TLS in
# front of the app). Local HTTP development can explicitly opt out via an env var so `curl`/local
# testing over plain http:// still works — production is safe by default without needing any env
# var to be set correctly on Render.
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', 'True').strip().lower() != 'false'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

@app.after_request
def set_security_headers(response):
    """M1: basic, safe security headers on every response. Purely additive — never changes response
    status/body/existing headers, only adds these if not already present."""
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    return response

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
    # D1: nullable, backward-compatible — existing rows are untouched (stay NULL), only new actions
    # taken during an active Super Admin temporary-access grant populate this.
    cursor.execute('ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS via_temp_access_grant_id INTEGER')

    # E1: per-user permission overrides — Tier 2 of the role permission system. Empty by default
    # (zero impact on any existing user); a row here only exists when a Factory Admin has
    # explicitly customized a specific user's access for one module+action beyond their role's
    # default. Absence of a row means "use the role-default matrix" (see ROLE_PERMISSIONS_DEFAULT).
    cursor.execute('''CREATE TABLE IF NOT EXISTS user_permission_overrides (
        id SERIAL PRIMARY KEY,
        user_id INTEGER, factory_id INTEGER,
        module TEXT, action TEXT, allowed BOOLEAN,
        UNIQUE(user_id, module, action)
    )''')

    # --- N1: Driver Unloading Follow-up (Exotel robo-call) state machine. Additive, DB-persisted
    # (never in-memory) so Render restarts/redeploys never lose or duplicate a pending follow-up.
    # UNIQUE(trip_id) is the idempotency guarantee — evaluate_dc_automation() may run many times per
    # trip, but only the FIRST "In DC" transition can ever create this row. ---
    cursor.execute('''CREATE TABLE IF NOT EXISTS driver_followups (
        id SERIAL PRIMARY KEY,
        trip_id INTEGER UNIQUE, factory_id INTEGER,
        status TEXT DEFAULT 'CALL_1_DUE',
        dc_arrived_at TEXT,
        first_call_due_at TEXT, first_call_at TEXT, first_call_status TEXT,
        first_call_sid TEXT, first_call_duration INTEGER, first_call_response TEXT,
        second_call_due_at TEXT, second_call_at TEXT, second_call_status TEXT,
        second_call_sid TEXT, second_call_duration INTEGER, second_call_response TEXT,
        response_text TEXT,
        whatsapp_escalated_at TEXT,
        next_action_at TEXT,
        last_error TEXT,
        created_at TEXT, updated_at TEXT
    )''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_driver_followups_factory ON driver_followups(factory_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_driver_followups_trip ON driver_followups(trip_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_driver_followups_status_next ON driver_followups(status, next_action_at)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_driver_followups_factory_status_next ON driver_followups(factory_id, status, next_action_at)')

    # --- Super Admin temporary, logged, time-boxed access to a single factory's data ---
    cursor.execute('''CREATE TABLE IF NOT EXISTS temp_access_grants (
        id SERIAL PRIMARY KEY,
        super_admin_user_id INTEGER, super_admin_username TEXT,
        factory_id INTEGER, reason TEXT, module TEXT DEFAULT 'All',
        granted_at TEXT, duration_minutes INTEGER, expires_at TEXT,
        revoked_at TEXT, is_active BOOLEAN DEFAULT TRUE
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
    # F1: tracking source (GPS Device vs Driver Mobile) — additive, default preserves 100% of current
    # behavior (every existing vehicle keeps using driver-mobile-based tracking, unchanged).
    cursor.execute("ALTER TABLE vehicle_master ADD COLUMN IF NOT EXISTS tracking_mode TEXT DEFAULT 'Driver Mobile'")
    cursor.execute("UPDATE vehicle_master SET tracking_mode = 'Driver Mobile' WHERE tracking_mode IS NULL")
    cursor.execute('ALTER TABLE vehicle_master ADD COLUMN IF NOT EXISTS gps_device_id TEXT')
    # Vehicle Type / Ownership — additive, existing vehicles safely default to 'Company Vehicle'
    # (never silently reinterpreted as 'Outside Vehicle').
    cursor.execute("ALTER TABLE vehicle_master ADD COLUMN IF NOT EXISTS vehicle_type TEXT DEFAULT 'Company Vehicle'")
    cursor.execute("UPDATE vehicle_master SET vehicle_type = 'Company Vehicle' WHERE vehicle_type IS NULL")
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
    # G1: GPS jump/anomaly detection — additive only. Existing rows default to is_suspicious=FALSE
    # (never retroactively flagged), anomaly_reason/calculated_speed_kmph stay NULL for them.
    cursor.execute('ALTER TABLE location_history ADD COLUMN IF NOT EXISTS is_suspicious BOOLEAN DEFAULT FALSE')
    cursor.execute('ALTER TABLE location_history ADD COLUMN IF NOT EXISTS anomaly_reason TEXT')
    cursor.execute('ALTER TABLE location_history ADD COLUMN IF NOT EXISTS calculated_speed_kmph DOUBLE PRECISION')

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
        # Preserves the exact old login username ('admin') so the existing team's workflow doesn't change at all.
        # The password itself now comes from an environment variable — no hardcoded default in source.
        initial_admin_password = os.environ.get('INITIAL_ADMIN_PASSWORD')
        if not initial_admin_password:
            raise RuntimeError(
                'INITIAL_ADMIN_PASSWORD environment variable is not set. Add it in Render → Environment '
                'before starting the app on a fresh database (this only applies to first-time setup).'
            )
        cursor.execute('''INSERT INTO users (factory_id, name, username, password_hash, role, status, created_at, updated_at)
                           VALUES (%s, %s, %s, %s, %s, 'Active', %s, %s)''',
                       (default_factory_id, 'Admin', 'admin', generate_password_hash(initial_admin_password), 'Factory Admin', ts, ts))

    # Bootstrap a single platform-level Super Admin account (factory_id = NULL) the very first time this
    # migration runs, so there's always a way into the /superadmin panel. Change this password after first login.
    cursor.execute("SELECT id FROM users WHERE role = 'Super Admin' LIMIT 1")
    if not cursor.fetchone():
        ts = now_ist().isoformat()
        initial_superadmin_password = os.environ.get('INITIAL_SUPERADMIN_PASSWORD')
        if not initial_superadmin_password:
            raise RuntimeError(
                'INITIAL_SUPERADMIN_PASSWORD environment variable is not set. Add it in Render → Environment '
                'before starting the app on a fresh database (this only applies to first-time setup).'
            )
        cursor.execute('''INSERT INTO users (factory_id, name, username, password_hash, role, status, created_at, updated_at)
                           VALUES (NULL, %s, %s, %s, 'Super Admin', 'Active', %s, %s)''',
                       ('Platform Owner', 'superadmin', generate_password_hash(initial_superadmin_password), ts, ts))

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

    # --- DC Arrival & Return Automation (new, additive only): destination geofence + timestamps ---
    cursor.execute('ALTER TABLE trips ADD COLUMN IF NOT EXISTS dc_latitude DOUBLE PRECISION')
    cursor.execute('ALTER TABLE trips ADD COLUMN IF NOT EXISTS dc_longitude DOUBLE PRECISION')
    cursor.execute('ALTER TABLE trips ADD COLUMN IF NOT EXISTS dc_location_name TEXT')
    cursor.execute('ALTER TABLE trips ADD COLUMN IF NOT EXISTS dc_window_start_at TEXT')
    cursor.execute('ALTER TABLE trips ADD COLUMN IF NOT EXISTS dc_arrived_at TEXT')
    cursor.execute('ALTER TABLE trips ADD COLUMN IF NOT EXISTS dc_returned_at TEXT')
    # F1: tracks whether the WhatsApp tracking-link share was ever completed for this trip
    # (manual-click share flow — see WHATSAPP_INTEGRATION note near the tracking routes).
    cursor.execute('ALTER TABLE trips ADD COLUMN IF NOT EXISTS whatsapp_link_status TEXT')

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
    cursor.execute('ALTER TABLE po_items ADD COLUMN IF NOT EXISTS rate DOUBLE PRECISION')
    cursor.execute("ALTER TABLE po_items ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'Approved'")
    cursor.execute("UPDATE po_items SET status = 'Approved' WHERE status IS NULL")
    cursor.execute('ALTER TABLE po_items ADD COLUMN IF NOT EXISTS po_date TEXT')
    cursor.execute('ALTER TABLE po_items ADD COLUMN IF NOT EXISTS delivery_date TEXT')
    cursor.execute('ALTER TABLE po_items ADD COLUMN IF NOT EXISTS tax_percent DOUBLE PRECISION')
    cursor.execute('ALTER TABLE po_items ADD COLUMN IF NOT EXISTS approved_by TEXT')
    cursor.execute('ALTER TABLE po_items ADD COLUMN IF NOT EXISTS approved_at TEXT')
    cursor.execute('ALTER TABLE companies ADD COLUMN IF NOT EXISTS sub_brands TEXT')

    # --- Barcode Catalog: company/sub-brand/PIN code/packing size, per the master workflow spec ---
    cursor.execute('ALTER TABLE barcode_catalog ADD COLUMN IF NOT EXISTS company TEXT')
    cursor.execute('ALTER TABLE barcode_catalog ADD COLUMN IF NOT EXISTS sub_brand TEXT')
    cursor.execute('ALTER TABLE barcode_catalog ADD COLUMN IF NOT EXISTS pin_code TEXT')
    cursor.execute('ALTER TABLE barcode_catalog ADD COLUMN IF NOT EXISTS packing_size TEXT')

    # --- QC availability + pending-inspection workflow ---
    cursor.execute("ALTER TABLE quality_checkers ADD COLUMN IF NOT EXISTS availability TEXT DEFAULT 'Available'")
    cursor.execute("UPDATE quality_checkers SET availability = 'Available' WHERE availability IS NULL")
    cursor.execute("ALTER TABLE daily_production ADD COLUMN IF NOT EXISTS qc_status TEXT DEFAULT 'Approved'")
    cursor.execute("UPDATE daily_production SET qc_status = 'Approved' WHERE qc_status IS NULL")

    # --- ERP Approved Rate Master: one approved rate per (factory, company, product) ---
    cursor.execute('''CREATE TABLE IF NOT EXISTS rate_master (
        id SERIAL PRIMARY KEY,
        factory_id INTEGER,
        company TEXT, product_name TEXT,
        approved_rate DOUBLE PRECISION,
        updated_at TEXT, updated_by TEXT
    )''')
    cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_rate_master_factory_company_product ON rate_master(factory_id, company, product_name)')

    # --- AI-based PDF/Photo import staging: extracted data always lands here for human review first ---
    cursor.execute('''CREATE TABLE IF NOT EXISTS ai_import_staging (
        id SERIAL PRIMARY KEY,
        factory_id INTEGER,
        import_type TEXT, company TEXT, source_filename TEXT,
        extracted_json TEXT, status TEXT DEFAULT 'pending_review',
        error_message TEXT,
        created_at TEXT, created_by TEXT
    )''')

    # --- GRN (Goods Receipt Note) log: tracks partial deliveries against a PO, linked back to it ---
    cursor.execute('''CREATE TABLE IF NOT EXISTS grn_log (
        id SERIAL PRIMARY KEY,
        factory_id INTEGER,
        company TEXT, po_number TEXT, item_name TEXT,
        grn_number TEXT, grn_date TEXT,
        received_qty INTEGER,
        created_at TEXT, created_by TEXT
    )''')
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

    # --- L1: safe, additive indexes for company-scoped/high-frequency queries. CREATE INDEX IF NOT
    # EXISTS never fails due to existing data (unlike a UNIQUE constraint), so these are unconditional. ---
    for idx_sql in [
        'CREATE INDEX IF NOT EXISTS idx_po_items_factory ON po_items(factory_id)',
        'CREATE INDEX IF NOT EXISTS idx_grn_log_factory ON grn_log(factory_id)',
        'CREATE INDEX IF NOT EXISTS idx_daily_production_factory ON daily_production(factory_id)',
        'CREATE INDEX IF NOT EXISTS idx_dispatch_log_factory ON dispatch_log(factory_id)',
        'CREATE INDEX IF NOT EXISTS idx_barcode_catalog_factory ON barcode_catalog(factory_id)',
        'CREATE INDEX IF NOT EXISTS idx_vehicle_master_factory ON vehicle_master(factory_id)',
        'CREATE INDEX IF NOT EXISTS idx_trips_factory ON trips(factory_id)',
        'CREATE INDEX IF NOT EXISTS idx_trips_tracking_token ON trips(tracking_token)',
        'CREATE INDEX IF NOT EXISTS idx_location_history_trip ON location_history(trip_id)',
        'CREATE INDEX IF NOT EXISTS idx_location_history_vehicle ON location_history(vehicle_id)',
        'CREATE INDEX IF NOT EXISTS idx_audit_log_factory ON audit_log(factory_id)',
        'CREATE INDEX IF NOT EXISTS idx_user_permission_overrides_user ON user_permission_overrides(user_id)',
        'CREATE INDEX IF NOT EXISTS idx_temp_access_grants_factory_active ON temp_access_grants(factory_id, is_active)',
        'CREATE INDEX IF NOT EXISTS idx_users_factory ON users(factory_id)',
        'CREATE INDEX IF NOT EXISTS idx_companies_factory ON companies(factory_id)',
    ]:
        cursor.execute(idx_sql)

    # --- L1: company-scoped vehicle-number uniqueness — ALREADY ENFORCED by a pre-existing
    # constraint from an earlier phase (uq_vehicle_factory_number, see line ~269 above), discovered
    # during L1 testing. No new constraint needed here — adding a second, differently-named unique
    # index on the exact same (factory_id, vehicle_number) pair would be redundant. Kept only the
    # read-only diagnostic helper below (check_duplicate_vehicle_numbers) since it's still useful on
    # its own for an operator to inspect data, independent of which constraint enforces uniqueness.
    cursor.execute('''SELECT factory_id, vehicle_number, COUNT(*) FROM vehicle_master
                       GROUP BY factory_id, vehicle_number HAVING COUNT(*) > 1''')
    dup_rows = cursor.fetchall()

    conn.commit()
    conn.close()
init_db()

def check_duplicate_vehicle_numbers():
    """L1 helper — read-only diagnostic. Returns a list of {factory_id, vehicle_number, count} for
    any company that currently has more than one vehicle_master row with the same vehicle_number.
    Never modifies data. Used by the L1 test suite and can be called manually to check production
    data before deciding whether the uq_vehicle_master_factory_number constraint is safe to rely on."""
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('''SELECT factory_id, vehicle_number, COUNT(*) as cnt FROM vehicle_master
                       GROUP BY factory_id, vehicle_number HAVING COUNT(*) > 1''')
    rows = [{'factory_id': r[0], 'vehicle_number': r[1], 'count': r[2]} for r in cursor.fetchall()]
    conn.close()
    return rows

def current_factory_id():
    fid = session.get('factory_id')
    if fid is not None:
        return fid
    # Only reachable for a Super Admin session (whose own factory_id is NULL by design — no
    # default access to any tenant's data). Check for an active, unexpired temporary access
    # grant they've explicitly opened; if none, they see nothing, exactly as before this feature.
    grant_id = session.get('temp_access_grant_id')
    if not grant_id:
        return None
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT factory_id, expires_at, is_active FROM temp_access_grants WHERE id = %s", (grant_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        session.pop('temp_access_grant_id', None)
        return None
    grant_factory_id, expires_at, is_active = row
    if not is_active:
        session.pop('temp_access_grant_id', None)
        return None
    try:
        if now_ist() > datetime.fromisoformat(expires_at):
            session.pop('temp_access_grant_id', None)
            return None
    except Exception:
        session.pop('temp_access_grant_id', None)
        return None
    return grant_factory_id

def log_audit(cursor, action, module, record_id='', details=''):
    """Records an entry in the audit trail. Call this right before conn.commit() in the route
    that performs the action, using the SAME cursor/connection so it's part of the same transaction."""
    cursor.execute('INSERT INTO audit_log (factory_id, user_id, user_name, action, module, record_id, details, timestamp, via_temp_access_grant_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                   (current_factory_id(), session.get('user_id'), session.get('user_name'), action, module, str(record_id), details, now_ist().isoformat(), session.get('temp_access_grant_id')))

def log_audit_system(cursor, factory_id, action, module, record_id='', details=''):
    """Same as log_audit(), but for contexts with no reliable Flask session-derived factory/user
    (e.g. N1's scheduler processing a cron-triggered request, or its Exotel callback — neither has a
    logged-in user). factory_id is passed explicitly (from the driver_followups/trip row itself, not
    guessed), user_id/user_name are NULL (system-triggered, not a person). Mirrors the same pattern
    already used for Login/Failed Login/Logout, which have the identical problem."""
    cursor.execute('INSERT INTO audit_log (factory_id, user_id, user_name, action, module, record_id, details, timestamp, via_temp_access_grant_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULL)',
                   (factory_id, None, None, action, module, str(record_id), details, now_ist().isoformat()))

def get_rate_status(cursor, fid, company, item_name, po_rate):
    """Compares a PO item's rate against the ERP Approved Rate Master for the same
    (factory, company, product). Returns a dict the templates can render directly."""
    cursor.execute('SELECT approved_rate FROM rate_master WHERE factory_id = %s AND company = %s AND product_name = %s',
                   (fid, company or '', item_name or ''))
    row = cursor.fetchone()
    approved_rate = row[0] if row else None
    if approved_rate is None:
        return {'status': 'none', 'label': 'No Approved Rate', 'approved_rate': None, 'diff': None, 'diff_pct': None}
    if po_rate is None:
        return {'status': 'unknown', 'label': 'PO Rate Missing', 'approved_rate': approved_rate, 'diff': None, 'diff_pct': None}
    diff = round(po_rate - approved_rate, 2)
    diff_pct = round((diff / approved_rate) * 100, 1) if approved_rate else 0
    if abs(diff) < 0.005:
        return {'status': 'match', 'label': 'Rate Matched', 'approved_rate': approved_rate, 'diff': 0, 'diff_pct': 0}
    return {'status': 'diff', 'label': 'Rate Difference', 'approved_rate': approved_rate, 'diff': diff, 'diff_pct': diff_pct}

def get_po_shortage_map(cursor, fid, company=None, po_number=None):
    """Computes ordered/received/pending quantity per (company, po_number, item_name), by summing
    po_items.ordered_qty and grn_log.received_qty. Returns {(company, po_number, item_name): dict}.
    Optionally scoped to a single company/po_number for efficiency."""
    where = 'factory_id = %s'
    params = [fid]
    if company:
        where += ' AND company = %s'
        params.append(company)
    if po_number:
        where += ' AND po_number = %s'
        params.append(po_number)
    cursor.execute(f'SELECT company, po_number, item_name, SUM(ordered_qty) FROM po_items WHERE {where} GROUP BY company, po_number, item_name', params)
    ordered_map = {(r[0] or '', r[1], r[2]): (r[3] or 0) for r in cursor.fetchall()}
    cursor.execute(f'SELECT company, po_number, item_name, SUM(received_qty) FROM grn_log WHERE {where} GROUP BY company, po_number, item_name', params)
    received_map = {(r[0] or '', r[1], r[2]): (r[3] or 0) for r in cursor.fetchall()}
    result = {}
    for key, ordered in ordered_map.items():
        received = received_map.get(key, 0)
        pending = max(ordered - received, 0)
        result[key] = {'ordered': ordered, 'received': received, 'pending': pending, 'fulfilled': pending <= 0}
    return result

def check_grn_capacity(cursor, fid, company, po_number, item_name, new_qty):
    """I1: server-side, race-condition-safe over-receiving protection. MUST be called on the same
    cursor/connection as the subsequent GRN INSERT, inside the same transaction (i.e. before that
    connection's commit()) — the SELECT ... FOR UPDATE below locks the matching po_items rows until
    that commit, so two concurrent GRN requests against the same PO/item are forced to process
    sequentially rather than both reading a stale 'pending' value and both succeeding.
    Returns (ok, error_message_or_None, pending_before_this_receipt)."""
    cursor.execute('''SELECT COALESCE(SUM(ordered_qty), 0) FROM po_items
                       WHERE factory_id = %s AND company = %s AND po_number = %s AND item_name = %s FOR UPDATE''',
                   (fid, company, po_number, item_name))
    ordered = cursor.fetchone()[0] or 0
    if ordered <= 0:
        return False, f'No matching PO item found for {company} / {po_number} / {item_name} — cannot receive against a PO that does not have this line item.', 0
    cursor.execute('''SELECT COALESCE(SUM(received_qty), 0) FROM grn_log
                       WHERE factory_id = %s AND company = %s AND po_number = %s AND item_name = %s''',
                   (fid, company, po_number, item_name))
    already_received = cursor.fetchone()[0] or 0
    pending = max(ordered - already_received, 0)
    if new_qty > pending:
        return False, f'Cannot receive {new_qty} — only {pending} is pending against PO {po_number} for {item_name} (ordered: {ordered}, already received: {already_received}).', pending
    return True, None, pending

def call_ai_extraction(file_bytes, mimetype, doc_type):
    """Calls the Anthropic API (vision/document understanding) to extract structured PO or GRN
    data from an uploaded PDF/photo. Requires the ANTHROPIC_API_KEY environment variable to be
    set on the server (e.g. in Render's Environment settings) — this is a paid external API call,
    made only when the user explicitly uploads a file here, never automatically.
    Returns (parsed_dict_or_None, error_message_or_None). Never raises — always fails gracefully."""
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return None, 'AI import is not set up yet. Add an ANTHROPIC_API_KEY environment variable in your Render settings to enable this feature.'

    if doc_type == 'po':
        schema_prompt = '''You are extracting structured data from a Purchase Order (PO) document image or PDF.
Return ONLY valid JSON, nothing else — no markdown fences, no explanation. Match this exact schema:
{
  "po_number": string or null,
  "po_date": string in YYYY-MM-DD format or null,
  "delivery_date": string in YYYY-MM-DD format or null,
  "tax_percent": number or null,
  "items": [
    {"item_name": string, "weight": string or null, "ordered_qty": integer, "rate": number or null, "barcode": string or null}
  ]
}
If a field isn't clearly present in the document, use null — never invent or guess data. "items" must include every line item found.'''
    else:
        schema_prompt = '''You are extracting structured data from a GRN (Goods Receipt Note) document image or PDF.
Return ONLY valid JSON, nothing else — no markdown fences, no explanation. Match this exact schema:
{
  "po_number": string or null,
  "grn_number": string or null,
  "grn_date": string in YYYY-MM-DD format or null,
  "items": [
    {"item_name": string, "received_qty": integer}
  ]
}
If a field isn't clearly present in the document, use null — never invent or guess data. "items" must include every line item found.'''

    b64_data = base64.b64encode(file_bytes).decode('ascii')
    if mimetype == 'application/pdf':
        content_block = {"type": "document", "source": {"type": "base64", "media_type": mimetype, "data": b64_data}}
    else:
        content_block = {"type": "image", "source": {"type": "base64", "media_type": mimetype, "data": b64_data}}

    try:
        resp = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
            json={
                'model': 'claude-sonnet-4-6',
                'max_tokens': 2000,
                'messages': [{'role': 'user', 'content': [content_block, {'type': 'text', 'text': schema_prompt}]}],
            },
            timeout=60,
        )
    except requests.RequestException as e:
        return None, f'Could not reach the AI service: {e}'

    if resp.status_code != 200:
        return None, f'AI service returned an error (HTTP {resp.status_code}). Check your ANTHROPIC_API_KEY is valid and has credit.'

    try:
        data = resp.json()
        text = ''.join(block.get('text', '') for block in data.get('content', []) if block.get('type') == 'text')
        text = text.strip()
        if text.startswith('```'):
            text = re.sub(r'^```(json)?', '', text).rstrip('`').strip()
        parsed = json.loads(text)
    except Exception as e:
        return None, f'Could not parse the AI response as JSON: {e}'
    return parsed, None

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

def current_temp_access_module_set():
    """Returns the set of modules the current Super Admin's active temporary-access grant covers
    (e.g. {'Vehicle'}, {'Vehicle','GRN'}, or {'All'}), or None if there is no active/valid grant
    right now. Does NOT modify current_factory_id() at all — this is a separate, additive check
    used only by the module-access gate below. Only meaningful for Super Admin sessions; normal
    factory users never call this."""
    grant_id = session.get('temp_access_grant_id')
    if not grant_id:
        return None
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT module, expires_at, is_active FROM temp_access_grants WHERE id = %s", (grant_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    module_str, expires_at, is_active = row
    if not is_active:
        return None
    try:
        if now_ist() > datetime.fromisoformat(expires_at):
            return None
    except Exception:
        return None
    if not module_str:
        return None
    return {m.strip() for m in module_str.split(',') if m.strip()}

@app.before_request
def enforce_viewer_readonly():
    """Backend-enforced permission: a Viewer-role user can look at every page but can never
    submit a form or call a mutating endpoint. This is enforced here (not just hidden in the UI)
    so it can't be bypassed by calling the URL directly."""
    if request.method == 'POST' and session.get('role') == 'Viewer' and request.endpoint not in ('login', 'logout', 'register'):
        return "Access denied — your account is view-only. Contact your Factory Admin for edit access.", 403

# Endpoints that legitimately receive POST requests without any ERP login/session — CSRF's
# synchronizer-token pattern doesn't apply to them (no session cookie exists to protect).
# track_ping: public driver-facing GPS endpoint, secured instead by its own unguessable token (handled separately).
CSRF_EXEMPT_ENDPOINTS = {'track_ping', 'exotel_callback', 'n1_process_due_endpoint'}

@app.before_request
def enforce_csrf_protection():
    """Backend-enforced CSRF check for every state-changing (POST) request. Compares the token
    embedded in the submitted form/body against the one stored server-side in the signed session
    cookie. Never logs the token value itself. Rejects with a clean 403 (never a 500) on any
    mismatch or absence — e.g. an expired page, a forged cross-site request, or a stale tab."""
    if request.method != 'POST' or request.endpoint in CSRF_EXEMPT_ENDPOINTS:
        return
    session_token = session.get('csrf_token')
    submitted_token = request.form.get('csrf_token', '')
    if not session_token or not submitted_token or not secrets.compare_digest(session_token, submitted_token):
        return render_template_string(CSRF_ERROR_HTML), 403

# --- D1: module-level temporary access map (Super Admin only — never applies to normal factory users) ---
ROUTE_MODULE_MAP = {
    # PO
    'pos_page': 'PO', 'pos_add': 'PO', 'pos_edit_item': 'PO', 'pos_delete': 'PO', 'pos_delete_po': 'PO',
    'pos_approve_po': 'PO', 'pos_reopen_po': 'PO', 'pos_import_csv': 'PO',
    'pos_ai_import': 'PO', 'pos_ai_import_review': 'PO', 'pos_ai_import_commit': 'PO', 'pos_ai_import_discard': 'PO',
    # GRN
    'grn_page': 'GRN', 'grn_add': 'GRN', 'grn_delete': 'GRN', 'grn_import_csv': 'GRN',
    'grn_ai_import': 'GRN', 'grn_ai_import_review': 'GRN', 'grn_ai_import_commit': 'GRN', 'grn_ai_import_discard': 'GRN',
    # Production (the shared /production page and its barcode-lookup helper also serve the QC and
    # Barcode tabs, so either of those grants is enough to view it — see 'production_page' below)
    'production_add': 'Production', 'production_edit': 'Production', 'production_delete': 'Production',
    'production_page': ('Production', 'QC', 'Barcode'),
    'production_lookup_barcode': ('Production', 'Barcode'),
    # QC
    'production_qc_add': 'QC', 'production_qc_inspect': 'QC', 'production_qc_availability': 'QC', 'production_qc_delete': 'QC',
    # Barcode
    'barcode_catalog_add': 'Barcode', 'barcode_catalog_delete': 'Barcode', 'barcode_catalog_import': 'Barcode',
    # Vehicle (vehicle master, loading, trip lifecycle incl. dispatch/deliver status + tracking controls)
    'vehicles_page': 'Vehicle', 'vehicles_add': 'Vehicle', 'vehicles_start_loading': 'Vehicle', 'vehicle_master_detail': 'Vehicle',
    'vehicles_edit': 'Vehicle', 'vehicles_delete': 'Vehicle',
    'trip_detail': 'Vehicle', 'trip_add_po': 'Vehicle', 'trip_remove_po': 'Vehicle', 'trip_po_status': 'Vehicle',
    'trip_set_active_po': 'Vehicle', 'trip_regen_token': 'Vehicle', 'trip_stop_tracking': 'Vehicle', 'trip_resend_link': 'Vehicle',
    'trip_dispatch': 'Vehicle', 'trip_deliver': 'Vehicle', 'n1_call_now': 'Vehicle',
    # Rate Master
    'rate_master_page': 'Rate Master', 'rate_master_add': 'Rate Master', 'rate_master_delete': 'Rate Master',
    # Users
    'users_page': 'Users', 'users_add': 'Users', 'users_toggle': 'Users', 'user_permissions_page': 'Users',
    # Dashboard/Reports
    'home': 'Dashboard/Reports',
    # Audit
    'audit_page': 'Audit',
    # Companies
    'companies_page': 'Companies', 'companies_add': 'Companies', 'companies_edit_subbrands': 'Companies',
    # Dispatch (floor scan/loading workflow, distinct from Vehicle's trip dispatch/deliver status)
    'start_session': 'Dispatch', 'lookup_barcode': 'Dispatch', 'process_scan': 'Dispatch',
    'dispatch_edit': 'Dispatch', 'dispatch_delete': 'Dispatch', 'history_page': 'Dispatch',
    'export_csv': 'Dispatch', 'export_progress_csv': 'Dispatch',
}
ALL_MODULE_NAMES = ['PO', 'GRN', 'Production', 'QC', 'Barcode', 'Vehicle', 'Rate Master', 'Users',
                     'Dashboard/Reports', 'Audit', 'Companies', 'Dispatch']
# Account-level/public endpoints this gate never touches at all (same spirit as the CSRF exemptions above).
MODULE_GATE_EXEMPT_ENDPOINTS = {'login', 'register', 'logout', 'change_password', 'track_page', 'track_ping', 'exotel_callback', 'n1_process_due_endpoint'}

@app.before_request
def enforce_module_access():
    """Backend-enforced module scoping for a Super Admin's temporary-access grant. Only ever runs
    for Super Admin sessions — for every normal factory user (Factory Admin/Manager/Viewer) this
    returns immediately on the very first line, so their existing permissions/workflows are
    completely untouched. Applies to BOTH GET and POST (not just POST like the CSRF check), so a
    Super Admin can't bypass module scoping just by typing a URL directly. Any tenant-data route
    that isn't explicitly mapped below is denied by default (fail-closed), not allowed by default."""
    if not is_super_admin():
        return
    endpoint = request.endpoint
    if endpoint is None or endpoint in MODULE_GATE_EXEMPT_ENDPOINTS or endpoint.startswith('superadmin'):
        return
    required = ROUTE_MODULE_MAP.get(endpoint)
    if required is None:
        return render_template_string(MODULE_ACCESS_DENIED_HTML, required=endpoint), 403
    required_set = {required} if isinstance(required, str) else set(required)
    allowed = current_temp_access_module_set()
    if allowed is None or ('All' not in allowed and not (required_set & allowed)):
        return render_template_string(MODULE_ACCESS_DENIED_HTML, required=' / '.join(sorted(required_set))), 403

# ===========================================================================
# E1: ROLE / ACTION PERMISSION SYSTEM
# A separate, additive layer on top of D1's module gate and C1's CSRF check — neither of those is
# modified. This layer answers "can this ROLE perform this ACTION on this MODULE", where D1 already
# answers "can this Super Admin's temp grant see this MODULE at all" and CSRF answers "is this a
# genuine same-session POST". All three are independent checks that must each pass.
# ===========================================================================

# Same route-name keys as D1's ROUTE_MODULE_MAP (that dict is untouched) — this is a PARALLEL
# dimension (action instead of module) for the exact same set of tenant routes.
ROUTE_ACTION_MAP = {
    # PO
    'pos_page': 'view', 'pos_add': 'create', 'pos_edit_item': 'edit', 'pos_delete': 'delete', 'pos_delete_po': 'delete',
    'pos_approve_po': 'approve', 'pos_reopen_po': 'manage', 'pos_import_csv': 'import',
    'pos_ai_import': 'import', 'pos_ai_import_review': 'view', 'pos_ai_import_commit': 'import', 'pos_ai_import_discard': 'delete',
    # GRN
    'grn_page': 'view', 'grn_add': 'create', 'grn_delete': 'delete', 'grn_import_csv': 'import',
    'grn_ai_import': 'import', 'grn_ai_import_review': 'view', 'grn_ai_import_commit': 'import', 'grn_ai_import_discard': 'delete',
    # Production
    'production_add': 'create', 'production_edit': 'edit', 'production_delete': 'delete',
    'production_page': 'view', 'production_lookup_barcode': 'view',
    # QC
    'production_qc_add': 'create', 'production_qc_inspect': 'approve', 'production_qc_availability': 'edit', 'production_qc_delete': 'delete',
    # Barcode
    'barcode_catalog_add': 'create', 'barcode_catalog_delete': 'delete', 'barcode_catalog_import': 'import',
    # Vehicle
    'vehicles_page': 'view', 'vehicles_add': 'create', 'vehicles_start_loading': 'create', 'vehicle_master_detail': 'view',
    'vehicles_edit': 'edit', 'vehicles_delete': 'delete',
    'trip_detail': 'view', 'trip_add_po': 'edit', 'trip_remove_po': 'edit', 'trip_po_status': 'edit', 'trip_set_active_po': 'edit',
    'trip_regen_token': 'manage', 'trip_stop_tracking': 'manage', 'trip_resend_link': 'manage',
    'trip_dispatch': 'manage', 'trip_deliver': 'manage', 'n1_call_now': 'manage',
    # Rate Master
    'rate_master_page': 'view', 'rate_master_add': 'create', 'rate_master_delete': 'delete',
    # Users
    'users_page': 'view', 'users_add': 'create', 'users_toggle': 'manage', 'user_permissions_page': 'manage',
    # Dashboard/Reports
    'home': 'view',
    # Audit
    'audit_page': 'view',
    # Companies
    'companies_page': 'view', 'companies_add': 'create', 'companies_edit_subbrands': 'edit',
    # Dispatch
    'start_session': 'manage', 'lookup_barcode': 'view', 'process_scan': 'create',
    'dispatch_edit': 'edit', 'dispatch_delete': 'delete', 'history_page': 'view',
    'export_csv': 'view', 'export_progress_csv': 'view',
}
ALL_PERMISSION_ACTIONS = ['view', 'create', 'edit', 'delete', 'approve', 'import', 'manage']

# Role-default matrix. These values were derived by auditing the EXACT current behavior of every
# route before E1 existed (Factory Admin = full access everywhere; Manager = identical full access
# EXCEPT Users/Audit, which were already Factory-Admin-only via a pre-existing, unrelated check;
# Viewer = view-only everywhere via the pre-existing enforce_viewer_readonly, PLUS also already
# blocked from Users/Audit). Implementing this changes NOTHING for any existing user by default —
# it only formalizes what was already true into an enforceable, customizable matrix.
ALL_ACTIONS_SET = set(ALL_PERMISSION_ACTIONS)
ROLE_PERMISSIONS_DEFAULT = {
    'Factory Admin': {m: set(ALL_ACTIONS_SET) for m in ALL_MODULE_NAMES},
    'Manager': {m: (set() if m in ('Users', 'Audit') else set(ALL_ACTIONS_SET)) for m in ALL_MODULE_NAMES},
    'Viewer': {m: (set() if m in ('Users', 'Audit') else {'view'}) for m in ALL_MODULE_NAMES},
}
# Endpoints this permission layer never touches — same spirit/set as C1's CSRF and D1's module gate exemptions.
PERMISSION_GATE_EXEMPT_ENDPOINTS = {'login', 'register', 'logout', 'change_password', 'track_page', 'track_ping', 'exotel_callback', 'n1_process_due_endpoint'}

def has_permission(cursor, user_id, role, module, action):
    """Tier 2 (per-user override) takes precedence if a row exists; otherwise falls back to Tier 1
    (the role-default matrix above). Company isolation is NOT this function's job — it only answers
    whether the action is permitted at all; current_factory_id() and factory_id-scoped queries
    (untouched by E1) are what keep users inside their own company's data."""
    cursor.execute('SELECT allowed FROM user_permission_overrides WHERE user_id = %s AND module = %s AND action = %s',
                   (user_id, module, action))
    row = cursor.fetchone()
    if row is not None:
        return bool(row[0])
    return action in ROLE_PERMISSIONS_DEFAULT.get(role, {}).get(module, set())

@app.before_request
def enforce_role_permission():
    """E1: backend-enforced role/action permission check for every normal factory user (Factory
    Admin/Manager/Viewer). Deliberately does NOT apply to Super Admin sessions at all — a Super
    Admin's access is governed entirely by D1's temporary module grant (Option A, confirmed): once
    a module is granted, full operational access within it is available, exactly as D1 already
    behaves today. This keeps E1 a pure addition with no interaction needed inside D1's own logic.
    Applies to both GET and POST. Any tenant route missing from ROUTE_ACTION_MAP is fail-closed
    (denied) for normal users, same fail-closed philosophy as D1's module gate."""
    if not session.get('logged_in') or is_super_admin():
        return
    endpoint = request.endpoint
    if endpoint is None or endpoint in PERMISSION_GATE_EXEMPT_ENDPOINTS or endpoint.startswith('superadmin'):
        return
    module = ROUTE_MODULE_MAP.get(endpoint)
    action = ROUTE_ACTION_MAP.get(endpoint)
    if module is None or action is None:
        return  # not a mapped tenant-data route (e.g. static/misc) — nothing for E1 to check here
    module_set = {module} if isinstance(module, str) else set(module)
    role = session.get('role')
    user_id = session.get('user_id')
    conn = get_conn()
    cursor = conn.cursor()
    allowed = any(has_permission(cursor, user_id, role, m, action) for m in module_set)
    if not allowed:
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            # Permission-denied audit: mutating attempts only (per confirmed scope) — never logs
            # passwords, CSRF tokens, tracking tokens, or any other secret.
            log_audit(cursor, 'Permission Denied', '/'.join(sorted(module_set)), '',
                       f'{role} denied: action={action}, route={endpoint}')
            conn.commit()
        conn.close()
        return render_template_string(PERMISSION_DENIED_HTML, module='/'.join(sorted(module_set)), action=action), 403
    conn.close()

PLATFORM_NAME = 'AI Factory ERP'

@app.context_processor
def inject_factory_branding():
    """Makes factory_display_name / factory_initials / platform_name available in every template
    automatically, without every route needing to pass them explicitly. Falls back to neutral
    platform branding when no one is logged in yet (e.g. the login page itself). During an active
    Super Admin temporary-access grant, this also shows the target factory's own branding plus a
    persistent banner, so it's always visually obvious whose data is on screen."""
    fid = current_factory_id()
    name = PLATFORM_NAME
    logo_url = None
    temp_access_banner = None
    if fid:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT display_name, company_name, logo_url FROM factories WHERE id = %s', (fid,))
        row = cursor.fetchone()
        conn.close()
        if row:
            name = row[0] or row[1] or name
            logo_url = row[2]
    grant_id = session.get('temp_access_grant_id')
    if grant_id and is_super_admin():
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT expires_at, reason FROM temp_access_grants WHERE id = %s AND is_active = TRUE', (grant_id,))
        grow = cursor.fetchone()
        conn.close()
        if grow:
            temp_access_banner = {'factory_name': name, 'expires_at': grow[0][:16] if grow[0] else '', 'reason': grow[1]}
    initials = ''.join(w[0] for w in name.split()[:2]).upper() if name else 'AI'
    # CSRF: ensure every session (even a brand-new anonymous one, e.g. on /login or /register) has a
    # token, and make it available to every template automatically via a single shared context var.
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return dict(factory_display_name=name, factory_initials=initials, factory_logo_url=logo_url,
                platform_name=PLATFORM_NAME, temp_access_banner=temp_access_banner, csrf_token=session['csrf_token'])

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
    .badge-red { background: rgba(239,68,68,0.14); color: #f87171; }

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
<meta name="csrf-token" content="{{ csrf_token }}">
<script>
    // CSRF: token is available globally for any AJAX call (e.g. the dispatch scan page), and is
    // automatically added as a hidden field to every POST form on the page — no per-form edits needed.
    window.CSRF_TOKEN = document.querySelector('meta[name="csrf-token"]').content;
    document.addEventListener('DOMContentLoaded', function() {
        document.querySelectorAll('form').forEach(function(f) {
            if ((f.method || 'get').toLowerCase() === 'post' && !f.querySelector('input[name="csrf_token"]')) {
                var inp = document.createElement('input');
                inp.type = 'hidden';
                inp.name = 'csrf_token';
                inp.value = window.CSRF_TOKEN;
                f.appendChild(inp);
            }
        });
    });
</script>
"""

CSRF_ERROR_HTML = STYLE_BLOCK + """
<title>Security Check Failed</title>
</head>
<body>
<div class="container" style="max-width:480px; margin-top:60px;">
    <div class="card" style="text-align:center;">
        <div style="font-size:40px;">🔒</div>
        <h2 style="margin:12px 0 6px;">Security Check Failed</h2>
        <p style="color:var(--text-muted); font-size:13.5px;">
            This request could not be verified and was rejected for your safety. This can happen if a page
            was left open for a long time, or was reloaded from an old link. Please go back and try again.
        </p>
        <a href="/" class="btn btn-primary" style="margin-top:14px; display:inline-block;">← Back to Dashboard</a>
    </div>
</div>
</body>
</html>
"""

MODULE_ACCESS_DENIED_HTML = STYLE_BLOCK + """
<title>Access Denied</title>
</head>
<body>
<div class="container" style="max-width:480px; margin-top:60px;">
    <div class="card" style="text-align:center;">
        <div style="font-size:40px;">🔒</div>
        <h2 style="margin:12px 0 6px;">Access Denied</h2>
        <p style="color:var(--text-muted); font-size:13.5px;">
            Your temporary access grant for this company doesn't cover this section
            (requires: <strong style="color:var(--text);">{{ required }}</strong>).
            Go to the Temporary Access panel to request broader access if you need it.
        </p>
        <a href="/superadmin/access" class="btn btn-primary" style="margin-top:14px; display:inline-block;">← Temporary Access Panel</a>
    </div>
</div>
</body>
</html>
"""

PERMISSION_DENIED_HTML = STYLE_BLOCK + """
<title>Access Denied</title>
</head>
<body>
<div class="container" style="max-width:480px; margin-top:60px;">
    <div class="card" style="text-align:center;">
        <div style="font-size:40px;">🚫</div>
        <h2 style="margin:12px 0 6px;">Access Denied</h2>
        <p style="color:var(--text-muted); font-size:13.5px;">
            Your account doesn't have permission to <strong style="color:var(--text);">{{ action }}</strong>
            in <strong style="color:var(--text);">{{ module }}</strong>. Contact your Factory Admin if you need this access.
        </p>
        <a href="/" class="btn btn-primary" style="margin-top:14px; display:inline-block;">← Back to Dashboard</a>
    </div>
</div>
</body>
</html>
"""

def nav_html(active):
    def cls(name):
        return "active" if active == name else ""
    base = f"""
    <div class="nav">
        <a href="/" class="{cls('dashboard')}">Dashboard</a>
        <a href="/companies" class="{cls('companies')}">Companies</a>
        <a href="/pos" class="{cls('pos')}">Manage POs</a>
        <a href="/rate_master" class="{cls('rate_master')}">Rate Master</a>
        <a href="/grn" class="{cls('grn')}">GRN</a>
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
{% if temp_access_banner %}
<div style="background:#f59e0b; color:#1a1a1a; padding:9px 16px; font-size:12.5px; font-weight:700; display:flex; justify-content:space-between; align-items:center; border-radius:8px; margin-bottom:16px; flex-wrap:wrap; gap:8px;">
    <span>🔐 TEMPORARY ACCESS ACTIVE — viewing {{ temp_access_banner.factory_name }}'s data (reason: {{ temp_access_banner.reason }}) &middot; expires {{ temp_access_banner.expires_at }}</span>
    <form method="POST" action="/superadmin/access/exit" style="margin:0;"><button type="submit" style="background:#1a1a1a; color:#fff; border:none; padding:5px 12px; border-radius:6px; font-size:11.5px; cursor:pointer; font-weight:700;">Exit Access</button></form>
</div>
{% endif %}
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
                    document.getElementById('scannedItemName').textContent = info.item_name + (info.from_catalog ? ' (from Barcode Catalog)' : '');
                    document.getElementById('scannedItemWeight').textContent = info.weight || 'Not specified';
                    document.getElementById('qtyInput').style.display = 'block';
                    document.getElementById('qtyLabel').style.display = 'block';
                    document.getElementById('confirmLoadBtn').style.display = 'block';
                    document.getElementById('qtyInput').value = 1;
                } else {
                    document.getElementById('scannedItemName').textContent = 'Barcode not found — not on this PO or in the Barcode Catalog';
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
            body: 'barcode=' + encodeURIComponent(lastBarcode) + '&qty=' + qty + '&csrf_token=' + encodeURIComponent(window.CSRF_TOKEN),
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
            <a href="/pos/ai_import" class="btn btn-outline btn-sm">🤖 AI Import from PDF/Photo</a>
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
            Both .csv and .xlsx (Excel) files work. Required columns: <span style="font-family:monospace; color:var(--text);">po_number, item_name, ordered_qty</span> (weight, barcode, rate/price, po_date, delivery_date and tax/gst are all optional — add later if not in the file).<br>
            Common export formats also work automatically — e.g. Zepto's <span style="font-family:monospace; color:var(--text);">PurchaseOrderId, Sku, PO_Qty</span>.<br>
            Example: <span style="font-family:monospace;">PO-2026-014, Instant Noodles, 1kg, 500, 8901234567890</span><br>
            Imported items land in <strong style="color:var(--text);">Draft</strong> — review and click <strong style="color:var(--text);">Approve PO</strong> below before they're treated as final.<br>
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
                <div>
                    <label>PO Rate (₹) — optional</label>
                    <input type="number" step="0.01" name="rate" placeholder="e.g. 45.50">
                </div>
                <div>
                    <label>PO Date — optional</label>
                    <input type="date" name="po_date">
                </div>
                <div>
                    <label>Delivery Date — optional</label>
                    <input type="date" name="delivery_date">
                </div>
                <div>
                    <label>Tax / GST % — optional</label>
                    <input type="number" step="0.01" name="tax_percent" placeholder="e.g. 5">
                </div>
            </div>
            <button type="submit" class="btn" style="margin-top:14px;">Add PO Item (as Draft)</button>
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
                    {% if g.po_date %}<span style="color:var(--text-muted); font-size:12px;">PO: {{ g.po_date }}</span>{% endif %}
                    {% if g.delivery_date %}<span style="color:var(--text-muted); font-size:12px;">Delivery: {{ g.delivery_date }}</span>{% endif %}
                    <span style="color:var(--text-muted); font-size:12.5px;">{{ g.rows|length }} item(s) &middot; {{ g.total_ordered }} total ordered</span>
                    {% if g.has_rate_diff %}<span class="badge badge-red">🔴 Rate Difference in this PO</span>{% endif %}
                    {% if g.has_draft %}<span class="badge badge-amber">🟡 Draft — Pending Approval</span>{% else %}<span class="badge badge-green">🟢 Approved</span>{% endif %}
                    {% if g.fully_received %}<span class="badge badge-green">✅ PO Fulfilled (GRN)</span>{% else %}<span class="badge badge-amber">⏳ GRN Pending</span>{% endif %}
                </div>
                <div style="display:flex; gap:8px; flex-wrap:wrap;">
                    {% if g.has_draft %}
                    <form method="POST" action="/pos/approve_po" onsubmit="return confirm('Approve PO {{ g.po_number }}? This marks it as the Final PO.');">
                        <input type="hidden" name="po_number" value="{{ g.po_number }}">
                        <input type="hidden" name="company" value="{{ g.company }}">
                        <button type="submit" class="btn btn-sm">✅ Approve PO</button>
                    </form>
                    {% else %}
                    <form method="POST" action="/pos/reopen_po" onsubmit="return confirm('Reopen PO {{ g.po_number }} for editing? It will go back to Draft.');">
                        <input type="hidden" name="po_number" value="{{ g.po_number }}">
                        <input type="hidden" name="company" value="{{ g.company }}">
                        <button type="submit" class="btn btn-outline btn-sm">↩ Reopen for Edit</button>
                    </form>
                    {% endif %}
                    <form method="POST" action="/pos/delete_po" onsubmit="return confirm('Delete the ENTIRE PO {{ g.po_number }} ({{ g.rows|length }} items)? This cannot be undone.');">
                        <input type="hidden" name="po_number" value="{{ g.po_number }}">
                        <input type="hidden" name="company" value="{{ g.company }}">
                        <button type="submit" class="btn btn-danger btn-sm">🗑 Delete Entire PO</button>
                    </form>
                </div>
            </div>
            <table>
                <thead><tr>
                    <th>Item</th><th>Weight</th><th>Ordered Qty</th><th>Barcode</th><th>PO Rate</th><th>Rate Check</th><th>Status</th><th>GRN Received / Pending</th><th></th>
                </tr></thead>
                <tbody>
                {% for it in g.rows %}
                <tr id="row-{{ it[0] }}">
                    <td>{{ it[2] }}</td>
                    <td>{{ it[3] }}</td>
                    <td>{{ it[4] }}</td>
                    <td style="color:var(--text-muted); font-family:monospace;">{{ it[5] }}</td>
                    <td>{% if it[7] is not none %}₹{{ "%.2f"|format(it[7]) }}{% else %}—{% endif %}</td>
                    <td>
                        {% set rc = it[14] %}
                        {% if rc.status == 'match' %}<span class="badge badge-green">🟢 Rate Matched</span>
                        {% elif rc.status == 'diff' %}<span class="badge badge-red">🔴 Diff: ₹{{ "%.2f"|format(rc.diff) }} ({{ rc.diff_pct }}%) &middot; Approved ₹{{ "%.2f"|format(rc.approved_rate) }}</span>
                        {% elif rc.status == 'unknown' %}<span class="badge badge-amber">PO Rate Missing</span>
                        {% else %}<span style="color:var(--text-muted); font-size:12px;">No Approved Rate</span>
                        {% endif %}
                    </td>
                    <td>
                        {% if it[8] == 'Draft' %}<span class="badge badge-amber">Draft</span>
                        {% else %}<span class="badge badge-green" title="Approved by {{ it[12] }} at {{ it[13] }}">Approved</span>
                        {% endif %}
                    </td>
                    <td>
                        {% set sh = it[15] %}
                        {{ sh.received }} / {{ sh.ordered }}
                        {% if sh.fulfilled %}<span class="badge badge-green">✅ Fulfilled</span>
                        {% else %}<span class="badge badge-amber">⏳ {{ sh.pending }} pending</span>
                        {% endif %}
                    </td>
                    <td style="display:flex; gap:6px;">
                        {% if it[8] == 'Draft' %}
                        <button type="button" class="btn btn-outline btn-sm" onclick="document.getElementById('edit-{{ it[0] }}').style.display='table-row';">Edit</button>
                        {% endif %}
                        <form method="POST" action="/pos/delete/{{ it[0] }}" onsubmit="return confirm('Delete this item?');">
                            <button type="submit" class="btn btn-danger btn-sm">Delete</button>
                        </form>
                    </td>
                </tr>
                {% if it[8] == 'Draft' %}
                <tr id="edit-{{ it[0] }}" style="display:none; background:rgba(255,255,255,0.03);">
                    <td colspan="9">
                        <form method="POST" action="/pos/edit/{{ it[0] }}" style="display:flex; gap:8px; flex-wrap:wrap; align-items:flex-end; padding:8px 0;">
                            <div><label style="font-size:11px;">Item</label><input type="text" name="item_name" value="{{ it[2] }}" style="margin:0; min-width:140px;" required></div>
                            <div><label style="font-size:11px;">Weight</label><input type="text" name="weight" value="{{ it[3] }}" style="margin:0; min-width:90px;" required></div>
                            <div><label style="font-size:11px;">Qty</label><input type="number" name="ordered_qty" value="{{ it[4] }}" style="margin:0; min-width:80px;" required></div>
                            <div><label style="font-size:11px;">Barcode</label><input type="text" name="barcode" value="{{ it[5] }}" style="margin:0; min-width:120px;" required></div>
                            <div><label style="font-size:11px;">Rate (₹)</label><input type="number" step="0.01" name="rate" value="{{ it[7] if it[7] is not none else '' }}" style="margin:0; min-width:90px;"></div>
                            <button type="submit" class="btn btn-sm">Save</button>
                            <button type="button" class="btn btn-outline btn-sm" onclick="document.getElementById('edit-{{ it[0] }}').style.display='none';">Cancel</button>
                        </form>
                    </td>
                </tr>
                {% endif %}
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

RATE_MASTER_HTML = STYLE_BLOCK + """
<title>Rate Master | {{ factory_display_name }}</title>
</head>
<body>
<div class="container">
""" + TOPBAR_TEMPLATE.replace("__NAV__", nav_html('rate_master')) + """
    {% if error_msg %}
    <div class="badge badge-amber" style="display:block; padding:10px 14px; margin-bottom:14px; font-size:13px;">{{ error_msg }}</div>
    {% endif %}
    <div class="card">
        <div class="card-header"><h2>💰 ERP Approved Rate Master</h2></div>
        <div style="color:var(--text-muted); font-size:12.5px; margin-bottom:14px;">
            Save the approved rate for each company + product here. Every PO item is automatically checked against this rate on the Manage POs page — 🟢 if it matches, 🔴 if it differs.
        </div>
        <form method="POST" action="/rate_master/add">
            <div class="form-grid">
                <div>
                    <label>Company / Client</label>
                    <select name="company" required>
                        <option value="" disabled selected>Select company</option>
                        {% for c in companies %}
                        <option value="{{ c }}" {% if c == filter_company %}selected{% endif %}>{{ c }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div>
                    <label>Product Name (must match item name exactly)</label>
                    <input type="text" name="product_name" placeholder="e.g. Instant Noodles" required>
                </div>
                <div>
                    <label>Approved Rate (₹)</label>
                    <input type="number" step="0.01" name="approved_rate" placeholder="e.g. 45.50" required>
                </div>
            </div>
            <button type="submit" class="btn" style="margin-top:14px;">Save Approved Rate</button>
        </form>
        {% if companies|length == 0 %}
        <div style="color:var(--text-muted); font-size:12.5px; margin-top:10px;">No companies yet — <a href="/companies" style="color:var(--primary);">add one first</a>.</div>
        {% endif %}
    </div>

    <div class="card">
        <div class="card-header"><h2>📋 Approved Rates</h2></div>
        {% if rates|length > 0 %}
        <table>
            <thead><tr><th>Company</th><th>Product</th><th>Approved Rate</th><th>Last Updated</th><th></th></tr></thead>
            <tbody>
            {% for r in rates %}
            <tr>
                <td><span class="badge badge-blue">{{ r[1] }}</span></td>
                <td>{{ r[2] }}</td>
                <td>₹{{ "%.2f"|format(r[3]) }}</td>
                <td style="color:var(--text-muted); font-size:12px;">{{ r[4][:16] if r[4] else '—' }}</td>
                <td>
                    <form method="POST" action="/rate_master/delete/{{ r[0] }}" onsubmit="return confirm('Delete this approved rate?');">
                        <button type="submit" class="btn btn-danger btn-sm">Delete</button>
                    </form>
                </td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <div class="empty-state">No approved rates set yet. Add one above.</div>
        {% endif %}
    </div>

    <footer>{{ factory_display_name }} &middot; {{ platform_name }}</footer>
</div>
</body>
</html>
"""

AI_IMPORT_PO_HTML = STYLE_BLOCK + """
<title>AI Import | {{ factory_display_name }}</title>
</head>
<body>
<div class="container">
""" + TOPBAR_TEMPLATE.replace("__NAV__", nav_html('pos')) + """
    {% if request.args.get('error') %}
    <div class="badge badge-amber" style="display:block; padding:10px 14px; margin-bottom:14px; font-size:13px;">{{ request.args.get('error') }}</div>
    {% endif %}
    <div class="card">
        <div class="card-header"><h2>🤖 AI Import from PDF / Photo — {{ 'Purchase Order' if import_type == 'po' else 'GRN' }}</h2>
            <a href="{{ '/pos' if import_type == 'po' else '/grn' }}" class="btn btn-outline btn-sm">← Back</a>
        </div>
        <div style="color:var(--text-muted); font-size:12.5px; margin-bottom:14px; line-height:1.6;">
            Upload a photo or PDF of a {{ 'Purchase Order' if import_type == 'po' else 'GRN' }} and AI will read it and extract the line items automatically.
            Nothing is saved directly — you'll get a review screen to check and correct everything before it's added as a
            {% if import_type == 'po' %}Draft PO (same Draft → Approve flow as any other import){% else %}GRN receipt{% endif %}.
        </div>
        {% if not api_key_set %}
        <div class="badge badge-amber" style="display:block; padding:12px 14px; font-size:13px; line-height:1.6;">
            ⚠️ AI import is not set up yet. This feature calls the Anthropic API (a paid external service) to read your documents.
            To enable it: create an API key at <span style="font-family:monospace;">console.anthropic.com</span>, then add it as an
            environment variable named <span style="font-family:monospace; color:var(--text);">ANTHROPIC_API_KEY</span> in your Render
            service's Environment settings, and redeploy. Each document you scan will use a small amount of API credit.
        </div>
        {% else %}
        <form method="POST" action="{{ action_url }}" enctype="multipart/form-data">
            <div class="form-grid">
                <div>
                    <label>Company / Client</label>
                    <select name="company" required>
                        <option value="" disabled selected>Select company</option>
                        {% for c in companies %}
                        <option value="{{ c }}">{{ c }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div>
                    <label>Document (PDF, JPG or PNG)</label>
                    <input type="file" name="doc_file" accept=".pdf,.jpg,.jpeg,.png" required style="padding:10px;">
                </div>
            </div>
            <button type="submit" class="btn" style="margin-top:14px;">🤖 Scan &amp; Extract</button>
        </form>
        {% if companies|length == 0 %}
        <div style="color:var(--text-muted); font-size:12.5px; margin-top:10px;">No companies yet — <a href="/companies" style="color:var(--primary);">add one first</a>.</div>
        {% endif %}
        {% endif %}
    </div>
    <footer>{{ factory_display_name }} &middot; {{ platform_name }}</footer>
</div>
</body>
</html>
"""

AI_IMPORT_GRN_HTML = STYLE_BLOCK + """
<title>AI Import | {{ factory_display_name }}</title>
</head>
<body>
<div class="container">
""" + TOPBAR_TEMPLATE.replace("__NAV__", nav_html('grn')) + """
    {% if request.args.get('error') %}
    <div class="badge badge-amber" style="display:block; padding:10px 14px; margin-bottom:14px; font-size:13px;">{{ request.args.get('error') }}</div>
    {% endif %}
    <div class="card">
        <div class="card-header"><h2>🤖 AI Import from PDF / Photo — {{ 'Purchase Order' if import_type == 'po' else 'GRN' }}</h2>
            <a href="{{ '/pos' if import_type == 'po' else '/grn' }}" class="btn btn-outline btn-sm">← Back</a>
        </div>
        <div style="color:var(--text-muted); font-size:12.5px; margin-bottom:14px; line-height:1.6;">
            Upload a photo or PDF of a {{ 'Purchase Order' if import_type == 'po' else 'GRN' }} and AI will read it and extract the line items automatically.
            Nothing is saved directly — you'll get a review screen to check and correct everything before it's added as a
            {% if import_type == 'po' %}Draft PO (same Draft → Approve flow as any other import){% else %}GRN receipt{% endif %}.
        </div>
        {% if not api_key_set %}
        <div class="badge badge-amber" style="display:block; padding:12px 14px; font-size:13px; line-height:1.6;">
            ⚠️ AI import is not set up yet. This feature calls the Anthropic API (a paid external service) to read your documents.
            To enable it: create an API key at <span style="font-family:monospace;">console.anthropic.com</span>, then add it as an
            environment variable named <span style="font-family:monospace; color:var(--text);">ANTHROPIC_API_KEY</span> in your Render
            service's Environment settings, and redeploy. Each document you scan will use a small amount of API credit.
        </div>
        {% else %}
        <form method="POST" action="{{ action_url }}" enctype="multipart/form-data">
            <div class="form-grid">
                <div>
                    <label>Company / Client</label>
                    <select name="company" required>
                        <option value="" disabled selected>Select company</option>
                        {% for c in companies %}
                        <option value="{{ c }}">{{ c }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div>
                    <label>Document (PDF, JPG or PNG)</label>
                    <input type="file" name="doc_file" accept=".pdf,.jpg,.jpeg,.png" required style="padding:10px;">
                </div>
            </div>
            <button type="submit" class="btn" style="margin-top:14px;">🤖 Scan &amp; Extract</button>
        </form>
        {% if companies|length == 0 %}
        <div style="color:var(--text-muted); font-size:12.5px; margin-top:10px;">No companies yet — <a href="/companies" style="color:var(--primary);">add one first</a>.</div>
        {% endif %}
        {% endif %}
    </div>
    <footer>{{ factory_display_name }} &middot; {{ platform_name }}</footer>
</div>
</body>
</html>
"""

AI_IMPORT_REVIEW_PO_HTML = STYLE_BLOCK + """
<title>Review AI Import | {{ factory_display_name }}</title>
</head>
<body>
<div class="container">
""" + TOPBAR_TEMPLATE.replace("__NAV__", nav_html('pos')) + """
    {% if request.args.get('error') %}
    <div class="badge badge-amber" style="display:block; padding:10px 14px; margin-bottom:14px; font-size:13px;">{{ request.args.get('error') }}</div>
    {% endif %}
    <div class="card">
        <div class="card-header"><h2>🔍 Review Extracted PO — {{ filename }}</h2></div>
        <div style="color:var(--text-muted); font-size:12.5px; margin-bottom:14px;">
            AI read this document — check every field carefully, correct anything wrong, uncheck any line that shouldn't be imported, then Confirm.
            This will land in <strong style="color:var(--text);">Draft</strong>, same as any other PO import, so you can review it once more before Approving.
        </div>
        {% if status == 'committed' %}
        <div class="badge badge-green" style="display:block; padding:10px 14px;">✅ Already imported.</div>
        {% else %}
        <form method="POST" action="/pos/ai_import/review/{{ staging_id }}/commit">
            <input type="hidden" name="company" value="{{ company }}">
            <div class="form-grid">
                <div><label>Company</label><input type="text" value="{{ company }}" disabled></div>
                <div><label>PO Number</label><input type="text" name="po_number" value="{{ extracted.po_number or '' }}" required></div>
                <div><label>PO Date</label><input type="date" name="po_date" value="{{ extracted.po_date or '' }}"></div>
                <div><label>Delivery Date</label><input type="date" name="delivery_date" value="{{ extracted.delivery_date or '' }}"></div>
                <div><label>Tax / GST %</label><input type="number" step="0.01" name="tax_percent" value="{{ extracted.tax_percent if extracted.tax_percent is not none else '' }}"></div>
            </div>
            <table style="margin-top:16px;">
                <thead><tr><th></th><th>Item</th><th>Weight</th><th>Qty</th><th>Rate</th><th>Barcode</th></tr></thead>
                <tbody>
                {% for it in extracted['items'] %}
                <tr>
                    <td><input type="checkbox" name="include" value="{{ loop.index0 }}" checked style="width:auto;"></td>
                    <td><input type="text" name="item_name" value="{{ it.item_name or '' }}" style="margin:0; min-width:140px;"></td>
                    <td><input type="text" name="weight" value="{{ it.weight or '' }}" style="margin:0; min-width:80px;"></td>
                    <td><input type="number" name="ordered_qty" value="{{ it.ordered_qty or '' }}" style="margin:0; min-width:80px;"></td>
                    <td><input type="number" step="0.01" name="rate" value="{{ it.rate if it.rate is not none else '' }}" style="margin:0; min-width:80px;"></td>
                    <td><input type="text" name="barcode" value="{{ it.barcode or '' }}" style="margin:0; min-width:120px;"></td>
                </tr>
                {% endfor %}
                {% if extracted['items']|length == 0 %}
                <tr><td colspan="6" style="text-align:center; color:var(--text-muted);">No items were extracted — the document may be unclear. You can still add items manually on the Manage POs page.</td></tr>
                {% endif %}
                </tbody>
            </table>
            <div style="display:flex; gap:8px; margin-top:16px;">
                <button type="submit" class="btn">✅ Confirm &amp; Import as Draft</button>
                <button type="submit" formaction="/pos/ai_import/review/{{ staging_id }}/discard" class="btn btn-danger">Discard</button>
            </div>
        </form>
        {% endif %}
    </div>
    <footer>{{ factory_display_name }} &middot; {{ platform_name }}</footer>
</div>
</body>
</html>
"""

AI_IMPORT_REVIEW_GRN_HTML = STYLE_BLOCK + """
<title>Review AI Import | {{ factory_display_name }}</title>
</head>
<body>
<div class="container">
""" + TOPBAR_TEMPLATE.replace("__NAV__", nav_html('grn')) + """
    {% if request.args.get('error') %}
    <div class="badge badge-amber" style="display:block; padding:10px 14px; margin-bottom:14px; font-size:13px;">{{ request.args.get('error') }}</div>
    {% endif %}
    <div class="card">
        <div class="card-header"><h2>🔍 Review Extracted GRN — {{ filename }}</h2></div>
        <div style="color:var(--text-muted); font-size:12.5px; margin-bottom:14px;">
            AI read this document — check every field, correct anything wrong, uncheck any line that shouldn't be logged, then Confirm.
            This links back to the original PO's shortage tracking exactly like a manual GRN entry.
        </div>
        {% if status == 'committed' %}
        <div class="badge badge-green" style="display:block; padding:10px 14px;">✅ Already imported.</div>
        {% else %}
        <form method="POST" action="/grn/ai_import/review/{{ staging_id }}/commit">
            <input type="hidden" name="company" value="{{ company }}">
            <div class="form-grid">
                <div><label>Company</label><input type="text" value="{{ company }}" disabled></div>
                <div><label>PO Number</label><input type="text" name="po_number" value="{{ extracted.po_number or '' }}" required></div>
                <div><label>GRN Number</label><input type="text" name="grn_number" value="{{ extracted.grn_number or '' }}"></div>
                <div><label>GRN Date</label><input type="date" name="grn_date" value="{{ extracted.grn_date or '' }}"></div>
            </div>
            <table style="margin-top:16px;">
                <thead><tr><th></th><th>Item</th><th>Received Qty</th></tr></thead>
                <tbody>
                {% for it in extracted['items'] %}
                <tr>
                    <td><input type="checkbox" name="include" value="{{ loop.index0 }}" checked style="width:auto;"></td>
                    <td><input type="text" name="item_name" value="{{ it.item_name or '' }}" style="margin:0; min-width:160px;"></td>
                    <td><input type="number" name="received_qty" value="{{ it.received_qty or '' }}" style="margin:0; min-width:100px;"></td>
                </tr>
                {% endfor %}
                {% if extracted['items']|length == 0 %}
                <tr><td colspan="3" style="text-align:center; color:var(--text-muted);">No items were extracted — the document may be unclear. You can still add entries manually on the GRN page.</td></tr>
                {% endif %}
                </tbody>
            </table>
            <div style="display:flex; gap:8px; margin-top:16px;">
                <button type="submit" class="btn">✅ Confirm &amp; Log GRN</button>
                <button type="submit" formaction="/grn/ai_import/review/{{ staging_id }}/discard" class="btn btn-danger">Discard</button>
            </div>
        </form>
        {% endif %}
    </div>
    <footer>{{ factory_display_name }} &middot; {{ platform_name }}</footer>
</div>
</body>
</html>
"""

GRN_HTML = STYLE_BLOCK + """
<title>GRN | {{ factory_display_name }}</title>
</head>
<body>
<div class="container">
""" + TOPBAR_TEMPLATE.replace("__NAV__", nav_html('grn')) + """
    {% if msg %}
    <div class="badge {% if msg_ok %}badge-green{% else %}badge-amber{% endif %}" style="display:block; padding:10px 14px; margin-bottom:14px; font-size:13px;">{{ msg }}</div>
    {% endif %}

    <div class="card">
        <div class="card-header"><h2>📦 GRN Shortage Tracking — by PO</h2></div>
        <div style="color:var(--text-muted); font-size:12.5px; margin-bottom:14px;">
            For every PO item, this shows what's been received so far via GRN entries vs. what's still pending. Log each delivery below as it comes in.
        </div>
        {% if shortage_rows|length > 0 %}
        <table>
            <thead><tr><th>Company</th><th>PO Number</th><th>Item</th><th>Ordered</th><th>Received</th><th>Pending</th><th>Status</th></tr></thead>
            <tbody>
            {% for r in shortage_rows %}
            <tr>
                <td><span class="badge badge-blue">{{ r.company }}</span></td>
                <td>{{ r.po_number }}</td>
                <td>{{ r.item_name }}</td>
                <td>{{ r.ordered }}</td>
                <td>{{ r.received }}</td>
                <td>{{ r.pending }}</td>
                <td>
                    {% if r.fulfilled %}<span class="badge badge-green">✅ PO Fulfilled</span>
                    {% else %}<span class="badge badge-amber">⏳ Pending: {{ r.pending }}</span>
                    {% endif %}
                </td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <div class="empty-state">No PO items yet — add POs first, then log GRN receipts against them here.</div>
        {% endif %}
    </div>

    <div class="card">
        <div class="card-header"><h2>📤 Bulk Import GRN from CSV / Excel</h2><a href="/grn/ai_import" class="btn btn-outline btn-sm">🤖 AI Import from PDF/Photo</a></div>
        <form method="POST" action="/grn/import_csv" enctype="multipart/form-data">
            <label>Company / Client</label>
            <select name="company" required>
                <option value="" disabled selected>Select company</option>
                {% for c in companies %}
                <option value="{{ c }}">{{ c }}</option>
                {% endfor %}
            </select>
            <label>Choose CSV or Excel File</label>
            <input type="file" name="grn_file" accept=".csv,.xlsx" required style="padding:10px;">
            <button type="submit" class="btn" style="margin-top:10px;">Upload &amp; Import</button>
        </form>
        <div style="color:var(--text-muted); font-size:12.5px; margin-top:12px; line-height:1.6;">
            Required columns: <span style="font-family:monospace; color:var(--text);">po_number, item_name, received_qty</span> (grn_number and grn_date are optional).
        </div>
    </div>

    <div class="card">
        <div class="card-header"><h2>➕ Log a GRN Receipt</h2></div>
        <form method="POST" action="/grn/add">
            <div class="form-grid">
                <div>
                    <label>Company / Client</label>
                    <select name="company" required>
                        <option value="" disabled selected>Select company</option>
                        {% for c in companies %}
                        <option value="{{ c }}">{{ c }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div>
                    <label>PO Number</label>
                    <input type="text" name="po_number" placeholder="e.g. PO-2026-014" required>
                </div>
                <div>
                    <label>Item Name (must match the PO item exactly)</label>
                    <input type="text" name="item_name" placeholder="e.g. Instant Noodles" required>
                </div>
                <div>
                    <label>GRN Number — optional</label>
                    <input type="text" name="grn_number" placeholder="e.g. GRN-1002">
                </div>
                <div>
                    <label>GRN Date — optional</label>
                    <input type="date" name="grn_date">
                </div>
                <div>
                    <label>Received Quantity</label>
                    <input type="number" name="received_qty" placeholder="e.g. 20000" required>
                </div>
            </div>
            <button type="submit" class="btn" style="margin-top:14px;">Log GRN Receipt</button>
        </form>
        {% if companies|length == 0 %}
        <div style="color:var(--text-muted); font-size:12.5px; margin-top:10px;">No companies yet — <a href="/companies" style="color:var(--primary);">add one first</a>.</div>
        {% endif %}
    </div>

    <div class="card">
        <div class="card-header"><h2>📋 GRN Entries Logged</h2></div>
        {% if grn_entries|length > 0 %}
        <table>
            <thead><tr><th>Company</th><th>PO Number</th><th>Item</th><th>GRN #</th><th>GRN Date</th><th>Received Qty</th><th></th></tr></thead>
            <tbody>
            {% for g in grn_entries %}
            <tr>
                <td><span class="badge badge-blue">{{ g[1] }}</span></td>
                <td>{{ g[2] }}</td>
                <td>{{ g[3] }}</td>
                <td>{{ g[4] or '—' }}</td>
                <td>{{ g[5] or '—' }}</td>
                <td>{{ g[6] }}</td>
                <td>
                    <form method="POST" action="/grn/delete/{{ g[0] }}" onsubmit="return confirm('Delete this GRN entry?');">
                        <button type="submit" class="btn btn-danger btn-sm">Delete</button>
                    </form>
                </td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <div class="empty-state">No GRN entries logged yet.</div>
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
        <a href="/production?tab=pending_qc" class="{% if tab == 'pending_qc' %}active{% endif %}">🔔 Pending QC{% if pending_qc_count > 0 %} ({{ pending_qc_count }}){% endif %}</a>
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
                {% if available_qcs|length == 0 %}
                <div class="badge badge-amber" style="display:block; padding:10px 14px; margin-bottom:10px;">No Quality Checker is Available right now (all On Leave / Unavailable). This entry will be saved as <strong>QC Pending</strong> — any QC can inspect it once available, from the Pending QC tab.</div>
                <input type="hidden" name="save_as_pending" value="1">
                {% else %}
                <label style="font-size:12px; display:flex; align-items:center; gap:6px; margin-bottom:8px; font-weight:400;">
                    <input type="checkbox" id="pendingToggle" onchange="togglePendingQc()" style="width:auto; margin:0;"> No QC available for me right now — save as QC Pending instead
                </label>
                <input type="hidden" name="save_as_pending" id="savePendingField" value="0">
                <div id="qcCheckinFields">
                <select name="qc_name" id="qcSelect" required onchange="showQcPhoto()">
                    <option value="" disabled selected>Select your name</option>
                    {% for qc in available_qcs %}
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
                {% endif %}
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
                <th>Time</th><th>Company</th><th>Sub-brand</th><th>Item</th><th>Batch</th><th>Qty</th><th>QC</th><th>QC Status</th><th></th>
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
                <td>{{ e.qc_name or '—' }}</td>
                <td>
                    {% if e.qc_status == 'QC Pending' %}<span class="badge badge-amber">🔔 Pending</span>
                    {% elif e.qc_status == 'Rejected' %}<span class="badge badge-red">❌ Rejected</span>
                    {% else %}<span class="badge badge-green">✅ Approved</span>
                    {% endif %}
                </td>
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

    {% elif tab == 'pending_qc' %}
    <div class="card">
        <div class="card-header"><h2>🔔 Production Entries Awaiting QC Inspection</h2></div>
        <div style="color:var(--text-muted); font-size:12.5px; margin-bottom:14px;">
            These entries were logged when no Quality Checker was Available. Any QC who is currently Available can inspect and approve/reject one below.
        </div>
        {% if available_qcs|length == 0 %}
        <div class="badge badge-amber" style="display:block; padding:10px 14px;">No QC is currently marked Available — go to the Admin tab to mark someone Available before inspecting.</div>
        {% endif %}
        {% if pending_qc_entries|length > 0 %}
        {% for e in pending_qc_entries %}
        <div style="border:1px solid var(--border); border-radius:12px; padding:14px; margin-bottom:12px;">
            <div style="display:flex; justify-content:space-between; flex-wrap:wrap; gap:8px; margin-bottom:10px;">
                <div>
                    <span class="badge badge-blue">{{ e.company }}</span>
                    <strong style="margin-left:6px;">{{ e.item_name }}</strong>
                    <span style="color:var(--text-muted); font-size:12px;"> &middot; Batch {{ e.batch_number }} &middot; Qty {{ e.quantity }} &middot; Packed {{ e.prod_date }} {{ e.prod_time }}</span>
                </div>
            </div>
            {% if available_qcs|length > 0 %}
            <form method="POST" action="/production/qc/inspect/{{ e.id }}" style="display:flex; gap:10px; flex-wrap:wrap; align-items:flex-end;">
                <div>
                    <label style="font-size:11px;">Inspecting QC</label>
                    <select name="qc_name" id="inspectQcSelect{{ e.id }}" required onchange="showInspectPhoto({{ e.id }})" style="margin:0;">
                        <option value="" disabled selected>Select your name</option>
                        {% for qc in available_qcs %}
                        <option value="{{ qc.name }}">{{ qc.name }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div id="inspectRefWrap{{ e.id }}" style="display:none; align-items:center; gap:6px;">
                    <img id="inspectRefImg{{ e.id }}" style="width:40px; height:40px; border-radius:6px; object-fit:cover;">
                </div>
                <div>
                    <label style="font-size:11px;">Check-in selfie</label>
                    <input type="file" accept="image/*" capture="user" onchange="compressImage(this, 'inspectPhotoData{{ e.id }}', 'inspectPhotoPreview{{ e.id }}')">
                    <input type="hidden" name="qc_photo" id="inspectPhotoData{{ e.id }}" required>
                </div>
                <img id="inspectPhotoPreview{{ e.id }}" style="display:none; width:40px; height:40px; border-radius:6px; object-fit:cover;">
                <button type="submit" name="verdict" value="Approved" class="btn btn-sm">✅ Approve</button>
                <button type="submit" name="verdict" value="Rejected" class="btn btn-danger btn-sm">❌ Reject</button>
            </form>
            {% endif %}
        </div>
        {% endfor %}
        {% else %}
        <div class="empty-state">No entries pending QC inspection right now. 🎉</div>
        {% endif %}
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
        <div style="display:grid; grid-template-columns:repeat(auto-fill, minmax(170px, 1fr)); gap:14px;">
            {% for qc in quality_checkers %}
            <div style="background:rgba(255,255,255,0.03); border:1px solid var(--border); border-radius:12px; padding:14px; text-align:center;">
                <img src="{{ qc.photo_data }}" style="width:56px; height:56px; border-radius:50%; object-fit:cover; margin-bottom:8px;">
                <div style="font-weight:600; font-size:13px; margin-bottom:8px;">{{ qc.name }}</div>
                <form method="POST" action="/production/qc/availability/{{ qc.id }}" style="margin-bottom:8px;">
                    <select name="availability" onchange="this.form.submit()" style="margin:0; font-size:12px; padding:6px;
                        {% if qc.availability == 'Available' %}color:#4ade80;{% elif qc.availability == 'On Leave' %}color:#fbbf24;{% else %}color:#f87171;{% endif %}">
                        <option value="Available" {% if qc.availability == 'Available' %}selected{% endif %}>🟢 Available</option>
                        <option value="On Leave" {% if qc.availability == 'On Leave' %}selected{% endif %}>🟡 On Leave</option>
                        <option value="Unavailable" {% if qc.availability == 'Unavailable' %}selected{% endif %}>🔴 Unavailable</option>
                    </select>
                </form>
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
            <label>Bulk Import (CSV or Excel — columns: barcode, item_name, company, sub_brand, pin_code, packing_size)</label>
            <input type="file" name="catalog_file" accept=".csv,.xlsx" required style="padding:10px;">
            <button type="submit" class="btn" style="margin-top:10px;">Upload &amp; Import</button>
        </form>
        <form method="POST" action="/production/barcode_catalog/add">
            <div class="form-grid">
                <div><label>Barcode / EAN</label><input type="text" name="barcode" placeholder="Barcode / EAN" required></div>
                <div><label>Product Name</label><input type="text" name="item_name" placeholder="Item name" required></div>
                <div>
                    <label>Company</label>
                    <select name="company">
                        <option value="">— optional —</option>
                        {% for c in companies %}
                        <option value="{{ c.name }}">{{ c.name }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div><label>Sub-brand</label><input type="text" name="sub_brand" placeholder="e.g. Daily Good"></div>
                <div><label>PIN Code</label><input type="text" name="pin_code" placeholder="e.g. 560074"></div>
                <div><label>Packing Size</label><input type="text" name="packing_size" placeholder="e.g. 500g"></div>
            </div>
            <button type="submit" class="btn btn-outline" style="margin-top:10px;">Add Single Barcode</button>
        </form>
        {% if catalog_rows|length > 0 %}
        <table style="margin-top:18px;">
            <thead><tr><th>Barcode</th><th>Product</th><th>Company</th><th>Sub-brand</th><th>PIN</th><th>Packing</th><th></th></tr></thead>
            <tbody>
            {% for cr in catalog_rows %}
            <tr>
                <td style="font-family:monospace;">{{ cr[1] }}</td>
                <td>{{ cr[2] }}</td>
                <td>{{ cr[3] or '—' }}</td>
                <td>{{ cr[4] or '—' }}</td>
                <td>{{ cr[5] or '—' }}</td>
                <td>{{ cr[6] or '—' }}</td>
                <td>
                    <form method="POST" action="/production/barcode_catalog/delete/{{ cr[0] }}" onsubmit="return confirm('Remove this barcode from the catalog?');">
                        <button type="submit" class="btn btn-danger btn-sm">Delete</button>
                    </form>
                </td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
        {% endif %}
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

    function showInspectPhoto(entryId) {
        const name = document.getElementById('inspectQcSelect' + entryId).value;
        const wrap = document.getElementById('inspectRefWrap' + entryId);
        const img = document.getElementById('inspectRefImg' + entryId);
        if (qcPhotos[name]) {
            img.src = qcPhotos[name];
            wrap.style.display = 'flex';
        } else {
            wrap.style.display = 'none';
        }
    }

    function togglePendingQc() {
        const checked = document.getElementById('pendingToggle').checked;
        document.getElementById('savePendingField').value = checked ? '1' : '0';
        const fields = document.getElementById('qcCheckinFields');
        fields.style.display = checked ? 'none' : 'block';
        const qcSelect = document.getElementById('qcSelect');
        const qcPhotoData = document.getElementById('qcPhotoData');
        if (checked) {
            qcSelect.required = false;
            qcPhotoData.required = false;
        } else {
            qcSelect.required = true;
            qcPhotoData.required = true;
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
                    if (info.company) {
                        const compSelect = document.getElementById('prodCompany');
                        if ([...compSelect.options].some(o => o.value === info.company)) {
                            compSelect.value = info.company;
                            updateSubBrands();
                            if (info.sub_brand) {
                                const subSelect = document.getElementById('prodSubBrand');
                                if ([...subSelect.options].some(o => o.value === info.sub_brand)) {
                                    subSelect.value = info.sub_brand;
                                }
                            }
                        }
                    }
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

@app.route('/grn')
def grn_page():
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    msg = request.args.get('msg')
    msg_ok = request.args.get('ok') == '1'
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT name FROM companies WHERE factory_id = %s ORDER BY name', (fid,))
    companies = [r[0] for r in cursor.fetchall()]

    shortage_map = get_po_shortage_map(cursor, fid)
    shortage_rows = [
        {'company': c, 'po_number': po, 'item_name': item, **info}
        for (c, po, item), info in sorted(shortage_map.items(), key=lambda x: (x[0][0], x[0][1], x[0][2]))
    ]

    cursor.execute('SELECT id, company, po_number, item_name, grn_number, grn_date, received_qty FROM grn_log WHERE factory_id = %s ORDER BY id DESC LIMIT 300', (fid,))
    grn_entries = cursor.fetchall()
    conn.close()
    return render_template_string(GRN_HTML, companies=companies, shortage_rows=shortage_rows, grn_entries=grn_entries, msg=msg, msg_ok=msg_ok)

@app.route('/grn/add', methods=['POST'])
def grn_add():
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    company = request.form.get('company', '').strip()
    po_number = request.form.get('po_number', '').strip()
    item_name = request.form.get('item_name', '').strip()
    grn_number = request.form.get('grn_number', '').strip()
    grn_date = request.form.get('grn_date', '').strip()
    qty_raw = request.form.get('received_qty', '').strip()
    if not company or not po_number or not item_name or not qty_raw:
        return redirect('/grn?ok=0&msg=' + quote('Company, PO number, item name and received quantity are all required.'))
    try:
        received_qty = int(float(qty_raw))
    except ValueError:
        return redirect('/grn?ok=0&msg=' + quote('Received quantity must be a number.'))
    if received_qty <= 0:
        return redirect('/grn?ok=0&msg=' + quote('Received quantity must be greater than zero.'))
    conn = get_conn()
    cursor = conn.cursor()
    # I1: server-side over-receiving protection, race-condition-safe via row lock (see docstring).
    ok, err, _ = check_grn_capacity(cursor, fid, company, po_number, item_name, received_qty)
    if not ok:
        conn.close()
        return redirect('/grn?ok=0&msg=' + quote(err))
    cursor.execute('''INSERT INTO grn_log (factory_id, company, po_number, item_name, grn_number, grn_date, received_qty, created_at, created_by)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                   (fid, company, po_number, item_name, grn_number or None, grn_date or None, received_qty, now_ist().isoformat(), session.get('user_name')))
    # Check whether this receipt fulfills the PO item, for a clearer audit trail
    shortage_map = get_po_shortage_map(cursor, fid, company=company, po_number=po_number)
    info = shortage_map.get((company, po_number, item_name))
    status_note = ' — PO Fulfilled' if info and info['fulfilled'] else (f" — Pending: {info['pending']}" if info else '')
    log_audit(cursor, 'GRN Received', 'GRN', po_number, f'{company}: {item_name} +{received_qty}{status_note}')
    conn.commit()
    conn.close()
    return redirect('/grn?ok=1&msg=' + quote(f'GRN receipt of {received_qty} logged against PO {po_number}.'))

@app.route('/grn/ai_import', methods=['GET', 'POST'])
def grn_ai_import():
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    if request.method == 'GET':
        cursor.execute('SELECT name FROM companies WHERE factory_id = %s ORDER BY name', (fid,))
        companies = [r[0] for r in cursor.fetchall()]
        conn.close()
        api_key_set = bool(os.environ.get('ANTHROPIC_API_KEY'))
        return render_template_string(AI_IMPORT_GRN_HTML, companies=companies, api_key_set=api_key_set,
                                       import_type='grn', action_url='/grn/ai_import')

    company = request.form.get('company', '').strip()
    file = request.files.get('doc_file')
    if not company or not file or file.filename == '':
        conn.close()
        return redirect('/grn/ai_import?error=' + quote('Please select a company and a file.'))

    filename_lower = file.filename.lower()
    if filename_lower.endswith('.pdf'):
        mimetype = 'application/pdf'
    elif filename_lower.endswith(('.jpg', '.jpeg')):
        mimetype = 'image/jpeg'
    elif filename_lower.endswith('.png'):
        mimetype = 'image/png'
    else:
        conn.close()
        return redirect('/grn/ai_import?error=' + quote('Please upload a PDF, JPG or PNG file.'))

    file_bytes = file.read()
    parsed, error = call_ai_extraction(file_bytes, mimetype, 'grn')
    ts = now_ist().isoformat()
    if error:
        cursor.execute('''INSERT INTO ai_import_staging (factory_id, import_type, company, source_filename, extracted_json, status, error_message, created_at, created_by)
                           VALUES (%s,'GRN',%s,%s,NULL,'failed',%s,%s,%s)''', (fid, company, file.filename, error, ts, session.get('user_name')))
        conn.commit()
        conn.close()
        return redirect('/grn/ai_import?error=' + quote(error))

    cursor.execute('''INSERT INTO ai_import_staging (factory_id, import_type, company, source_filename, extracted_json, status, created_at, created_by)
                       VALUES (%s,'GRN',%s,%s,%s,'pending_review',%s,%s) RETURNING id''',
                   (fid, company, file.filename, json.dumps(parsed), ts, session.get('user_name')))
    staging_id = cursor.fetchone()[0]
    conn.commit()
    conn.close()
    return redirect(f'/grn/ai_import/review/{staging_id}')

@app.route('/grn/ai_import/review/<int:staging_id>')
def grn_ai_import_review(staging_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT company, source_filename, extracted_json, status FROM ai_import_staging WHERE id = %s AND factory_id = %s AND import_type = %s',
                   (staging_id, fid, 'GRN'))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return "Import not found. <a href='/grn/ai_import'>Back</a>", 404
    company, filename, extracted_json, status = row
    extracted = json.loads(extracted_json) if extracted_json else {'items': []}
    return render_template_string(AI_IMPORT_REVIEW_GRN_HTML, staging_id=staging_id, company=company,
                                   filename=filename, extracted=extracted, status=status)

@app.route('/grn/ai_import/review/<int:staging_id>/commit', methods=['POST'])
def grn_ai_import_commit(staging_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT company, status FROM ai_import_staging WHERE id = %s AND factory_id = %s AND import_type = %s', (staging_id, fid, 'GRN'))
    row = cursor.fetchone()
    if not row or row[1] == 'committed':
        conn.close()
        return redirect('/grn/ai_import')
    company = row[0]
    po_number = request.form.get('po_number', '').strip()
    grn_number = request.form.get('grn_number', '').strip() or None
    grn_date = request.form.get('grn_date', '').strip() or None
    item_names = request.form.getlist('item_name')
    qtys = request.form.getlist('received_qty')
    includes = request.form.getlist('include')

    if not po_number or not item_names:
        conn.close()
        return redirect(f'/grn/ai_import/review/{staging_id}?error=' + quote('PO number and at least one item are required.'))

    ts = now_ist().isoformat()
    inserted = 0
    over_capacity = 0
    for i in range(len(item_names)):
        if str(i) not in includes:
            continue
        item_name = item_names[i].strip()
        if not item_name:
            continue
        try:
            received_qty = int(float(qtys[i])) if i < len(qtys) and qtys[i].strip() else 0
        except ValueError:
            received_qty = 0
        if received_qty <= 0:
            continue
        ok, err, _ = check_grn_capacity(cursor, fid, company, po_number, item_name, received_qty)
        if not ok:
            over_capacity += 1
            continue
        cursor.execute('''INSERT INTO grn_log (factory_id, company, po_number, item_name, grn_number, grn_date, received_qty, created_at, created_by)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                       (fid, company, po_number, item_name, grn_number, grn_date, received_qty, ts, session.get('user_name')))
        inserted += 1

    cursor.execute("UPDATE ai_import_staging SET status = 'committed' WHERE id = %s", (staging_id,))
    log_audit(cursor, 'AI GRN Import Committed', 'GRN', po_number, f'{company}: {inserted} item(s) logged from AI-scanned document')
    conn.commit()
    conn.close()
    msg = f'{inserted} GRN item(s) logged against PO {po_number}.'
    if over_capacity:
        msg += f' {over_capacity} item(s) rejected (would exceed PO ordered quantity).'
    return redirect('/grn?ok=1&msg=' + quote(msg))

@app.route('/grn/ai_import/review/<int:staging_id>/discard', methods=['POST'])
def grn_ai_import_discard(staging_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE ai_import_staging SET status = 'discarded' WHERE id = %s AND factory_id = %s AND import_type = 'GRN'", (staging_id, fid))
    conn.commit()
    conn.close()
    return redirect('/grn/ai_import')

@app.route('/grn/import_csv', methods=['POST'])
def grn_import_csv():
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    company = request.form.get('company', '').strip()
    if not company:
        return redirect('/grn?ok=0&msg=' + quote('Please select a company for this import.'))
    file = request.files.get('grn_file')
    if not file or file.filename == '':
        return redirect('/grn?ok=0&msg=' + quote('No file selected.'))
    filename_lower = file.filename.lower()
    if not (filename_lower.endswith('.csv') or filename_lower.endswith('.xlsx')):
        return redirect('/grn?ok=0&msg=' + quote('Please upload a .csv or .xlsx file.'))

    fieldnames, rows = _read_spreadsheet_rows(file)
    if fieldnames is None:
        return redirect('/grn?ok=0&msg=' + quote('Could not read the file — check it has a header row and try again.'))

    colmap = _map_csv_headers(fieldnames)
    received_alias = {'received_qty': ['received_qty', 'received qty', 'received quantity', 'grn_qty', 'grn qty', 'qty received']}
    normalized = {f.strip().lower(): f for f in fieldnames if f}
    received_col = None
    for alias in received_alias['received_qty']:
        if alias in normalized:
            received_col = normalized[alias]
            break
    if 'po_number' not in colmap or 'item_name' not in colmap or not received_col:
        return redirect('/grn?ok=0&msg=' + quote('File must have po_number, item_name and received_qty columns.'))

    grn_num_col = normalized.get('grn_number') or normalized.get('grn no') or normalized.get('grn_no')
    grn_date_col = normalized.get('grn_date') or normalized.get('grn date')

    conn = get_conn()
    cursor = conn.cursor()
    inserted, skipped, over_capacity = 0, 0, 0
    ts = now_ist().isoformat()
    for row in rows:
        po_number = (row.get(colmap['po_number']) or '').strip()
        item_name = (row.get(colmap['item_name']) or '').strip()
        qty_raw = (row.get(received_col) or '').strip()
        grn_number = (row.get(grn_num_col) or '').strip() if grn_num_col else ''
        grn_date = (row.get(grn_date_col) or '').strip() if grn_date_col else ''
        if not po_number or not item_name or not qty_raw:
            skipped += 1
            continue
        try:
            received_qty = int(float(qty_raw))
        except ValueError:
            skipped += 1
            continue
        if received_qty <= 0:
            skipped += 1
            continue
        # I1: same server-side, race-condition-safe check as the single-entry GRN form.
        ok, err, _ = check_grn_capacity(cursor, fid, company, po_number, item_name, received_qty)
        if not ok:
            over_capacity += 1
            continue
        cursor.execute('''INSERT INTO grn_log (factory_id, company, po_number, item_name, grn_number, grn_date, received_qty, created_at, created_by)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                       (fid, company, po_number, item_name, grn_number or None, grn_date or None, received_qty, ts, session.get('user_name')))
        inserted += 1
    log_audit(cursor, 'GRN Bulk Imported', 'GRN', '', f'{company}: {inserted} GRN rows imported')
    conn.commit()
    conn.close()
    msg = f'{inserted} GRN entries imported for {company}.'
    if skipped:
        msg += f' {skipped} rows skipped (incomplete data).'
    if over_capacity:
        msg += f' {over_capacity} row(s) rejected (would exceed PO ordered quantity).'
    return redirect('/grn?ok=1&msg=' + quote(msg))

@app.route('/grn/delete/<int:grn_id>', methods=['POST'])
def grn_delete(grn_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT po_number, item_name, received_qty FROM grn_log WHERE id = %s AND factory_id = %s', (grn_id, fid))
    row = cursor.fetchone()
    cursor.execute('DELETE FROM grn_log WHERE id = %s AND factory_id = %s', (grn_id, fid))
    if row:
        log_audit(cursor, 'GRN Entry Deleted', 'GRN', row[0], f'{row[1]}: removed receipt of {row[2]}')
    conn.commit()
    conn.close()
    return redirect('/grn')

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

@app.route('/rate_master')
def rate_master_page():
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    filter_company = request.args.get('company', '').strip()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT name FROM companies WHERE factory_id = %s ORDER BY name', (fid,))
    companies = [r[0] for r in cursor.fetchall()]
    if filter_company:
        cursor.execute('SELECT id, company, product_name, approved_rate, updated_at FROM rate_master WHERE factory_id = %s AND company = %s ORDER BY company, product_name', (fid, filter_company))
    else:
        cursor.execute('SELECT id, company, product_name, approved_rate, updated_at FROM rate_master WHERE factory_id = %s ORDER BY company, product_name', (fid,))
    rates = cursor.fetchall()
    conn.close()
    error_msg = request.args.get('error')
    return render_template_string(RATE_MASTER_HTML, rates=rates, companies=companies, filter_company=filter_company, error_msg=error_msg)

@app.route('/rate_master/add', methods=['POST'])
def rate_master_add():
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    company = request.form.get('company', '').strip()
    product_name = request.form.get('product_name', '').strip()
    rate_raw = request.form.get('approved_rate', '').strip()
    if not company or not product_name or not rate_raw:
        return redirect('/rate_master?error=' + quote('Company, product name and rate are all required.'))
    try:
        approved_rate = float(rate_raw)
    except ValueError:
        return redirect('/rate_master?error=' + quote('Rate must be a number.'))
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT approved_rate FROM rate_master WHERE factory_id = %s AND company = %s AND product_name = %s', (fid, company, product_name))
    existing = cursor.fetchone()
    ts = now_ist().isoformat()
    cursor.execute('''INSERT INTO rate_master (factory_id, company, product_name, approved_rate, updated_at, updated_by)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (factory_id, company, product_name) DO UPDATE SET approved_rate = EXCLUDED.approved_rate, updated_at = EXCLUDED.updated_at, updated_by = EXCLUDED.updated_by''',
                   (fid, company, product_name, approved_rate, ts, session.get('user_name')))
    if existing and existing[0] != approved_rate:
        log_audit(cursor, 'PO Rate Changed', 'Rate Master', product_name, f'{company}: ₹{existing[0]} to ₹{approved_rate}')
    elif not existing:
        log_audit(cursor, 'Approved Rate Added', 'Rate Master', product_name, f'{company}: set to ₹{approved_rate}')
    conn.commit()
    conn.close()
    return redirect('/rate_master?company=' + quote(company) if company else '/rate_master')

@app.route('/rate_master/delete/<int:rate_id>', methods=['POST'])
def rate_master_delete(rate_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT company, product_name FROM rate_master WHERE id = %s AND factory_id = %s', (rate_id, fid))
    rrow = cursor.fetchone()
    cursor.execute('DELETE FROM rate_master WHERE id = %s AND factory_id = %s', (rate_id, fid))
    if rrow:
        log_audit(cursor, 'Approved Rate Deleted', 'Rate Master', rate_id, f'{rrow[0]}: {rrow[1]}')
    conn.commit()
    conn.close()
    return redirect('/rate_master')

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

    cursor.execute('SELECT id, name, photo_data, availability FROM quality_checkers WHERE factory_id = %s ORDER BY name', (fid,))
    qc_rows = cursor.fetchall()
    quality_checkers = [{'id': r[0], 'name': r[1], 'photo_data': r[2], 'availability': r[3] or 'Available'} for r in qc_rows]
    available_qcs = [qc for qc in quality_checkers if qc['availability'] == 'Available']
    qc_photos = {r[1]: r[2] for r in qc_rows}

    grouped_history = []
    by_company, by_item, grand_total, total_entries = [], [], 0, 0
    catalog_count = 0
    catalog_rows = []
    pending_qc_entries = []
    cursor.execute("SELECT COUNT(*) FROM daily_production WHERE factory_id = %s AND qc_status = 'QC Pending'", (fid,))
    pending_qc_count = cursor.fetchone()[0]

    if tab == 'history':
        cursor.execute('''SELECT id, company, sub_brand, item_name, batch_number, quantity, qc_name,
                           packing_date, use_by_date, prod_date, prod_time, created_at, qc_status
                           FROM daily_production WHERE factory_id = %s ORDER BY id DESC LIMIT 500''', (fid,))
        rows = cursor.fetchall()
        now = now_ist()
        groups = {}
        order = []
        for r in rows:
            (pid, company, sub_brand, item_name, batch_number, quantity, qc_name,
             packing_date, use_by_date, prod_date, prod_time, created_at, qc_status) = r
            try:
                created_dt = datetime.fromisoformat(created_at)
            except Exception:
                created_dt = now
            editable = (now - created_dt) <= timedelta(hours=12)
            entry = {
                'id': pid, 'company': company, 'sub_brand': sub_brand, 'item_name': item_name,
                'batch_number': batch_number, 'quantity': quantity, 'qc_name': qc_name,
                'packing_date': packing_date, 'use_by_date': use_by_date,
                'prod_time': prod_time, 'editable': editable, 'qc_status': qc_status or 'Approved'
            }
            if prod_date not in groups:
                groups[prod_date] = {'date': prod_date, 'entries': [], 'total_qty': 0}
                order.append(prod_date)
            groups[prod_date]['entries'].append(entry)
            groups[prod_date]['total_qty'] += quantity or 0
        grouped_history = [groups[d] for d in order]

    elif tab == 'pending_qc':
        cursor.execute('''SELECT id, company, sub_brand, item_name, batch_number, quantity,
                           packing_date, use_by_date, prod_date, prod_time
                           FROM daily_production WHERE factory_id = %s AND qc_status = 'QC Pending' ORDER BY id ASC''', (fid,))
        pending_qc_entries = [{'id': r[0], 'company': r[1], 'sub_brand': r[2], 'item_name': r[3], 'batch_number': r[4],
                                'quantity': r[5], 'packing_date': r[6], 'use_by_date': r[7], 'prod_date': r[8], 'prod_time': r[9]}
                               for r in cursor.fetchall()]

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
        cursor.execute('SELECT id, barcode, item_name, company, sub_brand, pin_code, packing_size FROM barcode_catalog WHERE factory_id = %s ORDER BY id DESC LIMIT 200', (fid,))
        catalog_rows = cursor.fetchall()

    conn.close()

    return render_template_string(
        PRODUCTION_HTML,
        tab=tab, msg=msg, msg_ok=msg_ok,
        companies=companies, quality_checkers=quality_checkers, available_qcs=available_qcs,
        company_subbrands_json=json.dumps(company_subbrands),
        qc_photos_json=json.dumps(qc_photos),
        grouped_history=grouped_history,
        by_company=by_company, by_item=by_item, grand_total=grand_total, total_entries=total_entries,
        catalog_count=catalog_count, catalog_rows=catalog_rows,
        pending_qc_entries=pending_qc_entries, pending_qc_count=pending_qc_count
    )

def validate_production_input(cursor, fid, company, sub_brand, barcode, item_name):
    """J1: server-side production entry validation — never trusts client-submitted company/sub_brand/
    barcode fields even though the UI already scopes them correctly; this is the backend-authoritative
    check for direct POST/manipulation attempts. Returns (ok, error_message_or_None)."""
    if not company:
        return False, 'Company is required.'
    cursor.execute('SELECT sub_brands FROM companies WHERE factory_id = %s AND name = %s', (fid, company))
    crow = cursor.fetchone()
    if not crow:
        return False, f'"{company}" is not a valid company for your factory.'
    if sub_brand:
        valid_subs = {s.strip() for s in (crow[0] or '').split(',') if s.strip()}
        if sub_brand not in valid_subs:
            return False, f'"{sub_brand}" is not a registered sub-brand of {company}.'
    cursor.execute('SELECT company FROM barcode_catalog WHERE factory_id = %s AND barcode = %s', (fid, barcode))
    brow = cursor.fetchone()
    if not brow:
        return False, f'Barcode {barcode} is not registered in the Barcode Catalog.'
    barcode_company = (brow[0] or '').strip()
    if barcode_company and barcode_company != company:
        return False, f'Barcode {barcode} belongs to a different company ({barcode_company}), not {company}.'
    if not item_name:
        return False, 'Item name is required.'
    return True, None

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
    save_as_pending = request.form.get('save_as_pending', '').strip() == '1'

    if not barcode or not item_name:
        return redirect('/production?tab=log&ok=0&msg=' + quote('Please scan and confirm a barcode before submitting.'))
    if not save_as_pending and (not qc_name or not qc_photo):
        return redirect('/production?tab=log&ok=0&msg=' + quote('Quality Checker check-in (name + photo) is required.'))
    try:
        quantity = int(float(quantity_raw))
    except ValueError:
        return redirect('/production?tab=log&ok=0&msg=' + quote('Quantity must be a number.'))
    if quantity <= 0:
        return redirect('/production?tab=log&ok=0&msg=' + quote('Quantity must be greater than zero.'))

    conn = get_conn()
    cursor = conn.cursor()
    # J1: server-side authoritative validation — company/sub-brand/barcode all backend-verified,
    # regardless of what the form/JS submitted.
    ok, err = validate_production_input(cursor, fid, company, sub_brand, barcode, item_name)
    if not ok:
        conn.close()
        return redirect('/production?tab=log&ok=0&msg=' + quote(err))

    qc_status = 'QC Pending' if save_as_pending else 'Approved'
    if save_as_pending:
        qc_name, qc_photo = None, None

    now = now_ist()
    cursor.execute('''INSERT INTO daily_production
        (factory_id, company, sub_brand, item_name_entered, item_name, barcode, packing_date, use_by_date,
         batch_number, quantity, qc_name, qc_photo, qc_status, prod_date, prod_time, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)''',
        (fid, company, sub_brand, item_name_entered, item_name, barcode, packing_date, use_by_date,
         batch_number, quantity, qc_name, qc_photo, qc_status, now.strftime('%d %b %Y'), now.strftime('%I:%M %p'), now.isoformat()))
    log_audit(cursor, 'Production Entry Created', 'Production', '', f'{company}: {item_name} x{quantity} (batch {batch_number or "—"})')
    conn.commit()
    conn.close()
    if save_as_pending:
        return redirect('/production?tab=history&ok=1&msg=' + quote('No QC was available — entry saved as QC Pending. Any available QC can inspect it later.'))
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
    if quantity <= 0:
        conn.close()
        return redirect('/production?tab=history&ok=0&msg=' + quote('Quantity must be greater than zero.'))

    cursor.execute('UPDATE daily_production SET packing_date=%s, use_by_date=%s, batch_number=%s, quantity=%s WHERE id=%s AND factory_id=%s',
                   (packing_date, use_by_date, batch_number, quantity, entry_id, fid))
    log_audit(cursor, 'Production Entry Edited', 'Production', entry_id, f'quantity={quantity}, batch={batch_number or "—"}')
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
    log_audit(cursor, 'Production Entry Deleted', 'Production', entry_id, '')
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

@app.route('/production/qc/inspect/<int:entry_id>', methods=['POST'])
def production_qc_inspect(entry_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    qc_name = request.form.get('qc_name', '').strip()
    qc_photo = request.form.get('qc_photo', '').strip()
    verdict = request.form.get('verdict', '').strip()
    if verdict not in ('Approved', 'Rejected') or not qc_name or not qc_photo:
        return redirect('/production?tab=pending_qc&ok=0&msg=' + quote('QC name, selfie and a decision (Approve/Reject) are all required.'))
    conn = get_conn()
    cursor = conn.cursor()
    # Only a currently-Available QC belonging to this factory may inspect — prevents someone
    # marked On Leave/Unavailable (or from another factory) from claiming a pending entry.
    cursor.execute("SELECT id FROM quality_checkers WHERE factory_id = %s AND name = %s AND availability = 'Available'", (fid, qc_name))
    if not cursor.fetchone():
        conn.close()
        return redirect('/production?tab=pending_qc&ok=0&msg=' + quote(f'{qc_name} is not currently marked Available.'))
    cursor.execute("UPDATE daily_production SET qc_name = %s, qc_photo = %s, qc_status = %s WHERE id = %s AND factory_id = %s AND qc_status = 'QC Pending'",
                   (qc_name, qc_photo, verdict, entry_id, fid))
    log_audit(cursor, f'QC {verdict}', 'QC', entry_id, f'by {qc_name}')
    conn.commit()
    conn.close()
    return redirect('/production?tab=pending_qc&ok=1&msg=' + quote(f'Entry {verdict.lower()} by {qc_name}.'))

@app.route('/production/qc/availability/<int:qc_id>', methods=['POST'])
def production_qc_availability(qc_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    availability = request.form.get('availability', '').strip()
    if availability not in ('Available', 'On Leave', 'Unavailable'):
        return redirect('/production?tab=admin')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('UPDATE quality_checkers SET availability = %s WHERE id = %s AND factory_id = %s', (availability, qc_id, fid))
    conn.commit()
    conn.close()
    return redirect('/production?tab=admin')

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
    company = request.form.get('company', '').strip() or None
    sub_brand = request.form.get('sub_brand', '').strip() or None
    pin_code = request.form.get('pin_code', '').strip() or None
    packing_size = request.form.get('packing_size', '').strip() or None
    if not barcode or not item_name:
        return redirect('/production?tab=admin')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('''INSERT INTO barcode_catalog (factory_id, barcode, item_name, company, sub_brand, pin_code, packing_size) VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (factory_id, barcode) DO UPDATE SET item_name = EXCLUDED.item_name, company = EXCLUDED.company,
                       sub_brand = EXCLUDED.sub_brand, pin_code = EXCLUDED.pin_code, packing_size = EXCLUDED.packing_size''',
                   (fid, barcode, item_name, company, sub_brand, pin_code, packing_size))
    conn.commit()
    conn.close()
    return redirect('/production?tab=admin&ok=1&msg=' + quote('Barcode added to catalog.'))

@app.route('/production/barcode_catalog/delete/<int:catalog_id>', methods=['POST'])
def barcode_catalog_delete(catalog_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    # Per spec: Barcode Catalog add/edit/delete is intentionally NOT recorded in the Audit Log.
    cursor.execute('DELETE FROM barcode_catalog WHERE id = %s AND factory_id = %s', (catalog_id, fid))
    conn.commit()
    conn.close()
    return redirect('/production?tab=admin')

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
        company = (row.get('company') or '').strip() or None
        sub_brand = (row.get('sub_brand') or '').strip() or None
        pin_code = (row.get('pin_code') or '').strip() or None
        packing_size = (row.get('packing_size') or '').strip() or None
        if not barcode or not item_name:
            skipped += 1
            continue
        cursor.execute('''INSERT INTO barcode_catalog (factory_id, barcode, item_name, company, sub_brand, pin_code, packing_size) VALUES (%s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (factory_id, barcode) DO UPDATE SET item_name = EXCLUDED.item_name, company = EXCLUDED.company,
                           sub_brand = EXCLUDED.sub_brand, pin_code = EXCLUDED.pin_code, packing_size = EXCLUDED.packing_size''',
                       (fid, barcode, item_name, company, sub_brand, pin_code, packing_size))
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
    cursor.execute('SELECT item_name, company, sub_brand, packing_size FROM barcode_catalog WHERE factory_id = %s AND barcode = %s', (fid, barcode))
    row = cursor.fetchone()
    if row:
        conn.close()
        return {'found': True, 'item_name': row[0], 'company': row[1], 'sub_brand': row[2], 'packing_size': row[3]}
    cursor.execute('SELECT item_name, company FROM po_items WHERE factory_id = %s AND barcode = %s LIMIT 1', (fid, barcode))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {'found': True, 'item_name': row[0], 'company': row[1], 'sub_brand': None, 'packing_size': None}
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
        cursor.execute('SELECT id, po_number, item_name, weight, ordered_qty, barcode, company, rate, status, po_date, delivery_date, tax_percent, approved_by, approved_at FROM po_items WHERE factory_id = %s AND company = %s ORDER BY po_number, id DESC', (fid, filter_company))
    else:
        cursor.execute('SELECT id, po_number, item_name, weight, ordered_qty, barcode, company, rate, status, po_date, delivery_date, tax_percent, approved_by, approved_at FROM po_items WHERE factory_id = %s ORDER BY po_number, id DESC', (fid,))
    rows = cursor.fetchall()
    cursor.execute('SELECT name FROM companies WHERE factory_id = %s ORDER BY name', (fid,))
    companies = [r[0] for r in cursor.fetchall()]

    # Group items by (company, po_number) so a whole wrongly-uploaded PO can be deleted in one go
    groups_map = {}
    order = []
    shortage_map = get_po_shortage_map(cursor, fid)
    for it in rows:
        key = (it[6] or '', it[1])
        if key not in groups_map:
            groups_map[key] = {'company': it[6] or '', 'po_number': it[1], 'rows': [], 'total_ordered': 0,
                                'has_rate_diff': False, 'has_draft': False, 'po_date': it[9], 'delivery_date': it[10],
                                'fully_received': True}
            order.append(key)
        rate_info = get_rate_status(cursor, fid, it[6], it[2], it[7])
        if rate_info['status'] == 'diff':
            groups_map[key]['has_rate_diff'] = True
        if (it[8] or 'Draft') == 'Draft':
            groups_map[key]['has_draft'] = True
        shortage_info = shortage_map.get((it[6] or '', it[1], it[2]), {'ordered': it[4] or 0, 'received': 0, 'pending': it[4] or 0, 'fulfilled': False})
        if not shortage_info['fulfilled']:
            groups_map[key]['fully_received'] = False
        groups_map[key]['rows'].append(it + (rate_info, shortage_info))
        groups_map[key]['total_ordered'] += it[4] or 0
    po_groups = [groups_map[k] for k in order]
    conn.close()

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

@app.route('/pos/approve_po', methods=['POST'])
def pos_approve_po():
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    po_number = request.form.get('po_number', '').strip()
    company = request.form.get('company', '').strip()
    conn = get_conn()
    cursor = conn.cursor()
    ts = now_ist().isoformat()
    cursor.execute("UPDATE po_items SET status = 'Approved', approved_by = %s, approved_at = %s WHERE factory_id = %s AND po_number = %s AND company = %s AND status = 'Draft'",
                   (session.get('user_name'), ts, fid, po_number, company))
    log_audit(cursor, 'PO Approved', 'Manage POs', po_number, f'{company}: PO {po_number} approved by {session.get("user_name")}')
    conn.commit()
    conn.close()
    return redirect('/pos?company=' + quote(company) if company else '/pos')

@app.route('/pos/reopen_po', methods=['POST'])
def pos_reopen_po():
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    po_number = request.form.get('po_number', '').strip()
    company = request.form.get('company', '').strip()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE po_items SET status = 'Draft', approved_by = NULL, approved_at = NULL WHERE factory_id = %s AND po_number = %s AND company = %s AND status = 'Approved'",
                   (fid, po_number, company))
    log_audit(cursor, 'PO Reopened for Edit', 'Manage POs', po_number, f'{company}: PO {po_number} reopened by {session.get("user_name")}')
    conn.commit()
    conn.close()
    return redirect('/pos?company=' + quote(company) if company else '/pos')

@app.route('/pos/edit/<int:item_id>', methods=['POST'])
def pos_edit_item(item_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    item_name = request.form.get('item_name', '').strip()
    weight = request.form.get('weight', '').strip()
    ordered_qty = request.form.get('ordered_qty', '').strip() or 0
    barcode = request.form.get('barcode', '').strip()
    rate_raw = request.form.get('rate', '').strip()
    rate = float(rate_raw) if rate_raw else None
    conn = get_conn()
    cursor = conn.cursor()
    # Only editable while still in Draft — an Approved item must be reopened first, so a final PO can't silently change.
    cursor.execute("UPDATE po_items SET item_name = %s, weight = %s, ordered_qty = %s, barcode = %s, rate = %s WHERE id = %s AND factory_id = %s AND status = 'Draft'",
                   (item_name, weight, int(ordered_qty), barcode, rate, item_id, fid))
    log_audit(cursor, 'PO Item Edited', 'Manage POs', item_id, f'Edited {item_name} ({ordered_qty})')
    conn.commit()
    conn.close()
    return redirect('/pos')

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
    rate_raw = request.form.get('rate', '').strip()
    rate = float(rate_raw) if rate_raw else None
    po_date = request.form.get('po_date', '').strip()
    delivery_date = request.form.get('delivery_date', '').strip()
    tax_raw = request.form.get('tax_percent', '').strip()
    tax_percent = float(tax_raw) if tax_raw else None
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('''INSERT INTO po_items (factory_id, po_number, item_name, weight, ordered_qty, barcode, company, rate, status, po_date, delivery_date, tax_percent)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'Draft', %s, %s, %s)''',
                   (fid, po_number, item_name, weight, int(ordered_qty), barcode, company, rate, po_date or None, delivery_date or None, tax_percent))
    log_audit(cursor, 'Product Updated', 'Manage POs', po_number, f'Added {item_name} ({ordered_qty}) to PO {po_number} (Draft)')
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
    'rate': ['rate', 'price', 'unit price', 'unit_price', 'po rate', 'po_rate', 'mrp', 'cost'],
    'po_date': ['po_date', 'po date', 'order date', 'order_date', 'date'],
    'delivery_date': ['delivery_date', 'delivery date', 'due date', 'due_date', 'expected delivery'],
    'tax_percent': ['tax_percent', 'tax', 'gst', 'tax %', 'gst %', 'gst_percent'],
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

@app.route('/pos/ai_import', methods=['GET', 'POST'])
def pos_ai_import():
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    if request.method == 'GET':
        cursor.execute('SELECT name FROM companies WHERE factory_id = %s ORDER BY name', (fid,))
        companies = [r[0] for r in cursor.fetchall()]
        conn.close()
        api_key_set = bool(os.environ.get('ANTHROPIC_API_KEY'))
        return render_template_string(AI_IMPORT_PO_HTML, companies=companies, api_key_set=api_key_set,
                                       import_type='po', action_url='/pos/ai_import')

    company = request.form.get('company', '').strip()
    file = request.files.get('doc_file')
    if not company or not file or file.filename == '':
        conn.close()
        return redirect('/pos/ai_import?error=' + quote('Please select a company and a file.'))

    filename_lower = file.filename.lower()
    if filename_lower.endswith('.pdf'):
        mimetype = 'application/pdf'
    elif filename_lower.endswith(('.jpg', '.jpeg')):
        mimetype = 'image/jpeg'
    elif filename_lower.endswith('.png'):
        mimetype = 'image/png'
    else:
        conn.close()
        return redirect('/pos/ai_import?error=' + quote('Please upload a PDF, JPG or PNG file.'))

    file_bytes = file.read()
    parsed, error = call_ai_extraction(file_bytes, mimetype, 'po')
    ts = now_ist().isoformat()
    if error:
        cursor.execute('''INSERT INTO ai_import_staging (factory_id, import_type, company, source_filename, extracted_json, status, error_message, created_at, created_by)
                           VALUES (%s,'PO',%s,%s,NULL,'failed',%s,%s,%s)''', (fid, company, file.filename, error, ts, session.get('user_name')))
        conn.commit()
        conn.close()
        return redirect('/pos/ai_import?error=' + quote(error))

    cursor.execute('''INSERT INTO ai_import_staging (factory_id, import_type, company, source_filename, extracted_json, status, created_at, created_by)
                       VALUES (%s,'PO',%s,%s,%s,'pending_review',%s,%s) RETURNING id''',
                   (fid, company, file.filename, json.dumps(parsed), ts, session.get('user_name')))
    staging_id = cursor.fetchone()[0]
    conn.commit()
    conn.close()
    return redirect(f'/pos/ai_import/review/{staging_id}')

@app.route('/pos/ai_import/review/<int:staging_id>')
def pos_ai_import_review(staging_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT company, source_filename, extracted_json, status FROM ai_import_staging WHERE id = %s AND factory_id = %s AND import_type = %s',
                   (staging_id, fid, 'PO'))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return "Import not found. <a href='/pos/ai_import'>Back</a>", 404
    company, filename, extracted_json, status = row
    extracted = json.loads(extracted_json) if extracted_json else {'items': []}
    return render_template_string(AI_IMPORT_REVIEW_PO_HTML, staging_id=staging_id, company=company,
                                   filename=filename, extracted=extracted, status=status)

@app.route('/pos/ai_import/review/<int:staging_id>/commit', methods=['POST'])
def pos_ai_import_commit(staging_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT company, status FROM ai_import_staging WHERE id = %s AND factory_id = %s AND import_type = %s', (staging_id, fid, 'PO'))
    row = cursor.fetchone()
    if not row or row[1] == 'committed':
        conn.close()
        return redirect('/pos/ai_import')
    company = row[0]
    po_number = request.form.get('po_number', '').strip()
    po_date = request.form.get('po_date', '').strip() or None
    delivery_date = request.form.get('delivery_date', '').strip() or None
    tax_raw = request.form.get('tax_percent', '').strip()
    tax_percent = float(tax_raw) if tax_raw else None
    item_names = request.form.getlist('item_name')
    weights = request.form.getlist('weight')
    qtys = request.form.getlist('ordered_qty')
    rates = request.form.getlist('rate')
    barcodes = request.form.getlist('barcode')
    includes = request.form.getlist('include')  # indices of rows the user kept checked

    if not po_number or not item_names:
        conn.close()
        return redirect(f'/pos/ai_import/review/{staging_id}?error=' + quote('PO number and at least one item are required.'))

    inserted = 0
    for i in range(len(item_names)):
        if str(i) not in includes:
            continue
        item_name = item_names[i].strip()
        if not item_name:
            continue
        try:
            ordered_qty = int(float(qtys[i])) if i < len(qtys) and qtys[i].strip() else 0
        except ValueError:
            ordered_qty = 0
        if ordered_qty <= 0:
            continue
        weight = weights[i].strip() if i < len(weights) else ''
        rate = float(rates[i]) if i < len(rates) and rates[i].strip() else None
        barcode = barcodes[i].strip() if i < len(barcodes) else ''
        cursor.execute('''INSERT INTO po_items (factory_id, po_number, item_name, weight, ordered_qty, barcode, company, rate, status, po_date, delivery_date, tax_percent)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'Draft',%s,%s,%s)''',
                       (fid, po_number, item_name, weight, ordered_qty, barcode, company, rate, po_date, delivery_date, tax_percent))
        inserted += 1

    cursor.execute("UPDATE ai_import_staging SET status = 'committed' WHERE id = %s", (staging_id,))
    log_audit(cursor, 'AI PO Import Committed', 'Manage POs', po_number, f'{company}: {inserted} item(s) imported as Draft from AI-scanned document')
    conn.commit()
    conn.close()
    return redirect('/pos?company=' + quote(company) + '&ok=1&msg=' + quote(f'{inserted} item(s) imported as Draft — review and approve.'))

@app.route('/pos/ai_import/review/<int:staging_id>/discard', methods=['POST'])
def pos_ai_import_discard(staging_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE ai_import_staging SET status = 'discarded' WHERE id = %s AND factory_id = %s AND import_type = 'PO'", (staging_id, fid))
    conn.commit()
    conn.close()
    return redirect('/pos/ai_import')

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
        rate_raw = (row.get(colmap['rate']) or '').strip() if 'rate' in colmap else ''
        rate = None
        if rate_raw:
            try:
                rate = float(rate_raw)
            except ValueError:
                rate = None
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
        po_date = (row.get(colmap['po_date']) or '').strip() if 'po_date' in colmap else ''
        delivery_date = (row.get(colmap['delivery_date']) or '').strip() if 'delivery_date' in colmap else ''
        tax_raw = (row.get(colmap['tax_percent']) or '').strip() if 'tax_percent' in colmap else ''
        tax_percent = None
        if tax_raw:
            try:
                tax_percent = float(tax_raw)
            except ValueError:
                tax_percent = None
        cursor.execute('''INSERT INTO po_items (factory_id, po_number, item_name, weight, ordered_qty, barcode, company, rate, status, po_date, delivery_date, tax_percent)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'Draft', %s, %s, %s)''',
                       (fid, po_number, item_name, weight, ordered_qty, barcode, company, rate, po_date or None, delivery_date or None, tax_percent))
        inserted += 1
    conn.commit()
    conn.close()

    msg = f'{inserted} items imported as Draft for {company} — review and approve before dispatch.'
    if skipped:
        msg += f' {skipped} rows skipped (incomplete data).'
    return redirect('/pos?company=' + quote(company) + '&ok=1&msg=' + quote(msg))

@app.route('/pos/delete/<int:item_id>', methods=['POST'])
def pos_delete(item_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT po_number, item_name FROM po_items WHERE id = %s AND factory_id = %s', (item_id, fid))
    prow = cursor.fetchone()
    cursor.execute('DELETE FROM po_items WHERE id = %s AND factory_id = %s', (item_id, fid))
    if prow:
        log_audit(cursor, 'PO Item Deleted', 'PO', prow[0], f'{prow[1]}')
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
    conn = get_conn()
    cursor = conn.cursor()
    log_audit(cursor, 'Dispatch Session Started', 'Dispatch', po_number, f'vehicle={vehicle_no}, location={location}')
    conn.commit()
    conn.close()
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
    if item:
        conn.close()
        return {'found': True, 'item_name': item[0], 'weight': item[1]}
    # Fall back to the Barcode Catalog so a worker can still scan+load an item even if it
    # isn't on the currently-active PO — matches the master workflow's "Loading Barcode Match" rule.
    cursor.execute('SELECT item_name, packing_size, company, sub_brand FROM barcode_catalog WHERE factory_id = %s AND barcode = %s', (fid, barcode))
    cat = cursor.fetchone()
    conn.close()
    if cat:
        return {'found': True, 'item_name': cat[0], 'weight': cat[1], 'company': cat[2], 'sub_brand': cat[3], 'from_catalog': True}
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
    cursor.execute('SELECT po_number, product_name FROM dispatch_log WHERE id = %s AND factory_id = %s', (log_id, fid))
    drow = cursor.fetchone()
    cursor.execute('UPDATE dispatch_log SET loaded_qty = %s WHERE id = %s AND factory_id = %s', (new_qty, log_id, fid))
    if drow:
        log_audit(cursor, 'Dispatch Load Edited', 'Dispatch', drow[0], f'{drow[1]}: qty -> {new_qty}')
    conn.commit()
    conn.close()
    return redirect('/')

@app.route('/dispatch/delete/<int:log_id>', methods=['POST'])
def dispatch_delete(log_id):
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT po_number, product_name FROM dispatch_log WHERE id = %s AND factory_id = %s', (log_id, fid))
    drow = cursor.fetchone()
    cursor.execute('DELETE FROM dispatch_log WHERE id = %s AND factory_id = %s', (log_id, fid))
    if drow:
        log_audit(cursor, 'Dispatch Load Deleted', 'Dispatch', drow[0], f'{drow[1]}')
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

# F1: minimum seconds between two ACCEPTED GPS pings for the same trip. Chosen well below the
# client's own ~15-20s natural ping interval (see TRACK_HTML's watchPosition/setInterval), so no
# legitimate driver update is ever throttled — this only blocks abnormally rapid/abusive bursts.
GPS_PING_MIN_INTERVAL_SECONDS = 5

def validate_gps_payload(lat, lng, accuracy):
    """Strict server-side validation for an incoming GPS ping. Returns (lat, lng, accuracy, error).
    error is None on success. Never raises — always returns a clean result either way."""
    try:
        lat = float(lat)
        lng = float(lng)
    except (TypeError, ValueError):
        return None, None, None, 'lat/lng must be numeric'
    if math.isnan(lat) or math.isinf(lat) or not (-90 <= lat <= 90):
        return None, None, None, 'latitude out of range'
    if math.isnan(lng) or math.isinf(lng) or not (-180 <= lng <= 180):
        return None, None, None, 'longitude out of range'
    acc = None
    if accuracy is not None:
        try:
            acc = float(accuracy)
        except (TypeError, ValueError):
            return None, None, None, 'accuracy must be numeric'
        if math.isnan(acc) or math.isinf(acc) or acc < 0:
            return None, None, None, 'accuracy invalid'
    return lat, lng, acc, None

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
                      'Delivered':'badge-green', 'Cancelled':'badge-amber', 'Available':'badge-green',
                      'In DC':'badge-amber'}

# --- DC Arrival & Return Automation config (configurable thresholds, per the master workflow spec) ---
DC_ARRIVAL_RADIUS_KM = 2        # vehicle must stay within this distance of the DC location to count as "at DC"
DC_ARRIVAL_WINDOW_MINUTES = 30  # ...continuously for this long before arrival is auto-confirmed
DC_RETURN_RADIUS_KM = 10        # once "In DC", vehicle must move at least this far from DC to auto-mark Available

def haversine_km(lat1, lng1, lat2, lng2):
    """Great-circle distance in km between two lat/lng points."""
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(min(1, math.sqrt(a)))

# G1: GPS jump/anomaly detection. Configurable via environment, not scattered/hardcoded — generous
# enough that normal highway travel (40-100 km/h) never trips it, while still catching physically
# impossible jumps (e.g. Delhi -> Mumbai in 2 minutes) by a wide margin.
GPS_JUMP_SPEED_LIMIT_KMPH = float(os.environ.get('GPS_JUMP_SPEED_LIMIT_KMPH', '150'))
GPS_POOR_ACCURACY_METERS = 1000  # advisory-only threshold — see detect_gps_anomaly() docstring

def detect_gps_anomaly(prev_lat, prev_lng, prev_ts, new_lat, new_lng, new_ts, accuracy):
    """Server-side-only GPS jump detection — never trusts any client-supplied speed/distance/flag
    (track_ping only ever reads lat/lng/accuracy from the client; this function only looks at
    values already validated and previously persisted by the server itself).

    Returns (is_suspicious, reason, speed_kmph):
    - prev_lat/prev_lng/prev_ts is None (trip's very first ping — no prior trusted point to compare
      against) -> never suspicious, speed_kmph is None (nothing to calculate against).
    - elapsed <= 0 (non-positive/clock-skew time delta) -> suspicious, speed not computed (avoids
      a division-by-zero and a meaningless/undefined instantaneous speed).
    - otherwise: speed = haversine distance / elapsed hours; suspicious only if it exceeds
      GPS_JUMP_SPEED_LIMIT_KMPH.

    Poor GPS accuracy is advisory-only, per confirmed scope: if accuracy is present and worse than
    GPS_POOR_ACCURACY_METERS, that fact is appended to the reason text ONLY when the ping is
    otherwise already suspicious on speed grounds — accuracy alone never makes a ping suspicious,
    so normal driver tracking in weak-signal areas is never broken by this.
    """
    if prev_lat is None or prev_lng is None or prev_ts is None:
        return False, None, None
    try:
        elapsed_seconds = (new_ts - prev_ts).total_seconds()
    except Exception:
        return False, None, None
    if elapsed_seconds <= 0:
        return True, 'invalid time delta (non-positive elapsed time since previous trusted point)', None
    distance_km = haversine_km(prev_lat, prev_lng, new_lat, new_lng)
    speed_kmph = distance_km / (elapsed_seconds / 3600.0)
    if speed_kmph > GPS_JUMP_SPEED_LIMIT_KMPH:
        reason = f'speed {speed_kmph:.0f} km/h exceeds {GPS_JUMP_SPEED_LIMIT_KMPH:.0f} km/h threshold over {distance_km:.1f}km / {elapsed_seconds:.0f}s'
        if accuracy is not None and accuracy > GPS_POOR_ACCURACY_METERS:
            reason += f' (also poor accuracy: {accuracy:.0f}m)'
        return True, reason, speed_kmph
    return False, None, speed_kmph

def evaluate_dc_automation(cursor, fid, trip_id, lat, lng, ts):
    """New, additive-only automation: auto-detects DC arrival (Loading/In Transit -> In DC) and DC
    return (In DC -> Available, via the existing 'Delivered' mechanism) purely from GPS pings.
    Does nothing at all unless the trip has an explicit DC location set — every existing trip that
    doesn't use this new optional field behaves exactly as before, unaffected."""
    cursor.execute('''SELECT trip_status, dc_latitude, dc_longitude, dc_window_start_at, dc_arrived_at, vehicle_number, vehicle_id
                       FROM trips WHERE id = %s AND factory_id = %s''', (trip_id, fid))
    row = cursor.fetchone()
    if not row:
        return
    trip_status, dc_lat, dc_lng, window_start_at, arrived_at, vehicle_number, veh_id = row
    if dc_lat is None or dc_lng is None:
        return  # no DC location captured for this trip — automation not applicable, nothing to do
    if trip_status not in ('In Transit', 'In DC'):
        return  # only monitor trips that are actually out on the road or already confirmed at DC

    dist_km = haversine_km(lat, lng, dc_lat, dc_lng)

    if trip_status == 'In Transit':
        # Phase 1: watching for a confirmed, continuous DC arrival
        if dist_km <= DC_ARRIVAL_RADIUS_KM:
            if not window_start_at:
                cursor.execute('UPDATE trips SET dc_window_start_at = %s WHERE id = %s', (ts, trip_id))
            else:
                try:
                    started = datetime.fromisoformat(window_start_at)
                    elapsed_minutes = (now_ist() - started).total_seconds() / 60.0
                except Exception:
                    elapsed_minutes = 0
                if elapsed_minutes >= DC_ARRIVAL_WINDOW_MINUTES:
                    cursor.execute("UPDATE trips SET trip_status = 'In DC', dc_arrived_at = %s WHERE id = %s", (ts, trip_id))
                    log_audit(cursor, 'Vehicle Arrived at DC', 'Vehicles', trip_id,
                               f'{vehicle_number}: auto-detected at DC (within {DC_ARRIVAL_RADIUS_KM}km for {DC_ARRIVAL_WINDOW_MINUTES}+ min)')
                    # N1: reuses this exact DC-arrival moment — no second/independent DC detection system.
                    create_driver_followup_if_needed(cursor, fid, trip_id, ts)
        else:
            if window_start_at:
                # Moved back out of the DC zone before the dwell window completed — restart the timer next time it's near
                cursor.execute('UPDATE trips SET dc_window_start_at = NULL WHERE id = %s', (trip_id,))

    elif trip_status == 'In DC':
        # Phase 2: watching for the vehicle to head back out beyond the return radius
        if dist_km >= DC_RETURN_RADIUS_KM:
            cursor.execute('''UPDATE trips SET trip_status = 'Delivered', delivered_at = %s, dc_returned_at = %s,
                               tracking_status = 'stopped' WHERE id = %s''', (ts, ts, trip_id))
            log_audit(cursor, 'Vehicle Returned - Auto Available', 'Vehicles', trip_id,
                       f'{vehicle_number}: auto-detected {DC_RETURN_RADIUS_KM}km+ from DC, now Available')
            if veh_id:
                maybe_auto_remove_outside_vehicle(cursor, fid, veh_id)
            # N1: trip is no longer active — no future calls/escalation for it.
            stop_driver_followup(cursor, trip_id, 'Trip auto-delivered (returned from DC)')

def recompute_trip_status(cursor, trip_id):
    fid = current_factory_id()
    cursor.execute('SELECT trip_status, vehicle_number FROM trips WHERE id = %s AND factory_id = %s', (trip_id, fid))
    row = cursor.fetchone()
    if not row: return
    current, vehicle_number = row
    if current in ('In Transit', 'Delivered', 'Cancelled', 'In DC'):
        return  # dispatched/delivered/cancelled/at-DC trips are not auto-changed by loading-scan logic
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

# ===========================================================================
# N1: DRIVER UNLOADING FOLLOW-UP (Exotel robo-call state machine)
# ===========================================================================
# Credentials are read ONLY from environment variables, never hardcoded. If any required Exotel
# variable is missing, N1 is gracefully disabled (create_driver_followup_if_needed still creates the
# DB row so the state machine/UI stay consistent, but exotel_initiate_call() simply records the
# missing-config error and never crashes the app or blocks any existing DC/delivery workflow).
EXOTEL_ACCOUNT_SID = os.environ.get('EXOTEL_ACCOUNT_SID')
EXOTEL_API_KEY = os.environ.get('EXOTEL_API_KEY')
EXOTEL_API_TOKEN = os.environ.get('EXOTEL_API_TOKEN')
EXOTEL_EXOPHONE = os.environ.get('EXOTEL_EXOPHONE')
EXOTEL_SUBDOMAIN = os.environ.get('EXOTEL_SUBDOMAIN')
# Only needed if the configured Exotel Flow/App requires an explicit App ID in the call request
# (typical for a "Connect to Flow" style call). Optional — some Exotel setups don't need it.
EXOTEL_APP_ID = os.environ.get('EXOTEL_APP_ID')
# Shared secret for the Render Cron Job / external scheduler to call process_due_driver_followups()
# over HTTP — see /internal/n1/process_due below. NOT a Flask session/login credential.
N1_CRON_SECRET = os.environ.get('N1_CRON_SECRET')

N1_FIRST_CALL_DELAY_HOURS = float(os.environ.get('N1_FIRST_CALL_DELAY_HOURS', '3'))
N1_SECOND_CALL_DELAY_HOURS = float(os.environ.get('N1_SECOND_CALL_DELAY_HOURS', '1'))

def exotel_configured():
    """FIX 1 (confirmed): previously this only checked the 5 core account credentials, which let
    exotel_initiate_call() attempt a 'configured' call even when APP_BASE_URL was missing (producing
    a broken/relative StatusCallback URL Exotel could never reach) or when EXOTEL_APP_ID was missing.
    N1's entire purpose is to ask the driver a question via an IVR flow — a Connect API call with no
    'Url' (i.e. no App/Flow) only bridges two numbers with no prompt/DTMF collection at all, which is
    useless for N1's business requirement. So EXOTEL_APP_ID is now required here too, not optional.
    All 7 variables must be present for N1 to consider itself configured; missing any one keeps N1 in
    the same graceful-disabled state as before (never a fake success, never a boot failure)."""
    return bool(EXOTEL_ACCOUNT_SID and EXOTEL_API_KEY and EXOTEL_API_TOKEN and EXOTEL_EXOPHONE
                and EXOTEL_SUBDOMAIN and EXOTEL_APP_ID and os.environ.get('APP_BASE_URL'))

def create_driver_followup_if_needed(cursor, fid, trip_id, dc_arrived_at_iso):
    """N1: idempotent — called every time a trip transitions to 'In DC'. The UNIQUE(trip_id)
    constraint on driver_followups is the real guarantee; this ON CONFLICT DO NOTHING makes repeated
    calls (e.g. if evaluate_dc_automation ever re-evaluates an already-arrived trip) silently no-op
    rather than erroring, so callers never need to pre-check existence themselves."""
    try:
        due = (datetime.fromisoformat(dc_arrived_at_iso) + timedelta(hours=N1_FIRST_CALL_DELAY_HOURS)).isoformat()
    except Exception:
        due = (now_ist() + timedelta(hours=N1_FIRST_CALL_DELAY_HOURS)).isoformat()
    ts = now_ist().isoformat()
    cursor.execute('''INSERT INTO driver_followups (trip_id, factory_id, status, dc_arrived_at, first_call_due_at,
                       next_action_at, created_at, updated_at)
                       VALUES (%s,%s,'CALL_1_DUE',%s,%s,%s,%s,%s) ON CONFLICT (trip_id) DO NOTHING RETURNING id''',
                   (trip_id, fid, dc_arrived_at_iso, due, due, ts, ts))
    row = cursor.fetchone()
    if row:
        log_audit_system(cursor, fid, 'N1 Follow-up Created', 'Vehicles', trip_id, f'First call due at {due}')

def stop_driver_followup(cursor, trip_id, reason):
    """N1 stop condition — called whenever a trip becomes Delivered/Cancelled/otherwise inactive.
    No-ops cleanly if no follow-up row exists (most trips never reach 'In DC' at all) or it's
    already stopped/completed. Never deletes the row — history is preserved for audit purposes."""
    cursor.execute("SELECT id, status, factory_id FROM driver_followups WHERE trip_id = %s", (trip_id,))
    row = cursor.fetchone()
    if not row or row[1] in ('STOPPED', 'COMPLETED', 'CALL_1_RESPONDED', 'CALL_2_RESPONDED'):
        return
    cursor.execute("UPDATE driver_followups SET status = 'STOPPED', next_action_at = NULL, updated_at = %s WHERE id = %s",
                   (now_ist().isoformat(), row[0]))
    log_audit_system(cursor, row[2], 'N1 Follow-up Stopped', 'Vehicles', trip_id, reason)

def normalize_driver_response(raw_text):
    """Maps whatever the Exotel flow reports (DTMF digit or transcribed/voice-intent text) to one of
    the app's fixed normalized values. Never trusts the raw text for anything beyond this mapping."""
    if not raw_text:
        return 'UNKNOWN'
    t = raw_text.strip().lower()
    if t in ('1',) or 'complet' in t:
        return 'UNLOADING_COMPLETED'
    if t in ('2',) or 'progress' in t or 'in progress' in t:
        return 'UNLOADING_IN_PROGRESS'
    if t in ('3',) or ('start' in t and 'not' not in t):
        return 'UNLOADING_STARTED'
    if t in ('4',) or 'wait' in t:
        return 'WAITING'
    if t in ('5',) or 'not start' in t or 'not_start' in t:
        return 'NOT_STARTED'
    return 'UNKNOWN'

def exotel_initiate_call(cursor, followup_id, trip_id, driver_mobile, call_leg):
    """N1: places one Exotel Connect-to-Flow (robo-)call. call_leg is 'first' or 'second'. Returns
    (call_sid_or_None, error_message_or_None). NEVER raises, never logs credentials — only the
    resulting call SID (a call reference, not a secret) is stored. Driver mobile is ALWAYS the
    server-held trips.driver_mobile value (passed in by the caller from the DB) — this function
    never accepts a phone number from any external/callback source.

    Verified against Exotel's official "Connect Number to Call Flow" API docs
    (developer.exotel.com/docs/voice-v1/api-reference/connect-to-flow), which is the correct API for
    N1 — it calls one number and, once answered, connects them into an IVR/App flow (unlike the
    separate "Connect Two Numbers" API, which bridges two real people and has no `Url`/flow concept
    at all). Per that confirmed spec:
      - `From` = the number to call first — this IS where the driver's number belongs for this API
        (there is no `To` field in the flow-based Connect call; `To` only applies to the different
        "Connect Two Numbers" API, which is not what N1 needs since it must play an IVR question).
      - `CallerId` = the ExoPhone shown to the driver as caller ID.
      - `Url` = the flow/App URL, format `http://my.exotel.com/{account_sid}/exoml/start_voice/{app_id}`
        — this is a FIXED domain (my.exotel.com), independent of the API subdomain/region used for
        the request itself (which is EXOTEL_SUBDOMAIN, e.g. api.exotel.com or api.in.exotel.com).
        The previous implementation incorrectly built this URL using EXOTEL_SUBDOMAIN instead of the
        fixed my.exotel.com domain — that is the specific bug this fix corrects.
    """
    if not re.match(r'^[0-9]{10}$', driver_mobile or ''):
        return None, 'Invalid driver mobile number on file for this trip'
    if not exotel_configured():
        return None, 'Exotel is not configured (missing environment variable(s)) — N1 is disabled until EXOTEL_ACCOUNT_SID/API_KEY/API_TOKEN/EXOPHONE/SUBDOMAIN/APP_ID/APP_BASE_URL are all set'
    url = f'https://{EXOTEL_API_KEY}:{EXOTEL_API_TOKEN}@{EXOTEL_SUBDOMAIN}/v1/Accounts/{EXOTEL_ACCOUNT_SID}/Calls/connect'
    payload = {
        'From': f'+91{driver_mobile}',  # E.164 format, matching the current official docs' example
        'CallerId': EXOTEL_EXOPHONE,
        'Url': f'http://my.exotel.com/{EXOTEL_ACCOUNT_SID}/exoml/start_voice/{EXOTEL_APP_ID}',  # fixed domain per official docs, not EXOTEL_SUBDOMAIN
        'CallType': 'trans',
        'StatusCallback': f'{os.environ.get("APP_BASE_URL", "")}/exotel/callback',
        'StatusCallbackContentType': 'application/x-www-form-urlencoded',
        'StatusCallbackEvents[0]': 'terminal',  # explicit, matching every confirmed official docs example that uses StatusCallback
        'CustomField': f'followup_id={followup_id}&leg={call_leg}',
    }
    try:
        resp = requests.post(url, data=payload, timeout=15)
        if resp.status_code not in (200, 201, 202):
            return None, f'Exotel API returned status {resp.status_code}'
        data = resp.json()
        call_sid = (data.get('Call') or {}).get('Sid')
        if not call_sid:
            return None, 'Exotel API did not return a call SID'
        return call_sid, None
    except Exception as e:
        # Never include the request URL (embeds API key/token) in any stored error message.
        return None, f'Exotel call request failed: {type(e).__name__}'

def send_whatsapp_escalation(cursor, factory_id, followup_id, trip_id, message):
    """N1 WhatsApp escalation abstraction. As audited (F1/N1): this app has NO real WhatsApp
    Business API integration today — only a manual wa.me share-link exists elsewhere for driver
    tracking links. Sending an automated GROUP message additionally requires a WhatsApp Business API
    provider (Meta Cloud API / Twilio / Gupshup etc.) with its own number, template approval, and
    credentials, none of which exist in this codebase. This function is the integration POINT for
    that — it does NOT fake a successful send. It records the composed message in the audit log
    (for a human to manually relay) and returns False, so the caller/report is never misled into
    thinking automated delivery happened."""
    log_audit_system(cursor, factory_id, 'N1 WhatsApp Escalation (Manual Send Required)', 'Vehicles', trip_id,
               f'No WhatsApp Business API configured — message not auto-sent. Composed message: {message}')
    return False

def build_escalation_message(vehicle_number, trip_id, driver_name, dc_arrived_at):
    return (f"N1 UNLOADING ALERT\nVehicle: {vehicle_number}\nTrip: {trip_id}\nDriver: {driver_name or '—'}\n"
            f"DC Arrival: {dc_arrived_at or '—'}\nFirst call: No response\nSecond call: No response\n"
            f"Action required: Please check unloading status.")

def process_due_driver_followups():
    """N1 scheduler entry point — safe to call from multiple workers/processes concurrently (each
    due row is locked with SELECT ... FOR UPDATE before any action, so two simultaneous callers can
    never both act on the same row). Intended to be invoked periodically by an external scheduler
    (Render Cron Job hitting /internal/n1/process_due) — see final report for exact setup. Processes
    ALL currently-due rows in one call; continues past any single row's failure."""
    conn = get_conn()
    cursor = conn.cursor()
    now_iso = now_ist().isoformat()
    cursor.execute("""SELECT id FROM driver_followups WHERE status IN ('CALL_1_DUE','WAIT_1_HOUR')
                       AND next_action_at IS NOT NULL AND next_action_at <= %s""", (now_iso,))
    due_ids = [r[0] for r in cursor.fetchall()]
    conn.close()
    results = []
    for fid_row in due_ids:
        try:
            results.append(_process_one_due_followup(fid_row))
        except Exception as e:
            results.append({'id': fid_row, 'error': str(type(e).__name__)})
    return results

def _process_one_due_followup(followup_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM driver_followups WHERE id = %s FOR UPDATE', (followup_id,))
    cols = [d[0] for d in cursor.description]
    row = cursor.fetchone()
    if not row:
        conn.close()
        return {'id': followup_id, 'skipped': 'not found'}
    f = dict(zip(cols, row))
    # Re-check status/next_action_at AFTER acquiring the lock — another worker may have already
    # processed this row between our SELECT (above, unlocked) and this lock being granted.
    now_iso = now_ist().isoformat()
    if f['status'] not in ('CALL_1_DUE', 'WAIT_1_HOUR') or not f['next_action_at'] or f['next_action_at'] > now_iso:
        conn.close()
        return {'id': followup_id, 'skipped': 'no longer due'}
    cursor.execute('SELECT vehicle_number, driver_name, driver_mobile, trip_status FROM trips WHERE id = %s', (f['trip_id'],))
    trow = cursor.fetchone()
    if not trow or trow[3] in ('Delivered', 'Cancelled'):
        cursor.execute("UPDATE driver_followups SET status='STOPPED', next_action_at=NULL, updated_at=%s WHERE id=%s", (now_iso, followup_id))
        log_audit_system(cursor, f['factory_id'], 'N1 Follow-up Stopped', 'Vehicles', f['trip_id'], 'Trip no longer active at processing time')
        conn.commit(); conn.close()
        return {'id': followup_id, 'stopped': True}
    vehicle_number, driver_name, driver_mobile, _ = trow

    if f['status'] == 'CALL_1_DUE':
        cursor.execute("UPDATE driver_followups SET status='CALL_1_SENT', first_call_at=%s, updated_at=%s WHERE id=%s", (now_iso, now_iso, followup_id))
        conn.commit()
        call_sid, err = exotel_initiate_call(cursor, followup_id, f['trip_id'], driver_mobile, 'first')
        cursor.execute("UPDATE driver_followups SET first_call_sid=%s, first_call_status=%s, last_error=%s, updated_at=%s WHERE id=%s",
                       (call_sid, 'initiated' if call_sid else 'failed', err, now_ist().isoformat(), followup_id))
        if call_sid:
            log_audit_system(cursor, f['factory_id'], 'N1 Call 1 Initiated', 'Vehicles', f['trip_id'], f'{vehicle_number}: SID recorded')
        else:
            log_audit_system(cursor, f['factory_id'], 'N1 Call Failed', 'Vehicles', f['trip_id'], f'{vehicle_number}: first call — {err}')
            # Leave it CALL_1_SENT with no SID; callback can never arrive for a failed dial, so the
            # cron will need manual attention — but we don't silently retry-loop it.
        conn.commit(); conn.close()
        return {'id': followup_id, 'action': 'call_1_initiated', 'sid': call_sid, 'error': err}

    elif f['status'] == 'WAIT_1_HOUR':
        cursor.execute("UPDATE driver_followups SET status='CALL_2_SENT', second_call_at=%s, updated_at=%s WHERE id=%s", (now_iso, now_iso, followup_id))
        conn.commit()
        call_sid, err = exotel_initiate_call(cursor, followup_id, f['trip_id'], driver_mobile, 'second')
        cursor.execute("UPDATE driver_followups SET second_call_sid=%s, second_call_status=%s, last_error=%s, updated_at=%s WHERE id=%s",
                       (call_sid, 'initiated' if call_sid else 'failed', err, now_ist().isoformat(), followup_id))
        if call_sid:
            log_audit_system(cursor, f['factory_id'], 'N1 Call 2 Initiated', 'Vehicles', f['trip_id'], f'{vehicle_number}: SID recorded')
        else:
            log_audit_system(cursor, f['factory_id'], 'N1 Call Failed', 'Vehicles', f['trip_id'], f'{vehicle_number}: second call — {err}')
        conn.commit(); conn.close()
        return {'id': followup_id, 'action': 'call_2_initiated', 'sid': call_sid, 'error': err}

    conn.close()
    return {'id': followup_id, 'skipped': 'unhandled status'}

def get_or_create_vehicle(cursor, vehicle_number):
    fid = current_factory_id()
    cursor.execute('SELECT id, is_active FROM vehicle_master WHERE factory_id = %s AND vehicle_number = %s', (fid, vehicle_number))
    row = cursor.fetchone()
    if row:
        # If this vehicle_number was previously auto-removed (Outside Vehicle after a completed
        # trip), reusing the same number for a new trip naturally brings it back — matches how an
        # outside vendor's truck genuinely returns for another job. Its vehicle_type is preserved.
        if row[1] is False:
            cursor.execute('UPDATE vehicle_master SET is_active = TRUE, updated_at = %s WHERE id = %s', (now_ist().isoformat(), row[0]))
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

def maybe_auto_remove_outside_vehicle(cursor, fid, vehicle_id):
    """Vehicle Type/Ownership: called whenever a trip transitions to Delivered (manual or the DC
    auto-return path). If the vehicle is an 'Outside Vehicle' and has no OTHER active trip right
    now, soft-removes it (is_active=False) from the active Vehicle Master list. This never deletes
    anything — the vehicle_master row, its trips, audit entries and location history all remain
    exactly as they are; it's purely a visibility/list flag, and get_or_create_vehicle above
    automatically reactivates it if the same vehicle_number is used again later. Company Vehicles
    are completely unaffected by this function — it does nothing for them."""
    if not vehicle_id:
        return
    cursor.execute("SELECT vehicle_type, vehicle_number, is_active FROM vehicle_master WHERE id = %s AND factory_id = %s", (vehicle_id, fid))
    row = cursor.fetchone()
    if not row or row[0] != 'Outside Vehicle' or row[2] is False:
        return  # not an Outside Vehicle, or already inactive — nothing to do
    vehicle_number = row[1]
    cursor.execute("SELECT COUNT(*) FROM trips WHERE vehicle_id = %s AND factory_id = %s AND trip_status NOT IN ('Delivered','Cancelled')",
                   (vehicle_id, fid))
    if cursor.fetchone()[0] > 0:
        return  # still has another active/incomplete trip — never auto-remove while that's true
    cursor.execute('UPDATE vehicle_master SET is_active = FALSE, updated_at = %s WHERE id = %s', (now_ist().isoformat(), vehicle_id))
    log_audit(cursor, 'Outside Vehicle Automatically Removed', 'Vehicles', vehicle_id,
               f'{vehicle_number}: auto-removed from active Vehicle Master after trip delivered (no other active trips)')

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

                <label style="margin-top:12px; margin-bottom:4px; display:block; font-size:12.5px; font-weight:600;">Vehicle Type — required</label>
                <div style="display:flex; gap:14px; font-size:13px; margin-bottom:10px;">
                    <label style="display:flex; align-items:center; gap:6px; font-weight:400; margin:0;">
                        <input type="radio" name="vehicle_type" value="Company Vehicle" required style="width:auto; margin:0;"> Company Vehicle
                    </label>
                    <label style="display:flex; align-items:center; gap:6px; font-weight:400; margin:0;">
                        <input type="radio" name="vehicle_type" value="Outside Vehicle" required style="width:auto; margin:0;"> Outside Vehicle
                    </label>
                </div>

                <label style="margin-bottom:4px; display:block; font-size:12.5px; font-weight:600;">Tracking Source</label>
                <div style="display:flex; gap:14px; font-size:13px; margin-bottom:6px;">
                    <label style="display:flex; align-items:center; gap:6px; font-weight:400; margin:0;">
                        <input type="radio" name="tracking_mode" value="Driver Mobile" checked style="width:auto; margin:0;"> Driver Mobile
                    </label>
                    <label style="display:flex; align-items:center; gap:6px; font-weight:400; margin:0;">
                        <input type="radio" name="tracking_mode" value="GPS Device" style="width:auto; margin:0;"> GPS Device
                    </label>
                </div>
                <input type="text" name="gps_device_id" placeholder="GPS Device ID (optional, only if GPS Device)" style="margin-top:0;">

                <button type="submit" class="btn btn-block" style="margin-top:10px;">Add Vehicle</button>
            </form>
            <button class="btn btn-outline btn-block" style="margin-top:8px;" onclick="document.getElementById('addVehicleModal').style.display='none'">Close</button>
        </div>
    </div>


    <div class="stats-grid">
        <a href="/vehicles{% if q %}?q={{ q }}{% endif %}" style="text-decoration:none; color:inherit;">
        <div class="stat-card" style="cursor:pointer; {% if not status_filter %}border-color:var(--primary);{% endif %}"><div class="icon">🚛</div><div class="label">Total Vehicles</div><div class="value">{{ summary.total }}</div></div>
        </a>
        <a href="/vehicles?status=Loading{% if q %}&q={{ q }}{% endif %}" style="text-decoration:none; color:inherit;">
        <div class="stat-card" style="cursor:pointer; {% if status_filter == 'Loading' %}border-color:var(--primary);{% endif %}"><div class="icon">📦</div><div class="label">Loading</div><div class="value">{{ summary.loading }}</div></div>
        </a>
        <a href="/vehicles?status=In Transit{% if q %}&q={{ q }}{% endif %}" style="text-decoration:none; color:inherit;">
        <div class="stat-card" style="cursor:pointer; {% if status_filter == 'In Transit' %}border-color:var(--primary);{% endif %}"><div class="icon">🛣️</div><div class="label">In Transit</div><div class="value">{{ summary.transit }}</div></div>
        </a>
        <a href="/vehicles?status=Available{% if q %}&q={{ q }}{% endif %}" style="text-decoration:none; color:inherit;">
        <div class="stat-card" style="cursor:pointer; {% if status_filter == 'Available' %}border-color:var(--primary);{% endif %}"><div class="icon">🟢</div><div class="label">Available</div><div class="value">{{ summary.available }}</div></div>
        </a>
        <a href="/vehicles?status=Offline{% if q %}&q={{ q }}{% endif %}" style="text-decoration:none; color:inherit;">
        <div class="stat-card" style="cursor:pointer; {% if status_filter == 'Offline' %}border-color:var(--primary);{% endif %}"><div class="icon">📴</div><div class="label">Location Offline</div><div class="value">{{ summary.offline }}</div></div>
        </a>
    </div>

    <div class="card">
        <div class="card-header"><h2>🗺️ Live Vehicle Map</h2></div>
        <div id="liveMap" style="height:340px; border-radius:12px; overflow:hidden;"></div>
        <div style="font-size:12px; color:var(--text-dim); margin-top:8px;">🟢 live &amp; fresh &nbsp; 🟡 live but a few min old &nbsp; ⚪ last known location only</div>
    </div>

    <div class="card">
        <div class="card-header"><h2>🔍 {% if status_filter %}{{ status_filter }} Vehicles{% else %}All Vehicles{% endif %}</h2>
            {% if status_filter %}<a href="/vehicles{% if q %}?q={{ q }}{% endif %}" class="btn btn-outline btn-sm">✕ Clear Filter</a>{% endif %}
        </div>
        <form method="GET" action="/vehicles" style="margin-bottom:14px;">
            {% if status_filter %}<input type="hidden" name="status" value="{{ status_filter }}">{% endif %}
            <input type="text" name="q" value="{{ q }}" placeholder="Search Vehicle Number, Driver Name or Mobile...">
        </form>
        <table class="table">
            <tr><th>Vehicle No.</th><th>Type</th><th>Driver</th><th>Mobile</th><th>Status</th><th>Current / Last Location</th><th>Last Updated</th><th></th></tr>
            {% for veh in vehicles %}
            <tr>
                <td>{{ veh.vehicle_number }}</td>
                <td><span class="badge {% if veh.vehicle_type == 'Outside Vehicle' %}badge-amber{% else %}badge-blue{% endif %}">{{ veh.vehicle_type }}</span></td>
                <td>{{ veh.driver_name or '—' }}</td>
                <td>{{ veh.driver_mobile or '—' }}</td>
                <td><span class="badge {{ veh.status_class }}">{{ veh.status }}</span></td>
                <td>{% if veh.has_location %}{{ veh.lat_display }}, {{ veh.lng_display }}{% else %}No location yet{% endif %}</td>
                <td><span style="color:{{ veh.freshness_color }};">●</span> {{ veh.freshness_label }}</td>
                <td style="display:flex; gap:6px;">
                    <a href="/vehicles/{{ veh.id }}" class="btn btn-outline btn-sm">Open</a>
                    {% if not veh.has_active_trip %}
                    <form method="POST" action="/vehicles/{{ veh.id }}/delete" onsubmit="return confirm('Remove {{ veh.vehicle_number }} from Vehicle Master? Its trip history stays intact.');" style="margin:0;">
                        <button type="submit" class="btn btn-danger btn-sm">Remove</button>
                    </form>
                    {% endif %}
                </td>
            </tr>
            {% endfor %}
            {% if not vehicles %}<tr><td colspan="8" style="text-align:center; color:var(--text-dim); padding:20px;">No vehicles found.</td></tr>{% endif %}
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
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css">
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

            <div style="margin-top:18px; padding:16px; background:rgba(255,255,255,0.03); border-radius:12px;">
                <label style="margin-top:0;">📍 DC / Unloading Location — optional</label>
                <div style="color:var(--text-muted); font-size:12px; margin-bottom:10px;">
                    Set this to enable automatic arrival &amp; return detection: the system will auto-mark the vehicle
                    "In DC" once it's near this point for {{ dc_arrival_window_minutes }}+ minutes, and auto-mark it
                    Available again once it's {{ dc_return_radius_km }}km+ away from here on the way back.
                    Leave this blank to keep everything working exactly as before, with only manual Dispatch/Deliver.
                </div>
                <input type="text" name="dc_location_name" placeholder="DC / drop location name (optional label)" style="margin-bottom:8px;">
                <div id="dcPickerMap" style="height:220px; border-radius:10px; margin-bottom:8px;"></div>
                <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
                    <button type="button" class="btn btn-outline btn-sm" onclick="useMyLocationForDc()">📍 Use My Current Location</button>
                    <span id="dcPickedLabel" style="font-size:12px; color:var(--text-muted);">Tap the map to set the DC location, or leave blank to skip automation.</span>
                </div>
                <input type="hidden" name="dc_latitude" id="dcLatField">
                <input type="hidden" name="dc_longitude" id="dcLngField">
            </div>

            <button type="submit" class="btn btn-primary btn-block" style="margin-top:14px;">Start Loading</button>
        </form>
    </div>
</div>
<script>
const dcMap = L.map('dcPickerMap').setView([20.5937, 78.9629], 5);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '&copy; OpenStreetMap' }).addTo(dcMap);
let dcMarker = null;
function setDcPoint(lat, lng) {
    document.getElementById('dcLatField').value = lat;
    document.getElementById('dcLngField').value = lng;
    document.getElementById('dcPickedLabel').textContent = 'DC location set: ' + lat.toFixed(5) + ', ' + lng.toFixed(5);
    if (dcMarker) { dcMarker.setLatLng([lat, lng]); } else { dcMarker = L.marker([lat, lng]).addTo(dcMap); }
    dcMap.setView([lat, lng], 13);
}
dcMap.on('click', (e) => setDcPoint(e.latlng.lat, e.latlng.lng));
function useMyLocationForDc() {
    if (!navigator.geolocation) { alert('Geolocation not supported on this device.'); return; }
    navigator.geolocation.getCurrentPosition(
        (pos) => setDcPoint(pos.coords.latitude, pos.coords.longitude),
        () => alert('Could not get current location. Tap the map instead.')
    );
}
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
        {% if error_msg %}<div class="badge badge-amber" style="display:block; padding:10px 14px; margin-bottom:14px; font-size:13px;">{{ error_msg }}</div>{% endif %}
        <div class="form-grid">
            <div><strong>Driver (current/last trip):</strong> {{ veh.driver_name or '—' }}</div>
            <div><strong>Mobile:</strong> {{ veh.driver_mobile or '—' }}</div>
            <div><strong>GPS Status:</strong> {{ veh.mode }}</div>
            <div><strong>Vehicle Type:</strong> <span class="badge {% if veh.vehicle_type == 'Outside Vehicle' %}badge-amber{% else %}badge-blue{% endif %}">{{ veh.vehicle_type }}</span></div>
            <div><strong>Tracking Source:</strong> {{ veh.tracking_mode }}{% if veh.gps_device_id %} ({{ veh.gps_device_id }}){% endif %}</div>
        </div>
        <details style="margin-top:12px;">
            <summary style="cursor:pointer; color:var(--primary); font-size:13px;">✏️ Edit Vehicle Type / Tracking Source</summary>
            <form method="POST" action="/vehicles/{{ veh.id }}/edit" style="margin-top:10px; display:flex; gap:14px; flex-wrap:wrap; align-items:flex-end;">
                <div>
                    <label style="font-size:11px;">Vehicle Type</label>
                    <select name="vehicle_type" style="margin:0;">
                        <option value="Company Vehicle" {% if veh.vehicle_type == 'Company Vehicle' %}selected{% endif %}>Company Vehicle</option>
                        <option value="Outside Vehicle" {% if veh.vehicle_type == 'Outside Vehicle' %}selected{% endif %}>Outside Vehicle</option>
                    </select>
                </div>
                <div>
                    <label style="font-size:11px;">Tracking Source</label>
                    <select name="tracking_mode" style="margin:0;">
                        <option value="Driver Mobile" {% if veh.tracking_mode == 'Driver Mobile' %}selected{% endif %}>Driver Mobile</option>
                        <option value="GPS Device" {% if veh.tracking_mode == 'GPS Device' %}selected{% endif %}>GPS Device</option>
                    </select>
                </div>
                <div>
                    <label style="font-size:11px;">GPS Device ID</label>
                    <input type="text" name="gps_device_id" value="{{ veh.gps_device_id or '' }}" style="margin:0;">
                </div>
                <button type="submit" class="btn btn-sm">Save</button>
            </form>
        </details>
        {% if not veh.has_active_trip %}
        <form method="POST" action="/vehicles/{{ veh.id }}/delete" onsubmit="return confirm('Remove {{ veh.vehicle_number }} from Vehicle Master? Its trip history stays intact.');" style="margin-top:10px;">
            <button type="submit" class="btn btn-danger btn-sm">🗑 Remove Vehicle</button>
        </form>
        {% endif %}
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
            <tr><th>Date</th><th>Time</th><th>Lat</th><th>Lng</th><th></th></tr>
            {% for h in location_history %}
            <tr><td>{{ h.date }}</td><td>{{ h.time }}</td><td>{{ h.lat }}</td><td>{{ h.lng }}</td>
                <td>{% if h.is_suspicious %}<span class="badge badge-amber">⚠ Suspicious GPS</span>{% endif %}</td></tr>
            {% endfor %}
            {% if not location_history %}<tr><td colspan="5" style="text-align:center; color:var(--text-dim); padding:20px;">Koi location history nahi hai.</td></tr>{% endif %}
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
        {% if t.dc_latitude %}
        <div class="form-grid" style="margin-top:10px;">
            <div><strong>📍 DC Location:</strong> {{ t.dc_location_name or 'Set on map' }} ({{ '%.5f'|format(t.dc_latitude) }}, {{ '%.5f'|format(t.dc_longitude) }})</div>
            <div><strong>DC Auto-Status:</strong>
                {% if t.trip_status == 'In DC' %}<span class="badge badge-amber">🏭 In DC since {{ t.dc_arrived_at }}</span>
                {% elif t.dc_returned_at %}<span class="badge badge-green">✅ Returned/Available at {{ t.dc_returned_at }}</span>
                {% else %}<span style="color:var(--text-muted); font-size:12px;">Watching for arrival (auto)</span>
                {% endif %}
            </div>
        </div>
        {% endif %}
        <div style="margin-top:14px; display:flex; gap:8px; flex-wrap:wrap;">
            {% if t.trip_status not in ['In Transit','In DC','Delivered','Cancelled'] %}
                <form method="POST" action="/trips/{{ t.id }}/regen_token" style="display:inline;"><button class="btn btn-outline btn-sm">🔗 Generate/Reset Tracking Link</button></form>
                {% if t.tracking_token %}
                <button class="btn btn-outline btn-sm" onclick="navigator.clipboard.writeText('{{ track_url }}'); alert('Link copied!');">📋 Copy Tracking Link</button>
                <a class="btn btn-outline btn-sm" target="_blank" href="https://wa.me/91{{ t.driver_mobile }}?text={{ track_url_encoded }}">📲 Share on WhatsApp</a>
                {% endif %}
                {% if t.tracking_status == 'active' %}
                <form method="POST" action="/trips/{{ t.id }}/stop_tracking" style="display:inline;"><button class="btn btn-danger btn-sm">⛔ Stop Tracking</button></form>
                {% endif %}
            {% endif %}
            {% if t.tracking_token and t.trip_status not in ['Delivered','Cancelled'] %}
                <form method="POST" action="/trips/{{ t.id }}/resend_link" style="display:inline;"><button class="btn btn-outline btn-sm">🔁 Resend Tracking Link</button></form>
            {% endif %}
            {% if t.trip_status == 'Ready to Dispatch' %}
                <form method="POST" action="/trips/{{ t.id }}/dispatch" style="display:inline;"><button class="btn btn-primary btn-sm">🚀 Dispatch Vehicle</button></form>
            {% endif %}
            {% if t.trip_status in ['In Transit','In DC'] %}
                <form method="POST" action="/trips/{{ t.id }}/deliver" style="display:inline;"><button class="btn btn-primary btn-sm">✅ Mark Delivered</button></form>
            {% endif %}
        </div>
    </div>

    {% if n1 %}
    <div class="card">
        <div class="card-header"><h2>📞 N1 Unloading Follow-up</h2></div>
        <div class="form-grid">
            <div><strong>Current Status:</strong> {{ n1.status }}</div>
            <div><strong>DC Arrived:</strong> {{ t.dc_arrived_at or '—' }}</div>
            <div><strong>First Call:</strong> {{ n1.first_call_at or 'Not yet' }} ({{ n1.first_call_status or '—' }})</div>
            <div><strong>First Call Result:</strong> {{ n1.first_call_response or '—' }}</div>
            <div><strong>Second Call:</strong> {{ n1.second_call_at or 'Not yet' }} ({{ n1.second_call_status or '—' }})</div>
            <div><strong>Second Call Result:</strong> {{ n1.second_call_response or '—' }}</div>
            <div><strong>Next Action:</strong> {{ n1.next_action_at or '—' }}</div>
            <div><strong>WhatsApp Escalation:</strong> {{ n1.whatsapp_escalated_at or 'Not escalated' }}</div>
        </div>
        {% if n1.status in ['CALL_1_DUE', 'WAIT_1_HOUR'] %}
        <form method="POST" action="/trips/{{ t.id }}/n1/call_now" style="margin-top:10px;">
            <button type="submit" class="btn btn-outline btn-sm">📞 Call Now (test)</button>
        </form>
        {% endif %}
    </div>
    {% endif %}

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
                    {% if t.trip_status not in ['In Transit','In DC','Delivered','Cancelled'] %}
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
        {% if t.trip_status not in ['In Transit','In DC','Delivered','Cancelled'] %}
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
    status_filter = request.args.get('status', '').strip()  # '', 'Loading', 'In Transit', 'Available', 'Offline'
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('''SELECT id, vehicle_number, current_latitude, current_longitude, last_location_at, vehicle_type, tracking_mode
                       FROM vehicle_master WHERE factory_id = %s AND is_active = TRUE ORDER BY vehicle_number''', (fid,))
    rows = cursor.fetchall()
    vehicles, map_markers = [], []
    summary = {'total': 0, 'loading': 0, 'transit': 0, 'available': 0, 'offline': 0}
    for vid, vnum, lat, lng, last_loc, vehicle_type, tracking_mode in rows:
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

        # Clicking a dashboard card filters this same list by the identical categorization used for its count above
        if status_filter == 'Offline' and not gps_offline:
            continue
        elif status_filter and status_filter != 'Offline' and status != status_filter:
            continue

        d = {'id': vid, 'vehicle_number': vnum, 'driver_name': driver_name, 'driver_mobile': driver_mobile,
             'status': status, 'status_class': TRIP_STATUS_BADGE.get(status, 'badge-amber'),
             'has_location': lat is not None, 'lat_display': lat, 'lng_display': lng,
             'freshness_label': label, 'freshness_color': color if lat is not None else 'grey',
             'vehicle_type': vehicle_type or 'Company Vehicle', 'tracking_mode': tracking_mode or 'Driver Mobile',
             'has_active_trip': active_trip_id is not None}
        vehicles.append(d)
        if lat is not None:
            map_markers.append({'id': vid, 'lat': lat, 'lng': lng, 'vehicle_number': vnum, 'driver_name': driver_name,
                                 'driver_mobile': driver_mobile, 'status': status, 'po_count': po_count,
                                 'color': 'green' if (is_live and secs <= 60) else ('yellow' if (is_live and secs <= 600) else 'grey'),
                                 'mode': mode, 'freshness': label})
    conn.commit()
    conn.close()
    return render_template_string(VEHICLES_LIST_HTML, vehicles=vehicles, summary=summary, map_markers=map_markers,
                                   q=request.args.get('q', ''), add_vehicle_error=request.args.get('add_error', ''),
                                   status_filter=status_filter)

@app.route('/vehicles/add', methods=['POST'])
def vehicles_add():
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    vehicle_number = request.form.get('vehicle_number', '').strip().upper()
    vehicle_type = request.form.get('vehicle_type', '').strip()
    tracking_mode = request.form.get('tracking_mode', '').strip() or 'Driver Mobile'
    gps_device_id = request.form.get('gps_device_id', '').strip() or None
    if not vehicle_number:
        return redirect('/vehicles?add_error=' + quote('Vehicle number zaroori hai.'))
    # No silent default: the person must explicitly pick one of the two options.
    if vehicle_type not in ('Company Vehicle', 'Outside Vehicle'):
        return redirect('/vehicles?add_error=' + quote('Please select a Vehicle Type — Company Vehicle or Outside Vehicle.'))
    if tracking_mode not in ('Driver Mobile', 'GPS Device'):
        tracking_mode = 'Driver Mobile'
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT id, is_active FROM vehicle_master WHERE factory_id = %s AND vehicle_number = %s', (fid, vehicle_number))
    existing = cursor.fetchone()
    if existing and existing[1] is not False:
        conn.close()
        return redirect('/vehicles?add_error=' + quote(f'Vehicle {vehicle_number} pehle se system mein hai.'))
    ok, limit_msg = check_usage_limit(cursor, 'vehicle')
    if not ok:
        conn.close()
        return redirect('/vehicles?add_error=' + quote(limit_msg))
    ts = now_ist().isoformat()
    if existing:
        # Was previously auto-removed (Outside Vehicle) — reactivate rather than error out, and
        # let this submission set its type/tracking fresh.
        vehicle_id = existing[0]
        cursor.execute('''UPDATE vehicle_master SET is_active = TRUE, vehicle_type = %s, tracking_mode = %s,
                           gps_device_id = %s, updated_at = %s WHERE id = %s''',
                       (vehicle_type, tracking_mode, gps_device_id, ts, vehicle_id))
    else:
        cursor.execute('''INSERT INTO vehicle_master (factory_id, vehicle_number, vehicle_type, tracking_mode, gps_device_id, created_at, updated_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
                       (fid, vehicle_number, vehicle_type, tracking_mode, gps_device_id, ts, ts))
        vehicle_id = cursor.fetchone()[0]
    log_audit(cursor, 'Vehicle Created', 'Vehicles', vehicle_id, f'Added vehicle {vehicle_number} ({vehicle_type}, tracking: {tracking_mode})')
    conn.commit()
    conn.close()
    return redirect(f'/vehicles/{vehicle_id}')

@app.route('/vehicles/<int:vehicle_id>/edit', methods=['POST'])
def vehicles_edit(vehicle_id):
    """Lets an authorized user update a vehicle's Type/Tracking configuration after creation
    (e.g. correcting a mis-selected Vehicle Type, or switching GPS Device <-> Driver Mobile)."""
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    vehicle_type = request.form.get('vehicle_type', '').strip()
    tracking_mode = request.form.get('tracking_mode', '').strip()
    gps_device_id = request.form.get('gps_device_id', '').strip() or None
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT vehicle_number, vehicle_type, tracking_mode FROM vehicle_master WHERE id = %s AND factory_id = %s', (vehicle_id, fid))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return redirect('/vehicles')
    vehicle_number, old_type, old_mode = row
    if vehicle_type not in ('Company Vehicle', 'Outside Vehicle'):
        vehicle_type = old_type
    if tracking_mode not in ('Driver Mobile', 'GPS Device'):
        tracking_mode = old_mode
    cursor.execute('UPDATE vehicle_master SET vehicle_type = %s, tracking_mode = %s, gps_device_id = %s, updated_at = %s WHERE id = %s',
                   (vehicle_type, tracking_mode, gps_device_id, now_ist().isoformat(), vehicle_id))
    changes = []
    if vehicle_type != old_type: changes.append(f'type {old_type} -> {vehicle_type}')
    if tracking_mode != old_mode: changes.append(f'tracking {old_mode} -> {tracking_mode}')
    log_audit(cursor, 'Vehicle Updated', 'Vehicles', vehicle_id, f'{vehicle_number}: ' + ('; '.join(changes) if changes else 'no field changes'))
    conn.commit()
    conn.close()
    return redirect(f'/vehicles/{vehicle_id}')

@app.route('/vehicles/<int:vehicle_id>/delete', methods=['POST'])
def vehicles_delete(vehicle_id):
    """Manual removal — always blocked while the vehicle has any active/incomplete trip, for both
    Company and Outside vehicles alike. Soft-removes (is_active=False); never deletes the row or
    touches its trips/audit/location history."""
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT vehicle_number FROM vehicle_master WHERE id = %s AND factory_id = %s', (vehicle_id, fid))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return redirect('/vehicles')
    vehicle_number = row[0]
    cursor.execute("SELECT COUNT(*) FROM trips WHERE vehicle_id = %s AND factory_id = %s AND trip_status NOT IN ('Delivered','Cancelled')",
                   (vehicle_id, fid))
    if cursor.fetchone()[0] > 0:
        conn.close()
        return redirect(f'/vehicles/{vehicle_id}?error=' + quote('Vehicle is currently in an active trip and cannot be removed.'))
    cursor.execute('UPDATE vehicle_master SET is_active = FALSE, updated_at = %s WHERE id = %s', (now_ist().isoformat(), vehicle_id))
    log_audit(cursor, 'Vehicle Removed', 'Vehicles', vehicle_id, f'{vehicle_number}: manually removed by {session.get("user_name")}')
    conn.commit()
    conn.close()
    return redirect('/vehicles')

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
                                       all_po_numbers=all_po_numbers, preselect_vehicle=preselect,
                                       dc_arrival_window_minutes=DC_ARRIVAL_WINDOW_MINUTES, dc_return_radius_km=DC_RETURN_RADIUS_KM)

    # POST — create the trip
    vehicle_choice = request.form.get('vehicle_choice', '').strip()
    new_vehicle_number = request.form.get('new_vehicle_number', '').strip().upper()
    vehicle_number = new_vehicle_number if vehicle_choice == '__new__' else vehicle_choice.strip().upper()
    driver_name = request.form.get('driver_name', '').strip()
    driver_mobile = request.form.get('driver_mobile', '').strip()
    start_location = request.form.get('start_location', '').strip()
    dc_lat_raw = request.form.get('dc_latitude', '').strip()
    dc_lng_raw = request.form.get('dc_longitude', '').strip()
    dc_location_name = request.form.get('dc_location_name', '').strip() or None
    dc_latitude = float(dc_lat_raw) if dc_lat_raw else None
    dc_longitude = float(dc_lng_raw) if dc_lng_raw else None
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
                       loading_started_at, tracking_token, tracking_status, dc_latitude, dc_longitude, dc_location_name)
                       VALUES (%s,%s,%s,%s,%s,%s,'Loading',%s,%s,'active',%s,%s,%s) RETURNING id''',
                   (fid, vehicle_id, vehicle_number, driver_name, driver_mobile, start_location, now_ist().strftime("%d %b %Y, %I:%M %p"), token,
                    dc_latitude, dc_longitude, dc_location_name))
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
    cursor.execute('SELECT id, vehicle_number, current_latitude, current_longitude, last_location_at, vehicle_type, tracking_mode, gps_device_id, is_active FROM vehicle_master WHERE id = %s AND factory_id = %s', (vehicle_id, fid))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return "Vehicle not found. <a href='/vehicles'>Back</a>", 404
    vid, vnum, lat, lng, last_loc, vehicle_type, tracking_mode, gps_device_id, is_active = row
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
           'freshness_label': label, 'freshness_color': color if lat is not None else 'grey', 'mode': mode if lat is not None else 'NO DATA',
           'vehicle_type': vehicle_type or 'Company Vehicle', 'tracking_mode': tracking_mode or 'Driver Mobile',
           'gps_device_id': gps_device_id, 'is_active': is_active, 'has_active_trip': active_trip_id is not None}

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

    cursor.execute('''SELECT recorded_at, latitude, longitude, is_suspicious FROM location_history
                       WHERE vehicle_id = %s AND factory_id = %s ORDER BY id DESC LIMIT 50''', (vid, fid))
    location_history = []
    for rec_at, hlat, hlng, is_susp in cursor.fetchall():
        try:
            dt = datetime.fromisoformat(rec_at)
            date_s, time_s = dt.strftime('%d %b %Y'), dt.strftime('%I:%M %p')
        except Exception:
            date_s, time_s = rec_at, ''
        location_history.append({'date': date_s, 'time': time_s, 'lat': round(hlat, 5), 'lng': round(hlng, 5), 'is_suspicious': bool(is_susp)})

    conn.commit()
    conn.close()
    return render_template_string(VEHICLE_MASTER_DETAIL_HTML, veh=veh, trips=trips, route_trip_id=route_trip_id,
                                   route_points=route_points, location_history=location_history, error_msg=request.args.get('error'))

def _trip_dict(cursor, row):
    (tid, vehicle_id, vehicle_number, driver_name, driver_mobile, start_location, trip_status,
     loading_started_at, cur_lat, cur_lng, cur_acc, last_loc_at, tracking_token, tracking_status,
     dispatched_at, delivered_at, dc_latitude, dc_longitude, dc_location_name, dc_arrived_at, dc_returned_at) = row
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
        'dc_latitude': dc_latitude, 'dc_longitude': dc_longitude, 'dc_location_name': dc_location_name,
        'dc_arrived_at': dc_arrived_at, 'dc_returned_at': dc_returned_at,
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
        tracking_token, tracking_status, dispatched_at, delivered_at,
        dc_latitude, dc_longitude, dc_location_name, dc_arrived_at, dc_returned_at
        FROM trips WHERE id = %s AND factory_id = %s''', (trip_id, fid))
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

    cursor.execute('''SELECT status, first_call_at, first_call_status, first_call_response, second_call_at,
                       second_call_status, second_call_response, next_action_at, whatsapp_escalated_at
                       FROM driver_followups WHERE trip_id = %s''', (trip_id,))
    frow = cursor.fetchone()
    n1 = None
    if frow:
        n1 = {'status': frow[0], 'first_call_at': frow[1], 'first_call_status': frow[2], 'first_call_response': frow[3],
              'second_call_at': frow[4], 'second_call_status': frow[5], 'second_call_response': frow[6],
              'next_action_at': frow[7], 'whatsapp_escalated_at': frow[8]}

    conn.commit()
    conn.close()
    track_url = request.url_root.rstrip('/') + f"/track/{t['tracking_token']}" if t['tracking_token'] else ''
    return render_template_string(TRIP_DETAIL_HTML, t=t, pos=pos, available_pos=available_pos,
                                   route_points=route_points, track_url=track_url, track_url_encoded=quote(track_url), n1=n1)

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
    log_audit(cursor, 'PO Attached to Vehicle', 'Vehicles', trip_id, f'{trow[0]}: PO {po} attached')
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
    cursor.execute('SELECT vehicle_number FROM trips WHERE id = %s AND factory_id = %s', (trip_id, fid))
    trow = cursor.fetchone()
    if not trow:
        conn.close(); return redirect('/vehicles')
    cursor.execute('DELETE FROM vehicle_po_map WHERE trip_id = %s AND po_number = %s', (trip_id, po))
    recompute_trip_status(cursor, trip_id)
    log_audit(cursor, 'PO Removed from Vehicle', 'Vehicles', trip_id, f'{trow[0]}: PO {po} removed')
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
    cursor.execute('SELECT vehicle_number FROM trips WHERE id = %s AND factory_id = %s', (trip_id, fid))
    trow = cursor.fetchone()
    if not trow:
        conn.close(); return redirect('/vehicles')
    if action == 'hold':
        cursor.execute('UPDATE vehicle_po_map SET is_hold = TRUE WHERE trip_id = %s AND po_number = %s', (trip_id, po))
        log_audit(cursor, 'PO Held', 'Vehicles', trip_id, f'{trow[0]}: PO {po} held')
    elif action == 'resume':
        cursor.execute('UPDATE vehicle_po_map SET is_hold = FALSE WHERE trip_id = %s AND po_number = %s', (trip_id, po))
        log_audit(cursor, 'PO Resumed', 'Vehicles', trip_id, f'{trow[0]}: PO {po} resumed')
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
    log_audit(cursor, 'Tracking Token Regenerated', 'Vehicles', trip_id, 'Old tracking link invalidated, new one issued')
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
    log_audit(cursor, 'GPS Tracking Stopped', 'Vehicles', trip_id, '')
    conn.commit()
    conn.close()
    return redirect(f'/trips/{trip_id}')

@app.route('/trips/<int:trip_id>/resend_link', methods=['POST'])
def trip_resend_link(trip_id):
    """F1: re-shares the tracking link for a trip. Reuses the EXISTING token whenever it's still
    valid — only generates a new one if tracking was stopped/the token is otherwise unusable. Every
    resend (and whether it needed a fresh token) is recorded in the audit log."""
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT tracking_token, tracking_status, driver_mobile, vehicle_number, trip_status FROM trips WHERE id = %s AND factory_id = %s", (trip_id, fid))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return redirect('/vehicles')
    token, tracking_status, driver_mobile, vehicle_number, trip_status = row
    regenerated = False
    if not token or tracking_status != 'active':
        token = gen_tracking_token()
        cursor.execute("UPDATE trips SET tracking_token = %s, tracking_status = 'active' WHERE id = %s", (token, trip_id))
        regenerated = True
    ts = now_ist().isoformat()
    cursor.execute("UPDATE trips SET whatsapp_link_status = 'manual_pending' WHERE id = %s", (trip_id,))
    log_audit(cursor, 'Tracking Link Resent', 'Vehicles', trip_id,
               f'{vehicle_number} -> {driver_mobile}: link resent' + (' (new token issued — old one was inactive)' if regenerated else ' (existing token reused)'))
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
    cursor.execute("SELECT vehicle_id FROM trips WHERE id = %s AND factory_id = %s", (trip_id, fid))
    vrow = cursor.fetchone()
    cursor.execute("UPDATE trips SET trip_status = 'Delivered', delivered_at = %s, tracking_status = 'stopped' WHERE id = %s AND factory_id = %s",
                   (now_ist().strftime("%d %b %Y, %I:%M %p"), trip_id, fid))
    log_audit(cursor, 'Delivery Completed', 'Vehicles', trip_id, 'Trip marked Delivered')
    if vrow and vrow[0]:
        maybe_auto_remove_outside_vehicle(cursor, fid, vrow[0])
    stop_driver_followup(cursor, trip_id, 'Trip manually marked Delivered')
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
    lat_raw, lng_raw, acc_raw = data.get('lat'), data.get('lng'), data.get('accuracy')
    if lat_raw is None or lng_raw is None:
        return jsonify({'ok': False, 'error': 'missing coordinates'}), 400
    lat, lng, acc, gps_error = validate_gps_payload(lat_raw, lng_raw, acc_raw)
    if gps_error:
        return jsonify({'ok': False, 'error': gps_error}), 400
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("""SELECT id, factory_id, vehicle_id, trip_status, tracking_status, last_location_at,
                       current_latitude, current_longitude FROM trips WHERE tracking_token = %s""", (token,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return jsonify({'ok': False, 'error': 'invalid token'}), 404
    tid, tfid, vehicle_id, trip_status, tracking_status, last_location_at, prev_lat, prev_lng = row
    if tracking_status != 'active' or trip_status in ('Delivered', 'Cancelled'):
        conn.close()
        return jsonify({'ok': False, 'error': 'tracking not active'}), 403
    # F1: rate limiting — silently accept-but-skip an over-frequent ping (still 200/ok:true, so the
    # driver's browser JS never treats it as an error and never stops watching position), while
    # never writing more than one row/update per GPS_PING_MIN_INTERVAL_SECONDS for this trip.
    if last_location_at:
        try:
            elapsed = (now_ist() - datetime.fromisoformat(last_location_at)).total_seconds()
            if elapsed < GPS_PING_MIN_INTERVAL_SECONDS:
                conn.close()
                return jsonify({'ok': True, 'throttled': True})
        except Exception:
            pass
    ts_dt = now_ist()
    ts = ts_dt.isoformat()

    # G1: server-side-only GPS jump/anomaly detection, computed strictly from server-held previous
    # trusted point (trips.current_latitude/longitude) vs this new validated point — never from
    # anything the client claims about its own speed/distance/suspicious-ness.
    prev_ts_dt = None
    if last_location_at:
        try:
            prev_ts_dt = datetime.fromisoformat(last_location_at)
        except Exception:
            prev_ts_dt = None
    is_suspicious, anomaly_reason, speed_kmph = detect_gps_anomaly(prev_lat, prev_lng, prev_ts_dt, lat, lng, ts_dt, acc)

    if is_suspicious:
        # Raw point IS preserved (for investigation), but it never becomes the new trusted position:
        # trips/vehicle_master are NOT updated and DC automation is NOT evaluated against it. Last
        # Seen / trusted route therefore stay exactly at the previous good point, unchanged.
        cursor.execute('''INSERT INTO location_history (factory_id, vehicle_id, trip_id, latitude, longitude, accuracy,
                           recorded_at, is_suspicious, anomaly_reason, calculated_speed_kmph) VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE,%s,%s)''',
                       (tfid, vehicle_id, tid, lat, lng, acc, ts, anomaly_reason, speed_kmph))
        log_audit(cursor, 'Suspicious GPS Detected', 'Vehicles', tid,
                   f'speed={speed_kmph}km/h reason="{anomaly_reason}" lat={lat} lng={lng}' if speed_kmph is not None
                   else f'reason="{anomaly_reason}" lat={lat} lng={lng}')
        conn.commit()
        conn.close()
        return jsonify({'ok': True})

    # Update the trip's own snapshot (for backward-compat display) and the permanent vehicle_master record
    cursor.execute('''UPDATE trips SET current_latitude=%s, current_longitude=%s, current_accuracy=%s, last_location_at=%s WHERE id=%s''',
                   (lat, lng, acc, ts, tid))
    if vehicle_id:
        cursor.execute('''UPDATE vehicle_master SET current_latitude=%s, current_longitude=%s, current_accuracy=%s,
                           last_location_at=%s, gps_status='live', updated_at=%s WHERE id=%s''',
                       (lat, lng, acc, ts, ts, vehicle_id))
    cursor.execute('''INSERT INTO location_history (factory_id, vehicle_id, trip_id, latitude, longitude, accuracy,
                       recorded_at, is_suspicious, calculated_speed_kmph) VALUES (%s,%s,%s,%s,%s,%s,%s,FALSE,%s)''',
                   (tfid, vehicle_id, tid, lat, lng, acc, ts, speed_kmph))
    evaluate_dc_automation(cursor, tfid, tid, lat, lng, ts)
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/exotel/callback', methods=['GET', 'POST'])
def exotel_callback():
    """N1: Exotel callback endpoint. Public (no Flask login session exists for an external
    provider callback) — this is why it's added to CSRF_EXEMPT_ENDPOINTS / MODULE_GATE_EXEMPT_ENDPOINTS
    / PERMISSION_GATE_EXEMPT_ENDPOINTS below, exactly like track_ping. Security instead comes from
    looking the row up by the CallSid WE generated and stored when placing the call — factory_id and
    trip_id are NEVER taken from the callback payload, only derived from our own matched row, so a
    forged/replayed callback can never touch a trip it doesn't have the real SID for.

    LIVE-INTEGRATION AUDIT FINDING (confirmed against Exotel's official StatusCallback docs AND the
    Passthru applet docs — support.exotel.com/support/articles/48283, exotelapi.docs.apiary.io):
    Exotel actually sends TWO structurally different requests here, and this endpoint must accept
    both:
      1. StatusCallback (POST, call-level, fired when the call reaches a terminal state) — confirmed
         fields: CallSid, EventType, Status, DialCallDuration/Duration, To, From, StartTime, EndTime,
         Direction, RecordingUrl. Does NOT include the driver's DTMF digit.
      2. Passthru applet (GET, mid-flow, configured separately inside the ExoML flow builder in the
         Exotel dashboard's App Bazaar) — confirmed field for DTMF is `digits` (lowercase, an array
         of digits gathered by the preceding Gather applet, sometimes delivered as a bracketed/quoted
         string like ["1"]). This is a SEPARATE request Exotel makes ONLY if the flow's Passthru
         applet's Application URL is configured to point here — that is a manual dashboard step (see
         final report), this code alone cannot make Exotel send it.
    Because of #2, this route must accept GET as well as POST, and read parameters via
    request.values (merges query-string args and form-body so either request shape works). The
    digits value is also unwrapped if it arrives as a bracketed/quoted array string.
    CallSid-based security and the None/'UNKNOWN'-on-no-match fallback are both unchanged."""
    call_sid = request.values.get('CallSid', '').strip()
    call_status = request.values.get('Status', '').strip().lower()
    duration_raw = request.values.get('DialCallDuration', '') or request.values.get('Duration', '')
    duration_raw = duration_raw.strip()
    # Widened, not narrowed: try every plausible field name across both the StatusCallback (POST)
    # and Passthru (GET) mechanisms. None of these replace the CallSid-based security check.
    digits_raw = (request.values.get('Digits', '').strip() or request.values.get('digits', '').strip()
                  or request.values.get('dtmf', '').strip() or request.values.get('DTMF', '').strip())
    # Passthru's confirmed format is sometimes a bracketed/quoted array e.g. ["1"] — unwrap it to a
    # plain digit string so normalize_driver_response() sees the same shape either way.
    digits = re.sub(r'[\[\]"\'\s]', '', digits_raw)
    response_text_raw = request.values.get('response_text', '').strip() or digits
    if not call_sid:
        return jsonify({'ok': False}), 400
    try:
        duration = int(duration_raw) if duration_raw else None
    except ValueError:
        duration = None

    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, trip_id, factory_id, status FROM driver_followups WHERE first_call_sid = %s FOR UPDATE", (call_sid,))
    row = cursor.fetchone()
    leg = 'first'
    if not row:
        cursor.execute("SELECT id, trip_id, factory_id, status FROM driver_followups WHERE second_call_sid = %s FOR UPDATE", (call_sid,))
        row = cursor.fetchone()
        leg = 'second'
    if not row:
        # Unknown SID — never trust/act on anything else in the payload. Clean, silent rejection.
        conn.close()
        return jsonify({'ok': False}), 404
    followup_id, trip_id, fid, status = row
    ts = now_ist().isoformat()

    # LIVE-INTEGRATION AUDIT FINDING: a call being "completed" at the telephony level is NOT the
    # same as a valid driver response, AND the two Exotel mechanisms are asynchronous — the Passthru
    # GET (which actually carries the digit) has no 'Status' field at all, while the StatusCallback
    # POST (which carries 'Status') never carries the digit. Gating on Status=='completed' would
    # therefore make a genuine Passthru-delivered digit never register. The correct check is: did we
    # receive a digit/response that normalizes to something meaningful? If yes, that alone is valid
    # evidence of a driver response, independent of whether a Status field was present on this
    # particular request.
    normalized = normalize_driver_response(response_text_raw)
    has_valid_response = normalized != 'UNKNOWN'

    cursor.execute("SELECT vehicle_number, driver_name, t.dc_arrived_at FROM trips t JOIN driver_followups d ON d.trip_id = t.id WHERE d.id = %s",
                   (followup_id,))
    vrow = cursor.fetchone()
    vehicle_number = vrow[0] if vrow else '?'

    if leg == 'first':
        cursor.execute("UPDATE driver_followups SET first_call_status=%s, first_call_duration=%s, updated_at=%s WHERE id=%s",
                       (call_status, duration, ts, followup_id))
        if has_valid_response:
            cursor.execute("""UPDATE driver_followups SET status='CALL_1_RESPONDED', first_call_response=%s,
                               response_text=%s, next_action_at=NULL, updated_at=%s WHERE id=%s""",
                           (normalized, response_text_raw, ts, followup_id))
            log_audit_system(cursor, fid, 'N1 Call 1 Response', 'Vehicles', trip_id, f'{vehicle_number}: {normalized}')
        else:
            second_due = (now_ist() + timedelta(hours=N1_SECOND_CALL_DELAY_HOURS)).isoformat()
            cursor.execute("""UPDATE driver_followups SET status='WAIT_1_HOUR', second_call_due_at=%s,
                               next_action_at=%s, updated_at=%s WHERE id=%s""",
                           (second_due, second_due, ts, followup_id))
            log_audit_system(cursor, fid, 'N1 Call 1 No Response', 'Vehicles', trip_id,
                       f'{vehicle_number}: call_status={call_status}, second call scheduled at {second_due} — no WhatsApp yet')
    else:
        cursor.execute("UPDATE driver_followups SET second_call_status=%s, second_call_duration=%s, updated_at=%s WHERE id=%s",
                       (call_status, duration, ts, followup_id))
        if has_valid_response:
            cursor.execute("""UPDATE driver_followups SET status='CALL_2_RESPONDED', second_call_response=%s,
                               response_text=%s, next_action_at=NULL, updated_at=%s WHERE id=%s""",
                           (normalized, response_text_raw, ts, followup_id))
            log_audit_system(cursor, fid, 'N1 Call 2 Response', 'Vehicles', trip_id, f'{vehicle_number}: {normalized}')
        else:
            driver_name = vrow[1] if vrow else None
            dc_arrived_at = vrow[2] if vrow else None
            message = build_escalation_message(vehicle_number, trip_id, driver_name, dc_arrived_at)
            cursor.execute("""UPDATE driver_followups SET status='WHATSAPP_ESCALATED', whatsapp_escalated_at=%s,
                               next_action_at=NULL, updated_at=%s WHERE id=%s""", (ts, ts, followup_id))
            log_audit_system(cursor, fid, 'N1 Call 2 No Response', 'Vehicles', trip_id, f'{vehicle_number}: call_status={call_status}')
            send_whatsapp_escalation(cursor, fid, followup_id, trip_id, message)
            log_audit_system(cursor, fid, 'N1 WhatsApp Escalated', 'Vehicles', trip_id, f'{vehicle_number}: escalation composed (see WhatsApp Escalation note)')
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/trips/<int:trip_id>/n1/call_now', methods=['POST'])
def n1_call_now(trip_id):
    """N1 TEST-ONLY manual trigger — 'Call Now' button. Lets an authorized user (D1/E1-gated exactly
    like every other Vehicle-module action; no bypass) place the first Exotel call immediately
    instead of waiting for the normal T+3h schedule. Does NOT alter the production timing logic —
    it only forces the existing CALL_1_DUE row to be due right now, then reuses the exact same
    _process_one_due_followup() codepath a real scheduled run would use."""
    if not session.get('logged_in'): return redirect('/login')
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, status FROM driver_followups WHERE trip_id = %s AND factory_id = %s", (trip_id, fid))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return redirect(f'/trips/{trip_id}?error=' + quote('No N1 follow-up exists for this trip yet (only created once the trip is In DC).'))
    followup_id, status = row
    if status not in ('CALL_1_DUE', 'WAIT_1_HOUR'):
        conn.close()
        return redirect(f'/trips/{trip_id}?error=' + quote(f'Follow-up is in state {status} — cannot manually trigger a call from here.'))
    cursor.execute("UPDATE driver_followups SET next_action_at = %s WHERE id = %s", (now_ist().isoformat(), followup_id))
    log_audit(cursor, 'N1 Manual Call Now Triggered', 'Vehicles', trip_id, f'By {session.get("user_name")}')
    conn.commit()
    conn.close()
    _process_one_due_followup(followup_id)
    return redirect(f'/trips/{trip_id}?ok=1&msg=' + quote('N1 call triggered manually (test).'))


@app.route('/internal/n1/process_due', methods=['POST'])
def n1_process_due_endpoint():
    """N1 scheduler trigger — intended to be called by a Render Cron Job (see final report for the
    exact recommended `curl` command/schedule), NOT by any logged-in user. Authenticated via a
    shared secret (N1_CRON_SECRET) compared with constant-time comparison — never a Flask session,
    since a cron job has no browser session. Returns 503 if N1_CRON_SECRET isn't configured at all
    (fail-closed: no way to trigger this without an explicitly-set secret)."""
    if not N1_CRON_SECRET:
        return jsonify({'ok': False, 'error': 'N1_CRON_SECRET not configured'}), 503
    provided = request.headers.get('X-N1-Cron-Secret', '')
    if not secrets.compare_digest(provided, N1_CRON_SECRET):
        return jsonify({'ok': False, 'error': 'unauthorized'}), 403
    results = process_due_driver_followups()
    return jsonify({'ok': True, 'processed': len(results)})


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
            <form method="POST" style="text-align:left;" autocomplete="off">
                <label style="margin-top:0;">Company Name</label>
                <input type="text" name="company_name" placeholder="e.g. ABC Foods Pvt Ltd" required value="{{ form.company_name or '' }}" autocomplete="off">
                <label>Your Name</label>
                <input type="text" name="admin_name" placeholder="Admin's full name" required value="{{ form.admin_name or '' }}" autocomplete="off">
                <label>Username (for login)</label>
                <input type="text" name="username" placeholder="e.g. abcfoods_admin" required value="{{ form.username or '' }}" autocomplete="off">
                <label>Mobile</label>
                <input type="text" name="mobile" placeholder="10-digit mobile number" value="{{ form.mobile or '' }}" autocomplete="off">
                <label>Password</label>
                <div style="position:relative;">
                    <input type="password" name="password" id="regPassword" placeholder="Choose a password" required autocomplete="new-password" style="padding-right:44px;">
                    <button type="button" onclick="toggleRegPw('regPassword', this)" style="position:absolute; right:10px; top:50%; transform:translateY(-50%); background:none; border:none; cursor:pointer; font-size:15px; padding:0;">👁</button>
                </div>
                <label>Confirm Password</label>
                <div style="position:relative;">
                    <input type="password" name="confirm_password" id="regConfirmPassword" placeholder="Re-enter password" required autocomplete="new-password" style="padding-right:44px;">
                    <button type="button" onclick="toggleRegPw('regConfirmPassword', this)" style="position:absolute; right:10px; top:50%; transform:translateY(-50%); background:none; border:none; cursor:pointer; font-size:15px; padding:0;">👁</button>
                </div>
                <button type="submit" class="btn btn-block" style="margin-top:16px;">Create Account</button>
            </form>
            <div style="margin-top:16px; padding-top:14px; border-top:1px solid var(--border); font-size:12.5px; color:var(--text-muted);">
                Already have an account? <a href="/login" style="color:var(--primary); font-weight:600; text-decoration:none;">Log in</a>
            </div>
        </div>
    </div>
    <script>
    function toggleRegPw(fieldId, btn) {
        const field = document.getElementById(fieldId);
        if (field.type === 'password') { field.type = 'text'; btn.textContent = '🙈'; }
        else { field.type = 'password'; btn.textContent = '👁'; }
    }
    </script>
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
                        {% if u.role != 'Super Admin' %}
                        <a href="/users/{{ u.id }}/permissions" class="btn btn-outline btn-sm">Permissions</a>
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

USER_PERMISSIONS_HTML = STYLE_BLOCK + """
<title>Permissions: {{ target_user.name }} | {{ factory_display_name }}</title>
</head>
<body>
<div class="container">
""" + TOPBAR_TEMPLATE.replace("__NAV__", nav_html('users')) + """
    <div class="card">
        <div class="card-header">
            <h2>🔐 {{ target_user.name }}'s Permissions</h2>
            <a href="/users" class="btn btn-outline btn-sm">← Back</a>
        </div>
        <div style="color:var(--text-muted); font-size:12.5px; margin-bottom:14px;">
            {{ target_user.username }} &middot; Role: <strong style="color:var(--text);">{{ target_user.role }}</strong>.
            Checkboxes show the CURRENT effective permission (role default, unless customized below).
            Only changing a box away from the role's default creates a per-user override — leave everything as-is to keep this user on plain role defaults.
        </div>
        <form method="POST" action="/users/{{ target_user.id }}/permissions">
            <table class="table">
                <tr><th>Module</th>{% for a in all_actions %}<th style="text-transform:capitalize;">{{ a }}</th>{% endfor %}</tr>
                {% for row in matrix %}
                <tr>
                    <td>{{ row.module }}</td>
                    {% for cell in row.actions %}
                    <td style="text-align:center;">
                        <input type="checkbox" name="perm__{{ row.module }}__{{ cell.action }}"
                               {% if cell.checked %}checked{% endif %} style="width:auto;">
                    </td>
                    {% endfor %}
                </tr>
                {% endfor %}
            </table>
            <button type="submit" class="btn" style="margin-top:14px;">Save Permissions</button>
        </form>
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
    cursor.execute('SELECT status, name FROM users WHERE id = %s AND factory_id = %s', (user_id, fid))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return redirect('/users')
    new_status = 'Inactive' if row[0] == 'Active' else 'Active'
    cursor.execute('UPDATE users SET status = %s, updated_at = %s WHERE id = %s AND factory_id = %s', (new_status, now_ist().isoformat(), user_id, fid))
    log_audit(cursor, 'User Activated' if new_status == 'Active' else 'User Deactivated', 'Users', user_id, row[1])
    conn.commit()
    conn.close()
    return redirect('/users')

@app.route('/users/<int:user_id>/permissions', methods=['GET', 'POST'])
def user_permissions_page(user_id):
    """E1: view/edit a specific user's Tier-2 permission overrides. GET shows the current
    effective permission (role-default, with any override applied) for every module+action as
    checkboxes; POST saves only the differences from the role default as override rows — if a
    checkbox matches what the role would already grant, no override row is created (keeps the
    override table minimal and makes 'reset to role default' as simple as re-checking the defaults)."""
    if not session.get('logged_in'): return redirect('/login')
    if session.get('role') != 'Factory Admin':
        return "Access denied — only a Factory Admin can manage team members.", 403
    fid = current_factory_id()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT id, name, username, role FROM users WHERE id = %s AND factory_id = %s', (user_id, fid))
    urow = cursor.fetchone()
    if not urow:
        conn.close()
        return redirect('/users')
    _, uname, uusername, urole = urow

    if request.method == 'POST':
        cursor.execute('DELETE FROM user_permission_overrides WHERE user_id = %s AND factory_id = %s', (user_id, fid))
        for module in ALL_MODULE_NAMES:
            for action in ALL_PERMISSION_ACTIONS:
                checked = request.form.get(f'perm__{module}__{action}') == 'on'
                role_default = action in ROLE_PERMISSIONS_DEFAULT.get(urole, {}).get(module, set())
                if checked != role_default:  # only store the DIFFERENCE from the role default
                    cursor.execute('''INSERT INTO user_permission_overrides (user_id, factory_id, module, action, allowed)
                                       VALUES (%s,%s,%s,%s,%s)''', (user_id, fid, module, action, checked))
        log_audit(cursor, 'User Permissions Updated', 'Users', user_id, f'Permissions customized for {uname} ({uusername})')
        conn.commit()
        conn.close()
        return redirect('/users?ok=' + quote(f"{uname}'s permissions updated."))

    cursor.execute('SELECT module, action, allowed FROM user_permission_overrides WHERE user_id = %s AND factory_id = %s', (user_id, fid))
    overrides = {(m, a): bool(v) for m, a, v in cursor.fetchall()}
    conn.close()
    matrix = []
    for module in ALL_MODULE_NAMES:
        row = {'module': module, 'actions': []}
        for action in ALL_PERMISSION_ACTIONS:
            is_override = (module, action) in overrides
            effective = overrides[(module, action)] if is_override else (action in ROLE_PERMISSIONS_DEFAULT.get(urole, {}).get(module, set()))
            row['actions'].append({'action': action, 'checked': effective, 'is_override': is_override})
        matrix.append(row)
    return render_template_string(USER_PERMISSIONS_HTML, target_user={'id': user_id, 'name': uname, 'username': uusername, 'role': urole},
                                   matrix=matrix, all_actions=ALL_PERMISSION_ACTIONS)

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
        <div class="nav"><a href="/superadmin/access" class="">🔐 Temporary Access</a><a href="/change_password" class="">🔑 Change Password</a><a href="/logout" class="">Logout</a></div>
    </div>
    {% if temp_access_banner %}
    <div style="background:#f59e0b; color:#1a1a1a; padding:9px 16px; font-size:12.5px; font-weight:700; display:flex; justify-content:space-between; align-items:center; border-radius:8px; margin-bottom:16px; flex-wrap:wrap; gap:8px;">
        <span>🔐 TEMPORARY ACCESS ACTIVE — viewing {{ temp_access_banner.factory_name }}'s data (reason: {{ temp_access_banner.reason }}) &middot; expires {{ temp_access_banner.expires_at }}</span>
        <form method="POST" action="/superadmin/access/exit" style="margin:0;"><button type="submit" style="background:#1a1a1a; color:#fff; border:none; padding:5px 12px; border-radius:6px; font-size:11.5px; cursor:pointer; font-weight:700;">Exit Access</button></form>
    </div>
    {% endif %}

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
        <form method="POST" action="/superadmin/create" class="form-grid" autocomplete="off">
            <div><label>Company Name</label><input type="text" name="company_name" autocomplete="off" required></div>
            <div><label>Admin Name</label><input type="text" name="admin_name" autocomplete="off" required></div>
            <div><label>Admin Username</label><input type="text" name="new_admin_username" autocomplete="off" placeholder="Choose a username" readonly onfocus="this.removeAttribute('readonly')" required></div>
            <div><label>Admin Password</label><input type="password" name="new_admin_password" autocomplete="new-password" placeholder="Choose a password" readonly onfocus="this.removeAttribute('readonly')" required></div>
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
    username = request.form.get('new_admin_username', '').strip()
    password = request.form.get('new_admin_password', '')
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

TEMP_ACCESS_HTML = STYLE_BLOCK + """
<title>Temporary Access | {{ platform_name }}</title>
</head>
<body>
<div class="container">
    <div class="topbar">
        <div class="brand">
            <div class="brand-logo">SA</div>
            <div>
                <h1>{{ platform_name }}</h1>
                <span>Super Admin &middot; Temporary Cross-Company Access</span>
            </div>
        </div>
        <div class="nav"><a href="/superadmin" class="">← Back to Super Admin</a><a href="/logout" class="">Logout</a></div>
    </div>

    {% if error_msg %}<div class="badge badge-amber" style="display:block; padding:10px 14px; margin-bottom:14px; font-size:13px;">{{ error_msg }}</div>{% endif %}
    {% if ok_msg %}<div class="badge badge-green" style="display:block; padding:10px 14px; margin-bottom:14px; font-size:13px;">{{ ok_msg }}</div>{% endif %}

    <div class="card">
        <div class="card-header"><h2>🔐 Request Temporary Access</h2></div>
        <div style="color:var(--text-muted); font-size:12.5px; margin-bottom:14px;">
            By default, Super Admin has NO access to any company's business data — only this platform-management panel.
            To view a company's data (for support/debugging), request time-boxed access below with a reason. It auto-expires,
            and every grant, exit and action taken is recorded in that company's own Audit Log.
        </div>
        <form method="POST" action="/superadmin/access/grant" id="tempAccessForm">
            <div class="form-grid">
                <div>
                    <label>Company</label>
                    <select name="factory_id" required>
                        <option value="" disabled selected>Select company</option>
                        {% for f in factories %}
                        <option value="{{ f.id }}">{{ f.company_name }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div>
                    <label>Reason (required)</label>
                    <input type="text" name="reason" placeholder="e.g. Debugging a reported PO import issue" required>
                </div>
                <div>
                    <label>Duration (minutes)</label>
                    <select name="duration_minutes">
                        <option value="15">15 minutes</option>
                        <option value="30" selected>30 minutes</option>
                        <option value="60">1 hour</option>
                        <option value="180">3 hours</option>
                    </select>
                </div>
            </div>
            <div style="margin-top:14px; padding:14px; background:rgba(255,255,255,0.03); border-radius:12px;">
                <label style="margin-top:0;">Module(s) — required, nothing is pre-selected</label>
                <div style="color:var(--text-muted); font-size:12px; margin-bottom:10px;">
                    Pick exactly what this grant should cover. Access is limited to only the modules checked here —
                    backend-enforced, not just hidden menus. Check "All" only if the company's full data is genuinely needed.
                </div>
                <div style="display:grid; grid-template-columns:repeat(auto-fill, minmax(140px, 1fr)); gap:8px;">
                    {% for m in all_modules %}
                    <label style="display:flex; align-items:center; gap:6px; font-weight:400; font-size:13px; margin:0;">
                        <input type="checkbox" name="modules" value="{{ m }}" class="module-cb" style="width:auto; margin:0;"> {{ m }}
                    </label>
                    {% endfor %}
                    <label style="display:flex; align-items:center; gap:6px; font-weight:600; font-size:13px; margin:0; color:var(--primary);">
                        <input type="checkbox" name="modules" value="All" id="allModulesCb" style="width:auto; margin:0;"> All Modules
                    </label>
                </div>
            </div>
            <button type="submit" class="btn" style="margin-top:14px;">Grant &amp; Enter Access</button>
        </form>
        <script>
            // "All" and individual module picks are mutually exclusive — checking one clears the other,
            // so there's never ambiguity about what a grant actually covers.
            (function() {
                const allCb = document.getElementById('allModulesCb');
                const moduleCbs = document.querySelectorAll('.module-cb');
                allCb.addEventListener('change', function() {
                    if (allCb.checked) moduleCbs.forEach(cb => cb.checked = false);
                });
                moduleCbs.forEach(cb => cb.addEventListener('change', function() {
                    if (cb.checked) allCb.checked = false;
                }));
            })();
        </script>
    </div>

    <div class="card">
        <div class="card-header"><h2>📋 Access Grant History</h2></div>
        {% if grants|length > 0 %}
        <table>
            <thead><tr><th>Company</th><th>Reason</th><th>Granted</th><th>Duration</th><th>Expires</th><th>Status</th><th></th></tr></thead>
            <tbody>
            {% for g in grants %}
            <tr>
                <td><span class="badge badge-blue">{{ g.company_name or '—' }}</span></td>
                <td>{{ g.reason }}</td>
                <td style="color:var(--text-muted); font-size:12px;">{{ g.granted_at[:16] if g.granted_at else '—' }}</td>
                <td>{{ g.duration_minutes }} min</td>
                <td style="color:var(--text-muted); font-size:12px;">{{ g.expires_at[:16] if g.expires_at else '—' }}</td>
                <td>
                    {% if g.current %}<span class="badge badge-green">🟢 Currently Active (this session)</span>
                    {% elif g.revoked_at %}<span class="badge badge-amber">Revoked</span>
                    {% elif g.live %}<span class="badge badge-green">Active</span>
                    {% else %}<span style="color:var(--text-muted); font-size:12px;">Expired</span>
                    {% endif %}
                </td>
                <td>
                    {% if g.live and not g.current %}
                    <form method="POST" action="/superadmin/access/{{ g.id }}/revoke" onsubmit="return confirm('Revoke this access grant now?');">
                        <button type="submit" class="btn btn-danger btn-sm">Revoke</button>
                    </form>
                    {% endif %}
                </td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <div class="empty-state">No access grants yet.</div>
        {% endif %}
    </div>
</div>
</body>
</html>
"""


@app.route('/superadmin/access')
def superadmin_access_page():
    if not session.get('logged_in'): return redirect('/login')
    if not is_super_admin():
        return "Access denied — Super Admin only.", 403
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT id, company_name FROM factories ORDER BY company_name')
    factories = [{'id': r[0], 'company_name': r[1]} for r in cursor.fetchall()]
    cursor.execute('''SELECT g.id, f.company_name, g.reason, g.module, g.granted_at, g.duration_minutes,
                       g.expires_at, g.revoked_at, g.is_active, g.super_admin_username
                       FROM temp_access_grants g LEFT JOIN factories f ON f.id = g.factory_id
                       ORDER BY g.id DESC LIMIT 100''')
    grants = []
    now = now_ist()
    for gid, cname, reason, module, granted_at, duration, expires_at, revoked_at, is_active, admin_uname in cursor.fetchall():
        expired = False
        if expires_at:
            try:
                expired = now > datetime.fromisoformat(expires_at)
            except Exception:
                pass
        live = bool(is_active) and not expired and not revoked_at
        grants.append({'id': gid, 'company_name': cname, 'reason': reason, 'module': module, 'granted_at': granted_at,
                        'duration_minutes': duration, 'expires_at': expires_at, 'revoked_at': revoked_at,
                        'live': live, 'admin_username': admin_uname,
                        'current': session.get('temp_access_grant_id') == gid})
    conn.close()
    return render_template_string(TEMP_ACCESS_HTML, factories=factories, grants=grants,
                                   error_msg=request.args.get('error'), ok_msg=request.args.get('ok'),
                                   all_modules=ALL_MODULE_NAMES)

@app.route('/superadmin/access/grant', methods=['POST'])
def superadmin_access_grant():
    if not session.get('logged_in'): return redirect('/login')
    if not is_super_admin():
        return "Access denied — Super Admin only.", 403
    factory_id = request.form.get('factory_id', '').strip()
    reason = request.form.get('reason', '').strip()
    # D1: no silent "blank -> All" fallback. Super Admin must explicitly check one or more modules,
    # or explicitly check "All" — a blank/empty selection is a hard validation error.
    selected_modules = [m.strip() for m in request.form.getlist('modules') if m.strip()]
    if not selected_modules:
        return redirect('/superadmin/access?error=' + quote("Please select at least one module, or check 'All'."))
    module = 'All' if 'All' in selected_modules else ','.join(selected_modules)
    duration_raw = request.form.get('duration_minutes', '30').strip()
    if not factory_id or not reason:
        return redirect('/superadmin/access?error=' + quote('Please select a company and give a reason for access.'))
    try:
        factory_id = int(factory_id)
        duration_minutes = max(5, min(int(duration_raw), 480))  # 5 min to 8 hr sanity bounds
    except ValueError:
        return redirect('/superadmin/access?error=' + quote('Invalid company or duration.'))
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT company_name FROM factories WHERE id = %s', (factory_id,))
    frow = cursor.fetchone()
    if not frow:
        conn.close()
        return redirect('/superadmin/access?error=' + quote('Company not found.'))
    ts = now_ist()
    granted_at = ts.isoformat()
    expires_at = (ts + timedelta(minutes=duration_minutes)).isoformat()
    cursor.execute('''INSERT INTO temp_access_grants (super_admin_user_id, super_admin_username, factory_id, reason, module,
                       granted_at, duration_minutes, expires_at, is_active) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,TRUE) RETURNING id''',
                   (session.get('user_id'), session.get('user_name'), factory_id, reason, module, granted_at, duration_minutes, expires_at))
    grant_id = cursor.fetchone()[0]
    # Enter the grant immediately so this Super Admin's session now resolves to the target factory
    session['temp_access_grant_id'] = grant_id
    cursor.execute('INSERT INTO audit_log (factory_id, user_id, user_name, action, module, record_id, details, timestamp) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                   (factory_id, session.get('user_id'), session.get('user_name'), 'Temporary Access Granted', module, grant_id,
                    f'Super Admin {session.get("user_name")} granted {duration_minutes}min access to {frow[0]}. Reason: {reason}', granted_at))
    conn.commit()
    conn.close()
    return redirect('/')

@app.route('/superadmin/access/exit', methods=['POST'])
def superadmin_access_exit():
    if not session.get('logged_in'): return redirect('/login')
    if not is_super_admin():
        return "Access denied — Super Admin only.", 403
    grant_id = session.get('temp_access_grant_id')
    if grant_id:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT factory_id FROM temp_access_grants WHERE id = %s', (grant_id,))
        row = cursor.fetchone()
        target_fid = row[0] if row else None
        cursor.execute('INSERT INTO audit_log (factory_id, user_id, user_name, action, module, record_id, details, timestamp) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                       (target_fid, session.get('user_id'), session.get('user_name'), 'Temporary Access Exited', 'All', grant_id, '', now_ist().isoformat()))
        conn.commit()
        conn.close()
    session.pop('temp_access_grant_id', None)
    return redirect('/superadmin')

@app.route('/superadmin/access/<int:grant_id>/revoke', methods=['POST'])
def superadmin_access_revoke(grant_id):
    if not session.get('logged_in'): return redirect('/login')
    if not is_super_admin():
        return "Access denied — Super Admin only.", 403
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT factory_id FROM temp_access_grants WHERE id = %s', (grant_id,))
    row = cursor.fetchone()
    ts = now_ist().isoformat()
    cursor.execute('UPDATE temp_access_grants SET is_active = FALSE, revoked_at = %s WHERE id = %s', (ts, grant_id))
    if row:
        cursor.execute('INSERT INTO audit_log (factory_id, user_id, user_name, action, module, record_id, details, timestamp) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                       (row[0], session.get('user_id'), session.get('user_name'), 'Temporary Access Revoked', 'All', grant_id, '', ts))
    conn.commit()
    conn.close()
    if session.get('temp_access_grant_id') == grant_id:
        session.pop('temp_access_grant_id', None)
    return redirect('/superadmin/access')

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
                # K1: explicit-context audit insert — session isn't fully populated yet at this exact
                # point, so this bypasses log_audit()'s session-reliant current_factory_id()/session
                # lookups and writes the row directly with values we already have in hand.
                cursor.execute('INSERT INTO audit_log (factory_id, user_id, user_name, action, module, record_id, details, timestamp, via_temp_access_grant_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULL)',
                               (None, user_id, name, 'Login Success', 'Auth', username, 'Super Admin login', now_ist().isoformat()))
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
                    cursor.execute('INSERT INTO audit_log (factory_id, user_id, user_name, action, module, record_id, details, timestamp, via_temp_access_grant_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULL)',
                                   (factory_id, user_id, name, 'Login Success', 'Auth', username, f'role={role}', now_ist().isoformat()))
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
            # K1: failed login — factory_id is only known if the username matched an existing user
            # (wrong password case); for a fully unknown username there is no factory to attribute
            # it to, so it's logged with factory_id=NULL (platform-level, still visible to Super Admin).
            fail_factory_id = row[1] if row else None
            cursor.execute('INSERT INTO audit_log (factory_id, user_id, user_name, action, module, record_id, details, timestamp, via_temp_access_grant_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULL)',
                           (fail_factory_id, None, None, 'Failed Login', 'Auth', username, 'Invalid username or password', now_ist().isoformat()))
            conn.commit()
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
    if session.get('logged_in'):
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('INSERT INTO audit_log (factory_id, user_id, user_name, action, module, record_id, details, timestamp, via_temp_access_grant_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULL)',
                       (session.get('factory_id'), session.get('user_id'), session.get('user_name'), 'Logout', 'Auth', session.get('username', ''), '', now_ist().isoformat()))
        conn.commit()
        conn.close()
    session.clear()
    return redirect('/login')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
