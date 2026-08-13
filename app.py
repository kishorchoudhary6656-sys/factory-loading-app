from flask import Flask, render_template_string, request, redirect, session
import sqlite3
import csv
import io
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'real_instant_foods_pro_2026'

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
            barcode TEXT DEFAULT '',
            vehicle_no TEXT DEFAULT '',
            location TEXT DEFAULT '',
            weight TEXT DEFAULT '500g'
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS dispatch_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            po_number TEXT,
            vehicle_no TEXT,
            location TEXT,
            dispatch_date TEXT
        )
    ''')
    
    cursor.execute('SELECT COUNT(*) FROM po_items')
    if cursor.fetchone()[0] == 0:
        sample_data = [
            ("P5229701", "RAW PEANUT / SINGDANA", "500g", 1620, "8909177015591"),
            ("P5229701", "Daily Good Idli Rice", "1kg", 540, "8909177015874"),
            ("P5229701", "Bulk Toor Dal", "10kg", 100, "8909177019999")
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
    <title>REAL INSTANT FOODS - Secure Portal</title>
    <style>
        body { font-family: sans-serif; background: #090d16; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; color: #f1f5f9; }
        .login-card { background: #111827; padding: 40px; border-radius: 20px; width: 100%; max-width: 380px; box-shadow: 0 20px 40px rgba(0,0,0,0.6); text-align: center; border: 1px solid #1f2937; }
        input { width: 100%; padding: 14px; margin: 15px 0; border: 1px solid #374151; border-radius: 10px; box-sizing: border-box; font-size: 1rem; background: #1f2937; color: white; }
        button { width: 100%; padding: 14px; background: #2563eb; color: white; border: none; border-radius: 10px; font-size: 1rem; font-weight: bold; cursor: pointer; }
        .error { color: #f87171; font-size: 0.85rem; margin-top: 5px; }
    </style>
</head>
<body>
    <div class="login-card">
        <h2 style="color: #60a5fa; margin-top:0;">REAL INSTANT FOODS</h2>
        <p style="color: #9ca3af; font-size: 0.9rem;">World-Class Logistics ERP</p>
        <form action="/login" method="POST">
            <input type="password" name="password" placeholder="सिक्योर पासवर्ड दर्ज करें" required>
            <button type="submit">सिस्टम में प्रवेश करें</button>
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
    <title>REAL INSTANT FOODS | Pro ERP</title>
    <script src="https://unpkg.com/html5-qrcode" type="text/javascript"></script>
    <style>
        :root { --primary: #3b82f6; --dark: #090d16; --card: #111827; --border: #1f2937; --text: #f3f4f6; }
        body { font-family: sans-serif; background: var(--dark); margin: 0; color: var(--text); }
        .navbar { background: var(--card); padding: 1.2rem 2rem; color: white; display: flex; align-items: center; justify-content: space-between; border-bottom: 2px solid var(--primary); }
        .navbar h1 { margin: 0; font-size: 1.5rem; font-weight: 800; color: #60a5fa; }
        .logout-btn { background: #dc2626; color: white; padding: 0.5rem 1.2rem; border-radius: 8px; text-decoration: none; font-size: 0.85rem; font-weight: bold; }
        .container { max-width: 1250px; margin: 2rem auto; padding: 0 1rem; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 1.5rem; margin-bottom: 2.5rem; }
        .card { background: var(--card); border-radius: 16px; padding: 1.5rem; border: 1px solid var(--border); }
        .card-title { font-size: 1.15rem; font-weight: 700; color: #93c5fd; margin-top: 0; margin-bottom: 1.2rem; }
        input, select { width: 100%; padding: 0.85rem; margin-bottom: 1rem; border: 1px solid #374151; border-radius: 10px; font-size: 0.95rem; box-sizing: border-box; background: #1f2937; color: white; }
        .btn { width: 100%; padding: 0.9rem; border: none; border-radius: 10px; font-weight: 600; font-size: 1rem; color: white; cursor: pointer; }
        .btn-upload { background: #f59e0b; }
        .btn-load { background: #10b981; }
        .btn-scan { background: #8b5cf6; }
        .btn-manual { background: #0284c7; }
        #reader { width: 100%; border-radius: 12px; display: none; margin-bottom: 1rem; border: 2px dashed #8b5cf6; background: #000; }
        .table-container { background: var(--card); border-radius: 16px; border: 1px solid var(--border); overflow: hidden; margin-bottom: 2rem; }
        .table-header { padding: 1.5rem; background: #1f2937; border-bottom: 1px solid var(--border); }
        table { width: 100%; border-collapse: collapse; text-align: left; }
        th { background: #1f2937; padding: 1rem 1.5rem; font-weight: 700; color: #9ca3af; font-size: 0.85rem; text-transform: uppercase; }
        td { padding: 1.2rem 1.5rem; border-bottom: 1px solid var(--border); vertical-align: middle; color: #e5e7eb; }
        tr:hover { background: #161e2e; }
        .text-green { color: #4ade80; font-weight: 800; }
        .text-red { color: #f87171; font-weight: 800; }
        .loose-badge { color: #fbbf24; font-size: 0.85rem; background: #451a03; padding: 0.2rem 0.6rem; border-radius: 6px; display: inline-block; margin-top: 4px; }
        .barcode-badge { color: #93c5fd; font-size: 0.85rem; background: #1f2937; padding: 0.2rem 0.5rem; border-radius: 4px; font-family: monospace; border: 1px solid #374151; }
        .update-form { display: flex; gap: 0.4rem; align-items: center; justify-content: center; }
        .update-input { width: 65px; margin: 0; padding: 0.5rem; text-align: center; font-weight: bold; background: #1f2937; color: white; border: 1px solid #374151; }
        .btn-update { padding: 0.5rem 0.8rem; background: #374151; color: white; font-size: 0.8rem; border-radius: 6px; border:none; cursor:pointer; }
        .btn-edit-qty { background: #b45309; }
    </style>
</head>
<body>
    <header class="navbar">
        <h1>REAL INSTANT FOODS</h1>
        <a href="/logout" class="logout-btn">लॉगआउट</a>
    </header>
    <div class="container">
        <div class="grid">
            <div class="card" style="border: 2px solid #8b5cf6;">
                <h2 class="card-title">⚡ AI 1-Sec बारकोड स्कैनर</h2>
                <div id="reader"></div>
                <button type="button" onclick="startScanner()" class="btn btn-scan" id="scanBtn">कैमरा चालू करें (Instant Scan)</button>
                <form action="/manual_scan" method="POST" style="margin-top: 15px;">
                    <input type="text" name="barcode" placeholder="या बारकोड यहाँ टाइप करें" required style="font-size: 0.9rem;">
                    <div style="display: flex; gap: 10px;">
                        <input type="number" name="box_count" value="1" placeholder="मास्टर बैग संख्या" required style="margin-bottom:0;">
                        <input type="number" name="loose_count" value="0" placeholder="लूज पीस" required style="margin-bottom:0;">
                        <button type="submit" class="btn btn-manual" style="width: auto; padding: 0.8rem 1.2rem;">जोड़ें</button>
                    </div>
                </form>
            </div>
            <div class="card">
                <h2 class="card-title">📦 पेंडिंग PO लोड & लॉजिस्टिक्स</h2>
                <form action="/load_po" method="POST">
                    <select name="po_number" required>
                        <option value="">-- उपलब्ध PO चुनें --</option>
                        {% for po in po_list %}
                        <option value="{{ po[0] }}">PO: {{ po[0] }}</option>
                        {% endfor %}
                    </select>
                    <input type="text" name="vehicle_no" placeholder="गाड़ी नंबर (Vehicle No)" required>
                    <input type="text" name="location" placeholder="डिलीवरी लोकेशन (Location)" required>
                    <button type="submit" class="btn btn-load">पूरा PO लोडिंग में जोड़ें</button>
                </form>
            </div>
            <div class="card">
                <h2 class="card-title">📁 CSV फाइल अपलोड & सर्च</h2>
                <form action="/upload_file" method="POST" enctype="multipart/form-data">
                    <input type="file" name="po_file" accept=".csv" required>
                    <button type="submit" class="btn btn-upload">फाइल अपलोड करें</button>
                </form>
                <h3 class="card-title" style="margin-top:20px; font-size:1rem;">🔍 PO सर्च रिकॉर्ड</h3>
                <form action="/search" method="GET" style="display:flex; gap:5px;">
                    <input type="text" name="po_number" placeholder="PO नंबर लिखें..." required style="margin-bottom:0;">
                    <button type="submit" style="width:auto; padding:0 15px; background:#2563eb; border-radius:8px; border:none; color:white; font-weight:bold; cursor:pointer;">सर्च</button>
                </form>
            </div>
        </div>
        <div class="table-container">
            <div class="table-header">
                <h3>🚛 लाइव डिस्पैच, वाहन और ऑटो-मास्टर बैग कैलकुलेशन</h3>
            </div>
            <div style="overflow-x:auto;">
                <table>
                    <thead>
                        <tr>
                            <th>PO नंबर</th>
                            <th>उत्पाद विवरण & वजन</th>
                            <th>गाड़ी नंबर & लोकेशन</th>
                            <th>मास्टर बैग नियम कैलकुलेशन</th>
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
                            <td>{{ item[2] }}<br><span style="color:#38bdf8; font-size:0.85rem;">वजन: {{ item[9] }}</span><br><span class="barcode-badge">{{ item[6] }}</span></td>
                            <td><span style="color:#38bdf8; font-weight:bold;">🚚 {{ item[7] or 'N/A' }}</span><br><span style="color:#9ca3af; font-size:0.85rem;">📍 {{ item[8] or 'N/A' }}</span></td>
                            <td>
                                {% set w = item[9] | string | lower %}
                                {% if '10kg' in w or '26kg' in w or '10 kg' in w or '26 kg' in w %}
                                    <span style="color:#f59e0b; font-weight:bold;">📦 केवल लूज (Loose)</span>
                                {% else %}
                                    {% set box = item[4] if item[4] > 0 else 50 %}
                                    {% set bags = item[5] // box %}
                                    {% set loose = item[5] % box %}
                                    <b>{{ box }} pcs/bag</b><br>{{ bags }} मास्टर बैग्स<br>
                                    {% if loose > 0 %} <span class="loose-badge">+ {{ loose }} pcs loose</span> {% endif %}
                                {% endif %}
                            </td>
                            <td style="font-weight: bold; color: #38bdf8;">{{ item[3] }} pcs</td>
                            <td class="text-green">{{ item[5] }} pcs</td>
                            <td class="text-red">{{ item[3] - item[5] }} pcs</td>
                            <td>
                                <form action="/update_load" method="POST" class="update-form" style="margin-bottom: 4px;">
                                    <input type="hidden" name="id" value="{{ item[0] }}">
                                    <input type="hidden" name="action_type" value="add">
                                    <input type="number" name="qty" value="1" class="update-input" required>
                                    <button type="submit" class="btn-update">+Add</button>
                                </form>
                                <form action="/update_load" method="POST" class="update-form">
                                    <input type="hidden" name="id" value="{{ item[0] }}">
                                    <input type="hidden" name="action_type" value="edit">
                                    <input type="number" name="qty" value="{{ item[5] }}" class="update-input" style="background:#451a03;" required>
                                    <button type="submit" class="btn-update btn-edit-qty">Set/Edit</button>
                                </form>
                            </td>
                        </tr>
                        {% else %}
                        <tr><td colspan="8" style="text-align:center; padding:3rem; color:#9ca3af;">📋 लोडिंग के लिए कोई PO सिलेक्ट नहीं किया गया है।</td></tr>
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
                { fps: 25, qrbox: { width: 300, height: 120 } },
                (decodedText, decodedResult) => {
                    html5QrCode.stop();
                    let boxCount = prompt("⚡ AI स्कैन सफल: " + decodedText + "\\n\\nकितने मास्टर बैग्स (Boxes) लोड किए?", "1");
                    if (boxCount !== null && boxCount.trim() !== "") {
                        let looseCount = prompt("और कितने लूज पीस (Loose Pcs) लोड किए?", "0");
                        if (looseCount !== null) {
                            fetch('/ai_scan_handler', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                                body: `barcode=${encodeURIComponent(decodedText)}&box_count=${boxCount}&loose_count=${looseCount}`
                            }).then(response => { 
                                if(response.ok) { window.location.reload(); } 
                                else { alert("❌ यह बारकोड सिस्टम के किसी एक्टिव PO में नहीं मिला!"); window.location.reload(); }
                            });
                        } else { window.location.reload(); }
                    } else { window.location.reload(); }
                },
                (errorMessage) => {}
            ).catch(err => { 
                alert("कैमरा परमिशन एरर: कृपया ब्राउज़र सेटिंग से कैमरा Allow करें।");
                document.getElementById('reader').style.display = 'none';
                document.getElementById('scanBtn').style.display = 'block';
            });
        }
    </script>
</body>
</html>
"""

@app.route('/')
def home():
    if not session.get('logged_in'): return redirect('/login')
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT DISTINCT po_number FROM po_items')
    po_list = cursor.fetchall()
    cursor.execute('SELECT * FROM loading_log ORDER BY id DESC')
    loads = cursor.fetchall()
    conn.close()
    return render_template_string(DASHBOARD_TEMPLATE, po_list=po_list, loads=loads)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == 'real@8283':
            session['logged_in'] = True
            return redirect('/')
        else:
            return render_template_string(LOGIN_TEMPLATE, error="गलत पासवर्ड!")
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect('/login')

@app.route('/googlec1dd36a62fa9245c.html')
def google_verify():
    return "google-site-verification: googlec1dd36a62fa9245c.html"

def get_box_size_by_weight(weight_str):
    w = weight_str.lower().strip()
    if '1kg' in w or '1 kg' in w: return 25
    elif '500g' in w or '500 g' in w: return 50
    elif '200g' in w or '200 g' in w: return 100
    elif '2kg' in w or '2 kg' in w: return 12
    elif '5kg' in w or '5 kg' in w: return 6
    elif '10kg' in w or '26kg' in w or '10 kg' in w or '26 kg' in w: return 1
    return 50

@app.route('/ai_scan_handler', methods=['POST'])
def ai_scan_handler():
    if not session.get('logged_in'): return '', 403
    barcode = request.form.get('barcode').strip()
    try:
        box_count = int(request.form.get('box_count', 0))
        loose_count = int(request.form.get('loose_count', 0))
    except ValueError:
        box_count, loose_count = 1, 0
    
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT item_name, weight, po_number, ordered_qty FROM po_items WHERE barcode = ?', (barcode,))
    item = cursor.fetchone()
    
    if item:
        item_name, weight, po_no, target_qty = item[0], item[1], item[2], item[3]
        name_weight = f"{item_name} ({weight})"
        box_size = get_box_size_by_weight(weight)
        
        if box_size == 1:
            total_qty = box_count + loose_count
        else:
            total_qty = (box_count * box_size) + loose_count
        
        cursor.execute('SELECT id FROM loading_log WHERE po_number = ? AND product_name = ?', (po_no, name_weight))
        if not cursor.fetchone():
            cursor.execute('INSERT INTO loading_log (po_number, product_name, total_ordered, box_size, loaded_qty, barcode, vehicle_no, location, weight) VALUES (?, ?, ?, ?, 0, ?, "AI Scan", "Factory Hub", ?)',
                           (po_no, name_weight, target_qty, box_size, barcode, weight))
        
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
    try:
        box_count = int(request.form.get('box_count', 1))
        loose_count = int(request.form.get('loose_count', 0))
    except ValueError:
        box_count, loose_count = 1, 0
    
    conn = sqlite3.connect('factory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT item_name, weight, po_number, ordered_qty FROM po_items WHERE barcode = ?', (barcode,))
    item = cursor.fetchone()
    
    if item:
        item_name, weight, po_no, target_qty = item[0], item[1], item[2], item[3]
        n
