from flask import Flask, render_template_string, request, redirect, session
import sqlite3
import csv
import io
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'real_instant_foods_secure_2026'

def init_db():
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    # PO और लोडिंग रिकॉर्ड्स के लिए टेबल
    cursor.execute('''CREATE TABLE IF NOT EXISTS po_items (id INTEGER PRIMARY KEY AUTOINCREMENT, po_number TEXT, item_name TEXT, weight TEXT, ordered_qty INTEGER, barcode TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS dispatch_log (id INTEGER PRIMARY KEY AUTOINCREMENT, po_number TEXT, vehicle_no TEXT, location TEXT, dispatch_date TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS loading_details (id INTEGER PRIMARY KEY AUTOINCREMENT, po_number TEXT, product_name TEXT, loaded_qty INTEGER, vehicle_no TEXT, location TEXT, timestamp TEXT)''')
    conn.commit()
    conn.close()

init_db()

DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>REAL INSTANT FOODS | Dispatch ERP</title>
    <style>
        :root { --accent: #2563eb; --bg: #0f172a; --card: #1e293b; --text: #f1f5f9; }
        body { font-family: 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); padding: 20px; }
        .header { text-align: center; margin-bottom: 30px; }
        .header h1 { color: var(--accent); letter-spacing: 3px; }
        .card { background: var(--card); padding: 20px; border-radius: 15px; margin-bottom: 20px; box-shadow: 0 4px 10px rgba(0,0,0,0.3); }
        input, select, button { padding: 10px; border-radius: 5px; border: none; }
        button { background: var(--accent); color: white; cursor: pointer; }
        table { width: 100%; border-collapse: collapse; background: var(--card); }
        th, td { padding: 12px; border-bottom: 1px solid #475569; text-align: left; }
    </style>
</head>
<body>
    <div class="header"><h1>REAL INSTANT FOODS</h1><p>Logistics & Dispatch Management System</p></div>
    
    <div class="grid" style="display:grid; grid-template-columns: 1fr 1fr; gap: 20px;">
        <div class="card">
            <h3>🔍 Search Dispatch History</h3>
            <form action="/search" method="GET">
                <input type="text" name="po_number" placeholder="Enter PO Number to search..." required>
                <button type="submit">Search Records</button>
            </form>
        </div>
        <div class="card">
            <h3>📦 New Dispatch Entry</h3>
            <form action="/new_dispatch" method="POST">
                <input type="text" name="po_number" placeholder="PO Number" required>
                <input type="text" name="vehicle_no" placeholder="Vehicle Number" required>
                <input type="text" name="location" placeholder="Destination Location" required>
                <button type="submit">Save Dispatch Log</button>
            </form>
        </div>
    </div>

    <div class="card">
        <h3>📋 Recent Dispatches</h3>
        <table>
            <tr><th>PO Number</th><th>Vehicle</th><th>Location</th><th>Date</th></tr>
            {% for log in logs %}
            <tr><td>{{ log[1] }}</td><td>{{ log[2] }}</td><td>{{ log[3] }}</td><td>{{ log[4] }}</td></tr>
            {% endfor %}
        </table>
    </div>
</body>
</html>
"""

@app.route('/')
def home():
    if not session.get('logged_in'): return redirect('/login')
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM dispatch_log ORDER BY id DESC')
    logs = cursor.fetchall()
    conn.close()
    return render_template_string(DASHBOARD_TEMPLATE, logs=logs)

@app.route('/new_dispatch', methods=['POST'])
def new_dispatch():
    po = request.form['po_number']
    v = request.form['vehicle_no']
    loc = request.form['location']
    date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('INSERT INTO dispatch_log (po_number, vehicle_no, location, dispatch_date) VALUES (?, ?, ?, ?)', (po, v, loc, date))
    conn.commit()
    conn.close()
    return redirect('/')

@app.route('/search', methods=['GET'])
def search():
    po = request.args.get('po_number')
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM dispatch_log WHERE po_number = ?', (po,))
    logs = cursor.fetchall()
    conn.close()
    return render_template_string(DASHBOARD_TEMPLATE, logs=logs)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST' and request.form.get('password') == 'real@8283':
        session['logged_in'] = True
        return redirect('/')
    return "<body style='background:#0f172a; text-align:center; padding-top:100px;'><form method='POST'><input type='password' name='password' placeholder='Enter Password'><button type='submit'>Login</button></form></body>"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
