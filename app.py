#!/usr/bin/env python3

import threading
import logging
import sqlite3
from datetime import datetime
from flask import Flask, render_template, jsonify

from config import DB_PATH, DAILY_LIMIT
from db import init_db, get_applied_jobs_count
from hitl_engine import HITLEngine
from linkedin_driver import LinkedInDriver
from naukri_driver import NaukriDriver

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

# Global HITL engine instance (shared with drivers)
hitl_engine = HITLEngine()
automation_running = False
automation_lock = threading.Lock()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status", methods=["GET"])
def get_status():
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        daily_count = get_applied_jobs_count(today)

        recent_apps = []
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, title, company, platform, match_score, status, applied_at "
                "FROM applied_jobs ORDER BY applied_at DESC LIMIT 10"
            )
            recent_apps = [dict(row) for row in cursor.fetchall()]
            conn.close()
        except Exception as db_error:
            logger.warning(f"Database query error: {db_error}")

        return jsonify(
            {
                "daily_count": daily_count,
                "daily_limit": DAILY_LIMIT,
                "is_paused": hitl_engine.is_paused(),
                "paused_job": hitl_engine.get_paused_job(),
                "recent_applications": recent_apps,
                "automation_running": automation_running,
            }
        )
    except Exception as e:
        logger.error(f"Error getting status: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/resume", methods=["POST"])
def resume():
    try:
        if not hitl_engine.is_paused():
            return jsonify({"status": "not_paused"}), 400

        hitl_engine.resume_execution()
        logger.info("HITL automation resumed via dashboard")
        return jsonify({"status": "resumed"})
    except Exception as e:
        logger.error(f"Error resuming: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/start", methods=["POST"])
def start_automation():
    global automation_running

    with automation_lock:
        if automation_running:
            return jsonify({"error": "Automation already running"}), 400

        automation_running = True

    def run_automation():
        global automation_running
        try:
            logger.info("Starting LinkedIn automation...")
            with LinkedInDriver(headless=True, hitl_engine=hitl_engine) as driver:
                result = driver.search_and_apply_jobs()
                logger.info(f"LinkedIn automation completed: {result}")

            logger.info("Starting Naukri automation...")
            with NaukriDriver(headless=True, hitl_engine=hitl_engine) as driver:
                result = driver.search_and_apply_jobs()
                logger.info(f"Naukri automation completed: {result}")

        except Exception as e:
            logger.error(f"Automation error: {e}", exc_info=True)
        finally:
            with automation_lock:
                automation_running = False

    thread = threading.Thread(target=run_automation, daemon=True)
    thread.start()

    return jsonify({"status": "automation started"})


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Internal server error"}), 500


if __name__ == "__main__":
    init_db()
    app.run(debug=False, port=5000, threaded=True)
