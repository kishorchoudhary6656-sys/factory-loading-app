from flask import Flask, render_template_string, request, redirect, session
import sqlite3, csv, io
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'real_instant_foods_final_all_in_one'

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
<html lang="hi"><head><meta charset="UTF-8">
<title>REAL INSTANT FOODS | ERP</title>
<script src="https://unpkg.com/html5-qrcode"></script>
<style>
    :root { --primary: #2563eb; --bg: #020617; --card: #1e293b; --text: #f8fafc; }
    body { font-family: 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); padding: 20px; }
    .card { background: var(--card); padding: 20px; border-radius: 15px; margin-bottom: 20px; border: 1px solid #334155; }
    input, select, button { width: 100%; padding: 12px; margin: 8px 0; border-radius: 8px; border: none; background: #334155; color: white; }
    button { background: var(--primary); font-weight: bold; cursor: pointer; }
    .modal { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.8); justify-content:center; align-items:center; }
    .modal-content { background:var(--card); padding:20px; border-radius:15px; width:300px; text-align:center; }
    table { width: 100%; border-collapse: collapse; margin-top: 20px; }
    th, td { padding: 12px; border-bottom: 1px solid #475569; text-align: left; }
</style></head>
<body>
    <h1 style="text-align:center; color:var(--primary);">REAL INSTANT FOODS | DISPATCH ERP</h1>
    <div class="card">
        <h3>⚡ स्मार्ट AI स्कैनर</h3>
        <div id="reader" style="width:100%; display:none;"></div>
        <button onclick="startScanner()">Start AI Scan</button>
    </div>
    <div class="card">
        <h3>📦 PO लोड करें & डिस्पैच एंट्री</h3>
        <form action="/load_po" method="POST">
            <select name="po_number">{% for po in po_list %}<option value="{{ po[0] }}">{{ po[0] }}</option>{% endfor %}</select>
            <input type="text" name="vehicle_no" placeholder="Vehicle Number" required>
            <input type="text" name="location" placeholder="Location" required>
            <button type="submit">Load PO</button>
        </form>
    </div>
    <div class="card">
        <h3>📋 डिस्पैच हिस्ट्री</h3>
        <table>
            <tr><th>PO</th><th>Vehicle</th><th>Product</th><th>Qty</th></tr>
            {% for log in logs %}<tr><td>{{ log[1] }}</td><td>{{ log[2] }}</td><td>{{ log[4] }}</td><td>{{ log[5] }} pcs</td></tr>{% endfor %}
        </table>
    </div>

    <div id="scanModal" class="modal"><div class="modal-content">
        <h3 id="pName">...</h3>
        <input type="number" id="qtyIn" value="1">
        <button onclick="submitQty()">Confirm</button>
    </div></div>

    <script>
        let bCode = "";
        function startScanner() {
            document.getElementById('reader').style.display='block';
            const s = new Html5Qrcode("reader");
            s.start({facingMode: "environment"}, {fps: 20, qrbox: 200}, (data) => {
                s.stop(); bCode = data;
                fetch('/get_info?b='+data).then(res=>res.json()).then(info => {
                    document.getElementById('pName').innerText = info.name;
                    document.getElementById('scanModal').style.display='flex';
                });
            });
        }
        function submitQty() {
            fetch('/process', {method:'POST', body:'b='+bCode+'&q='+document.getElementById('qtyIn').value, headers:{'Content-Type':'application/x-www-form-urlencoded'}}).then(() => location.reload());
        }
    </script>
</body></html>
"""

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

@app.route('/get_info')
def get_info():
    b = request.args.get('b')
    conn = sqlite3.connect('factory.db')
    c = conn.cursor()
    c.execute('SELECT item_name FROM po_items WHERE barcode = ?', (b,))
    res = c.fetchone()
    return {"name": res[0] if res else "Unknown"}

@app.route('/process', methods=['POST'])
def process():
    b = request.form['b']
    q = int(request.form['q'])
    conn = sqlite3.connect('factory.db')
    c = conn.cursor()
    c.execute('SELECT item_name, weight FROM po_items WHERE barcode = ?', (b,))
    i = c.fetchone()
    if i:
        mult = 25 if "1kg" in i[1] else 50 if "500g" in i[1] else 1
        c.execute('INSERT INTO dispatch_log (po_number, product_name, loaded_qty, timestamp) VALUES (?, ?, ?, ?)', ("Scan", f"{i[0]} ({i[1]})", q*mult, datetime.now().strftime("%H:%M")))
    conn.commit()
    conn.close()
    return '', 200

@app.route('/load_po', methods=['POST'])
def load_po():
    po, v, l = request.form['po_number'], request.form['vehicle_no'], request.form['location']
    conn = sqlite3.connect('factory.db')
    c = conn.cursor()
    c.execute('SELECT item_name, weight FROM po_items WHERE po_number = ?', (po,))
    for i in c.fetchall():
        c.execute('INSERT INTO dispatch_log (po_number, vehicle_no, location, product_name, loaded_qty, timestamp) VALUES (?, ?, ?, ?, 0, ?)', (po, v, l, f"{i[0]} ({i[1]})", datetime.now().strftime("%H:%M")))
    conn.commit()
    conn.close()
    return redirect('/')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST' and request.form.get('password') == 'real@8283':
        session['logged_in'] = True
        return redirect('/')
    return "<body style='background:#0f172a; padding:50px; text-align:center;'><form method='POST'><input type='password' name='password'><button>Login</button></form></body>"

if __name__ == '__main__': app.run(host='0.0.0.0', port=5000)
