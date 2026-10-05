import os
import re
import time
import logging
from pathlib import Path

import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType


try:
    from deployer import ProjectDeployer
except ModuleNotFoundError:
    # Single-file fallback for hosts that only start/upload main.py.
    import posixpath
    import tempfile
    import shutil
    from ftplib import FTP, error_perm
    import mysql.connector

    TEMPLATE_DIR = Path("template/crmp")

    class ProjectDeployer:
        def __init__(self, d):
            self.d = d
            self.ftp = None
            self.tmp = None

        def connect(self):
            self.ftp = FTP()
            self.ftp.connect(self.d["ftp_host"], self.d["ftp_port"], timeout=20)
            self.ftp.login(self.d["ftp_login"], self.d["ftp_password"])
            c = mysql.connector.connect(host=self.d["db_host"], port=self.d["db_port"], user=self.d["db_user"], password=self.d["db_password"], database=self.d["db_name"], connection_timeout=15)
            c.close()

        def prepare_local_template(self):
            if not TEMPLATE_DIR.exists():
                raise RuntimeError("template/crmp не найден")
            self.tmp = tempfile.mkdtemp(prefix="crmp-build-")
            dst = Path(self.tmp) / "server"
            shutil.copytree(TEMPLATE_DIR, dst)
            mysql_ini = dst / "scriptfiles/bykranin_mysql_settings.ini"
            if mysql_ini.exists():
                text = mysql_ini.read_text(encoding="utf-8", errors="ignore")
                for key, value in {"host":self.d["db_host"],"username":self.d["db_user"],"password":self.d["db_password"],"database":self.d["db_name"]}.items():
                    text = re.sub(rf"(?m)^{re.escape(key)}\s*=.*$", f"{key} = {value}", text)
                mysql_ini.write_text(text, encoding="utf-8")
            settings = dst / "scriptfiles/bykranin_server_settings.ini"
            if settings.exists():
                st=settings.read_text(encoding="utf-8", errors="ignore")
                for key,value in {"nameserver":self.d["name"],"vk":self.d.get("vk",""),"site":self.d.get("weburl",""),"tg":self.d.get("tg","")}.items():
                    st=re.sub(rf"(?m)^{re.escape(key)}\s*=.*$", f"{key} = {value}", st)
                settings.write_text(st, encoding="utf-8")
            cfg=dst/"server.cfg"
            if cfg.exists():
                ct=cfg.read_text(encoding="utf-8", errors="ignore")
                ct=re.sub(r"(?m)^hostname\s+.*$", f"hostname {self.d['name']}", ct)
                ct=re.sub(r"(?m)^weburl\s+.*$", f"weburl {self.d.get('weburl','')}", ct)
                ct=re.sub(r"(?m)^port\s+\d+$", f"port {self.d['port']}", ct)
                cfg.write_text(ct, encoding="utf-8")

        def _mkdir(self,path):
            if not path or path=="/": return
            cur=""
            for part in [x for x in path.split('/') if x]:
                cur += '/' + part
                try: self.ftp.cwd(cur)
                except error_perm:
                    try: self.ftp.mkd(cur)
                    except error_perm: pass

        def upload_server(self):
            root=self.d["remote_path"].rstrip('/') or '/'
            local=Path(self.tmp)/"server"
            self._mkdir(root)
            for path in local.rglob('*'):
                rel=path.relative_to(local).as_posix(); remote=posixpath.join(root,rel)
                if path.is_dir(): self._mkdir(remote)
                else:
                    self._mkdir(posixpath.dirname(remote))
                    with open(path,'rb') as f: self.ftp.storbinary(f"STOR {remote}",f)

        def import_database(self):
            sql=Path("template/crmp/bd.sql")
            if not sql.exists(): raise RuntimeError("template/crmp/bd.sql не найден")
            conn=mysql.connector.connect(host=self.d["db_host"],port=self.d["db_port"],user=self.d["db_user"],password=self.d["db_password"],database=self.d["db_name"],connection_timeout=20)
            cur=conn.cursor(); raw=sql.read_text(encoding='utf-8',errors='ignore')
            # Use mysql-connector multi=True when available.
            try:
                for result in cur.execute(raw, multi=True):
                    if result.with_rows: result.fetchall()
            except TypeError:
                for stmt in [x.strip() for x in raw.split(';') if x.strip()]: cur.execute(stmt)
            conn.commit(); cur.close(); conn.close()

