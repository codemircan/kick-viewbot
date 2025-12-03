import sys
import time
import random
import datetime
import threading
import asyncio
import json
import os
from threading import Thread, Semaphore, Lock
from itertools import cycle
from fake_useragent import UserAgent
import tls_client

# websocket-client (sync) used for proxyable websocket connections
from websocket import create_connection, WebSocketTimeoutException, WebSocketConnectionClosedException

CLIENT_TOKEN = "e1393935a959b4020a4491574f6490129f678acdaa92760471263db43487f823"

channel = ""
channel_id = None
stream_id = None
max_threads = 0
threads = []
thread_limit = None
active = 0
stop = False
start_time = None
lock = Lock()
connections = 0
attempts = 0
pings = 0
heartbeats = 0
viewers = 0
last_check = 0

# ----------------------------
# Proxy helpers
# ----------------------------
PROXY_FILE = "proxies.txt"
_proxy_cycle = None
_proxy_list = []

def load_proxies(path=PROXY_FILE):
    global _proxy_list, _proxy_cycle
    if not os.path.isfile(path):
        print(f"Proxy file not found: {path}")
        sys.exit(1)

    proxies = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            try:
                host, port, user, pwd = line.split(":")
                proxies.append({
                    "host": host,
                    "port": int(port),
                    "user": user,
                    "pass": pwd,
                    "url": f"http://{user}:{pwd}@{host}:{port}"
                })
            except ValueError:
                print(f"Skipping invalid proxy line: {line}")
                continue

    if not proxies:
        print("No valid proxies found in file. Please use 'host:port:user:pass' format.")
        sys.exit(1)

    _proxy_list = proxies
    # We will use random.choice instead of cycle for better distribution on errors
    print(f"Loaded {len(proxies)} proxies.")
    return proxies

def get_next_proxy():
    global _proxy_list
    if not _proxy_list:
        load_proxies()
    # Return a random proxy from the list
    return random.choice(_proxy_list)

def prepare_proxy_dict(proxy):
    """
    Returns proxies dict suitable for tls_client (requests-like):
    {"http": "...", "https": "..."}
    """
    url = proxy.get("url")
    return {"http": url, "https": url}

def prepare_proxy_auth_header(proxy):
    """
    Returns Proxy-Authorization header value for HTTP CONNECT when needed (Basic ...)
    If proxy has no credentials returns None.
    """
    user = proxy.get("user")
    pwd = proxy.get("pass")
    if not user:
        return None
    import base64
    token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    return f"Basic {token}"

# ----------------------------
# Utility helpers (existing logic preserved, but using proxies)
# ----------------------------
def clean_channel_name(name):
    if "kick.com/" in name:
        parts = name.split("kick.com/")
        channel_local = parts[1].split("/")[0].split("?")[0]
        return channel_local.lower()
    return name.lower()

