import os
import re
import time
import logging
from pathlib import Path

import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType

from deployer import ProjectDeployer, ProjectInput
from launcher_patch import patch_apk

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
