"""CMTS upstream and Zabbix group planning tests."""
import unittest
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import cmts_groups


class _Value:
    def __init__(self, value):
        self.value = value

    def asOctets(self):
        return self.value if isinstance(self.value, bytes) else str(self.value).encode()

    def prettyPrint(self):
        return str(self.value)

    def __int__(self):
        return int(self.value)


class CmtsGroupTests(unittest.IsolatedAsyncioTestCase):
    async def test_docsis3_channels_and_legacy_upstream_join_by_modem_index(self):
        tables = [
            {"3961": _Value("mai_37"), "3962": _Value("mai_37"),
             "4705": _Value("side_9/tammsaare_32")},
            {"2457601": _Value(bytes.fromhex("0024d1aad405")),
             "2966380": _Value(bytes.fromhex("cc3540e8ee64"))},
            {"2457601": _Value("192.0.2.1"), "2966380": _Value("192.0.2.2")},
            {"2457601": _Value(3961)},
            {"2457601.3961": _Value(1), "2457601.3962": _Value(1),
             "2966380.4705": _Value(1)},
        ]
        with patch.object(cmts_groups, "_target", AsyncMock(return_value=object())), \
             patch.object(cmts_groups, "SnmpEngine", MagicMock()), \
             patch.object(cmts_groups, "_walk", AsyncMock(side_effect=tables)):
            result = await cmts_groups.scan({"address": "192.0.2.10", "port": 161,
                                             "community": "public"})
        by_mac = {modem["mac"]: modem for modem in result["modems"]}
        self.assertEqual(by_mac["0024d1aad405"]["alias"], "mai_37")
        self.assertEqual(by_mac["0024d1aad405"]["ifindices"], [3961, 3962])
        self.assertEqual(by_mac["cc3540e8ee64"]["alias"], "side_9/tammsaare_32")

    def test_plan_preserves_unrelated_groups_and_skips_ambiguous_modem(self):
        snapshot = {"aliases": ["mai_37", "side_9/tammsaare_32"], "modems": [
            {"mac": "0024d1aad405", "ip": "192.0.2.1", "alias": "mai_37",
             "index": "2457601", "ifindices": [3961], "reason": ""},
            {"mac": "cc3540e8ee64", "ip": "192.0.2.2", "alias": "",
             "index": "2966380", "ifindices": [4705, 4706],
             "reason": "нет единого описания upstream"},
        ]}
        hosts = [{"hostid": "42", "host": "00:24:d1:aa:d4:05", "name": "Modem 42",
                  "inventory": {}, "interfaces": [{"ip": "192.0.2.1"}],
                  "groups": [{"groupid": "1", "name": "Modems"},
                             {"groupid": "2", "name": "Parnu/modems/old"}]}]
        groups = [{"groupid": "2", "name": "Parnu/modems/old"}]
        plan = cmts_groups.make_plan("Parnu", snapshot, hosts, groups)
        self.assertEqual(plan["groups_to_create"], ["Parnu/modems/mai_37"])
        self.assertEqual(plan["area_count"], 1)
        self.assertEqual(plan["assignments"][0]["remove_groupids"], ["2"])
        self.assertEqual(plan["assignments"][0]["from"], ["Parnu/modems/old"])
        self.assertEqual(len(plan["skipped"]), 1)
        self.assertEqual(plan["skipped"][0]["reason"],
                         "нет единого описания upstream")

    def test_no_group_is_created_without_a_matching_zabbix_modem(self):
        snapshot = {"modems": [{"mac": "0024d1aad405", "ip": None,
                                "alias": "mai_37", "index": "2457601",
                                "ifindices": [3961], "reason": ""}]}
        plan = cmts_groups.make_plan("Parnu", snapshot, [], [])
        self.assertEqual(plan["groups_to_create"], [])
        self.assertEqual(plan["assignments"], [])
        self.assertEqual(plan["area_count"], 0)
        self.assertEqual(plan["skipped"][0]["reason"], "хост Zabbix не найден")

    def test_apply_adds_target_before_removing_only_old_city_group(self):
        plan = {"city": "Parnu", "groups_to_create": ["Parnu/modems/mai_37"],
                "assignments": [{"hostid": "42", "group": "Parnu/modems/mai_37",
                                 "has_target": False, "remove_groupids": ["2"]}]}
        def api(method, params):
            if method == "hostgroup.get":
                return []
            if method == "hostgroup.create":
                return {"groupids": ["3"]}
            return {"groupids": ["3"]}
        call = MagicMock(side_effect=api)
        with patch.dict(sys.modules, {"zabbix_service": SimpleNamespace(zabbix_call=call)}):
            self.assertEqual(cmts_groups.apply_plan(plan), (1, 1))
        self.assertEqual([entry.args[0] for entry in call.call_args_list], [
            "hostgroup.get", "hostgroup.create", "hostgroup.massadd",
            "hostgroup.massremove"])
        self.assertEqual(call.call_args_list[-1].args[1]["groupids"], ["2"])


if __name__ == "__main__":
    unittest.main()
