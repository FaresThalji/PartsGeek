"""
PG Fulfillment Engine (Flask Web Version)
====================================================
"""

from flask import Flask, request, jsonify, render_template
import requests
from bs4 import BeautifulSoup
import datetime
import time
import threading
import re
import os
import webbrowser
import logging

app = Flask(__name__)

# Silence the default Flask/Werkzeug terminal output
log_werkzeug = logging.getLogger('werkzeug')
log_werkzeug.setLevel(logging.ERROR)

# =====================================================================
# CONFIG & STATE
# =====================================================================
SESSION_FILE = "pg_session_cookie.txt"
DEFAULT_COOKIE = "COMMERCE_DEVICE_UUID=4104C997-8D9A-4762-ACF4-5F1DAB827959; COMMERCE_SESSION_ID=aa1p3ov05ood73c9ou5g9acte0; cf_clearance=UqFCn9jLXcZum.AscXRCyXAVsSbgISj0t78X_Dl3W_U-1784792093-1.2.1.1-gtu_6d8jMrI9.DiUSqoTZw85kMFEGf8YI0nDtmf.Mfmcrq3bz60qdZEWQknKYAVodosRCwqps0NaxKa3LoyXjnQQNPY5AiApCCtRdQAfHTPbS.n_4HRc4621SzjdEypPfPEjmXFX8XptW_6GTic.zOBe_IL1b8DZW3.N5KbSWY3ob0Bln6XP4nEV_qBtmcT9JSRVG.cRYEU9luod6KTRhQLO3FLos45zCZg4vphNmfWLrDdfxVQARNlmwpmlIbbO5fAwt6MoQQm20nb4MnOOSerE.v.0c8ro1qruf_89aVNMOc_curxm2o8_cQf8hdN2RzdkCzrMKM_2mU6FrGiBsF7NzQZoZ4cHD2ne0UjYDc1D7Qj__Kfj6Ot2FWLQCs5sKgC2k5OUqzeYL1N1Qfu1y_ou35H5nVaFhWe4Sfg.v7.BXGxNyPdD6urdEboXeVSz_mJInMrSsy8q6_q3P77COd9BaIoQuLtEO4ttYRWwLetWJpq8tF47jhZOuthExmvvqevasNhOVG887ROYv5xUalz6r_sos7aPZC08gTHuIxEp3cgWURJmM53xHYY_kbqs2z44UoPypw.jdEekP25y7Q; ssabt=a; _pg=v%3D1%26vid%3Dd3dcd6e8-271d-4db9-87ba-515124e88385%26fh%3D1784792094729%26sc%3D1%26lv%3D1784792094729; cjConsent=MHxOfDB8Tnww; cjUser=da9f9d42-188e-4787-8ce5-f9c1fe96c1c9; _ga=GA1.1.1220533976.1784792095; PGFPID=FPID2.2.yT26w0eeOlEVGFoXobnAQPrR0LHSLgSCk8sjfSlqvSw%3D.1784792095; FPAU=1.2.370327694.1784792097; _gtmeec=e30%3D; _rdt_uuid=1784792095025.646f322f-e318-4e86-8940-ad3f5d90dfb4; _ga_RV4L35KF7B=GS2.1.s1784792095$o1$g1$t1784792266$j60$l0$h592455491; PHPSESSID=ckbvhpbbon22duiq3q4r8hcbja; COMMERCE_SESSION_UUID=45DEC76B-004E-4716-9A15-2531042B8223; helper_flashMessenger=%5B%5D"

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36')

BASE_URL = "https://admin.partsgeek.com/suppliers/orders/view"
DASHBOARD_URL = "https://admin.partsgeek.com/suppliers/orders/index?ss=&rows=1000&sort=default&status=3"

# Thread Control & Logging
automation_running = threading.Event()
app_logs = []
pending_fast_check_orders = []

def log(msg):
    app_logs.append(msg)
    print(msg) # Print to console as well

def get_logs():
    global app_logs
    logs_copy = list(app_logs)
    app_logs.clear()
    return logs_copy

