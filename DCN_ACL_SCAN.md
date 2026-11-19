# DCN ACL scan

Скрипт `dcn_acl_scan.py` сканирует переданные IPv4-сети через SNMPv2c,
распознаёт DCN по sysObjectID enterprise `1.3.6.1.4.1.6339` и выводит sysDescr.
Дополнительно требует в sysDescr модель Sxxxx / DCS-xxxx либо слово switch,
чтобы не применять команды к беспроводному оборудованию того же производителя.
Он проверяет все IP в указанных сетях, а не только узлы, известные Zabbix.
SNMP timeout не позволяет отличить выключенный узел от неверного community.

## Подготовка

Python 3.10 или новее:

```bash
python -m pip install -r requirements-dcn-acl.txt
```

В начале скрипта заполнить `TELNET_USERNAME`, `TELNET_PASSWORD` и, при
необходимости, `ENABLE_PASSWORD`. Можно вместо этого задать переменные
`DCN_TELNET_USERNAME`, `DCN_TELNET_PASSWORD`, `DCN_ENABLE_PASSWORD`.
Не коммитьте реальные пароли. Telnet передаёт их без шифрования.

Community уже получен из DCN-узла в Zabbix и записан в локальный
`.env.dcn_acl` рядом со скриптом. Этот файл исключён из Git.
При переносе на другой сервер перенесите его отдельно либо создайте:

```dotenv
DCN_SNMP_COMMUNITY="your-community"
DCN_SCAN_NETWORKS="10.255.82.0/24 10.255.83.0/24"
DCN_TELNET_USERNAME=""
DCN_TELNET_PASSWORD=''
DCN_ENABLE_PASSWORD=''
```

Сети можно заполнить в `DCN_SCAN_NETWORKS` через пробел, запятую или `;`.
Тогда `python dcn_acl_scan.py` запускает только SNMP-поиск по сетям из файла.
Сети в аргументах командной строки имеют приоритет над файлом.
Пустые Telnet-реквизиты не мешают поиску; они нужны только с `--apply`.

## Запуск

Только поиск, без Telnet и изменений:

```bash
python dcn_acl_scan.py 10.255.82.0/24 10.255.83.0/24
```

Проверка применения на одном IP, затем на выбранных сетях:

```bash
python dcn_acl_scan.py 10.255.82.20/32 --apply
python dcn_acl_scan.py 10.255.82.0/24 10.255.83.0/24 --apply
```

Применяется команда `access-list 6 permit 10.18.90.0 0.0.0.255`.
Последовательность: login → при необходимости enable → terminal length 0 → show running-config →
config → ACL → end → проверка running-config → write →
проверка show startup-config. Уже существующая строка повторно не добавляется,
но конфигурация сохраняется. `write` сохраняет всю текущую конфигурацию свитча.
Подтверждение сохранения и постраничный вывод обрабатываются автоматически.
Команды `CONFIG_COMMAND`, `SAVE_COMMAND`, `SHOW_RUNNING`, `SHOW_STARTUP`
можно адаптировать в начале скрипта под версию DCN CLI.

Отчёт: `outputs/dcn_acl_report.json`; можно задать `--report путь.json`.
Не содержит паролей, community или полного конфига. Новый запуск перезаписывает
отчёт с тем же именем. `added_and_saved` / `saved_existing` означают, что строка
найдена и в startup-config. При ошибке указывается этап; изменения, уже
внесённые в running-config, автоматически не откатываются. Остальные найденные
устройства продолжают обрабатываться. Ctrl+C прекращает работу, уже выполненные
изменения остаются.

SNMP опрашивается параллельно (`--workers 32`), Telnet последовательно.
Лимит по умолчанию: 65536 адресов (`--max-hosts`). Пересекающиеся сети
объединяются. Доступны `--snmp-timeout`, `--cli-timeout`, `--snmp-port`,
`--telnet-port`, `--permit-network` и `--help`.

На реальных свитчах применение скрипта при разработке не запускалось;
автоматические проверки используют имитацию CLI.
