from flask import Flask, render_template_string, request, redirect, session
import sqlite3
import csv
import io

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
            ("P5229701", "RAW PEANUT / SINGDANA", "500g", 1620, "8909177015591"),
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
    <title>FactoryFlow Pro - Advanced ERP</title>
    <script src="https://unpkg.com/html5-qrcode" type="text/javascript"></script>
    <style>
        :root { --primary: #2563eb; --secondary: #10b981; --dark: #0f172a; --bg: #f8fafc; }
        body { font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background: var(--bg); margin: 0; color: #334155; }
        .navbar { background: var(--dark); padding: 1.2rem 2rem; color: white; display: flex; align-items: center; justify-content: space-between; box-shadow: 0 4px 12px rgba(0,0,0,0.15); }
        .navbar h1 { margin: 0; font-size: 1.5rem; font-weight: 700; display: flex; align-items: center; gap: 10px; }
        .logout-btn { background: #dc2626; color: white; padding: 0.5rem 1rem; border-radius: 8px; text-decoration: none; font-size: 0.85rem; font-weight: bold; }
        
        .container { max-width: 1200px; margin: 2rem auto; padding: 0 1rem; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 1.5rem; margin-bottom: 2.5rem; }
        .card { background: white; border-radius: 16px; padding: 1.5rem; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05); border: 1px solid #e2e8f0; }
        .card-title { font-size: 1.1rem; font-weight: 700; color: var(--dark); margin-top: 0; margin-bottom: 1.2rem; }
        
        input, select { width: 100%; padding: 0.8rem; margin-bottom: 1rem; border: 1px solid #cbd5e1; border-radius: 10px; font-size: 0.95rem; box-sizing: border-box; background: #f8fafc; }
        .btn { width: 100%; padding: 0.9rem; border: none; border-radius: 10px; font-weight: 600; font-size: 1rem; color: white; cursor: pointer; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
        .btn-upload { background: linear-gradient(135deg, #f59e0b, #ea580c); }
        .btn-load { background: linear-gradient(135deg, #10b981, #059669); }
        .btn-scan { background: linear-gradient(135deg, #8b5cf6, #6d28d9); }
        .btn-manual { background: linear-gradient(135deg, #0284c7, #0369a1); }
        
        #reader { width: 100%; border-radius: 10px; overflow: hidden; display: none; margin-bottom: 1rem; }
        
        .table-container { background: white; border-radius: 16px; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.1); border: 1px solid #e2e8f0; overflow: hidden; }
        .table-header { padding: 1.5rem; background: #f8fafc; border-bottom: 1px solid #e2e8f0; display: flex; justify-content: space-between; align-items: center; }
        .table-header h3 { margin: 0; font-size: 1.2rem; color: var(--dark); }
        
        table { width: 100%; border-collapse: collapse; text-align: left; }
        th { background: #f1f5f9; padding: 1rem 1.5rem; font-weight: 700; color: #475569; font-size: 0.85rem; text-transform: uppercase; }
        td { padding: 1.2rem 1.5rem; border-bottom: 1px solid #e2e8f0; vertical-align: middle; color: #1e293b; font-weight: 500; }
        tr:hover { background: #f8fafc; }
        
        .text-green { color: #16a34a; font-weight: 800; font-size: 1.1rem; }
        .text-red { color: #dc2626; font-weight: 800; font-size: 1.1rem; }
        .loose-badge { color: #d97706; font-size: 0.85rem; background: #fef3c7; padding: 0.2rem 0.6rem; border-radius: 6px; display: inline-block; margin-top: 4px; }
        .barcode-badge { color: #475569; font-size: 0.85rem; background: #f1f5f9; padding: 0.2rem 0.5rem; border-radius: 4px; font-family: monospace; display: inline-block; margin-top: 4px; border: 1px solid #cbd5e1; }
        
        .update-form { display: flex; gap: 0.4rem; align-items: center; justify-content: center; }
        .update-input { width: 65px; margin: 0; padding: 0.5rem; text-align: center; font-weight: bold; }
        .btn-update { padding: 0.5rem 0.8rem; background: var(--dark); color: white; font-size: 0.8rem; border-radius: 6px; border:none; cursor:pointer; }
        .btn-edit-qty { background: #d97706; }
    </style>
</head>
<body>

    <header class="navbar">
        <h1>🔒 FactoryFlow Pro <span style="font-weight:400; font-size:1.1rem; color:#94a3b8;">| Smart Scanner & ERP</span></h1>
        <a href="/logout" class="logout-btn">लॉगआउट (Logout)</a>
    </header>

    <div class="container">
        <div class="grid">
            <!-- सुपर फास्ट स्मार्ट स्कैनर -->
            <div class="card" style="border: 2px solid #8b5cf6;">
                <h2 class="card-title" style="color: #6d28d9;">📷 स्मार्ट बारकोड स्कैनर (डिटेक्ट & पॉप-अप)</h2>
                <div id="reader"></div>
                <button type="button" onclick="startScanner()" class="btn btn-scan" id="scanBtn">कैमरा चालू करें (Scan & Enter Qty)</button>
                
                <form action="/manual_scan" method="POST" style="margin-top: 15px;">
                    <input type="text" name="barcode" placeholder="या बारकोड यहाँ टाइप करें" required style="font-size: 0.9rem;">
                    <div style="display: flex; gap: 10px;">
                        <input type="number" name="box_size" value="50" placeholder="बॉक्स साइज (50)" required style="margin-bottom:0;">
                        <button type="submit" class="btn btn-manual" style="width: auto; padding: 0.8rem 1.2rem;">जोड़ें</button>
                    </div>
                </form>
                <div id="scan-result" style="margin-top: 10px; font-weight: bold; color: #10b981; font-size: 0.9rem;"></div>
            </div>

            <!-- पेंडिंग PO लोड करें -->
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

            <!-- CSV फाइल अपलोड -->
            <div class="card">
                <h2 class="card-title">📁 PO फाइल (CSV) अपलोड</h2>
                <form action="/upload_file" method="POST" enctype="multipart/form-data">
                    <input type="file" name="po_file" accept=".csv" required>
                    <button type="submit" class="btn btn-upload">फाइल अपलोड करें</button>
                </form>
            </div>
        </div>

        <!-- लाइव डिस्पैच टेबल -->
        <div class="table-container">
            <div class="table-header">
                <h3>🚛 लाइव डिस्पैच & शॉर्ट/एक्सेस मास्टर ट्रैकिंग</h3>
            </div>
            <div style="overflow-x:auto;">
                <table>
                    <thead>
                        <tr>
                            <th>PO नंबर</th>
                            <th>प्रोडक्ट विवरण</th>
                            <th>Barcode / EAN</th>
                            <th>मास्टर बैग हिसाब</th>
                            <th>कुल आर्डर (Target)</th>
                            <th>लोड हुआ</th>
                            <th>बाकी (Short/Pending)</th>
                            <th style="text-align:center;">शॉर्ट/एडिट कंट्रोल</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for item in loads %}
                        <tr>
                            <td><b>{{ item[1] }}</b></td>
                            <td>{{ item[2] }}</td>
                            <td><span class="barcode-badge">{{ item[6] }}</span></td>
                            <td>
                                {% set box = item[4] if item[4] > 0 else 50 %}
                                {% set bags = item[5] // box %}
                                {% set loose = item[5] % box %}
                                <b>{{ box }} pcs/bag</b><br>
                                {{ bags }} मास्टर बैग्स<br>
                                {% if loose > 0 %} <span class="loose-badge">+ {{ loose }} pcs loose</span> {% endif %}
                            </td>
                            <td style="font-weight: bold; color: #0284c7;">{{ item[3] }} pcs</td>
                            <td class="text-green">{{ item[5] }} pcs</td>
                            <td class="text-red">{{ item[3] - item[5] }} pcs</td>
                            <td>
                                <form action="/update_load" method="POST" class="update-form" style="margin-bottom: 4px;">
                                    <input type="hidden" name="id" value="{{ item[0] }}">
                                    <input type="hidden" name="action_type" value="add">
                                    <input type="number" name="qty" value="{{ box }}" class="update-input" required>
                                    <button type="submit" class="btn-update">+Add</button>
                                </form>
                                <form action="/update_load" method="POST" class="update-form">
                                    <input type="hidden" name="id" value="{{ item[0] }}">
                                    <input type="hidden" name="action_type" value="edit">
                                    <input type="number" name="qty" value="{{ item[5] }}" class="update-input" style="background:#fef3c7;" required title="सीधे सही लोड हुआ पीस दर्ज करें (Edit / Short)">
                                    <button type="submit" class="btn-update btn-edit-qty">Set/Edit</button>
                                </form>
                            </td>
                        </tr>
                        {% else %}
                        <tr>
                            <td colspan="8" style="text-align:center; padding:3rem; color:#94a3b8;">
                                📋 लोडिंग के लिए कोई PO सिलेक्ट नहीं किया गया है। ऊपर दिए गए बॉक्स से PO चुनें या बारकोड स्कैन करें।
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
                { fps: 20, qrbox: { width: 300, height: 120 } },
                (decodedText, decodedResult) => {
                    // तुरंत स्कैनर बंद करें
                    html5QrCode.stop();
                    
                    // पॉप-अप के जरिए बैग्स और लूज पीस की संख्या पूछें
                    let boxCount = prompt("✅ बारकोड स्कैन हुआ: " + decodedText + "\n\nकितने मास्टर बैग्स (Boxes) लोड किए?", "1");
                    if (boxCount !== null && boxCount.trim() !== "") {
                        let looseCount = prompt("और कितने लूज पीस (Loose Pcs) लोड किए?", "0");
                        if (looseCount !== null) {
                            // डेटा सर्वर पर भेजें
                            fetch('/manual_scan_smart', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                                body: `barcode=${encodeURIComponent(decodedText)}&box_count=${boxCount}&loose_count=${looseCount}`
                            }).then(response => { 
                                if(response.ok) { 
                                    window.location.reload(); 
                                } else {
                                    alert("❌ यह बारकोड सिस्टम के PO में नहीं मिला!");
                                    window.location.reload();
                                }
                            });
                        } else {
                            window.location.reload();
                        }
                    } else {
                        window.location.reload();
                    }
                },
                (errorMessage) => {}
            ).catch(err => { alert("कैमरा चालू करने में एरर: " + err); });
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
    if request.form.get('password') == 'real@8283':
        session['logged_in'] = True
        return redirect('/')
    return render_template_string(LOGIN_TEMPLATE, error="गलत पासवर्ड!")

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect('/')

@app.route('/googlec1dd36a62fa9245c.html')
def google_verify():
    return "google-site-verification: googlec1dd36a62fa9245c.html"

@app.route('/manual_scan_smart', methods=['POST'])
def manual_scan_smart():
    if not session.get('logged_in'): return '', 403
    barcode = request.form.get('barcode').strip()
    try:
        box_count = int(request.form.get('box_count', 0))
        loose_count = int(request.form.get('loose_count', 0))
    except ValueError:
        box_count = 1
        loose_count = 0
    
    box_size = 50 # मानक मास्टर बॉक्स साइज
    total_qty = (box_count * box_size) + loose_count
    
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT item_name, weight, po_number, ordered_qty FROM po_items WHERE barcode = ?', (barcode,))
    item = cursor.fetchone()
    
    if item:
        name_weight = f"{item[0]} ({item[1]})"
        po_no = item[2]
        target_qty = item[3]
        
        cursor.execute('SELECT id FROM loading_log WHERE po_number = ? AND product_name = ?', (po_no, name_weight))
        if not cursor.fetchone():
            cursor.execute('INSERT INTO loading_log (po_number, product_name, total_ordered, box_size, loaded_qty, barcode) VALUES (?, ?, ?, ?, 0, ?)',
                           (po_no, name_weight, target_qty, box_size, barcode))
        
        cursor.execute('UPDATE loading_log SET loaded_qty = loaded_qty + ?, box_size = ? WHERE po_number = ? AND product_name = ?', 
                       (total_qty, box_size, po_no, name_weight))
        conn.commit()
        conn.close()
        return '', 200
    
    conn.close()
    return '', 404

@app.route('/manual_scan', methods=['POST'])
def manual_scan():
    if not session.get('logged_in'): return redirect('/')
    barcode = request.form.get('barcode').strip()
    box_size = int(request.form.get('box_size', 50))
    
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT item_name, weight, po_number, ordered_qty FROM po_items WHERE barcode = ?', (barcode,))
    item = cursor.fetchone()
    
    if item:
        name_weight = f"{item[0]} ({item[1]})"
        po_no = item[2]
        target_qty = item[3]
        
        cursor.execute('SELECT id FROM loading_log WHERE po_number = ? AND product_name = ?', (po_no, name_weight))
        if not cursor.fetchone():
            cursor.execute('INSERT INTO loading_log (po_number, product_name, total_ordered, box_size, loaded_qty, barcode) VALUES (?, ?, ?, ?, 0, ?)',
                           (po_no, name_weight, target_qty, box_size, barcode))
        
        cursor.execute('UPDATE loading_log SET loaded_qty = loaded_qty + ?, box_size = ? WHERE po_number = ? AND product_name = ?', 
                       (box_size, box_size, po_no, name_weight))
        conn.commit()
    conn.close()
    return redirect('/')

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
            cursor.execute('INSERT INTO loading_log (po_number, product_name, total_ordered, box_size, loaded_qty, barcode) VALUES (?, ?, ?, 50, 0, ?)',
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
    item_id = request.form['id']
    action_type = request.form.get('action_type', 'add')
    qty = int(request.form['qty'])
    
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    if action_type == 'edit':
        cursor.execute('UPDATE loading_log SET loaded_qty = ? WHERE id = ?', (qty, item_id))
    else:
        cursor.execute('UPDATE loading_log SET loaded_qty = loaded_qty + ? WHERE id = ?', (qty, item_id))
    conn.commit()
    conn.close()
    return redirect('/')

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5000)