# =====================================================================
# SESSION MANAGEMENT
# =====================================================================
def load_cookie():
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                saved = f.read().strip()
                if saved: return saved
        except Exception: pass
    return DEFAULT_COOKIE

def save_cookie(new_cookie):
    new_cookie = new_cookie.strip()
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        f.write(new_cookie)
    SESSION.headers['Cookie'] = new_cookie

SESSION = requests.Session()
SESSION.headers.update({
    'User-Agent': USER_AGENT,
    'Cookie': load_cookie(),
})

def post_headers(referer_url):
    return {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Origin': 'https://admin.partsgeek.com',
        'Referer': referer_url,
    }

class CloudflareBlockedError(Exception): pass

def get_today_date():
    return datetime.datetime.now().strftime("%m/%d/%Y")

def check_response(resp):
    if resp.status_code in (403, 503):
        raise CloudflareBlockedError(f"HTTP {resp.status_code} - cookie expired.")
    sample = (resp.text or "")[:2000].lower()
    if "just a moment" in sample or "cf-browser-verification" in sample or "attention required" in sample:
        raise CloudflareBlockedError("Cloudflare challenge page - cookie expired.")
    return resp

# =====================================================================
# SCRAPING LOGIC
# =====================================================================
def order_url(order_num): return f"{BASE_URL}?id={order_num.replace('PG', '')}"

def fetch_order_page(order_num):
    url = order_url(order_num)
    resp = SESSION.get(url, timeout=20)
    check_response(resp)
    soup = BeautifulSoup(resp.text, 'html.parser')
    return resp, soup, url

def extract_form_payload(soup):
    candidates = ['createShipment[carrier_code]', 'form[comments]', 'item_id[]', 'form[supplier_order_number]']
    form = None
    for name in candidates:
        el = soup.find(attrs={'name': name})
        if el:
            form = el.find_parent('form')
            if form: break
    if not form: form = soup.find('form')
    if not form: return {}

    payload = {}
    for el in form.find_all(['input', 'select', 'textarea']):
        name = el.get('name')
        if not name: continue
        tag = el.name
        if tag == 'textarea': payload[name] = el.text or ''
        elif tag == 'select':
            opt = el.find('option', selected=True) or el.find('option')
            payload[name] = opt.get('value', '') if opt else ''
        else:
            itype = (el.get('type') or 'text').lower()
            if itype in ('checkbox', 'radio', 'submit', 'button', 'file', 'image', 'reset'): continue
            payload[name] = el.get('value', '')
    return payload

def extract_tracking_from_comment(comment_text):
    if not comment_text or 'NO TN' in comment_text.upper(): return []
    match = re.search(r':\s*([A-Za-z0-9\s-]+?)\s*-\s*ORDER', comment_text, re.IGNORECASE)
    if match:
        parts = [t.strip() for t in match.group(1).split('-')]
        return [t for t in parts if t and not re.fullmatch(r'(DT|JZ)', t, re.IGNORECASE)]
    return re.findall(r'\b\d{10,22}\b', comment_text)

def submit_comment(order_num, soup, url, comment_text):
    textarea = soup.find('textarea', {'name': 'form[comments]'})
    if textarea is None: return "FAILED: comments field not found"
    
    current_text = (textarea.text or '').strip()
    target_text = comment_text.strip()
    today_date = get_today_date()
    
    # Check 1 & 2: If the exact target string is present, both template and date are correct.
    if target_text in current_text:
        return "SKIPPED (Template & Date Match)"

    # If it fails the check, update the comment box with the corrected data.
    payload = extract_form_payload(soup)
    payload.update({
        'action:update': '',
        'form[supplier_order_number]': order_num,
        'form[comments]': target_text
    })
    
    resp = SESSION.post(url, data=payload, headers=post_headers(url), allow_redirects=False, timeout=20)
    check_response(resp)
    
    # Granular logging based on what was wrong with the original text
    if resp.status_code == 302:
        if today_date not in current_text:
            return "UPDATED (Date Mismatch Fixed)"
        else:
            return "UPDATED (Template/Tracking Fixed)"
            
    return f"FAILED (HTTP {resp.status_code})"
