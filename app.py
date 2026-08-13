from flask import Flask, render_template_string, request, redirect, session
import sqlite3, csv, io
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'real_instant_foods_master_2026'

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
    <title>REAL INSTANT FOODS | Advanced ERP</title>
    <script src="https://unpkg.com/html5-qrcode"></script>
    <style>
        :root { --primary: #2563eb; --bg: #0f172a; --card: #1e293b; --text: #f8fafc; }
        body { font-family: 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); padding: 20px; margin: 0; }
        .header { text-align: center; border-bottom: 2px solid var(--primary); padding-bottom: 10px; margin-bottom: 20px; }
        .header h1 { color: var(--primary); letter-spacing: 2px; margin: 0; font-size: 2rem; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 20px; margin-bottom: 25px; }
        .card { background: var(--card); padding: 20px; border-radius: 15px; border: 1px solid #334155; box-shadow: 0 4px 15px rgba(0,0,0,0.3); }
        input, select, button { width: 100%; padding: 12px; margin: 8px 0; border-radius: 8px; border: none; background: #334155; color: white; box-sizing: border-box; }
        button { background: var(--primary); font-weight: bold; cursor: pointer; }
        button:hover { background: #1d4ed8; }
        #reader { width: 100%; border-radius: 10px; overflow: hidden; margin-bottom: 10px; display: none; }
        table { width: 100%; border-collapse: collapse; background: var(--card); border-radius: 10px; overflow: hidden; }
        th { background: #334155; padding: 12px; text-align: left; }
        td { padding: 12px; border-bottom: 1px solid #475569; }
        .badge { background: #059669; padding: 4px 8px; border-radius: 5px; font-size: 0.8rem; }
    </style>
</head>
<body>
    <div class="header">
        <h1>REAL INSTANT FOODS</h1>
        <p>AI-Powered Logistics & Smart Dispatch ERP</p>
    </div>
    
    <div class="grid">
        <!-- 1. AI स्कैनर -->
        <div class="card" style="border: 2px solid #8b5cf6;">
            <h3 style="color: #a78bfa;">⚡ AI सुपर-फास्ट बारकोड स्कैनर</h3>
            <div id="reader"></div>
            <button onclick="startScanner()" style="background: linear-gradient(135deg, #8b5cf6, #6d28d9);">कैमरा चालू करें (1-Sec Scan)</button>
        </div>

        <!-- 2. PO लोडिंग और लॉजिस्टिक्स -->
        <div class="card">
            <h3>📦 PO लोडिंग & व्हीकल डिटेल्स</h3>
            <form action="/load_po" method="POST">
                <select name="po_number" required>
                    <option value="">-- उपलब्ध PO चुनें --</option>
                    {% for po in po_list %}<option value="{{ po[0] }}">PO: {{ po[0] }}</option>{% endfor %}
                </select>
                <input type="text" name="vehicle_no" placeholder="गाड़ी नंबर (Vehicle No, e.g. KA-01)" required>
                <input type="text" name="location" placeholder="डिलीवरी लोकेशन (Destination)" required>
                <button type="submit" style="background: #10b981;">लोडिंग शुरू करें</button>
            </form>
        </div>

        <!-- 3. CSV अपलोड -->
        <div class="card">
            <h3>📁 CSV फाइल सिंक करें</h3>
            <form action="/upload_file" method="POST" enctype="multipart/form-data">
                <input type="file" name="po_file" accept=".csv" required>
                <button type="submit" style="background: #f59e0b;">CSV अपलोड करें</button>
            </form>
        </div>
    </div>

    <!-- लाइव डिस्पैच टेबल -->
    <div class="card">
        <h3>📋 लाइव डिस्पैच हिस्ट्री (Live Dispatches)</h3>
        <table>
            <thead>
                <tr>
                    <th>PO नंबर</th>
                    <th>गाड़ी नंबर</th>
                    <th>लोकेशन</th>
                    <th>प्रोडक्ट विवरण</th>
                    <th>कुल मात्रा (Qty)</th>
                    <th>समय</th>
                </tr>
            </thead>
            <tbody>
                {% for log in logs %}
                <tr>
                    <td><b>{{ log[1] }}</b></td>
                    <td>{{ log[2] }}</td>
                    <td>{{ log[3] }}</td>
                    <td>{{ log[4] }}</td>
                    <td style="color: #34d399; font-weight: bold;">{{ log[5] }} pcs</td>
                    <td>{{ log[6] }}</td>
                </tr>
                {% else %}
                <tr><td colspan="6" style="text-align:center; color:#94a3b8;">कोई रिकॉर्ड उपलब्ध नहीं है।</td></tr>
                {% endfor %}
            </tbody>
        </table>
    </div>

    <script>
        function startScanner() {
            document.getElementById('reader').style.display = 'block';
            const scanner = new Html5Qrcode("reader");
            scanner.start({facingMode: "environment"}, {fps: 30, qrbox: 250}, (data) => {
                scanner.stop();
                document.getElementById('reader').style.display = 'none';
                let packs = prompt("⚡ बारकोड स्कैन हुआ: " + data + "\\n\\nमास्टर बैग्स या बॉक्स की संख्या दर्ज करें:", "1");
                if(packs) {
                    fetch('/process_scan', {
                        method: 'POST',
                        body: 'barcode=' + encodeURIComponent(data) + '&qty=' + packs,
                        headers: {'Content-Type': 'application/x-www-form-urlencoded'}
                    }).then(res => {
                        if(res.ok) { alert("सफलतापूर्वक जुड़ गया!"); location.reload(); }
                        else { alert("यह बारकोड डेटाबेस में नहीं मिला!"); location.reload(); }
                    });
                }
            }).catch(err => { alert("कैमरा एरर: " + err); });
        }
    </script>
</body>
</html>
"""

def calculate_qty(weight, master_units):
    w = weight.lower().strip()
    units = int(master_units)
    # आपके बताए नियमों के अनुसार ऑटोमैटिक कैलकुलेशन
    if "1kg" in w: return units * 25
    elif "500g" in w: return units * 50
    elif "2kg" in w: return units * 12
    elif "200g" in w: return units * 100
    elif "5kg" in w: return units * 12
    else: 
        # 10kg, 26kg या अन्य लूज आइटम्स के लिए सीधे पीस गिने जाएंगे
        return units

@app.route('/')
def home():
    if not session.get('logged_in'): return redirect('/login')
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT DISTINCT po_number FROM po_items')
    po_list = cursor.fetchall()
    cursor.execute('SELECT * FROM dispatch_log ORDER BY id DESC')
    logs = cursor.fetchall()
    conn.close()
    return render_template_string(DASHBOARD_HTML, po_list=po_list, logs=logs)

@app.route('/process_scan', methods=['POST'])
def process_scan():
    if not session.get('logged_in'): return '', 403
    barcode = request.form['barcode'].strip()
    m_units = request.form['qty']
    
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT item_name, weight, po_number FROM po_items WHERE barcode = ?', (barcode,))
    item = cursor.fetchone()
    
    if item:
        total_qty = calculate_qty(item[1], m_units)
        time_now = datetime.now().strftime("%d-%m-%Y %H:%M")
        cursor.execute('INSERT INTO dispatch_log (po_number, vehicle_no, location, product_name, loaded_qty, timestamp) VALUES (?, ?, ?, ?, ?, ?)', 
                       ("Auto-Scan", "N/A", "N/A", f"{item[0]} ({item[1]})", total_qty, time_now))
        conn.commit()
        conn.close()
        return '', 200
    
    conn.close()
    return '', 404

@app.route('/load_po', methods=['POST'])
def load_po():
    if not session.get('logged_in'): return redirect('/')
    po = request.form['po_number']
    v_no = request.form['vehicle_no']
    loc = request.form['location']
    
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT item_name, weight FROM po_items WHERE po_number = ?', (po,))
    items = cursor.fetchall()
    time_now = datetime.now().strftime("%d-%m-%Y %H:%M")
    
    for item in items:
        cursor.execute('INSERT INTO dispatch_log (po_number, vehicle_no, location, product_name, loaded_qty, timestamp) VALUES (?, ?, ?, ?, ?, ?)', 
                       (po, v_no, loc, f"{item[0]} ({item[1]})", 0, time_now))
    conn.commit()
    conn.close()
    return redirect('/')

@app.route('/upload_file', methods=['POST'])
def upload_file():
    if not session.get('logged_in'): return redirect('/')
    file = request.files['po_file']
    stream = io.TextIOWrapper(file.stream, encoding="utf-8")
    reader = csv.DictReader(stream)
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    for row in reader:
        po = row.get('PoNumber') or row.get('PO Number') or ''
        desc = row.get('SkuDesc') or row.get('Item') or ''
        qty = row.get('Quantity') or row.get('Qty') or '0'
        ean = row.get('EAN') or row.get('Barcode') or ''
        if po and desc:
            cursor.execute('INSERT INTO po_items (po_number, item_name, weight, ordered_qty, barcode) VALUES (?, ?, ?, ?, ?)', 
                           (po.strip(), desc.strip(), "1 pack", int(float(qty)) if qty else 0, ean.strip()))
    conn.commit()
    conn.close()
    return redirect('/')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST' and request.form.get('password') == 'real@8283':
        session['logged_in'] = True
        return redirect('/')
    return "<body style='background:#0f172a; display:flex; justify-content:center; align-items:center; height:100vh;'><form method='POST' style='background:#1e293b; padding:30px; border-radius:15px;'><h2 style='color:white;'>REAL INSTANT FOODS</h2><input type='password' name='password' placeholder='Password' style='padding:10px; width:100%; margin-bottom:10px;'><button type='submit' style='padding:10px; width:100%; background:#2563eb; color:white; border:none;'>Login</button></form></body>"

if __name__ == '__main__': app.run(host='0.0.0.0', port=5000)
