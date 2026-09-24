# Установка одного Android-телефона

Инструкция рассчитана на новую установку. Перед применением к действующему телефону или серверу проверьте конфликты портов, пользователей, boot-скриптов и ключей; установщики не предназначены для обновления существующей конфигурации. Примеры IP в репозитории служат только образцами. Приватные файлы создаются на конкретном телефоне/сервере с правами `0600` и не попадают в Git.

## 0. Подготовка

Помощник в этом проекте имеет `minSdkVersion=34` (Android 14); остальной стек не проверен на всех моделях Android. Нужны Chrome и беспроводная отладка, Termux с работающим запуском `~/.termux/boot`, компьютер для сборки APK (Android SDK platform 35, build-tools 34, JDK 17), Linux VPS с OpenSSH и пользователь, который будет управлять телефоном. На телефоне нужны Python 3, `adb`, OpenSSH, `flock` и wake-lock command Termux; для веб-панели дополнительно Caddy и droidVNC-NG/noVNC. Проверьте наличие этих программ в выбранном дистрибутиве Termux до установки. Для теста после реальной перезагрузки владелец должен иметь физический доступ к телефону.

Получите serial через `adb shell getprop ro.serialno`, а не из маркетингового названия модели. Значение должно совпадать с префиксом mDNS-сервиса `_adb-tls-connect._tcp`. Если серийный номер недоступен, этот вариант моста не подходит без отдельной доработки проверки личности. `foreground_recovery_packages` по умолчанию содержит только Chrome. Добавляйте launcher конкретного телефона, лишь если хотите разрешить возврат Chrome при включённом экране и этом launcher на переднем плане.

На VPS убедитесь, что порты `127.0.0.1:19222` и `127.0.0.1:19223` свободны и правила межсетевого экрана не публикуют их наружу. На телефоне проверьте свободные `8022`, `8443`, `8766`, `5800`, `5900`. Время и модель Android могут менять поведение фоновых процессов; затем выполните реальный reboot-тест.

## 1. Сопряжение ADB и ключи

На телефоне в настройках разработчика включите беспроводную отладку, доверяйте текущей Wi-Fi сети и откройте сопряжение по коду. **В Termux на этом же телефоне** выполните `adb pair <IP:pair-port>`, затем найдите его `_adb-tls-connect._tcp` запись через `adb mdns services`, выполните `adb connect 127.0.0.1:<connect-port>` и убедитесь, что `adb -s 127.0.0.1:<connect-port> get-state` возвращает `device`. IP/порт сопряжения и шестизначный код краткоживущие; не сохраняйте их в Git. Запишите loopback endpoint в `~/agent-phone/adb-endpoint`.

Сгенерируйте на телефоне ключ браузерного туннеля:

```sh
umask 077
mkdir -p ~/agent-phone ~/phone-admin
ssh-keygen -q -t ed25519 -N '' -f ~/agent-phone/tunnel-key
```

На компьютере владельца создайте отдельный Ed25519 ключ входа в Termux и храните **закрытый ключ только у владельца**. Получите проверенный публичный host key VPS и его fingerprint через уже доверенный административный канал. Передайте на телефон публичный ключ владельца, публичный host key VPS и приватный `ssh-owner/inventory.example.json`, где заменены LAN IP и VPS IP. Установите Termux SSH из каталога `ssh-owner/`:

```sh
python ssh-owner/install.py --owner-key /private/owner-key.pub \
  --vps-host-key /private/vps-host-key.pub \
  --inventory /private/owner-inventory.json
```

Скрипт откажет при повторной установке. Он создаёт `~/phone-owner/`, локальный `sshd` на LAN/loopback `8022`, отдельный ключ обратного туннеля и boot entry. Владелец настраивает SSH-клиент по [client.example.conf](ssh-owner/client.example.conf), сверяет host fingerprint по доверенному каналу и проверяет вход `ssh phone`. Не делайте закрытый ключ владельца доступным агенту.

## 2. Ограниченные учётные записи VPS

Передайте на VPS только три **публичных** ключа: `~/agent-phone/tunnel-key.pub`, `~/phone-owner/link-key.pub` с телефона и ключ входа владельца с компьютера. Сначала установите ограничения SSH, затем выдайте ключи. Из активной SSH-сессии администратора запустите оба установщика на VPS (им нужен `SSH_CONNECTION` для проверки эффективных `Match`-правил):

```sh
sudo --preserve-env=SSH_CONNECTION python3 bridge/install_vps.py \
  --link-key /private/browser-link.pub
sudo --preserve-env=SSH_CONNECTION python3 ssh-owner/install_vps.py \
  --owner-key /private/owner-key.pub --link-key /private/owner-link.pub
```

Они создают `browser-link`, `phone-owner-link` и `phone-owner-jump`, проверяют `sshd -T` и выдают только ограниченную пересылку `127.0.0.1:19222`/`19223`. При существующем пользователе/конфиге они останавливаются для ручного разбора. Скрипты не удаляют существующие настройки.

На телефоне добавьте проверенный host key VPS в `~/agent-phone/known_hosts` и убедитесь, что `~/phone-owner/known_hosts` содержит тот же ключ. После настройки VPS создайте `~/phone-owner/tunnel-enabled`: supervisor поднимет владельческий обратный туннель. Проверьте `~/phone-owner/status.json`. Доступ владельца через VPS использует `ssh phone-remote` из клиентского шаблона.

## 3. Мост Chrome

