"""
ORIGIN Operations Console - Unified Backend
====================================================
"""

from flask import Flask, request, jsonify, render_template, session, redirect, url_for
import requests
from bs4 import BeautifulSoup
import datetime
import time
import threading
import re
import os
import webbrowser
import logging
import asyncio
import aiohttp
import json
import uuid

app = Flask(__name__)
app.secret_key = os.urandom(24)

log_werkzeug = logging.getLogger('werkzeug')
log_werkzeug.setLevel(logging.ERROR)

# =====================================================================
# CONFIG & PG STATE
# =====================================================================
SESSION_FILE = "pg_session_cookie.txt"
DEFAULT_COOKIE = "COMMERCE_DEVICE_UUID=4104C997-8D9A-4762-ACF4-5F1DAB827959; COMMERCE_SESSION_ID=aa1p3ov05ood73c9ou5g9acte0;"
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36'
BASE_URL = "https://admin.partsgeek.com/suppliers/orders/view"
DASHBOARD_URL = "https://admin.partsgeek.com/suppliers/orders/index?ss=&rows=1000&sort=default&status=3"

automation_running = threading.Event()
app_logs = []
pending_fast_check_orders = []

def log(msg):
    app_logs.append(msg)
    print(msg)

def get_logs():
    global app_logs
    logs_copy = list(app_logs)
    app_logs.clear()
    return logs_copy

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
# PARTSGEEK SCRAPING LOGIC
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
    
    if target_text in current_text:
        return "SKIPPED (Template & Date Match)"

    payload = extract_form_payload(soup)
    payload.update({
        'action:update': '',
        'form[supplier_order_number]': order_num,
        'form[comments]': target_text
    })
    
    resp = SESSION.post(url, data=payload, headers=post_headers(url), allow_redirects=False, timeout=20)
    check_response(resp)
    
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
def process_comment_push(items):
    automation_running.set()
    log("\n--- STARTING COMMENT PUSH TO PARTSGEEK ---")
    today = get_today_date()
    
    for item in items:
        if not automation_running.is_set():
            log("--- Stopped by User ---")
            break
            
        order_num = item['order']
        facility_raw = item.get('ship_from', 'DT').upper()
        facility = 'DT' if 'DT' in facility_raw else ('JZ' if 'JZ' in facility_raw else facility_raw)
        
        if item['category'] == 'SHIPPED':
            tracking = item.get('tracking', '')
            comment_text = f"{facility} : {tracking} - ORDER WAS ESCALATED TO BE SHIPPED ( {today} ) // FARES."
        else:
            comment_text = f"DT : NO TN - ORDER WAS ESCALATED TO BE SHIPPED ( {today} ) // FARES."
            
        try:
            resp_pg, soup_pg, url_pg = fetch_order_page(order_num)
            
            if "In Transit" in resp_pg.text:
                log(f"[SKIP] {order_num}: Already In Transit")
                continue
                
            status = submit_comment(order_num, soup_pg, url_pg, comment_text)
            log(f"[CMNT] {order_num} -> {status}")
            
        except CloudflareBlockedError as e:
            log(f"[BLOCKED] {order_num}: {e}")
            break
        except Exception as e:
            log(f"[ERROR] {order_num}: {e}")
        
        time.sleep(0.3)
        
    log("--- COMMENT PUSH COMPLETE ---")
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
# DEPOSCO WMS ENGINE
# =====================================================================
async def authenticate_deposco(http_session, company, username, password):
    url = "https://dax.deposco.com/deposco/resources/nonsecure/authenticate"
    payload = {"company": company, "username": username, "password": password, "isMobile": False}
    headers = {"accept": "application/json, text/plain, */*", "content-type": "application/json"}
    
    try:
        async with http_session.post(url, json=payload, headers=headers, timeout=10) as response:
            if response.status != 200:
                return None, f"Login Failed (HTTP {response.status})"
            data = await response.json(content_type=None)
            token = data.get('X-Auth-Token')
            return (token, "Success") if token else (None, "Token not found")
    except Exception as e:
        return None, f"Connection Error: {str(e)}"

