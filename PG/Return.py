import tkinter as tk
from tkinter import ttk, messagebox
import asyncio
import aiohttp
import json
import threading
import webbrowser

# Global dictionary to store the full API JSON data for each inspected tracking number
fetched_data_cache = {}

async def check_tracking(session, tracking_number, semaphore):
    """Checks tracking, handles rate limits, and returns the raw JSON data if found."""
    url = f"https://returns.detroitaxle.com/api/returns/tracking?tracking={tracking_number}"
    
    async with semaphore:
        for attempt in range(3):
            try:
                async with session.get(url, timeout=15) as response:
                    if response.status == 429:
                        # Exponential backoff for rate limits
                        await asyncio.sleep(2 ** attempt)
                        continue 
                        
                    if response.status != 200:
                        return tracking_number, f"HTTP {response.status}", None
                    
                    text = await response.text()
                    try:
                        data = json.loads(text)
                        is_found = isinstance(data, list) and len(data) > 0
                        return tracking_number, "Inspected" if is_found else "Not Found", data if is_found else None
                    except json.JSONDecodeError:
                        return tracking_number, "Parse Error", None
                        
            except asyncio.TimeoutError:
                if attempt < 2:
                    await asyncio.sleep(1)
                    continue
                return tracking_number, "Timeout Error", None
            except Exception:
                return tracking_number, "Network Error", None
                
        return tracking_number, "HTTP 429 (Rate Limited)", None

async def process_trackings_async(tracking_numbers, update_ui_callback, finish_callback):
    """Processes trackings concurrently with a strict connection limit."""
    semaphore = asyncio.Semaphore(10) 
    connector = aiohttp.TCPConnector(limit=10)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [
            asyncio.ensure_future(check_tracking(session, t, semaphore))
            for t in tracking_numbers
        ]
        
        for coro in asyncio.as_completed(tasks):
            tracking, status, data = await coro
            
            # If we got data back, cache it so we can extract the ID later
            if data:
                fetched_data_cache[tracking] = data
                
            update_ui_callback(tracking, status)
            
    finish_callback()

class InspectionDashboard:
    def __init__(self, root):
        self.root = root
        self.root.title("📦 Detroit Axle Inspection Checker")
        self.root.geometry("550x650")
        self.root.configure(padx=20, pady=20)
        
        # --- UI Layout ---
        tk.Label(root, text="Paste Tracking Numbers (one per line or comma-separated):", font=("Arial", 10, "bold")).pack(anchor="w")
        
        self.text_input = tk.Text(root, height=8, font=("Consolas", 10))
        self.text_input.pack(fill="x", pady=(5, 10))
        
        self.check_btn = tk.Button(root, text="Check Status", bg="#0d6efd", fg="white", font=("Arial", 11, "bold"), command=self.start_checking)
        self.check_btn.pack(fill="x", pady=(0, 10))
        
        tk.Label(root, text="💡 Double-click an 'Inspected' row to view it in Chrome.", font=("Arial", 9, "italic"), fg="#055160").pack(anchor="w", pady=(0, 5))
        
        self.status_label = tk.Label(root, text="Ready.", fg="#666")
        self.status_label.pack(anchor="w", pady=(0, 5))
        
        # --- Treeview (Table) Setup ---
        columns = ("Tracking Number", "Status")
        self.tree = ttk.Treeview(root, columns=columns, show="headings", height=15)
        self.tree.heading("Tracking Number", text="Tracking Number")
        self.tree.heading("Status", text="Status")
        self.tree.column("Tracking Number", width=250)
        self.tree.column("Status", width=150)
        self.tree.pack(fill="both", expand=True)
        
        self.tree.tag_configure("inspected", background="#d1e7dd", foreground="#0f5132")
        self.tree.tag_configure("not_found", background="#f8d7da", foreground="#842029")
        self.tree.tag_configure("error", background="#fff3cd", foreground="#664d03")

        # Bind the Double-Click Event
        self.tree.bind("<Double-1>", self.on_row_double_click)

    def start_checking(self):
        raw_text = self.text_input.get("1.0", tk.END)
        raw_text = raw_text.replace(',', ' ')
        tracking_list = [t.strip() for t in raw_text.split() if t.strip()]
        
        if not tracking_list:
            self.status_label.config(text="Please paste at least one tracking number.")
            return
            
        # Reset UI & Cache
        self.tree.delete(*self.tree.get_children())
        fetched_data_cache.clear()
        
        self.check_btn.config(state="disabled", text="Checking...")
        self.status_label.config(text=f"Processing {len(tracking_list)} numbers...")
        
        # Fire background thread
        threading.Thread(target=self.run_async_loop, args=(tracking_list,), daemon=True).start()

    def run_async_loop(self, tracking_list):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(process_trackings_async(tracking_list, self.safe_update_row, self.safe_finish))
        loop.close()

    def safe_update_row(self, tracking, status):
        self.root.after(0, self._insert_row, tracking, status)
        
    def _insert_row(self, tracking, status):
        tag = "error"
        if status == "Inspected":
            tag = "inspected"
        elif status == "Not Found":
            tag = "not_found"
            
        self.tree.insert("", "end", values=(tracking, status), tags=(tag,))

    def safe_finish(self):
        self.root.after(0, self._finish_ui)
        
    def _finish_ui(self):
        self.check_btn.config(state="normal", text="Check Status")
        self.status_label.config(text=f"✅ Finished checking {len(self.tree.get_children())} numbers.")

    def on_row_double_click(self, event):
        """Extracts the internal ID and opens the Detroit Axle return page in the browser."""
        selected_item = self.tree.selection()
        if not selected_item:
            return
            
        item = self.tree.item(selected_item)
        tracking_number, status = item['values']
        
        if status == "Inspected" and str(tracking_number) in fetched_data_cache:
            data = fetched_data_cache[str(tracking_number)]
            
            # The API returns a list. We grab the 'id' from the first dictionary.
            if isinstance(data, list) and len(data) > 0:
                return_id = data[0].get("id")
                
                if return_id:
                    url = f"https://returns.detroitaxle.com/returns/{return_id}"
                    webbrowser.open(url)
                else:
                    messagebox.showerror("Data Error", "Could not locate the Return ID for this tracking number.")
                    
        elif status != "Inspected":
            messagebox.showinfo("Not Available", "This tracking number has not been inspected yet.")

if __name__ == "__main__":
    root = tk.Tk()
    app = InspectionDashboard(root)
    root.mainloop()