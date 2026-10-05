import shutil
import struct
import subprocess
import tempfile
import zipfile
from pathlib import Path
import zlib
import hashlib
import os

# Strings discovered in the supplied APK. We replace only known configuration URLs.
REPLACEMENTS = {
    "https://ragerussia.online/": "SERVER_WEBURL",
    "https://vizirs-shop.online/": "PROJECT_SHOP_URL",
    "https://vizirs-shop.online/client/": "PROJECT_CLIENT_URL",
}

def _uleb(n):
    out = bytearray()
    while True:
        b = n & 0x7f
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)

def patch_dex(data, mapping):
    # DEX string_ids entries point to string_data_item. We append new string data
    # and update each string_id offset, avoiding invasive relocation of the file.
    if data[:4] != b'dex\n':
        return data, []
    string_ids_size = struct.unpack_from("<I", data, 0x38)[0]
    string_ids_off = struct.unpack_from("<I", data, 0x3c)[0]
    buf = bytearray(data)
    changes = []
    for i in range(string_ids_size):
        off_pos = string_ids_off + 4*i
        off = struct.unpack_from("<I", buf, off_pos)[0]
        j = off
        length = 0
        shift = 0
        while True:
            x = buf[j]; j += 1
            length |= (x & 0x7f) << shift
            if x < 0x80: break
            shift += 7
        raw = bytes(buf[j:j+length])
        try:
            old = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if old not in mapping:
            continue
        new = mapping[old]
        if not isinstance(new, str):
            continue
        item = _uleb(len(new)) + new.encode("utf-8") + b"\\x00"
        new_off = len(buf)
        buf.extend(item)
        struct.pack_into("<I", buf, off_pos, new_off)
        changes.append((old, new))
    # Recalculate DEX signature/checksum.
    sha1 = hashlib.sha1(bytes(buf[32:])).digest()
    buf[12:32] = sha1
    checksum = zlib.adler32(bytes(buf[12:])) & 0xffffffff
    struct.pack_into("<I", buf, 8, checksum)
    return bytes(buf), changes

def patch_apk(template, output, cfg):
    mapping = {
        "https://ragerussia.online/": (cfg.get("weburl") or "https://example.com").rstrip("/") + "/",
        "https://vizirs-shop.online/": (cfg.get("shop_url") or "https://example.com/shop/").rstrip("/") + "/",
        "https://vizirs-shop.online/client/": (cfg.get("client_url") or "https://example.com/client/").rstrip("/") + "/",
    }
    with zipfile.ZipFile(template, "r") as zin, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            name = info.filename
            if name.startswith("META-INF/") and (name.endswith(".SF") or name.endswith(".RSA") or name.endswith(".DSA") or name == "MANIFEST.MF"):
                continue
            raw = zin.read(name)
            if name.endswith(".dex"):
                raw, _ = patch_dex(raw, mapping)
            zout.writestr(info, raw)
    # The modified APK is unsigned after META-INF removal. Sign it with the local key if available.
    sign_apk(output)

def sign_apk(apk):
    keystore = Path("data/launcher.keystore")
    alias = os.getenv("KEY_ALIAS", "raze")
    password = os.getenv("KEYSTORE_PASSWORD", "change-me-strong")
    if not keystore.exists():
        subprocess.run([
            "keytool","-genkeypair","-v","-keystore",str(keystore),
            "-storepass",password,"-keypass",password,
            "-alias",alias,"-keyalg","RSA","-keysize","2048",
            "-validity","10000","-dname","CN=CRMP Project Builder"
        ], check=True, stdout=subprocess.DEVNULL)
    subprocess.run([
        "jarsigner","-keystore",str(keystore),
        "-storepass",password,"-keypass",password,
        apk,alias
    ], check=True, stdout=subprocess.DEVNULL)
