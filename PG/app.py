"""
PG Fulfillment Engine (Python / API-call version)
====================================================
"""

import tkinter as tk
from tkinter import scrolledtext, messagebox
import requests
from bs4 import BeautifulSoup
import datetime
import time
import threading
import re
import os

# =====================================================================
# CONFIG
# =====================================================================
SESSION_FILE = "pg_session_cookie.txt"

DEFAULT_COOKIE = (
    'COMMERCE_DEVICE_UUID=4104C997-8D9A-4762-ACF4-5F1DAB827959; '
    'COMMERCE_SESSION_ID=aa1p3ov05ood73c9ou5g9acte0; '
    'cf_clearance=UqFCn9jLXcZum.AscXRCyXAVsSbgISj0t78X_Dl3W_U-1784792093-1.2.1.1-gtu_6d8jMrI9.DiUSqoTZw85kMFEGf8YI0nDtmf.Mfmcrq3bz60qdZEWQknKYAVodosRCwqps0NaxKa3LoyXjnQQNPY5AiApCCtRdQAfHTPbS.n_4HRc4621SzjdEypPfPEjmXFX8XptW_6GTic.zOBe_IL1b8DZW3.N5KbSWY3ob0Bln6XP4nEV_qBtmcT9JSRVG.cRYEU9luod6KTRhQLO3FLos45zCZg4vphNmfWLrDdfxVQARNlmwpmlIbbO5fAwt6MoQQm20nb4MnOOSerE.v.0c8ro1qruf_89aVNMOc_curxm2o8_cQf8hdN2RzdkCzrMKM_2mU6FrGiBsF7NzQZoZ4cHD2ne0UjYDc1D7Qj__Kfj6Ot2FWLQCs5sKgC2k5OUqzeYL1N1Qfu1y_ou35H5nVaFhWe4Sfg.v7.BXGxNyPdD6urdEboXeVSz_mJInMrSsy8q6_q3P77COd9BaIoQuLtEO4ttYRWwLetWJpq8tF47jhZOuthExmvvqevasNhOVG887ROYv5xUalz6r_sos7aPZC08gTHuIxEp3cgWURJmM53xHYY_kbqs2z44UoPypw.jdEekP25y7Q; '
    'ssabt=a; '
    '_pg=v%3D1%26vid%3Dd3dcd6e8-271d-4db9-87ba-515124e88385%26fh%3D1784792094729%26sc%3D1%26lv%3D1784792094729; '
    'cjConsent=MHxOfDB8Tnww; '
    'cjUser=da9f9d42-188e-4787-8ce5-f9c1fe96c1c9; '
    '_ga=GA1.1.1220533976.1784792095; '
    'PGFPID=FPID2.2.yT26w0eeOlEVGFoXobnAQPrR0LHSLgSCk8sjfSlqvSw%3D.1784792095; '
    'FPAU=1.2.370327694.1784792097; '
    '_gtmeec=e30%3D; '
    '_rdt_uuid=1784792095025.646f322f-e318-4e86-8940-ad3f5d90dfb4; '
    '_ga_RV4L35KF7B=GS2.1.s1784792095$o1$g1$t1784792266$j60$l0$h592455491; '
    'PHPSESSID=ckbvhpbbon22duiq3q4r8hcbja; '
    'COMMERCE_SESSION_UUID=45DEC76B-004E-4716-9A15-2531042B8223; '
    'helper_flashMessenger=%5B%5D'
)

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36')

BASE_URL = "https://admin.partsgeek.com/suppliers/orders/view"
DASHBOARD_URL = "https://admin.partsgeek.com/suppliers/orders/index?ss=&rows=1000&sort=default&status=3"

# Global Thread Control Event
automation_running = threading.Event()

def load_cookie():
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                saved = f.read().strip()
                if saved:
                    return saved
        except Exception:
            pass
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

# =====================================================================
# Errors / small helpers
# =====================================================================

class CloudflareBlockedError(Exception):
    pass

def get_today_date():
    return datetime.datetime.now().strftime("%m/%d/%Y")