На телефоне поместите `bridge.py` и `policy.py` из `bridge/` в `~/agent-phone/`, а `owner-control/owner_control.py` в `~/phone-admin/`. Создайте `~/agent-phone/config.json` по [bridge/config.example.json](bridge/config.example.json): `serial` = результат `adb shell getprop ro.serialno`, `ssh_target` = `browser-link@<VPS IPv4>`, пакетный список = одобренный `foreground_recovery_packages`. **Всегда** создайте `~/phone-admin/control.json` по [owner-control/control.example.json](owner-control/control.example.json) с тем же serial, точным LAN IP, соответствующим `origin` и отдельным случайным токеном: проверка возвращения экрана использует этот файл даже без веб-панели. Запишите точный ADB endpoint в `~/agent-phone/adb-endpoint`; режим файлов `0600`, каталогов `0700`. Мост проверяет `ro.serialno` при каждом цикле и только mDNS-сервис этого serial при смене порта.

Скопируйте `bridge/10-agent-phone` в `~/.termux/boot/10-agent-phone`, поставьте режим `0700` и запустите его один раз для первичной проверки. Установленный boot-провайдер Termux должен действительно запускать скрипты после включения устройства; проверьте это реальной перезагрузкой. Мост выводит `~/agent-phone/status.json` и лог `runner.log`.

## 4. Помощник для восстановления ADB

На компьютере соберите временный debuggable APK:

```sh
PHONE_ADB_PROVISIONING_BUILD=1 adb-recovery/build.sh
scp "$HOME/.local/share/phone-adb-recovery/phone-adb-recovery.apk" phone:agent-phone/provisioning.apk
```

Укажите `ANDROID_SDK_ROOT` и при необходимости `PHONE_ADB_JAVA_HOME`; signing directory по умолчанию находится **вне Git** в `~/.local/share/phone-adb-recovery`. Сохраните keystore и пароль: тот же ключ обязателен для обновлений. На телефоне в Termux установите временный APK через **тот же точный ADB endpoint**, сгенерируйте секрет локально и передайте его в приватный каталог приложения через stdin:

```sh
umask 077
endpoint=$(cat ~/agent-phone/adb-endpoint)
adb -s "$endpoint" install ~/agent-phone/provisioning.apk
python -c 'import secrets; print(secrets.token_hex(32))' > ~/agent-phone/recovery-token
adb -s "$endpoint" shell -T "run-as dev.agentphone.adbrecovery sh -c 'umask 077; mkdir -p files; cat > files/command-token'" < ~/agent-phone/recovery-token
```

`recovery-token` содержит ровно 64 hex-символа и завершающий LF; при передаче через stdin токен не попадает в аргументы shell. Соберите обычный APK **тем же** `build.sh` без `PHONE_ADB_PROVISIONING_BUILD`, передайте его на телефон как `~/agent-phone/release.apk`, затем в Termux:

```sh
endpoint=$(cat ~/agent-phone/adb-endpoint)
adb -s "$endpoint" install -r ~/agent-phone/release.apk
adb -s "$endpoint" shell pm grant dev.agentphone.adbrecovery android.permission.WRITE_SECURE_SETTINGS
```

Проверьте, что установленная версия не допускает `run-as`, а право осталось выданным. Удалите временные APK с телефона после проверки; signing directory на компьютере сохраните. Не кладите токен в логи, чат или репозиторий. Поставьте `adb-recovery/watchdog.py` как `~/agent-phone/adb_recovery_watchdog.py`, а `adb-recovery/20-agent-phone-adb-recovery` как `~/.termux/boot/20-agent-phone-adb-recovery` с режимом `0700`; запустите boot entry один раз. Это автоматизирует восстановление прежней доверенной сети; новое сопряжение и новое доверие Wi-Fi требуют владельца.

## 5. Дополнительные возможности

- **Панель владельца.** Используйте уже созданный `~/phone-admin/control.json`. Скопируйте `owner-control/owner-session.js` в `~/phone-admin/static/`, а `owner-control/render_caddy.py` и `owner-control/Caddyfile.template` — рядом друг с другом в `~/phone-admin/`. Настройте droidVNC-NG локально на `5800/5900`, Caddy и TLS сертификат с SAN для LAN IP. Сгенерируйте bcrypt через `caddy hash-password`, сохраните хеш в приватном файле и запустите `python ~/phone-admin/render_caddy.py` с `--control`, `--hash-file`, `--username`, `--termux-home` и `--output ~/phone-admin/Caddyfile`. Скрипт не перезаписывает существующий файл и не запускает shell expansion для `$` в хеше. Выполните `caddy validate --config ~/phone-admin/Caddyfile --adapter caddyfile`, затем установите `owner-control/19-phone-owner-control` и `owner-control/20-phone-admin` в `~/.termux/boot/`. Проверьте сертификат без `curl -k`; пароли панели и VNC, токен helper должны различаться.
- **Очередь агента.** Используйте только с совместимым `browser-use` CLI по [concurrency/README.md](concurrency/README.md). Поставьте отдельный shim и приватный config; не выдавайте агенту SSH/панель владельца.
- **Очиститель вкладок.** [tab-cleaner/](tab-cleaner/) на VPS закрывает все page target спустя 12 часов наблюдения; включайте только если такая политика подходит задачам. Установите service/timer с проверенным CDP loopback.

Завершите [проверки и fault-сценарии](OPERATIONS.md) до передачи телефона агентам.
