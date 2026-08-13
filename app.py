from flask import Flask, render_template_string, request, redirect, session
import sqlite3, io
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'real_instant_foods_final_pro_2026'

# --- डेटाबेस इनिशियलाइज़ेशन ---
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
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <title>REAL INSTANT FOODS | AI ERP</title>
    <script src="https://unpkg.com/html5-qrcode"></script>
    <style>
        :root { --primary: #2563eb; --bg: #020617; --card: #1e293b; --text: #f8fafc; }
        body { font-family: 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); padding: 20px; }
        .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); justify-content: center; align-items: center; }
        .modal-content { background: var(--card); padding: 25px; border-radius: 20px; width: 350px; text-align: center; border: 1px solid var(--primary); }
        .product-info { background: #334155; padding: 15px; border-radius: 10px; margin: 15px 0; text-align: left; }
        button { background: var(--primary); color: white; padding: 15px; border: none; border-radius: 10px; width: 100%; font-weight: bold; cursor: pointer; }
    </style>
</head>
<body>
    <h1 style="text-align:center;">REAL INSTANT FOODS</h1>
    
    <!-- Custom AI Scan Modal -->
    <div id="scanModal" class="modal">
        <div class="modal-content">
            <h3>✅ प्रोडक्ट विवरण (Product Details)</h3>
            <div id="productDetails" class="product-info"></div>
            <input type="number" id="qtyInput" placeholder="मास्टर बैग्स/संख्या" value="1" style="padding:10px; width:90%; border-radius:5px;">
            <button onclick="submitQty()" style="margin-top:15px;">Confirm Loading</button>
        </div>
    </div>

    <div id="reader" style="width:100%; border-radius:15px; overflow:hidden;"></div>
    <button onclick="startScanner()">⚡ Start AI High-Speed Scan</button>

    <script>
        let currentBarcode = "";
        function startScanner() {
            document.getElementById('reader').style.display = 'block';
            const scanner = new Html5Qrcode("reader");
            scanner.start({facingMode: "environment"}, {fps: 30, qrbox: 200}, (data) => {
                scanner.stop();
                document.getElementById('reader').style.display = 'none';
                currentBarcode = data;
                
                // सर्वर से प्रोडक्ट की जानकारी लाएं
                fetch('/get_product_info?barcode=' + data)
                .then(res => res.json())
                .then(info => {
                    if(info.error) { alert("प्रोडक्ट नहीं मिला!"); return; }
                    document.getElementById('productDetails').innerHTML = 
                        `<b>नाम:</b> ${info.name}<br><b>वजन:</b> ${info.weight}<br><b>EAN:</b> ${data}`;
                    document.getElementById('scanModal').style.display = 'flex';
                });
            });
        }
        function submitQty() {
            let qty = document.getElementById('qtyInput').value;
            fetch('/process_scan', {
                method: 'POST',
                body: 'barcode=' + encodeURIComponent(currentBarcode) + '&qty=' + qty,
                headers: {'Content-Type': 'application/x-www-form-urlencoded'}
            }).then(() => location.reload());
        }
    </script>
</body>
</html>
"""

@app.route('/')
def home():
    if not session.get('logged_in'): return redirect('/login')
    return render_template_string(DASHBOARD_HTML)

@app.route('/get_product_info')
def get_product_info():
    barcode = request.args.get('barcode')
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT item_name, weight FROM po_items WHERE barcode = ?', (barcode,))
    item = cursor.fetchone()
    conn.close()
    if item:
        return {"name": item[0], "weight": item[1]}
    return {"error": "Not Found"}

@app.route('/process_scan', methods=['POST'])
def process_scan():
    b = request.form['barcode'].strip()
    m_units = request.form['qty']
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT item_name, weight FROM po_items WHERE barcode = ?', (b,))
    item = cursor.fetchone()
    if item:
        # कैलकुलेशन रूल्स
        w = item[1].lower()
        multiplier = 25 if "1kg" in w else 50 if "500g" in w else 12 if "2kg" in w or "5kg" in w else 100 if "200g" in w else 1
        total = int(m_units) * multiplier
        cursor.execute('INSERT INTO dispatch_log (product_name, loaded_qty, timestamp) VALUES (?, ?, ?)', 
                       (f"{item[0]} ({item[1]})", total, datetime.now().strftime("%H:%M")))
        conn.commit()
    conn.close()
    return '', 200

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST' and request.form.get('password') == 'real@8283':
        session['logged_in'] = True
        return redirect('/')
    return "<body style='background:#020617; display:flex; justify-content:center; align-items:center; height:100vh;'><form method='POST' style='background:#1e293b; padding:40px; border-radius:20px;'><input type='password' name='password' style='padding:10px; width:100%; margin-bottom:10px;'><button type='submit' style='padding:10px; width:100%;'>Login</button></form></body>"

if __name__ == '__main__': app.run(host='0.0.0.0', port=5000)
