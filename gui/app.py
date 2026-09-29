import tkinter as tk
from tkinter import filedialog, messagebox
import ttkbootstrap as ttk
from ttkbootstrap.constants import *
import threading
import queue
import os

from tkinter.ttk import PanedWindow
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from core.scanner import DirectoryScanner
from utils.formatters import format_size

ICON_CLOSED = "📁"
ICON_OPEN   = "📂"

# ── Chart palette (darkly-friendly) ──────────────────────────────────
_BG      = "#2b3035"
_COLORS  = [
    "#3498db", "#e74c3c", "#2ecc71", "#f39c12",
    "#9b59b6", "#1abc9c", "#e67e22", "#fd79a8",
    "#00cec9", "#a29bfe",
]


class FolderSizeApp(ttk.Window):
    def __init__(self):
        super().__init__(themename="darkly")
        self.title("FolderSize")
        self.geometry("1100x640")
        self.minsize(800, 480)

        self.scanner    = None
        self.scan_thread = None
        self.msg_queue  = queue.Queue()

        # Focus / hover state
        self._hovered_item   = None
        self._focused_item   = None
        self._subtree_tagged = set()

        # iid ↔ name mapping for icon swaps
        self.iid_to_name = {}
        self.nodes       = {}

        # Folder-size data for the pie chart
        self.folder_sizes  = {}   # path  -> bytes (int)
        self.children_map  = {}   # path  -> [direct child paths]

        self._setup_ui()
        self._check_queue()

    # ── UI setup ──────────────────────────────────────────────────────

    def _setup_ui(self):
        # Toolbar
        toolbar = ttk.Frame(self, padding=(10, 8))
        toolbar.pack(fill=X)

        self.btn_select = ttk.Button(toolbar, text="Select Folder",
                                     command=self.select_folder, bootstyle=PRIMARY)
        self.btn_select.pack(side=LEFT, padx=(0, 6))

        self.lbl_path = ttk.Label(toolbar, text="No folder selected",
                                  bootstyle=SECONDARY)
        self.lbl_path.pack(side=LEFT, padx=5, fill=X, expand=YES)

        self.btn_scan = ttk.Button(toolbar, text="Scan",
                                   command=self.start_scan,
                                   bootstyle=SUCCESS, state=DISABLED)
        self.btn_scan.pack(side=LEFT, padx=6)

        self.btn_cancel = ttk.Button(toolbar, text="Cancel",
                                     command=self.cancel_scan,
                                     bootstyle=DANGER, state=DISABLED)
        self.btn_cancel.pack(side=LEFT)

        # Horizontal pane: tree (left) | chart (right)
        pane = PanedWindow(self, orient=HORIZONTAL)
        pane.pack(fill=BOTH, expand=YES, padx=10, pady=(4, 0))

        # ── Left: tree ────────────────────────────────────────────────
        left = ttk.Frame(pane)
        pane.add(left, weight=3)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        columns = ("size", "path")
        self.tree = ttk.Treeview(left, columns=columns,
                                 show="tree headings",
                                 bootstyle=INFO, selectmode="browse")
        self.tree.heading("#0",    text="Folder",    anchor=W)
        self.tree.heading("size",  text="Size",      anchor=E)
        self.tree.heading("path",  text="Full Path", anchor=W)
        self.tree.column("#0",   width=280, anchor=W, minwidth=140)
        self.tree.column("size", width=100, anchor=E, stretch=False)
        self.tree.column("path", width=380, anchor=W)

        ttk.Style().configure("Treeview", rowheight=26)

        # Tags
        self.tree.tag_configure("focused_row",
                                font=("Segoe UI", 10, "bold"))
        self.tree.tag_configure("subtree",
                                background="#1c3547", foreground="#aec6cf")
        self.tree.tag_configure("hover",
                                background="#2e4057")

        vsb = ttk.Scrollbar(left, orient=VERTICAL,   command=self.tree.yview)
        hsb = ttk.Scrollbar(left, orient=HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        # Bindings
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Motion>",           self._on_hover)
        self.tree.bind("<Leave>",            self._on_leave)
        self.tree.bind("<<TreeviewOpen>>",   self._on_expand)
        self.tree.bind("<<TreeviewClose>>",  self._on_collapse)

        # Context menu
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="Open in Explorer", command=self.open_folder)
        self.menu.add_command(label="Copy Path",        command=self.copy_path)
        self.tree.bind("<Button-3>", self.show_context_menu)

        # ── Right: pie chart ──────────────────────────────────────────
        right = ttk.Frame(pane, padding=4)
        pane.add(right, weight=2)

        self.fig = Figure(facecolor=_BG)
        self.ax  = self.fig.add_subplot(111, facecolor=_BG)
        self.fig.subplots_adjust(top=0.90, bottom=0.28, left=0.05, right=0.95)

        self.chart_canvas = FigureCanvasTkAgg(self.fig, master=right)
        self.chart_canvas.get_tk_widget().pack(fill=BOTH, expand=YES)

        self._draw_empty_chart()

        # ── Status bar ────────────────────────────────────────────────
        status_frame = ttk.Frame(self, padding=(10, 4))
        status_frame.pack(fill=X, side=BOTTOM)

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(status_frame, textvariable=self.status_var,
                  bootstyle=INFO).pack(side=LEFT)

        self.progress = ttk.Progressbar(status_frame, mode="indeterminate",
                                        bootstyle=SUCCESS)

    # ── Focus / hover ─────────────────────────────────────────────────

    def _on_select(self, event=None):
        self._clear_focus_tags()
        selected = self.tree.selection()
        if not selected:
            return
        item = selected[0]
        self._focused_item = item
        self.tree.item(item, tags=("focused_row",))
        for child in self._iter_children(item):
            self.tree.item(child, tags=("subtree",))
            self._subtree_tagged.add(child)
        # Update pie chart for the selected folder's path
        path = self.tree.item(item, "values")[1]
        self._update_pie_chart(path)

    def _clear_focus_tags(self):
        for item in self._subtree_tagged:
            try:
                self.tree.item(item, tags=())
            except tk.TclError:
                pass
        self._subtree_tagged.clear()
        if self._focused_item:
            try:
                self.tree.item(self._focused_item, tags=())
            except tk.TclError:
                pass
            self._focused_item = None

    def _iter_children(self, item):
        for child in self.tree.get_children(item):
            yield child
            yield from self._iter_children(child)

    def _on_hover(self, event):
        item = self.tree.identify_row(event.y)
        if item == self._hovered_item:
            return
        if self._hovered_item:
            tags = [t for t in self.tree.item(self._hovered_item, "tags")
                    if t != "hover"]
            try:
                self.tree.item(self._hovered_item, tags=tags)
            except tk.TclError:
                pass
        self._hovered_item = item
        if item and item not in self.tree.selection():
            tags = list(self.tree.item(item, "tags"))
            if "hover" not in tags:
                tags.append("hover")
            self.tree.item(item, tags=tags)

    def _on_leave(self, event):
        if self._hovered_item:
            tags = [t for t in self.tree.item(self._hovered_item, "tags")
                    if t != "hover"]
            try:
                self.tree.item(self._hovered_item, tags=tags)
            except tk.TclError:
                pass
            self._hovered_item = None

    def _on_expand(self, event):
        item = self.tree.focus()
        name = self.iid_to_name.get(item, "")
        self.tree.item(item, text=f"{ICON_OPEN} {name}")

    def _on_collapse(self, event):
        item = self.tree.focus()
        name = self.iid_to_name.get(item, "")
        self.tree.item(item, text=f"{ICON_CLOSED} {name}")

    # ── Pie chart ─────────────────────────────────────────────────────

    def _update_pie_chart(self, path):
        """Draw a pie chart for the direct children of `path`."""
        parent_size = self.folder_sizes.get(path, 0)

        if parent_size == 0:
            self._draw_empty_chart("(empty folder)")
            return

        # Gather direct children with non-zero size
        entries = []
        children_total = 0
        for child_path in self.children_map.get(path, []):
            sz = self.folder_sizes.get(child_path, 0)
            if sz > 0:
                entries.append((os.path.basename(child_path), sz))
                children_total += sz

        # Remaining bytes = files sitting directly in this folder
        files_sz = parent_size - children_total
        if files_sz > 0:
            entries.append(("(files here)", files_sz))

        if not entries:
            self._draw_empty_chart("(empty)")
            return

        entries.sort(key=lambda x: x[1], reverse=True)

        # Cap at 9 slices; bundle the rest into "Other"
        if len(entries) > 9:
            other_sz = sum(s for _, s in entries[8:])
            entries   = entries[:8] + [("Other", other_sz)]

        labels = [e[0] for e in entries]
        sizes  = [e[1] for e in entries]

        self.ax.clear()
        self.ax.set_facecolor(_BG)

        wedges, _, autotexts = self.ax.pie(
            sizes,
            autopct=lambda p: f"{p:.0f}%" if p >= 5 else "",
            startangle=90,
            colors=_COLORS[:len(sizes)],
            wedgeprops={"linewidth": 1.5, "edgecolor": "#1e2730"},
            pctdistance=0.72,
            radius=0.85,
        )
        for at in autotexts:
            at.set_color("white")
            at.set_fontsize(7)
            at.set_fontweight("bold")

        folder_name = os.path.basename(path) or path
        self.ax.set_title(folder_name, color="#ced4da", fontsize=9, pad=6)

        legend_lines = [f"{l}  ({format_size(s)})" for l, s in zip(labels, sizes)]
        self.ax.legend(
            wedges, legend_lines,
            loc="upper center",
            bbox_to_anchor=(0.5, -0.02),
            ncol=2,
            fontsize=6.5,
            framealpha=0.15,
            labelcolor="#ced4da",
            facecolor=_BG,
            edgecolor="#444",
        )

        self.chart_canvas.draw()

    def _draw_empty_chart(self, msg="Select a folder\nto see its size breakdown"):
        self.ax.clear()
        self.ax.set_facecolor(_BG)
        self.ax.text(0.5, 0.5, msg,
                     ha="center", va="center",
                     color="#6c757d", fontsize=10,
                     transform=self.ax.transAxes,
                     multialignment="center")
        self.ax.set_axis_off()
        self.chart_canvas.draw()

    # ── Context menu ──────────────────────────────────────────────────

    def show_context_menu(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.menu.tk_popup(event.x_root, event.y_root)

    def open_folder(self):
        selected = self.tree.selection()
        if not selected:
            return
        path = self.tree.item(selected[0], "values")[1]
        if os.path.exists(path):
            os.startfile(path)

    def copy_path(self):
        selected = self.tree.selection()
        if not selected:
            return
        path = self.tree.item(selected[0], "values")[1]
        self.clipboard_clear()
        self.clipboard_append(path)

    # ── Scan controls ─────────────────────────────────────────────────

    def select_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.lbl_path.config(text=folder)
            self.selected_folder = folder
            self.btn_scan.config(state=NORMAL)

    def start_scan(self):
        if not hasattr(self, "selected_folder") or not self.selected_folder:
            return
        self.btn_select.config(state=DISABLED)
        self.btn_scan.config(state=DISABLED)
        self.btn_cancel.config(state=NORMAL)
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.iid_to_name.clear()
        self.nodes.clear()
        self.folder_sizes.clear()
        self.children_map.clear()
        self._hovered_item   = None
        self._focused_item   = None
        self._subtree_tagged.clear()
        self._draw_empty_chart("Scanning…")
        self.progress.pack(side=RIGHT, padx=10, fill=X, expand=YES)
        self.progress.start(10)
        self.scanner = DirectoryScanner(callback=self._scanner_callback)
        self.scan_thread = threading.Thread(
            target=self._run_scan, args=(self.selected_folder,), daemon=True)
        self.scan_thread.start()

    def cancel_scan(self):
        if self.scanner:
            self.scanner.cancel()
            self.status_var.set("Cancelling…")

    def _scanner_callback(self, current_path):
        self.msg_queue.put({"type": "progress", "path": current_path})

    def _run_scan(self, root_dir):
        try:
            results = self.scanner.scan(root_dir)
            if self.scanner.is_cancelled:
                self.msg_queue.put({"type": "cancelled"})
            else:
                self.msg_queue.put({"type": "done", "results": results})
        except Exception as e:
            self.msg_queue.put({"type": "error", "message": str(e)})

    def _check_queue(self):
        while not self.msg_queue.empty():
            try:
                msg = self.msg_queue.get_nowait()
                if msg["type"] == "progress":
                    path = msg["path"]
                    if len(path) > 70:
                        path = "..." + path[-67:]
                    self.status_var.set(f"Scanning: {path}")
                elif msg["type"] == "done":
                    self._populate_tree(msg["results"])
                    self._reset_ui("Scan completed.")
                elif msg["type"] == "cancelled":
                    self._draw_empty_chart()
                    self._reset_ui("Scan cancelled.")
                elif msg["type"] == "error":
                    messagebox.showerror("Error", msg["message"])
                    self._reset_ui("Error during scan.")
            except queue.Empty:
                break
        self.after(100, self._check_queue)

    def _reset_ui(self, status_msg):
        self.progress.stop()
        self.progress.pack_forget()
        self.btn_select.config(state=NORMAL)
        self.btn_scan.config(state=NORMAL)
        self.btn_cancel.config(state=DISABLED)
        self.status_var.set(status_msg)

    def _populate_tree(self, raw_sizes):
        self.folder_sizes = dict(raw_sizes)

        sorted_paths = sorted(raw_sizes.keys(), key=lambda p: p.count(os.sep))
        for path in sorted_paths:
            size        = raw_sizes[path]
            parent_path = os.path.dirname(path)

            if parent_path in self.nodes and path != parent_path:
                parent_iid = self.nodes[parent_path]
                is_open    = False
                # Register this path as a child of its parent
                self.children_map.setdefault(parent_path, []).append(path)
            else:
                parent_iid = ""
                is_open    = True

            folder_name = os.path.basename(path) or path
            icon = ICON_OPEN if is_open else ICON_CLOSED
            iid  = self.tree.insert(parent_iid, END,
                                    text=f"{icon} {folder_name}",
                                    values=(format_size(size), path),
                                    open=is_open)
            self.nodes[path]       = iid
            self.iid_to_name[iid]  = folder_name

        self._draw_empty_chart()


if __name__ == "__main__":
    app = FolderSizeApp()
    app.mainloop()