def get_channel_info(name, proxy):
    global channel_id, stream_id

    proxies = prepare_proxy_dict(proxy)
    s = tls_client.Session(client_identifier="chrome_120", random_tls_extension_order=True)
    s.proxies = proxies
    s.headers.update({
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Referer': 'https://kick.com/',
        'Origin': 'https://kick.com',
        'DNT': '1',
        'Connection': 'keep-alive',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
    })

    # try API v2
    response = s.get(f'https://kick.com/api/v2/channels/{name}', timeout=15)
    if response.status_code == 200:
        data = response.json()
        channel_id = data.get("id")
        if 'livestream' in data and data['livestream']:
            stream_id = data['livestream'].get('id')
        return channel_id

    # try API v1
    response = s.get(f'https://kick.com/api/v1/channels/{name}', timeout=15)
    if response.status_code == 200:
        data = response.json()
        channel_id = data.get("id")
        if 'livestream' in data and data['livestream']:
            stream_id = data['livestream'].get('id')
        return channel_id

    # fallback to scraping page
    response = s.get(f'https://kick.com/{name}', timeout=15)
    if response.status_code == 200:
        import re
        patterns = [
            r'"id":(\d+).*?"slug":"' + re.escape(name) + r'"',
            r'"channel_id":(\d+)',
            r'channelId["\']:\s*(\d+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, response.text, re.IGNORECASE)
            if match:
                channel_id = int(match.group(1))
                break

        stream_patterns = [
            r'"livestream":\s*\{[^}]*"id":(\d+)',
            r'livestream.*?"id":(\d+)',
        ]
        for pattern in stream_patterns:
            match = re.search(pattern, response.text, re.IGNORECASE | re.DOTALL)
            if match:
                stream_id = int(match.group(1))
                break

        if channel_id:
            return channel_id

    # If we reach here, all attempts failed
    raise Exception(f"Failed to get channel info for {name} with proxy {proxy['url']}")

def get_token(proxy):
    proxies = prepare_proxy_dict(proxy)
    s = tls_client.Session(client_identifier="chrome_120", random_tls_extension_order=True)
    s.proxies = proxies
    s.headers.update({
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'Accept-Language': 'en-US,en;q=0.9',
        'Connection': 'keep-alive',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Upgrade-Insecure-Requests': '1',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
    })

    s.get("https://kick.com", timeout=15)
    s.headers["X-CLIENT-TOKEN"] = CLIENT_TOKEN
    response = s.get('https://websockets.kick.com/viewer/v1/token', timeout=15)
    if response.status_code == 200:
        data = response.json()
        token = data.get("data", {}).get("token")
        if token:
            return token

    # Fallback endpoints if the primary one fails
    endpoints = [
        'https://kick.com/api/websocket/token',
        'https://kick.com/api/v1/websocket/token'
    ]
    for endpoint in endpoints:
        response = s.get(endpoint, timeout=15)
        if response.status_code == 200:
            data = response.json()
            token = data.get("data", {}).get("token") or data.get("token")
            if token:
                return token

    raise Exception(f"Failed to get a viewer token with proxy {proxy['url']}")

def get_viewer_count():
    global viewers, last_check
    if not stream_id:
        return 0
    try:
        proxy = get_next_proxy()
        proxies = prepare_proxy_dict(proxy)
        s = tls_client.Session(client_identifier="chrome_120", random_tls_extension_order=True)
        s.proxies = proxies
        s.headers.update({
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Referer': 'https://kick.com/',
            'Origin': 'https://kick.com',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
        })
        url = f"https://kick.com/current-viewers?ids[]={stream_id}"
        response = s.get(url, timeout=15)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list) and len(data) > 0:
                viewers = data[0].get('viewers', 0)
                last_check = time.time()
                return viewers
        return 0
    except Exception:
        return 0

# ----------------------------
# Stats printer (preserved)
# ----------------------------
def show_stats():
    global stop, start_time, connections, attempts, pings, heartbeats, viewers, last_check
    os.system('cls' if os.name == 'nt' else 'clear')

    while not stop:
        try:
            now = time.time()
            if now - last_check >= 5:
                get_viewer_count()

            with lock:
                if start_time:
                    elapsed = datetime.datetime.now() - start_time
                    duration = f"{int(elapsed.total_seconds())}s"
                else:
                    duration = "0s"

                ws_count = connections
                ws_attempts = attempts
                ping_count = pings
                heartbeat_count = heartbeats
                stream_display = stream_id if stream_id else 'N/A'
                viewer_display = viewers if viewers else 'N/A'

            print("\033[3A", end="")
            print(f"\033[2K\r[x] Connections: \033[32m{ws_count}\033[0m | Attempts: \033[32m{ws_attempts}\033[0m")
            print(f"\033[2K\r[x] Pings: \033[32m{ping_count}\033[0m | Heartbeats: \033[32m{heartbeat_count}\033[0m | Duration: \033[32m{duration}\033[0m | Stream ID: \033[32m{stream_display}\033[0m")
            print(f"\033[2K\r[x] Viewers: \033[32m{viewer_display}\033[0m | Updated: \033[32m{time.strftime('%H:%M:%S', time.localtime(last_check))}\033[0m")
            sys.stdout.flush()
            time.sleep(1)
        except Exception:
            time.sleep(1)

# ----------------------------
# Connection / websocket logic
# ----------------------------
def connect():
    send_connection()