def check_response(resp):
    if resp.status_code in (403, 503):
        raise CloudflareBlockedError(f"HTTP {resp.status_code} - cookie/session likely expired.")
    sample = (resp.text or "")[:2000].lower()
    if "just a moment" in sample or "cf-browser-verification" in sample or "attention required" in sample:
        raise CloudflareBlockedError("Cloudflare challenge page returned - cookie expired, please refresh it.")
    return resp

def order_url(order_num):
    order_id = order_num.replace('PG', '')
    return f"{BASE_URL}?id={order_id}"

def fetch_order_page(order_num):
    url = order_url(order_num)
    resp = SESSION.get(url, timeout=20)
    check_response(resp)
    soup = BeautifulSoup(resp.text, 'html.parser')
    return resp, soup, url

def extract_form_payload(soup):
    candidates = [
        'createShipment[carrier_code]',
        'form[comments]',
        'item_id[]',
        'form[supplier_order_number]',
    ]
    form = None
    for name in candidates:
        el = soup.find(attrs={'name': name})
        if el:
            form = el.find_parent('form')
            if form:
                break
    if not form:
        form = soup.find('form')
    if not form:
        return {}

    payload = {}
    for el in form.find_all(['input', 'select', 'textarea']):
        name = el.get('name')
        if not name:
            continue
        tag = el.name
        if tag == 'textarea':
            payload[name] = el.text or ''
        elif tag == 'select':
            opt = el.find('option', selected=True) or el.find('option')
            payload[name] = opt.get('value', '') if opt else ''
        else:
            itype = (el.get('type') or 'text').lower()
            if itype in ('checkbox', 'radio', 'submit', 'button', 'file', 'image', 'reset'):
                continue
            payload[name] = el.get('value', '')
    return payload

def extract_tracking_from_comment(comment_text):
    if not comment_text or 'NO TN' in comment_text.upper():
        return []

    match = re.search(r':\s*([A-Za-z0-9\s-]+?)\s*-\s*ORDER', comment_text, re.IGNORECASE)
    if match:
        parts = [t.strip() for t in match.group(1).split('-')]
        return [t for t in parts if t and not re.fullmatch(r'(DT|JZ)', t, re.IGNORECASE)]

    digits = re.findall(r'\b\d{10,22}\b', comment_text)
    return digits

# =====================================================================
# Core actions
# =====================================================================

def submit_comment(order_num, soup, url, comment_text):
    textarea = soup.find('textarea', {'name': 'form[comments]'})
    if textarea is None:
        return "FAILED: comments field not found on page"

    if get_today_date() in (textarea.text or ''):
        return "SKIPPED (already has today's date)"

    payload = extract_form_payload(soup)
    payload['action:update'] = ''
    payload['form[supplier_order_number]'] = order_num
    payload['form[comments]'] = comment_text

    resp = SESSION.post(url, data=payload, headers=post_headers(url),
                         allow_redirects=False, timeout=20)
    check_response(resp)

    if resp.status_code == 302:
        return "SAVED"
    return f"FAILED (HTTP {resp.status_code}, no redirect)"

def submit_shipment(order_num, soup, url, tracking_numbers, log=None):
    checkboxes = soup.find_all('input', {'name': 'item_id[]'})
    item_ids = [cb.get('value') for cb in checkboxes if cb.get('value')]
    if not item_ids:
        return "NO_ITEMS_FOUND"

    base_payload = extract_form_payload(soup)
    last_error = None

    for tn in tracking_numbers:
        if not automation_running.is_set():
            return "STOPPED_BY_USER"

        payload_items = [(k, v) for k, v in base_payload.items() if k != 'item_id[]']
        payload_items += [
            ('action:shipped', ''),
            ('form[supplier_order_number]', order_num),
            ('createShipment[carrier_code]', 'FEDEX'),
            ('createShipment[tracking_number]', tn),
        ]
        payload_items += [('item_id[]', iid) for iid in item_ids]

        resp = SESSION.post(url, data=payload_items, headers=post_headers(url),
                             allow_redirects=False, timeout=20)
        check_response(resp)

        if resp.status_code == 302:
            return f"SHIPPED_SUCCESS"

        err_soup = BeautifulSoup(resp.text, 'html.parser')
        err_el = err_soup.find(id='createShipmentError')
        last_error = err_el.get_text(strip=True) if err_el and err_el.get_text(strip=True) else f"HTTP {resp.status_code}"

        if log and len(tracking_numbers) > 1:
            log(f"    tracking {tn} rejected ({last_error}), trying next...")

    return f"SHIPPING_REJECTED: {last_error}" if last_error else "SHIPPING_REJECTED"