try:
    from launcher_patch import patch_apk
except ModuleNotFoundError:
    def patch_apk(*args, **kwargs):
        raise RuntimeError("launcher_patch.py не загружен; серверная часть работает, но APK пока не собирается")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

TOKEN = os.getenv("VK_TOKEN", "").strip()
OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x.isdigit()}
DATA = Path("data")
PROJECTS = Path("projects")
TEMPLATE_APK = Path("template/launcher-template.apk")
DATA.mkdir(exist_ok=True)
PROJECTS.mkdir(exist_ok=True)
Path("template").mkdir(exist_ok=True)

sessions = {}

def is_owner(uid):
    return uid in OWNER_IDS

def send(vk, peer, text):
    return vk.messages.send(peer_id=peer, random_id=int(time.time()*1000000) % 2147483647, message=text)

def menu():
    return (
        "🤖 CRMP PROJECT BUILDER\n\n"
        "/create — создать проект\n"
        "/status — текущий процесс\n"
        "/cancel — отменить\n"
        "/help — помощь\n\n"
        "Администратору:\n"
        "/settemplate — инструкция по установке APK-шаблона"
    )

def parse_value(text):
    return text.strip()

def start_create(vk, peer, uid):
    sessions[peer] = {"uid": uid, "step": "name", "data": {}}
    send(vk, peer, "🛠 Создание проекта\\n\\nВведите название проекта:")

def next_step(vk, peer, state, value):
    step = state["step"]
    d = state["data"]
    if step == "name":
        d["name"] = value
        state["step"] = "ip"
        send(vk, peer, "Введите IP или домен сервера:")
    elif step == "ip":
        d["ip"] = value
        state["step"] = "port"
        send(vk, peer, "Введите порт сервера:")
    elif step == "port":
        if not value.isdigit() or not 1 <= int(value) <= 65535:
            return send(vk, peer, "❌ Порт должен быть числом от 1 до 65535.")
        d["port"] = int(value)
        state["step"] = "ftp_host"
        send(vk, peer, "FTP/SFTP host:")
    elif step == "ftp_host":
        d["ftp_host"] = value
        state["step"] = "ftp_port"
        send(vk, peer, "FTP/SFTP порт (обычно 21 для FTP или 22 для SFTP):")
    elif step == "ftp_port":
        if not value.isdigit():
            return send(vk, peer, "❌ Введите номер порта.")
        d["ftp_port"] = int(value)
        state["step"] = "ftp_login"
        send(vk, peer, "FTP/SFTP логин:")
    elif step == "ftp_login":
        d["ftp_login"] = value
        state["step"] = "ftp_password"
        send(vk, peer, "FTP/SFTP пароль:")
    elif step == "ftp_password":
        d["ftp_password"] = value
        state["step"] = "remote_path"
        send(vk, peer, "Удалённая папка проекта на FTP (например /server):")
    elif step == "remote_path":
        d["remote_path"] = value
        state["step"] = "db_host"
        send(vk, peer, "MySQL host:")
    elif step == "db_host":
        d["db_host"] = value
        state["step"] = "db_port"
        send(vk, peer, "MySQL порт (обычно 3306):")
    elif step == "db_port":
        if not value.isdigit():
            return send(vk, peer, "❌ Введите номер порта.")
        d["db_port"] = int(value)
        state["step"] = "db_name"
        send(vk, peer, "Название базы данных:")
    elif step == "db_name":
        d["db_name"] = value
        state["step"] = "db_user"
        send(vk, peer, "Пользователь MySQL:")
    elif step == "db_user":
        d["db_user"] = value
        state["step"] = "db_password"
        send(vk, peer, "Пароль MySQL:")
    elif step == "db_password":
        d["db_password"] = value
        state["step"] = "weburl"
        send(vk, peer, "Ссылка сайта/форума (можно -):")
    elif step == "weburl":
        d["weburl"] = "" if value == "-" else value
        state["step"] = "vk"
        send(vk, peer, "VK проекта (можно -):")
    elif step == "vk":
        d["vk"] = "" if value == "-" else value
        state["step"] = "tg"
        send(vk, peer, "Telegram проекта (можно -):")
    elif step == "tg":
        d["tg"] = "" if value == "-" else value
        state["step"] = "confirm"
        summary = (
            f"Проверьте данные:\n\n"
            f"Название: {d['name']}\nСервер: {d['ip']}:{d['port']}\n"
            f"FTP: {d['ftp_host']}:{d['ftp_port']}\n"
            f"Папка: {d['remote_path']}\n"
            f"MySQL: {d['db_host']}:{d['db_port']} / {d['db_name']}\n\n"
            "Напишите ДА для запуска или НЕТ для отмены."
        )
        send(vk, peer, summary)
    elif step == "confirm":
        if value.lower() in ("да", "yes", "y"):
            run_project(vk, peer, state)
        else:
            sessions.pop(peer, None)
            send(vk, peer, "❌ Создание отменено.")

