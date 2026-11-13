# angie-top

angie-top - терминальный аналог Angie Console Light поверх REST API (/status/).
Зависимости: только стандартная библиотека Python 3.8+ (curses, urllib).
Структура JSON соответствует документации модуля http_api Angie 1.12.

## Установка и запуск

```bash
sudo install -m 755 angie_top.py /usr/local/bin/angie-top

angie-top                                              # http://127.0.0.1:8080/status/
angie-top -u http://127.0.0.1:8080/status/ -i 2        # опрос раз в 2 с
angie-top -u https://edge/status/ --user mon --password secret -k
angie-top -u http://127.0.0.1:8080/status/ --once --no-color   # снимок в stdout
angie-top -u http://127.0.0.1:8080/status/ --json              # сырой JSON
```

## Пример вывода (`--once`)

```
Angie 1.12.4   address 192.168.16.5   generation 1   reloaded 2h5m ago

CONNECTIONS
  active         5  ▇█   idle 3
  accepted 2.5k (+239/s)
  dropped  4 (+4.0/s)

HTTP  (server zones: 1)
  RPS      3.0k/s  ▁█   in 175.1KB/s  out 13.3MB/s
  2xx/s 2.8k   3xx/s 19.9   4xx/s 59.8   5xx/s 10.0   discarded/s 4.0

ALERTS
  ! connections dropped +4.0/s - check worker_connections / nofile
  ! HTTP 5xx responses +10.0/s
  ! upstream backend: peer 10.0.0.11:8080 is unavailable (fails=14)
  ! slab zone limit_req_zone: 2 allocation fails - enlarge zone

▲ backend  peers 1/2 up  keepalive 2
  PEER                  STATE         CUR/MAX   SEL/s   2xx/s  4xx/s  5xx/s  FAILS UNAVL DOWNTIME
  10.0.0.11:8080        unavailable       0/-    19.9       0      0    6.0     14     3    47.0s
  192.168.16.4:80       up                4/5    1.4k    1.4k      0      0      0     0      0ms
```
Клавиши в TUI: `1–6` вкладки, `←/→`/`Tab` переключение, `↑/↓`/`j/k` прокрутка, `Space` пауза, `+/−` интервал, `r` обновить, `?` справка, `q` выход.

## Примечания

- Нужен опубликованный API:
  ```nginx
  location /status/ { api /status/; allow 127.0.0.1; deny all; }
  ```
  и `status_zone` в `server`/`location`, `zone` в `upstream` — иначе соответствующие разделы будут пустыми (утилита это корректно показывает).
- Скорости появляются со второго опроса; `--snapshot` делает один запрос и показывает только накопленные totals.
- Поля Angie PRO (`queue`, `header_time`/`response_time`, состояния `checking`/`unhealthy`) отображаются автоматически, если присутствуют в ответе.