def get_view_payload(po_number):
    return {
        "view": {
            "id": 10644, "entityId": 5053, "companyId": 73, "userId": 2509, "groupId": 0,
            "text": "Rob Tracking numbers", "entityName": "OrderHeader", "entityClass": "com.deposco.domain.OrderHeader",
            "isOwn": True, "isShared": True, "bookmarkActive": False, "addActionLinks": True,
            "columns": [
                {"title": "Customer Order Number", "name": "customerOrderNumber", "fieldName": "customerOrderNumber", "sortOrder": 0, "dataType": "Text", "returnDataType": "Text", "filtering": {"filterString": po_number, "operator": 5, "filterStrings": []}, "length": 50, "displayOrder": 3, "entityId": 5053, "entity": "com.deposco.domain.OrderHeader", "entityType": "Business", "entityName": "OrderHeader", "attributeId": 114035, "subAttributeId": 0, "relatedToId": 0, "required": False, "readOnly": False, "custom": False, "searchable": True, "sortable": False, "businessKey": False, "isEntityTag": False, "allowNegative": False},
                {"title": "Number", "name": "number", "fieldName": "number", "displayOrder": 0},
                {"title": "Updated Date", "name": "updatedDate", "fieldName": "updatedDate", "displayOrder": 1},
                {"title": "Created Date", "name": "createdDate", "fieldName": "createdDate", "displayOrder": 2},
                {"title": "Current Status", "name": "currentStatus", "fieldName": "currentStatus", "displayOrder": 5},
                {"title": "Ship From Facility - Number", "name": "shipFrom.number", "fieldName": "shipFrom", "displayOrder": 15},
                {"title": "Tracking Link(s)", "name": "baseTrackingLink", "fieldName": "baseTrackingLink", "dataType": "API", "apiSql": "SELECT group_concat(concat( CASE WHEN c_.TRACKING_NUMBER IS NOT NULL THEN COALESCE(concat(ss_.SHIP_VENDOR, '=', ss_.FREIGHT_TYPE, '=', c_.TRACKING_NUMBER, ','),'') ELSE '' END , '', COALESCE(concat(ss_.SHIP_VENDOR, '=', ss_.FREIGHT_TYPE, '=', ch_.TRACKING_NUMBER), '') )) from SHIPMENT_ORDER_HEADER soh_ inner join SHIPMENT s_ on soh_.SHIPMENT_ID = s_.SHIPMENT_ID INNER JOIN SHIPPING_SERVICE ss_ ON ss_.ship_via = s_.SHIP_VIA LEFT JOIN CONTAINER c_ ON c_.shipment_id = s_.shipment_id LEFT JOIN CONTAINER_HIST ch_ ON ch_.shipment_id = s_.shipment_id WHERE soh_.ORDER_HEADER_ID = :id", "displayOrder": 11},
                {"title": "Tracking Number", "name": "billToPhone2", "fieldName": "billToPhone2", "displayOrder": 70}
            ],
            "filterAttributes": [], "numberOfRows": 100
        },
        "page": 1, "rowsPerPage": 100, "uiRowsPerPage": -1, "useLabel": False, "isExport": False, "translateEnums": False
    }

