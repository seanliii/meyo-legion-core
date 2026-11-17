"""个人设备真实浏览器入口及 HTTP roundtrip；不启动设备或访问远控服务。"""
import json
import os
from pathlib import Path
import threading
from http.server import ThreadingHTTPServer

import pytest

SERVICE = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _pin_isolation_env(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SERVICE))
    for key, name in (("MEYO_LEGION_DIR", "legion"), ("LEGION_HOME_DIR", "home"),
                      ("LEGION_SESSIONS_DIR", "sessions"), ("LEGION_TEAM_DIR", "teams"),
                      ("LEGION_PROJECT_CONTEXT_DIR", "projects"), ("LEGION_TASK_MEMORY_DIR", "memory")):
        monkeypatch.setenv(key, str(tmp_path / name))
    for key in ("MEYO_LEGION_UID", "MEYO_USER_ID", "LEGION_BILLD_SELF_HOSTED_ORIGIN", "LEGION_PUBLIC_ORIGIN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LEGION_NO_PREWARM", "1")
    monkeypatch.setenv("LEGION_EXT_AUTO_UPDATE", "0")


@pytest.fixture
def personal_browser(tmp_path, monkeypatch):
    playwright_api = pytest.importorskip("playwright.sync_api")
    import cloud_desktop
    import server

    tokens = {"alice": "__ut_device_alice_token_0123456789", "bob": "__ut_device_bob_token_0123456789"}
    token_file = tmp_path / "personal-tokens.json"
    token_file.write_text(json.dumps({token: {"user": user} for user, token in tokens.items()}))
    token_file.chmod(0o600)
    monkeypatch.setenv("LEGION_PERSONAL_TOKENS_FILE", str(token_file))
    monkeypatch.setattr(server, "_REMOTE_BIND_OK", False)
    monkeypatch.setattr(server, "_LEGACY_NO_ORIGIN", False)
    monkeypatch.setattr(server, "_PUBLIC_ORIGIN", None, raising=False)
    monkeypatch.setattr(cloud_desktop, "_docker_env", lambda: pytest.fail("Personal devices must not probe Docker"))

    class QuietHandler(server.Handler):
        def log_message(self, *_args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
    httpd.api_token = "__ut_instance_not_personal"
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    evidence = Path(os.environ.get("MEYO_DESKTOP_TEST_EVIDENCE", str(tmp_path / "screenshots")))
    evidence.mkdir(parents=True, exist_ok=True)
    try:
        with playwright_api.sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except playwright_api.Error:
                pytest.skip("Chromium unavailable; no browser installation attempted")
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            context.route("https://desk.hsslive.cn/**", lambda route: route.abort())
            try:
                yield context, base, tokens, evidence, cloud_desktop, server
            finally:
                browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def _authenticate(page, user, tokens):
    from playwright.sync_api import expect
    if page.locator("#mobileSessionToggle").is_visible():
        page.locator("#mobileSessionToggle").click()
    page.locator("#whoAmI").click()
    if not page.locator("#personalToken").is_visible():
        page.locator("#personalAuthDialog details").filter(has=page.locator("#personalAuthForm")).locator("summary").click()
    page.locator("#personalToken").fill(tokens[user])
    page.locator("#personalAuthSubmit").click()
    expect(page.locator("#personalAuthDialog")).not_to_be_visible()
    expect(page.locator("#whoAmI")).to_contain_text(user)
    assert page.locator("#personalToken").input_value() == ""
    if page.locator("#mobileSessionToggle").is_visible() and page.locator("#mobileBackdrop").is_visible():
        page.locator("#mobileBackdrop").click()


def _open_devices(page):
    from playwright.sync_api import expect
    if page.locator("#mobileSessionToggle").is_visible():
        page.locator("#mobileSessionToggle").click()
    page.locator("#openPersonalDevicesHub").click()
    expect(page.locator("#advPanelTitle")).to_have_text("接入个人设备")


def test_personal_devices_real_ui_entry(personal_browser):
    from playwright.sync_api import expect
    context, base, tokens, evidence, _desktop, _server = personal_browser
    page = context.new_page()
    page.goto(base, wait_until="domcontentloaded")
    _authenticate(page, "alice", tokens)
    _open_devices(page)
    expect(page.locator(".pd-workspace")).to_be_visible()
    expect(page.locator(".pd-boundary")).to_contain_text("军团自动操作尚未接通")
    assert page.locator(".pd-workspace input[type=password]").count() == 0
    expect(page.locator("#pdRefresh")).to_be_enabled()
    page.screenshot(path=evidence / "personal-devices-entry-desktop.png")
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator(".pd-boundary").scroll_into_view_if_needed()
    page.screenshot(path=evidence / "personal-devices-entry-mobile.png")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_personal_devices_real_ui_roundtrip(personal_browser, monkeypatch):
    from playwright.sync_api import expect
    context, base, tokens, evidence, cloud_desktop, server = personal_browser
    page = context.new_page()
    page.goto(base, wait_until="domcontentloaded")
    _authenticate(page, "alice", tokens)
    _open_devices(page)
    expect(page.locator("#pdSave")).to_be_enabled()
    page.locator("#pdName").fill("验收工作电脑")
    page.locator("#pdCode").fill("invalid/code")
    page.locator("#pdPlatform").select_option("macos")
    page.locator("#pdAuthorized").check()
    with page.expect_response(lambda response: "/personal_devices/register" in response.url) as response:
        page.locator("#pdSave").click()
    assert response.value.status == 400
    expect(page.locator("#pdFormError")).to_contain_text("设备码")
    page.locator("#pdCode").fill("__ut_fixture_device123")
    with page.expect_response(lambda response: "/personal_devices/register" in response.url) as response:
        page.locator("#pdSave").click()
    assert response.value.status == 201
    expect(page.locator(".pd-device h3")).to_have_text("验收工作电脑")
    assert page.locator(".pd-workspace input[type=password]").count() == 0
    registered = cloud_desktop.personal_devices("alice")["devices"][0]
    # Use the original transport: the page fetch wrapper otherwise pins the
    # verified subject and would hide a forged-owner request from the server.
    denied = page.evaluate("""async deviceId => {
      const endpoint = '/api/legion/cloud_desktop/personal_devices';
      const request = async (path, body) => {
        const response = await _personalFetch(endpoint + path, body === undefined ? {} : {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
        });
        return {status: response.status, body: await response.json()};
      };
      return {
        ownerList: await request('?uid=bob'),
        ownerRegister: await request('/register', {uid:'bob', name:'fixture', device_code:'__ut_wrong_owner', authorized:true}),
        ownerRemove: await request('/remove', {uid:'bob', device_id:deviceId}),
        password: await request('/register', {name:'fixture', device_code:'__ut_password_rejected', authorized:true, password:'__ut_fake_not_a_real_password'})
      };
    }""", registered["id"])
    for key in ("ownerList", "ownerRegister", "ownerRemove"):
        assert denied[key]["status"] == 403, key
    assert denied["password"]["status"] == 400
    assert denied["password"]["body"]["error"] == "unsupported_device_fields"
    assert cloud_desktop.personal_devices("alice")["total"] == 1
    assert cloud_desktop.personal_devices("bob")["total"] == 0
    assert "password" not in json.dumps(registered)
    page.locator("[data-pd-copy]").click()
    expect(page.locator(".pd-status")).to_contain_text("复制")
    page.screenshot(path=evidence / "personal-devices-roundtrip-desktop.png")
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#whoAmI")).to_contain_text("alice")
    _open_devices(page)
    expect(page.locator(".pd-code")).to_have_value("__ut_fixture_device123")
    with page.expect_popup() as popup:
        page.locator("[data-pd-open]").click()
    popup.value.close()
    assert page.locator("[data-pd-open]").get_attribute("href") == "https://desk.hsslive.cn/#/remote"
    expect(page.locator(".pd-status")).to_contain_text("未确认设备已连接")
    mobile = context.new_page()
    mobile.set_viewport_size({"width": 390, "height": 844})
    mobile.goto(base, wait_until="domcontentloaded")
    _open_devices(mobile)
    expect(mobile.locator(".pd-device h3")).to_have_text("验收工作电脑")
    mobile.locator(".pd-device").scroll_into_view_if_needed()
    mobile.screenshot(path=evidence / "personal-devices-roundtrip-mobile.png")
    assert mobile.evaluate("document.documentElement.scrollWidth <= innerWidth")
    mobile.locator("[data-pd-remove]").click()
    mobile.locator("[data-pd-cancel-remove]").click()
    expect(mobile.locator(".pd-remove-confirm")).not_to_be_visible()
    mobile.close()
    page.locator("#advPanelClose").click()
    _authenticate(page, "bob", tokens)
    _open_devices(page)
    expect(page.locator(".pd-device")).to_have_count(0)
    expect(page.locator(".pd-workspace")).not_to_contain_text("__ut_fixture_device123")
    page.locator("#advPanelClose").click()
    _authenticate(page, "alice", tokens)
    _open_devices(page)
    expect(page.locator(".pd-device")).to_have_count(1)
    with monkeypatch.context() as failure:
        failure.setattr(cloud_desktop, "personal_devices", lambda uid: {"ok": False, "error": "storage_error"})
        page.locator("#pdRefresh").click()
        expect(page.locator(".pd-error")).to_be_visible()
    page.locator("#pdRetry").click()
    expect(page.locator(".pd-device")).to_have_count(1)
    page.locator("[data-pd-remove]").click()
    with page.expect_response(lambda response: "/personal_devices/remove" in response.url) as response:
        page.locator("[data-pd-confirm-remove]").click()
    assert response.value.status == 200
    assert response.value.json()["remote_session_terminated"] is False
    expect(page.locator(".pd-device")).to_have_count(0)
    page.reload(wait_until="domcontentloaded")
    _open_devices(page)
    expect(page.locator(".pd-device")).to_have_count(0)
