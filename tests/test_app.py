"""Smoke tests for authentication, roles, filters, and device API."""
import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
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
        self.assertIn('href="/" class="active"', response.text)
        self.assertIn('href="/devices/VOIP"', response.text)

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
            self.assertRegex(detail.text, r"/static/ping\.js\?v=\d+")
            self.assertIn('class="ping-overlay"', detail.text)
            self.assertIn('id="ping-avg"', detail.text)
            self.assertIn('id="ping-loss-history"', detail.text)
            self.assertIn('data-period="2d"', detail.text)
            self.assertIn('/static/metric-history.js', detail.text)
            self.assertNotIn('class="ping-stats"', detail.text)
            self.assertEqual(api.call_count, 3)
        self.assertEqual(self.client.get("/api/devices/VOIP?q=missing").json()["devices"], [])

    def test_read_user_cannot_change_settings(self):
        self.login()
        admin_page = self.client.get("/profile").text
        self.assertIn('class="settings-menu"', admin_page)
        self.assertIn('href="/settings/users"', admin_page)
        csrf = self.token("/settings/users")
        self.client.post("/settings/users", data={"csrf_token": csrf, "username": "Reader",
                         "password": "reader-password-123", "role": "read"})
        self.client.post("/logout", data={"csrf_token": self.token("/settings/users")})
        self.login("Reader", "reader-password-123")
        reader_page = self.client.get("/profile").text
        self.assertIn('class="settings-menu"', reader_page)
        self.assertIn('href="/profile"', reader_page)
        self.assertNotIn('href="/settings/users"', reader_page)
        self.assertNotIn('href="/settings"', reader_page)
        self.assertEqual(self.client.get("/settings").status_code, 403)
        self.assertEqual(self.client.get("/settings/users").status_code, 403)
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
            self.assertEqual(self.client.get(path.replace("/42/", "/99/")).status_code, 404)
            self.assertEqual(self.client.get(path.replace("/TV_Amplifires/", "/VOIP/")).status_code, 404)

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

    def test_profile_theme_is_available_to_reader_and_saved_per_user(self):
        self.assertEqual(self.client.get("/profile", follow_redirects=False).status_code, 303)
        self.login()
        self.assertIn('data-theme="light"', self.client.get("/profile").text)
        csrf = self.token("/settings/users")
        self.client.post("/settings/users", data={"csrf_token": csrf,
                         "username": "ThemeReader", "password": "theme-reader-password-123",
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

    def test_csrf_rejects_mutation(self):
        self.login()
        self.assertEqual(self.client.post("/settings/users", data={"username": "x"}).status_code, 400)

    def test_z_admin_can_change_password(self):
        self.login()
        csrf = self.token("/settings/users")
        response = self.client.post("/settings/admin-password", data={
            "csrf_token": csrf, "current_password": "test-admin-password",
            "new_password": "different-password-123"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Пароль Admin изменён", response.text)
        csrf = self.token("/settings/users")
        self.client.post("/settings/admin-password", data={
            "csrf_token": csrf, "current_password": "different-password-123",
            "new_password": "test-admin-password"})


if __name__ == "__main__":
    unittest.main()
