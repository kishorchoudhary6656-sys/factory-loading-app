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
            location TEXT DEFAULT ''
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
    
    conn.commit()
    conn.close()

init_db()

LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>REAL INSTANT FOODS - Login</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #0f172a; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; color: #f1f5f9; }
        .login-card { background: #1e293b; padding: 35px; border-radius: 16px; width: 100%; max-width: 350px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); text-align: center; border: 1px solid #334155; }
        input { width: 100%; padding: 12px; margin: 15px 0; border: 1px solid #475569; border-radius: 8px; box-sizing: border-box; font-size: 1rem; background: #0f172a; color: white; }
        button { width: 100%; padding: 12px; background: #2563eb; color: white; border: none; border-radius: 8px; font-size: 1rem; font-weight: bold; cursor: pointer; }
        button:hover { background: #1d4ed8; }
        .error { color: #dc2626; font-size: 0.85rem; margin-top: 5px; }
        