def submit_shipment(order_num, soup, url, tracking_numbers):
    checkboxes = soup.find_all('input', {'name': 'item_id[]'})
    item_ids = [cb.get('value') for cb in checkboxes if cb.get('value')]
    if not item_ids: return "NO_ITEMS_FOUND"

    base_payload = extract_form_payload(soup)
    last_error = None

    for tn in tracking_numbers:
        if not automation_running.is_set(): return "STOPPED_BY_USER"
        payload_items = [(k, v) for k, v in base_payload.items() if k != 'item_id[]']
        payload_items += [('action:shipped', ''), ('form[supplier_order_number]', order_num), ('createShipment[carrier_code]', 'FEDEX'), ('createShipment[tracking_number]', tn)]
        payload_items += [('item_id[]', iid) for iid in item_ids]

        resp = SESSION.post(url, data=payload_items, headers=post_headers(url), allow_redirects=False, timeout=20)
        check_response(resp)
        if resp.status_code == 302: return f"SHIPPED_SUCCESS"

        err_soup = BeautifulSoup(resp.text, 'html.parser')
        err_el = err_soup.find(id='createShipmentError')
        last_error = err_el.get_text(strip=True) if err_el and err_el.get_text(strip=True) else f"HTTP {resp.status_code}"
        if len(tracking_numbers) > 1: log(f"    tracking {tn} rejected ({last_error}), trying next...")

    return f"SHIPPING_REJECTED: {last_error}" if last_error else "SHIPPING_REJECTED"

def scrape_dashboard_for_aged_orders():
    resp = SESSION.get(DASHBOARD_URL, timeout=20)
    check_response(resp)
    soup = BeautifulSoup(resp.text, 'html.parser')
    orders = []
    
    for row in soup.find_all('tr'):
        tds = row.find_all('td')
        if len(tds) < 10: continue
        order_match = re.search(r'PG\d+', tds[4].get_text().strip())
        if not order_match: continue
        
        num_match = re.search(r'\d+', tds[9].get_text().strip())
        age_val = int(num_match.group(0)) if num_match else 0
        if age_val >= 3: orders.append({'order': order_match.group(0), 'age': age_val})

    return sorted(orders, key=lambda x: x['age'], reverse=True)

# =====================================================================
# BACKGROUND PROCESSES
# =====================================================================
def process_excel(mode, raw_data, template):
    automation_running.set()
    log(f"\n--- EXCEL AUTOMATION ({mode.upper()}) ---")
    
    lines = raw_data.split('\n')
    total_valid = 0

    for line in lines:
        if not automation_running.is_set():
            log("--- Stopped by User ---")
            break

        cols = [c.strip() for c in line.split('\t')]
        if len(cols) < 3 or not cols[0].startswith('PG'): continue
        
        total_valid += 1
        order_num, tracking_raw, facility_raw = cols[0], cols[1], cols[2].upper()
        tracking_list = [t.strip() for t in re.split(r'[\s,]+', tracking_raw) if t.strip()]
        tracking_display = ' - '.join(tracking_list) if tracking_list else 'NO TN'
        facility = 'DT' if 'DT' in facility_raw else ('JZ' if 'JZ' in facility_raw else facility_raw)

        try:
            resp, soup, url = fetch_order_page(order_num)
            if "In Transit" in resp.text:
                log(f"[SKIP] {order_num}: already In Transit")
                continue

            if mode in ("both", "comment"):
                comment_text = template.format(facility=facility, tracking=tracking_display, date=get_today_date())
                status = submit_comment(order_num, soup, url, comment_text)
                log(f"[CMNT] {order_num} -> {status}")

            if mode in ("both", "ship"):
                if tracking_list:
                    log(f"  └─ [TRACKING CONFIRMED] {', '.join(tracking_list)}")
                    ship_status = submit_shipment(order_num, soup, url, tracking_list)
                    log(f"  └─ [SHIP] {order_num} -> {ship_status}")
                else:
                    log(f"  └─ [SHIP SKIP] {order_num} -> No tracking provided")
        
        except CloudflareBlockedError as e:
            log(f"[BLOCKED] {order_num}: {e}")
            break
        except Exception as e:
            log(f"[NETWORK ERROR] {order_num}: {e}")
            
        time.sleep(0.3)

    log(f"--- Complete: {total_valid} Rows Processed ---")
    automation_running.clear()

