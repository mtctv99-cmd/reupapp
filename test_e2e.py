import os
import time
import socket
import threading
import pytest
import uvicorn
from playwright.sync_api import sync_playwright, expect
from app import app

TEST_PORT = 15588
BASE_URL = f"http://127.0.0.1:{TEST_PORT}"

@pytest.fixture(scope="module", autouse=True)
def run_test_server():
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=TEST_PORT, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    
    # Wait for server to be responsive
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", TEST_PORT), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    yield
    server.should_exit = True

def test_ui_page_load_and_elements():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(BASE_URL)

        # Title check
        expect(page).to_have_title("Sever Dub Studio Pro")

        # Check key components
        expect(page.locator(".titlebar-title")).to_contain_text("SEVER DUB STUDIO PRO")
        expect(page.locator("#input-url")).to_be_visible()
        expect(page.locator("#btn-start")).to_be_visible()

        browser.close()

def test_ui_settings_modal():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(BASE_URL)

        # Open settings modal
        page.locator("#btn-open-settings").click()
        modal = page.locator("#modal-settings")
        expect(modal).to_be_visible()

        # Close settings modal
        page.locator("#btn-close-settings").click()
        expect(modal).not_to_be_visible()

        browser.close()

def test_ui_concat_modal():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(BASE_URL)

        # Open concat modal
        page.locator("#btn-open-concat").click()
        modal = page.locator("#modal-concat")
        expect(modal).to_be_visible()

        # Close concat modal
        page.locator("#btn-close-concat-modal").click()
        expect(modal).not_to_be_visible()

        browser.close()

def test_ui_empty_url_validation():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(BASE_URL)

        # Click Start without URL
        page.locator("#btn-start").click()
        
        # Check error modal shows up
        err_modal = page.locator("#modal-error")
        expect(err_modal).to_be_visible(timeout=3000)
        expect(page.locator("#modal-error-msg")).to_contain_text("Vui lòng dán link video")

        # Dismiss error modal
        page.locator("#btn-error-ok").click()
        expect(err_modal).not_to_be_visible()

        browser.close()
