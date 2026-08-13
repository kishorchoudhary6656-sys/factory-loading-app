from flask import Flask, render_template_string, request, redirect, session
import sqlite3
import csv
import io
import random

app = Flask(__name__)
app.secret_key = 'factory_secret_key_secure_999'

def init_db():
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS po_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            po_number TEXT NOT NULL,
            item_name TEXT NOT NULL,
            weight TEXT NOT NULL,
            ordered_qty INTEGER NOT NULL,
            barcode TEXT DEFAULT '8900000000'
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS loading_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            po_number TEXT,
            product_name TEXT,
            total_ordered INTEGER,
            box_size INTEGER,
            loaded_qty INTEGER DEFAULT 0,
            barcode TEXT DEFAULT ''
        )
    ''')
    
    cursor.execute('SELECT COUNT(*) FROM po_items')
    if cursor.fetchone()[0] == 0:
        sample_data = [
            ("P5229701", "Daily Good Raw Peanut / Singdana", "500g", 1620, "8909177015591"),
            ("P5229701", "Daily Good Idli Rice", "1 kg", 540, "8909177015874")
        ]
        cursor.executemany('INSERT INTO po_items (po_number, item_name, weight, ordered_qty, barcode) VALUES (?, ?, ?, ?, ?)', sample_data)
        conn.commit()
        
    conn.close()

init_db()

LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FactoryFlow - Login</title>
    <meta name="google-site-verification" content="db57yD1AuDoUejtp" />
    <style>
        body { font-family: Arial, sans-serif; background: #0f172a; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .login-card { background: white; padding: 30px; border-radius: 12px; width: 100%; max-width: 350px; box-shadow: 0 10px 25px rgba(0,0,0,0.3); text-align: center; }
        input { width: 100%; padding: 12px; margin: 15px 0; border: 1px solid #cbd5e1; border-radius: 8px; box-sizing: border-box; font-size: 1rem; }
        button { width: 100%; padding: 12px; background: #2563eb; color: white; border: none; border-radius: 8px; font-size: 1rem; font-weight: bold; cursor: pointer; }
        button:hover { background: #1d4ed8; }
        .error { color: #dc2626; font-size: 0.85rem; margin-top: 5px; }
    </style>
</head>
<body>
    <div class="login-card">
        <h2>🔒 फैक्ट्री लॉगिन</h2>
        <p style="color: #64748b; font-size: 0.9rem;">कृपया पासवर्ड दर्ज करें</p>
        <form action="/login" method="POST">
            <input type="password" name="password" placeholder="पासवर्ड डालें" required>
            <button type="submit">लॉगिन करें</button>
            {% if error %}
            <div class="error">{{ error }}</div>
            {% endif %}
        </form>
    </div>
</body>
</html>
"""

DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FactoryFlow AI ERP - Secure</title>
    <meta name="google-site-verification" content="db57yD1AuDoUejtp" />
    <script src="https://unpkg.com/html5-qrcode" type="text/javascript"></script>
    <style>
        :root { --primary: #2563eb; --secondary: #10b981; --dark: #0f172a; --bg: #f8fafc; }
        body { font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background: var(--bg); margin: 0; color: #334155; }
        .navbar { background: var(--dark); padding: 1.2rem 2rem; color: white; display: flex; align-items: center; justify-content: space-between; box-shadow: 0 4px 12px rgba(0,0,0,0.15); }
        .navbar h1 { margin: 0; font-size: 1.5rem; font-weight: 700; display: flex; align-items: center; gap: 10px; }
        .logout-btn { background: #dc2626; color: white; padding: 0.5rem 1rem; border-radius: 8px; text-decoration: none; font-size: 0.85rem; font-weight: bold; }
        
        .container { max-width: 1100px; margin: 2rem auto; padding: 0 1rem; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1.5rem; margin-bottom: 2.5rem; }
        .card { background: white; border-radius: 16px; padding: 1.5rem; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05); border: 1px solid #e2e8f0; }
        .card-title { font-size: 1.1rem; font-weight: 700; color: var(--dark); margin-top: 0; margin-bottom: 1.2rem; }
        
        input, select { width: 100%; padding: 0.8rem; margin-bottom: 1rem; border: 1px solid #cbd5e1; border-radius: 10px; font-size: 0.95rem; box-sizing: border-box; background: #f8fafc; }
        .btn { width: 100%; padding: 0.9rem; border: none; border-radius: 10px; font-weight: 600; font-size: 1rem; color: white; cursor: pointer; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
        .btn-upload { background: linear-gradient(135deg, #f59e0b, #ea580c); }
        .btn-load { background: linear-gradient(135deg, #10b981, #059669); }
        .btn-scan { background: linear-gradient(135deg, #8b5cf6, #6d28d9); }
        .btn-manual { background: linear-gradient(135deg, #0284c7, #0369a1); margin-top: 5px; }
        
        #reader { width: 100%; border-radius: 10px; overflow: hidden; display: none; margin-bottom: 1rem; }
        
        .table-container { background: white; border-radius: 16px; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.1); border: 1px solid #e2e8f0; overflow: hidden; }
        .table-header { padding: 1.5rem; background: #f8fafc; border-bottom: 1px solid #e2e8f0; }
        .table-header h3 { margin: 0; font-size: 1.2rem; color: var(--dark); }
        
        table { width: 100%; border-collapse: collapse; text-align: left; }
        th { background: #f1f5f9; padding: 1rem 1.5rem; font-weight: 700; color: #475569; font-size: 0.85rem; text-transform: uppercase; }
        td { padding: 1.2rem 1.5rem; border-bottom: 1px solid #e2e8f0; vertical-align: middle; color: #1e293b; font-weight: 500; }
        tr:hover { background: #f8fafc; }
        
        .text-green { color: #16a34a; font-weight: 800; font-size: 1.1rem; }
        .text-red { color: #dc2626; font-weight: 800; font-size: 1.1rem; }
        .loose-badge { color: #d97706; font-size: 0.85rem; background: #fef3c7; padding: 0.2rem 0.6rem; border-radius: 6px; display: inline-block; margin-top: 4px; }
        .barcode-badge { color: #475569; font-size: 0.85rem; background: #f1f5f9; padding: 0.2rem 0.5rem; border-radius: 4px; font-family: monospace; display: inline-block; margin-top: 4px; border: 1px solid #cbd5e1; }
        
        .update-form { display: flex; gap: 0.5rem; align-items: center; justify-content: center; }
        .update-input { width: 75px; margin: 0; padding: 0.6rem; text-align: center; font-weight: bold; }
        .btn-update { width: auto; padding: 0.6rem 1rem; background: var(--dark); color: white; font-size: 0.85rem; border-radius: 8px; border:none; cursor:pointer; }
    </style>
</head>
<body>

    <header class="navbar">
        <h1>🔒 FactoryFlow AI <span style="font-weight:400; font-size:1.1rem; color:#94a3b8;">| Secure ERP</span></h1>
        <a href="/logout" class="logout-btn">लॉगआउट (Logout)</a>
    </header>

    <div class="container">
        <div class="grid">
            <div class="card" style="border: 2px solid #8b5cf6;">
                <h2 class="card-title" style="color: #6d28d9;">📷 बारकोड स्कैनर & मैन्युअल एंट्री</h2>
                <div id="reader"></div>
                <button type="button" onclick="startScanner()" class="btn btn-scan" id="scanBtn">कैमरा चालू करके स्कैन करें</button>
                
                <form action="/manual_barcode" method="POST" style="margin-top: 15px;">
                    <input type="text" name="barcode" placeholder="या बारकोड नंबर टाइप करें" required style="margin-bottom: 5px; font-size: 0.9rem;">
                    <button type="submit" class="btn btn-manual">टाइप करके जोड़े (+25Qty)</button>
                </form>
                <div id="scan-result" style="margin-top: 10px; font-weight: bold; color: #10b981; font-size: 0.9rem;"></div>
            </div>

            <div class="card">
                <h2 class="card-title">📦 पेंडिंग PO लोड करें</h2>
                <form action="/load_po" method="POST">
                    <select name="po_number" required>
                        <option value="">-- उपलब्ध PO चुनें --</option>
                        {% for po in po_list %}
                        <option value="{{ po[0] }}">PO: {{ po[0] }}</option>
                        {% endfor %}
                    </select>
                    <button type="submit" class="btn btn-load">लोडिंग लिस्ट में जोड़ें</button>
                </form>
            </div>

            <div class="card">
                <h2 class="card-title">📁 PO फाइल (CSV) अपलोड</h2>
                <form action="/upload_file" method="POST" enctype="multipart/form-data">
                    <input type="file" name="po_file" accept=".csv" required>
                    <button type="submit" class="btn btn-upload">फाइल अपलोड करें</button>
                </form>
            </div>
        </div>

        <div class="table-container">
            <div class="table-header">
                <h3>🚛 लाइव डिस्पैच & मास्टर बैग ट्रैकिंग</h3>
            </div>
            <div style="overflow-x:auto;">
                <table>
                    <thead>
                        <tr>
                            <th>PO नंबर</th>
                            <th>प्रोडक्ट / साइज़</th>
                            <th>EAN / Barcode</th>
                            <th>मास्टर बैग हिसाब</th>
                            <th>लोड हुआ</th>
                            <th>बाकी (Pending)</th>
                            <th style="text-align:center;">मैनुअल अपडेट</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for item in loads %}
                        <tr>
                            <td><b>{{ item[1] }}</b></td>
                            <td>{{ item[2] }}</td>
                            <td><span class="barcode-badge">{{ item[6] }}</span></td>
                            <td>
                                {% set bags = item[3] // item[4] %}
                                {% set loose = item[3] % item[4] %}
                                {{ bags }} बैग्स × {{ item[4] }}pcs<br>
                                {% if loose > 0 %} <span class="loose-badge">+ 1 लूज बैग × {{ loose }}pcs</span><br> {% endif %}
                                <span style="color:#64748b; font-size:0.8rem;">(कुल: {{ item[3] }} pcs)</span>
                            </td>
                            <td class="text-green">{{ item[5] }} pcs</td>
                            <td class="text-red">{{ item[3] - item[5] }} pcs</td>
                            <td>
                                <form action="/update_load" method="POST" class="update-form">
                                    <input type="hidden" name="id" value="{{ item[0] }}">
                                    <input type="number" name="add_qty" placeholder="Qty" required class="update-input">
                                    <button type="submit" class="btn-update">Add</button>
                                </form>
                            </td>
                        </tr>
                        {% else %}
                        <tr>
                            <td colspan="7" style="text-align:center; padding:3rem; color:#94a3b8;">
                                📋 लोडिंग के लिए कोई PO सिलेक्ट नहीं किया गया है। ऊपर दिए गए बॉक्स से PO चुनें।
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        function startScanner() {
            document.getElementById('reader').style.display = 'block';
            document.getElementById('scanBtn').style.display = 'none';
            const html5QrCode = new Html5Qrcode("reader");
            html5QrCode.start(
                { facingMode: "environment" }, 
                { fps: 10, qrbox: { width: 250, height: 150 } },
                (decodedText, decodedResult) => {
                    document.getElementById('scan-result').innerText = "स्कैन हुआ: " + decodedText;
                    fetch('/scan_update', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                        body: 'barcode=' + encodeURIComponent(decodedText)
                    }).then(response => { if(response.ok) { window.location.reload(); } });
                },
                (errorMessage) => {}
            ).catch(err => { alert("कैमरा एरर: " + err); });
        }
    </script>
</body>
</html>
"""

@app.route('/')
def home():
    if not session.get('logged_in'):
        return render_template_string(LOGIN_TEMPLATE)
    
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT DISTINCT po_number FROM po_items')
    po_list = cursor.fetchall()
    
    cursor.execute('SELECT * FROM loading_log')
    loads = cursor.fetchall()
    conn.close()
    return render_template_string(DASHBOARD_TEMPLATE, po_list=po_list, loads=loads)

@app.route('/login', methods=['POST'])
def login():
    entered_password = request.form.get('password')
    if entered_password == 'real@8283':
        session['logged_in'] = True
        return redirect('/')
    else:
        return render_template_string(LOGIN_TEMPLATE, error="गलत पासवर्ड! कृपया दोबारा कोशिश करें।")

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect('/')

@app.route('/manual_barcode', methods=['POST'])
def manual_barcode():
    if not session.get('logged_in'): return redirect('/')
    barcode = request.form.get('barcode').strip()
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT item_name, weight, po_number FROM po_items WHERE barcode = ?', (barcode,))
    item = cursor.fetchone()
    
    if item:
        name_weight = f"{item[0]} ({item[1]})"
        po_no = item[2]
        cursor.execute('SELECT id FROM loading_log WHERE po_number = ? AND product_name = ?', (po_no, name_weight))
        if not cursor.fetchone():
            cursor.execute('INSERT INTO loading_log (po_number, product_name, total_ordered, box_size, loaded_qty, barcode) VALUES (?, ?, 500, 25, 0, ?)',
                           (po_no, name_weight, barcode))
        cursor.execute('UPDATE loading_log SET loaded_qty = loaded_qty + 25 WHERE po_number = ? AND product_name = ?', (po_no, name_weight))
        conn.commit()
    
    conn.close()
    return redirect('/')

@app.route('/scan_update', methods=['POST'])
def scan_update():
    if not session.get('logged_in'): return '', 403
    barcode = request.form.get('barcode')
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT item_name, weight, po_number FROM po_items WHERE barcode = ?', (barcode,))
    item = cursor.fetchone()
    if item:
        name_weight = f"{item[0]} ({item[1]})"
        po_no = item[2]
        cursor.execute('UPDATE loading_log SET loaded_qty = loaded_qty + 25 WHERE po_number = ? AND product_name = ?', (po_no, name_weight))
        conn.commit()
    conn.close()
    return '', 204

@app.route('/load_po', methods=['POST'])
def load_po():
    if not session.get('logged_in'): return redirect('/')
    po_number = request.form['po_number']
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT item_name, weight, ordered_qty, barcode FROM po_items WHERE po_number = ?', (po_number,))
    items = cursor.fetchall()
    for item in items:
        name_weight = f"{item[0]} ({item[1]})"
        qty = item[2]
        b_code = item[3]
        cursor.execute('SELECT id FROM loading_log WHERE po_number = ? AND product_name = ?', (po_number, name_weight))
        if not cursor.fetchone():
            cursor.execute('INSERT INTO loading_log (po_number, product_name, total_ordered, box_size, loaded_qty, barcode) VALUES (?, ?, ?, 25, 0, ?)',
                           (po_number, name_weight, qty, b_code))
    conn.commit()
    conn.close()
    return redirect('/')

@app.route('/upload_file', methods=['POST'])
def upload_file():
    if not session.get('logged_in'): return redirect('/')
    if 'po_file' in request.files:
        file = request.files['po_file']
        if file.filename != '':
            try:
                stream = io.TextIOWrapper(file.stream, encoding="utf-8")
                reader = csv.DictReader(stream)
                conn = sqlite3.connect('factory.db')
                cursor = conn.cursor()
                
                for row in reader:
                    po_no = row.get('PoNumber', '').strip()
                    desc = row.get('SkuDesc', '').strip()
                    qty = row.get('Quantity', '0').strip()
                    ean = row.get('EAN', '').strip()
                    
                    if po_no and desc:
                        cursor.execute('SELECT id FROM po_items WHERE po_number = ? AND item_name = ?', (po_no, desc))
                        if not cursor.fetchone():
                            cursor.execute('INSERT INTO po_items (po_number, item_name, weight, ordered_qty, barcode) VALUES (?, ?, ?, ?, ?)',
                                           (po_no, desc, "1 pack", int(float(qty)) if qty else 0, ean))
                conn.commit()
                conn.close()
            except Exception as e:
                print("Error:", e)
    return redirect('/')

@app.route('/update_load', methods=['POST'])
def update_load():
    if not session.get('logged_in'): return redirect('/')
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE loading_log SET loaded_qty = loaded_qty + ? WHERE id = ?', 
                   (int(request.form['add_qty']), request.form['id']))
    conn.commit()
    conn.close()
    return redirect('/')

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5000)