def send_connection():
    global active, attempts, channel_id, thread_limit

    with lock:
        attempts += 1
    active += 1

    try:
        # We will try up to the number of available proxies to find one that works
        # This prevents infinite loops if all proxies are bad
        max_retries = len(_proxy_list)
        for i in range(max_retries):
            proxy = get_next_proxy() # Get a random proxy
            try:
                # Step 1: Get channel info if not already set
                if not channel_id:
                    get_channel_info(channel, proxy)
                    if channel_id:
                         print(f"Successfully got Channel ID: {channel_id} using proxy {proxy['host']}")

                # Step 2: Get a viewer token
                token = get_token(proxy)

                # Step 3: Connect to WebSocket
                websocket_handler_sync(token, proxy)

                # If all steps succeed, break the loop
                break

            except Exception as e:
                # If it's the last attempt, log the final failure
                if i == max_retries - 1:
                    pass
                continue # Try the next proxy

    finally:
        active -= 1
        try:
            thread_limit.release()
        except Exception:
            pass

def websocket_handler_sync(token, proxy):
    """
    Uses websocket-client's create_connection with HTTP proxy params.
    Maintains basic ping loop similar to original.
    """
    global connections, stop, channel_id, heartbeats, pings
    connected = False
    ws_url = f"wss://websockets.kick.com/viewer/v1/connect?token={token}"

    proxy_auth = (proxy["user"], proxy["pass"])

    try:
        # create_connection supports http_proxy_host/http_proxy_port and http_proxy_auth
        # timeouts provided to avoid hanging threads
        ws = create_connection(
            ws_url,
            http_proxy_host=proxy["host"],
            http_proxy_port=proxy["port"],
            http_proxy_auth=proxy_auth,
            timeout=20,
            enable_multithread=True
        )
        with lock:
            connections += 1
        connected = True

        # perform handshake (preserve original handshake message)
        handshake = {
            "type": "channel_handshake",
            "data": {"message": {"channelId": channel_id}}
        }
        try:
            ws.send(json.dumps(handshake))
            with lock:
                heartbeats += 1
        except Exception:
            pass

        ping_count = 0
        while not stop and ping_count < 10:
            ping_count += 1
            ping = {"type": "ping"}
            try:
                ws.send(json.dumps(ping))
                with lock:
                    pings += 1
            except (WebSocketTimeoutException, WebSocketConnectionClosedException):
                break
            except Exception:
                break

            sleep_time = 12 + random.randint(1, 5)
            time.sleep(sleep_time)
    except Exception:
        pass
    finally:
        try:
            ws.close()
        except Exception:
            pass
        if connected:
            with lock:
                if connections > 0:
                    connections -= 1

# ----------------------------
# Runner
# ----------------------------
def run(thread_count, channel_name):
    global max_threads, channel, start_time, threads, thread_limit, channel_id
    max_threads = int(thread_count)
    channel = clean_channel_name(channel_name)
    thread_limit = Semaphore(max_threads)
    start_time = datetime.datetime.now()

    # Create proxies.txt if it doesn't exist
    if not os.path.exists(PROXY_FILE):
        print(f"'{PROXY_FILE}' not found. Creating it with the default test proxy.")
        with open(PROXY_FILE, "w", encoding="utf-8") as f:
            f.write("ankara1.buymobileproxy.com:1028:buymobileproxycom:igdir3849\n")

    # load proxies once up-front (exits if not found)
    load_proxies(PROXY_FILE)

    # channel_id will be fetched by the first successful thread
    threads = []

    stats_thread = Thread(target=show_stats, daemon=True)
    stats_thread.start()

    try:
        while True:
            for i in range(max_threads):
                thread_limit.acquire()
                t = Thread(target=connect)
                threads.append(t)
                t.daemon = True
                t.start()
                time.sleep(0.35)

            if stop:
                for _ in range(max_threads):
                    try:
                        thread_limit.release()
                    except Exception:
                        pass
                break
    except KeyboardInterrupt:
        pass

    for t in threads:
        try:
            t.join()
        except Exception:
            pass

# ----------------------------
# CLI entry
# ----------------------------
if __name__ == "__main__":
    try:
        os.system('cls' if os.name == 'nt' else 'clear')
        channel_input = input("Enter channel name or URL: ").strip()
        if not channel_input:
            print("Channel name needed.")
            sys.exit(1)

        while True:
            try:
                thread_input = int(input("Enter number of viewers: ").strip())
                if thread_input > 0:
                    break
                else:
                    print("Must be greater than 0")
            except ValueError:
                print("Enter a valid number")

        run(thread_input, channel_input)
    except KeyboardInterrupt:
        stop = True
        print("Stopping...")
        sys.exit(0)