def scrape_dashboard_for_aged_orders():
    resp = SESSION.get(DASHBOARD_URL, timeout=20)
    check_response(resp)

    soup = BeautifulSoup(resp.text, 'html.parser')
    orders = []
    rows = soup.find_all('tr')

    for row in rows:
        tds = row.find_all('td')
        # We need at least 10 columns to reach the actual Age column
        if len(tds) < 10:
            continue

        # Column 5 (index 4) is 'Your Order #'
        order_text = tds[4].get_text().strip()
        pg_match = re.search(r'PG\d+', order_text)
        if not pg_match:
            continue

        order_num = pg_match.group(0)
        age_val = 0

        # Column 10 (index 9) is 'Age'
        age_text = tds[9].get_text().strip()
        num_match = re.search(r'\d+', age_text)
        
        if num_match:
            age_val = int(num_match.group(0))

        if age_val >= 3:
            orders.append({'order': order_num, 'age': age_val})

    return sorted(orders, key=lambda x: x['age'], reverse=True)

# =====================================================================
# Execution threads (GUI callbacks)
# =====================================================================

def set_running_state(is_running):
    if is_running:
        automation_running.set()
        btn_run_all.config(state=tk.DISABLED)
        btn_run_comment.config(state=tk.DISABLED)
        btn_run_ship.config(state=tk.DISABLED)
        btn_fast_check.config(state=tk.DISABLED)
        btn_stop.config(state=tk.NORMAL)
    else:
        automation_running.clear()
        btn_run_all.config(state=tk.NORMAL)
        btn_run_comment.config(state=tk.NORMAL)
        btn_run_ship.config(state=tk.NORMAL)
        btn_fast_check.config(state=tk.NORMAL)
        btn_stop.config(state=tk.DISABLED)

def stop_automation():
    automation_running.clear()
    text_log.insert(tk.END, "\n[SYSTEM] Stop signal sent. Halting...\n")
    text_log.see(tk.END)

def execute_excel_automation(mode):
    raw_data = text_input.get("1.0", tk.END).strip()
    template = entry_comment.get()

    if not raw_data:
        messagebox.showwarning("Input Error", "Please paste the Excel data.")
        return

    set_running_state(True)
    text_log.insert(tk.END, f"\n--- EXCEL AUTOMATION ({mode.upper()}) ---\n")
    text_log.see(tk.END)

    def log(msg):
        text_log.insert(tk.END, msg + "\n")
        text_log.see(tk.END)

    def process():
        lines = raw_data.split('\n')
        total_valid = 0

        for line in lines:
            if not automation_running.is_set():
                log("--- Stopped by User ---")
                break

            cols = [c.strip() for c in line.split('\t')]
            if len(cols) < 3 or not cols[0].startswith('PG'):
                continue
            
            total_valid += 1
            order_num = cols[0]
            tracking_raw = cols[1]
            facility_raw = cols[2].upper()

            tracking_list = [t.strip() for t in re.split(r'[\s,]+', tracking_raw) if t.strip()]
            tracking_display = ' - '.join(tracking_list) if tracking_list else 'NO TN'
            facility = 'DT' if 'DT' in facility_raw else ('JZ' if 'JZ' in facility_raw else facility_raw)

            try:
                resp, soup, url = fetch_order_page(order_num)
            except CloudflareBlockedError as e:
                log(f"[BLOCKED] {order_num}: {e}")
                log("Stopping batch - refresh your session cookie below and try again.")
                break
            except Exception as e:
                log(f"[NETWORK ERROR] {order_num}: {e}")
                continue

            if "In Transit" in resp.text:
                log(f"[SKIP] {order_num}: already In Transit")
                time.sleep(0.2)
                continue

            if mode in ("both", "comment"):
                comment_text = template.format(facility=facility, tracking=tracking_display, date=get_today_date())
                try:
                    status = submit_comment(order_num, soup, url, comment_text)
                except CloudflareBlockedError as e:
                    log(f"[BLOCKED] {order_num}: {e}")
                    break
                log(f"[CMNT] {order_num} -> {status}")
                time.sleep(0.2)

            if mode in ("both", "ship"):
                if tracking_list:
                    log(f"  └─ [TRACKING CONFIRMED] {', '.join(tracking_list)}")
                    try:
                        ship_status = submit_shipment(order_num, soup, url, tracking_list, log=log)
                    except CloudflareBlockedError as e:
                        log(f"[BLOCKED] {order_num}: {e}")
                        break
                    log(f"  └─ [SHIP] {order_num} -> {ship_status}")
                else:
                    log(f"  └─ [SHIP SKIP] {order_num} -> No tracking provided")
                time.sleep(0.3)

        log(f"--- Complete: {total_valid} Rows Processed ---")
        set_running_state(False)

    threading.Thread(target=process, daemon=True).start()