def run_project(vk, peer, state):
    d = state["data"]
    sessions[peer] = {"uid": state["uid"], "step": "running", "data": d}
    try:
        send(vk, peer, "🔎 Проверяю FTP/SFTP и MySQL...")
        deployer = ProjectDeployer(d)
        deployer.connect()
        send(vk, peer, "✅ Подключения проверены.\n\n📦 Загружаю CRMP-мод...")
        deployer.prepare_local_template()
        deployer.upload_server()
        send(vk, peer, "🗄 Импортирую базу данных...")
        deployer.import_database()
        send(vk, peer, "⚙️ Настраиваю server.cfg и параметры мода...")
        deployer.configure()
        send(vk, peer, "🚀 Проект загружен на хостинг.")
        if TEMPLATE_APK.exists():
            out = PROJECTS / re.sub(r"[^A-Za-z0-9_-]+", "_", d["name"]) / "launcher.apk"
            out.parent.mkdir(parents=True, exist_ok=True)
            patch_apk(TEMPLATE_APK, out, d)
            send(vk, peer, f"📱 Лаунчер собран: {out.name}\n\n"
                           "На следующем шаге его можно автоматически отправлять пользователю как документ VK.")
        else:
            send(vk, peer, "⚠️ APK-шаблон пока не установлен. Сервер готов, лаунчер пока не собирался.")
        send(vk, peer, f"🎉 Проект «{d['name']}» готов.")
    except Exception as e:
        logging.exception("project failed")
        send(vk, peer, f"❌ Ошибка создания проекта:\n{type(e).__name__}: {e}")
    finally:
        sessions.pop(peer, None)

def main():
    if not TOKEN:
        raise SystemExit("VK_TOKEN is not set")
    vk_session = vk_api.VkApi(token=TOKEN)
    vk = vk_session.get_api()
    longpoll = VkLongPoll(vk_session)
    logging.info("RAZE CRMP PROJECT BUILDER started")
    for event in longpoll.listen():
        if event.type != VkEventType.MESSAGE_NEW or not event.text:
            continue
        uid = event.user_id
        peer = event.peer_id
        text = event.text.strip()
        low = text.lower()
        try:
            if low in ("/start", "/help", "бот", "помощь"):
                send(vk, peer, menu())
                continue
            if low == "/create":
                start_create(vk, peer, uid)
                continue
            if low == "/cancel":
                sessions.pop(peer, None)
                send(vk, peer, "❌ Текущая операция отменена.")
                continue
            if low == "/status":
                s = sessions.get(peer)
                send(vk, peer, "🟢 Проект создаётся..." if s else "ℹ️ Активных операций нет.")
                continue
            if low == "/settemplate":
                if not is_owner(uid):
                    send(vk, peer, "⛔ Команда доступна только владельцу.")
                else:
                    send(vk, peer, "Пришли этому боту APK-шаблон документом VK.\n"
                                   "Сейчас автоматическая загрузка документа в шаблон не включена в MVP; "
                                   "положи файл вручную в template/launcher-template.apk.")
                continue
            if peer in sessions and sessions[peer]["step"] not in ("running",):
                next_step(vk, peer, sessions[peer], parse_value(text))
            else:
                send(vk, peer, "Не понял команду. Напиши /help.")
        except Exception:
            logging.exception("event error")

if __name__ == "__main__":
    main()
