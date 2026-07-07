import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingTCPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from job_hunt.log import get_logger

logger = get_logger()

# Global state trackers for background threads
STATE_LOCK = threading.Lock()
SCAN_STATE = {
    "status": "idle",       # "idle", "running", "success", "error"
    "error_message": None,
}
DRAFT_STATE = {
    "status": "idle",       # "idle", "running", "success", "error"
    "error_message": None,
    "last_drafted_dir": None,
}

class ThreadingHTTPServer(ThreadingTCPServer):
    allow_reuse_address = True

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Redirect standard HTTP logging to logger.debug to keep stdout clean
        logger.debug(f"HTTP Server: {format % args}")

    def _send_json(self, status_code, data):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def do_OPTIONS(self):
        # Handle CORS preflight
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        query = parse_qs(parsed_url.query)

        # Serve static SPA dashboard
        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode("utf-8"))
            return

        # Serve jobs list from state files
        if path == "/api/jobs":
            history_file = Path("state/job_history.json")
            last_scan_file = Path("state/last_scan.json")
            jobs = []
            
            if history_file.exists():
                try:
                    jobs = json.loads(history_file.read_text(encoding="utf-8"))
                except Exception as e:
                    logger.error(f"Failed to load job history: {e}")
            elif last_scan_file.exists():
                try:
                    jobs = json.loads(last_scan_file.read_text(encoding="utf-8"))
                except Exception as e:
                    logger.error(f"Failed to load last scan: {e}")

            self._send_json(200, {"jobs": jobs})
            return

        # Serve scan/draft status
        if path == "/api/status":
            with STATE_LOCK:
                self._send_json(200, {
                    "scan": SCAN_STATE,
                    "draft": DRAFT_STATE
                })
            return

        # List all generated files inside output/
        if path == "/api/drafts":
            output_dir = Path("output")
            drafts = []
            if output_dir.exists():
                for folder in sorted(output_dir.iterdir(), reverse=True):
                    if folder.is_dir():
                        files = []
                        for f in folder.iterdir():
                            if f.is_file():
                                files.append({
                                    "name": f.name,
                                    "rel_path": str(f.relative_to(output_dir)).replace("\\", "/")
                                })
                        drafts.append({
                            "folder": folder.name,
                            "files": files
                        })
            self._send_json(200, {"drafts": drafts})
            return

        # Read and serve file content safely (directory traversal protection)
        if path == "/api/drafts/view":
            file_param = query.get("file", [""])[0]
            if not file_param:
                self._send_json(400, {"error": "Missing 'file' parameter"})
                return

            output_dir = Path("output").resolve()
            target_path = (output_dir / file_param).resolve()

            if not target_path.is_relative_to(output_dir) or not target_path.exists() or not target_path.is_file():
                self._send_json(403, {"error": "Forbidden or file not found"})
                return

            try:
                content = target_path.read_text(encoding="utf-8")
                self._send_json(200, {"content": content, "filename": target_path.name})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return

        # 404 fallback
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length else ""

        # Trigger job scanner in a background thread
        if path == "/api/scan":
            with STATE_LOCK:
                if SCAN_STATE["status"] == "running":
                    self._send_json(400, {"error": "Scan is already running"})
                    return
                SCAN_STATE["status"] = "running"
                SCAN_STATE["error_message"] = None

            def run_scan_job():
                try:
                    logger.info("Background Job: Starting daily job scan...")
                    from job_hunt.main import load_config, load_companies
                    from job_hunt.scanner import run_scan
                    config = load_config()
                    run_scan(config, load_companies())
                    with STATE_LOCK:
                        SCAN_STATE["status"] = "success"
                    logger.info("Background Job: Job scan completed successfully.")
                except Exception as e:
                    logger.error(f"Background Job: Job scan failed: {e}")
                    with STATE_LOCK:
                        SCAN_STATE["status"] = "error"
                        SCAN_STATE["error_message"] = str(e)

            threading.Thread(target=run_scan_job, daemon=True).start()
            self._send_json(200, {"status": "started"})
            return

        # Trigger AI resume & cover letter drafting in a background thread
        if path == "/api/draft":
            try:
                req_data = json.loads(body)
                job_ref = req_data.get("job_url") or req_data.get("job_ref")
            except Exception:
                self._send_json(400, {"error": "Invalid JSON body"})
                return

            if not job_ref:
                self._send_json(400, {"error": "Missing 'job_url' or 'job_ref'"})
                return

            with STATE_LOCK:
                if DRAFT_STATE["status"] == "running":
                    self._send_json(400, {"error": "Draft tailoring is already in progress"})
                    return
                DRAFT_STATE["status"] = "running"
                DRAFT_STATE["error_message"] = None
                DRAFT_STATE["last_drafted_dir"] = None

            def run_draft_job():
                try:
                    logger.info(f"Background Job: Tailoring application for '{job_ref}'...")
                    from job_hunt.main import load_config
                    from job_hunt.drafter import draft_application
                    config = load_config()
                    out_dir = draft_application(config, job_ref)
                    with STATE_LOCK:
                        DRAFT_STATE["status"] = "success"
                        DRAFT_STATE["last_drafted_dir"] = out_dir.name if out_dir else None
                    logger.info("Background Job: Application tailoring complete.")
                except Exception as e:
                    logger.error(f"Background Job: Application tailoring failed: {e}")
                    with STATE_LOCK:
                        DRAFT_STATE["status"] = "error"
                        DRAFT_STATE["error_message"] = str(e)

            threading.Thread(target=run_draft_job, daemon=True).start()
            self._send_json(200, {"status": "started"})
            return

        self.send_response(404)
        self.end_headers()

