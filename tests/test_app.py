"""Smoke tests for authentication, roles, filters, and device API."""
import os
import re
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        os.environ["APP_DB_PATH"] = os.path.join(cls.temp.name, "monitor.db")
        os.environ["APP_SECRET_KEY"] = "test-only-secret-" + "x" * 32
        os.environ["ADMIN_PASSWORD"] = "test-admin-password"
        os.environ["ZABBIX_API_TOKEN"] = "test-token"
        os.environ["APP_TIMEZONE"] = "Europe/Tallinn"
        import main
        import app_config
        import app_storage
        import routes_devices
        import zabbix_service
        cls.module = main
        cls.config = app_config
        cls.storage = app_storage
        cls.devices = routes_devices
        cls.zabbix = zabbix_service

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.client = TestClient(self.module.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def token(self, path):
        page = self.client.get(path)
        match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        self.assertIsNotNone(match)
        return match.group(1)

    def login(self, name="Admin", password="test-admin-password"):
        csrf = self.token("/login")
        return self.client.post("/login", data={"csrf_token": csrf,
                               "username": name, "password": password})

    def test_home_page_after_login(self):
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.url.path, "/")
        self.assertIn("Добро пожаловать в Zabbix Live Monitoring!", response.text)
        self.assertIn('<a class="brand" href="/">Zabbix Live Monitoring</a>', response.text)
        header = response.text.split('<header class="site-header">', 1)[1].split("</header>", 1)[0]
        self.assertLess(header.index('action="/logout"'), header.index('class="settings-menu"'))
        self.assertIn('class="logout-button" aria-label="Выйти"', header)
        self.assertIn('aria-label="Настройки" title="Настройки"><svg', header)
        nav = response.text.split('<div class="device-nav">', 1)[1].split("</div>", 1)[0]
        self.assertNotIn("settings-menu", nav)
        self.assertNotIn(">Главная</a>", nav)
        links = re.findall(r'href="/devices/([^"]+)"', nav)
        self.assertEqual(links, ["TV_Amplifires", "Switches", "Modems", "VOIP", "Routers"])

    def test_static_assets_use_origin_relative_urls(self):
        page = self.client.get("/login")
        self.assertEqual(page.status_code, 200)
        self.assertRegex(page.text, r'href="/static/style\.css\?v=\d+"')
        self.assertNotIn('href="http://', page.text)
        self.assertEqual(self.client.get("/static/style.css").status_code, 200)

    def test_favorites_are_personal_and_recent_history_keeps_last_fifteen(self):
        path = "/api/favorites/VOIP/favorite-test-host"
        self.assertEqual(self.client.post(path).status_code, 401)
        self.assertEqual(self.client.get("/favorites", follow_redirects=False).status_code, 303)
        self.login()
        token = self.token("/favorites")
        device = {"id": "favorite-test-host", "name": "Favorite phone",
                  "technical_name": "favorite-phone", "address": "192.0.2.77"}
        self.assertEqual(self.client.post(path, data={"action": "add"}).status_code, 400)
        self.assertEqual(self.client.post(path, data={
            "csrf_token": token, "action": "invalid"}).status_code, 400)
        with patch.object(self.devices, "host_rows", return_value=[]):
            self.assertEqual(self.client.post(path, data={
                "csrf_token": token, "action": "add"}).status_code, 404)
        with (patch.object(self.devices, "host_rows", return_value=[device]),
              patch.object(self.devices, "device_descriptions", return_value={})):
            self.assertTrue(self.client.post(path, data={
                "csrf_token": token, "action": "add"}).json()["favorite"])
            self.assertTrue(self.client.get("/api/devices/VOIP").json()["devices"][0]["favorite"])
            detail = self.client.get("/devices/VOIP/favorite-test-host")
        self.assertEqual(detail.status_code, 200)
        self.assertIn('data-favorite="true"', detail.text)
        page = self.client.get("/favorites")
        self.assertIn("Favorite phone", page.text)
        self.assertIn("Недавно открытые", page.text)

        with closing(self.storage.connect()) as con:
            admin_id = con.execute("SELECT id FROM users WHERE username='Admin'").fetchone()[0]
            con.execute("INSERT INTO users(username,password_hash,role) VALUES (?,?,?)",
                        ("FavoriteReader", self.storage.hash_password("reader-password-123"), "read"))
            con.commit()
        for number in range(16):
            self.storage.record_recent_device(admin_id, {
                "id": f"recent-{number}", "name": f"Recent {number}",
                "address": f"192.0.2.{number}"}, "Modems")
        recent = self.storage.recent_devices_for(admin_id)
        self.assertEqual(len(recent), 15)
        self.assertEqual(recent[0]["host_id"], "recent-15")
        self.assertNotIn("recent-0", [row["host_id"] for row in recent])
        self.assertFalse(self.client.post(path, data={
            "csrf_token": token, "action": "remove"}).json()["favorite"])
        self.assertNotIn("Favorite phone", self.client.get("/favorites").text)
        self.client.post("/logout", data={"csrf_token": token})
        self.login("FavoriteReader", "reader-password-123")
        with closing(self.storage.connect()) as con:
            reader_id = con.execute(
                "SELECT id FROM users WHERE username='FavoriteReader'").fetchone()[0]
        self.assertEqual(self.storage.favorite_devices_for(reader_id), [])
        with patch.object(self.devices, "host_rows", return_value=[device]):
            self.assertTrue(self.client.post(path, data={
                "csrf_token": self.token("/favorites"), "action": "add"}).json()["favorite"])
        self.assertEqual(self.storage.favorite_devices_for(admin_id), [])

    def test_session_expires_after_seven_days_from_login(self):
        self.login()
        import app_web
        with patch.object(app_web, "SESSION_SECONDS", 0):
            response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")
        self.assertEqual(self.client.get("/login").status_code, 200)

    def test_activity_log_is_admin_only_filterable_and_paginated(self):
        self.login()
        self.client.get("/profile")
        with closing(self.storage.connect()) as con:
            rows = con.execute(
                "SELECT event,section FROM activity_log WHERE username='Admin' "
                "ORDER BY id DESC LIMIT 3").fetchall()
        self.assertIn(("view", "Мои настройки"), [(r["event"], r["section"]) for r in rows])
        self.assertIn(("login", "Вход"), [(r["event"], r["section"]) for r in rows])
        with closing(self.storage.connect()) as con:
            before_api = con.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0]
        with patch.object(self.devices, "host_rows", return_value=[]):
            self.client.get("/api/devices/VOIP")
        with closing(self.storage.connect()) as con:
            after_api = con.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0]
        self.assertEqual(before_api, after_api)
        response = self.client.get("/settings/activity")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Журнал посещений", response.text)
        self.assertIn('href="/settings/activity"', response.text)
        with closing(self.storage.connect()) as con:
            con.executemany(
                "INSERT INTO activity_log(occurred_at,user_id,username,event,section,path) "
                "VALUES (?,?,?,?,?,?)",
                [(int(time.time()), 999, "PaginationOnly", "view", f"Раздел {i}", "/")
                 for i in range(201)],
            )
            con.commit()
        first = self.client.get("/settings/activity?username=PaginationOnly")
        second = self.client.get("/settings/activity?username=PaginationOnly&page=2")
        self.assertIn("Всего записей: 201", first.text)
        self.assertIn("Страница 1 из 2", first.text)
        self.assertIn("Страница 2 из 2", second.text)
        self.assertEqual(first.text.count('data-label="Пользователь">PaginationOnly'), 200)
        self.assertEqual(second.text.count('data-label="Пользователь">PaginationOnly'), 1)
        self.client.post("/logout", data={"csrf_token": self.token("/profile")})
        with closing(self.storage.connect()) as con:
            con.execute("INSERT INTO users(username,password_hash,role) VALUES (?,?,?)",
                        ("AuditReader", self.storage.hash_password("audit-password-123"), "read"))
            con.commit()
        self.login("AuditReader", "audit-password-123")
        self.assertEqual(self.client.get("/settings/activity").status_code, 403)
        self.assertNotIn('href="/settings/activity"', self.client.get("/").text)

    def test_activity_times_use_tallinn_winter_and_summer_offsets(self):
        winter = datetime(2026, 1, 15, 12, tzinfo=timezone.utc).timestamp()
        summer = datetime(2026, 7, 15, 12, tzinfo=timezone.utc).timestamp()
        self.assertEqual(self.config.format_display_timestamp(winter),
                         "2026-01-15 14:00:00 EET")
        self.assertEqual(self.config.format_display_timestamp(summer),
                         "2026-07-15 15:00:00 EEST")
        self.login()
        page = self.client.get("/settings/activity")
        self.assertIn("Europe/Tallinn", page.text)
        self.assertIn('data-timezone="Europe/Tallinn"', page.text)

    def test_activity_retention_setting_removes_old_entries(self):
        self.login()
        self.assertIn('name="activity_retention_days"', self.client.get("/settings").text)
        now = int(time.time())
        with closing(self.storage.connect()) as con:
            con.executemany(
                "INSERT INTO activity_log(occurred_at,user_id,username,event,section,path) "
                "VALUES (?,?,?,?,?,?)",
                [(now - 8 * 86400, 999, "RetentionProbe", "view", "Old", "/"),
                 (now - 86400, 999, "RetentionProbe", "view", "Recent", "/")],
            )
            con.commit()
        response = self.client.post("/settings/activity-retention", data={
            "csrf_token": self.token("/settings"), "activity_retention_days": "7"})
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="activity_retention_days" min="0" max="3650" step="1" value="7"',
                      response.text)
        with closing(self.storage.connect()) as con:
            sections = [row[0] for row in con.execute(
                "SELECT section FROM activity_log WHERE username='RetentionProbe'")]
        self.assertEqual(sections, ["Recent"])
        self.client.post("/settings/activity-retention", data={
            "csrf_token": self.token("/settings"), "activity_retention_days": "0"})
        with closing(self.storage.connect()) as con:
            con.execute(
                "INSERT INTO activity_log(occurred_at,user_id,username,event,section,path) "
                "VALUES (?,?,?,?,?,?)", (1, 999, "RetentionProbe", "view", "Ancient", "/"))
            con.commit()
        self.assertIn("Ancient", self.client.get(
            "/settings/activity?username=RetentionProbe").text)
        self.client.post("/settings/activity-retention", data={
            "csrf_token": self.token("/settings"), "activity_retention_days": "90"})
        self.client.post("/settings/activity-retention", data={
            "csrf_token": self.token("/settings"), "activity_retention_days": "-1"})
        self.assertEqual(self.storage.setting("activity_retention_days"), "90")

    def test_admin_configuration_and_device_search(self):
        self.assertEqual(self.login().status_code, 200)
        csrf = self.token("/settings")
        data = {"csrf_token": csrf, "zabbix_url": "https://example.test/api_jsonrpc.php"}
        for category in self.config.CATEGORIES:
            data["mode:" + category] = "group"
            data["ids:" + category] = "101" if category == "VOIP" else ""
        self.client.post("/settings", data=data)
        host = {"hostid": "42", "host": "voip-42", "name": "Phone 42",
                "status": "0", "interfaces": [{"ip": "192.0.2.42", "available": "1"}]}
        def fake_zabbix(method, params):
            if method == "host.get":
                return [host]
            if method == "item.get":
                self.assertEqual(params["hostids"], ["42"])
                self.assertEqual(params["filter"], {"name": "Device description"})
                return [{"hostid": "42", "lastclock": "1720000000",
                         "lastvalue": "SIP gateway, floor 2"}]
            raise AssertionError(method)
        with patch("zabbix_service.zabbix_call", side_effect=fake_zabbix) as api:
            response = self.client.get("/api/devices/VOIP?q=phone")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["devices"][0]["name"], "Phone 42")
            self.assertEqual(response.json()["devices"][0]["device_description"],
                             "SIP gateway, floor 2")
            detail = self.client.get("/devices/VOIP/42")
            self.assertEqual(detail.status_code, 200)
            self.assertIn("SIP gateway, floor 2", detail.text)
            self.assertIn('class="device-web-link" href="http://192.0.2.42" '
                          'target="_blank" rel="noopener noreferrer"', detail.text)
            self.assertIn('>192.0.2.42</a>', detail.text)
            self.assertRegex(detail.text, r"/static/ping\.js\?v=\d+")
            self.assertIn('class="ping-overlay"', detail.text)
            self.assertIn('id="ping-avg"', detail.text)
            self.assertIn('id="ping-loss-history"', detail.text)
            self.assertIn('data-period="2d"', detail.text)
            self.assertIn('/static/metric-history.js', detail.text)
            self.assertNotIn('class="ping-stats"', detail.text)
            self.assertEqual(api.call_count, 3)
        self.assertEqual(self.client.get("/api/devices/VOIP?q=missing").json()["devices"], [])

    def test_device_web_link_uses_literal_ip_only(self):
        import routes_devices
        self.assertEqual(routes_devices.device_http_url({
            "interface_ip": "10.253.7.254", "address": "example.test"}),
            "http://10.253.7.254")
        self.assertEqual(routes_devices.device_http_url({
            "address": "2001:db8::10"}), "http://[2001:db8::10]")
        self.assertIsNone(routes_devices.device_http_url({
            "address": "javascript:alert(1)", "snmp_address": "example.test"}))

    def test_read_user_cannot_change_settings(self):
        self.login()
        admin_page = self.client.get("/profile").text
        self.assertIn('class="settings-menu"', admin_page)
        self.assertIn('href="/settings/users"', admin_page)
        csrf = self.token("/settings/users")
        self.client.post("/settings/users", data={"csrf_token": csrf, "username": "Reader",
                          "email": "reader@example.test",
                          "password": "reader-password-123",
                          "password_confirm": "reader-password-123", "role": "read"})
        self.client.post("/logout", data={"csrf_token": self.token("/settings/users")})
        self.login("Reader", "reader-password-123")
        reader_page = self.client.get("/profile").text
        self.assertIn('class="settings-menu"', reader_page)
        self.assertIn('href="/profile"', reader_page)
        self.assertNotIn('href="/settings/users"', reader_page)
        self.assertNotIn('href="/settings"', reader_page)
        self.assertEqual(self.client.get("/settings").status_code, 403)
        self.assertEqual(self.client.get("/settings/users").status_code, 403)
        self.assertEqual(self.client.post("/settings/admin-email", data={
            "csrf_token": self.token("/profile"), "email": "reader.admin@example.test"
        }).status_code, 403)
        self.assertEqual(self.client.get("/api/zabbix/catalog/group").status_code, 403)
        self.assertEqual(self.client.get("/devices/VOIP").status_code, 200)

    def test_catalog_api_lists_groups_and_templates(self):
        self.login()
        with self.zabbix._cache_lock:
            self.zabbix._catalog_cache.clear()
        def fake_zabbix(method, params):
            if method == "hostgroup.get":
                return [{"groupid": "12", "name": "VOIP phones"}]
            if method == "template.get":
                return [{"templateid": "34", "name": "Network switches"}]
            raise AssertionError(method)
        with patch("zabbix_service.zabbix_call", side_effect=fake_zabbix) as api:
            groups = self.client.get("/api/zabbix/catalog/group")
            templates = self.client.get("/api/zabbix/catalog/template")
            self.assertEqual(groups.json()["items"], [{"id": "12", "name": "VOIP phones"}])
            self.assertEqual(templates.json()["items"], [{"id": "34", "name": "Network switches"}])
            self.assertEqual(api.call_count, 2)
        self.assertEqual(self.client.get("/api/zabbix/catalog/invalid").status_code, 404)

    def test_device_pagination_and_search(self):
        self.login()
        devices = [{"id": str(i), "name": "Device %03d" % i,
                    "technical_name": "host-%03d" % i, "address": "192.0.2.1"}
                   for i in range(1, 124)]
        with patch("routes_devices.host_rows", return_value=devices), \
             patch("routes_devices.device_descriptions", return_value={}):
            first = self.client.get("/api/devices/VOIP").json()
            self.assertEqual((first["total"], first["page"], first["per_page"],
                              first["pages"], len(first["devices"])), (123, 1, 50, 3, 50))
            third = self.client.get("/api/devices/VOIP?page=3").json()
            self.assertEqual((third["devices"][0]["id"], len(third["devices"])), ("101", 23))
            selected = self.client.get("/api/devices/VOIP?page=2&per_page=25").json()
            self.assertEqual((selected["devices"][0]["id"], len(selected["devices"])),
                             ("26", 25))
            filtered = self.client.get("/api/devices/VOIP?q=Device%2001&page=99").json()
            self.assertEqual((filtered["total"], filtered["page"], filtered["pages"]),
                             (10, 1, 1))
        self.assertEqual(self.client.get("/api/devices/VOIP?per_page=500").status_code, 422)
        self.assertEqual(self.client.get("/api/devices/VOIP?page=0").status_code, 422)
        self.assertIn('value="50" selected', self.client.get("/devices/VOIP").text)

    def test_device_search_treats_separators_equally(self):
        self.login()
        devices = [
            {"id": "1", "name": "alpha_beta", "technical_name": "first", "address": "192.0.2.1"},
            {"id": "2", "name": "alpha-beta", "technical_name": "second", "address": "192.0.2.2"},
            {"id": "3", "name": "alpha beta", "technical_name": "third", "address": "192.0.2.3"},
            {"id": "4", "name": "alphabeta", "technical_name": "fourth", "address": "192.0.2.4"},
        ]
        with patch("routes_devices.host_rows", return_value=devices), \
             patch("routes_devices.device_descriptions", return_value={}):
            for query in ("alpha_beta", "alpha-beta", "alpha beta", "alpha__-- beta"):
                response = self.client.get("/api/devices/VOIP", params={"q": query})
                self.assertEqual([row["id"] for row in response.json()["devices"]],
                                 ["1", "2", "3"])

    def test_device_search_treats_estonian_letters_both_ways(self):
        self.login()
        names = ["Mägi", "Magi", "Söö", "Soo", "Põlva", "Polva", "Türi", "Turi"]
        devices = [{"id": str(index), "name": name, "technical_name": "host-" + str(index),
                    "address": "192.0.2." + str(index)}
                   for index, name in enumerate(names, 1)]
        with (patch("routes_devices.host_rows", return_value=devices),
              patch("routes_devices.device_descriptions", return_value={})):
            for query, expected in (("magi", ["1", "2"]), ("MÄGI", ["1", "2"]),
                                    ("soo", ["3", "4"]), ("SÖÖ", ["3", "4"]),
                                    ("polva", ["5", "6"]), ("PÕLVA", ["5", "6"]),
                                    ("turi", ["7", "8"]), ("TÜRI", ["7", "8"])):
                response = self.client.get("/api/devices/VOIP", params={"q": query})
                self.assertEqual([row["id"] for row in response.json()["devices"]], expected)

    def test_device_ping_uses_zabbix_interface_and_requires_login(self):
        path = "/api/devices/VOIP/42/ping"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.login()
        device = {"id": "42", "address": "192.0.2.42"}
        with patch("routes_devices.host_rows", return_value=[device]), \
             patch("routes_devices.snapshot_for", return_value={"samples": [], "sent": 0}) as ping:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(ping.call_args.args, ("42", "192.0.2.42"))
            self.assertEqual(self.client.get("/api/devices/VOIP/999/ping").status_code, 404)

    def test_device_description_missing_or_without_value(self):
        with patch("zabbix_service.zabbix_call", return_value=[
            {"hostid": "1", "lastclock": "0", "lastvalue": "stale"},
            {"hostid": "2", "lastclock": "1720000000", "lastvalue": ""},
            {"hostid": "3", "lastclock": "1720000000", "lastvalue": "Available"},
        ]) as api:
            self.assertEqual(self.zabbix.device_descriptions(["1", "2", "3"]),
                             {"3": "Available"})
            self.assertEqual(api.call_count, 1)
        self.assertEqual(self.zabbix.device_descriptions([]), {})

    def test_modem_channels_requires_login_and_reads_direct_snmp(self):
        path = "/api/devices/Modems/42/modem-channels"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.login()
        device = {"id": "42", "snmp_address": "192.0.2.42", "snmp_port": "161"}
        definitions = [{"id": "7", "oid": "1.2.3", "label": "US1 Level",
                        "multiplier": "0.1", "units": "mV"}]
        with (patch("routes_devices.host_rows", return_value=[device]),
              patch("routes_devices.modem_channel_definitions", return_value=definitions),
              patch("routes_devices.snmp_community_for", return_value="public"),
              patch("routes_devices.snmp_snapshot_for", new_callable=AsyncMock,
                    return_value={"items": [{"id": "7", "value": "32.7"}]}) as poll):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["items"][0]["value"], "32.7")
            self.assertEqual(poll.call_args.args[:4],
                             ("42", "192.0.2.42", "161", "public"))
            self.assertEqual(self.client.get("/api/devices/VOIP/42/modem-channels").status_code, 404)
            self.assertEqual(self.client.get("/api/devices/Modems/999/modem-channels").status_code, 404)

    def test_tv_optical_power_uses_direct_snmp_and_device_layout(self):
        path = "/api/devices/TV_Amplifires/42/optical-power"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.login()
        device = {"id": "42", "name": "TV amplifier", "technical_name": "tv-42",
                  "address": "192.0.2.42", "snmp_address": "192.0.2.42",
                  "snmp_port": "161", "enabled": True, "availability": "available"}
        definitions = [{"id": "81", "oid": "1.2.3", "label": "Optical input power",
                        "multiplier": "0.1", "units": "dBm", "value_type": "0"}]
        with (patch("routes_devices.host_rows", return_value=[device]),
              patch("routes_devices.device_descriptions", return_value={}),
              patch("routes_devices.optical_power_definitions", return_value=definitions),
              patch("routes_devices.snmp_community_for", return_value="public"),
              patch("routes_devices.snmp_snapshot_for", new_callable=AsyncMock,
                    return_value={"items": [{"id": "81", "label": "Optical input power",
                                             "value": "-3.4", "units": "dBm"}]}) as poll):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["items"][0]["value"], "-3.4")
            self.assertEqual(poll.call_args.args[:4],
                             ("42", "192.0.2.42", "161", "public"))
            detail = self.client.get("/devices/TV_Amplifires/42")
            self.assertIn('id="optical-monitor"', detail.text)
            self.assertIn('/static/optical.js', detail.text)
            self.assertIn('device-monitor-grid', detail.text)
            self.assertLess(detail.text.index('id="optical-monitor"'),
                            detail.text.index('id="ping-monitor"'))
            self.assertEqual(self.client.get(path.replace("/42/", "/99/")).status_code, 404)
            self.assertEqual(self.client.get(path.replace("/TV_Amplifires/", "/VOIP/")).status_code, 404)

    def test_switch_card_and_port_api(self):
        self.login()
        device = {"id": "42", "name": "Switch 42", "technical_name": "sw-42",
                  "address": "192.0.2.42", "snmp_address": "192.0.2.42",
                  "snmp_port": "161", "enabled": True, "availability": "available"}
        with (patch("routes_devices.host_rows", return_value=[device]),
              patch("routes_devices.device_descriptions", return_value={}),
              patch("routes_devices.switch_snapshot_for", new_callable=AsyncMock,
                    return_value={"ports": [{"index": 1, "name": "Gi1", "label": "1", "state": "fast",
                                             "speed_mbps": 1000}], "updated_at": 1}) as poll):
            detail = self.client.get("/devices/Switches/42")
            self.assertIn('id="switch-monitor"', detail.text)
            self.assertLess(detail.text.index('id="switch-monitor"'),
                            detail.text.index('id="ping-monitor"'))
            self.assertIn('/static/switch.js', detail.text)
            response = self.client.get("/api/devices/Switches/42/switch-ports")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["ports"][0]["state"], "fast")
            self.assertEqual(poll.call_args.args,
                              ("42", "192.0.2.42", "161", "public",
                               ("vlan", "aux", "loop")))
            self.assertEqual(self.client.get(
                "/api/devices/Modems/42/switch-ports").status_code, 404)

    def test_switch_cable_test_requires_execute_role_and_uses_selected_port(self):
        path = "/api/devices/Switches/42/switch-ports/8/cable-test"
        self.assertEqual(self.client.post(path).status_code, 401)
        self.login()
        token = self.token("/settings")
        device = {"id": "42", "snmp_address": "192.0.2.42", "snmp_port": "161"}
        snapshot = {"ports": [{"index": 8, "name": "Gi1/0/8", "label": "8",
                               "state": "fast", "speed_mbps": 1000}]}
        with (patch("routes_devices.host_rows", return_value=[device]),
              patch("routes_devices.switch_snapshot_for", new_callable=AsyncMock,
                    return_value=snapshot),
              patch("routes_devices.run_cable_test", new_callable=AsyncMock,
                    return_value={"port": "1/0/8", "pairs": [
                        {"pair": "Pair-A", "status": "Normal",
                         "length": "66 m", "error": "—"}]}) as test):
            self.assertEqual(self.client.post(path).status_code, 400)
            with patch("routes_devices.require_user", return_value={"role": "read"}):
                self.assertEqual(self.client.post(
                    path, data={"csrf_token": token}).status_code, 403)
            self.assertEqual(self.client.post(
                path.replace("/8/", "/9/"),
                data={"csrf_token": token}).status_code, 404)
            with patch("routes_devices.require_user", return_value={"role": "execute"}):
                response = self.client.post(path, data={"csrf_token": token})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["pairs"][0]["length"], "66 m")
            self.assertEqual(test.await_args.args,
                             ("42", "192.0.2.42", "161", "public", "private",
                              "Gi1/0/8"))

    def test_switch_port_filter_is_admin_setting(self):
        self.login()
        self.assertIn('value="Vlan|AUX|Loop"', self.client.get("/settings").text)
        response = self.client.post("/settings/switch-ports", data={
            "csrf_token": self.token("/settings"),
            "switch_port_exclude": "Vlan|AUX|Loop|Virtual",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.storage.setting("switch_port_exclude"),
                         "Vlan|AUX|Loop|Virtual")
        self.assertIn('value="Vlan|AUX|Loop|Virtual"', response.text)
        with (patch("routes_devices.host_rows", return_value=[{
                  "id": "42", "snmp_address": "192.0.2.42", "snmp_port": "161"}]),
              patch("routes_devices.switch_snapshot_for", new_callable=AsyncMock,
                    return_value={"ports": [], "updated_at": 1}) as poll):
            self.assertEqual(self.client.get(
                "/api/devices/Switches/42/switch-ports").status_code, 200)
            self.assertEqual(poll.call_args.args[-1],
                             ("vlan", "aux", "loop", "virtual"))
        self.client.post("/settings/switch-ports", data={
            "csrf_token": self.token("/settings"), "switch_port_exclude": "Vlan||AUX",
        })
        self.assertEqual(self.storage.setting("switch_port_exclude"),
                         "Vlan|AUX|Loop|Virtual")
        self.client.post("/settings/switch-ports", data={
            "csrf_token": self.token("/settings"), "switch_port_exclude": "",
        })
        self.assertEqual(self.storage.setting("switch_port_exclude"), "")

    def test_tv_optical_history_uses_selected_zabbix_item(self):
        path = "/api/devices/TV_Amplifires/42/optical-power/81/history"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.login()
        definitions = [{"id": "81", "label": "Optical input power", "units": "dBm",
                        "value_type": "0"}]
        with (patch("routes_devices.host_rows", return_value=[{"id": "42"}]),
              patch("routes_devices.optical_power_definitions", return_value=definitions),
              patch("routes_devices.zabbix_call", return_value=[
                  {"clock": "1799999990", "value": "-3.4"}]) as api):
            response = self.client.get(path, params={"period": "12h"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["points"][0]["value"], -3.4)
            self.assertEqual(response.json()["to"] - response.json()["from"], 43200)
            self.assertEqual(api.call_args.args[1]["itemids"], ["81"])
            self.assertEqual(self.client.get(path.replace("/81/", "/82/")).status_code, 404)
            self.assertEqual(self.client.get(path, params={"period": "3d"}).status_code, 422)

    def test_modem_history_uses_selected_zabbix_item(self):
        path = "/api/devices/Modems/42/modem-channels/7/history"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.login()
        definitions = [{"id": "7", "label": "DS SNR", "units": "dB", "value_type": "0"}]
        with (patch("routes_devices.host_rows", return_value=[{"id": "42"}]),
              patch("routes_devices.modem_channel_definitions", return_value=definitions),
              patch("routes_devices.zabbix_call", return_value=[
                  {"clock": "1799999990", "value": "31.2"},
                  {"clock": "1799999980", "value": "30.8"}]) as api):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["points"], [
                {"time": 1799999980, "value": 30.8},
                {"time": 1799999990, "value": 31.2}])
            self.assertEqual(response.json()["label"], "DS SNR")
            self.assertEqual(api.call_args.args[0], "history.get")
            self.assertEqual(api.call_args.args[1]["itemids"], ["7"])
            self.assertEqual(api.call_args.args[1]["history"], 0)
            self.assertEqual(api.call_args.args[1]["time_from"], response.json()["from"])
            self.assertEqual(response.json()["to"] - response.json()["from"], 3600)
            for period, seconds in (("12h", 43200), ("24h", 86400), ("2d", 172800)):
                ranged = self.client.get(path, params={"period": period})
                self.assertEqual(ranged.status_code, 200)
                self.assertEqual(ranged.json()["period"], period)
                self.assertEqual(ranged.json()["to"] - ranged.json()["from"], seconds)
                self.assertEqual(api.call_args.args[1]["time_from"], ranged.json()["from"])
            self.assertEqual(self.client.get(path, params={"period": "7d"}).status_code, 422)
            self.assertEqual(self.client.get(path.replace("/42/", "/99/")).status_code, 404)
            self.assertEqual(self.client.get(path.replace("/7/", "/8/")).status_code, 404)
            self.assertEqual(self.client.get(path.replace("/Modems/", "/VOIP/")).status_code, 404)

    def test_modem_restarts_use_latest_zabbix_item_and_guard_history(self):
        path = "/api/devices/Modems/42/modem-restarts"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.login()
        self.zabbix.clear_caches()
        items = [
            {"itemid": "90", "name": "restarts count per hour", "status": "1",
             "value_type": "3", "lastclock": "200", "lastvalue": "99"},
            {"itemid": "91", "name": "restarts count per hour", "status": "0",
             "value_type": "3", "lastclock": "100", "lastvalue": "2", "units": ""},
            {"itemid": "92", "name": "restarts count per hour", "status": "0",
             "value_type": "3", "lastclock": "200", "lastvalue": "3", "units": ""},
        ]
        with (patch("routes_devices.host_rows", return_value=[{"id": "42"}]),
              patch("zabbix_service.zabbix_call", return_value=items) as api):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["item"]["value"], 3)
            self.assertEqual(response.json()["item"]["id"], "92")
            self.assertEqual(self.client.get(path).json()["item"]["value"], 3)
            self.assertEqual(api.call_count, 1)
            self.assertEqual(api.call_args.args[0], "item.get")
            self.assertEqual(self.client.get(path.replace("/42/", "/99/")).status_code, 404)
            self.assertEqual(self.client.get(path.replace("/Modems/", "/VOIP/")).status_code, 404)
        history = path + "/92/history"
        with (patch("routes_devices.host_rows", return_value=[{"id": "42"}]),
              patch("routes_devices.zabbix_call", return_value=[
                  {"clock": "1799999990", "value": "3"}]) as api):
            response = self.client.get(history)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["points"][0]["value"], 3)
            self.assertEqual(api.call_args.args[1]["itemids"], ["92"])
            self.assertEqual(self.client.get(history.replace("/92/", "/91/")).status_code, 404)
            self.assertEqual(self.client.get(history, params={"period": "7d"}).status_code, 422)

    def test_fourteen_day_history_uses_zabbix_hourly_trends_on_all_graphs(self):
        self.login()
        modem = {"id": "7", "label": "DS SNR", "units": "dB", "value_type": "0"}
        error = {"id": "8", "label": "Errors", "units": "", "value_type": "3"}
        modem_with_error = {**modem, "error_rate_id": "8"}
        restarts = {"id": "9", "label": "Рестарты/ч", "units": "", "value_type": "3", "value": 2}
        loss = {"id": "10", "label": "Потери", "units": "%", "value_type": "0"}
        optical = {"id": "11", "label": "Optical input power", "units": "dBm", "value_type": "0"}
        cases = [
            ("/api/devices/Modems/42/modem-channels/7/history", "7"),
            ("/api/devices/Modems/42/modem-restarts/9/history", "9"),
            ("/api/devices/Modems/42/ping-loss/history", "10"),
            ("/api/devices/TV_Amplifires/42/optical-power/11/history", "11"),
        ]
        def trend_api(method, params):
            self.assertEqual(method, "trend.get")
            return [{"clock": "1799990000", "value_avg": "2.5"}]
        device = {"id": "42", "name": "Modem 42", "address": "192.0.2.42"}
        with (patch("routes_devices.host_rows", return_value=[device]),
              patch("routes_devices.modem_channel_definitions", return_value=[modem]),
              patch("routes_devices.modem_restarts_item", return_value=restarts),
              patch("routes_devices.ping_loss_definition", return_value=loss),
              patch("routes_devices.optical_power_definitions", return_value=[optical]),
              patch("routes_devices.zabbix_call", side_effect=trend_api) as api):
            for path, item_id in cases:
                with self.subTest(path=path):
                    response = self.client.get(path, params={"period": "14d"})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["points"], [
                        {"time": 1799990000, "value": 2.5}])
                    self.assertEqual(response.json()["aggregation"], "hourly_average")
                    self.assertEqual(response.json()["to"] - response.json()["from"], 1209600)
                    self.assertEqual(api.call_args.args[1]["itemids"], [item_id])
            with patch("routes_devices.device_descriptions", return_value={}):
                detail = self.client.get("/devices/Modems/42")
            self.assertEqual(detail.status_code, 200)
            self.assertIn('data-period="14d"', detail.text)
            self.assertIn('id="modem-restarts"', detail.text)
            self.assertIn('/static/metric-history.js', detail.text)
        with (patch("routes_devices.host_rows", return_value=[{"id": "42"}]),
              patch("routes_devices.modem_channel_definitions",
                    return_value=[modem_with_error, error]),
              patch("routes_devices.zabbix_call", side_effect=trend_api) as api):
            response = self.client.get(cases[0][0], params={"period": "14d"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["secondary"]["points"][0]["value"], 2.5)
            self.assertEqual(api.call_count, 2)

        def no_trends(method, params):
            if method == "trend.get":
                return []
            self.assertEqual(method, "history.get")
            return [{"clock": "1799990000", "value": "4"}]
        with (patch("routes_devices.host_rows", return_value=[{"id": "42"}]),
              patch("routes_devices.modem_restarts_item", return_value=restarts),
              patch("routes_devices.zabbix_call", side_effect=no_trends) as api):
            response = self.client.get(cases[1][0], params={"period": "14d"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["points"][0]["value"], 4)
            self.assertEqual([call.args[0] for call in api.call_args_list],
                             ["trend.get", "history.get"])

    def test_snr_history_includes_matching_error_rate_from_zabbix(self):
        self.login()
        definitions = [
            {"id": "7", "label": "DS52 459MHz SNR", "units": "dB",
             "value_type": "0", "error_rate_id": "8"},
            {"id": "8", "label": "DS52 459MHz ErrorRate", "units": "",
             "value_type": "0", "metric": "error_rate"},
        ]
        def history(method, params):
            self.assertEqual(method, "history.get")
            return ([{"clock": "1799999990", "value": "33.9"}]
                    if params["itemids"] == ["7"] else
                    [{"clock": "1799999985", "value": "2.4"}])
        with (patch("routes_devices.host_rows", return_value=[{"id": "42"}]),
              patch("routes_devices.modem_channel_definitions", return_value=definitions),
              patch("routes_devices.zabbix_call", side_effect=history) as api):
            response = self.client.get(
                "/api/devices/Modems/42/modem-channels/7/history?period=12h")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["points"][0]["value"], 33.9)
            self.assertEqual(response.json()["secondary"]["points"][0]["value"], 2.4)
            self.assertEqual(response.json()["secondary"]["units"], "ош/с")
            self.assertEqual({call.args[1]["itemids"][0] for call in api.call_args_list},
                             {"7", "8"})

    def test_live_snr_contains_error_rate_without_separate_row(self):
        self.login()
        definitions = [
            {"id": "7", "label": "DS52 459MHz SNR", "units": "dB",
             "error_rate_id": "8"},
            {"id": "8", "label": "DS52 459MHz ErrorRate", "units": "",
             "metric": "error_rate"},
        ]
        with (patch("routes_devices.host_rows", return_value=[{
                  "id": "42", "snmp_address": "192.0.2.42", "snmp_port": "161"}]),
              patch("routes_devices.modem_channel_definitions", return_value=definitions),
              patch("routes_devices.snmp_snapshot_for", new_callable=AsyncMock,
                    return_value={"items": [
                        {"id": "7", "label": "DS52 459MHz SNR", "value": "33.9", "units": "dB"},
                        {"id": "8", "label": "DS52 459MHz ErrorRate", "value": "2.4", "units": ""},
                    ], "updated_at": 1})):
            response = self.client.get("/api/devices/Modems/42/modem-channels")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [
            {"id": "7", "label": "DS52 459MHz SNR", "value": "33.9",
             "units": "dB", "error_rate": "2.4"}])

    def test_long_history_keeps_extreme_values_when_reduced(self):
        points = [{"time": index, "value": 0} for index in range(3000)]
        points[700]["value"] = -20
        points[1700]["value"] = 50
        reduced = self.devices.plot_points(points)
        self.assertLessEqual(len(reduced), 1200)
        self.assertEqual(reduced[0], points[0])
        self.assertEqual(reduced[-1], points[-1])
        self.assertIn(points[700], reduced)
        self.assertIn(points[1700], reduced)

    def test_ping_loss_history_reads_zabbix_item_for_selected_host(self):
        path = "/api/devices/VOIP/42/ping-loss/history"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.login()
        item = {"id": "89", "label": "Потери пакетов Zabbix", "units": "%",
                "value_type": "0"}
        with (patch("routes_devices.host_rows", return_value=[{"id": "42"}]),
              patch("routes_devices.ping_loss_definition", return_value=item),
              patch("routes_devices.zabbix_call", return_value=[
                  {"clock": "1799999990", "value": "2.5"}]) as api):
            response = self.client.get(path, params={"period": "2d"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["points"][0]["value"], 2.5)
            self.assertEqual(response.json()["units"], "%")
            self.assertEqual(response.json()["to"] - response.json()["from"], 172800)
            self.assertEqual(api.call_args.args[1]["itemids"], ["89"])
            self.assertEqual(self.client.get(path, params={"period": "3d"}).status_code, 422)
            self.assertEqual(self.client.get(path.replace("/42/", "/99/")).status_code, 404)
        with (patch("routes_devices.host_rows", return_value=[{"id": "42"}]),
              patch("routes_devices.ping_loss_definition", return_value=None)):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404)
            self.assertIn("icmppingloss", response.json()["error"])

    def test_ping_loss_item_selection_prefers_host_default_target(self):
        with self.zabbix._cache_lock:
            self.zabbix._ping_loss_items_cache.clear()
        with patch("zabbix_service.zabbix_call", return_value=[
            {"itemid": "1", "key_": "icmppingloss[192.0.2.1]", "status": "0",
             "value_type": "0", "units": "%"},
            {"itemid": "2", "key_": "icmppingloss[,50,1000,64,3000]", "status": "0",
             "value_type": "0", "units": "%"},
            {"itemid": "3", "key_": "icmppingloss", "status": "1",
             "value_type": "0", "units": "%"},
        ]) as api:
            item = self.zabbix.ping_loss_definition("test-host")
            self.assertEqual(item["id"], "2")
            self.assertEqual(api.call_args.args[1]["hostids"], ["test-host"])

    def test_existing_user_database_gets_light_theme(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            with sqlite3.connect(path) as con:
                con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, "
                            "username TEXT NOT NULL UNIQUE COLLATE NOCASE, "
                            "password_hash TEXT NOT NULL, role TEXT NOT NULL, "
                            "active INTEGER NOT NULL DEFAULT 1)")
                con.execute("INSERT INTO users(username,password_hash,role) "
                            "VALUES ('Admin','existing-hash','admin')")
            with patch("app_storage.DB_PATH", path):
                self.storage.init_db()
                with closing(self.storage.connect()) as con:
                    self.assertEqual(con.execute("SELECT theme FROM users WHERE username='Admin'")
                                      .fetchone()[0], "light")
                    self.assertIsNone(con.execute(
                        "SELECT email FROM users WHERE username='Admin'").fetchone()[0])

    def test_profile_theme_is_available_to_reader_and_saved_per_user(self):
        self.assertEqual(self.client.get("/profile", follow_redirects=False).status_code, 303)
        self.login()
        self.assertIn('data-theme="light"', self.client.get("/profile").text)
        csrf = self.token("/settings/users")
        self.client.post("/settings/users", data={"csrf_token": csrf,
                         "username": "ThemeReader", "email": "theme@example.test",
                         "password": "theme-reader-password-123",
                         "password_confirm": "theme-reader-password-123",
                         "role": "read"})
        self.client.post("/logout", data={"csrf_token": self.token("/profile")})
        self.login("ThemeReader", "theme-reader-password-123")
        self.assertIn('data-theme="light"', self.client.get("/profile").text)
        response = self.client.post("/profile", data={
            "csrf_token": self.token("/profile"), "theme": "dark"})
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-theme="dark"', response.text)
        self.assertIn('data-theme="dark"', self.client.get("/devices/VOIP").text)
        self.assertEqual(self.client.post("/profile", data={
            "csrf_token": self.token("/profile"), "theme": "invalid"}).status_code, 400)
        self.assertEqual(self.client.get("/settings").status_code, 403)
        self.client.post("/logout", data={"csrf_token": self.token("/profile")})
        self.login()
        self.assertIn('data-theme="light"', self.client.get("/profile").text)

    def test_snmp_community_is_encrypted_and_can_reset_to_public(self):
        self.login()
        self.assertEqual(self.storage.snmp_community_for("Modems"), "public")

        data = {"csrf_token": self.token("/settings"), "zabbix_url": ""}
        for category in self.config.CATEGORIES:
            data["mode:" + category] = "group"
            data["ids:" + category] = ""
        data["snmp_community:Modems"] = "modem-secret-test"
        self.client.post("/settings", data=data)
        with closing(self.storage.connect()) as con:
            stored = con.execute("SELECT value FROM settings WHERE key='snmp_community:Modems'").fetchone()[0]
        self.assertNotIn("modem-secret-test", stored)
        self.assertEqual(self.storage.snmp_community_for("Modems"), "modem-secret-test")
        self.assertNotIn("modem-secret-test", self.client.get("/settings").text)
        data["csrf_token"] = self.token("/settings")
        data["snmp_community:Modems"] = ""
        data["clear_snmp:Modems"] = "1"
        self.client.post("/settings", data=data)
        self.assertEqual(self.storage.snmp_community_for("Modems"), "public")

    def test_snmp_write_community_is_encrypted_and_defaults_to_private(self):
        self.login()
        self.assertEqual(self.storage.snmp_write_community_for("Modems"), "private")
        data = {"csrf_token": self.token("/settings"), "zabbix_url": ""}
        for category in self.config.CATEGORIES:
            data["mode:" + category] = "group"
            data["ids:" + category] = ""
        data["snmp_write_community:Modems"] = "write-secret-test"
        self.client.post("/settings", data=data)
        with closing(self.storage.connect()) as con:
            stored = con.execute(
                "SELECT value FROM settings WHERE key='snmp_write_community:Modems'"
            ).fetchone()[0]
        self.assertNotIn("write-secret-test", stored)
        self.assertEqual(self.storage.snmp_write_community_for("Modems"),
                         "write-secret-test")
        self.assertNotIn("write-secret-test", self.client.get("/settings").text)
        data["csrf_token"] = self.token("/settings")
        data["snmp_write_community:Modems"] = ""
        self.client.post("/settings", data=data)
        self.assertEqual(self.storage.snmp_write_community_for("Modems"),
                         "write-secret-test")
        data["csrf_token"] = self.token("/settings")
        data["snmp_write_community:Modems"] = "invalid\ncommunity"
        self.client.post("/settings", data=data)
        self.assertEqual(self.storage.snmp_write_community_for("Modems"),
                         "write-secret-test")
        data["csrf_token"] = self.token("/settings")
        data["snmp_write_community:Modems"] = ""
        data["clear_snmp_write:Modems"] = "1"
        self.client.post("/settings", data=data)
        self.assertEqual(self.storage.snmp_write_community_for("Modems"), "private")

    def test_snmp_timeout_returns_unavailable_values(self):
        self.login()
        device = {"id": "42", "snmp_address": "192.0.2.42", "snmp_port": 161}
        definition = {"id": "55", "label": "US1 Level", "units": "dBmV"}
        for category, endpoint, lookup in (
            ("Modems", "modem-channels", "modem_channel_definitions"),
            ("TV_Amplifires", "optical-power", "optical_power_definitions"),
        ):
            with (
                self.subTest(category=category),
                patch.object(self.devices, "host_rows", return_value=[device]),
                patch.object(self.devices, lookup, return_value=[definition]),
                patch.object(self.devices, "snmp_snapshot_for", new_callable=AsyncMock,
                             side_effect=RuntimeError("SNMP timeout")),
            ):
                response = self.client.get(f"/api/devices/{category}/42/{endpoint}")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["items"], [
                    {"id": "55", "label": "US1 Level", "value": None, "units": "dBmV"}
                ])

    def test_csrf_rejects_mutation(self):
        self.login()
        self.assertEqual(self.client.post("/settings/users", data={"username": "x"}).status_code, 400)

    def test_password_confirmation_is_required_for_admin_user_management(self):
        self.login()
        token = self.token("/settings/users")
        base = {"csrf_token": token, "username": "ConfirmReader",
                "email": "confirm@example.test", "role": "read"}
        response = self.client.post("/settings/users", data={
            **base, "password": "first-pass-123", "password_confirm": "different-pass-123"})
        self.assertIn("Пароли не совпадают", response.text)
        with closing(self.storage.connect()) as con:
            self.assertIsNone(con.execute(
                "SELECT id FROM users WHERE username='ConfirmReader'").fetchone())
        response = self.client.post("/settings/users", data={
            **base, "password": "first-pass-123", "password_confirm": "first-pass-123"})
        self.assertIn("Пользователь создан", response.text)
        with closing(self.storage.connect()) as con:
            reader = con.execute(
                "SELECT id,password_hash FROM users WHERE username='ConfirmReader'").fetchone()
            reader_id, old_hash = reader["id"], reader["password_hash"]
        response = self.client.post(f"/settings/users/{reader_id}", data={
            "csrf_token": token, "email": "changed@example.test", "role": "execute",
            "active": "1", "password": "second-pass-123",
            "password_confirm": "different-pass-123"})
        self.assertIn("Пароли не совпадают", response.text)
        with closing(self.storage.connect()) as con:
            reader = con.execute(
                "SELECT email,role,password_hash FROM users WHERE id=?", (reader_id,)).fetchone()
        self.assertEqual((reader["email"], reader["role"], reader["password_hash"]),
                         ("confirm@example.test", "read", old_hash))
        response = self.client.post(f"/settings/users/{reader_id}", data={
            "csrf_token": token, "email": "changed@example.test", "role": "execute",
            "active": "1", "password": "", "password_confirm": ""})
        self.assertIn("Пользователь обновлён", response.text)
        response = self.client.post("/settings/admin-password", data={
            "csrf_token": token, "current_password": "test-admin-password",
            "new_password": "new-admin-pass-123", "new_password_confirm": "different-pass-123"})
        self.assertIn("Пароли не совпадают", response.text)
        with closing(self.storage.connect()) as con:
            admin_hash = con.execute(
                "SELECT password_hash FROM users WHERE username='Admin'").fetchone()[0]
        self.assertTrue(self.storage.check_password("test-admin-password", admin_hash))

    def test_reader_can_change_own_password_with_confirmation(self):
        self.login()
        token = self.token("/settings/users")
        self.client.post("/settings/users", data={
            "csrf_token": token, "username": "SelfServiceReader",
            "email": "self-service@example.test", "role": "read",
            "password": "initial-pass-123", "password_confirm": "initial-pass-123"})
        self.client.post("/logout", data={"csrf_token": token})
        self.login("SelfServiceReader", "initial-pass-123")
        token = self.token("/profile")
        profile = self.client.get("/profile").text
        self.assertIn('name="new_password_confirm"', profile)
        response = self.client.post("/profile/password", data={
            "csrf_token": token, "current_password": "initial-pass-123",
            "new_password": "new-reader-pass-123", "new_password_confirm": "different-pass-123"})
        self.assertIn("Пароли не совпадают", response.text)
        response = self.client.post("/profile/password", data={
            "csrf_token": token, "current_password": "wrong-password",
            "new_password": "new-reader-pass-123", "new_password_confirm": "new-reader-pass-123"})
        self.assertIn("Текущий пароль неверен", response.text)
        response = self.client.post("/profile/password", data={
            "csrf_token": token, "current_password": "initial-pass-123",
            "new_password": "short", "new_password_confirm": "short"})
        self.assertIn("не менее 9 символов", response.text)
        response = self.client.post("/profile/password", data={
            "csrf_token": token, "current_password": "initial-pass-123",
            "new_password": "new-reader-pass-123", "new_password_confirm": "new-reader-pass-123"})
        self.assertIn("Пароль изменён", response.text)
        self.client.post("/logout", data={"csrf_token": token})
        self.assertEqual(self.login("SelfServiceReader", "initial-pass-123").url.path, "/login")
        self.assertEqual(self.login("SelfServiceReader", "new-reader-pass-123").url.path, "/")

    def test_email_login_and_nine_character_password(self):
        self.login()
        token = self.token("/settings/users")
        response = self.client.post("/settings/admin-email", data={
            "csrf_token": token, "email": "Admin.Login@Example.test"})
        self.assertIn("Email Admin сохранён", response.text)
        self.assertIn('value="admin.login@example.test"', response.text)
        base = {"csrf_token": token, "username": "EmailReader",
                "email": "Reader.Login@Example.test", "role": "read"}
        response = self.client.post("/settings/users", data={
            **base, "password": "12345678", "password_confirm": "12345678"})
        self.assertIn("от 9 символов", response.text)
        response = self.client.post("/settings/users", data={
            **base, "password": "123456789", "password_confirm": "123456789"})
        self.assertIn("Пользователь создан", response.text)
        with closing(self.storage.connect()) as con:
            user = con.execute("SELECT id,email FROM users WHERE username='EmailReader'").fetchone()
        self.assertEqual(user["email"], "reader.login@example.test")
        response = self.client.post("/settings/users", data={
            **base, "username": "AnotherReader", "email": "READER.LOGIN@example.test",
            "password": "123456789", "password_confirm": "123456789"})
        self.assertIn("Имя или email уже используется", response.text)
        response = self.client.post("/settings/users", data={
            **base, "username": "admin.login@example.test",
            "email": "another@example.test", "password": "123456789",
            "password_confirm": "123456789"})
        self.assertIn("Имя или email уже используется", response.text)
        response = self.client.post(f"/settings/users/{user['id']}", data={
            "csrf_token": token, "email": "new.reader@example.test",
            "role": "read", "active": "1", "password": "shortpass",
            "password_confirm": "shortpass"})
        self.assertIn("Пользователь обновлён", response.text)
        self.client.post("/logout", data={"csrf_token": token})
        response = self.login("NEW.READER@EXAMPLE.TEST", "shortpass")
        self.assertEqual(response.url.path, "/")
        self.client.post("/logout", data={"csrf_token": self.token("/profile")})
        response = self.login("ADMIN.LOGIN@EXAMPLE.TEST", "test-admin-password")
        self.assertEqual(response.url.path, "/")

    def test_z_admin_can_change_password(self):
        self.login()
        csrf = self.token("/settings/users")
        response = self.client.post("/settings/admin-password", data={
            "csrf_token": csrf, "current_password": "test-admin-password",
            "new_password": "12345678", "new_password_confirm": "12345678"})
        self.assertIn("не менее 9 символов", response.text)
        response = self.client.post("/settings/admin-password", data={
            "csrf_token": csrf, "current_password": "test-admin-password",
            "new_password": "shortpass", "new_password_confirm": "shortpass"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Пароль Admin изменён", response.text)
        csrf = self.token("/settings/users")
        self.client.post("/settings/admin-password", data={
            "csrf_token": csrf, "current_password": "shortpass",
            "new_password": "test-admin-password",
            "new_password_confirm": "test-admin-password"})


if __name__ == "__main__":
    unittest.main()