async def fetch_order(http_session, po_number, token, semaphore):
    headers = {"accept": "application/json, text/plain, */*", "content-type": "application/json", "authorization": f"Bearer {token}"}
    
    async with semaphore:
        try:
            view_api_url = "https://dax.deposco.com/deposco/resources/secure/entity"
            payload = get_view_payload(po_number)
            
            async with http_session.post(view_api_url, headers=headers, json=payload, timeout=15) as res_view:
                if res_view.status != 200:
                    return [{"order": po_number, "category": "NOT_FOUND", "reason": f"View HTTP {res_view.status}"}]
                    
                view_data = await res_view.json(content_type=None)
                records = view_data.get("response", [])
                
                if not records:
                    return [{"order": po_number, "category": "NOT_FOUND", "reason": "Not Found in Deposco"}]
                    
                out_results = []
                restricted_prefixes = ("CA", "PA", "RE", "PA+")

                for rec in records:
                    so_num = rec.get("number", "N/A")
                    cust_order = rec.get("customerOrderNumber") or "N/A"
                    created_date = rec.get("createdDate", "N/A")
                    ship_from = rec.get("shipFrom.number", "N/A")
                    status = rec.get("currentStatus", "Unknown")
                    
                    base_track = rec.get("baseTrackingLink") or ""
                    fallback_track = rec.get("billToPhone2") or ""
                    
                    has_warning = False
                    if so_num and so_num != "N/A":
                        if any(so_num.upper().startswith(p) for p in restricted_prefixes):
                            has_warning = True

                    t_nums = []
                    if base_track:
                        for link in base_track.split(','):
                            if '=' in link: t_nums.append(link.split('=')[-1].strip())
                                
                    final_trk = " | ".join([t for t in t_nums if t])
                    if not final_trk and fallback_track: final_trk = fallback_track.strip()
                        
                    if final_trk:
                        out_results.append({
                            "order": po_number, "so_number": so_num, "customer_order": cust_order, 
                            "ship_from": ship_from, "created_date": created_date, "category": "SHIPPED", 
                            "status": status, "tracking": final_trk, "has_prefix_warning": has_warning,
                            "raw_trackings": t_nums or ([fallback_track.strip()] if fallback_track else [])
                        })
                    else:
                        out_results.append({
                            "order": po_number, "so_number": so_num, "customer_order": cust_order, 
                            "ship_from": ship_from, "created_date": created_date, "category": "NO_TRACKING", 
                            "status": status, "reason": "No tracking info found", "has_prefix_warning": has_warning,
                            "raw_trackings": []
                        })
                        
                return out_results

        except Exception as e:
            return [{"order": po_number, "category": "NOT_FOUND", "reason": f"Error: {type(e).__name__}"}]

async def process_batch(company, username, password, order_numbers):
    semaphore = asyncio.Semaphore(30)
    async with aiohttp.ClientSession() as http_session:
        token, auth_msg = await authenticate_deposco(http_session, company, username, password)
        
        if not token:
            return {"error": f"Authentication Failed: {auth_msg}"}
            
        tasks = [fetch_order(http_session, order, token, semaphore) for order in order_numbers]
        results = await asyncio.gather(*tasks)
        
        flat_results = []
        for sublist in results: flat_results.extend(sublist)

        shipped = [r for r in flat_results if r["category"] == "SHIPPED"]
        no_tracking = [r for r in flat_results if r["category"] == "NO_TRACKING"]
        not_found = [r for r in flat_results if r["category"] == "NOT_FOUND"]

        return {"shipped": shipped, "no_tracking": no_tracking, "not_found": not_found}

def parse_cookie_input(cookie_input):
    try:
        cookies = json.loads(cookie_input)
        if isinstance(cookies, list):
            return "; ".join([f"{c['name']}={c['value']}" for c in cookies if 'name' in c and 'value' in c])
    except Exception:
        pass
    return cookie_input

async def async_fetch_walmart_unshipped(session_cookie):
    url = "https://seller.walmart.com/aurora/v2/auroraOrderService/gql"
    xsrf_match = re.search(r'XSRF-TOKEN=([^;]+)', session_cookie)
    xsrf_token = xsrf_match.group(1) if xsrf_match else ""

    headers = {
        "accept": "application/json", "content-type": "application/json", "cookie": session_cookie,
        "origin": "https://seller.walmart.com", "user-agent": USER_AGENT, "wm_aurora.locale": "en-US",
        "wm_aurora.market": "US", "wm_svc.name": "API"
    }
    if xsrf_token: headers["x-xsrf-token"] = xsrf_token

    fetch_query = """query get_orders_getAllOrders($params: SearchParams) {
      get_orders_getAllOrders(searchParams: $params) {
         orderInfo { purchaseOrders { purchaseOrderId } }
      }
    }"""
    payload = {"query": fetch_query, "variables": {"params": {"orderGroups": "Unshipped", "pageInfo": {"limit": "200", "offset": "0", "cursor": "*"}}}}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload, headers=headers, timeout=15) as res:
                if res.status != 200: return {"success": False, "error": f"HTTP {res.status}"}
                data = await res.json()
                if "errors" in data: return {"success": False, "error": str(data["errors"])}
                
                purchase_orders = data.get("data", {}).get("get_orders_getAllOrders", {}).get("orderInfo", {}).get("purchaseOrders", [])
                orders = [o.get("purchaseOrderId") for o in purchase_orders if o.get("purchaseOrderId")]
                return {"success": True, "orders": orders}
        except Exception as e:
            return {"success": False, "error": str(e)}