def start_server(port=8000):
    server_address = ("", port)
    httpd = ThreadingHTTPServer(server_address, DashboardHandler)
    logger.info(f"Dashboard server started at http://localhost:{port}/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down dashboard server...")
        httpd.server_close()


# Embedded beautiful responsive glassmorphism client application
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Autopilot Job Hunting Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0b0f19;
            --card-bg: rgba(25, 35, 58, 0.4);
            --card-border: rgba(255, 255, 255, 0.08);
            --text-main: #f3f4f6;
            --text-muted: #9ca3af;
            --accent-primary: #6366f1;
            --accent-primary-hover: #4f46e5;
            --score-high: #10b981;
            --score-med: #f59e0b;
            --score-low: #ef4444;
            --shadow-primary: 0 8px 32px 0 rgba(0, 0, 0, 0.37);
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: 'Inter', sans-serif;
            background-color: var(--bg-color);
            background-image: 
                radial-gradient(at 0% 0%, rgba(99, 102, 241, 0.15) 0px, transparent 50%),
                radial-gradient(at 100% 100%, rgba(168, 85, 247, 0.1) 0px, transparent 50%);
            background-attachment: fixed;
            color: var(--text-main);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            overflow-x: hidden;
        }

        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 1.5rem 2rem;
            backdrop-filter: blur(12px);
            background: rgba(11, 15, 25, 0.6);
            border-bottom: 1px solid var(--card-border);
            position: sticky;
            top: 0;
            z-index: 100;
        }

        .logo-section h1 {
            font-size: 1.5rem;
            font-weight: 700;
            letter-spacing: -0.025em;
            background: linear-gradient(to right, #6366f1, #a855f7);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .logo-section p {
            font-size: 0.75rem;
            color: var(--text-muted);
            margin-top: 0.2rem;
        }

        .actions-bar {
            display: flex;
            gap: 1rem;
            align-items: center;
        }

        button {
            background-color: var(--accent-primary);
            color: white;
            border: none;
            padding: 0.6rem 1.2rem;
            border-radius: 8px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s ease;
            box-shadow: 0 4px 12px rgba(99, 102, 241, 0.2);
            font-size: 0.875rem;
        }

        button:hover:not(:disabled) {
            background-color: var(--accent-primary-hover);
            transform: translateY(-1px);
        }

        button:disabled {
            background-color: #374151;
            color: #9ca3af;
            cursor: not-allowed;
            box-shadow: none;
        }

        button.btn-secondary {
            background-color: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--card-border);
            color: var(--text-main);
            box-shadow: none;
        }

        button.btn-secondary:hover:not(:disabled) {
            background-color: rgba(255, 255, 255, 0.1);
        }

        .status-pill {
            display: inline-flex;
            align-items: center;
            gap: 0.5rem;
            font-size: 0.75rem;
            background-color: rgba(255, 255, 255, 0.05);
            padding: 0.4rem 0.8rem;
            border-radius: 20px;
            border: 1px solid var(--card-border);
        }

        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background-color: var(--text-muted);
        }

        .status-dot.active {
            background-color: #10b981;
            box-shadow: 0 0 8px #10b981;
            animation: pulse 2s infinite;
        }

        @keyframes pulse {
            0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
            70% { transform: scale(1); box-shadow: 0 0 0 6px rgba(16, 185, 129, 0); }
            100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
        }

        main {
            flex: 1;
            padding: 2rem;
            display: grid;
            grid-template-columns: 320px 1fr;
            gap: 2rem;
            max-width: 1600px;
            margin: 0 auto;
            width: 100%;
        }

        .sidebar {
            display: flex;
            flex-direction: column;
            gap: 1.5rem;
        }

        .card {
            background: var(--card-bg);
            backdrop-filter: blur(16px);
            border: 1px solid var(--card-border);
            border-radius: 12px;
            padding: 1.5rem;
            box-shadow: var(--shadow-primary);
        }

        .card h2 {
            font-size: 1.1rem;
            font-weight: 600;
            margin-bottom: 1rem;
            border-bottom: 1px solid var(--card-border);
            padding-bottom: 0.5rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .filters {
            display: flex;
            flex-direction: column;
            gap: 1rem;
        }

        .form-group {
            display: flex;
            flex-direction: column;
            gap: 0.4rem;
        }

        .form-group label {
            font-size: 0.75rem;
            color: var(--text-muted);
            font-weight: 500;
        }

        .form-group input, .form-group select {
            background-color: rgba(0, 0, 0, 0.2);
            border: 1px solid var(--card-border);
            color: var(--text-main);
            padding: 0.5rem 0.8rem;
            border-radius: 6px;
            font-size: 0.875rem;
            outline: none;
            transition: border-color 0.2s;
        }

        .form-group input:focus, .form-group select:focus {
            border-color: var(--accent-primary);
        }

        .drafts-list {
            display: flex;
            flex-direction: column;
            gap: 1rem;
            max-height: 400px;
            overflow-y: auto;
        }

        .draft-folder {
            border-bottom: 1px solid rgba(255, 255, 255, 0.03);
            padding-bottom: 0.5rem;
        }

        .draft-folder-name {
            font-size: 0.8rem;
            font-weight: 600;
            color: var(--accent-primary);
            margin-bottom: 0.3rem;
            cursor: pointer;
        }

        .draft-files {
            padding-left: 0.5rem;
            display: flex;
            flex-direction: column;
            gap: 0.25rem;
        }

        .draft-file {
            font-size: 0.75rem;
            color: var(--text-muted);
            cursor: pointer;
            padding: 0.2rem 0.4rem;
            border-radius: 4px;
            transition: all 0.2s;
            display: flex;
            justify-content: space-between;
        }

        .draft-file:hover {
            background-color: rgba(255, 255, 255, 0.05);
            color: var(--text-main);
        }

        .dashboard-content {
            display: flex;
            flex-direction: column;
            gap: 2rem;
        }

        .job-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
            gap: 1.5rem;
        }

        .job-card {
            background: var(--card-bg);
            backdrop-filter: blur(16px);
            border: 1px solid var(--card-border);
            border-radius: 12px;
            padding: 1.5rem;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            min-height: 240px;
            transition: transform 0.2s, border-color 0.2s;
            cursor: pointer;
            position: relative;
        }

        .job-card:hover {
            transform: translateY(-2px);
            border-color: rgba(99, 102, 241, 0.4);
            box-shadow: 0 10px 20px rgba(0,0,0,0.3);
        }

        .job-card.selected {
            border-color: var(--accent-primary);
            box-shadow: 0 0 15px rgba(99, 102, 241, 0.3);
        }

        .job-card-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 0.8rem;
        }

        .job-title {
            font-size: 1rem;
            font-weight: 600;
            line-height: 1.4;
            max-width: 80%;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        .job-score-badge {
            font-weight: 700;
            font-size: 0.85rem;
            padding: 0.25rem 0.5rem;
            border-radius: 6px;
            color: white;
            text-shadow: 0 1px 2px rgba(0,0,0,0.5);
        }

        .job-company {
            font-size: 0.85rem;
            font-weight: 500;
            color: var(--accent-primary);
            margin-bottom: 0.5rem;
        }

        .job-meta-row {
            display: flex;
            gap: 1rem;
            font-size: 0.75rem;
            color: var(--text-muted);
            margin-bottom: 0.8rem;
        }

        .job-reason {
            font-size: 0.8rem;
            color: var(--text-muted);
            line-height: 1.5;
            flex-grow: 1;
            margin-bottom: 1rem;
        }

        .job-tags {
            display: flex;
            flex-wrap: wrap;
            gap: 0.4rem;
            margin-bottom: 1rem;
        }

        .job-tag {
            font-size: 0.65rem;
            background-color: rgba(255, 255, 255, 0.04);
            border: 1px solid var(--card-border);
            padding: 0.2rem 0.5rem;
            border-radius: 4px;
            color: var(--text-muted);
        }

        .detail-view {
            display: grid;
            grid-template-columns: 2fr 1.2fr;
            gap: 2rem;
        }

        .detail-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 1.5rem;
        }

        .detail-header h3 {
            font-size: 1.3rem;
            font-weight: 700;
            margin-bottom: 0.4rem;
        }

        .detail-body {
            line-height: 1.6;
            font-size: 0.9rem;
        }

        .detail-body h4 {
            margin: 1.5rem 0 0.5rem 0;
            font-size: 1rem;
            font-weight: 600;
            color: var(--accent-primary);
        }

        .detail-body p {
            margin-bottom: 1rem;
            color: #d1d5db;
        }

        .detail-body pre {
            background-color: rgba(0, 0, 0, 0.3);
            padding: 1rem;
            border-radius: 8px;
            overflow-x: auto;
            font-family: monospace;
            border: 1px solid var(--card-border);
            margin-bottom: 1.5rem;
            white-space: pre-wrap;
            font-size: 0.8rem;
        }

        /* File Viewer Modal / Section */
        .file-viewer-container {
            grid-column: span 2;
            margin-top: 1rem;
        }

        .file-viewer-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1rem;
        }

        .file-viewer-content {
            background-color: rgba(0, 0, 0, 0.3);
            border: 1px solid var(--card-border);
            border-radius: 8px;
            padding: 1.5rem;
            max-height: 600px;
            overflow-y: auto;
            font-family: monospace;
            font-size: 0.85rem;
            white-space: pre-wrap;
            line-height: 1.5;
            color: #e5e7eb;
        }

        .empty-state {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 4rem 2rem;
            color: var(--text-muted);
            text-align: center;
            grid-column: span 3;
        }

        .empty-state svg {
            width: 48px;
            height: 48px;
            stroke: rgba(255,255,255,0.1);
            margin-bottom: 1rem;
        }

        /* Custom Scrollbar */
        ::-webkit-scrollbar {
            width: 8px;
            height: 8px;
        }
        ::-webkit-scrollbar-track {
            background: rgba(0,0,0,0.1);
        }
        ::-webkit-scrollbar-thumb {
            background: rgba(255,255,255,0.1);
            border-radius: 4px;
        }
        ::-webkit-scrollbar-thumb:hover {
            background: rgba(255,255,255,0.2);
        }

        /* Notification Toast */
        .toast {
            position: fixed;
            bottom: 2rem;
            right: 2rem;
            background: rgba(17, 24, 39, 0.9);
            border: 1px solid var(--accent-primary);
            border-radius: 8px;
            padding: 1rem 1.5rem;
            box-shadow: 0 10px 25px rgba(0,0,0,0.5);
            z-index: 1000;
            display: flex;
            align-items: center;
            gap: 0.8rem;
            transform: translateY(150%);
            transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1);
            backdrop-filter: blur(10px);
        }

        .toast.show {
            transform: translateY(0);
        }
    </style>
