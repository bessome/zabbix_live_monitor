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
        self.assertIn('data-view-mode="mobile"', header)
        self.assertIn('data-view-mode="desktop"', header)
        nav = response.text.split('<div class="device-nav">', 1)[1].split("</div>", 1)[0]
        self.assertNotIn("settings-menu", nav)
        self.assertNotIn(">Главная</a>", nav)
        links = re.findall(r'href="/devices/([^"]+)"', nav)
        self.assertEqual(links, ["TV_Amplifires", "Switches", "Modems", "EDFA", "VOIP", "Routers"])
        self.assertIn('href="/onu-ont"', nav)

    def test_static_assets_use_origin_relative_urls(self):
        page = self.client.get("/login")
        self.assertEqual(page.status_code, 200)
        self.assertRegex(page.text, r'href="/static/style\.css\?v=\d+"')
        self.assertRegex(page.text, r'href="/static/onu\.css\?v=\d+"')
        self.assertRegex(page.text, r'src="/static/view-mode\.js\?v=\d+"')
        self.assertNotIn('href="http://', page.text)
        self.assertNotIn('src="http://', page.text)
        self.assertEqual(self.client.get("/static/style.css").status_code, 200)
        self.assertEqual(self.client.get("/static/view-mode.js").status_code, 200)
        self.assertEqual(self.client.get("/static/onu.css").status_code, 200)
        self.login()
        devices_page = self.client.get("/devices/VOIP")
        self.assertEqual(devices_page.status_code, 200)
        self.assertRegex(devices_page.text, r'href="/static/pagination\.css\?v=\d+"')
        self.assertNotIn('href="http://', devices_page.text)
        self.assertNotIn('src="http://', devices_page.text)
        self.assertEqual(self.client.get("/static/pagination.css").status_code, 200)

    def test_edfa_section_and_basic_card(self):
        self.login()
        self.assertIn('data-category="EDFA"', self.client.get("/settings").text)
        self.assertEqual(self.client.get("/devices/EDFA").status_code, 200)
        device = {"id": "42", "name": "EDFA 42", "technical_name": "edfa-42",
                  "address": "192.0.2.42"}
        with (patch("routes_devices.host_rows", return_value=[device]),
              patch("routes_devices.device_descriptions", return_value={"42": "EDFA description"})):
            page = self.client.get("/devices/EDFA/42")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="ping-monitor"', page.text)
        self.assertIn("Device description</dt>", page.text)
        self.assertIn("EDFA description", page.text)
        self.assertNotIn('id="optical-monitor"', page.text)

    def test_onu_olt_item_search_and_history_are_scoped_to_group(self):
        self.assertEqual(self.client.get("/onu-ont", follow_redirects=False).status_code, 303)
        self.assertEqual(self.client.get("/api/onu-ont/olts").status_code, 401)
        self.login()
        page = self.client.get("/onu-ont")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="onu-query"', page.text)
        self.assertIn('id="onu-olt"', page.text)
        self.assertRegex(page.text, r'src="/static/onu\.js\?v=\d+"')
        self.assertRegex(page.text, r'href="/static/onu\.css\?v=\d+"')
        self.assertNotIn('src="http://', page.text)
        self.assertEqual(self.client.get("/static/onu.js").status_code, 200)
        hosts = [{"id": "10", "name": "OLT A", "technical_name": "olt-a"},
                 {"id": "11", "name": "OLT B", "technical_name": "olt-b"}]
        item = {"itemid": "55", "hostid": "10", "name": "ONU OPT RX 70 A5 6A AD C2 EE",
                "key_": "onu.rx[70A56AADC2EE]", "units": "dBm", "lastvalue": "-22.3",
                "lastclock": "1799999990", "value_type": "0", "status": "0"}
        with (patch("routes_onu.olt_hosts", return_value=hosts),
              patch("routes_onu.olt_items", return_value=([item], False)) as search):
            self.assertEqual(len(self.client.get("/api/onu-ont/olts").json()["olts"]), 2)
            self.assertIn("Введите минимум два", self.client.get(
                "/api/onu-ont/items").json()["hint"])
            self.assertIn("Введите минимум два", self.client.get(
                "/api/onu-ont/items?q=ONU").json()["hint"])
            self.assertEqual(self.client.get(
                "/api/onu-ont/items?host_id=99&q=onu").status_code, 404)
            found = self.client.get("/api/onu-ont/items?host_id=10&q=70%20A5")
            self.assertEqual(found.status_code, 200)
            self.assertEqual(search.call_args.args, (["10"], "70 A5"))
            self.assertEqual(found.json()["items"][0]["name"], item["name"])
            self.assertTrue(found.json()["items"][0]["numeric"])
            self.assertIsNone(found.json()["items"][0]["interface_state"])
            interface = {**item,
                         "name": "OLT Status interface EPON0/1:6 (C4 CD 50 43 11 12 )",
                         "key_": "ifOperStatus.[EPON0/1:6]", "units": ""}
            for raw, state in (("1", "up"), ("2", "down"), ("3", None)):
                with patch("routes_onu.olt_items", return_value=([{
                        **interface, "lastvalue": raw}], False)):
                    row = self.client.get(
                        "/api/onu-ont/items?host_id=10&q=C4%20CD").json()["items"][0]
                    self.assertEqual(row["value"], raw)
                    self.assertEqual(row["interface_state"], state)
            with patch("routes_onu.olt_items", return_value=([{**item, "hostid": "11"}], False)):
                self.assertEqual(self.client.get(
                    "/api/onu-ont/items?host_id=10&q=70A5").json()["items"], [])
            history_path = "/api/onu-ont/olts/10/items/55/history"
            self.assertEqual(self.client.get(history_path.replace("/10/", "/99/")).status_code, 404)
            self.assertEqual(self.client.get(history_path + "?period=7d").status_code, 422)
            with (patch("routes_onu.zabbix_call", return_value=[item]) as api,
                  patch("routes_onu.numeric_history", new_callable=AsyncMock,
                        return_value={"points": [], "label": item["name"]}) as history):
                self.assertEqual(self.client.get(history_path).status_code, 200)
                self.assertEqual(api.call_args.args[1]["hostids"], ["10"])
                self.assertEqual(api.call_args.args[1]["itemids"], ["55"])
                self.assertEqual(history.await_args.args[1], "1h")
            with (patch("routes_onu.zabbix_call", return_value=[{
                    **item, "name": "ONU Optical RX 70 A5 6A AD C2 EE"}]),
                  patch("routes_onu.numeric_history", new_callable=AsyncMock,
                        return_value={"points": [], "label": "ONU Optical RX"})):
                self.assertEqual(self.client.get(history_path).status_code, 200)
            level_name = "ONU Opitcal RX Level on interface EPON0/1:16 (E0 67 B3 B3 DD 99 )"
            with (patch("routes_onu.zabbix_call", return_value=[{
                    **item, "name": level_name}]),
                  patch("routes_onu.numeric_history", new_callable=AsyncMock,
                        return_value={"points": [], "label": level_name}) as history):
                self.assertEqual(self.client.get(history_path).status_code, 200)
                self.assertEqual(history.await_args.args[0]["label"], level_name)
            with patch("routes_onu.zabbix_call", return_value=[{**item, "hostid": "11"}]):
                self.assertEqual(self.client.get(history_path).status_code, 404)
            with patch("routes_onu.zabbix_call", return_value=[{**item, "value_type": "4"}]):
                self.assertEqual(self.client.get(history_path).status_code, 404)
            with patch("routes_onu.zabbix_call", return_value=[{**item, "name": "ONU Voltage"}]):
                self.assertEqual(self.client.get(history_path).status_code, 404)

    def test_olt_lookup_matches_partial_mac_with_or_without_spaces(self):
        self.zabbix.clear_caches()
        host = {"hostid": "10", "host": "olt-a", "name": "OLT A"}
        rx = {"itemid": "1", "name": "ONU OPT RX 70 A5 6A AD C2 EE",
              "key_": "onu.rx[70A56AADC2EE]"}
        status = {"itemid": "2", "name": "OLT Status interface 70:A5:6A:AD:C2:EE",
                  "key_": "olt.status[70A56AADC2EE]"}
        epon_status = {"itemid": "5",
                       "name": "OLT Status interface EPON0/1:6 (C4 CD 50 43 11 12 )",
                       "key_": "ifOperStatus.[EPON0/1:6]"}
        epon_level = {"itemid": "6",
                      "name": "ONU Opitcal RX Level on interface EPON0/1:16 (E0 67 B3 B3 DD 99 )",
                      "key_": "onuOpticalRx.[EPON0/1:16]"}
        optical = {"itemid": "4", "name": "ONU Optical RX 70A56AADC2EE",
                   "key_": "olt.optical.rx[70A56AADC2EE]"}
        unrelated = {"itemid": "3", "name": "ONU Voltage 70 A5 6A AD C2 EE",
                     "key_": "onu.voltage[70A56AADC2EE]"}
        with patch("zabbix_service.zabbix_call",
                   side_effect=[[host], [rx, unrelated], [status, epon_status],
                                [optical], [epon_level]]) as api:
            self.assertEqual(self.zabbix.olt_hosts()[0]["name"], "OLT A")
            for query in ("70 A5 6A AD C2 EE", "70A56AADC2EE", "A5 6A", "A56A"):
                rows, more = self.zabbix.olt_items(["10"], query)
                self.assertEqual({row["itemid"] for row in rows}, {"1", "2", "4"})
                self.assertFalse(more)
            rows, more = self.zabbix.olt_items(["10"], "C4CD5043")
            self.assertEqual([row["itemid"] for row in rows], ["5"])
            self.assertFalse(more)
            rows, more = self.zabbix.olt_items(["10"], "E0 67 B3 B3 DD 99")
            self.assertEqual([row["itemid"] for row in rows], ["6"])
            self.assertFalse(more)
            self.assertEqual(self.zabbix.olt_items(["10"], "ONU"), ([], False))
            self.assertFalse(self.zabbix.is_olt_target_item("OLT Optical RX 70A56AADC2EE"))
        self.assertEqual(api.call_args_list[0].args[1]["groupids"], ["100"])
        self.assertEqual(api.call_count, 5)
        self.assertEqual([call.args[1]["search"]["name"] for call in api.call_args_list[1:]],
                         ["ONU OPT RX", "OLT Status interface", "ONU Optical RX",
                          "ONU Opitcal RX Level"])
        self.assertTrue(all(call.args[1]["hostids"] == ["10"]
                            for call in api.call_args_list[1:]))
        self.assertTrue(all(call.kwargs == {"timeout": 30}
                            for call in api.call_args_list[1:]))
        self.zabbix.clear_caches()

    def test_onu_live_uses_each_items_oid_and_rejects_other_serial(self):
        self.zabbix.clear_caches()
        serial = "70 A5 6A AD C2 EE"
        rx = {"itemid": "55", "hostid": "10", "name": "ONU OPT RX " + serial,
              "status": "0", "type": "20", "snmp_oid": "1.3.6.1.4.1.345.1",
              "interfaceid": "7", "units": "dBm", "value_type": "0", "preprocessing": []}
        optical = {**rx, "itemid": "56", "name": "ONU Optical RX " + serial,
                   "snmp_oid": "1.3.6.1.4.1.345.2"}
        state = {**rx, "itemid": "57",
                 "name": "OLT Status interface EPON0/1:6 (" + serial + " )",
                 "snmp_oid": "1.3.6.1.4.1.345.3", "units": ""}
        level = {**rx, "itemid": "59",
                 "name": "ONU Opitcal RX Level on interface EPON0/1:16 (" + serial + " )",
                 "snmp_oid": "1.3.6.1.4.1.345.5"}
        other = {**rx, "itemid": "58", "name": "ONU OPT RX 70 A5 6A AD C2 EF",
                 "snmp_oid": "1.3.6.1.4.1.345.4"}
        interface = {"interfaceid": "7", "hostid": "10", "type": "2",
                     "ip": "192.0.2.10", "useip": "1", "port": "161",
                     "details": {"version": "2", "community": "test-community"}}
        def api(method, params):
            if method == "hostinterface.get":
                return [interface]
            if params["itemids"] == ["55"]:
                return [rx]
            return [rx, optical, state, level, other]
        with (patch("zabbix_service.zabbix_call", side_effect=api),
              patch("zabbix_service.olt_items", return_value=([rx, optical, state, level, other], False))):
            config = self.zabbix.olt_live_config("10", "55")
        self.assertEqual(config["serial"], "70A56AADC2EE")
        self.assertEqual({row["id"] for row in config["rows"]}, {"55", "56", "57", "59"})
        definitions = next(iter(config["groups"].values()))
        self.assertEqual({d["oid"] for d in definitions},
                         {"1.3.6.1.4.1.345.1", "1.3.6.1.4.1.345.2",
                          "1.3.6.1.4.1.345.3", "1.3.6.1.4.1.345.5"})
        self.assertEqual(self.zabbix.olt_item_serial("EPON0/1:6"), None)
        self.zabbix.clear_caches()

    def test_onu_live_api_returns_current_values_and_na_on_timeout(self):
        path = "/api/onu-ont/olts/10/items/55/live"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.login()
        hosts = [{"id": "10", "name": "OLT A"}]
        config = {"serial": "70A56AADC2EE",
                  "rows": [{"id": "55", "label": "ONU OPT RX", "units": "dBm"},
                           {"id": "57", "label": "OLT Status interface", "units": ""}],
                  "groups": {("192.0.2.10", "161", "test-community"):
                             [{"id": "55"}, {"id": "57"}]}}
        with (patch("routes_onu.olt_hosts", return_value=hosts),
              patch("routes_onu.olt_live_config", return_value=config),
              patch("routes_onu.snapshot_for", new_callable=AsyncMock,
                    return_value={"items": [{"id": "55", "value": "-22.3"},
                                            {"id": "57", "value": "1"}]}) as poll):
            result = self.client.get(path)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["items"][1]["interface_state"], "up")
            self.assertEqual(poll.await_args.args[1:4],
                             ("192.0.2.10", "161", "test-community"))
            poll.side_effect = RuntimeError("SNMP timeout")
            failed = self.client.get(path)
            self.assertEqual([row["value"] for row in failed.json()["items"]],
                             [None, None])
            self.assertIn("SNMP timeout", failed.json()["error"])
            self.assertEqual(self.client.get(path.replace("/10/", "/11/")).status_code, 404)
        self.assertIn('id="onu-live-dialog"', self.client.get("/onu-ont").text)

    def test_onu_live_uses_zabbix_value_when_direct_snmp_is_unavailable(self):
        self.login()
        hosts = [{"id": "10", "name": "OLT A"}]
        config = {"serial": "E067B3B3DD99", "groups": {},
                  "rows": [{"id": "57", "label": "OLT Status interface EPON0/1:16",
                            "units": "", "available": False},
                           {"id": "59", "label": "ONU Opitcal RX Level on interface EPON0/1:16",
                            "units": "dBm", "available": False}]}
        with (patch("routes_onu.olt_hosts", return_value=hosts),
              patch("routes_onu.olt_live_config", return_value=config),
              patch("routes_onu.olt_latest_values", side_effect=[
                  {"57": "1", "59": "-22.3"}, {"57": "2", "59": "-23.1"}]) as latest,
              patch("routes_onu.snapshot_for", new_callable=AsyncMock) as poll):
            response = self.client.get("/api/onu-ont/olts/10/items/57/live")
            updated = self.client.get("/api/onu-ont/olts/10/items/57/live")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["error"])
        self.assertEqual([(row["value"], row["source"]) for row in response.json()["items"]],
                         [("1", "zabbix"), ("-22.3", "zabbix")])
        self.assertEqual(response.json()["items"][0]["interface_state"], "up")
        self.assertEqual([row["value"] for row in updated.json()["items"]], ["2", "-23.1"])
        self.assertEqual(updated.json()["items"][0]["interface_state"], "down")
        self.assertEqual(latest.call_count, 2)
        self.assertEqual(latest.call_args.args, ("10", ["57", "59"]))
        poll.assert_not_awaited()

    def test_onu_live_reads_zabbix_value_without_snmp_oid(self):
        self.zabbix.clear_caches()
        name = "ONU Opitcal RX Level on interface EPON0/1:16 (E0 67 B3 B3 DD 99 )"
        level = {"itemid": "59", "hostid": "10", "name": name, "status": "0",
                 "type": "2", "snmp_oid": "", "units": "dBm", "value_type": "0",
                 "lastvalue": "-22.3", "lastclock": "1799999990"}
        with (patch("zabbix_service.zabbix_call", return_value=[level]) as api,
              patch("zabbix_service.olt_items", return_value=([level], False))):
            config = self.zabbix.olt_live_config("10", "59")
            values = self.zabbix.olt_latest_values("10", ["59"])
        self.assertEqual(config["groups"], {})
        self.assertFalse(config["rows"][0]["available"])
        self.assertEqual(values, {"59": "-22.3"})
        self.assertEqual(api.call_args.args[1]["itemids"], ["59"])
        self.zabbix.clear_caches()

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
        self.assertIn('src="/static/view-mode.js', detail.text)
        self.assertIn('href="/static/mobile.css', detail.text)
        self.assertNotIn('src="http://', detail.text)
        self.assertNotIn('<link rel="stylesheet" href="http://', detail.text)
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
            self.assertRegex(detail.text, r'href="/static/style\.css\?v=\d+"')
            self.assertNotRegex(detail.text, r'(?:src|href)="https?://[^"\s]+/static/')
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
        self.assertEqual(self.client.post("/settings/cmts/1/groups/preview",
                                          data={"csrf_token": self.token("/profile")}).status_code,
                         403)
        self.assertEqual(self.client.post("/settings/cmts/1/groups/apply",
                                          data={"csrf_token": self.token("/profile")}).status_code,
                         403)
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

    def test_device_selection_filter_ignores_query_and_limits_to_visible_hosts(self):
        path = "/api/devices/VOIP"
        self.assertEqual(self.client.get(path + "?ids=1").status_code, 401)
        self.login()
        devices = [{"id": str(i), "name": "Device %s" % i,
                    "technical_name": "host-%s" % i, "address": "192.0.2.1"}
                   for i in range(1, 61)]
        with (patch("routes_devices.host_rows", return_value=devices),
              patch("routes_devices.device_descriptions", return_value={})):
            for view in ("list", "tree"):
                result = self.client.get(path, params={
                    "ids": "1,60,999", "q": "unrelated query", "view": view}).json()
                self.assertEqual([row["id"] for row in result["devices"]], ["1", "60"])
                self.assertEqual(result["total"], 2)
                self.assertEqual(self.client.get(path, params={
                    "ids": "", "view": view}).json()["devices"], [])
            duplicate = self.client.get(path + "?ids=1,1").json()
            self.assertEqual(duplicate["total"], 1)
            self.assertEqual(self.client.get(path + "?ids=1,2,3,4,5,6").status_code, 422)

    def test_device_tree_uses_zabbix_groups_across_all_pages(self):
        self.login()
        host = {"hostid": "1", "host": "voip-1", "name": "Phone 1", "status": "0",
                "interfaces": [], "hostgroups": [{"groupid": "12", "name": "Pärnu/VOIP/floor_2"},
                                                 {"groupid": "13", "name": "Backup phones"}]}
        with (patch.object(self.zabbix, "category_filter", return_value=("group", ["12"])),
              patch.object(self.zabbix, "zabbix_call", return_value=[host]) as api):
            self.zabbix._cache.pop("VOIP", None)
            mapped = self.zabbix.host_rows("VOIP")
            self.assertEqual(mapped[0]["groups"], ["Pärnu/VOIP/floor_2", "Backup phones"])
            self.assertEqual(api.call_args.args[1]["selectHostGroups"], ["groupid", "name"])
        devices = [{"id": str(i), "name": "Phone %s" % i,
                    "technical_name": "phone-%s" % i, "address": "192.0.2.1",
                    "groups": ["Pärnu/VOIP/floor_2"], "availability": "unknown"}
                   for i in range(60)]
        with (patch("routes_devices.host_rows", return_value=devices),
              patch("routes_devices.device_descriptions") as descriptions):
            response = self.client.get("/api/devices/VOIP?view=tree")
            self.assertEqual((response.status_code, response.json()["total"],
                              len(response.json()["devices"])), (200, 60, 60))
            self.assertEqual(response.json()["devices"][0]["groups"],
                             ["Pärnu/VOIP/floor_2"])
            self.assertEqual(self.client.get("/api/devices/VOIP?view=tree&q=floor_2")
                             .json()["total"], 60)
            descriptions.assert_not_called()
        page = self.client.get("/devices/VOIP").text
        self.assertIn('data-device-view="tree"', page)
        self.assertIn('id="device-tree"', page)
        self.assertNotIn('data-device-view="tree"', self.client.get("/onu-ont").text)

    def test_tree_summaries_are_scoped_to_visible_devices(self):
        path = "/api/devices/Modems/summaries?ids=42,42"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.login()
        modem = {"id": "42"}
        levels = {"42": {"us": {"value": "45.5", "units": "dBmV"}}}
        with (patch("routes_devices.host_rows", return_value=[modem]),
              patch("routes_devices.modem_list_levels", return_value=levels) as read_levels):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["summaries"],
                             {"42": {"modem_levels": levels["42"]}})
            read_levels.assert_called_once_with(["42"])
            self.assertEqual(self.client.get(
                "/api/devices/Modems/summaries?ids=99").status_code, 404)
            self.assertEqual(read_levels.call_count, 1)
        with (patch("routes_devices.host_rows", return_value=[modem]),
              patch("routes_devices.device_descriptions",
                    return_value={"42": "Office"}) as descriptions):
            response = self.client.get("/api/devices/VOIP/summaries?ids=42")
            self.assertEqual(response.json()["summaries"],
                             {"42": {"device_description": "Office"}})
            descriptions.assert_called_once_with(["42"])
        self.assertEqual(self.client.get(
            "/api/devices/VOIP/summaries?ids=").status_code, 422)
        self.assertEqual(self.client.get("/api/devices/VOIP/summaries?ids="
                                         + ",".join(str(i) for i in range(101))).status_code,
                         422)

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

    def test_device_uptime_reads_snmp_interface_with_category_community(self):
        path = "/api/devices/VOIP/42/uptime"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.login()
        device = {"id": "42", "name": "Phone", "address": "192.0.2.42",
                  "snmp_address": "192.0.2.43", "snmp_port": "161"}
        with (patch("routes_devices.host_rows", return_value=[device]),
              patch("routes_devices.snmp_community_for", return_value="phone-ro"),
              patch("routes_devices.uptime_for", new_callable=AsyncMock,
                    return_value=86461) as uptime,
              patch("routes_devices.device_descriptions", return_value={})):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"seconds": 86461})
            self.assertEqual(uptime.await_args.args,
                             ("42", "192.0.2.43", "161", "phone-ro"))
            self.assertEqual(self.client.get(path.replace("/42/", "/99/")).status_code, 404)
            page = self.client.get("/devices/VOIP/42")
            self.assertIn('id="ping-uptime"', page.text)
            self.assertIn('>RTT</span>', page.text)
            self.assertIn('>Loss</span>', page.text)

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

    def test_modem_list_levels_selects_one_current_channel_per_host(self):
        def item(item_id, host_id, name, value, clock="200", **extra):
            return {"itemid": item_id, "hostid": host_id, "name": name,
                    "lastvalue": value, "lastclock": clock, "status": "0",
                    "value_type": "0", "units": "dBmV", **extra}
        items = [
            item("1", "1", "Upstream channel US1 Level", "45.2000", "100"),
            item("2", "1", "Upstream channel US1 Level", "45.3000"),
            item("3", "1", "Upstream channel US2 Level", "47.0"),
            item("4", "1", "Downstream channel 3 447MHz Level", "-2.5"),
            item("5", "1", "Downstream channel 1 435MHz Level", "-3.4000000000000004"),
            item("6", "1", "Downstream channel 1 435MHz SNR", "35"),
            item("7", "1", "Upstream channel US Level", "90"),
            item("12", "1", "Downstream channel Level", "30"),
            item("8", "2", "Upstream channel US Level", "42.126"),
            item("9", "2", "Downstream channel Level", "0.00"),
            item("10", "2", "Downstream channel 1 435MHz Level", "NaN"),
            item("11", "3", "Upstream channel US Level", "19.999"),
            item("13", "3", "Downstream channel Level", "-15.001"),
            item("14", "4", "Downstream channel Level", "18"),
            item("15", "5", "Downstream channel Level", "18.001"),
        ]
        with patch("zabbix_service.zabbix_call", return_value=items) as api:
            self.assertEqual(self.zabbix.modem_list_levels(["1", "2", "3", "4", "5"]), {
                "1": {"us": {"value": "45.3", "units": "dBmV", "alert": False},
                      "ds": {"value": "-3.4", "units": "dBmV", "alert": False}},
                "2": {"us": {"value": "42.13", "units": "dBmV", "alert": False},
                      "ds": {"value": "0", "units": "dBmV", "alert": False}},
                "3": {"us": {"value": "20", "units": "dBmV", "alert": True},
                      "ds": {"value": "-15", "units": "dBmV", "alert": True}},
                "4": {"ds": {"value": "18", "units": "dBmV", "alert": False}},
                "5": {"ds": {"value": "18", "units": "dBmV", "alert": True}},
            })
            self.assertEqual(api.call_args.args[0], "item.get")
            self.assertEqual(api.call_args.args[1]["hostids"], ["1", "2", "3", "4", "5"])
            self.assertEqual(api.call_args.args[1]["search"], {"name": "Level"})
        self.assertEqual(self.zabbix.modem_list_levels([]), {})

    def test_modem_list_api_uses_zabbix_levels_instead_of_descriptions(self):
        self.login()
        modem = {"id": "42", "name": "Modem", "technical_name": "modem-42",
                 "address": "192.0.2.42", "availability": "available"}
        levels = {"42": {"us": {"value": "45.3", "units": "dBmV"}}}
        with (patch("routes_devices.host_rows", return_value=[modem]),
              patch("routes_devices.modem_list_levels", return_value=levels) as read_levels,
              patch("routes_devices.device_descriptions") as descriptions):
            response = self.client.get("/api/devices/Modems")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["devices"][0]["modem_levels"], levels["42"])
            read_levels.assert_called_once_with(["42"])
            descriptions.assert_not_called()

    def test_amplifier_list_optical_levels_uses_current_zabbix_item(self):
        def item(item_id, host_id, value, clock="200", **extra):
            return {"itemid": item_id, "hostid": host_id, "name": "Optical input power",
                    "lastvalue": value, "lastclock": clock, "status": "0",
                    "value_type": "0", "units": "dBm", **extra}
        items = [item("1", "42", "-3.41"), item("2", "42", "-3.2", "100"),
                 item("3", "43", "-3.4"), item("4", "44", "bad"),
                 item("5", "45", "-5", status="1"), item("6", "99", "-7"),
                 item("7", "46", "-3.4001")]
        with patch("zabbix_service.zabbix_call", return_value=items) as api:
            self.assertEqual(self.zabbix.amplifier_list_optical_levels(["42", "43", "44", "45", "46"]),
                             {"42": {"value": "-3.41", "units": "dBm", "alert": True},
                              "43": {"value": "-3.4", "units": "dBm", "alert": False},
                              "46": {"value": "-3.4", "units": "dBm", "alert": True}})
            self.assertEqual(api.call_args.args[1]["filter"],
                             {"name": "Optical input power"})
        self.assertEqual(self.zabbix.amplifier_list_optical_levels([]), {})

    def test_amplifier_list_and_detail_replace_description_with_optical_level(self):
        self.login()
        amplifier = {"id": "42", "name": "Amplifier", "technical_name": "amp-42",
                     "address": "192.0.2.42", "availability": "available"}
        levels = {"42": {"value": "-3.41", "units": "dBm", "alert": True}}
        with (patch("routes_devices.host_rows", return_value=[amplifier]),
              patch("routes_devices.amplifier_list_optical_levels", return_value=levels) as read_levels,
              patch("routes_devices.device_descriptions", return_value={"42": "Amplifier description"}) as descriptions):
            response = self.client.get("/api/devices/TV_Amplifires")
            self.assertEqual(response.json()["devices"][0]["optical_level"], levels["42"])
            summary = self.client.get("/api/devices/TV_Amplifires/summaries?ids=42")
            self.assertEqual(summary.json()["summaries"], {"42": {"optical_level": levels["42"]}})
            detail = self.client.get("/devices/TV_Amplifires/42")
            self.assertIn("Optical input power</dt>", detail.text)
            self.assertIn("-3.41 dBm", detail.text)
            self.assertIn('class="device-level-alert-blink">-3.41 dBm', detail.text)
            self.assertIn("Device description</dt>", detail.text)
            self.assertIn("Amplifier description", detail.text)
            self.assertIn('href="/static/ping.css?', detail.text)
            self.assertIn('src="/static/optical.js?', detail.text)
            levels["42"] = {"value": "-3.4", "units": "dBm", "alert": False}
            healthy_detail = self.client.get("/devices/TV_Amplifires/42")
            self.assertIn('class="">-3.4 dBm', healthy_detail.text)
            self.assertEqual(read_levels.call_count, 4)
            self.assertEqual(descriptions.call_count, 2)

    def test_amplifier_sorting_uses_raw_levels_before_pagination(self):
        self.login()
        devices = [{"id": str(i), "name": f"Amplifier {i:02}", "technical_name": f"amp-{i}",
                    "address": f"192.0.2.{i}"} for i in range(1, 31)]
        levels = {str(i): {"value": str(-i), "raw_value": str(-i), "units": "dBm"}
                  for i in range(1, 29)}
        # Same presentation value, different raw values: sorting must retain precision.
        levels["1"] = {"value": "-3.4", "raw_value": "-3.4002", "units": "dBm"}
        levels["2"] = {"value": "-3.4", "raw_value": "-3.4001", "units": "dBm"}
        levels["29"] = {"value": "NaN", "raw_value": "NaN"}
        path = "/api/devices/TV_Amplifires?sort_by=level&per_page=25"
        with (patch("routes_devices.host_rows", return_value=devices),
              patch("routes_devices.amplifier_list_optical_levels", return_value=levels) as read):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            first = response.json()["devices"]
            self.assertEqual(first[0]["id"], "28")
            read.assert_called_once_with([row["id"] for row in devices], include_raw=True)
            second = self.client.get(path + "&page=2").json()["devices"]
            ordered = [row["id"] for row in first + second]
            self.assertLess(ordered.index("1"), ordered.index("2"))
            self.assertEqual(ordered[-2:], ["29", "30"])
            desc = self.client.get(path + "&sort_dir=desc&view=tree").json()["devices"]
            self.assertEqual(desc[0]["id"], "3")
            self.assertEqual([row["id"] for row in desc[-2:]], ["29", "30"])
            names = self.client.get("/api/devices/TV_Amplifires?sort_by=name&sort_dir=desc").json()["devices"]
            self.assertEqual(names[0]["name"], "Amplifier 30")
            self.assertEqual(self.client.get(path + "&sort_dir=invalid").status_code, 422)
            self.assertEqual(self.client.get("/api/devices/Modems?sort_by=level").status_code, 422)
        page = self.client.get("/devices/TV_Amplifires")
        self.assertIn('id="amplifier-sort"', page.text)
        self.assertIn('data-amplifier-sort="level"', page.text)

    def test_amplifier_sort_source_retains_raw_precision(self):
        item = {"itemid": "1", "hostid": "42", "name": "Optical input power", "status": "0",
                "value_type": "0", "units": "dBm", "lastclock": "200", "lastvalue": "-3.40001"}
        with patch("zabbix_service.zabbix_call", return_value=[item]):
            metric = self.zabbix.amplifier_list_optical_levels(["42"], include_raw=True)["42"]
        self.assertEqual(metric, {"value": "-3.4", "raw_value": "-3.40001", "units": "dBm", "alert": True})

    def test_modem_reboot_permissions_targets_and_settings(self):
        paths = ["/api/devices/Modems/42/modem-reboot",
                 "/api/cmts/1/modems/cc3540e608b1/modem-reboot"]
        for path in paths:
            self.assertEqual(self.client.post(path).status_code, 401)
        self.login()
        csrf = self.token("/settings/cmts")
        setting_path = "/settings/cmts/modem-write-community"
        self.assertEqual(self.client.post(setting_path, data={"community": "secret"}).status_code, 400)
        self.client.post(setting_path, data={"csrf_token": csrf, "clear_community": "1"})
        self.assertEqual(self.storage.snmp_write_community_for("Modems"), "private")
        self.client.post(setting_path, data={"csrf_token": csrf, "community": "reset-test-secret"})
        self.assertEqual(self.storage.snmp_write_community_for("Modems"), "reset-test-secret")
        self.assertNotIn("reset-test-secret", self.storage.setting("snmp_write_community:Modems"))
        self.assertNotIn("reset-test-secret", self.client.get("/settings/cmts").text)
        self.client.post(setting_path, data={"csrf_token": csrf, "community": ""})
        self.assertEqual(self.storage.snmp_write_community_for("Modems"), "reset-test-secret")
        device = {"id": "42", "name": "Modem", "technical_name": "cc3540e608b1",
                  "address": "192.0.2.42", "snmp_address": "192.0.2.42", "snmp_port": "1161"}
        modem = {"cmts_id": 1, "cmts_name": "CMTS", "mac": "cc:35:40:e6:08:b1",
                 "mac_compact": "cc3540e608b1", "ip": "192.0.2.43"}
        accepted = {"outcome": "accepted", "message": "OK", "cooldown_seconds": 30}
        with (patch("routes_devices.host_rows", return_value=[device]),
              patch("routes_devices.device_descriptions", return_value={}),
              patch("routes_devices.reboot_modem", AsyncMock(return_value=accepted)) as reset,
              patch("routes_cmts.cmts_for", return_value={"id": 1}),
              patch("routes_cmts.resolve_mac", AsyncMock(return_value=modem)) as resolve,
              patch("routes_cmts.verified_modem", AsyncMock(return_value=modem)),
              patch("routes_cmts.reboot_modem", AsyncMock(return_value=accepted)) as discovered_reset):
            for path in paths:
                self.assertEqual(self.client.post(path).status_code, 400)
                response = self.client.post(path, data={"csrf_token": csrf, "address": "198.51.100.99"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["outcome"], "accepted")
                self.assertEqual(self.client.get(path).status_code, 405)
            reset.assert_awaited_once_with("192.0.2.42", "1161", "reset-test-secret")
            discovered_reset.assert_awaited_once_with("192.0.2.43", 161, "reset-test-secret")
            with closing(self.storage.connect()) as con:
                logs = con.execute("SELECT * FROM modem_reboot_log ORDER BY id DESC LIMIT 2").fetchall()
            self.assertEqual([row["username"] for row in logs], ["Admin", "Admin"])
            self.assertEqual([row["outcome"] for row in logs], ["accepted", "accepted"])
            self.assertEqual([row["address"] for row in logs], ["192.0.2.43", "192.0.2.42"])
            journal = self.client.get("/settings/modem-reboots")
            self.assertEqual(journal.status_code, 200)
            self.assertIn("Команда подтверждена", journal.text)
            self.assertNotIn("reset-test-secret", journal.text)
            resolve.assert_awaited_once_with({"id": 1}, "cc3540e608b1")
            self.assertEqual(self.client.post(paths[0].replace("42", "99"), data={"csrf_token": csrf}).status_code, 404)
            resolve.return_value = {**modem, "ip": None}
            self.assertEqual(self.client.post(paths[1], data={"csrf_token": csrf}).status_code, 422)
            self.assertEqual(discovered_reset.await_count, 1)
            with patch("routes_cmts.verified_modem", AsyncMock(return_value={**modem, "ip": None})):
                self.assertNotIn('data-modem-reboot-url=', self.client.get("/cmts/1/modems/cc3540e608b1").text)
            for page_path in ("/devices/Modems/42", "/cmts/1/modems/cc3540e608b1"):
                page = self.client.get(page_path)
                self.assertIn('data-modem-reboot-url="/api/', page.text)
                self.assertIn('src="/static/modem-reboot.js?', page.text)
                self.assertNotIn('src="http://', page.text)
            self.assertEqual(self.client.get("/static/modem-reboot.js").status_code, 200)
            resolve.return_value = modem
            for role in ("execute", "read"):
                username = "Reboot" + role
                with closing(self.storage.connect()) as con:
                    con.execute("INSERT INTO users(username,password_hash,role) VALUES (?,?,?)",
                                (username, self.storage.hash_password("reboot-password-123"), role))
                    con.commit()
                self.client.post("/logout", data={"csrf_token": csrf})
                self.login(username, "reboot-password-123")
                csrf = self.token("/profile")
                for path in paths:
                    self.assertEqual(self.client.post(path, data={"csrf_token": csrf}).status_code,
                                     200 if role == "execute" else 403)
                page = self.client.get("/devices/Modems/42")
                self.assertEqual('data-modem-reboot-url=' in page.text, role == "execute")
                page = self.client.get("/cmts/1/modems/cc3540e608b1")
                self.assertEqual('data-modem-reboot-url=' in page.text, role == "execute")
                self.assertEqual(self.client.post(setting_path, data={"csrf_token": csrf}).status_code, 403)
                self.assertEqual(self.client.get("/settings/modem-reboots").status_code, 403)
                resolve.return_value = modem
        self.client.post("/logout", data={"csrf_token": csrf})
        self.login()
        self.client.post(setting_path, data={"csrf_token": self.token("/settings/cmts"), "clear_community": "1"})
        self.assertEqual(self.storage.snmp_write_community_for("Modems"), "private")

    def test_modem_status_is_authorized_and_assets_are_relative(self):
        path = "/api/devices/Modems/42/modem-status"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get("/api/cmts/1/modems/cc3540e608b1/modem-status").status_code, 401)
        self.login()
        device = {"id": "42", "name": "Modem", "technical_name": "cc3540e608b1",
                  "address": "192.0.2.42", "snmp_address": "192.0.2.42", "snmp_port": "161"}
        cmts = {"id": 1, "community": "private-test"}
        with (patch("routes_devices.host_rows", return_value=[device]),
              patch("routes_devices.cmts_list", return_value=[{"id": 1}]),
              patch("routes_devices.cmts_for", return_value=cmts),
              patch("routes_devices.device_descriptions", return_value={}),
              patch("routes_devices.modem_status_for", AsyncMock(return_value={"status": "p-online"})) as poll):
            response = self.client.get(path)
            self.assertEqual(response.json()["status"], "p-online")
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(poll.await_args.kwargs["device"], device)
            self.assertNotIn("private-test", response.text)
            self.assertEqual(self.client.get(path.replace("42", "99")).status_code, 404)
            self.assertEqual(self.client.get(path.replace("Modems", "VOIP")).status_code, 404)
            self.assertEqual(poll.await_count, 1)
            page = self.client.get("/devices/Modems/42")
            self.assertIn('data-modem-status-url="' + path + '"', page.text)
            self.assertRegex(page.text, r'src="/static/modem-status\.js\?v=\d+"')
            self.assertNotIn('src="http://', page.text)
        self.assertEqual(self.client.get("/static/modem-status.js").status_code, 200)

    def test_discovered_status_does_not_depend_on_modem_ip_or_verified_cache(self):
        self.login()
        path = "/api/cmts/1/modems/cc3540e608b1/modem-status"
        with (patch("routes_cmts.cmts_for", return_value={"id": 1}),
              patch("routes_cmts.modem_status_for", AsyncMock(return_value={"status": "init(d)"})) as poll,
              patch("routes_cmts.verified_modem", AsyncMock()) as verify):
            result = self.client.get(path)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["status"], "init(d)")
            verify.assert_not_awaited()
            self.assertEqual(poll.await_args.kwargs["mac"], "cc3540e608b1")
            self.assertEqual(self.client.get(path.replace("cc3540e608b1", "ab")).status_code, 422)
        modem = {"cmts_id": 1, "cmts_name": "CMTS", "mac": "cc:35:40:e6:08:b1",
                 "mac_compact": "cc3540e608b1", "ip": None}
        with patch("routes_cmts.verified_modem", AsyncMock(return_value=modem)):
            page = self.client.get("/cmts/1/modems/cc3540e608b1")
        self.assertEqual(page.status_code, 200)
        self.assertIn('data-modem-status-url="' + path + '"', page.text)
        self.assertRegex(page.text, r'src="/static/modem-status\.js\?v=\d+"')
        self.assertNotIn('src="http://', page.text)

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
              patch("routes_devices.amplifier_list_optical_levels", return_value={}),
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
            self.assertIn('class="panel device-side-panel optical-panel"', detail.text)
            self.assertLess(detail.text.index('id="ping-monitor"'),
                            detail.text.index('id="optical-monitor"'))
            self.assertLess(detail.text.index('id="optical-monitor"'),
                            detail.text.index('class="panel device-details"'))
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
            self.assertIn('href="/static/switch.css?', detail.text)
            self.assertEqual(self.client.get("/static/switch.css").status_code, 200)
            self.assertIn('id="switch-traffic-down"', detail.text)
            self.assertIn('id="switch-traffic-up"', detail.text)
            self.assertIn('id="switch-port-pvid"', detail.text)
            self.assertIn('id="switch-port-macs"', detail.text)
            self.assertNotRegex(detail.text, r'(?:src|href)="http://[^\"]*/static/')
            self.assertEqual(self.client.get("/static/switch.js").status_code, 200)
            response = self.client.get("/api/devices/Switches/42/switch-ports")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["ports"][0]["state"], "fast")
            self.assertEqual(poll.call_args.args,
                              ("42", "192.0.2.42", "161", "public",
                               ("vlan", "aux", "loop")))
            self.assertEqual(self.client.get(
                "/api/devices/Modems/42/switch-ports").status_code, 404)

    def test_switch_pvid_matches_exact_port_and_rejects_missing_or_ambiguous_data(self):
        item = {"itemid": "1", "name": "Interface Gi1/0/8: PVID", "key_": "pvid[8]",
                "lastclock": "100", "lastvalue": "42", "status": "0", "state": "0"}
        select = self.zabbix.switch_port_pvid
        self.assertEqual(select([item], 108, "Gi1/0/8"), 42)
        self.assertIsNone(select([item], 108, "Gi1/0/80"))
        self.assertIsNone(select([item, {**item, "itemid": "2"}], 108, "Gi1/0/8"))
        for changes in ({"lastclock": "0"}, {"status": "1"}, {"state": "1"},
                        {"lastvalue": "n/a"}, {"lastvalue": "4095"}):
            self.assertIsNone(select([{**item, **changes}], 108, "Gi1/0/8"))
        generic = {**item, "name": "PVID port", "snmp_oid": "1.3.6.1.2.1.17.7.1.4.5.1.1.3"}
        self.assertEqual(select([generic], 108, "Gi1/0/8", {(3,): 108}), 42)
        self.assertIsNone(select([generic], 3, "Gi1/0/3", {(3,): 108}))
        with patch.object(self.zabbix, "zabbix_call", return_value=[item]) as call:
            self.assertEqual(self.zabbix.switch_pvid_items("42"), [item])
        self.assertEqual(call.call_args.args[1]["hostids"], ["42"])

    def test_switch_port_details_checks_access_and_survives_source_errors(self):
        path = "/api/devices/Switches/42/switch-ports/108/details"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.login()
        device = {"id": "42", "snmp_address": "192.0.2.42", "snmp_port": "161"}
        item = {"name": "Gi1/0/8 PVID", "lastvalue": "42", "lastclock": "100"}
        addresses = [f"00:11:22:33:44:{index:02x}" for index in range(6)]
        with (patch("routes_devices.host_rows", return_value=[device]),
              patch("routes_devices.switch_snapshot_for", new_callable=AsyncMock,
                    return_value={"ports": [{"index": 108, "name": "Gi1/0/8"}]}),
              patch("routes_devices.switch_pvid_items", return_value=[item]) as pvid,
              patch("routes_devices.mac_ports_for", new_callable=AsyncMock,
                    return_value={"ports": {108: addresses}, "bridge_ports": {(3,): 108},
                                  "truncated": False}) as macs,
              patch("routes_devices.lookup_macs", side_effect=lambda values: [
                  {"mac": value, "vendor": "Example vendor", "first_seen": "2010-01-01",
                   "kind": "known"} for value in values]) as vendors):
            with patch("routes_devices.require_user", return_value={"role": "read"}):
                response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["pvid"], 42)
            self.assertEqual(response.json()["macs"], addresses[:5])
            self.assertTrue(response.json()["more_macs"])
            self.assertEqual(len(response.json()["mac_details"]), 5)
            self.assertEqual(response.json()["mac_details"][0]["vendor"], "Example vendor")
            self.assertEqual(response.json()["mac_details"][0]["first_seen"], "2010-01-01")
            vendors.assert_called_once_with(addresses[:5])
            self.assertEqual(macs.await_args.args, ("42", "192.0.2.42", "161", "public"))
            for invalid in (path.replace("/108/", "/9/"), path.replace("/42/", "/99/"),
                            path.replace("/Switches/", "/Modems/")):
                self.assertEqual(self.client.get(invalid).status_code, 404)
            self.assertEqual(macs.await_count, 1)
            pvid.side_effect = RuntimeError("Zabbix unavailable")
            data = self.client.get(path).json()
            self.assertIsNone(data["pvid"])
            self.assertEqual(data["macs"], addresses[:5])
            pvid.side_effect = None
            macs.side_effect = RuntimeError("SNMP timeout")
            data = self.client.get(path).json()
            self.assertEqual(data["pvid"], 42)
            self.assertIsNone(data["macs"])
            self.assertEqual(data["mac_error"], "SNMP timeout")
            macs.side_effect = None
            macs.return_value = {"ports": {}, "bridge_ports": {}, "truncated": False}
            pvid.return_value = []
            data = self.client.get(path).json()
            self.assertIsNone(data["pvid"])
            self.assertEqual(data["macs"], [])
            self.assertFalse(data["more_macs"])

    def test_mac_database_settings_update_requires_admin_and_csrf(self):
        path = "/settings/mac-database/update"
        with patch("routes_settings.update_database", return_value=59000) as update:
            self.assertEqual(self.client.post(path, follow_redirects=False).status_code, 303)
            update.assert_not_called()
            self.login()
            token = self.token("/settings")
            self.assertEqual(self.client.post(path).status_code, 400)
            with patch("app_web.user_for", return_value={"role": "read"}):
                self.assertEqual(self.client.post(path, data={"csrf_token": token}).status_code, 403)
            update.assert_not_called()
            response = self.client.post(path, data={"csrf_token": token})
            self.assertEqual(response.status_code, 200)
            self.assertIn("База MAC обновлена. Префиксов: 59000.", response.text)
            self.assertIn('action="/settings/mac-database/update"', response.text)
            update.assert_called_once_with()
            update.side_effect = RuntimeError("Предыдущая копия сохранена.")
            response = self.client.post(path, data={"csrf_token": token})
            self.assertIn("Предыдущая копия сохранена.", response.text)

    def test_switch_port_traffic_checks_host_port_and_allows_read_role(self):
        path = "/api/devices/Switches/42/switch-ports/8/traffic"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.login()
        device = {"id": "42", "snmp_address": "192.0.2.42", "snmp_port": "161"}
        snapshot = {"ports": [{"index": 8, "name": "Gi1/0/8"}]}
        with (patch("routes_devices.host_rows", return_value=[device]),
              patch("routes_devices.switch_snapshot_for", new_callable=AsyncMock,
                    return_value=snapshot),
              patch("routes_devices.traffic_for", new_callable=AsyncMock,
                    return_value={"down_bps": 2400, "up_bps": 1000}) as traffic):
            with patch("routes_devices.require_user", return_value={"role": "read"}):
                response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"down_bps": 2400, "up_bps": 1000})
            self.assertEqual(traffic.await_args.args,
                             ("42", 8, "192.0.2.42", "161", "public"))
            self.assertEqual(self.client.get(path.replace("/8/", "/9/")).status_code, 404)
            self.assertEqual(self.client.get(path.replace("/42/", "/99/")).status_code, 404)
            self.assertEqual(self.client.get(path.replace("/Switches/", "/Modems/")).status_code, 404)

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

    def test_modem_overview_selects_ds3_or_generic_channel_items(self):
        def item(item_id, label, metric, channel, frequency="", error_rate_id=None):
            return {"id": item_id, "label": label, "metric": metric,
                    "channel": channel, "frequency": frequency,
                    "error_rate_id": error_rate_id, "units": "dB",
                    "value_type": "0"}
        loss = item("9", "Потери", "loss", None)
        multi = [
            item("1", "US1 Level", "level", 1),
            item("2", "US2 Level", "level", 2),
            item("3", "DS2 435MHz SNR", "snr", 2, "435mhz"),
            item("4", "DS3 447MHz Level", "level", 3, "447mhz"),
            item("5", "DS3 447MHz SNR", "snr", 3, "447mhz", "6"),
            item("6", "DS3 ErrorRate", "error_rate", 3),
        ]
        mode, selected, missing = self.zabbix.modem_overview_definitions(multi, loss)
        self.assertEqual(mode, "multi")
        self.assertEqual([(row["label"], row["item"]["id"]) for row in selected], [
            ("US1 Level", "1"), ("DS3 Level", "4"), ("DS3 SNR", "5"),
            ("DS3 ErrorRate", "6"), ("Loss", "9")])
        self.assertEqual(missing, [])
        later_upstreams = [item("16", "US6 Level", "level", 6),
                           item("15", "US5 Level", "level", 5), *multi[2:]]
        mode, selected, missing = self.zabbix.modem_overview_definitions(later_upstreams, loss)
        self.assertEqual(mode, "multi")
        self.assertEqual((selected[0]["label"], selected[0]["item"]["id"]),
                         ("US5 Level", "15"))
        self.assertEqual(missing, [])
        mixed_upstreams = [later_upstreams[0], multi[0], later_upstreams[1], *multi[2:]]
        _, selected, _ = self.zabbix.modem_overview_definitions(mixed_upstreams, loss)
        self.assertEqual((selected[0]["label"], selected[0]["item"]["id"]),
                         ("US1 Level", "1"))
        generic = [item("11", "US Level", "level", 0),
                   item("12", "DS Level", "level", None),
                   item("13", "DS SNR", "snr", None, error_rate_id="14"),
                   item("14", "DS ErrorRate", "error_rate", None)]
        mode, selected, missing = self.zabbix.modem_overview_definitions(generic, loss)
        self.assertEqual(mode, "single")
        self.assertEqual([(row["label"], row["item"]["id"]) for row in selected], [
            ("US Level", "11"), ("DS Level", "12"), ("DS SNR", "13"),
            ("ErrorRate", "14"), ("Loss", "9")])
        self.assertEqual(missing, [])
        mode, selected, missing = self.zabbix.modem_overview_definitions(multi[:3], loss)
        self.assertEqual(mode, "multi")
        self.assertEqual(missing, ["DS3 Level", "DS3 SNR", "DS3 ErrorRate"])

    def test_modem_overview_history_reads_five_zabbix_items(self):
        path = "/api/devices/Modems/42/modem-overview/history"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.login()
        definitions = [
            {"id": "1", "label": "US Level", "metric": "level", "channel": 0,
             "frequency": "", "units": "dBmV", "value_type": "0"},
            {"id": "2", "label": "DS Level", "metric": "level", "channel": None,
             "frequency": "", "units": "dBmV", "value_type": "0"},
            {"id": "3", "label": "DS SNR", "metric": "snr", "channel": None,
             "frequency": "", "units": "dB", "value_type": "0", "error_rate_id": "4"},
            {"id": "4", "label": "DS ErrorRate", "metric": "error_rate",
             "channel": None, "frequency": "", "units": "", "value_type": "3"},
        ]
        loss = {"id": "5", "label": "Потери", "units": "%", "value_type": "0"}
        def history_api(method, params):
            item_id = params["itemids"][0]
            field = "value_avg" if method == "trend.get" else "value"
            return [{"clock": "1799990000", field: item_id}]
        with (patch("routes_devices.host_rows", return_value=[{"id": "42"}]),
              patch("routes_devices.modem_channel_definitions", return_value=definitions),
              patch("routes_devices.ping_loss_definition", return_value=loss),
              patch("routes_devices.zabbix_call", side_effect=history_api) as api):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["mode"], "single")
            self.assertEqual([row["key"] for row in response.json()["series"]],
                             ["us", "ds_level", "ds_snr", "error_rate", "loss"])
            self.assertEqual([row["points"][0]["value"] for row in response.json()["series"]],
                             [1, 2, 3, 4, 5])
            self.assertEqual({call.args[0] for call in api.call_args_list}, {"history.get"})
            ranged = self.client.get(path, params={"period": "14d"})
            self.assertEqual(ranged.status_code, 200)
            self.assertEqual(ranged.json()["to"] - ranged.json()["from"], 1209600)
            self.assertTrue(all(row["aggregation"] == "hourly_average"
                                for row in ranged.json()["series"]))
            self.assertEqual({call.args[0] for call in api.call_args_list},
                             {"history.get", "trend.get"})
            self.assertEqual(self.client.get(path, params={"period": "7d"}).status_code, 422)
            self.assertEqual(self.client.get(path.replace("/42/", "/99/")).status_code, 404)
            self.assertEqual(self.client.get(path.replace("/Modems/", "/VOIP/")).status_code, 404)
            with patch("routes_devices.device_descriptions", return_value={}):
                # A real device row is needed when the card records recent visits.
                with patch("routes_devices.host_rows", return_value=[{
                    "id": "42", "name": "Modem 42", "address": "192.0.2.42"}]):
                    detail = self.client.get("/devices/Modems/42")
            self.assertEqual(detail.status_code, 200)
            self.assertIn('id="modem-overview-open"', detail.text)
            self.assertIn('/static/modem-overview.js', detail.text)
            self.assertEqual(self.client.get('/static/modem-overview.js').status_code, 200)
            self.assertIn('id="victor-open"', detail.text)
            self.assertIn('id="victor-dialog"', detail.text)
            self.assertRegex(detail.text, r'src="/static/victor\.js\?v=\d+"')
            self.assertEqual(self.client.get('/static/victor.js').status_code, 200)

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
              patch("routes_devices.modem_restarts_count_24h", return_value=5),
              patch("zabbix_service.zabbix_call", return_value=items) as api):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["item"]["value"], 3)
            self.assertEqual(response.json()["item"]["id"], "92")
            self.assertEqual(response.json()["count_24h"], 5)
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

    def test_modem_restarts_daily_count_uses_one_value_per_rolling_hour(self):
        self.zabbix.clear_caches()
        now = 1000000
        rows = [
            {"clock": str(now - 300), "value": "2"},
            {"clock": str(now - 600), "value": "9"},
            {"clock": str(now - 3900), "value": "3"},
            {"clock": str(now - 7500), "value": "0"},
            {"clock": str(now - 86500), "value": "50"},
        ]
        item = {"id": "92", "value_type": "3"}
        with patch("zabbix_service.zabbix_call", return_value=rows) as api:
            count = self.zabbix.modem_restarts_count_24h("42", item, now=now)
            self.assertEqual(count, 5)
            self.assertEqual(self.zabbix.modem_restarts_count_24h("42", item, now=now), 5)
            self.assertEqual(api.call_count, 1)
            self.assertEqual(api.call_args.args[0], "history.get")
            self.assertEqual(api.call_args.args[1]["itemids"], ["92"])
            self.assertEqual(api.call_args.args[1]["time_from"], now - 86400)
        with patch("zabbix_service.zabbix_call", return_value=[]):
            self.assertIsNone(self.zabbix.modem_restarts_count_24h("43", item, now=now))

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
            self.assertRegex(detail.text, r'href="/static/onu\.css\?v=\d+"')
            self.assertIn('id="modem-restarts"', detail.text)
            self.assertIn('id="modem-restarts-daily"', detail.text)
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


    def test_cmts_search_resolves_zabbix_names_and_keeps_unmatched_modems(self):
        self.login()
        cmts = {"id": 1, "name": "Test CMTS"}
        modems = [
            {"cmts_id": 1, "cmts_name": "Test CMTS", "mac": mac,
             "mac_compact": mac, "ip": ip}
            for mac, ip in (("001122334455", "192.0.2.1"),
                            ("001122334466", "192.0.2.2"),
                            ("001122334477", None))]
        for field in ("interface_ip", "snmp_address", "address"):
            with (self.subTest(field=field),
                  patch("routes_cmts.cmts_list", return_value=[cmts]),
                  patch("routes_cmts.cmts_for", return_value=cmts),
                  patch("routes_cmts.search_cmts", new_callable=AsyncMock,
                        return_value=(modems, False)),
                  patch("routes_cmts.host_rows", return_value=[
                      {"name": "Named modem", field: "192.0.2.1"},
                      {"name": "Different modem", "address": "192.0.2.10"}
                  ]) as hosts):
                response = self.client.get("/api/cmts/search", params={"q": "001122"})
            self.assertEqual(response.status_code, 200)
            rows = response.json()["results"]
            self.assertEqual([row["name"] for row in rows], ["Named modem", "", ""])
            self.assertEqual([{key: row[key] for key in modem}
                              for row, modem in zip(rows, modems)], modems)
            hosts.assert_called_once_with("Modems")
        with (patch("routes_cmts.cmts_list", return_value=[cmts]),
              patch("routes_cmts.cmts_for", return_value=cmts),
              patch("routes_cmts.search_cmts", new_callable=AsyncMock,
                    return_value=(modems, False)),
              patch("routes_cmts.host_rows", side_effect=RuntimeError("Zabbix unavailable"))):
            response = self.client.get("/api/cmts/search", params={"q": "001122"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], modems)
        self.assertEqual(response.json()["errors"], [
            {"cmts": "Zabbix", "error": "Zabbix unavailable"}])

    def test_cmts_settings_search_and_discovered_card(self):
        self.login()
        csrf = self.token("/settings/cmts")
        response = self.client.post("/settings/cmts", data={
            "csrf_token": csrf, "name": "Test CMTS", "address": "192.0.2.10",
            "city": "Parnu", "port": "161", "community": "test-ro"})
        self.assertIn("CMTS сохранена", response.text)
        self.assertIn('name="city" maxlength="80" value="Parnu"', response.text)
        with closing(self.storage.connect()) as con:
            row = con.execute(
                "SELECT id,city,read_community FROM cmts WHERE name='Test CMTS'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["city"], "Parnu")
        self.assertNotEqual(row["read_community"], "test-ro")
        cmts_id = row["id"]
        self.assertEqual(self.storage.cmts_for(cmts_id)["city"], "Parnu")
        response = self.client.post("/settings/cmts", data={
            "csrf_token": csrf, "cmts_id": str(cmts_id), "name": "Test CMTS",
            "city": "Pärnu", "address": "192.0.2.10", "port": "161"})
        self.assertIn("CMTS сохранена", response.text)
        self.assertEqual(self.storage.cmts_for(cmts_id)["city"], "Pärnu")
        response = self.client.post("/settings/cmts", data={
            "csrf_token": csrf, "cmts_id": str(cmts_id), "name": "Test CMTS",
            "city": "Bad/City", "address": "192.0.2.10", "port": "161"})
        self.assertIn("Укажите город", response.text)
        self.assertEqual(self.storage.cmts_for(cmts_id)["city"], "Pärnu")
        modem = {"cmts_id": cmts_id, "cmts_name": "Test CMTS",
                 "mac": "cc:35:40:e8:ee:64", "mac_compact": "cc3540e8ee64",
                 "ip": "10.19.0.223"}
        with (patch("routes_cmts.search_cmts", new_callable=AsyncMock,
                    return_value=([modem], False)) as lookup,
              patch("routes_cmts.host_rows", return_value=[])):
            response = self.client.get("/api/cmts/search", params={
                "q": "CC35.40E8.EE64", "cmts_id": cmts_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["ip"], "10.19.0.223")
        self.assertEqual(lookup.await_args.args[1], "cc3540e8ee64")
        with patch("routes_cmts.resolve_mac", new_callable=AsyncMock,
                   return_value=modem):
            response = self.client.get(
                f"/cmts/{cmts_id}/modems/CC35.40E8.EE64")
        self.assertEqual(response.status_code, 200)
        self.assertIn("10.19.0.223", response.text)
        self.assertIn("/static/cmts.css", response.text)
        self.assertIn('data-direct-snmp="true"', response.text)
        self.assertEqual(self.client.get("/static/cmts.css").status_code, 200)
        with patch("routes_cmts.basic_channels", new_callable=AsyncMock,
                   return_value={"items": [{"id": "us", "label": "US Level",
                                            "value": "48.5", "units": "dBmV"}],
                                 "updated_at": 1}):
            channels = self.client.get(
                f"/api/cmts/{cmts_id}/modems/cc3540e8ee64/modem-channels")
        self.assertEqual(channels.status_code, 200)
        self.assertEqual(channels.json()["items"][0]["value"], "48.5")
        plan = {"city": "Pärnu", "modem_count": 1, "area_count": 1,
                "groups_to_create": ["Pärnu/modems/mai_37"],
                "assignments": [{"host": "Modem", "mac": "cc3540e8ee64",
                                 "method": "MAC", "from": [],
                                 "group": "Pärnu/modems/mai_37"}],
                "skipped": [], "digest": "test-digest"}
        with patch("routes_settings.build_plan", new_callable=AsyncMock,
                   return_value=plan) as build:
            preview = self.client.post(
                f"/settings/cmts/{cmts_id}/groups/preview",
                data={"csrf_token": csrf})
            self.assertEqual(preview.status_code, 200)
            self.assertIn("Pärnu/modems/mai_37", preview.text)
            self.assertIn('action="/settings/cmts/', preview.text)
            self.assertEqual(build.await_args.args[0]["city"], "Pärnu")
            with patch("routes_settings.apply_plan", return_value=(1, 1)):
                applied = self.client.post(
                    f"/settings/cmts/{cmts_id}/groups/apply",
                    data={"csrf_token": csrf, "digest": "test-digest"})
            self.assertIn("Создано групп: 1", applied.text)
        response = self.client.post(f"/settings/cmts/{cmts_id}/delete",
                                    data={"csrf_token": csrf})
        self.assertIn("CMTS удалена", response.text)


if __name__ == "__main__":
    unittest.main()