async def async_update_walmart_tracking(po_number, tracking_number_str, session_cookie):
    url = "https://seller.walmart.com/aurora/v2/auroraOrderService/gql"
    trackings = [t.strip() for t in re.split(r'[,\s\t]+', str(tracking_number_str)) if t.strip()]
    if not trackings: return {"success": False, "error": "No valid tracking numbers parsed"}

    xsrf_match = re.search(r'XSRF-TOKEN=([^;]+)', session_cookie)
    xsrf_token = xsrf_match.group(1) if xsrf_match else ""

    headers = {
        "accept": "application/json", "content-type": "application/json", "cookie": session_cookie,
        "origin": "https://seller.walmart.com", "user-agent": USER_AGENT, "wm_aurora.locale": "en-US",
        "wm_aurora.market": "US", "wm_svc.name": "API"
    }
    if xsrf_token: headers["x-xsrf-token"] = xsrf_token

    async with aiohttp.ClientSession() as session:
        headers["pxqueryname"] = "get_orders_getAllOrders,get_orders_getAllOrders"
        headers["wm_qos.correlation_id"] = str(uuid.uuid4())
        fetch_query = """query get_orders_getAllOrders($params: SearchParams) { get_orders_getAllOrders(searchParams: $params) { orderInfo { purchaseOrders { poLines { lineId primeLineNo quantity } } } } }"""
        fetch_payload = {"query": fetch_query, "variables": {"params": {"orderGroups": "All", "isDetailPage": True, "poNumber": str(po_number).strip()}}}
        
        po_lines = []
        try:
            async with session.post(url, json=fetch_payload, headers=headers, timeout=15) as res:
                if res.status == 200:
                    data = await res.json()
                    po_lines = data.get("data", {}).get("get_orders_getAllOrders", {}).get("orderInfo", {}).get("purchaseOrders", [{}])[0].get("poLines", [])
        except Exception: pass
        if not po_lines: po_lines = [{"lineId": ["1"], "primeLineNo": [1], "quantity": 1}]

        poLineRequestDTOList, unused_trackings = [], []
        num_lines, num_tracks = len(po_lines), len(trackings)

        for i, line in enumerate(po_lines):
            trk = trackings[i] if i < num_tracks else trackings[-1]
            poLineRequestDTOList.append({
                "lineIds": [str(line.get("lineId", ["1"])[0])], "primeLineNo": [int(line.get("primeLineNo", [1])[0])],
                "updatedQuantity": str(line.get("quantity", "1")), "updatedStatus": "Shipped", "intentToCancelOverride": False,
                "shipmentInfo": {"carrierServiceCode": "FDX-ST", "trackingNo": trk}
            })
        if num_tracks > num_lines: unused_trackings = trackings[num_lines:]

        headers["pxqueryname"] = "update_orders_updateOrder,update_orders_updateOrder"
        headers["wm_qos.correlation_id"] = str(uuid.uuid4())
        update_query = """mutation update_orders_updateOrder($input: [PoUpdateRequest]) { update_orders_updateOrder(poUpdateRequest: $input) { poUpdateResponseStatus { poNumber updateResponsePoLineList { status statusDescription lineIds error } errorList } } }"""
        update_payload = {"query": update_query, "variables": {"input": [{"poNumber": str(po_number).strip(), "isWCPOrder": False, "poLineRequestDTOList": poLineRequestDTOList}]}}

        try:
            async with session.post(url, json=update_payload, headers=headers, timeout=15) as res:
                if res.status != 200: return {"success": False, "error": f"HTTP {res.status}"}
                update_data = await res.json()
                if "errors" in update_data: return {"success": False, "error": str(update_data["errors"])}
                try:
                    resp_status = update_data["data"]["update_orders_updateOrder"]["poUpdateResponseStatus"]
                    if resp_status.get("errorList"): return {"success": False, "error": str(resp_status["errorList"])}
                    for l in resp_status.get("updateResponsePoLineList", []):
                        if l.get("error"): return {"success": False, "error": l.get("statusDescription")}
                except Exception: pass
                return {"success": True, "po_number": po_number, "tracking": trackings, "unused_trackings": unused_trackings}
        except Exception as e:
            return {"success": False, "error": str(e)}