</head>
<body>

    <header>
        <div class="logo-section">
            <h1>Autopilot Job Hunt</h1>
            <p>Scanner, Scorer & AI Application Tailor Dashboard</p>
        </div>
        <div class="actions-bar">
            <div class="status-pill">
                <div id="scan-dot" class="status-dot"></div>
                <span id="scan-status-text">Scanner Idle</span>
            </div>
            <button id="btn-scan" onclick="triggerScan()">Run Scans Now</button>
        </div>
    </header>

    <main>
        <div class="sidebar">
            <div class="card">
                <h2>Filters</h2>
                <div class="filters">
                    <div class="form-group">
                        <label for="search-input">Search Company / Role</label>
                        <input type="text" id="search-input" placeholder="Type to filter..." oninput="applyFilters()">
                    </div>
                    <div class="form-group">
                        <label for="score-filter">Min Match Score</label>
                        <select id="score-filter" onchange="applyFilters()">
                            <option value="0">All Scores</option>
                            <option value="80">Perfect Fit (80+)</option>
                            <option value="60">Good Fit (60+)</option>
                            <option value="40">Partial Fit (40+)</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label for="sort-select">Sort By</label>
                        <select id="sort-select" onchange="applyFilters()">
                            <option value="score-desc">Score (Highest First)</option>
                            <option value="date-desc">Scan Date (Newest First)</option>
                            <option value="score-asc">Score (Lowest First)</option>
                        </select>
                    </div>
                </div>
            </div>

            <div class="card">
                <h2>AI Generated Drafts</h2>
                <div class="drafts-list" id="drafts-container">
                    <div style="font-size: 0.8rem; color: var(--text-muted);">No drafts found. Tailor an application to create one.</div>
                </div>
            </div>
        </div>

        <div class="dashboard-content">
            <div class="card" style="display: none;" id="detail-card">
                <h2>Job Application Details</h2>
                <div class="detail-view">
                    <div>
                        <div class="detail-header">
                            <div>
                                <h3 id="det-title">Job Title</h3>
                                <div style="color: var(--accent-primary); font-weight: 500; margin-bottom: 0.5rem;" id="det-company">Company</div>
                                <div class="job-meta-row">
                                    <span id="det-location">Location</span>
                                    <span id="det-date">Scan Date</span>
                                </div>
                            </div>
                            <div class="job-score-badge" id="det-score-badge" style="background-color: var(--accent-primary); font-size: 1.1rem; padding: 0.4rem 0.8rem;">95% Match</div>
                        </div>
                        <div class="detail-body">
                            <h4>LLM Fit Analysis</h4>
                            <p id="det-reason">Reasoning goes here.</p>
                            
                            <h4>Emphasized Tech Stack</h4>
                            <div class="job-tags" id="det-tags" style="margin-top: 0.5rem;"></div>

                            <h4>Job Posting Description</h4>
                            <pre id="det-description">Full job description text will load here.</pre>
                        </div>
                    </div>

                    <div style="display: flex; flex-direction: column; gap: 1.5rem;">
                        <div class="card" style="background: rgba(0,0,0,0.15); border: 1px solid rgba(255,255,255,0.05); padding: 1.2rem;">
                            <h3 style="font-size: 1rem; font-weight: 600; margin-bottom: 0.8rem;">Tailor Application</h3>
                            <p style="font-size: 0.8rem; color: var(--text-muted); margin-bottom: 1rem; line-height: 1.4;">
                                Run the AI drafter for this job to generate a tailored markdown resume, cover letter, and application contact details based on your CV profile.
                            </p>
                            <div class="form-group" style="margin-bottom: 1rem;">
                                <label>Outbound LLM Service</label>
                                <div style="font-size: 0.85rem; font-weight: 500; color: var(--score-high); margin-top: 0.2rem;">
                                    Gemini-1.5-Flash (with FreeLLMAPI fallback)
                                </div>
                            </div>
                            <button id="btn-tailor" style="width: 100%;" onclick="triggerTailoring()">Tailor Resume + Cover Letter</button>
                            <div id="tailor-status-box" style="margin-top: 0.8rem; display: none;">
                                <div class="status-pill" style="width: 100%; display: flex; justify-content: center;">
                                    <div id="tailor-dot" class="status-dot"></div>
                                    <span id="tailor-status-text">Drafting...</span>
                                </div>
                            </div>
                        </div>

                        <div class="card" style="background: rgba(0,0,0,0.15); border: 1px solid rgba(255,255,255,0.05); padding: 1.2rem;">
                            <h3 style="font-size: 1rem; font-weight: 600; margin-bottom: 0.8rem;">External Links</h3>
                            <a id="det-link" href="#" target="_blank" style="display: flex; align-items: center; justify-content: center; background: rgba(99, 102, 241, 0.1); border: 1px solid rgba(99,102,241,0.3); color: var(--text-main); text-decoration: none; padding: 0.6rem; border-radius: 8px; font-weight: 500; font-size: 0.875rem; transition: background 0.2s;">
                                Open Original Job Posting
                            </a>
                        </div>
                    </div>

                    <!-- File Viewer Section -->
                    <div class="file-viewer-container" id="file-viewer-card" style="display: none;">
                        <div class="file-viewer-header">
                            <h3 id="viewer-filename">File Viewer</h3>
                            <button class="btn-secondary" onclick="closeFileViewer()">Close Preview</button>
                        </div>
                        <pre class="file-viewer-content" id="viewer-content">Content</pre>
                    </div>
                </div>
            </div>

            <div class="job-grid" id="jobs-container">
                <!-- Loaded dynamically -->
                <div class="empty-state">
                    <svg fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
                    </svg>
                    <p>Loading jobs list...</p>
                </div>
            </div>
        </div>
    </main>

    <div class="toast" id="toast-notify">
        <span id="toast-text">Task completed successfully.</span>
    </div>

    <script>
        let allJobs = [];
        let filteredJobs = [];
        let selectedJob = null;
        let isPolling = false;

        document.addEventListener("DOMContentLoaded", () => {
            fetchJobs();
            fetchDrafts();
            startStatusPolling();
        });

        async function fetchJobs() {
            try {
                const res = await fetch("/api/jobs");
                const data = await res.json();
                allJobs = data.jobs || [];
                applyFilters();
            } catch (err) {
                console.error("Failed to load jobs list", err);
                showToast("Failed to connect to dashboard API.");
            }
        }

        async function fetchDrafts() {
            try {
                const res = await fetch("/api/drafts");
                const data = await res.json();
                renderDrafts(data.drafts || []);
            } catch (err) {
                console.error("Failed to load drafts list", err);
            }
        }

        document.getElementById("search-input").addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                applyFilters();
            }
        });

        function renderDrafts(drafts) {
            const container = document.getElementById("drafts-container");
            if (drafts.length === 0) {
                container.innerHTML = `<div style="font-size: 0.8rem; color: var(--text-muted);">No drafts found. Tailor an application to create one.</div>`;
                return;
            }

            container.innerHTML = "";
            drafts.forEach(d => {
                const folderDiv = document.createElement("div");
                folderDiv.className = "draft-folder";

                const folderTitle = document.createElement("div");
                folderTitle.className = "draft-folder-name";
                folderTitle.innerText = d.folder;

                const filesDiv = document.createElement("div");
                filesDiv.className = "draft-files";

                d.files.forEach(f => {
                    const fileLink = document.createElement("div");
                    fileLink.className = "draft-file";
                    fileLink.innerText = f.name;
                    fileLink.onclick = () => viewDraftFile(f.rel_path);
                    filesDiv.appendChild(fileLink);
                });

                folderDiv.appendChild(folderTitle);
                folderDiv.appendChild(filesDiv);
                container.appendChild(folderDiv);
            });
        }

        function applyFilters() {
            const searchVal = document.getElementById("search-input").value.toLowerCase();
            const scoreVal = parseInt(document.getElementById("score-filter").value);
            const sortVal = document.getElementById("sort-select").value;

            filteredJobs = allJobs.filter(j => {
                const matchQuery = (j.company || '').toLowerCase().includes(searchVal) || 
                                   (j.title || '').toLowerCase().includes(searchVal) || 
                                   (j.extracted_title || '').toLowerCase().includes(searchVal);
                const matchScore = (j.score || 0) >= scoreVal;
                return matchQuery && matchScore;
            });

            // Sort
            if (sortVal === "score-desc") {
                filteredJobs.sort((a, b) => (b.score || 0) - (a.score || 0));
            } else if (sortVal === "score-asc") {
                filteredJobs.sort((a, b) => (a.score || 0) - (b.score || 0));
            } else if (sortVal === "date-desc") {
                filteredJobs.sort((a, b) => (b.scan_date || '').localeCompare(a.scan_date || ''));
            }

            renderJobs();
        }

        function renderJobs() {
            const container = document.getElementById("jobs-container");
            if (filteredJobs.length === 0) {
                container.innerHTML = `
                    <div class="empty-state">
                        <svg fill="none" viewBox="0 0 24 24" stroke="currentColor">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.172 16.172a4 4 0 015.656 0M9 10h.01M15 10h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                        </svg>
                        <p>No matching jobs found.</p>
                    </div>`;
                return;
            }

            container.innerHTML = "";
            filteredJobs.forEach(j => {
                const card = document.createElement("div");
                card.className = "job-card";
                if (selectedJob && selectedJob.url === j.url) {
                    card.classList.add("selected");
                }
                card.onclick = () => selectJob(j);

                const header = document.createElement("div");
                header.className = "job-card-header";

                const title = document.createElement("div");
                title.className = "job-title";
                title.innerText = j.extracted_title || j.title || "No Title";
                title.title = j.extracted_title || j.title || "";

                const score = document.createElement("div");
                score.className = "job-score-badge";
                score.innerText = `${j.score || 0}%`;
                score.style.backgroundColor = getScoreColor(j.score || 0);

                header.appendChild(title);
                header.appendChild(score);

                const company = document.createElement("div");
                company.className = "job-company";
                company.innerText = j.company || "Unknown Company";

                const meta = document.createElement("div");
                meta.className = "job-meta-row";
                meta.innerHTML = `<span>${j.location_remote || j.location || 'Unknown'}</span><span>${j.scan_date || ''}</span>`;

                const reason = document.createElement("div");
                reason.className = "job-reason";
                reason.innerText = j.reason || "No summary reason provided.";

                const tags = document.createElement("div");
                tags.className = "job-tags";
                const techList = j.stack ? j.stack.split(',') : [];
                techList.slice(0, 4).forEach(t => {
                    const tag = document.createElement("span");
                    tag.className = "job-tag";
                    tag.innerText = t.trim();
                    tags.appendChild(tag);
                });

                card.appendChild(header);
                card.appendChild(company);
                card.appendChild(meta);
                card.appendChild(reason);
                card.appendChild(tags);

                container.appendChild(card);
            });
        }

        function getScoreColor(score) {
            if (score >= 80) return "var(--score-high)";
            if (score >= 60) return "var(--score-med)";
            return "var(--score-low)";
        }

        function selectJob(job) {
            selectedJob = job;
            document.getElementById("detail-card").style.display = "block";
            
            document.getElementById("det-title").innerText = job.extracted_title || job.title;
            document.getElementById("det-company").innerText = job.company;
            document.getElementById("det-location").innerText = job.location_remote || job.location || "Unknown";
            document.getElementById("det-date").innerText = `Scanned: ${job.scan_date || "Unknown"}`;
            
            const badge = document.getElementById("det-score-badge");
            badge.innerText = `${job.score || 0}% Match`;
            badge.style.backgroundColor = getScoreColor(job.score || 0);

            document.getElementById("det-reason").innerText = job.reason || "No explanation provided.";
            
            // Link
            document.getElementById("det-link").href = job.url;

            // Tags
            const tagsBox = document.getElementById("det-tags");
            tagsBox.innerHTML = "";
            const techList = job.stack ? job.stack.split(',') : [];
            techList.forEach(t => {
                const tag = document.createElement("span");
                tag.className = "job-tag";
                tag.style.fontSize = "0.75rem";
                tag.innerText = t.trim();
                tagsBox.appendChild(tag);
            });

            // Content Description
            document.getElementById("det-description").innerText = job.content || "No job posting text stored in scan history.";

            // Close active file preview on switching jobs
            closeFileViewer();

            // Highlight selected card in list
            const cards = document.getElementsByClassName("job-card");
            for (let i = 0; i < cards.length; i++) {
                cards[i].classList.remove("selected");
            }
            applyFilters(); // Re-renders to highlight selected card
        }

        async function triggerScan() {
            const btn = document.getElementById("btn-scan");
            btn.disabled = true;
            try {
                const res = await fetch("/api/scan", { method: "POST" });
                if (res.ok) {
                    showToast("Job scan started in background...");
                    updateScanUI("running");
                } else {
                    const data = await res.json();
                    showToast("Error: " + (data.error || "Failed to start scan"));
                    btn.disabled = false;
                }
            } catch (err) {
                showToast("Connection error.");
                btn.disabled = false;
            }
        }

        async function triggerTailoring() {
            if (!selectedJob) return;
            const btn = document.getElementById("btn-tailor");
            btn.disabled = true;
            document.getElementById("tailor-status-box").style.display = "block";
            updateDraftUI("running");

            try {
                const res = await fetch("/api/draft", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ job_url: selectedJob.url })
                });
                
                if (res.ok) {
                    showToast("Tailoring application in progress...");
                } else {
                    const data = await res.json();
                    showToast("Error: " + (data.error || "Tailoring failed"));
                    btn.disabled = false;
                    document.getElementById("tailor-status-box").style.display = "none";
                }
            } catch (err) {
                showToast("Failed to invoke tailoring service.");
                btn.disabled = false;
                document.getElementById("tailor-status-box").style.display = "none";
            }
        }

        function startStatusPolling() {
            if (isPolling) return;
            isPolling = true;
            
            setInterval(async () => {
                try {
                    const res = await fetch("/api/status");
                    const data = await res.json();
                    
                    updateScanUI(data.scan.status, data.scan.error_message);
                    updateDraftUI(data.draft.status, data.draft.error_message);
                } catch (err) {
                    console.error("Status polling failed", err);
                }
            }, 2000);
        }

        function updateScanUI(status, errorMsg) {
            const dot = document.getElementById("scan-dot");
            const text = document.getElementById("scan-status-text");
            const btn = document.getElementById("btn-scan");

            if (status === "running") {
                dot.className = "status-dot active";
                text.innerText = "Scanning Careers Pages...";
                btn.disabled = true;
            } else {
                dot.className = "status-dot";
                text.innerText = "Scanner Idle";
                btn.disabled = false;
                if (status === "success") {
                    fetchJobs(); // reload jobs
                    showToast("Scan finished successfully!");
                } else if (status === "error") {
                    showToast("Scan error: " + errorMsg);
                }
            }
        }

        function updateDraftUI(status, errorMsg) {
            const dot = document.getElementById("tailor-dot");
            const text = document.getElementById("tailor-status-text");
            const btn = document.getElementById("btn-tailor");
            const box = document.getElementById("tailor-status-box");

            if (status === "running") {
                dot.className = "status-dot active";
                text.innerText = "AI Tailoring in Progress...";
                btn.disabled = true;
                box.style.display = "block";
            } else {
                dot.className = "status-dot";
                btn.disabled = false;
                box.style.display = "none";
                
                if (status === "success") {
                    showToast("Application tailored successfully!");
                    fetchDrafts(); // reload list
                } else if (status === "error") {
                    showToast("Tailoring failed: " + errorMsg);
                }
            }
        }

        async function viewDraftFile(relPath) {
            try {
                const res = await fetch(`/api/drafts/view?file=${encodeURIComponent(relPath)}`);
                const data = await res.json();
                if (res.ok) {
                    document.getElementById("file-viewer-card").style.display = "block";
                    document.getElementById("viewer-filename").innerText = data.filename;
                    document.getElementById("viewer-content").innerText = data.content;
                    document.getElementById("file-viewer-card").scrollIntoView({ behavior: "smooth" });
                } else {
                    showToast("Error loading file: " + data.error);
                }
            } catch (err) {
                showToast("Connection error loading file preview.");
            }
        }

        function closeFileViewer() {
            document.getElementById("file-viewer-card").style.display = "none";
        }

        function showToast(message) {
            const toast = document.getElementById("toast-notify");
            document.getElementById("toast-text").innerText = message;
            toast.classList.add("show");
            setTimeout(() => {
                toast.classList.remove("show");
            }, 4000);
        }
    </script>
</body>
</html>
"""