def execute_fast_check():
    set_running_state(True)
    text_log.insert(tk.END, "\n--- FAST CHECK (Scraping Dashboard...) ---\n")
    text_log.see(tk.END)

    def log(msg):
        text_log.insert(tk.END, msg + "\n")
        text_log.see(tk.END)

    def scrape_thread():
        try:
            orders = scrape_dashboard_for_aged_orders()
        except CloudflareBlockedError as e:
            root.after(0, lambda: log(f"[BLOCKED] {e}\nRefresh cookie and try again."))
            root.after(0, lambda: set_running_state(False))
            return

        if not orders:
            root.after(0, lambda: log("\nNo orders >= 3 days found.\n--- Fast Check Complete ---"))
            root.after(0, lambda: set_running_state(False))
            return

        root.after(0, prompt_confirmation, orders)

    def prompt_confirmation(orders):
        if not automation_running.is_set():
            return
            
        log(f"\nFound {len(orders)} valid aged orders on dashboard.")
        
        # Pause UI, ask user
        confirm = messagebox.askyesno("Confirm Fast Check", f"Found {len(orders)} aged orders (>= 3 days).\n\nDo you want to extract tracking and process them now?")
        
        if confirm:
            log("\nUser confirmed. Processing oldest first...")
            threading.Thread(target=process_orders, args=(orders,), daemon=True).start()
        else:
            log("\nFast Check canceled by user.")
            log("--- Fast Check Complete ---")
            set_running_state(False)

    def process_orders(orders):
        for item in orders:
            if not automation_running.is_set():
                log("\n--- Stopped by User ---")
                break

            order_num = item['order']
            age = item['age']
            log(f"\n[AGE: {age}] Processing {order_num}...")

            try:
                resp, soup, url = fetch_order_page(order_num)
                
                if "In Transit" in resp.text:
                    log("  └─ [SKIP] Already In Transit")
                    continue
                    
                textarea = soup.find('textarea', {'name': 'form[comments]'})
                comment_text = textarea.text if textarea else ''
                
                # Check tracking immediately
                tracking_numbers = extract_tracking_from_comment(comment_text)
                
                if not tracking_numbers:
                    log("  └─ [SKIP] No Tracking found in comment")
                    continue
                    
                # Tracking confirmed, print to log
                log(f"  └─ [TRACKING CONFIRMED] {', '.join(tracking_numbers)}")
                
                # Execute Shipment
                ship_status = submit_shipment(order_num, soup, url, tracking_numbers, log=log)
                log(f"  └─ [SHIP] -> {ship_status}")
                
            except CloudflareBlockedError as e:
                log(f"[BLOCKED] {e}")
                log("Stopping - refresh your session cookie below and try again.")
                break
            except Exception as e:
                log(f"  └─ [ERROR] {e}")

            time.sleep(0.4)

        log("--- Fast Check Complete ---")
        set_running_state(False)

    threading.Thread(target=scrape_thread, daemon=True).start()

