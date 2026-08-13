from flask import Flask, render_template_string, request, redirect, session
import sqlite3, csv, io
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'real_instant_foods_ai_2026'

def init_db():
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS po_items (id INTEGER PRIMARY KEY AUTOINCREMENT, po_number TEXT, item_name TEXT, weight TEXT, ordered_qty INTEGER, barcode TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS dispatch_log (id INTEGER PRIMARY KEY AUTOINCREMENT, po_number TEXT, vehicle_no TEXT, location TEXT, product_name TEXT, loaded_qty INTEGER, timestamp TEXT)''')
    conn.commit()
    conn.close()

init_db()

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>REAL INSTANT FOODS | AI ERP</title>
    <script src="https://unpkg.com/html5-qrcode"></script>
    <style>
        :root { --primary: #3b82f6; --bg: #020617; --card: #1e293b; --text: #f8fafc; }
        body { font-family: 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); padding: 20px; }
        .header { text-align: center; color: var(--primary); font-size: 2rem; font-weight: 800; margin-bottom: 20px; }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        .card { background: var(--card); padding: 20px; border-radius: 20px; border: 1px solid #334155; }
        button { background: var(--primary); color: white; padding: 15px; border: none; border-radius: 10px; width: 100%; font-weight: bold; cursor: pointer; }
        #reader { width: 100%; margin-top: 10px; border-radius: 15px; overflow: hidden; }
        .stats { display: flex; justify-content: space-around; margin: 20px 0; }
    </style>
</head>
<body>
    <div class="header">REAL INSTANT FOODS | AI Dispatcher</div>
    
    <div class="grid">
        <div class="card">
            <h3>⚡ AI High-Speed Scanner</h3>
            <div id="reader"></div>
            <button onclick="startScanner()">Start AI Scan</button>
        </div>
        <div class="card">
            <h3>📋 Manual Entry / CSV</h3>
            <form action="/upload_file" method="POST" enctype="multipart/form-data">
                <input type="file" name="po_file" accept=".csv"><button type="submit">Sync CSV</button>
            </form>
        </div>
    </div>

    <script>
        function startScanner() {
            const scanner = new Html5Qrcode("reader");
            scanner.start({facingMode: "environment"}, {fps: 30, qrbox: 200}, (data) => {
                scanner.stop();
                let pack = prompt("Scanned: " + data + "\\nEnter Quantity (in master units):", "1");
                if(pack) {
                    fetch('/process_scan', {
                        method: 'POST',
                        body: 'barcode='+data+'&qty='+pack,
                        headers: {'Content-Type': 'application/x-www-form-urlencoded'}
                    }).then(() => location.reload());
                }
            });
        }
    </script>
</body>
</html>
"""

# AI Logic: पैक्स के अनुसार ऑटो कैलकुलेशन
def calculate_total(item_name, weight, master_units):
    rules = {
        "1kg": 25, "500g": 50, "2kg": 12, "200g": 100, "5kg": 12
    }
    # अगर 10kg या 26kg है तो loose (1=1), वरना रूल के हिसाब से
    multiplier = rules.get(weight, 1) 
    return int(master_units) * multiplier

@app.route('/')
def home():
    return render_template_string(DASHBOARD_HTML)

@app.route('/process_scan', methods=['POST'])
def process_scan():
    b = request.form['barcode']
    m_units = request.form['qty']
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT item_name, weight FROM po_items WHERE barcode = ?', (b,))
    item = cursor.fetchone()
    if item:
        total = calculate_total(item[0], item[1], m_units)
        cursor.execute('INSERT INTO dispatch_log (product_name, loaded_qty, timestamp) VALUES (?, ?, ?)', 
                       (item[0], total, datetime.now().strftime("%H:%M:%S")))
        conn.commit()
    conn.close()
    return '', 200

if __name__ == '__main__': app.run(host='0.0.0.0', port=5000)