# =====================================================================
# API ROUTES
# =====================================================================
@app.route('/')
def index():
    if "username" not in session: return redirect(url_for('login'))
    return redirect(url_for('dashboard'))

@app.route('/login', methods=['GET', 'POST'])
async def login():
    if request.method == 'GET':
        if "username" in session: return redirect(url_for("dashboard"))
        return render_template("login.html")
        
    data = request.json
    async with aiohttp.ClientSession() as http_session:
        token, auth_msg = await authenticate_deposco(
            http_session, data.get("company", "").strip(), 
            data.get("username", "").strip(), data.get("password", "").strip()
        )
        if token:
            session.update({
                "company": data.get("company", "").strip(), 
                "username": data.get("username", "").strip(), "password": data.get("password", "").strip()
            })
            return jsonify({"success": True})
        return jsonify({"success": False, "error": auth_msg}), 401

@app.route('/dashboard')
def dashboard():
    if "username" not in session: return redirect(url_for('login'))
    return render_template('index.html', cookie=load_cookie(), company=session["company"])

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route('/api/extract', methods=["POST"])
async def extract():
    if "username" not in session: return jsonify({"error": "Not authenticated. Please log in again."}), 401
    orders = [o.strip() for o in request.json.get("orders", "").replace(',', ' ').split() if o.strip()]
    if not orders: return jsonify({"error": "No valid orders provided"}), 400
    results = await process_batch(session["company"], session["username"], session["password"], orders)
    return jsonify(results)

@app.route('/api/push_comments', methods=['POST'])
def push_comments():
    if "username" not in session: return jsonify({"error": "Not authenticated."}), 401
    if automation_running.is_set(): return jsonify({"error": "Already running"}), 400
    
    data = request.json
    items = data.get('items', [])
    if not items: return jsonify({"error": "No items provided"}), 400
    
    threading.Thread(target=process_comment_push, args=(items,), daemon=True).start()
    return jsonify({"status": "started"})

@app.route('/api/get-walmart-orders', methods=['POST'])
async def get_walmart_unshipped():
    if "username" not in session: return jsonify({"success": False, "error": "Not authenticated."}), 401
    data = request.get_json()
    if not data.get('session_cookie'): return jsonify({"success": False, "error": "Missing Walmart session cookies."}), 400
    session_cookie = parse_cookie_input(data.get('session_cookie'))
    result = await async_fetch_walmart_unshipped(session_cookie)
    return jsonify(result), 200

@app.route('/api/update-walmart', methods=['POST'])
async def update_walmart_order():
    if "username" not in session: return jsonify({"success": False, "error": "Not authenticated."}), 401
    data = request.get_json()
    if not all([data.get('po_number'), data.get('tracking_number'), data.get('session_cookie')]):
        return jsonify({"success": False, "error": "Missing required data"}), 400
    session_cookie = parse_cookie_input(data.get('session_cookie'))
    result = await async_update_walmart_tracking(data.get('po_number'), data.get('tracking_number'), session_cookie)
    return jsonify(result), 200

@app.route('/api/stop', methods=['POST'])
def stop():
    if "username" not in session: return jsonify({"error": "Not authenticated."}), 401
    automation_running.clear()
    log("\n[SYSTEM] Stop signal sent. Halting...")
    return jsonify({"status": "stopped"})

@app.route('/api/logs', methods=['GET'])
def get_recent_logs():
    return jsonify({"logs": get_logs(), "is_running": automation_running.is_set()})

if __name__ == '__main__':
    def open_browser():
        webbrowser.open_new('http://127.0.0.1:5000/')
    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        threading.Timer(1.0, open_browser).start()
    app.run(debug=True, port=5000)