def handle_cookie_update():
    new_cookie = entry_cookie.get().strip()
    if not new_cookie:
        messagebox.showwarning("Empty", "Paste a cookie value first.")
        return
    save_cookie(new_cookie)
    messagebox.showinfo("Saved", "Session cookie updated and saved to pg_session_cookie.txt.")


# =====================================================================
# GUI construction
# =====================================================================
root = tk.Tk()
root.title("PG Fulfillment Engine")
root.geometry("660x720")
root.configure(padx=15, pady=15)

# 0. Session cookie
cookie_frame = tk.Frame(root)
cookie_frame.pack(fill="x", pady=(0, 10))
tk.Label(cookie_frame, text="Session Cookie:", font=("Arial", 9, "bold")).pack(anchor="w")
cookie_row = tk.Frame(cookie_frame)
cookie_row.pack(fill="x", pady=(2, 0))
entry_cookie = tk.Entry(cookie_row)
entry_cookie.pack(side="left", fill="x", expand=True)
entry_cookie.insert(0, load_cookie())
tk.Button(cookie_row, text="Save", command=handle_cookie_update).pack(side="left", padx=(5, 0))

# 1. Comment Template
tk.Label(root, text="Comment Template (Use {facility}, {tracking}, {date}):", font=("Arial", 10, "bold")).pack(anchor="w")
entry_comment = tk.Entry(root, width=80)
entry_comment.insert(0, "{facility} : {tracking} - ORDER WAS ESCALATED TO BE SHIPPED ( {date} ) // FARES.")
entry_comment.pack(pady=5, fill="x")

# 2. Data Input Box
tk.Label(root, text="Paste Excel Data (Order, Tracking, Facility):", font=("Arial", 10, "bold")).pack(anchor="w", pady=(10, 0))
text_input = scrolledtext.ScrolledText(root, height=10, width=70)
text_input.pack(pady=5, fill="both", expand=True)

# 3. Action Buttons
button_frame = tk.Frame(root)
button_frame.pack(pady=10, fill="x")

btn_run_all = tk.Button(button_frame, text="▶ Run Both", font=("Arial", 10, "bold"), bg="#4CAF50", fg="white", command=lambda: execute_excel_automation("both"))
btn_run_all.grid(row=0, column=0, sticky="ew", padx=3, pady=3)

btn_run_comment = tk.Button(button_frame, text="💬 Comment Only", font=("Arial", 10, "bold"), bg="#FF9800", fg="white", command=lambda: execute_excel_automation("comment"))
btn_run_comment.grid(row=0, column=1, sticky="ew", padx=3, pady=3)

btn_run_ship = tk.Button(button_frame, text="📦 Ship Only", font=("Arial", 10, "bold"), bg="#2196F3", fg="white", command=lambda: execute_excel_automation("ship"))
btn_run_ship.grid(row=0, column=2, sticky="ew", padx=3, pady=3)

btn_fast_check = tk.Button(button_frame, text="⚡ Fast Check Shipped", font=("Arial", 11, "bold"), bg="#673AB7", fg="white", command=execute_fast_check)
btn_fast_check.grid(row=1, column=0, columnspan=2, sticky="ew", padx=3, pady=5)

# NEW STOP BUTTON
btn_stop = tk.Button(button_frame, text="⏹ Stop", font=("Arial", 11, "bold"), bg="#F44336", fg="white", command=stop_automation, state=tk.DISABLED)
btn_stop.grid(row=1, column=2, sticky="ew", padx=3, pady=5)

button_frame.columnconfigure(0, weight=1)
button_frame.columnconfigure(1, weight=1)
button_frame.columnconfigure(2, weight=1)

# 4. Logs
tk.Label(root, text="Execution Logs:", font=("Arial", 10, "bold")).pack(anchor="w")
text_log = scrolledtext.ScrolledText(root, height=10, width=70, bg="#f4f4f4")
text_log.pack(pady=5, fill="both", expand=True)

root.mainloop()