def process_fast_check_execution():
    automation_running.set()
    global pending_fast_check_orders
    log("\nUser confirmed. Processing oldest first...")
    
    for item in pending_fast_check_orders:
        if not automation_running.is_set():
            log("\n--- Stopped by User ---")
            break

        order_num = item['order']
        log(f"\n[AGE: {item['age']}] Processing {order_num}...")

        try:
            resp, soup, url = fetch_order_page(order_num)
            if "In Transit" in resp.text:
                log("  └─ [SKIP] Already In Transit")
                continue
                
            textarea = soup.find('textarea', {'name': 'form[comments]'})
            tracking_numbers = extract_tracking_from_comment(textarea.text if textarea else '')
            
            if not tracking_numbers:
                log("  └─ [SKIP] No Tracking found in comment")
                continue
                
            log(f"  └─ [TRACKING CONFIRMED] {', '.join(tracking_numbers)}")
            ship_status = submit_shipment(order_num, soup, url, tracking_numbers)
            log(f"  └─ [SHIP] -> {ship_status}")
            
        except CloudflareBlockedError as e:
            log(f"[BLOCKED] {e}")
            break
        except Exception as e:
            log(f"  └─ [ERROR] {e}")

        time.sleep(0.4)

    log("--- Fast Check Complete ---")
    pending_fast_check_orders.clear()
    automation_running.clear()

# =====================================================================
# API ROUTES
# =====================================================================
@app.route('/')
def index():
    return render_template('index.html', cookie=load_cookie())

@app.route('/api/cookie', methods=['POST'])
def update_cookie():
    data = request.json
    save_cookie(data.get('cookie', ''))
    return jsonify({"status": "success", "message": "Cookie saved."})

@app.route('/api/run_excel', methods=['POST'])
def run_excel():
    if automation_running.is_set(): return jsonify({"error": "Already running"}), 400
    data = request.json
    threading.Thread(target=process_excel, args=(data['mode'], data['raw_data'], data['template']), daemon=True).start()
    return jsonify({"status": "started"})

@app.route('/api/fast_check/scan', methods=['POST'])
def fast_check_scan():
    if automation_running.is_set(): return jsonify({"error": "Already running"}), 400
    global pending_fast_check_orders
    log("\n--- FAST CHECK (Scraping Dashboard...) ---")
    try:
        pending_fast_check_orders = scrape_dashboard_for_aged_orders()
        return jsonify({"status": "scanned", "count": len(pending_fast_check_orders)})
    except CloudflareBlockedError as e:
        log(f"[BLOCKED] {e}")
        return jsonify({"error": "Blocked by Cloudflare"}), 403

@app.route('/api/fast_check/execute', methods=['POST'])
def fast_check_execute():
    if automation_running.is_set(): return jsonify({"error": "Already running"}), 400
    threading.Thread(target=process_fast_check_execution, daemon=True).start()
    return jsonify({"status": "started"})

@app.route('/api/stop', methods=['POST'])
def stop():
    automation_running.clear()
    log("\n[SYSTEM] Stop signal sent. Halting...")
    return jsonify({"status": "stopped"})

@app.route('/api/logs', methods=['GET'])
def get_recent_logs():
    return jsonify({"logs": get_logs(), "is_running": automation_running.is_set()})

if __name__ == '__main__':
    def open_browser():
        webbrowser.open_new('http://127.0.0.1:5000/')
        
    # Prevent opening two tabs when Flask's debug auto-reloader triggers
    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        threading.Timer(1.0, open_browser).start()

            
    app.run(debug=True, port=5000)